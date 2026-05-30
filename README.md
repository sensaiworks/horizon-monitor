# horizon-monitor

A background monitoring agent that watches a Windows remote desktop running inside the
**Omnissa Horizon Client**. It screenshots the session via the
[`horizon-mcp`](../horizon-mcp) MCP server, extracts chat messages (Microsoft Teams,
Symphony) with Claude Vision, notifies you when someone talks to you, and accumulates
everything into a local RAG database so you can ask "what did John say about the
deployment this morning?".

> Read-only by design: it only takes screenshots and lists/focuses windows. The single
> exception is the optional "Unlock Remote Desktop" tray action, which types your
> `HORIZON_PASSWORD` into the Horizon login prompt.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, design rationale, and
implementation notes.

## Requirements

- Python 3.11+ (uses the stdlib `tomllib`; on older Pythons the `tomli` backport is
  installed automatically from `requirements.txt`)
- Node.js (to run the sibling `horizon-mcp` server)
- An `ANTHROPIC_API_KEY`

## Setup

```powershell
cd C:\github\horizon-monitor
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env              # then fill in ANTHROPIC_API_KEY
copy config.example.toml config.toml  # then adjust paths / settings
```

Both `.env` and `config.toml` are gitignored — keep your secrets and machine-specific
paths out of version control.

## Usage

```powershell
# Production: system tray icon (launches stopped — click "Start" to begin monitoring)
.venv\Scripts\pythonw main.py tray
# or double-click "Start horizon-monitor.bat"

# Debug the poll loop only — no LLM calls, prints changed/unchanged each tick
python main.py monitor --dry-run

# Headless monitoring with Vision extraction (terminal)
python main.py monitor

# One-shot question over recorded conversations
python main.py query "what did John say about the deployment?"

# Interactive Q&A REPL
python main.py agent
```

## Configuration

All tunables live in `config.toml` (poll interval, change threshold, monitored window
titles, models, embedding provider, notification cooldown). API keys and the optional
remote password live in `.env`. See `config.example.toml` and `.env.example` for the
full set of options.

## Embeddings

The RAG pipeline uses `voyage-3` when `embedding_provider = "voyage"` and `VOYAGE_API_KEY`
is set; otherwise it falls back automatically to the offline `all-MiniLM-L6-v2`
sentence-transformers model (~100 MB on first download). ChromaDB persists to
`./data/chromadb` (gitignored).
