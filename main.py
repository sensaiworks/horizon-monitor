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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    import tomli as tomllib

import click
from dotenv import load_dotenv


def app_dir() -> Path:
    """Where config.toml/.env live: beside the .exe when frozen, else the repo root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _bundled(name: str) -> Path:
    """A file packaged into the PyInstaller archive (or the source tree when not frozen)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / "config.toml"

# Load .env from beside the exe (or the repo root in dev). Missing file is fine.
load_dotenv(APP_DIR / ".env")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        example = _bundled("config.example.toml")
        if example.exists():
            import shutil
            shutil.copy(example, CONFIG_PATH)
            print(
                f"Created {CONFIG_PATH} from the bundled template — edit it as needed.",
                flush=True,
            )
        else:
            raise click.ClickException(
                f"{CONFIG_PATH.name} not found — copy config.example.toml to config.toml "
                "and edit it"
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
    from src.store import EventStore

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

    store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
    store.connect()

    # Optional retention: drop data older than [retention].retain_days on startup so a
    # long-running monitor doesn't accumulate forever. 0/absent = keep everything.
    retain_days = int(config.get("retention", {}).get("retain_days", 0) or 0)
    if retain_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        removed = store.expire_older_than(cutoff)
        rag.delete_older_than(cutoff)
        if removed:
            print(f"retention: dropped {removed} event(s) older than {retain_days}d", flush=True)

    async with HorizonMCPClient(
        server_path=mcp_cfg["server_path"],
        command=mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
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
                ch = f" «{ev.channel}»" if ev.channel else ""
                t = f" {ev.chat_time}" if ev.chat_time else ""
                print(f"  [{ev.app}{ch}]{t} {ev.speaker}: {ev.message[:80]}{tag}", flush=True)
            if not events and not is_locked:
                print("  (no chat messages detected)", flush=True)
            elif events:
                new = store.ingest(events)   # dedup gate — only genuinely new messages
                if new:
                    rag.ingest(new)          # embed only the new ones
                    print(f"  stored +{len(new)} ({store.count()} total)", flush=True)

        print(f"Starting monitor (dry_run={dry_run}, interval={poll_cfg['interval_seconds']}s) — Ctrl+C to stop", flush=True)
        await poller.run(on_change=on_change, dry_run=dry_run)


@cli.command()
def tray():
    """Launch the classic pystray menu icon (legacy; prefer `app`)."""
    config = load_config()
    api_key = require_api_key()
    from src.tray import TrayApp
    TrayApp(config, api_key).run()


@cli.command()
def app():
    """Launch the desktop app: tray icon + tabbed window (PySide6)."""
    config = load_config()
    # Don't hard-require the key here — the window should open even without it so the
    # user can paste it into the Settings tab. Features that need it error gracefully.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    from src.ui import run
    raise SystemExit(run(config, api_key))


def _parse_coords(s: str, n: int, what: str) -> tuple[int, ...]:
    """Parse a comma-separated integer tuple like '120,340' or '0,0,800,600'."""
    try:
        parts = tuple(int(p.strip()) for p in s.split(","))
    except ValueError:
        raise click.ClickException(f"{what} must be {n} comma-separated integers, got {s!r}")
    if len(parts) != n:
        raise click.ClickException(f"{what} must have {n} values, got {len(parts)}")
    return parts


@cli.command()
@click.argument(
    "action",
    type=click.Choice(
        [
            "unlock", "keep-alive", "launch", "activate", "taskbar", "run", "foreground",
            "reply", "read-file", "write-file", "open",
            "read-screen", "read-scroll", "paste",
        ]
    ),
)
@click.argument("value", required=False, default="")
@click.option(
    "--out", default="",
    help="read-*: write the captured text to this local file instead of stdout",
)
@click.option(
    "--save/--no-save", default=False,
    help="write-file/paste: press Ctrl+S in the remote after pasting",
)
@click.option(
    "--region", default="",
    help="read-screen/read-scroll: OCR only this 'x,y,w,h' region in ACTUAL screen "
         "pixels (not screenshot pixels — they differ under display scaling). Omit "
         "to OCR the full screen, which needs no coordinates.",
)
@click.option(
    "--at", "at", default="",
    help="paste: click 'x,y' to focus the target before pasting",
)
@click.option(
    "--double", is_flag=True,
    help="paste: double-click the --at point (some apps need two clicks to focus)",
)
@click.option(
    "--interval", type=float, default=None,
    help="keep-alive: seconds between nudges (default [control].keepalive_interval_seconds)",
)
@click.option(
    "--submit/--no-submit", default=True,
    help="unlock: press Enter after typing the password (--no-submit lets you verify "
         "the typed password before pressing Enter — avoids burning a login attempt)",
)
def remote(action: str, value: str, out: str, save: bool, region: str, at: str,
           double: bool, interval: float, submit: bool):
    """Drive the remote Horizon session (WRITE/READ actions; requires [control].enabled).

    Examples:
      python main.py remote foreground
      python main.py remote launch "Microsoft Teams"
      python main.py remote unlock                 # type the password and sign in
      python main.py remote unlock --no-submit     # type it but stop before Enter
      python main.py remote keep-alive             # nudge until Ctrl+C so it won't lock
      python main.py remote reply "on it, thanks"

    OCR read bridge (for VDIs that block clipboard copy-out — reads via screenshot+OCR):
      python main.py remote read-screen --out data/screen.txt
      python main.py remote read-scroll 960,500 --out data/pane.txt   # scroll-stitch a pane
      python main.py remote paste data/reply.txt --at 700,980 --double # focus + paste-in

    Clipboard code bridge (only where copy-out is allowed; read-file fails otherwise):
      python main.py remote open "src/app.py"
      python main.py remote read-file --out data/pull.txt
      python main.py remote write-file data/pull.txt --save
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "Remote control disabled — set [control].enabled = true in config.toml"
        )
    asyncio.run(
        _run_remote(config, action, value, out, save, region, at, double, interval, submit)
    )


async def _run_remote(
    config: dict, action: str, value: str, out: str, save: bool,
    region: str = "", at: str = "", double: bool = False,
    interval: float | None = None, submit: bool = True,
) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.controller import RemoteController

    mcp_cfg = config["mcp"]
    ctl = config.get("control", {})
    region_box = _parse_coords(region, 4, "--region") if region else None

    def _emit(text: str, label: str) -> None:
        if out:
            out_path = Path(out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"remote: {label} {len(text)} chars -> {out}", flush=True)
        else:
            print(text)

    async with HorizonMCPClient(
        mcp_cfg["server_path"], mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
    ) as client:
        c = RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
            screen=ctl.get("screen", 0),
        )
        print(f"remote: {action} {value!r}", flush=True)
        if action == "foreground":
            await c.bring_to_front()
        elif action == "unlock":
            password = os.environ.get("HORIZON_PASSWORD", "")
            if not password:
                raise click.ClickException("HORIZON_PASSWORD not set in .env")
            await c.unlock(password, submit=submit)
        elif action == "keep-alive":
            secs = interval if interval is not None else ctl.get(
                "keepalive_interval_seconds", 120.0
            )
            print(
                f"remote: keep-alive every {secs:g}s — Ctrl+C to stop", flush=True
            )
            try:
                while True:
                    await c.nudge()
                    await asyncio.sleep(secs)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("remote: keep-alive stopped", flush=True)
                return
        elif action in ("launch", "activate"):
            if not value:
                raise click.ClickException(f"'{action}' needs an app name")
            await c.launch_or_activate(value)
        elif action == "taskbar":
            if not value:
                raise click.ClickException("'taskbar' needs an app term to match")
            ok = await c.activate_taskbar([value])
            print(f"remote: taskbar {'ACTIVATED' if ok else 'NOT FOUND'}: {value!r}", flush=True)
        elif action == "run":
            if not value:
                raise click.ClickException("'run' needs a command")
            await c.run_command(value)
        elif action == "reply":
            if not value:
                raise click.ClickException("'reply' needs message text")
            await c.send_reply(value)
        elif action == "open":
            if not value:
                raise click.ClickException("'open' needs a file path")
            await c.open_file(value)
        elif action == "read-file":
            text = await c.copy_from_remote()
            _emit(text, "pulled")
        elif action == "write-file":
            if not value:
                raise click.ClickException("'write-file' needs a local file path")
            text = Path(value).read_text(encoding="utf-8")
            await c.paste_to_remote(text, replace_all=True, save=save)
            print(
                f"remote: pasted {len(text)} chars from {value}"
                f"{' and saved' if save else ''}",
                flush=True,
            )
        elif action == "read-screen":
            text = await c.read_screen(region=region_box)
            _emit(text, "ocr")
        elif action == "read-scroll":
            if not value:
                raise click.ClickException("'read-scroll' needs an 'x,y' scroll point")
            sx, sy = _parse_coords(value, 2, "read-scroll point")
            text, screens = await c.read_scrolling(sx, sy, region=region_box)
            _emit(text, f"ocr ({screens} screen(s))")
        elif action == "paste":
            if not value:
                raise click.ClickException("'paste' needs a local file path (text to paste)")
            text = Path(value).read_text(encoding="utf-8")
            ax = ay = None
            if at:
                ax, ay = _parse_coords(at, 2, "--at")
            await c.paste_at(text, x=ax, y=ay, double=double, save=save)
            print(
                f"remote: pasted {len(text)} chars from {value}"
                f"{f' at {ax},{ay}' if at else ''}{' and saved' if save else ''}",
                flush=True,
            )
        print("remote: done", flush=True)


@cli.command()
@click.argument("goal")
@click.option("--act", is_flag=True, help="Hands-on mode: perform actions (confirming each). "
                                          "Default is read-only advice.")
def assist(goal: str, act: bool):
    """Direct help — a screen-aware agent drives the remote VDI to do a task.

    \b
    python main.py assist "help me switch to master, do not stash my changes"
    python main.py assist "open the integration test file in VS Code" --act

    Advise (default) is read-only — it only tells you the steps. --act performs them,
    asking you to confirm every input action (y = do it, s = skip, n = stop).
    """
    config = load_config()
    api_key = require_api_key()
    from src.assistant import ComputerUseAgent

    agent = ComputerUseAgent(config, api_key)

    def confirm(desc: str) -> None:
        ans = input(f"\n  ACT — perform this? [{desc}]\n  (y)es / (s)kip / (n)o-stop: ").strip().lower()
        decision = {"y": "confirm", "yes": "confirm", "s": "skip", "skip": "skip"}.get(ans, "stop")
        agent.resolve_confirmation(decision)

    agent.on_text = lambda s: print(f"\nAssistant: {s}", flush=True)
    agent.on_action = confirm
    agent.on_result = lambda s: print(f"  · {s}", flush=True)
    agent.on_status = lambda s: print(f"  [{s}]", flush=True)
    agent.on_error = lambda e: print(f"  ERROR: {e}", flush=True)

    mode = "act" if act else "advise"
    print(f"assist ({mode}): {goal!r}\n", flush=True)
    agent.run_turn(goal, mode)
    print("\nassist: done", flush=True)


@cli.command()
@click.argument("chat_title")
@click.option("--months", type=int, default=None,
              help="Stop once messages older than this many months appear "
                   "(default [backfill].months).")
@click.option("--max-frames", type=int, default=None,
              help="Safety cap on total frames (default [backfill].max_frames).")
@click.option("--screen", type=int, default=None,
              help="VDI monitor the chat is on (default [backfill].screen).")
@click.option("--no-ingest", is_flag=True,
              help="Parse only; don't write to SQLite/ChromaDB (dry check).")
def backfill(chat_title: str, months: int | None, max_frames: int | None,
             screen: int | None, no_ingest: bool):
    """Import a whole Teams chat's back-history into the knowledge base.

    Unlike `monitor` (which sees only the current screen), this scrolls the named
    conversation newest->oldest, OCRs each frame (FREE — no Claude API), and
    ingests the history into SQLite + ChromaDB. Bring the chat to the front first.

    \b
    python main.py backfill "ResiDB Support"
    python main.py backfill "ResiDB Support" --months 3 --no-ingest
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "Backfill scrolls/drives the remote — set [control].enabled = true in config.toml"
        )
    asyncio.run(_run_backfill(config, chat_title, months, max_frames, screen, not no_ingest))


async def _run_backfill(config: dict, chat_title: str, months: int | None,
                        max_frames: int | None, screen: int | None, ingest: bool) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.backfill import ChatBackfiller

    mcp_cfg = config["mcp"]
    async with HorizonMCPClient(
        mcp_cfg["server_path"], mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
    ) as client:
        bf = ChatBackfiller(config, on_log=lambda s: print(s, flush=True))
        print(f"backfill: {chat_title!r} (ingest={ingest}) — bring the chat to the front. "
              "Ctrl+C to stop", flush=True)
        result = await bf.run(
            client, chat_title, months=months, max_frames=max_frames,
            screen=screen, ingest=ingest,
        )
        print(f"backfill: {result.summary()}", flush=True)


@cli.command(name="read-doc")
@click.option("--pages", type=int, default=None,
              help="Safety cap on viewports to page through (default [doc_reader].max_pages).")
@click.option("--screen", type=int, default=None,
              help="VDI monitor the document is on (default [doc_reader].screen).")
@click.option("--out", type=click.Path(dir_okay=False), default=None,
              help="Write the text to this file instead of printing it.")
@click.option("--force", is_flag=True,
              help="Skip the screen-share safety check. Only if you KNOW nothing is being shared.")
def read_doc(pages: int | None, screen: int | None, out: str | None, force: bool):
    """Read the front-most open document in the VDI into plain text.

    Pages the document top->bottom with the keyboard (PageDown - the mouse wheel
    barely moves this VDI) and OCRs each viewport (FREE - no Claude API), then
    stitches the overlapping viewports into one text. Open the document and
    MAXIMIZE IT ALONE on its monitor first, or a PageDown can land on another
    window. Refuses to run while a Teams/Webex screen share is active.

    \b
    python main.py read-doc
    python main.py read-doc --out notes.txt
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "read-doc drives the remote — set [control].enabled = true in config.toml"
        )
    asyncio.run(_run_read_doc(config, pages, screen, out, force))


async def _run_read_doc(config: dict, pages: int | None, screen: int | None,
                        out: str | None, force: bool) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.doc_reader import DocReader, DocReadBlocked

    mcp_cfg = config["mcp"]
    async with HorizonMCPClient(
        mcp_cfg["server_path"], mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
    ) as client:
        reader = DocReader(config, on_log=lambda s: print(s, flush=True))
        print("read-doc: bring the document to the front and MAXIMIZE it alone. "
              "Ctrl+C to stop", flush=True)
        try:
            result = await reader.read_frontmost(
                client, max_pages=pages, screen=screen, force=force)
        except DocReadBlocked as exc:
            raise click.ClickException(str(exc))
        print(f"read-doc: {result.summary()}", flush=True)
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(result.text)
            print(f"read-doc: wrote {result.chars} chars to {out}", flush=True)
        else:
            print("\n" + result.text, flush=True)


@cli.command()
@click.argument("remote_path")
@click.option(
    "--project", default="",
    help="Subfolder under [pull].root_dir to group this file under (e.g. PAT)",
)
@click.option(
    "--out", default="",
    help="Local destination. Relative paths resolve under [pull].root_dir (and --project, "
         "if given); absolute paths override entirely. Default: root_dir/[project/]basename.",
)
def pull(remote_path: str, project: str, out: str):
    """Pull one file out of the remote via the printer-redirection channel (src/pull.py).

    For VDIs that block clipboard copy-out, where `remote read-file` can't work at
    all: prints the file from Notepad to a local PDF via a Horizon-redirected virtual
    printer, then reconstructs the source text locally. Requires `pdftotext`
    (poppler) on PATH. For a .py destination, reports whether the reconstruction
    compiles rather than silently trusting it — always skim the result either way.

    Lands under [pull].root_dir (a folder OUTSIDE any git repo, e.g. C:\\github\\Mizuho —
    easy to open directly in VS Code, never at risk of being committed). Use --project to
    keep separate remote projects from colliding in one flat folder.

    \b
    python main.py pull "C:\\git\\project\\src\\app.py" --project PAT
    python main.py pull "C:\\git\\project\\README.md" --project PAT --out docs/README.md
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "Pull drives the remote — set [control].enabled = true in config.toml"
        )
    pull_cfg = config.get("pull", {})
    root = Path(pull_cfg.get("root_dir") or (APP_DIR / "data" / "pulled"))
    base = (root / project) if project else root
    name = Path(remote_path.replace("\\", "/")).name
    if out:
        out_path = Path(out)
        dest = out_path if out_path.is_absolute() else base / out_path
    else:
        dest = base / name
    asyncio.run(_run_pull(config, remote_path, dest))


