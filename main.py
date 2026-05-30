"""
Entry point for horizon-monitor.

Commands:
  python main.py tray              # system tray icon (production mode)
  python main.py monitor           # headless monitoring loop (terminal)
  python main.py monitor --dry-run # test poll loop without LLM
  python main.py query "..."       # one-shot question
  python main.py agent             # interactive REPL

Configuration is loaded from config.toml in the project root.
API keys are loaded from .env (copy from .env.example).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    import tomli as tomllib

import click
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.toml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise click.ClickException(
            f"{CONFIG_PATH.name} not found — copy config.example.toml to config.toml and edit it"
        )
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def require_api_key() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise click.ClickException(
            "ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in"
        )
    return api_key


@click.group()
def cli():
    pass


@cli.command()
@click.option("--dry-run", is_flag=True, help="Poll without calling LLM or notifying")
def monitor(dry_run: bool):
    """Start the monitoring loop."""
    config = load_config()
    if not dry_run:
        require_api_key()  # dry-run never calls the LLM, so the key is optional there
    asyncio.run(_run_monitor(config, dry_run))


async def _run_monitor(config: dict, dry_run: bool) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.poller import Poller
    from src.extractor import Extractor
    from src.rag import RAGPipeline

    mcp_cfg = config["mcp"]
    poll_cfg = config["polling"]
    win_cfg = config["windows"]
    claude_cfg = config["claude"]
    user_cfg = config["user"]
    rag_cfg = config["rag"]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    extractor = Extractor(
        api_key=api_key,
        model=claude_cfg["vision_model"],
        user_display_name=user_cfg["display_name"],
    )

    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()

    async with HorizonMCPClient(
        server_path=mcp_cfg["server_path"],
        command=mcp_cfg["command"],
    ) as client:
        poller = Poller(
            client=client,
            interval=poll_cfg["interval_seconds"],
            change_threshold=poll_cfg["change_threshold"],
            screen_index=poll_cfg["screen_index"],
            monitor_titles=win_cfg["monitor_titles"],
            focus_before_shot=poll_cfg["focus_before_shot"],
        )

        async def on_change(state, png: bytes) -> None:
            print(f"CHANGED  {state.screenshot_hash}  [{state.window_title}]", flush=True)
            events, is_locked = await extractor.extract(png, window_title=state.window_title)
            if is_locked:
                print("  [lock screen detected]", flush=True)
            for ev in events:
                tag = " @YOU" if ev.directed_at_user else ""
                print(f"  [{ev.app}] {ev.speaker}: {ev.message[:80]}{tag}", flush=True)
            if not events and not is_locked:
                print("  (no chat messages detected)", flush=True)
            elif events and not dry_run:
                added = rag.ingest(events)
                if added:
                    print(f"  RAG: +{added} stored", flush=True)

        print(f"Starting monitor (dry_run={dry_run}, interval={poll_cfg['interval_seconds']}s) — Ctrl+C to stop", flush=True)
        await poller.run(on_change=on_change, dry_run=dry_run)


@cli.command()
def tray():
    """Launch system tray icon with Start/Pause/Stop controls (production mode)."""
    config = load_config()
    api_key = require_api_key()
    from src.tray import TrayApp
    TrayApp(config, api_key).run()


@cli.command()
@click.argument("question")
def query(question: str):
    """Ask a question about recorded conversations."""
    config = load_config()
    api_key = require_api_key()
    _run_query(config, api_key, question)


@cli.command()
def agent():
    """Start interactive query REPL."""
    config = load_config()
    api_key = require_api_key()
    _run_query(config, api_key, question=None)


def _run_query(config: dict, api_key: str, question: str | None) -> None:
    from src.rag import RAGPipeline
    from src.agent import QueryAgent

    rag_cfg = config["rag"]
    claude_cfg = config["claude"]

    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()

    agent_obj = QueryAgent(
        api_key=api_key,
        model=claude_cfg["query_model"],
        rag=rag,
        max_tokens=claude_cfg["max_tokens"],
    )

    if question:
        agent_obj.query(question)
    else:
        agent_obj.repl()


if __name__ == "__main__":
    cli()
