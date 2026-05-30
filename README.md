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
| Vector store      | ChromaDB (persistent, local)                        |
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
```

## Configuration

All tunables live in `config.toml`: poll interval, change-detection threshold, monitored
window titles, model IDs, embedding provider, and notification cooldown. API keys and the
optional remote-desktop password live in `.env`. See `config.example.toml` and
`.env.example` for the full set of options.

**Embeddings.** The RAG pipeline uses `voyage-3` when `embedding_provider = "voyage"` and
`VOYAGE_API_KEY` is set; otherwise it falls back automatically to the offline
`all-MiniLM-L6-v2` sentence-transformers model (~100 MB on first download). ChromaDB
persists to `./data/chromadb` (gitignored).

## Privacy & safety

- **Read-only by default** — the monitor only screenshots, lists, and focuses windows.
- **Local storage** — extracted chat lives in a local ChromaDB directory, never uploaded.
- **Optional unlock only** — `HORIZON_PASSWORD` is read solely by the tray "Unlock Remote
  Desktop" action, typed into the Horizon login prompt, and never logged or sent to any
  API. Leave it blank to disable the feature entirely.

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