async def _run_pull(config: dict, remote_path: str, dest: Path) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.controller import RemoteController
    from src.pull import ProjectPuller

    mcp_cfg = config["mcp"]
    ctl = config.get("control", {})
    pull_cfg = config.get("pull", {})

    async with HorizonMCPClient(
        mcp_cfg["server_path"], mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
    ) as client:
        c = RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
            screen=ctl.get("screen", 0),
        )
        puller = ProjectPuller(
            c, client,
            printer_name=pull_cfg.get("printer_name", "Microsoft Print to PDF"),
            open_wait_seconds=pull_cfg.get("open_wait_seconds", 1.5),
            print_wait_seconds=pull_cfg.get("print_wait_seconds", 0.8),
            save_wait_seconds=pull_cfg.get("save_wait_seconds", 1.2),
            save_poll_seconds=pull_cfg.get("save_poll_seconds", 0.5),
            save_timeout_seconds=pull_cfg.get("save_timeout_seconds", 20.0),
            address_bar_xy=(
                (pull_cfg["address_bar_x"], pull_cfg["address_bar_y"])
                if "address_bar_x" in pull_cfg and "address_bar_y" in pull_cfg
                else None
            ),
            address_bar_screen=pull_cfg.get("address_bar_screen", 0),
            clipboard_sync_seconds=pull_cfg.get(
                "clipboard_sync_seconds", ctl.get("clipboard_sync_seconds", 0.6)
            ),
            yield_resume_seconds=pull_cfg.get("yield_resume_seconds", 30.0),
            on_log=lambda s: print(s, flush=True),
        )
        print(f"pull: {remote_path} -> {dest}", flush=True)
        result = await puller.pull_file(remote_path, dest)
        status = "OK" if result.ok else "NEEDS REVIEW"
        print(
            f"pull: {status} — {result.local_path} ({result.chars} chars)"
            f"{f' — {result.error}' if result.error else ''}",
            flush=True,
        )


