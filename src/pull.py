"""
Pull source files out of a remote session (clipboard copy-out disabled) via printer
redirection. Parked (not a current priority) — a side exploration run against mock
projects in a personal lab; see DECISIONS.md.

controller.py's clipboard bridge (copy_from_remote) needs Horizon clipboard copy-OUT
enabled; its OCR read bridge (read_screen/read_scrolling) is fine for chat text but
loses indentation and mangles symbols -- unusable for code. This module is a THIRD
channel, discovered field-testing a session where clipboard copy-out was disabled: printing
to a Horizon-redirected virtual printer ("Microsoft Print to PDF (vdi)" or similar)
pops a REAL LOCAL Windows Save dialog and produces a genuine, text-selectable PDF
straight on the local disk -- the remote-side print spooler renders exact glyphs, so
unlike OCR there is no character-recognition error.

The catch: Notepad's print engine word-wraps long lines, and `pdftotext`'s layout
heuristics reconstruct indentation unreliably (it resyncs each "paragraph" to its own
left margin, so a docstring line and the function body right after it can both come
out looking like column 0). Two things fix this:
  1. Disable Notepad's Word Wrap + use Landscape orientation before printing, so lines
     are far less likely to wrap at all.
  2. Extract the PDF TWICE with different `pdftotext` layout modes and merge them:
     `-table` mode gets the CONTENT right (no character loss) but the INDENT wrong;
     `-lineprinter` mode gets the INDENT right (fixed-pitch column tracking) but
     splits words with spurious spaces. Zipping table's content with lineprinter's
     indent line-for-line (they always have identical non-blank line counts, field-
     tested against two ~1800-line files) gives a reconstruction that's right the
     overwhelming majority of the time.

Still imperfect: rare indent-rounding collisions near an exact half-step boundary, a
handful of very long lines that wrap even in landscape, and the odd glyph the PDF font
drops outright. All of that surfaced, on the two files this was built against, as either
a SyntaxError/IndentationError on compile or a visibly empty character -- never a
silent-but-wrong token -- which is exactly the point of verify_python(): treat "compiles
clean" as the pass/fail gate, not a guarantee of byte-perfect fidelity. Always skim a
pulled file before trusting it, same honesty the OCR read bridge already asks for.

Entry points:
  merge_table_and_lineprinter(table_text, lp_text)   # pure, unit-testable
  verify_python(source)                               # pure, unit-testable
  reconstruct_from_pdf(pdf_path)                      # local only, no VDI I/O
  ProjectPuller(controller, client).pull_file(...)    # async, drives the remote
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .controller import RemoteController
from .idle import ActivityMonitor
from .mcp_client import HorizonMCPClient

# --------------------------------------------------------------------------- #
# Pure reconstruction pipeline -- no I/O, unit-testable offline against saved  #
# pdftotext output.                                                            #
# --------------------------------------------------------------------------- #

_INDENT_STEP = 6         # spaces per level in the reconstructed source
_LP_COLUMN_UNIT = 4      # pdftotext -lineprinter's own column unit (not ours)


def _ocr_safe_hint(stem: str, min_len: int = 3, max_len: int = 16) -> str:
    """A substring of `stem` (a filename with its extension already stripped)
    that's safe to OCR-match against a taskbar label.

    Field-tested live: Windows OCR doesn't render '_' and '.' consistently in
    taskbar button text -- "part_001.txt" read back as "export 001 .txt"
    (underscore -> space, period dropped entirely). A hint built from a raw
    prefix of the name (e.g. "part_001", including the literal underscore)
    then never matches, since the OCR'd text has a space there instead --
    confirmed live: this silently caused a working Notepad launch to be
    reported as "didn't appear", triggering a needless retry that covered the
    just-opened window and cascaded into a real failure two steps later.

    The fix sidesteps guessing how any ONE special character renders: return
    the first run of plain alphanumeric characters (length >= min_len) found
    ANYWHERE in `stem`, not a fixed-offset prefix -- for "part_001" that's
    "export", for "_archive_Pricing_Tool" (leads with '_') that's
    "horizon". Plain alphanumeric runs render the same regardless of how
    OCR handles whatever punctuation surrounds them. Falls back to a raw
    prefix only if no such run exists (e.g. a name that's ALL punctuation).
    """
    match = re.search(r"[A-Za-z0-9]{%d,}" % min_len, stem)
    return (match.group() if match else stem)[:max_len]

# Bytes pdftotext sometimes emits for glyphs the PDF's embedded font doesn't map
# cleanly (observed against real Notepad-printed PDFs) -> their intended characters.
_GLYPH_FIXUPS: dict[bytes, bytes] = {
    b"\xad": "—".encode("utf-8"),  # em dash
    b"\xd7": "×".encode("utf-8"),  # multiplication sign
}


def _fix_glyph_bytes(data: bytes) -> bytes:
    for bad, good in _GLYPH_FIXUPS.items():
        data = data.replace(bad, good)
    return data


def _content_indent_pairs(
    text: str, *, collapse_spaces: bool
) -> list[tuple[int, str, int]]:
    """Non-blank (leading_space_count, content, page_number) triples, in order.

    Blank/form-feed-only lines are dropped entirely -- pdftotext's blank-line count
    isn't meaningful here (see module docstring), and Python doesn't care how many
    blank lines separate statements, only what's inside a triple-quoted string, which
    this can't distinguish either way. A form feed (\\x0c) DOES matter though: it
    marks a PDF page boundary, and `-lineprinter`'s column measurement can drift
    across one -- see _page_margins.
    """
    out: list[tuple[int, str, int]] = []
    page = 0
    for raw in text.split("\n"):
        if "\x0c" in raw:
            page += 1
        line = raw.rstrip("\r\n").replace("\x0c", "")
        lead = len(line) - len(line.lstrip(" "))
        rest = line[lead:]
        if rest.strip() == "":
            continue
        if collapse_spaces:
            rest = re.sub(r"  +", " ", rest)
        out.append((lead, rest, page))
    return out


def _page_margins(lp_pairs: list[tuple[int, str, int]]) -> dict[int, int]:
    """Per-page left margin, phase-aligned to the document's overall margin.

    `-lineprinter`'s column measurement can drift by a sub-unit amount ACROSS A
    PDF PAGE BREAK, not just between separate print jobs (see _indent_level) --
    field-tested live against a real ~4300-line file: a page boundary sat between
    two lines that should have been the same indent level, and everything on the
    page after it measured 2 raw columns higher, tipping 30+ consecutive lines one
    level too deep and breaking the compile with no exception until verify_python()
    caught it.

    Recalibrating each page from its OWN minimum raw column (like the document-
    wide margin is derived) would be wrong: a page can start mid-method, already
    indented, with no line anywhere near true level 0 -- using its shallowest line
    as level 0 would mis-level the entire page instead of just the drifted part.

    Instead this keeps the document's overall margin and only nudges each page by
    a small residue DRIFT: since a whole page's columns shift by the same additive
    offset, every raw column on that page -- REGARDLESS of what level it actually
    represents -- sits that same distance (mod _LP_COLUMN_UNIT) from the document's
    established phase. Measuring that distance via the page's minimum raw column
    (any line works; the minimum is just convenient) recovers the drift without
    ever assuming that line IS level 0 -- unlike snapping the page's minimum to a
    nearby absolute value, which silently mis-levels an entire page with no true
    level-0 content of its own (field-tested: an earlier version of this function
    did exactly that, on this same file). A drift that happens to be an exact
    multiple of _LP_COLUMN_UNIT is invisible to this (indistinguishable from a real
    difference in nesting depth) and stays uncorrected -- not observed in practice
    so far, unlike the sub-unit case this fixes.
    """
    if not lp_pairs:
        return {0: 0}
    doc_margin = min(rc for rc, _, _ in lp_pairs)
    page_mins: dict[int, int] = {}
    for raw_column, _, page in lp_pairs:
        if page not in page_mins or raw_column < page_mins[page]:
            page_mins[page] = raw_column
    margins: dict[int, int] = {}
    for page, page_min in page_mins.items():
        drift = (page_min - doc_margin) % _LP_COLUMN_UNIT
        if drift > _LP_COLUMN_UNIT // 2:
            drift -= _LP_COLUMN_UNIT
        margins[page] = doc_margin + drift
    return margins


def _indent_level(raw_column: int, left_margin: int) -> int:
    """Map a `-lineprinter` raw column count to a 0-based indent level.

    Empirically `-lineprinter` uses a fixed left margin plus _LP_COLUMN_UNIT columns
    per level. `left_margin` is NOT a universal constant -- field-tested live across
    two separate pulls in the same session, it shifted from 12 to 9 between print
    jobs (different Notepad/print-dialog state), and a hardcoded margin silently
    computed level 0 for what should have been level 1 (a `try:` and its own `pass`
    body collapsed onto the same indent), breaking the compile with no exception
    until verify_python() caught it. Callers must derive it per-document -- see
    merge_table_and_lineprinter, which takes the minimum raw column across the whole
    file as the level-0 baseline. Plain round() uses banker's (round-half-to-even)
    rounding, which can collapse two ADJACENT levels onto the same even number right
    at a .5 boundary -- e.g. raw columns 1.5 and 2.5 levels above the margin both
    round to level 2. That also turned a `def foo():` and its own first body line
    into siblings in a real file and broke the compile. Round-half-up avoids it.
    """
    return max(0, (raw_column - left_margin + _LP_COLUMN_UNIT // 2) // _LP_COLUMN_UNIT)


def merge_table_and_lineprinter(table_text: str, lineprinter_text: str) -> str:
    """Reconstruct source text from a `pdftotext -table` and `-lineprinter` extraction
    of the SAME PDF: table's content (accurate characters) + lineprinter's indent
    (accurate nesting), zipped line-for-line.

    Raises ValueError if the two extractions don't have the same non-blank line count
    -- that means something more serious than the usual cosmetic drift went wrong (a
    dropped page, a failed extraction) and the zip alignment can't be trusted at all;
    re-print or fall back to manual transcription rather than trusting a misaligned
    merge.
    """
    table_pairs = _content_indent_pairs(table_text, collapse_spaces=True)
    lp_pairs = _content_indent_pairs(lineprinter_text, collapse_spaces=False)
    if len(table_pairs) != len(lp_pairs):
        raise ValueError(
            f"table/lineprinter non-blank line count mismatch "
            f"({len(table_pairs)} vs {len(lp_pairs)}) -- extraction likely failed on "
            "part of the PDF; re-print or fall back to manual transcription"
        )
    # The left margin (raw -lineprinter column of unindented/module-level code)
    # shifts between print jobs -- see _indent_level -- so derive it from this
    # document instead of assuming a fixed value: the least-indented line(s) in any
    # real source file sit at column 0, so their raw column IS the margin. It can
    # ALSO drift within a single document across a page break -- see _page_margins.
    margins = _page_margins(lp_pairs)
    lines = []
    for (_, content, _), (raw_column, _, page) in zip(table_pairs, lp_pairs):
        level = _indent_level(raw_column, margins[page])
        lines.append(" " * (level * _INDENT_STEP) + content)
    return "\n".join(lines) + "\n"


def looks_like_leaked_launch_command(source: str, remote_path: str) -> bool:
    """True if `source` is (or starts with) the literal `notepad "<remote_path>"`
    command used to launch Notepad, instead of that file's real content.

    This is the exact, deterministic signature of the address-bar-command-landed-
    in-Notepad-instead-of-Explorer failure (see _run_via_explorer and
    print_to_pdf's stale-tab guard): field-tested live, a 50-70 character pulled
    file that fails to compile with a line-1 SyntaxError is always this, never
    coincidental real content. Unlike verify_python(), this applies to EVERY file
    type -- a non-.py destination has no other content check at all, so without
    this a corrupted .txt/.json/.html silently reports "OK" (confirmed live:
    Requirements/_extract/dfd_run.txt did exactly this).
    """
    expected = f'notepad "{remote_path}"'
    return source.strip() == expected or source.strip().startswith(expected)


def verify_python(source: str, filename: str = "<pulled>") -> tuple[bool, str]:
    """Best-effort correctness gate for a reconstructed .py file: does it compile?

    A clean compile does NOT guarantee byte-perfect fidelity (see module docstring --
    silent content substitution is possible, just rare in practice), only that
    indentation/syntax survived reconstruction. Non-.py files have no equivalent
    check; skim them before trusting, same caveat as the OCR read bridge.
    """
    try:
        compile(source, filename, "exec")
        return True, ""
    except SyntaxError as exc:
        return False, f"{exc.__class__.__name__}: {exc.msg} (line {exc.lineno})"


def repair_indentation(source: str, filename: str = "<pulled>") -> tuple[str, bool, str]:
    """Best-effort recovery when `source` fails to compile with an IndentationError:
    try re-leveling a small window starting at the failing line by +-1 or +-2 indent
    steps, and keep the change ONLY if the result compiles.

    merge_table_and_lineprinter's per-PAGE margin (_page_margins) is the primary
    fix for a systematic drift across a whole page. This function is the
    remaining safety net for smaller-scale noise that isn't explained by a clean
    page-level shift -- e.g. font-kerning jitter on a single line that tips it
    across a rounding boundary in isolation. Repairing from the output side is
    cheap here specifically because the affected window is expected to be small:
    it tries a handful of candidate re-levelings and checks whether the result
    compiles, rather than trying to model the noise directly.

    Never trusted blindly -- like verify_python(), "compiles" is not "byte-perfect".
    Callers should still report this as needing a look, not silently as fully OK;
    the returned message says which lines were touched so a human can go check
    exactly that region.
    """
    ok, err = verify_python(source, filename)
    if ok:
        return source, True, ""
    try:
        compile(source, filename, "exec")
        return source, True, ""
    except SyntaxError as exc:
        # `exc` is implicitly deleted when this block exits (Python's except-
        # variable scoping), so pull out what's needed before that happens.
        exc_is_indentation_error = exc.__class__.__name__ == "IndentationError"
        exc_lineno = exc.lineno
    if not exc_is_indentation_error or exc_lineno is None:
        return source, False, err
    lines = source.split("\n")
    start = exc_lineno - 1
    if not (0 <= start < len(lines)):
        return source, False, err
    for run_len in range(1, 8):
        end = start + run_len
        if end > len(lines):
            break
        for delta_steps in (-1, 1, -2, 2):
            delta = delta_steps * _INDENT_STEP
            trial = lines[:]
            feasible = True
            for i in range(start, end):
                if trial[i].strip() == "":
                    continue
                cur_lead = len(trial[i]) - len(trial[i].lstrip(" "))
                new_lead = cur_lead + delta
                if new_lead < 0:
                    feasible = False
                    break
                trial[i] = " " * new_lead + trial[i].lstrip(" ")
            if not feasible:
                continue
            trial_source = "\n".join(trial)
            try:
                compile(trial_source, filename, "exec")
            except SyntaxError:
                continue
            return trial_source, True, (
                f"auto-repaired indentation on line(s) {exc_lineno}-{end} "
                f"({delta_steps:+d} level) after a compile failure -- verify this "
                f"region by eye, 'compiles' is not 'byte-perfect'"
            )
    return source, False, err


def _pdftotext(pdf_path: Path, mode: str) -> str:
    """Run `pdftotext -<mode>` and return the glyph-fixed decoded text.

    Raises RuntimeError with a clear message if `pdftotext` isn't on PATH -- it ships
    with poppler (e.g. `choco install poppler` on Windows); this project doesn't
    bundle it.
    """
    if shutil.which("pdftotext") is None:
        raise RuntimeError(
            "pdftotext not found on PATH -- install poppler "
            "(e.g. `choco install poppler` on Windows) to pull files this way"
        )
    result = subprocess.run(
        ["pdftotext", f"-{mode}", str(pdf_path), "-"],
        capture_output=True, check=True,
    )
    return _fix_glyph_bytes(result.stdout).decode("utf-8", errors="replace")


def reconstruct_from_pdf(pdf_path: Path) -> str:
    """The full local pipeline: PDF -> merged source text. No VDI/network I/O, so this
    can be re-run any time against a PDF already sitting on disk without touching the
    remote session again."""
    table_text = _pdftotext(pdf_path, "table")
    lp_text = _pdftotext(pdf_path, "lineprinter")
    return merge_table_and_lineprinter(table_text, lp_text)


# --------------------------------------------------------------------------- #
# Remote-driving side -- opens the file in Notepad inside the VDI, prints it   #
# to the redirected PDF printer, waits for the local file to land.            #
#                                                                               #
# Notepad is launched via an Explorer address bar (_run_via_explorer), NOT the #
# Win+R Run dialog -- field-tested live: key_combo(["Win", ...]) is unreliable #
# in this VDI (sometimes hits the LOCAL Start/Run instead of the remote's,     #
# sometimes drops outright), while the address-bar launch was confirmed       #
# working. The print/save dialog steps still target on-screen TEXT via OCR     #
# (RemoteController.find_text_point), the same pattern unlock() proved out     #
# live, but haven't been re-run end-to-end through this exact code path yet.   #
# Treat the first full print_to_pdf() run as a live test: watch it, and if a   #
# step can't find its target it raises a clear error rather than clicking the  #
# wrong thing blindly.                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class PullResult:
    remote_path: str
    local_path: Path
    ok: bool
    chars: int = 0
    error: str = ""


class ProjectPuller:
    """Pulls files out of a VDI (clipboard copy-out disabled) via the
    printer-redirection channel.

    Only reach for this when controller.copy_from_remote() isn't available
    (clipboard copy-out is disabled) -- prefer the clipboard bridge when it works;
    it's lossless and doesn't touch Notepad or any print dialog at all.
    """

    def __init__(
        self,
        controller: RemoteController,
        client: HorizonMCPClient,
        printer_name: str = "Microsoft Print to PDF",
        open_wait_seconds: float = 1.5,
        print_wait_seconds: float = 0.8,
        save_wait_seconds: float = 1.2,
        save_poll_seconds: float = 0.5,
        save_timeout_seconds: float = 20.0,
        address_bar_xy: tuple[int, int] | None = None,
        address_bar_screen: int | None = 0,
        clipboard_sync_seconds: float = 0.6,
        yield_resume_seconds: float = 30.0,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._ctl = controller
        self._client = client
        self._printer_name = printer_name
        self._open_wait = open_wait_seconds
        self._print_wait = print_wait_seconds
        self._save_wait = save_wait_seconds
        self._save_poll = save_poll_seconds
        self._save_timeout = save_timeout_seconds
        # Horizon clipboard redirection syncs local -> remote with a lag; set_clipboard
        # followed immediately by Ctrl+V pastes STALE content (field-tested live: this
        # is what was actually breaking the address-bar launch, not the click/OCR steps
        # it looked like at first -- RemoteController's own paste helpers all wait this
        # long for the same reason, this class just hadn't been given the same wait).
        self._clipboard_sync = clipboard_sync_seconds
        # Where to click to focus an open Explorer window's address bar. Field-tested
        # live: neither Ctrl+L nor F4 reliably focuses it in this VDI (same class of
        # key-delivery gap as Win+R), so this falls back to a direct coordinate click.
        # Default is this session's observed layout (screen index 0, a near-maximized
        # window); override via [pull].address_bar_x/y/screen if yours differs --
        # screenshot the VDI once to read it off your own window.
        self._addr_xy = address_bar_xy or (1030, 295)
        self._addr_screen = address_bar_screen
        self._log = on_log or (lambda _s: None)
        # Word Wrap is a Notepad-wide setting that persists across tabs/files for the
        # rest of the session, so it only needs handling once per instance -- and must
        # NOT be blindly re-toggled on a later file (see _ensure_word_wrap_off).
        self._wordwrap_handled = False
        self._monitors: list[dict] | None = None
        # Every action here focuses the LOCAL Horizon window first, which can
        # genuinely steal the user's mouse/keyboard mid-batch (confirmed live: the
        # user reported being unable to type at all while a batch ran). Reuses the
        # same GetLastInputInfo-based yield the auto-monitor rotation already uses
        # (src/idle.py) -- the moment real user input is detected, pause and wait
        # until they've been idle again for `yield_resume_seconds` before sending
        # any more of our own input. mark_self_input() is called after every batch
        # of actions we send so our OWN synthetic input never trips this itself.
        self._activity = ActivityMonitor()
        self._yield_resume_ms = int(yield_resume_seconds * 1000)

    async def _yield_to_user(self) -> None:
        if not self._activity.user_active():
            return
        self._log(
            "pull: input detected on this PC -- pausing until you've been idle "
            f"for {self._yield_resume_ms / 1000:.0f}s"
        )
        while self._activity.idle_ms() < self._yield_resume_ms:
            await self._client.wait(1000)
        self._log("pull: resuming")
        self._activity.mark_self_input()

    async def _addr_screen_origin(self) -> tuple[int, int]:
        """Absolute virtual-desktop (x, y) origin of `self._addr_screen`, so a
        monitor-relative coordinate like `self._addr_xy` (the frame `click(...,
        screen=N)` uses) can be converted for APIs that take absolute coordinates
        instead, like `RemoteController.read_screen`'s `region`."""
        if self._monitors is None:
            try:
                self._monitors = await self._client.list_monitors()
            except Exception:  # noqa: BLE001
                self._monitors = []
        for mon in self._monitors:
            if mon.get("index") == self._addr_screen:
                return mon["x"], mon["y"]
        return 0, 0  # single-monitor fallback: screen-relative == absolute

    async def _monitor_containing(self, x: int, y: int) -> dict | None:
        """The monitor dict (from list_monitors()) whose bounds contain absolute
        point (x, y), or None if it falls outside all known monitors. Reuses the
        same self._monitors cache _addr_screen_origin populates."""
        if self._monitors is None:
            try:
                self._monitors = await self._client.list_monitors()
            except Exception:  # noqa: BLE001
                self._monitors = []
        for mon in self._monitors:
            if (
                mon["x"] <= x < mon["x"] + mon["width"]
                and mon["y"] <= y < mon["y"] + mon["height"]
            ):
                return mon
        return None

    async def _click_text(self, needle: str) -> tuple[int, int]:
        point = await self._ctl.find_text_point(needle)
        if not point:
            raise RuntimeError(
                f"pull: couldn't find {needle!r} on screen -- the dialog layout may "
                "have changed; screenshot the VDI and adjust before retrying"
            )
        await self._client.click(point[0], point[1])
        self._activity.mark_self_input()
        return point

    async def _dump_diagnostics(self, tag: str) -> Path:
        """Best-effort: screenshot every monitor + a full-desktop OCR text dump,
        saved locally under data/diagnostics/<timestamp>_<tag>/ -- so a failure that
        needs a human to actually look at the screen leaves evidence behind
        regardless of what happens to the VDI/local session afterward. Written
        because this exact need bit live, twice: a screen-state question that
        couldn't be answered after the fact, then a full computer restart before a
        screenshot could be taken. Never raises -- a diagnostics failure must not
        mask the real error that triggered it.
        """
        out_dir = (
            Path(__file__).resolve().parent.parent / "data" / "diagnostics"
            / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}"
        )
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            monitors = await self._client.list_monitors()
            for mon in monitors:
                idx = mon.get("index", 0)
                png = await self._client.screenshot(screen=idx)
                (out_dir / f"screen_{idx}.png").write_bytes(png)
            ocr_text = await self._ctl.read_screen()
            (out_dir / "ocr.txt").write_text(ocr_text, encoding="utf-8")
            self._log(f"pull: saved failure diagnostics to {out_dir}")
        except Exception as exc:  # noqa: BLE001 -- must never mask the real error
            self._log(f"pull: couldn't save failure diagnostics ({exc})")
        return out_dir

    async def _poll_text_point(
        self, needle: str, *, timeout: float, interval: float = 0.3
    ) -> tuple[int, int] | None:
        """Poll find_text_point(needle) until found or `timeout` seconds elapse.

        Replaces a fixed wait-then-look-once check for dialogs that spawn a NEW
        top-level window (print -> save-as) -- field-tested live, a fixed wait
        sometimes fired before that window existed at all, so the very next OCR
        pass found nothing and the caller had no way to tell "not open yet" apart
        from "never opening". Polling removes the guesswork around exactly how long
        that takes on a given VDI/network.
        """
        waited = 0.0
        while True:
            point = await self._ctl.find_text_point(needle)
            if point:
                return point
            if waited >= timeout:
                return None
            await self._client.wait(int(interval * 1000))
            waited += interval

    async def run_remote_command(self, command: str) -> None:
        """Public wrapper around _run_via_explorer -- run any command (cmd, robocopy,
        powershell, notepad, ...) by typing it into an already-open Explorer window's
        address bar. Used directly by remote_tree.py for directory-listing commands,
        which have nothing to do with printing/PDFs but need the exact same launch
        mechanism pull_file() already relies on."""
        await self._run_via_explorer(command)

    async def wait_ms(self, ms: int) -> None:
        await self._client.wait(ms)

    async def _set_clipboard_verified(
        self, text: str, *, attempts: int = 4, exact: bool = True
    ) -> None:
        """set_clipboard(text), confirming via get_clipboard() before returning.

        Field-tested live: Horizon's local<->remote clipboard redirection can lag
        past `clipboard_sync_seconds` under load, and set_clipboard() itself never
        raises when that happens -- it just means whatever's about to be pasted is
        STALE (an earlier command/path), and everything downstream "succeeds" while
        being silently wrong. Retries a few times rather than trusting one wait.

        `exact=False` tolerates the LOCAL clipboard round-trip (set_clipboard() then
        get_clipboard() -- both only ever touch this machine's clipboard, same
        caveat as copy_from_remote's docstring) dropping exactly one trailing
        newline. Field-tested live via deposit_text_file(): setting a multi-line
        script ending in "\\n" and reading it straight back reliably returned the
        same text minus that one final newline, on every attempt -- a deterministic
        transformation somewhere in the clipboard/MCP round-trip, not the timing lag
        this method's retries otherwise defend against, so retrying alone can never
        pass. Only the trailing newline is tolerated; any other discrepancy (a
        truncated or genuinely stale paste) still fails and retries normally.
        """
        for attempt in range(attempts):
            await self._client.set_clipboard(text)
            await self._client.wait(int(self._clipboard_sync * 1000))
            current = await self._client.get_clipboard()
            if current == text or (not exact and current.rstrip("\r\n") == text.rstrip("\r\n")):
                return
            self._log(
                f"pull: clipboard didn't sync on attempt {attempt + 1} "
                f"(got {current!r}, wanted {text!r}) -- retrying"
            )
        raise RuntimeError(
            "pull: clipboard never synced to the intended value after retries -- "
            "aborting rather than risk pasting stale text"
        )

    # The first breadcrumb segment of an Explorer address bar: "This PC" for a
    # local path, "Network" for a UNC/network share -- field-tested live, an
    # Explorer window browsing a network path (unrelated to this project, but
    # still a perfectly valid typing surface -- the launch command is an
    # ABSOLUTE local path, so it works from any open Explorer window regardless
    # of what folder it currently shows) has no "This PC" anywhere on screen,
    # and anchoring on that alone missed it entirely. "ThisPC" (no space) is a
    # SEPARATE anchor, not a typo: field-tested live, OCR reads the breadcrumb's
    # first two segments as one merged line with no space between them --
    # "ThisPC SYSTEM(C:)" -- so the space-containing "This PC" never matches at
    # all (not the exact-line tier, not the substring tier either) and this
    # silently found nothing on every single attempt until caught by dumping
    # raw OCR output directly.
    _ADDRESS_BAR_ANCHORS = ("This PC", "ThisPC", "Network")

    async def _locate_explorer_address_bar(self) -> tuple[int, int] | None:
        """Dynamically find a safe click point in an open Explorer window's
        address bar instead of trusting a stored coordinate.

        Anchors on the first breadcrumb segment (see _ADDRESS_BAR_ANCHORS) --
        present in EVERY Explorer window regardless of which folder it's
        showing, from the root down to a deeply nested project folder.
        find_text_point() already OCRs every monitor and returns an ABSOLUTE
        virtual-desktop point, so this works regardless of which screen
        Explorer is currently on -- no address_bar_screen config to go stale.

        This replaces trusting self._addr_xy/_addr_screen as the FIRST thing
        tried: field-tested live across one session, the window's actual
        position moved three separate times (a reconnect, a resize, the user
        rearranging windows) while the configured coordinate stayed fixed,
        producing either a loud-but-safe abort (the existing corroboration
        check catching the mismatch) or, once, a near miss where an ad-hoc
        script that skipped that check pasted text into an unrelated, unsaved
        document instead. A coordinate that has to be re-eyeballed by hand
        every time the layout shifts doesn't hold up for unattended runs.

        The click target is offset well to the right of the anchor itself:
        clicking the breadcrumb TEXT navigates Explorer there instead of
        entering path-edit mode. +500px comfortably clears the longest
        breadcrumb this project has produced (e.g. "This PC > SYSTEM (C:) >
        git > residesk > PAT > src") without needing to also locate the
        breadcrumb's right edge.

        Returns None if no anchor is found on any monitor -- the caller falls
        back to the configured coordinate (still corroborated the old way)
        rather than raising here, so a real OCR miss doesn't turn a working
        session into a hard failure.
        """
        for anchor in self._ADDRESS_BAR_ANCHORS:
            # skip_top=80: Explorer shows this same text a THIRD time in its own
            # title bar (y~0-30), above the breadcrumb (y~150-260) -- field-
            # tested live, top_only alone (skip_top=0) matched the title bar
            # instead of the breadcrumb, and clicking there is a silent no-op
            # (no edit mode, no exception, the launch command just never got
            # typed anywhere). 80px clears the title bar with margin while
            # still starting well above the breadcrumb. See find_text_point's
            # docstring for the sidebar collision this also guards against.
            point = await self._ctl.find_text_point(
                anchor, top_only=True, skip_top=80, top_height=250
            )
            if not point:
                continue
            # Reject a false-positive match against a SEARCH BOX's "Search
            # This PC" placeholder text. Field-tested live and root-caused
            # only after it had silently spawned extra windows for days:
            # "This PC" is a literal substring of "Search This PC", so the
            # substring-tier match in find_text_point() picks up a search
            # box's placeholder as if it were the real breadcrumb -- and
            # WinSCP's local-file browser panel mimics Explorer's UI closely
            # enough to show that exact same placeholder. Clicking 500px
            # right of THAT lands inside WinSCP's own UI, and typing/
            # submitting the launch command there instead of into Explorer's
            # address bar is what was spawning a fresh WinSCP window on every
            # retry, confirmed live via a direct OCR scan of the exact match
            # ((self._box_center of the WHOLE "Search This PC" line, not
            # anchored on "This" specifically -- see find_text_point's
            # substring tier).
            #
            # Rejecting by a SEPARATE, delayed read_screen() call (an earlier
            # version of this fix) was unreliable: that call's own
            # ensure_foreground() + settle sleep gives the screen time to
            # shift between the ORIGINAL match and the verification read, so
            # it can end up checking a different screen state entirely --
            # field-tested live, it came back empty even immediately after a
            # fresh, confirmed "Search This PC" OCR read at the exact same
            # coordinates. Checking the match's POSITION instead needs no
            # second OCR call at all, so there's nothing to race: a search
            # box always sits near its window's right edge, while a real
            # breadcrumb -- even for the deepest path this project has
            # produced -- has never landed anywhere close to that far right.
            mon = await self._monitor_containing(*point)
            if mon is not None:
                local_x = point[0] - mon["x"]
                if local_x > 1100:
                    continue
            return point[0] + 500, point[1]
        return None

    async def preflight_explorer(self) -> tuple[int, int]:
        """Fail NOW, loudly, if no Explorer window is open in the remote session.

        Every remote action this class performs -- deposit_text_file,
        run_remote_command, print_to_pdf -- ultimately types into an already-open
        Explorer window's address bar (_run_via_explorer). That is an unstated
        environmental PRECONDITION, and until 2026-08-24 nothing checked it: with no
        Explorer window, _locate_explorer_address_bar() returns None, the caller
        silently falls back to the configured coordinate, clicks it (hitting the
        desktop or an unrelated window), types a launch command that goes nowhere,
        and then waits for an app that will never appear.

        Cost of not checking, measured: one live run sat wedged for an hour. And the
        precondition is genuinely easy to lose -- closing a stale Notepad tab with
        Ctrl+W closed the Explorer window along with it, turning a working session
        into an unrunnable one with no diagnostic.

        Deliberately a HARD failure rather than a warning: a false positive here
        costs the user one action ("open a File Explorer window"), while a false
        negative costs an hour and leaves the remote in a half-driven state. Note
        Win+E, Start-search and double-clicking Recycle Bin were ALL field-tested
        live and none of them opens Explorer in this VDI, so the recovery really is
        manual -- which is exactly why the message has to say so.
        """
        point = await self._locate_explorer_address_bar()
        if point is None:
            await self._dump_diagnostics("preflight_no_explorer")
            raise RuntimeError(
                "pull: no File Explorer window found in the remote session -- every "
                "remote action here types into Explorer's address bar, so nothing "
                "can run without one.\n"
                "  Fix: open a File Explorer window inside the VDI (any folder), "
                "then re-run.\n"
                "  Note: Win+E, Start-search and double-clicking Recycle Bin have all "
                "been field-tested and do NOT open Explorer in this VDI -- it has to "
                "be opened by hand.\n"
                "  (If a window IS open, OCR failed to find its breadcrumb: check it "
                "isn't fully covered by another window, and see the diagnostics dump "
                "just written.)"
            )
        return point

    async def _run_via_explorer(self, command: str) -> None:
        """Run `command` (e.g. 'notepad "C:\\...\\file.py"') by typing it into an
        already-open File Explorer window's address bar -- Explorer treats an
        unrecognized path there as a command to launch, the same trick `notepad` or
        `cmd /c ...` typed into Start would do.

        This is used INSTEAD OF the Win+R Run dialog (RemoteController.run_command /
        open_run) deliberately: field-tested live, key_combo(["Win", ...]) is
        unreliable in this VDI -- it sometimes opens the LOCAL Start/Run instead of the
        remote's (Windows intercepts the Win key before Horizon can forward it), and
        sometimes is dropped outright.

        Focusing the address bar is a direct coordinate CLICK, not Ctrl+L or F4 --
        both were field-tested live and neither reliably focused it in this VDI (the
        same class of key-delivery gap as Win+R; Ctrl+A/paste/submit afterward
        silently landed on whatever WAS focused instead, which is why this needed
        live debugging to catch rather than failing loudly). The coordinate itself
        is found FRESH each call via _locate_explorer_address_bar() (OCR-anchored on
        "This PC"), falling back to the configured self._addr_xy/_addr_screen only
        if that OCR search comes up empty -- see that method's docstring for why a
        stored coordinate alone isn't trustworthy across a whole session.

        PRECONDITION: a File Explorer window must already be focused/visible
        somewhere in the remote session -- this does not open a window itself. In
        practice that's already true for any realistic pull workflow (you're
        browsing the files you're about to pull).

        Field-tested live: `get_clipboard()` after `set_clipboard()` is NOT real
        verification -- both only ever touch the LOCAL clipboard (all horizon-mcp has
        direct OS access to), so they always agree instantly even when Horizon's
        local<->remote redirection hasn't actually propagated the new value into the
        REMOTE session's clipboard yet. That gap bit this live: an address bar pasted
        an EARLIER command's stale text (still sitting in the remote-side clipboard)
        and submitted THAT instead, with no exception anywhere -- just wrong-but-
        plausible output several steps later (a tiny "directory listing" that was
        actually leftover command text). OCR-ing the address bar afterward to verify
        was tried and abandoned: at the height needed to reliably return ANY text at
        all in this exact screen position, OCR came back garbled ("c: PAT" for
        "c:\\git\\residesk\\PAT") too often to trust as a pass/fail gate, producing
        false-positive aborts on perfectly correct pastes. `clipboard_sync_seconds`
        is deliberately generous (see config) to address the actual root cause --
        give redirection enough real time -- rather than trying to detect the failure
        after the fact through an unreliable channel.
        """
        await self._yield_to_user()
        await self._ctl.ensure_foreground()
        # Find the address bar FRESH -- see _locate_explorer_address_bar's
        # docstring for why a stored coordinate can't be trusted across a whole
        # session. This whole sequence ends in Ctrl+A then a newline
        # (Enter-equivalent) a few lines down, so a wrong click point here is a
        # real, not hypothetical, danger: Ctrl+A on the wrong target selects
        # whatever IS there instead of address-bar text, and the closing Enter
        # then acts on that selection. Field-tested live, repeatedly, with no
        # error anywhere up to that point -- every step "succeeded" right up
        # until an unrelated app silently opened, or once, an unrelated
        # document's text got overwritten.
        # Retried a few times with a short settle wait between attempts, not
        # checked once: field-tested live, this came back empty immediately after
        # closing a Notepad window opened moments earlier in the SAME run (proving
        # an Explorer window WAS there right before) -- consistent with the screen
        # still redrawing/settling right after a window closes, not a genuinely
        # missing Explorer window. Both the OCR search AND the coordinate fallback
        # get retried together each attempt since either could be the one affected.
        _EXPLORER_SEARCH_ATTEMPTS = 3
        _EXPLORER_SEARCH_SETTLE_SECONDS = 1.5
        click_xy = click_screen = None
        for attempt in range(1, _EXPLORER_SEARCH_ATTEMPTS + 1):
            # Find the address bar FRESH -- see _locate_explorer_address_bar's
            # docstring for why a stored coordinate can't be trusted across a whole
            # session. This whole sequence ends in Ctrl+A then a newline
            # (Enter-equivalent) a few lines down, so a wrong click point here is a
            # real, not hypothetical, danger: Ctrl+A on the wrong target selects
            # whatever IS there instead of address-bar text, and the closing Enter
            # then acts on that selection. Field-tested live, repeatedly, with no
            # error anywhere up to that point -- every step "succeeded" right up
            # until an unrelated app silently opened, or once, an unrelated
            # document's text got overwritten.
            dynamic_point = await self._locate_explorer_address_bar()
            if dynamic_point is not None:
                click_xy, click_screen = dynamic_point, None  # already absolute
                break
            # Fall back to the configured coordinate -- still corroborated the
            # OLD way (a "Home" ribbon tab, present near the top-left of any
            # Explorer window, found nearby) before trusting it, since a
            # stale/wrong fallback here is exactly the failure this function
            # exists to prevent.
            ax, ay = self._addr_xy
            ox, oy = await self._addr_screen_origin()
            # From the MONITOR's left edge, not ax -- the ribbon's "Home" tab sits
            # near the window's own left edge (~local x 20-100) regardless of
            # where the address bar (ax) falls horizontally, which can be far to
            # the right on a maximized window. Field-tested live: an ax-relative
            # region missed it entirely (started at local x=300, "Home" was at
            # local x=84) and produced a false-positive abort on a perfectly
            # healthy window.
            chrome_check = await self._ctl.read_screen(
                region=(ox, oy, min(1200, ax + 500), 260)
            )
            if "home" in chrome_check.lower():
                click_xy, click_screen = self._addr_xy, self._addr_screen
                break
            if attempt < _EXPLORER_SEARCH_ATTEMPTS:
                self._log(
                    f"pull: no Explorer window found on attempt {attempt}/"
                    f"{_EXPLORER_SEARCH_ATTEMPTS} -- waiting "
                    f"{_EXPLORER_SEARCH_SETTLE_SECONDS:g}s for the screen to "
                    "settle and retrying"
                )
                await self._client.wait(int(_EXPLORER_SEARCH_SETTLE_SECONDS * 1000))

        if click_xy is None:
            diag_dir = await self._dump_diagnostics("no_explorer_window")
            raise RuntimeError(
                f"pull: no Explorer window found after {_EXPLORER_SEARCH_ATTEMPTS} "
                "attempts -- OCR couldn't locate 'This PC' anywhere on screen, and "
                "the configured fallback address_bar_xy doesn't look like it's "
                "landing on an Explorer window either (no ribbon 'Home' tab found "
                "nearby). Open a File Explorer window and retry. Diagnostics "
                f"(screenshots + OCR dump) saved to {diag_dir}"
            )
        await self._client.click(*click_xy, screen=click_screen)
        await self._client.wait(200)
        await self._client.key_combo(["Ctrl", "A"])
        await self._client.set_clipboard(command)
        await self._client.wait(int(self._clipboard_sync * 1000))
        await self._client.key_combo(["Ctrl", "V"])
        await self._client.wait(300)
        # End before submitting: field-tested live, a pasted command that shares a
        # long substring with an EARLIER command run in this same session (e.g. both
        # reference the same manifest filename) can leave Explorer's autocomplete
        # dropdown showing that OLDER entry highlighted -- submitting then re-ran the
        # stale historical command instead of the freshly pasted one, silently, no
        # matter how long the clipboard-sync wait was (this was never a sync-timing
        # issue for that specific failure). End moves the caret past the pasted text
        # and, empirically, drops the highlighted suggestion without altering what
        # was actually typed/pasted.
        await self._client.key_combo(["End"])
        await self._client.wait(150)
        # type_text("\n"), not key_combo(["Enter"]): field-tested live, the address
        # bar's autocomplete dropdown swallows a key_combo Enter here even after
        # today's scan-code fix, while a literal newline through type_text submits
        # reliably (the same workaround this whole project already leans on).
        await self._client.type_text("\n")
        self._activity.mark_self_input()

    async def _ensure_word_wrap_off(self) -> None:
        """Best-effort, once per instance: open Notepad's View menu and turn Word Wrap
        off if it's on. OCR can't reliably read a menu checkmark glyph, so this ERRS ON
        THE SIDE OF NOT TOGGLING when it can't tell either way -- flipping an
        already-off setting back ON would silently wrap every line printed after that,
        which is a much worse failure than doing nothing. If pulled files keep coming
        back with SyntaxErrors on long lines, verify Word Wrap by eye once.
        """
        if self._wordwrap_handled:
            return
        self._wordwrap_handled = True
        await self._client.key_combo(["Alt", "V"])  # open the View menu
        self._activity.mark_self_input()
        await self._client.wait(400)
        point = await self._ctl.find_text_point("Word wrap")
        if not point:
            await self._client.key_combo(["Esc"])
            self._activity.mark_self_input()
            self._log("pull: couldn't find Word wrap in the View menu — leaving as-is")
            return
        line_text = await self._ctl.read_screen(
            region=(max(0, point[0] - 40), max(0, point[1] - 10), 200, 20)
        )
        if any(mark in line_text for mark in ("✓", "√", "✓")):
            await self._client.click(point[0], point[1])
        else:
            await self._client.key_combo(["Esc"])
        self._activity.mark_self_input()

    async def _ensure_notepad_focused(self) -> None:
        """Confirm Notepad has actually launched (its taskbar button appears)
        before sending it any keystrokes. Does NOT send Alt+Tab preemptively --
        see below for why that was tried and reverted.

        POLLS for the taskbar button rather than checking once: field-tested
        live, a single check right after `open_wait_seconds` still sometimes
        found nothing -- Notepad's taskbar button hadn't rendered yet even
        though the process had launched, the same "spawned a new window, one
        fixed wait doesn't reliably outlast it" gap _poll_text_point already
        exists to close for dialogs. Best-effort: if Notepad never appears in
        the taskbar at all, leave it for the caller's own polling/timeout to
        catch as a clear failure rather than raising here.

        History, for whoever next touches this: an earlier version of this
        method sent an UNCONDITIONAL Alt+Tab here, reasoning that a window
        just launched via _run_via_explorer might not actually hold focus
        (observed once, in a session where another app kept regaining focus
        between actions). That reasoning is sound for THAT scenario, but
        field-tested live against THIS exact deposit_text_file() call path, it
        was actively harmful in the much more common case: a freshly launched
        window already has focus (standard Windows behavior), and Alt+Tab
        pressed once from there switches AWAY to the PREVIOUS window (Explorer)
        -- confirmed live via a direct test: type_text() sent right after the
        Alt+Tab landed nowhere in Notepad (still "0 characters"), while the
        exact same launch sequence with NO Alt+Tab at all pasted correctly,
        repeatedly, including large multi-KB payloads. There is no reliable way
        to ask horizon-mcp which window inside the REMOTE session currently has
        focus (list_windows/get_foreground_window only see LOCAL OS windows,
        not apps running inside the VDI -- see CLAUDE.md's KEY DISTINCTION), so
        this can't safely condition Alt+Tab on actually needing it. Trusting
        the OS default plus verifying the actual downstream action (the
        pasted-content check in deposit_text_file, the "Printer" dialog poll in
        _print_open_notepad) is the safer bet than a blind "fix" that breaks
        the common case to maybe help a rare one.
        """
        waited = 0.0
        timeout = 5.0
        while waited < timeout:
            if await self._ctl.find_text_point("Notepad", taskbar_only=True):
                return
            await self._client.wait(300)
            waited += 0.3

    # Notepad's status bar, which no other app in this VDI renders. Matched
    # space-stripped for the same reason the Save-dialog corroboration is (OCR
    # drops/adds spaces unpredictably -- see _print_open_notepad).
    _NOTEPAD_CHROME = ("plaintext", "windows(crlf)", "ln1,col")

    async def _notepad_in_foreground(self) -> bool:
        """True if a Notepad window is actually visible and in front.

        Exists so _close_all_notepad_tabs can prove what it is about to send Ctrl+W
        to, rather than assuming a taskbar click did what was intended -- see the
        call site for the incident that motivated it.
        """
        text = re.sub(r"\s+", "", (await self._ctl.read_screen()).lower())
        return any(marker in text for marker in self._NOTEPAD_CHROME)

    async def _close_all_notepad_tabs(
        self, *, max_iterations: int = 25, extra_terms: list[str] | None = None
    ) -> bool:
        """Close EVERY open Notepad tab/window, looping until its taskbar button is
        gone entirely. Returns True if Notepad ended up fully closed, False if it
        gave up with tabs still open (caller decides how to react).

        `extra_terms`: additional taskbar-label substrings to also search for,
        beyond the literal word "Notepad". Field-tested live: that word only
        reliably appears in the taskbar label for an UNSAVED "Untitled -
        Notepad" document -- once a document is SAVED with a real filename,
        Windows 11's Notepad taskbar button shows just that filename, with no
        "Notepad" text anywhere in it. Left as "Notepad"-only, this returned
        True (claiming everything closed) while a just-saved Notepad window
        sat open full-screen, covering the Explorer window every subsequent
        step needed -- not a corrupted result, just a search term that only
        covered one of Notepad's two taskbar-label states. A caller that just
        saved a file (deposit_text_file) should pass that file's basename
        here so this can still find and close it.

        Uses Ctrl+W, NEVER Alt+F4 -- field-tested live, and this is the second time
        Alt+F4 has caused a real incident in this codebase, not a hypothetical:
          1. An early version searched the WHOLE VDI unscoped and Alt+F4'd whatever
             the (possibly wrong) match clicked -- it closed an unrelated chat app
             and editor that had nothing to do with the pull.
          2. A later version scoped the search to the taskbar strip (ruling out #1)
             and used Alt+F4 to close the whole multi-tab Notepad process in one
             shot instead of one tab at a time. That seemed safe -- taskbar-scoped,
             click-then-verify -- but missed a much bigger danger specific to
             Alt+F4: sent while the DESKTOP itself (not any window) holds focus,
             Windows treats it as "Shut Down Windows" and opens the system shutdown
             dialog. If the taskbar click ever landed wrong, or focus drifted to
             the desktop between the click and the key_combo, Alt+F4 didn't just
             fail quietly -- it tried to shut down the VDI, repeatedly, once per
             file in a batch. Confirmed live.

        Ctrl+W has no equivalent special-case: on the desktop it does nothing at
        all. Worst case here is a mismatched click closes one tab/window of
        whatever it hit -- not remotely comparable to a shutdown dialog. The
        trade-off is real (one tab per iteration is slower than closing a whole
        multi-tab process at once, and a large backlog takes longer to clear) but
        it's the only version of this method that has never caused an incident.
        """
        terms = ["Notepad"] + (extra_terms or [])

        async def _find() -> tuple[int, int] | None:
            for term in terms:
                point = await self._ctl.find_text_point(term, taskbar_only=True)
                if point:
                    return point
            return None

        for _ in range(max_iterations):
            await self._yield_to_user()
            point = await _find()
            if not point:
                # Confirm with a second check rather than trusting one OCR miss --
                # field-tested live: a single miss here returned True (claiming
                # "fully closed") while Notepad was still open full-screen,
                # covering the Explorer window every subsequent step needed. A
                # brief wait + recheck is cheap insurance at what turned out to be
                # a load-bearing point.
                await self._client.wait(400)
                if not await _find():
                    return True
                continue
            await self._client.click(point[0], point[1])
            await self._client.wait(350)
            # CORROBORATE that Notepad is actually in front before sending Ctrl+W.
            # A taskbar click TOGGLES (see CLAUDE.md gotcha 7): clicking the button
            # of an ALREADY-foreground Notepad minimizes it, and the Ctrl+W below
            # then lands on whatever surfaced underneath. Field-tested live
            # 2026-08-24 -- that silently closed the File Explorer window every
            # subsequent step depends on, twice in one session, turning a working
            # session into an unrunnable one with no error anywhere. The generic
            # extra_terms hint makes a mismatch easy too: _ocr_safe_hint(
            # "manifest") is just "export", which also matches this
            # project's own "_archive_<Project>" staging folder in an
            # Explorer taskbar label.
            #
            # One re-click restores a window we merely toggled shut; if Notepad
            # still isn't in front after that, we do NOT guess -- refusing to send
            # Ctrl+W at all is the whole point.
            if not await self._notepad_in_foreground():
                await self._client.click(point[0], point[1])
                await self._client.wait(450)
            if not await self._notepad_in_foreground():
                self._log(
                    "pull: taskbar match did not bring Notepad to the front -- "
                    "refusing to send Ctrl+W at an unidentified window (it would "
                    "close whatever is actually in front). Aborting the close loop."
                )
                return False
            await self._client.key_combo(["Ctrl", "W"])
            await self._client.wait(350)
            save_point = await self._ctl.find_text_point("Don't save")
            if save_point:
                await self._client.click(save_point[0], save_point[1])
                await self._client.wait(300)
            self._activity.mark_self_input()
        return False

    async def print_to_pdf(
        self, remote_path: str, out_pdf: Path, *, landscape: bool = True
    ) -> None:
        """Open `remote_path` in Notepad inside the VDI and print it to `out_pdf` (a
        LOCAL path) via the Horizon-redirected PDF printer. Blocks until the file
        appears and its size stops growing (the print spooler writes it in the
        background after the Save dialog closes).

        Closes every OTHER Notepad tab first (see _close_all_notepad_tabs) so the
        file opened here is unambiguously the only tab -- otherwise Ctrl+P can
        silently print a stale tab instead of the one just opened.
        """
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        if out_pdf.exists():
            out_pdf.unlink()

        # See _ocr_safe_hint's docstring: a plain alphanumeric run survives
        # OCR regardless of how '_'/'.' around it render (confirmed live for
        # both -- '.' dropped entirely, '_' read back as a space).
        taskbar_hint = _ocr_safe_hint(
            Path(remote_path.replace("\\", "/").rsplit("/", 1)[-1]).stem
        )

        if not await self._close_all_notepad_tabs(extra_terms=[taskbar_hint]):
            raise RuntimeError(
                "pull: couldn't fully close pre-existing Notepad tabs -- a stale tab "
                "left open near the address-bar click point is exactly what causes "
                "the address-bar command text to land IN that tab instead of "
                "Explorer (confirmed live: the resulting file content was the "
                "literal `notepad \"<path>\"` command, char-for-char, for every file "
                "pulled while this was silently ignored). Aborting rather than risk "
                "another silently-wrong pull."
            )

        # Opening Notepad WITH the path as an argument skips the File > Open dialog
        # entirely (one less fragile, layout-dependent dialog to drive). Launched via
        # an Explorer address bar, not Win+R -- see _run_via_explorer(). An earlier
        # version ALSO force-reopened the same path via Ctrl+O afterward as a
        # "belt and suspenders" guard against a stale restored tab stealing focus --
        # field-tested live and dropped: Windows 11's Notepad doesn't recognize
        # "this file is already open" and switch to it, it opens a SECOND tab for
        # the same file, which only made tab accumulation worse. Now that
        # _close_all_notepad_tabs() (below and before this call) already clears
        # any existing tabs first, so this launch should be the only tab -- the
        # double-open guard is no longer needed.
        # RETRY the launch itself, not just the surrounding print flow: field-
        # tested live, the exact same click/paste/submit sequence that reliably
        # opens Notepad when driven step-by-step sometimes silently doesn't when
        # run back-to-back with everything else in this function -- no
        # exception anywhere, Notepad's taskbar button just never appears.
        # Likely a race in how quickly the VDI/Horizon session processes input
        # right after the address-bar submit, not a wrong coordinate or a wrong
        # window (both already verified separately). Confirming the taskbar
        # button actually exists before moving on turns a silent no-op into a
        # bounded number of retries instead of a guaranteed downstream failure
        # ("the print dialog never appeared") that gives no hint of the real
        # cause.
        launch_attempts = 3
        for attempt in range(1, launch_attempts + 1):
            await self._run_via_explorer(f'notepad "{remote_path}"')
            await self._client.wait(int(self._open_wait * 1000))
            waited, found = 0.0, False
            while waited < 3.0:
                if await self._ctl.find_text_point(
                    "Notepad", taskbar_only=True
                ) or await self._ctl.find_text_point(taskbar_hint, taskbar_only=True):
                    found = True
                    break
                await self._client.wait(300)
                waited += 0.3
            if found:
                break
            if attempt < launch_attempts:
                self._log(
                    f"pull: Notepad didn't appear after launch attempt "
                    f"{attempt}/{launch_attempts} -- retrying"
                )
        try:
            await self._print_open_notepad(out_pdf, landscape=landscape)
        finally:
            # Direct Ctrl+W first, no taskbar OCR -- same fix and same reason
            # as deposit_text_file's finally block: Notepad should still hold
            # focus from the print flow that just ran, and this VDI's taskbar
            # doesn't OCR reliably for button-label text regardless. The
            # OCR-based sweep (now with taskbar_hint) still runs after as a
            # backstop.
            await self._client.key_combo(["Ctrl", "W"])
            self._activity.mark_self_input()
            await self._client.wait(350)
            save_point = await self._ctl.find_text_point("Don't save")
            if save_point:
                await self._client.click(save_point[0], save_point[1])
                await self._client.wait(300)
            await self._close_all_notepad_tabs(extra_terms=[taskbar_hint])

    async def _print_open_notepad(self, out_pdf: Path, *, landscape: bool) -> None:
        await self._yield_to_user()
        await self._ensure_notepad_focused()
        await self._ensure_word_wrap_off()

        await self._client.key_combo(["Ctrl", "P"])
        self._activity.mark_self_input()
        # Poll for the print dialog itself (its "Printer" label) rather than a fixed
        # wait -- see _poll_text_point's docstring; the same "spawned a new window,
        # fixed wait sometimes fires too early" issue applies here too.
        if not await self._poll_text_point("Printer", timeout=max(self._print_wait, 3.0)):
            raise RuntimeError(
                "pull: the print dialog never appeared after Ctrl+P -- screenshot "
                "the VDI and adjust before retrying"
            )
        if landscape:
            # If the orientation combo currently reads "Portrait", open it and pick
            # Landscape. If we can't find "Portrait" it's already Landscape (e.g.
            # remembered from a prior pull this run) -- nothing to do.
            point = await self._ctl.find_text_point("Portrait")
            if point:
                await self._client.click(point[0], point[1])
                self._activity.mark_self_input()
                await self._client.wait(300)
                await self._click_text("Landscape")
                await self._client.wait(300)
        # The redirected PDF printer is assumed to already be the dialog's default —
        # true in every session this was built against — so printer selection isn't
        # driven here; if that's ever wrong, the save-timeout check below still fails
        # loudly instead of silently producing garbage.
        # Extra settle wait: field-tested live, a click landing right at print_wait's
        # edge (no landscape-switch branch to absorb time) sometimes lands before this
        # WinUI dialog has finished animating in, and it swallows the click silently
        # (no error, dialog just stays open) rather than missing the target -- an
        # immediate manual click on the exact same coordinates right after DID work,
        # which pointed at timing rather than a wrong coordinate.
        await self._client.wait(500)
        # CONFIRM the Print click actually took, don't assume it. Field-tested live
        # 2026-08-24: the run sat with the print dialog still open and untouched
        # while the code had already moved on to waiting for a Save dialog that
        # therefore never came -- this WinUI dialog silently swallows a click that
        # lands while it's still settling (the same failure the 500ms wait above
        # guards against, which evidently isn't always enough). "Printer" is the
        # dialog's own label, so its DISAPPEARANCE is proof the dialog closed.
        #
        # Note also why the needle is "Print" and not something more specific:
        # find_text_point tries exact-line before substring precisely because a raw
        # substring search for "Print" matches "Microsoft Print to PDF (vdi)" (the
        # printer NAME) first -- confirmed live against this exact dialog. If the
        # exact-line tier ever stops matching the button, this loop is what turns
        # that into a named failure instead of a silent stall.
        # ALTERNATE mouse and keyboard. Field-tested live 2026-08-25: this WinUI
        # dialog swallowed three identical mouse clicks in a row on one file
        # (part_008) while the same click worked on the nine either side of it, so
        # "click again, harder" is not a retry strategy -- it just repeats the input
        # path that is failing. Print is this dialog's DEFAULT button, so Enter
        # activates it through a completely different delivery path (keyboard focus
        # rather than hit-testing a coordinate), which is the point: two independent
        # mechanisms, not one tried repeatedly.
        activations: list[tuple[str, object]] = [
            ("click", None), ("Enter", None), ("click", None), ("Enter", None),
        ]
        for attempt, (how, _) in enumerate(activations, 1):
            if how == "click":
                await self._click_text("Print")
            else:
                await self._client.key_combo(["Enter"])
                self._activity.mark_self_input()
            gone = not await self._poll_text_point("Printer", timeout=6.0)
            if gone:
                break
            self._log(
                f"pull: print dialog still open after {how} on Print "
                f"(attempt {attempt}/{len(activations)}) -- input was swallowed; "
                f"trying {'Enter' if how == 'click' else 'a click'} next"
            )
            await self._client.wait(800)
        else:
            await self._dump_diagnostics("print_click_swallowed")
            raise RuntimeError(
                "pull: tried to activate 'Print' four times (two clicks, two Enter "
                "presses) and the print dialog never closed -- this dialog is "
                "refusing input entirely; see the diagnostics dump. Re-running with "
                "--resume reprints only this file, keeping every PDF already verified."
            )

        # Poll for the Save dialog instead of a fixed wait -- field-tested live, a
        # fixed save_wait_seconds sometimes fired before this native dialog (a NEW
        # top-level window spawned after Print closes the WinUI print dialog) was
        # actually up, so the clipboard paste landed on nothing and the filename
        # field stayed empty. "File name" (the field's own label) OCRs reliably here.
        #
        # FLOOR the wait at 15s regardless of save_wait_seconds (config ships 1.2s,
        # which is a settle delay, not a dialog-appearance budget): spawning a native
        # Save dialog after the print job is handed off routinely takes several
        # seconds on this VDI, and being too impatient here doesn't fail cheaply --
        # it throws away a print that actually worked and retries the whole thing.
        label_point = await self._poll_text_point(
            "File name", timeout=max(self._save_wait, 15.0)
        )
        if not label_point:
            raise RuntimeError(
                "pull: couldn't find the Save dialog's 'File name' field -- it may "
                "not have opened; screenshot the VDI and adjust before retrying"
            )
        # CORROBORATE before trusting this point: `find_text_point("File name")`
        # searches the ENTIRE virtual desktop (both monitors), unscoped. A wrong or
        # stale OCR match here is uniquely dangerous -- unlike every other click in
        # this file, this one is immediately followed by Ctrl+A then Enter. If the
        # match is wrong and the click lands on bare desktop instead of the actual
        # Save dialog, Ctrl+A selects desktop ICONS instead of text, and Enter
        # LAUNCHES whatever ended up selected. This was field-tested live and did
        # exactly that -- an unrelated app opened on the VDI with no error anywhere,
        # because every step up to Enter "succeeded" (found text, clicked, typed).
        # A genuine Save dialog always has "Save as type" close by; requiring it
        # here turns a silent wrong-click into a loud, safe failure instead.
        corroboration = await self._ctl.read_screen(
            region=(max(0, label_point[0] - 150), max(0, label_point[1] - 60), 700, 200)
        )
        # Space-STRIPPED comparison, not just lowercased: field-tested live,
        # OCR merged an unrelated breadcrumb into "ThisPC" with no space where
        # one clearly renders on screen (see _locate_explorer_address_bar), and
        # this same corroboration check produced a false-negative abort on a
        # genuinely valid, visibly correct Save dialog -- almost certainly
        # "Save as type" read back as "Saveastype" or similar. Stripping
        # spaces from both sides survives that regardless of exactly where OCR
        # drops or adds one.
        corroboration_nospace = re.sub(r"\s+", "", corroboration.lower())
        if (
            "saveastype" not in corroboration_nospace
            and "cancel" not in corroboration_nospace
        ):
            raise RuntimeError(
                "pull: found 'File name' text but no corroborating Save-dialog text "
                "('Save as type' / 'Cancel') nearby -- this match is likely wrong and "
                "clicking it risks hitting something other than the Save dialog; "
                "aborting rather than blindly sending Ctrl+A + Enter"
            )
        # Click INSIDE the filename edit box, not just the label -- clicking the
        # label text itself doesn't reliably focus the adjacent control. The box
        # runs from just right of the label to the type-dropdown arrow; +250px lands
        # safely inside it in every session this was built against.
        await self._client.click(label_point[0] + 250, label_point[1])
        self._activity.mark_self_input()
        await self._client.wait(150)
        await self._client.key_combo(["Ctrl", "A"])
        self._activity.mark_self_input()
        # Type the FULL absolute local path into the filename field -- Windows' common
        # Save dialog accepts one, which sidesteps folder navigation entirely.
        await self._set_clipboard_verified(str(out_pdf))
        await self._client.key_combo(["Ctrl", "V"])
        self._activity.mark_self_input()
        await self._client.wait(200)
        # Submit via Enter, not a "Save" OCR click -- field-tested live, the Save
        # button's own label didn't reliably OCR at all in this dialog (unlike
        # "Cancel", which did), while Enter is the standard, reliable submit for a
        # native Windows common Save dialog once the filename field has focus.
        await self._client.key_combo(["Enter"])
        self._activity.mark_self_input()

        waited = 0.0
        last_size = -1
        while waited < self._save_timeout:
            await self._client.wait(int(self._save_poll * 1000))
            waited += self._save_poll
            if out_pdf.exists():
                size = out_pdf.stat().st_size
                if size > 0 and size == last_size:
                    return
                last_size = size
        raise RuntimeError(
            f"pull: {out_pdf} never stabilized after {self._save_timeout}s -- the "
            "print/save dialog may not have completed as expected; screenshot the VDI"
        )

    async def pull_file(
        self, remote_path: str, local_dest: Path, *, pdf_dir: Path | None = None
    ) -> PullResult:
        """print_to_pdf() + reconstruct_from_pdf(), written to `local_dest`.

        For a .py destination, verifies the reconstruction compiles and reports that
        in the result rather than silently trusting it. Other extensions are written
        as-is with no correctness check.
        """
        pdf_dir = pdf_dir or local_dest.parent
        pdf_path = pdf_dir / (local_dest.name + ".pdf")
        self._log(f"pull: printing {remote_path} -> {pdf_path}")
        try:
            await self.print_to_pdf(remote_path, pdf_path)
            source = reconstruct_from_pdf(pdf_path)
        except Exception as exc:  # noqa: BLE001 — reported in the result, not raised
            self._log(f"pull: FAILED {remote_path} — {exc}")
            return PullResult(remote_path, local_dest, ok=False, error=str(exc))

        ok, error = True, ""
        if looks_like_leaked_launch_command(source, remote_path):
            ok, error = False, (
                "pulled content is literally the notepad launch command, not the "
                "file -- the address-bar paste landed in a stale Notepad tab "
                "instead of Explorer; re-print after confirming Notepad is closed"
            )
        elif local_dest.suffix == ".py":
            ok, error = verify_python(source, filename=local_dest.name)
            if not ok:
                repaired, repaired_ok, repair_msg = repair_indentation(
                    source, filename=local_dest.name
                )
                if repaired_ok:
                    # Still surfaced as needing review, not silently "OK" -- a
                    # successful repair means it compiles, not that it's
                    # byte-perfect (see repair_indentation's docstring).
                    source, error = repaired, repair_msg

        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_text(source, encoding="utf-8", newline="\n")
        self._log(
            f"pull: {'OK' if ok else 'NEEDS REVIEW'} {local_dest} ({len(source)} chars)"
            f"{f' — {error}' if error else ''}"
        )
        return PullResult(remote_path, local_dest, ok=ok, chars=len(source), error=error)

    async def pull_files(
        self, remote_paths: list[str], local_root: Path, *, pdf_dir: Path | None = None
    ) -> list[PullResult]:
        """Pull each of `remote_paths` (absolute remote paths) into `local_root`,
        preserving each file's basename only (not its remote directory structure —
        callers that need that should build remote_path -> local_dest pairs
        themselves and call pull_file() directly)."""
        results = []
        for remote_path in remote_paths:
            name = remote_path.replace("\\", "/").rsplit("/", 1)[-1]
            results.append(
                await self.pull_file(remote_path, local_root / name, pdf_dir=pdf_dir)
            )
        return results

    async def deposit_text_file(self, remote_path: str, content: str) -> None:
        """Push `content` into a NEW file at `remote_path` inside the VDI: blank
        Notepad + clipboard paste + Save-As -- the mirror image of pull_file(), used
        by src/pull_payload.py's PayloadPuller only as a fallback when a whole
        export script can't fit in a single -EncodedCommand invocation and has to
        be deposited as a real .ps1 file instead.

        Unlike print_to_pdf's Save dialog (which saves to LOCAL disk and can be
        confirmed by polling the resulting file's size), this writes to the REMOTE
        filesystem, which this process has no direct way to inspect. Completion is
        inferred from the Save dialog's corroborated OCR text closing after Enter --
        the same trust level unlock() already relies on for remote-side state it
        can't otherwise observe. NOT field-tested live (only exercised when the
        primary -EncodedCommand path is too long for a given VDI's command-line
        length ceiling).
        """
        basename = remote_path.replace("\\", "/").rsplit("/", 1)[-1]
        # See _ocr_safe_hint's docstring: a taskbar button truncates a long
        # filename with no ellipsis (so the hint must be short), and OCR
        # doesn't render '_'/'.' consistently either (confirmed live: '.'
        # dropped, '_' read back as a space) -- a plain alphanumeric run
        # sidesteps both failure modes at once.
        taskbar_hint = _ocr_safe_hint(Path(basename).stem)
        if not await self._close_all_notepad_tabs(extra_terms=[taskbar_hint]):
            await self._dump_diagnostics("deposit_notepad_tabs_stuck")
            raise RuntimeError(
                "pull: couldn't fully close pre-existing Notepad tabs -- aborting "
                "rather than risk pasting into a stale tab"
            )
        await self._run_via_explorer("notepad")
        await self._client.wait(int(self._open_wait * 1000))
        await self._ensure_notepad_focused()
        try:
            await self._yield_to_user()
            await self._set_clipboard_verified(content, exact=False)

            # Verify the paste actually landed in Notepad before trusting it --
            # field-tested live, ROOT-CAUSED (see _ensure_notepad_focused's
            # docstring): the real failure was Alt+Tab switching focus AWAY from
            # Notepad, which that method no longer does preemptively. This check
            # stays anyway as a safety net for the rare genuine focus-drift case
            # the old Alt+Tab was originally trying to guard against -- if a
            # retry is actually needed, Alt+Tab (only NOW, not before every
            # attempt) is a reasonable recovery: it switches to the
            # most-recently-active window, which if focus really did drift away
            # from Notepad, is exactly it.
            #
            # Snippet taken from the END of `content`, not the start -- field-
            # tested live: an earlier version searched for an early line and got
            # a false "never landed" on every attempt despite the paste actually
            # working (confirmed via diagnostics screenshot, "7,091 characters"
            # in the status bar). Root cause: Ctrl+V leaves the cursor at the
            # END of the pasted text, and Notepad auto-scrolls its viewport to
            # keep the cursor visible -- for a multi-hundred-line paste, an
            # early-content snippet is scrolled off-screen the instant the paste
            # completes, so it can never appear in a screenshot/OCR check no
            # matter how many times it's retried. A line from the end is exactly
            # what the viewport shows right after pasting.
            snippet = next(
                (ln.strip()[:30] for ln in reversed(content.splitlines()) if len(ln.strip()) >= 15),
                "",
            )
            pasted = not snippet  # nothing distinctive to check -- trust it, as before
            for paste_attempt in range(1, 4):
                if paste_attempt > 1:
                    await self._client.key_combo(["Alt", "Tab"])
                    self._activity.mark_self_input()
                    await self._client.wait(400)
                await self._client.key_combo(["Ctrl", "A"])
                await self._client.key_combo(["Ctrl", "V"])
                self._activity.mark_self_input()
                await self._client.wait(500)
                if not snippet:
                    break
                visible = await self._ctl.read_screen()
                if snippet in visible:
                    pasted = True
                    break
                self._log(
                    f"pull: pasted content not visible on attempt "
                    f"{paste_attempt}/3 -- retrying"
                )
                await self._client.wait(500)
            if not pasted:
                await self._dump_diagnostics("deposit_paste_never_landed")
                raise RuntimeError(
                    f"pull: pasted content never appeared in Notepad while "
                    f"depositing {remote_path} after 3 attempts -- the remote "
                    "clipboard likely never received it despite the local "
                    "round-trip check passing; screenshot the VDI"
                )

            await self._client.wait(300)
            await self._client.key_combo(["Ctrl", "S"])
            self._activity.mark_self_input()

            # Floor of 3.0s regardless of the configured save_wait_seconds -- same
            # reasoning as _print_open_notepad's "Printer" poll just below: that
            # config value tunes how long to wait before interacting with an
            # ALREADY-open dialog, not how long a brand-new Save-As window takes to
            # spawn in the first place. Field-tested live: the raw configured
            # 1.2s was too tight here and produced a false "never opened" failure.
            label_point = await self._poll_text_point(
                "File name", timeout=max(self._save_wait, 3.0)
            )
            if not label_point:
                await self._dump_diagnostics("deposit_save_dialog_not_found")
                raise RuntimeError(
                    f"pull: couldn't find the Save dialog's 'File name' field while "
                    f"depositing {remote_path} -- it may not have opened; "
                    "screenshot the VDI"
                )
            # Same corroboration as _print_open_notepad's Save dialog -- see that
            # method's docstring for why a bare "File name" match isn't trusted
            # alone before sending Ctrl+A + Enter.
            corroboration = await self._ctl.read_screen(
                region=(max(0, label_point[0] - 150), max(0, label_point[1] - 60), 700, 200)
            )
            corroboration_nospace = re.sub(r"\s+", "", corroboration.lower())
            if (
                "saveastype" not in corroboration_nospace
                and "cancel" not in corroboration_nospace
            ):
                await self._dump_diagnostics("deposit_save_dialog_uncorroborated")
                raise RuntimeError(
                    "pull: found 'File name' text but no corroborating Save-dialog "
                    "text nearby while depositing a file -- aborting rather than "
                    "blindly typing a path into whatever this actually is"
                )
            await self._client.click(label_point[0] + 250, label_point[1])
            self._activity.mark_self_input()
            await self._client.wait(150)
            await self._client.key_combo(["Ctrl", "A"])
            self._activity.mark_self_input()
            await self._set_clipboard_verified(remote_path)
            await self._client.key_combo(["Ctrl", "V"])
            self._activity.mark_self_input()
            await self._client.wait(200)
            await self._client.key_combo(["Enter"])
            self._activity.mark_self_input()

            # Windows may show an ADDITIONAL "Confirm Save As" dialog if
            # `remote_path` already exists (e.g. a stale deposit left over from an
            # earlier attempt that failed before reaching cleanup) --
            # "<name> already exists. Do you want to replace it?" with Yes/No.
            # Field-tested live: left unhandled, this blocking dialog sat on
            # screen and was the actual reason the NEXT step's Explorer-window
            # search came up empty -- not a timing or OCR issue at all, just an
            # unanticipated dialog covering everything. Poll briefly; if it never
            # appears (the common case, a fresh deposit), move on immediately
            # rather than waiting out the full timeout.
            overwrite_point = await self._poll_text_point("already exists", timeout=2.5)
            if overwrite_point:
                # This is a classic Win32 MessageBox (Explorer's built-in Save-As
                # replace-confirmation), not a WinUI dialog like the rest of this
                # module deals with -- field-tested live via a direct OCR scan of
                # the exact button region: Windows OCR reliably reads the dialog's
                # STATIC text ("... already exists. Do you want to replace it?")
                # but returns NOTHING for the Yes/No BUTTON labels themselves, so
                # find_text_point("Yes") can never succeed here no matter how it's
                # scoped. Use the standard Windows message-box access key instead
                # of OCR+click: "Y" always activates "&Yes" regardless of which
                # button is the visual default -- and here "No" (the safer,
                # non-destructive option) IS the default, so Enter would do the
                # WRONG thing.
                await self._client.key_combo(["Y"])
                self._activity.mark_self_input()
                await self._client.wait(300)

            await self._client.wait(int(self._save_wait * 1000))
        finally:
            # Direct Ctrl+W FIRST, no taskbar OCR -- field-tested live: this
            # VDI's taskbar simply doesn't OCR reliably for button-label text.
            # Confirmed by direct OCR scans (both a full-taskbar sweep and a
            # tight crop right over the button in question) that came back
            # empty or reading a completely DIFFERENT button's label, despite
            # the target text being clearly visible in a screenshot at that
            # exact spot -- not a scoping/coordinate mistake, a genuine OCR
            # miss on this control style. Notepad still holds keyboard focus
            # immediately after the Ctrl+S save just performed, so Ctrl+W
            # reaches it directly with no lookup needed. The OCR-based sweep
            # still runs afterward as a backstop in case focus had already
            # drifted away before this point.
            await self._client.key_combo(["Ctrl", "W"])
            self._activity.mark_self_input()
            await self._client.wait(350)
            save_point = await self._ctl.find_text_point("Don't save")
            if save_point:
                await self._client.click(save_point[0], save_point[1])
                await self._client.wait(300)
            await self._close_all_notepad_tabs(extra_terms=[taskbar_hint])
