# horizon-monitor — Design Document for Claude Sessions

## What this project is

A background monitoring agent that watches a Windows remote desktop session running inside
the **Omnissa Horizon Client** (`horizon-client.exe`). It uses the **horizon-mcp** MCP server
(a sibling project at `C:\github\horizon-mcp`) to take screenshots and interact with the
desktop. It extracts conversation events from chat apps (Microsoft Teams, Symphony) visible
on the remote desktop, notifies the user when someone talks to them, and accumulates
everything into a local RAG database so an AI agent can answer questions like
"what did John say about the deployment this morning?".

## User context

- **User:** Dmitry Goubar (`dmitry-goubar` on GitHub)
- **Machine:** Windows 11, `C:\github\` is the projects root
- **Remote desktop:** Omnissa Horizon Client window, title contains "PVDI"
- **Chat apps in the remote desktop:** Microsoft Teams and Symphony
- **horizon-mcp repo:** `C:\github\horizon-mcp` — already built and working
- **This repo:** `C:\github\horizon-monitor`

## How horizon-mcp works (dependency)

horizon-mcp is a stdio MCP server started with:
```
node C:\github\horizon-mcp\dist\index.js
```

It exposes 7 tools over the MCP protocol:
- `screenshot` — returns base64 PNG of screen(s)
- `click(x, y, button?)` — mouse click at coordinates
- `double_click(x, y)` — double-click
- `type_text(text)` — types text into focused window
- `press_key(key)` — sends named key (Enter, Escape, Tab, F1–F12, Ctrl+C, …)
- `list_windows()` — returns JSON list of `{Id, Name, MainWindowTitle}`
- `focus_window(target)` — brings window to foreground by PID or partial title

In this project we use it **read-only** for monitoring: `screenshot`, `list_windows`,
`focus_window`. We do not automate clicks or typing unless the user explicitly requests
an "assist" feature later.

The Python MCP client SDK (`mcp` package) spawns the server as a subprocess and
communicates via stdin/stdout. See `src/mcp_client.py`.

## Architecture

```
[Omnissa Horizon Client window]
         │
         │ screenshots via horizon-mcp
         ▼
┌─────────────────────────────────────────────────────────┐
│                    horizon-monitor                       │
│                                                          │
│  ┌──────────────┐    ┌───────────────────────────────┐  │
│  │  Poller      │───▶│  Change Detector               │  │
│  │  (asyncio,   │    │  (perceptual hash, imagehash)  │  │
│  │   every 3s)  │    │  Only passes changed frames    │  │
│  └──────┬───────┘    └──────────────┬────────────────┘  │
│         │                           │ changed?           │
│         ▼                           ▼                    │
│  ┌──────────────┐    ┌───────────────────────────────┐  │
│  │  MCP Client  │    │  Extractor                    │  │
│  │  (horizon-   │    │  Claude Haiku vision API      │  │
│  │   mcp stdio) │    │  → list[MessageEvent]         │  │
│  └──────────────┘    └──────────────┬────────────────┘  │
│                                     │                    │
│                       ┌─────────────┴──────────────┐    │
│                       ▼                             ▼    │
│            ┌─────────────────┐         ┌──────────────┐ │
│            │  Notifier        │         │  RAG Pipeline│ │
│            │  plyer toast     │         │  ChromaDB +  │ │
│            │  on @mention     │         │  embeddings  │ │
│            └─────────────────┘         └──────┬───────┘ │
│                                               │          │
│                                    ┌──────────▼───────┐  │
│                                    │  Query Agent      │  │
│                                    │  Claude Sonnet    │  │
│                                    │  + RAG retrieval  │  │
│                                    └──────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Data models (`src/models.py`)

All events are Pydantic v2 models. The key ones:

- `MessageEvent` — a single chat message extracted from a screenshot
  - `timestamp`, `speaker`, `message`, `app` ("teams"|"symphony"|"unknown")
  - `window_title`, `directed_at_user: bool`

- `ScreenState` — snapshot of a single poll cycle
  - `timestamp`, `screenshot_hash`, `window_title`
  - `changed: bool`, `extracted_events: list[MessageEvent]`

- `ProcessInfo` — from `list_windows()` output
  - `pid`, `name`, `title`

## File layout

```
horizon-monitor/
  src/
    __init__.py
    models.py        # Pydantic data models — implement first
    mcp_client.py    # Async wrapper around MCP Python SDK — implement second
    poller.py        # Poll loop + change detection — implement third
    extractor.py     # Claude Vision → structured events — implement fourth
    rag.py           # ChromaDB ingest + query — implement fifth
    notifier.py      # Windows toast notifications — implement sixth
    agent.py         # CLI query agent — implement last
  data/              # ChromaDB persists here (gitignored)
  config.toml        # All tunable settings
  .env               # ANTHROPIC_API_KEY (gitignored)
  .env.example       # Template (committed)
  main.py            # Entry point: python main.py [monitor|query|agent]
  requirements.txt
  .gitignore
  CLAUDE.md          # This file
  README.md
```