@cli.command(name="pull-project")
@click.argument("project")
@click.option(
    "--root", default="",
    help="Remote project root, e.g. \"C:\\\\git\\\\residesk\\\\PAT\". Required the first "
         "time; later runs reuse the value cached in the project's manifest.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Discover + diff only — show what would be pulled/is gone, pull nothing.",
)
@click.option(
    "--force", is_flag=True,
    help="Ignore the cached manifest and re-pull every discovered file.",
)
def pull_project(project: str, root: str, dry_run: bool, force: bool):
    """Discover and pull (or re-sync) a WHOLE remote project's code/text files.

    Unlike `pull` (one file, path typed by hand), this lists the remote project's
    file tree itself (via robocopy /L, falling back to `dir /s`), filters to
    code/text files, and pulls only what's new or changed since the last run —
    tracked in a manifest at <project_dir>/.horizon_pull_manifest.json. Re-run any
    time to sync on demand; nothing here runs automatically.

    \b
    python main.py pull-project PAT --root "C:\\git\\residesk\\PAT"   # first run
    python main.py pull-project PAT                                    # later: re-sync
    python main.py pull-project PAT --dry-run                          # preview only
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "pull-project drives the remote — set [control].enabled = true in config.toml"
        )
    asyncio.run(_run_pull_project(config, project, root, dry_run, force))


async def _run_pull_project(
    config: dict, project: str, root_override: str, dry_run: bool, force: bool
) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.controller import RemoteController
    from src.pull import ProjectPuller
    from src.remote_tree import (
        RemoteTreeLister, filter_entries, load_manifest, save_manifest,
        diff_against_manifest,
    )

    mcp_cfg = config["mcp"]
    ctl_cfg = config.get("control", {})
    pull_cfg = config.get("pull", {})

    root_dir = Path(pull_cfg.get("root_dir") or (APP_DIR / "data" / "pulled"))
    project_dir = root_dir / project
    manifest_path = project_dir / ".horizon_pull_manifest.json"
    manifest = load_manifest(manifest_path)
    remote_root = root_override or manifest.get("remote_root", "")
    if not remote_root:
        raise click.ClickException(
            'No remote root known for this project yet — pass --root "C:\\path\\to\\project" '
            "the first time."
        )

    async with HorizonMCPClient(
        mcp_cfg["server_path"], mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
    ) as client:
        c = RemoteController(
            client,
            focus_target=ctl_cfg.get("focus_target", "PVDI"),
            launch_wait=ctl_cfg.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl_cfg.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl_cfg.get("copy_timeout_seconds", 6.0),
            screen=ctl_cfg.get("screen", 0),
        )
        puller = ProjectPuller(
            c, client,
            printer_name=pull_cfg.get("printer_name", "Microsoft Print to PDF"),
            open_wait_seconds=pull_cfg.get("open_wait_seconds", 1.5),
            print_wait_seconds=pull_cfg.get("print_wait_seconds", 0.8),
            save_wait_seconds=pull_cfg.get("save_wait_seconds", 1.2),
            save_poll_seconds=pull_cfg.get("save_poll_seconds", 0.5),
            save_timeout_seconds=pull_cfg.get("save_timeout_seconds", 20.0),
            address_bar_xy=(
                (pull_cfg["address_bar_x"], pull_cfg["address_bar_y"])
                if "address_bar_x" in pull_cfg and "address_bar_y" in pull_cfg
                else None
            ),
            address_bar_screen=pull_cfg.get("address_bar_screen", 0),
            clipboard_sync_seconds=pull_cfg.get(
                "clipboard_sync_seconds", ctl_cfg.get("clipboard_sync_seconds", 0.6)
            ),
            yield_resume_seconds=pull_cfg.get("yield_resume_seconds", 30.0),
            on_log=lambda s: print(s, flush=True),
        )
        lister = RemoteTreeLister(
            puller,
            list_wait_seconds=pull_cfg.get("tree_wait_seconds", 4.0),
            on_log=lambda s: print(s, flush=True),
        )

        raw_entries = await lister.list_tree(remote_root, project_dir / "._tree_scratch")
        if not raw_entries:
            print(
                "pull-project: no files discovered — check the remote root is correct "
                "and an Explorer window is open/focused in the remote session, then "
                "screenshot the VDI to see what happened",
                flush=True,
            )
            return
        entries = filter_entries(raw_entries, remote_root)
        print(
            f"pull-project: {len(entries)} code/text files match (of {len(raw_entries)} "
            "total discovered)",
            flush=True,
        )

        manifest_files = {} if force else manifest.get("files", {})
        diff = diff_against_manifest(entries, manifest_files)
        print(
            f"pull-project: {len(diff.to_pull)} new/changed, {len(diff.unchanged)} "
            f"unchanged, {len(diff.removed)} gone on remote",
            flush=True,
        )

        if dry_run:
            for rel in diff.to_pull:
                print(f"  would pull: {rel}", flush=True)
            for rel in diff.removed:
                print(f"  gone on remote (kept locally): {rel}", flush=True)
            return

        by_rel = {e.relpath: e for e in entries}
        updated_files = dict(manifest_files)
        ok_count = review_count = fail_count = 0
        for i, rel in enumerate(diff.to_pull, 1):
            # Cooperative emergency stop: create APP_DIR/STOP (an empty file, any
            # method — Explorer "New > Text Document" renamed, or `type nul >
            # STOP` / `New-Item STOP`) and this loop notices it before the NEXT
            # file and exits cleanly, saving progress already made. This exists
            # because "stop" typed in chat is a REQUEST that has to be seen and
            # acted on — it is not instant, and for a batch already running
            # in the background there could be real delay before it's honored.
            # A file check costs nothing and needs no cooperation from the
            # agent's current turn at all.
            stop_flag = APP_DIR / "STOP"
            if stop_flag.exists():
                stop_flag.unlink()
                print(
                    f"pull-project: STOP file found — halting after {i - 1}/"
                    f"{len(diff.to_pull)} files this run (progress saved)",
                    flush=True,
                )
                save_manifest(manifest_path, remote_root, updated_files)
                return
            e = by_rel[rel]
            remote_path = remote_root.rstrip("\\") + "\\" + rel.replace("/", "\\")
            local_dest = project_dir / rel
            print(f"=== [{i}/{len(diff.to_pull)}] {rel} ===", flush=True)
            result = await puller.pull_file(remote_path, local_dest)
            if result.chars > 0:
                # File actually landed on disk — record it so a re-sync doesn't keep
                # re-pulling it, even if it needs manual review (didn't compile).
                updated_files[rel] = {"size": e.size, "mtime": e.mtime}
                if result.ok:
                    ok_count += 1
                else:
                    review_count += 1
            else:
                fail_count += 1  # nothing written — leave off the manifest, retry next sync
            save_manifest(manifest_path, remote_root, updated_files)

        print(
            f"pull-project: done — {ok_count} ok, {review_count} needs review, "
            f"{fail_count} failed (will retry next sync), {len(diff.unchanged)} unchanged",
            flush=True,
        )
        if diff.removed:
            print(
                f"pull-project: {len(diff.removed)} file(s) gone on remote "
                "(left in place locally):",
                flush=True,
            )
            for rel in diff.removed:
                print(f"  {rel}", flush=True)


@cli.command(name="pull-payload")
@click.argument("project")
@click.option(
    "--root", required=True,
    help="Remote project root, e.g. \"C:\\\\git\\\\residesk\\\\PAT\".",
)
@click.option(
    "--resume", is_flag=True,
    help="Carry on from an interrupted run: skip the remote export, reuse the "
         "existing staging dir, and reprint only the export files whose local PDF "
         "is missing or fails verification.",
)
@click.option(
    "--keep-staging", is_flag=True,
    help="Don't delete the remote staging dir after a successful pull (so a later "
         "--resume can still reuse it).",
)
@click.option(
    "--max-chars-per-file", type=int, default=None,
    help="Override [pull_payload].max_chars_per_file for this run. Bigger = fewer "
         "print jobs = fewer chances to fail, at the cost of a longer single print. "
         "This is the calibration knob.",
)
@click.option(
    "--chunk-chars", type=int, default=None,
    help="Override [pull_payload].chunk_chars for this run (base64 chars per "
         "individually-hashed chunk).",
)
def pull_payload(project: str, root: str, resume: bool, keep_staging: bool,
                 max_chars_per_file: int | None, chunk_chars: int | None):
    """Byte-exact whole-project pull via a single base64 payload print.

    Unlike `pull-project` (one fragile print+reconstruct per file, RETIRED as a
    batch transport — see DECISIONS.md), this zips the whole remote project,
    base64-encodes it, and prints that as one or a few SHA-256-verified text files
    — immune to the indentation/line-wrap guessing that made the old pipeline
    unreliable. Decoded and verified locally; a failed chunk names exactly which
    export file to reprint rather than silently producing wrong output.

    Verified live 2026-08-18 against "Pricing Tool" (11 files incl. .xlsx/.PNG
    binaries, 1,469,364 zip bytes, all hashes clean) — see DECISIONS.md.

    Not incremental across pulls — each run fetches the whole project. It IS
    resumable WITHIN a run: if printing fails partway, re-run with --resume and
    only the missing/unverifiable export files are reprinted.

    \b
    python main.py pull-payload PAT --root "C:\\git\\residesk\\PAT"
    python main.py pull-payload PAT --root "C:\\git\\residesk\\PAT" --resume
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "pull-payload drives the remote — set [control].enabled = true in config.toml"
        )
    asyncio.run(_run_pull_payload(
        config, project, root, resume=resume, keep_staging=keep_staging,
        max_chars_per_file=max_chars_per_file, chunk_chars=chunk_chars,
    ))


