# horizon-monitor

> A background agent that *watches* a locked-down remote desktop the only way it can —
> through pixels — and turns the chat happening inside it into searchable, queryable memory.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![License](https://img.shields.io/badge/license-MIT-green)

Many corporate setups run chat apps (Microsoft Teams, Symphony) **inside** a virtual
desktop — an Omnissa Horizon session — that you can't script against directly: no APIs,
no accessibility tree, just a window full of rendered pixels. `horizon-monitor` treats
that constraint as the design: it periodically screenshots the session, uses a vision
model to read the chat on screen, alerts you when someone is talking *to you*, and files
every message into a local vector database so you can later ask, in plain English,
*"what did John say about the deployment this morning?"*

It captures the session through [`horizon-mcp`](https://github.com/dmitry-goubar/horizon-mcp),
a small companion MCP server that exposes screenshot / window controls over stdio.

> **A note on scope.** This is a personal demo project by
> [Dmitry Goubar](https://github.com/dmitry-goubar) — built to explore screen-grounded
> agents, cost-aware vision pipelines, and local-first RAG. It is **read-only by design**:
> it only screenshots and focuses windows. The single write action is an optional,
> opt-in "Unlock Remote Desktop" tray command.

---

## How it works

```
 ┌──────────────────────────┐
 │  Omnissa Horizon session │   chat apps render here (Teams / Symphony)
 └────────────┬─────────────┘
              │ screenshot (every ~3s, via horizon-mcp)
              ▼
   ┌──────────────────┐   perceptual hash — did the screen actually change?
   │  Poller          │──────────────┐ no  → drop the frame, spend nothing
   └────────┬─────────┘              │
            │ yes (changed frame)    ▼
            ▼               (90%+ of frames stop here)
   ┌──────────────────┐
   │  Extractor       │   Claude Haiku (vision) → structured chat messages
   └────────┬─────────┘
            ├───────────────► Notifier   — Windows toast when a message is for you
            ▼
   ┌──────────────────┐
   │  RAG pipeline    │   ChromaDB + embeddings (Voyage, or offline fallback)
   └────────┬─────────┘
            ▼
   ┌──────────────────┐
   │  Query agent     │   Claude Sonnet, streamed answers over your chat history
   └──────────────────┘
```

The whole thing lives behind a Windows **system-tray icon** with status, recent
messages, an "Ask…" dialog, and start/pause/stop controls.

## Why it's built this way

The interesting parts of this project are the constraints, not the line count:

- **Pixels are the only interface.** The VDI is locked down, so chat is read with a
  vision model over screenshots rather than any API. The extractor also detects the
  Windows lock screen and goes quiet instead of hallucinating messages.
- **A cheap gate in front of the expensive model.** A perceptual hash
  (`imagehash.average_hash`) costs ~0 ms and filters out the 90 %+ of 3-second polls
  where nothing changed (static screen, idle desktop). The vision API only ever sees
  frames that actually differ.
- **The right model for each job.** High-frequency screen reading uses **Claude Haiku**
  (fast, ~20× cheaper); the low-frequency, quality-sensitive Q&A uses **Claude Sonnet**.
- **Local-first and private.** Conversations are embedded and stored in **ChromaDB** on
  disk. With no `VOYAGE_API_KEY`, it transparently falls back to an offline
  sentence-transformers model — no chat content has to leave the machine to be searchable.
- **No agent framework.** The pipeline is linear and deterministic, so it calls the
  Claude API directly instead of dragging in LangChain/CrewAI/AutoGen abstractions.
- **Decoupled capture.** Screen/window control is a separate, reusable MCP server
  (`horizon-mcp`), so the monitoring logic never touches OS automation directly.

## Tech stack

| Area              | Choice                                              |
| ----------------- | --------------------------------------------------- |
| Language          | Python 3.11+ (`asyncio`)                            |
| Vision extraction | Claude Haiku                                         |
| Query agent       | Claude Sonnet (streaming, prompt-cached system)     |
| Screen capture    | `horizon-mcp` MCP server (stdio) + MCP Python SDK   |
| Change detection  | `imagehash` perceptual hashing                      |
| Vector store      | ChromaDB (semantic search, persistent, local)       |
| Structured store  | SQLite (timestamps, channels, exact/temporal queries) |
| Embeddings        | `voyage-3` (API) or `all-MiniLM-L6-v2` (offline)    |
| Data models       | Pydantic v2                                         |
| UI                | `pystray` tray icon + Tk "Ask…" dialog              |
| Notifications     | `plyer` Windows toasts                              |

## Requirements

- **Python 3.11+** — uses the stdlib `tomllib`; on older Pythons the `tomli` backport is
  installed automatically from `requirements.txt`.
- **Node.js** — to run the companion `horizon-mcp` server.
- An **`ANTHROPIC_API_KEY`**.

## Setup

```powershell
git clone https://github.com/dmitry-goubar/horizon-monitor.git
cd horizon-monitor

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env                 # then fill in ANTHROPIC_API_KEY
copy config.example.toml config.toml   # then adjust paths / settings
```

Both `.env` and `config.toml` are gitignored — keep secrets and machine-specific paths
out of version control.

## Usage

```powershell
# Production: system-tray icon (launches stopped — click "Start" to begin monitoring)
.venv\Scripts\pythonw main.py tray
# ...or double-click "Start horizon-monitor.bat"

# Debug the poll loop only — no LLM calls, prints changed/unchanged each tick
python main.py monitor --dry-run

# Headless monitoring with vision extraction (terminal)
python main.py monitor

# One-shot question over the recorded conversations
python main.py query "what did John say about the deployment?"

# Interactive Q&A REPL
python main.py agent

# Remote control (opt-in; requires [control].enabled = true)
python main.py remote foreground
python main.py remote launch "Microsoft Teams"
python main.py remote unlock

# Code-editing bridge: pull a file out of remote VS Code, edit locally, push it back
python main.py remote open "src/app.py"             # focus the file in the remote editor
python main.py remote read-file --out data/pull.txt # copy the whole file to a local file
python main.py remote write-file data/pull.txt --save
```

## Packaging a portable .exe

A single-file Windows build is produced with [PyInstaller](https://pyinstaller.org/)
from `horizon-monitor.spec`:

```powershell
.venv\Scripts\Activate.ps1
pip install pyinstaller
pyinstaller horizon-monitor.spec     # → dist\horizon-monitor.exe  (~115 MB)
# or just:  .\build_exe.ps1
```

The result is a self-contained **windowed** app. Double-clicking it launches the desktop
UI; it reads `config.toml` / `.env` from the folder the `.exe` sits in, auto-creating
`config.toml` from a bundled template on first run. Drop a `.env` (with `ANTHROPIC_API_KEY`,
optionally `VOYAGE_API_KEY` / `HORIZON_PASSWORD` / `TELEGRAM_*`) beside the `.exe`, or fill
them in from the **Settings** tab.

To keep the download small, the build **excludes PyTorch / sentence-transformers**, so RAG
embeddings use **Voyage** (`VOYAGE_API_KEY`); offline local embeddings remain available when
running from source. The app still needs `horizon-mcp` (Node) reachable at the path in
`config.toml`.

## Configuration

All tunables live in `config.toml`: poll interval, change-detection threshold, monitored
window titles, model IDs, embedding provider, and notification cooldown. API keys and the
optional remote-desktop password live in `.env`. See `config.example.toml` and
`.env.example` for the full set of options.

**Storage.** Each extracted message is written to two local stores under `./data/`
(gitignored), keyed on the same content hash so they stay in sync:

- **ChromaDB** (`./data/chromadb`) — embeddings for semantic search.
- **SQLite** (`./data/events.db`) — the structured source of truth, retaining the
  message's on-screen time and channel for exact and time-ranged queries; it also acts
  as the dedup gate so each message is embedded and notified exactly once.

**Embeddings.** The RAG pipeline uses `voyage-3` when `embedding_provider = "voyage"` and
`VOYAGE_API_KEY` is set; otherwise it falls back automatically to the offline
`all-MiniLM-L6-v2` sentence-transformers model (~100 MB on first download).

## Remote control (opt-in)

Monitoring is read-only, but the tool can also *drive* the session when you ask it to —
useful when a chat app is minimised inside the remote, or the desktop is locked. Actions
are exposed in the tray and via `python main.py remote <action>`:

- **Unlock** — sends Ctrl+Alt+Del to the remote and enters `HORIZON_PASSWORD` (pasted, then
  the clipboard is restored so the secret doesn't linger).
- **Launch / activate an app** — drives the remote's own Start menu
  (`key_combo(["Win"])` → type name → Enter), since apps inside the VDI aren't local
  windows and can't be focused directly.
- **Bring Horizon to front**, **run a command**, **send a reply**.

These are **write actions that type/click into a corporate desktop and steal local focus**,
so they are gated behind `[control].enabled` (off by default), never run automatically, and
only fire when you trigger them.

### Code-editing bridge

Corporate VDIs often run an IDE (e.g. VS Code) but block AI assistants inside the session.
This bridge brings the AI to the code: it pulls a file out of the remote editor, lets a
local agent edit it, and pastes the whole document back.

The transport is the **clipboard**, not OCR — `Ctrl+A`/`Ctrl+C` in the remote editor copies
the *entire* file exactly, Horizon clipboard redirection syncs it to the local clipboard,
and `read-file` captures it losslessly (OCR would drop indentation and off-screen lines;
typing code back in would be mangled by autocomplete). `write-file` is the mirror: it stages
the new text, selects all, pastes, and optionally saves.

```powershell
python main.py remote open "src/app.py"              # 1. focus the file in remote VS Code
python main.py remote read-file --out data/pull.txt  # 2. pull it out losslessly
#    3. edit data/pull.txt locally (by hand or with an AI agent)
python main.py remote write-file data/pull.txt --save # 4. replace the remote document
```

This requires Horizon **clipboard redirection** to be enabled (it usually is); `read-file`
reports a clear error if the clipboard never updates after the copy. The remote **editor
pane** must hold focus so `Ctrl+A` selects the document.

### OCR read bridge (for one-way-clipboard / DLP sessions)

Many corporate VDIs allow paste **in** but block copy **out** (a one-way-clipboard DLP
policy). There the clipboard bridge above can't read — there is no lossless way to get text
off the remote — so the read path falls back to the screen itself: **screenshot + Windows
OCR**, scrolling the pane and stitching captures for content taller than one screen. The
write path is unchanged (paste-in is allowed).

```powershell
python main.py remote read-screen --out data/screen.txt           # OCR the screen
python main.py remote read-scroll 960,500 --out data/pane.txt      # scroll + stitch a pane
python main.py remote paste data/reply.txt --at 700,980 --double   # click-to-focus, paste-in
```

OCR is **lossy** — it confuses `1`/`l`/`I`, drops indentation, and mangles symbols — so treat
anything read back through this path as a **draft to verify**, never source-of-truth,
especially for code. (`--region` takes *actual* screen-pixel coordinates, which differ from
screenshot pixels under display scaling; omit it to OCR the full screen.)

## Privacy & safety

- **Read-only monitoring** — the polling loop only screenshots, lists, and focuses windows;
  it never types or clicks. Control actions are a separate, opt-in, user-triggered path.
- **Local storage** — extracted chat lives in local ChromaDB + SQLite files, never uploaded.
- **Secret handling** — `HORIZON_PASSWORD` is read only by the unlock action, pasted into the
  login prompt, cleared from the clipboard afterward, and never logged or sent to any API.
  Leave it blank (and `[control].enabled = false`) to disable the write path entirely.

## Project status

Working end to end: capture → change detection → vision extraction → notifications → RAG
ingest → query agent, all wrapped in a tray UI. Planned next: give the interactive agent
direct access to `horizon-mcp` tools (e.g. "show me what's on screen right now" alongside
"summarize today's Teams threads").

## Author

**Dmitry Goubar** — [@dmitry-goubar](https://github.com/dmitry-goubar)

Companion project: [`horizon-mcp`](https://github.com/dmitry-goubar/horizon-mcp) — the
MCP server that provides screenshot and window control.

## License

Released under the [MIT License](LICENSE). © 2026 Dmitry Goubar.
