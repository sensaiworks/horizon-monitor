"""
Read a named Outlook folder in the VDI STRUCTURALLY -- through Outlook's COM object
model, via one readable PowerShell script -- instead of OCR-ing the reading pane
email by email.

Why (field-tested 2026-10-16)
-----------------------------
Summarizing the user's "LoanCAD" folder live took dozens of GUI steps: click each
email, scroll the reading pane, OCR it, repeat, then paste a summary through Notepad.
It worked, but every step was open-loop (no window handles, state inferred from OCR)
and burned tokens on screenshots. `New-Object -ComObject Outlook.Application` ->
MAPI -> folder.Items gives the same data as structured text in ONE launch, with no
scrolling, no window-overlap fragility, and no focus-stealing beyond that launch.

How it composes existing primitives (nothing here drives the VDI on its own)
---------------------------------------------------------------------------
  1. ProjectPuller.deposit_text_file()  -- lands src/remote_scripts/
     outlook_folder_dump.ps1 in the VDI (blank Notepad + paste + Save-As).
  2. ProjectPuller.run_remote_command() -- runs it with a plain
     `powershell ... -File <ps1> -Folder "<name>" -Out <dump>` typed into an
     Explorer address bar. READABLE -File, never base64 -EncodedCommand: the
     auto-mode safety classifier blocks pasting an encoded blob (denied live).
  3. The script opens the finished dump in Notepad. That is the completion signal
     -- the local side can't see the remote filesystem -- so we poll the screen for
     the dump's header marker, then maximize Notepad (Alt+Space, X: the Win key is
     unreliable in this VDI, it tends to open the LOCAL Start menu).
  4. DocReader.read_frontmost() -- keyboard-pages Notepad and OCRs each viewport
     (FREE, offline). Clipboard copy-OUT is blocked on this VDI, so OCR read-back is
     the only channel; clean generated text OCRs reliably.
  5. parse_dump() -- pure -- turns the text into MessageEvents; `--ingest` stores
     them through the existing EventStore + RAGPipeline (doc_id() dedup), so the Ask
     tab can answer over mail later for free.

Safety: refuses while a screen share looks active (DocReader.screen_share_active),
and the CLI gates everything behind [control].enabled -- never runs automatically.

Entry points
------------
  parse_dump(text, folder, user_name="")        # pure, unit-testable
  build_launch_command(...)                      # pure, unit-testable
  OutlookReader(config).read_folder(...)         # async, drives the remote
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .doc_reader import DocReader, DocReadBlocked
from .models import MessageEvent

SCRIPT_NAME = "outlook_folder_dump.ps1"


def script_path() -> Path:
    """The in-repo (or PyInstaller-bundled) copy of the remote script."""
    return Path(__file__).resolve().parent / "remote_scripts" / SCRIPT_NAME


# --------------------------------------------------------------------------- #
# Pure helpers -- no I/O.                                                      #
# --------------------------------------------------------------------------- #

def folder_slug(folder: str) -> str:
    """Filesystem-safe stem for a folder name ("Inbox/Clients/LoanCAD" -> "LoanCAD")."""
    leaf = folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", leaf).strip("_")
    return slug or "mail"


def build_launch_command(
    remote_script: str, folder: str, remote_out: str, *,
    max_items: int, body_max_chars: int,
) -> str:
    """The exact, human-readable command typed into the Explorer address bar.

    -File with named args, like project_archive.ps1's fallback path. Only the folder
    name is quoted (it may contain spaces); the two paths are expected to be
    space-free (see [mail].remote_dir) so nothing else needs quoting. The command
    goes to the remote via clipboard paste, so backslashes are fine -- type_text
    would hit ENAMETOOLONG on them.
    """
    if '"' in folder:
        raise ValueError("folder name must not contain double quotes")
    return (
        f"powershell -NoProfile -ExecutionPolicy Bypass -File {remote_script} "
        f'-Folder "{folder}" -Out {remote_out} '
        f"-MaxItems {int(max_items)} -BodyMaxChars {int(body_max_chars)}"
    )


# Markers as written by the .ps1 -- matched LOOSELY because they come back through
# OCR: "===MSG 2/41===" has been seen as "=== MSG 2 / 41 ===", "=" runs may drop or
# turn into "-"/"—", and a trailing "===" can vanish entirely. Only the word and
# the digits are load-bearing.
_RE_HEADER = re.compile(r"MAIL\W{0,3}DUMP\b(?!\W{0,3}END)", re.I)
_RE_FOOTER = re.compile(r"MAIL\W{0,3}DUMP\W{0,3}END\b", re.I)
_RE_MSG = re.compile(r"\bMSG\s*(\d+)\s*/\s*(\d+)", re.I)
_RE_MSG_ERR = re.compile(r"\bMSG\W{0,3}ERROR\b", re.I)
_RE_END_MSG = re.compile(r"\bEND\W{0,3}MSG\b", re.I)
_RE_FIELD = re.compile(r"^\s*(Received|From|To|Subject)\s*[:;]\s*(.*)$", re.I)
_RE_BODY = re.compile(r"^\s*Body\s*[:;]?\s*$", re.I)
_RE_ITEMS = re.compile(r"items\s*[=:]\s*(\d+)", re.I)


@dataclass
class DumpParse:
    events: list[MessageEvent] = field(default_factory=list)
    declared_items: int | None = None   # from the header's items=<n>
    header_found: bool = False
    footer_found: bool = False
    errors: int = 0                     # ===MSG-ERROR=== markers (items the script couldn't read)

    @property
    def complete(self) -> bool:
        """True when the whole dump was seen (header AND footer) and every declared
        item parsed. False means: re-read, or treat the result as partial."""
        return (
            self.header_found and self.footer_found
            and (self.declared_items is None
                 or len(self.events) + self.errors >= self.declared_items)
        )

    def summary(self) -> str:
        n = len(self.events)
        declared = f"/{self.declared_items}" if self.declared_items is not None else ""
        state = "complete" if self.complete else "INCOMPLETE"
        err = f", {self.errors} unreadable" if self.errors else ""
        return f"{n}{declared} messages parsed ({state}{err})"


def parse_dump(text: str, folder: str, user_name: str = "") -> DumpParse:
    """Parse the OCR'd text of an outlook_folder_dump.ps1 dump into MessageEvents.

    Field mapping (kept close to what the live monitor produces for Outlook so the
    Ask tab/RAG treats mail and chat alike):
      speaker       <- From (SenderName)
      message       <- "Subject: ...\\n<body>"  (subject rides inside the text so the
                       embedding sees it)
      chat_time     <- Received (as rendered by the script)
      channel       <- the folder name
      app           <- "outlook"
      directed_at_user <- `user_name` appears in the To line
    doc_id() keys on channel|speaker|chat_time|message[:120], so re-running the
    same folder dedups against what's already stored. OCR jitter in those fields
    can, rarely, let a message in twice -- tolerated, never silently dropped.
    """
    parse = DumpParse()
    cur: dict[str, str] | None = None
    body: list[str] | None = None

    def flush() -> None:
        nonlocal cur, body
        if cur is None:
            return
        subject = cur.get("subject", "").strip()
        body_text = "\n".join(body or []).strip()
        if subject or body_text:
            to_line = cur.get("to", "")
            message = f"Subject: {subject}\n{body_text}".strip() if subject else body_text
            parse.events.append(MessageEvent(
                speaker=cur.get("from", "").strip() or "unknown",
                message=message,
                app="outlook",
                window_title=f"Outlook - {folder}",
                directed_at_user=bool(user_name) and user_name.lower() in to_line.lower(),
                chat_time=cur.get("received", "").strip(),
                channel=folder,
            ))
        cur, body = None, None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if body is not None:
                body.append("")
            continue
        if _RE_FOOTER.search(line):
            flush()
            parse.footer_found = True
            continue
        if _RE_HEADER.search(line) and cur is None:
            parse.header_found = True
            m = _RE_ITEMS.search(line)
            if m:
                parse.declared_items = int(m.group(1))
            continue
        if _RE_MSG_ERR.search(line):
            flush()
            parse.errors += 1
            continue
        m = _RE_MSG.search(line)
        if m and "/" in line and not _RE_FIELD.match(line):
            flush()
            cur, body = {}, None
            if parse.declared_items is None:
                parse.declared_items = int(m.group(2))
            continue
        if _RE_END_MSG.search(line):
            flush()
            continue
        if cur is None:
            continue   # text before the first MSG marker (Notepad chrome, stray OCR)
        if body is None:
            fm = _RE_FIELD.match(line)
            if fm:
                cur[fm.group(1).lower()] = fm.group(2)
                continue
            if _RE_BODY.match(line):
                body = []
                continue
            # A wrapped continuation of the previous header field (long To lists).
            if cur:
                last = list(cur)[-1]
                cur[last] = (cur[last] + " " + line).strip()
            continue
        body.append(line)
    flush()
    return parse


# --------------------------------------------------------------------------- #
# Live reader -- composes ProjectPuller + DocReader.                            #
# --------------------------------------------------------------------------- #

@dataclass
class MailReadResult:
    folder: str
    text: str = ""
    pages: int = 0
    parse: DumpParse = field(default_factory=DumpParse)
    stored_rows: int = 0
    stored_vectors: int = 0

    def summary(self) -> str:
        s = f"{self.folder!r}: {self.pages} viewports, {len(self.text)} chars; {self.parse.summary()}"
        if self.stored_rows or self.stored_vectors:
            s += f"; stored +{self.stored_rows} rows / +{self.stored_vectors} vectors"
        return s


class OutlookReader:
    """Dump one Outlook folder inside the VDI and read it back as text/events."""

    def __init__(self, config: dict, on_log: Callable[[str], None] | None = None) -> None:
        self._config = config
        self._log = on_log or (lambda _s: None)
        mail = config.get("mail", {})
        self._default_folder = mail.get("folder", "Inbox")
        # Remote working dir for the script + dump. Must already EXIST (Notepad's
        # Save-As can't create folders) and be space-free (keeps the launch command
        # quote-free).
        self._remote_dir = str(mail.get("remote_dir", r"C:\Temp")).rstrip("\\/")
        self._max_items = int(mail.get("max_items", 200))
        self._body_max = int(mail.get("body_max_chars", 4000))
        self._dump_timeout = float(mail.get("dump_timeout_seconds", 120.0))
        self._poll_s = float(mail.get("poll_seconds", 3.0))
        self._maximize_wait_ms = int(mail.get("maximize_wait_ms", 800))
        self._user_name = str(config.get("user", {}).get("display_name", "")).strip()
        # DocReader is tuned for a maximized Word window by default; Notepad has no
        # ribbon, so [mail] carries its own region/max_pages and we hand DocReader a
        # config whose [doc_reader] is overlaid with them.
        dr = dict(config.get("doc_reader", {}))
        dr["region"] = mail.get("region", [20, 90, 1880, 950])
        dr["max_pages"] = int(mail.get("max_pages", 200))
        dr.setdefault("focus_click", True)
        self._reader = DocReader({**config, "doc_reader": dr}, on_log=on_log)

    # -- paths / commands ---------------------------------------------------------
    def remote_script_path(self) -> str:
        return f"{self._remote_dir}\\{SCRIPT_NAME}"

    def remote_dump_path(self, folder: str) -> str:
        return f"{self._remote_dir}\\mail_{folder_slug(folder)}.txt"

    def launch_command(self, folder: str, *, max_items: int | None = None) -> str:
        return build_launch_command(
            self.remote_script_path(), folder, self.remote_dump_path(folder),
            max_items=self._max_items if max_items is None else max_items,
            body_max_chars=self._body_max,
        )

    # -- the drive --------------------------------------------------------------
    async def read_folder(
        self, puller, controller, client, folder: str | None = None, *,
        max_items: int | None = None, force: bool = False,
        should_stop: Callable[[], bool] | None = None,
    ) -> MailReadResult:
        """Deposit + run the dump script, wait for Notepad, read it back.

        Raises DocReadBlocked if a screen share looks active (unless force=True),
        RuntimeError if the dump never shows up on screen. LIVE PATH NOT YET
        FIELD-TESTED end-to-end as of 2026-10-16 -- watch the first run.
        """
        folder = folder or self._default_folder
        result = MailReadResult(folder=folder)

        # 0) SAFETY first -- before depositing or launching anything.
        await client.focus_window(self._config.get("control", {}).get("focus_target", "PVDI"))
        await client.wait(400)
        if not force:
            reason = await self._reader.screen_share_active(client)
            if reason:
                raise DocReadBlocked(reason)

        # 1) Preconditions: an Explorer window is the typing surface for every step.
        await puller.preflight_explorer()

        # 2) Deposit the readable script, then run it. Re-deposited every run so the
        #    VDI copy can never drift from the reviewed one in this repo.
        script_text = script_path().read_text(encoding="utf-8")
        remote_script = self.remote_script_path()
        self._log(f"read-mail: depositing {SCRIPT_NAME} -> {remote_script}")
        await puller.deposit_text_file(remote_script, script_text)
        cmd = self.launch_command(folder, max_items=max_items)
        self._log(f"read-mail: running: {cmd}")
        await puller.run_remote_command(cmd)

        # 3) Wait for the script to finish = its Notepad window showing the dump
        #    header. Polls full-screen OCR, not the taskbar (taskbar labels don't OCR
        #    reliably on this VDI -- see pull.py's deposit_text_file).
        await self._wait_for_dump_on_screen(controller, client, should_stop)

        # 4) Maximize Notepad alone so PageDown always lands on it. Alt+Space opens
        #    the window's system menu; X = Maximize. No Win key involved.
        await client.key_combo(["Alt", "Space"])
        await client.wait(300)
        await client.press_key("x")
        await client.wait(self._maximize_wait_ms)

        # 5) Page + OCR it back.
        doc = await self._reader.read_frontmost(client, force=True, should_stop=should_stop)
        result.text, result.pages = doc.text, doc.pages
        result.parse = parse_dump(doc.text, folder, self._user_name)
        self._log(f"read-mail: {result.parse.summary()}")
        if not result.parse.complete:
            self._log("read-mail: dump looks INCOMPLETE -- footer/items missing; the "
                      "OCR pass may have stopped early (raise [mail].max_pages) or the "
                      "script hit an error. Check the Notepad window in the VDI.")
        return result

    async def _wait_for_dump_on_screen(
        self, controller, client, should_stop: Callable[[], bool] | None,
    ) -> None:
        waited = 0.0
        while True:
            if should_stop and should_stop():
                raise RuntimeError("read-mail: cancelled while waiting for the dump")
            text = await controller.read_screen()
            if _RE_HEADER.search(text):
                self._log(f"read-mail: dump is on screen after {waited:.0f}s")
                return
            if waited >= self._dump_timeout:
                raise RuntimeError(
                    f"read-mail: the dump header never appeared on screen within "
                    f"{self._dump_timeout:.0f}s. Either the script is still running "
                    "(large folder -- raise [mail].dump_timeout_seconds), Outlook isn't "
                    "open, the folder name didn't match, or Outlook raised a security "
                    "prompt. Look at the VDI: a PowerShell error or a Notepad window "
                    "will tell you which."
                )
            await client.wait(int(self._poll_s * 1000))
            waited += self._poll_s

    # -- storage ----------------------------------------------------------------
    def ingest(self, events: list[MessageEvent]) -> tuple[int, int]:
        """Store into SQLite + ChromaDB through the same dedup gate the monitor and
        backfill use. Returns (new rows, new vectors)."""
        if not events:
            return 0, 0
        from .rag import RAGPipeline
        from .store import EventStore

        rag_cfg = self._config["rag"]
        store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
        store.connect()
        try:
            new_rows = store.ingest(events)
            if not new_rows:
                return 0, 0
            rag = RAGPipeline(
                db_path=rag_cfg["db_path"],
                collection_name=rag_cfg["collection_name"],
                embedding_provider=rag_cfg.get("embedding_provider", "voyage"),
                voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
                top_k=rag_cfg.get("top_k", 8),
            )
            rag.connect()
            return len(new_rows), rag.ingest(new_rows)
        finally:
            store.close()