async def _run_pull_payload(
    config: dict, project: str, remote_root: str, *, resume: bool = False,
    keep_staging: bool = False, max_chars_per_file: int | None = None,
    chunk_chars: int | None = None,
) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.controller import RemoteController
    from src.pull import ProjectPuller
    from src.pull_payload import PayloadPuller

    mcp_cfg = config["mcp"]
    ctl_cfg = config.get("control", {})
    pull_cfg = config.get("pull", {})
    payload_cfg = config.get("pull_payload", {})

    root_dir = Path(pull_cfg.get("root_dir") or (APP_DIR / "data" / "pulled"))
    project_dir = root_dir / project

    async with HorizonMCPClient(
        mcp_cfg["server_path"], mcp_cfg["command"],
        call_timeout_seconds=mcp_cfg.get("call_timeout_seconds", 90.0),
    ) as client:
        c = RemoteController(
            client,
            focus_target=ctl_cfg.get("focus_target", "PVDI"),
            launch_wait=ctl_cfg.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl_cfg.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl_cfg.get("copy_timeout_seconds", 6.0),
            screen=ctl_cfg.get("screen", 0),
        )
        puller = ProjectPuller(
            c, client,
            printer_name=pull_cfg.get("printer_name", "Microsoft Print to PDF"),
            open_wait_seconds=pull_cfg.get("open_wait_seconds", 1.5),
            print_wait_seconds=pull_cfg.get("print_wait_seconds", 0.8),
            save_wait_seconds=pull_cfg.get("save_wait_seconds", 1.2),
            save_poll_seconds=pull_cfg.get("save_poll_seconds", 0.5),
            save_timeout_seconds=pull_cfg.get("save_timeout_seconds", 20.0),
            address_bar_xy=(
                (pull_cfg["address_bar_x"], pull_cfg["address_bar_y"])
                if "address_bar_x" in pull_cfg and "address_bar_y" in pull_cfg
                else None
            ),
            address_bar_screen=pull_cfg.get("address_bar_screen", 0),
            clipboard_sync_seconds=pull_cfg.get(
                "clipboard_sync_seconds", ctl_cfg.get("clipboard_sync_seconds", 0.6)
            ),
            yield_resume_seconds=pull_cfg.get("yield_resume_seconds", 30.0),
            on_log=lambda s: print(s, flush=True),
        )
        payload_puller = PayloadPuller(
            puller,
            chunk_chars=chunk_chars or payload_cfg.get("chunk_chars", 4000),
            max_chars_per_file=(
                max_chars_per_file or payload_cfg.get("max_chars_per_file", 200_000)
            ),
            max_file_bytes=payload_cfg.get("max_file_bytes", 5 * 1024 * 1024),
            exclude_dirs=payload_cfg.get("exclude_dirs"),
            staging_dir_name=payload_cfg.get("staging_dir_name", "_archive"),
            manifest_poll_seconds=payload_cfg.get("manifest_poll_seconds", 6.0),
            manifest_timeout_seconds=payload_cfg.get("manifest_timeout_seconds", 300.0),
            stop_flag_path=APP_DIR / "STOP",
            on_log=lambda s: print(s, flush=True),
        )

        print(f"pull-payload: {project} <- {remote_root}", flush=True)
        try:
            result = await payload_puller.pull_project_payload(
                remote_root, project_dir, project_name=project,
                resume=resume, keep_staging=keep_staging,
            )
        except Exception as exc:
            # The remote staging dir is intact (cleanup only runs on success) and
            # every PDF printed so far is on local disk, so the retry is cheap.
            raise click.ClickException(
                f"{exc}\n\nThe remote staging dir and every export PDF printed so "
                f"far were kept. Re-run the SAME command with --resume to reprint "
                f"only what's missing instead of starting over."
            ) from exc
        print(
            f"pull-payload: done — {len(result.files)} file(s) extracted to "
            f"{result.local_dest} ({result.printed} printed, {result.reused} reused)",
            flush=True,
        )
        if result.completeness is not None and not result.completeness.ok:
            print(
                "pull-payload: WARNING — the bytes are SHA-256 proven, but the "
                "completeness check did not come back clean (see above).",
                flush=True,
            )