## Implementation order

Build and test each layer before moving to the next. Each step is independently runnable.

### Step 1 — MCP client + poll loop (no LLM)
**Goal:** prove the loop works before spending API tokens.

1. Implement `src/mcp_client.py` — connects to horizon-mcp, exposes async methods
2. Implement `src/poller.py` — poll loop, take screenshot, compute hash, detect change
3. Run `python main.py monitor --dry-run` — should print "changed/unchanged" every 3s
4. Verify it correctly identifies the Horizon window using `list_windows`

Key detail: the poller focuses the Horizon window before each screenshot to ensure
it's visible, then restores focus. Use `focus_window("PVDI")` or the PID from
`list_windows()`.

### Step 2 — Claude Vision extraction
**Goal:** extract structured events from changed screenshots.

1. Implement `src/extractor.py`
2. The extractor sends the screenshot + a structured prompt to Claude Haiku
3. Prompt asks for JSON: `[{speaker, message, app, directed_at_user}]`
4. Returns `list[MessageEvent]`
5. Test with a real screenshot of Teams/Symphony open

Claude Haiku model ID: `claude-haiku-4-5-20251001`
Use `anthropic.Anthropic()` client with `messages.create()` and an image content block.

### Step 3 — RAG pipeline
**Goal:** persist events and enable semantic search.

1. Implement `src/rag.py`
2. ChromaDB collection: `"horizon_events"`
3. Document = message text; metadata = speaker, app, timestamp, directed_at_user
4. Embeddings: use `voyage-3` via `voyageai` package OR `all-MiniLM-L6-v2` via
   `sentence-transformers` (offline fallback). Prefer voyage-3 if API key available.
5. Ingest each `MessageEvent` after extraction
6. Test: query "what did anyone say about deployments"

### Step 4 — Notifications
**Goal:** alert user when someone talks to them.

1. Implement `src/notifier.py`
2. Trigger when `MessageEvent.directed_at_user == True`
3. Deduplicate: keep a set of `(speaker, message[:50])` hashes seen in last 5 min
4. Use `plyer.notification.notify()` for Windows toast
5. Test by simulating a directed MessageEvent

### Step 5 — Query agent
**Goal:** natural language Q&A over accumulated conversations.

1. Implement `src/agent.py`
2. CLI: `python main.py query "what did John say about the outage?"`
3. Retrieve top-k chunks from ChromaDB
4. Pass as context to Claude Sonnet with a system prompt
5. Stream the answer to stdout

Claude Sonnet model ID: `claude-sonnet-4-6`
Use prompt caching (`cache_control`) on the system prompt — conversations accumulate
and the system prompt will grow. See Anthropic docs on cache_control.

### Step 6 — Interactive agent mode
**Goal:** persistent chat interface that can also trigger horizon-mcp actions.

1. `python main.py agent` — REPL loop
2. Agent has access to RAG + can call horizon-mcp tools (screenshot, focus, etc.)
3. This is where it gets genuinely useful: "show me what's on screen" + "summarize
   today's Teams conversations"

## Configuration (`config.toml`)

```toml
[mcp]
server_path = "C:\\github\\horizon-mcp\\dist\\index.js"
command = "node"

[polling]
interval_seconds = 3
change_threshold = 10   # imagehash Hamming distance; 0=identical, 64=max different

[windows]
monitor_titles = ["PVDI", "horizon-client"]  # partial window title matches

[claude]
vision_model = "claude-haiku-4-5-20251001"
query_model  = "claude-sonnet-4-6"
max_tokens   = 1024

[rag]
db_path            = "./data/chromadb"
collection_name    = "horizon_events"
embedding_provider = "voyage"   # "voyage" or "local"
top_k              = 8

[notifications]
enabled           = true
mention_keywords  = []   # extra keywords that count as "directed at user"
cooldown_minutes  = 5    # deduplicate window

[user]
display_name = "Dmitry"   # used in prompts to detect self-mentions
```

## Environment variables

```
ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...        # optional, only if embedding_provider = "voyage"
HORIZON_PASSWORD=...         # optional, only for the tray "Unlock Remote Desktop" action
```

`HORIZON_PASSWORD` is the remote desktop login password. It is read only by the tray
unlock worker (`src/tray.py`), typed into the Horizon session via `type_text`, and never
logged or sent to any API. Leave it blank to disable the auto-unlock feature. `.env` is
gitignored — never commit it.

## Key design decisions and WHY

### Why Python, not Node.js?
ChromaDB, sentence-transformers, and voyageai all have first-class Python SDKs.
The MCP Python client SDK is also well-supported. Node would require workarounds.

### Why Claude Haiku for vision, not Sonnet?
Screenshot analysis runs every time pixels change — potentially dozens of times per
minute. Haiku is ~20x cheaper and fast enough for extraction. Sonnet is reserved for
the query agent where answer quality matters.

### Why perceptual hashing before calling Claude?
Without it, every 3-second poll calls the vision API even when nothing changed
(static screen, locked desktop, etc.). `imagehash.average_hash()` costs ~0ms and
filters out 90%+ of frames.

