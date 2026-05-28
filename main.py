"""
Entry point for horizon-monitor.

Commands:
  python main.py monitor           # start monitoring loop
  python main.py monitor --dry-run # test poll loop without LLM
  python main.py query "..."       # one-shot question
  python main.py agent             # interactive REPL

Configuration is loaded from config.toml in the project root.
API keys are loaded from .env (copy from .env.example).
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path

import click
from dotenv import load_dotenv
import os

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.toml"


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


@click.group()
def cli():
    pass


@cli.command()
@click.option("--dry-run", is_flag=True, help="Poll without calling LLM or notifying")
def monitor(dry_run: bool):
    """Start the monitoring loop."""
    config = load_config()
    # TODO: wire up HorizonMCPClient, Poller, Extractor, RAGPipeline, Notifier
    # and run the async poll loop.
    # See CLAUDE.md §"Implementation order / Step 1" for the sequence.
    print("monitor not yet implemented — see CLAUDE.md for implementation order")


@cli.command()
@click.argument("question")
def query(question: str):
    """Ask a question about recorded conversations."""
    config = load_config()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # TODO: connect RAGPipeline, instantiate QueryAgent, call agent.query(question)
    print("query not yet implemented — see CLAUDE.md Step 5")


@cli.command()
def agent():
    """Start interactive query REPL."""
    config = load_config()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # TODO: connect RAGPipeline, instantiate QueryAgent, call agent.repl()
    print("agent not yet implemented — see CLAUDE.md Step 6")


if __name__ == "__main__":
    cli()