@cli.command()
@click.argument("keyword")
@click.option("--limit", type=int, default=30, help="Max matches to show.")
@click.option("--channel", default="", help="Restrict to a channel (substring match).")
@click.option("--emails", is_flag=True,
              help="Also list every email address found in the matches.")
def search(keyword: str, limit: int, channel: str, emails: bool):
    """Exact keyword search over collected messages (FREE — no API, no LLM).

    The literal-lookup complement to `query` (which is semantic + Sonnet). Good for
    finding an exact string — an email address, a table name, "distro" — that vector
    search would only approximate.

    \b
    python main.py search distro
    python main.py search better.com --emails
    python main.py search email --channel "ResiDB Support"
    """
    import re as _re
    config = load_config()
    from src.store import EventStore

    store = EventStore(config["rag"].get("events_db", "./data/events.db"))
    store.connect()
    rows = store.search(keyword, limit=limit, channel=channel or None)
    if not rows:
        click.echo(f"No messages matching {keyword!r} (of {store.count()} stored).")
    for r in rows:
        when = r.get("chat_time") or (r.get("observed_at") or "")[:16]
        ch = f" «{r['channel']}»" if r.get("channel") else ""
        click.echo(f"[{when}]{ch} {r['speaker']}: {r['message'][:160]}")
    if emails:
        found: dict[str, tuple[str, str]] = {}
        for r in rows:
            for m in _re.findall(
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", r["message"]
            ):
                found.setdefault(m, (r["speaker"], r.get("chat_time", "")))
        if found:
            click.echo("\nEmail addresses found:")
            for addr, (sp, ct) in sorted(found.items()):
                click.echo(f"  {addr}  ({sp}, {ct})")
    store.close()


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