### Why ChromaDB, not Pinecone or Weaviate?
Local-first, no server process, persistent to disk, zero config. This runs on
Dmitry's Windows machine — cloud vector DBs add latency and require internet.

### Why NOT use LangChain/CrewAI/AutoGen?
The workflow is linear and deterministic. Frameworks add abstraction layers that
make debugging harder and add token overhead. We use Claude API directly.
If multi-step agent reasoning is needed later, add LangGraph at that point.

### Why commit dist/index.js in horizon-mcp?
horizon-mcp's compiled output is committed so horizon-monitor can run it without
needing a TypeScript build step. `npm run build` only needed if horizon-mcp source changes.

### Screenshot focus strategy
`focus_before_shot` defaults to `false`. The `CopyFromScreen` GDI+ call captures any
visible window without needing it to be focused. Only set `true` if Horizon fails to
render unless it is the active window (rare).

The original `focus_before_shot = true` default caused PowerShell console windows to
flash and steal focus every 3 s. Fixed in horizon-mcp by adding `windowsHide: true`
and `-WindowStyle Hidden` to every `ps()` call. When `focus_before_shot = false`,
`list_windows()` is also skipped, halving MCP tool calls per cycle.

## Extraction prompt (reference)

```python
EXTRACTION_PROMPT = """
You are analyzing a screenshot of a Windows remote desktop showing a chat application
(Microsoft Teams or Symphony). Extract all visible chat messages.

Return a JSON array only, no explanation:
[
  {
    "speaker": "Full Name or username",
    "message": "exact message text",
    "app": "teams" | "symphony" | "unknown",
    "directed_at_user": true | false
  }
]

directed_at_user is true if: the message @mentions '{user}', uses their first name
directly, or is a direct/private message to them. Otherwise false.

If no chat messages are visible, return [].
"""
```

Replace `{user}` with `config.user.display_name` at runtime.

## Known issues / gotchas

1. **horizon-mcp combined screenshot returns empty** — When called with no `screen`
   argument (all screens), the server returns an empty image on some configurations.
   Always pass `screen=0` (primary) or `screen=1` (secondary) explicitly.

2. **PowerShell startup latency** — Each MCP tool call spawns a new PowerShell process
   (~300ms). For the poller, this means ~300ms overhead per screenshot call. This is
   acceptable for a 3s poll interval but means real-time reaction isn't possible.

3. **Horizon window title** — The window title is "PVDI 2 DEV CT" (may vary by
   environment). Use partial match "PVDI" in config. The PID is 20892 in current
   session but changes on reconnect — always resolve by title, not PID.

4. **Symphony vs Teams UI** — Symphony uses Electron; Teams uses WebView2. Both render
   chat as scrollable lists. Claude Vision can distinguish them by the app chrome.

5. **Lock screen** — When the remote desktop is locked ("Press Ctrl+Alt+Delete to unlock"),
   the extractor should return [] and the poller should not count this as a change worth
   notifying. Detect via the lock screen pattern in the extraction prompt or by checking
   if the screenshot shows a black screen with a clock.

## What has been built so far

- [x] Repository scaffolded
- [x] CLAUDE.md written
- [x] models.py — Pydantic data models
- [x] config.toml / config.example.toml — settings (config.toml gitignored)
- [x] requirements.txt
- [x] src/mcp_client.py — Step 1: async MCP client (screenshot, list_windows, focus_window)
- [x] src/poller.py — Step 1: poll loop + imagehash change detection + stop/pause events
- [x] src/extractor.py — Step 2: Claude Haiku vision → list[MessageEvent]
- [x] src/notifier.py — Step 4: Windows toast on directed_at_user with cooldown dedup
- [x] src/tray.py — system tray icon (pystray) with Start/Pause/Resume/Stop/Quit
- [x] main.py — tray, monitor, monitor --dry-run, query (stub), agent (stub)
- [x] Start horizon-monitor.bat — double-click launcher / desktop shortcut
- [x] .claude/commands/commit-monitor.md — `/commit-monitor` skill to commit+push this repo only
- [x] src/rag.py — Step 3: ChromaDB ingest + voyage/local embeddings (auto-fallback to local)
- [x] src/agent.py — Step 5: CLI query agent + interactive REPL with RAG retrieval & streaming
- [x] README.md — quick-start and command reference
- [ ] Step 6: give the interactive agent direct horizon-mcp tool access (screenshot/focus)

## Running the project

```powershell
cd C:\github\horizon-monitor
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # fill in ANTHROPIC_API_KEY

# Production (tray icon; launches stopped — click "Start" to begin monitoring):
.venv\Scripts\pythonw main.py tray
# or double-click: Start horizon-monitor.bat

# Debug (terminal, no LLM):
python main.py monitor --dry-run

# Debug (terminal, with Vision extraction):
python main.py monitor

# Query (Step 5, not yet implemented):
python main.py query "what did John say about the deployment?"
```