@cli.command()
@click.option(
    "--older-than", type=int, default=None, metavar="DAYS",
    help="Delete only events older than DAYS days. Omit to purge everything.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def purge(older_than: int | None, yes: bool):
    """Trim or wipe stored conversation data (SQLite + ChromaDB).

    \b
    python main.py purge                  # delete ALL stored events (asks to confirm)
    python main.py purge --older-than 30  # delete events older than 30 days
    """
    config = load_config()
    if older_than is not None and older_than < 0:
        raise click.ClickException("--older-than must be >= 0")

    if older_than is None:
        if not yes and not click.confirm(
            "This deletes ALL stored events from SQLite and ChromaDB. Continue?"
        ):
            raise click.ClickException("Aborted.")
    _run_purge(config, older_than)


def _run_purge(config: dict, older_than: int | None) -> int:
    """Apply retention/purge to both stores. Returns SQLite rows removed.

    Shared by the `purge` command and the monitor's startup auto-retention.
    """
    from src.rag import RAGPipeline
    from src.store import EventStore

    rag_cfg = config["rag"]

    store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
    store.connect()
    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()

    if older_than is None:
        removed = store.purge_all()
        rag.purge_all()
        print(f"purge: removed all {removed} event(s) from SQLite + ChromaDB", flush=True)
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than)).isoformat()
        removed = store.expire_older_than(cutoff)
        rag.delete_older_than(cutoff)
        print(f"purge: removed {removed} event(s) older than {older_than}d", flush=True)
    store.close()
    return removed


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
    # Double-clicking the packaged .exe (no args) launches the desktop app.
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        sys.argv.append("app")
    cli()
