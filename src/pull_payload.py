"""
Byte-exact whole-project pull via a single base64 payload print.

DECISIONS.md retired the old per-file pull (pull.py's print-to-PDF + `-table`/
`-lineprinter` reconstruction, driven in batch by remote_tree.py): 1 of 33 files
pulled correctly in three days, because reconstructing indentation from a printed
page is fundamentally a guess. This module is the pivot it names instead: zip the
whole project on the remote, base64-encode the zip, print THAT (one plain ASCII
stream, immune to line-wrap/indentation ambiguity) as one or a few text files, and
verify byte-exactness locally with SHA-256 rather than inferring it from "it
compiles".

Two layers:
  * Pure, offline, unit-testable parsing/verification (parse_export_text,
    verify_and_assemble, extract_zip_bytes, parse_manifest_text, parse_contents,
    check_completeness) -- no VDI/network I/O, mirrors pull.py's
    merge_table_and_lineprinter/verify_python split.
  * PayloadPuller -- drives the remote: runs src/remote_scripts/project_archive.ps1
    (via a single -EncodedCommand, falling back to depositing it as a .ps1 file if
    the encoded command would be too long), waits for its manifest, and prints each
    produced export file via ProjectPuller.print_to_pdf() COMPLETELY UNCHANGED --
    that method doesn't know or care what text it's printing.

Two properties this module is built around, both learned the hard way (DECISIONS.md):

  RESUMABILITY. Printing is the only slow, GUI-driven, open-loop step (~2 min per
  export file). A run that requires all n prints to succeed goes as p**n, so raising
  n eventually beats any per-print reliability: at p=0.98, 10 prints succeed 82% of
  the time and 40 prints only 45%. `--resume` reuses any export PDF that parses and
  whose per-chunk hashes AND batch total_sha256 verify, turning all-or-nothing into
  retry-until-done. That is why the remote staging dir is cleaned up only on success,
  and why --resume deliberately does NOT re-run the export (a re-zip would change
  total_sha256 and invalidate every PDF already printed).

  COMPLETENESS =/= BYTE-EXACTNESS. SHA-256 over the zip proves the bytes that arrived
  are the bytes that left. It says nothing about what never entered the zip -- a file
  over MaxFileBytes, or a real source directory that happens to be named `bin`/
  `build`/`dist`. The export script therefore stages its own inventory
  (`_contents.txt`) INSIDE the zip, and check_completeness() reconciles it
  against what actually landed on disk.

See project_archive.ps1's own docstring for the remote-side half and the exact
marker format it writes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .pull import ProjectPuller, _pdftotext
from .remote_tree import DEFAULT_EXCLUDE_DIRS

DEFAULT_CHUNK_CHARS = 4000
DEFAULT_MAX_CHARS_PER_FILE = 200_000
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_STAGING_DIR_NAME = "_archive"

# Encoded commands are pasted (not typed) via ProjectPuller.run_remote_command's
# existing clipboard-verified paste, so the limiting factor is the OS/shell
# command-line length ceiling (~8191 chars for CreateProcess), not typing
# reliability. Kept comfortably under that; if the finished command exceeds this,
# PayloadPuller automatically falls back to depositing the script as a .ps1 file
# and invoking it with -File instead (see PayloadPuller._run_export).
ENCODED_COMMAND_MAX_LEN = 8000

_SCRIPT_PATH = Path(__file__).parent / "remote_scripts" / "project_archive.ps1"
_BODY_MARKER = "#BODY-START#"


# --------------------------------------------------------------------------- #
# Pure parsing / verification / extraction -- no I/O beyond what's passed in,  #
# unit-testable offline against a synthetic export text (see tests).          #
# --------------------------------------------------------------------------- #

_HEADER_RE = re.compile(
    r"===ARCHIVE-BEGIN\s+project=(?P<project>.+?)\s+file=(?P<file_index>\d+)/"
    r"(?P<num_files>\d+)\s+chunks=(?P<first_chunk>\d+)-(?P<last_chunk>\d+)/"
    r"(?P<total_chunks>\d+)\s+total_sha256=(?P<total_sha256>[0-9a-fA-F]{64})\s+"
    r"zip_bytes=(?P<zip_bytes>\d+)==="
)
_CHUNK_RE = re.compile(r"---CHUNK\s+(?P<index>\d+)\s+sha256=(?P<sha256>[0-9a-fA-F]{64})---")
_END_RE = re.compile(r"===ARCHIVE-END===")


@dataclass
class ExportHeader:
    project: str
    file_index: int
    num_files: int
    first_chunk: int
    last_chunk: int
    total_chunks: int
    total_sha256: str
    zip_bytes: int


@dataclass
class ExportChunk:
    index: int
    sha256: str
    text: str  # already whitespace-stripped


@dataclass
class ExportChunkFile:
    header: ExportHeader
    chunks: list[ExportChunk] = field(default_factory=list)


def parse_export_text(text: str) -> ExportChunkFile:
    """Parse one printed-and-extracted export file's text into its header + chunks.

    Tolerant of how `pdftotext -raw` reflows a page: markers are matched by
    searching the WHOLE text (by character offset), not line by line, and
    everything between one marker and the next is treated as that marker's
    content with ALL whitespace stripped before hashing -- base64 doesn't care
    about embedded newlines or spaces, only the character sequence.

    Searching per-LINE (an earlier version of this function) missed a real,
    field-tested failure mode: the header line is long enough (project name +
    chunk range + a 64-hex-char SHA-256 + byte count) that it can itself wrap
    across a printed-page line break, landing its `total_sha256=...` field on
    a physically different line than `chunks=...` -- confirmed live via a real
    printed PDF, `pdftotext -raw` inserted a bare `\\r\\n` mid-header with no
    marker of any kind at the wrap point. `_HEADER_RE` already tolerates that
    (`\\s+` between fields matches an embedded newline fine); the bug was
    asking it to match one LINE at a time instead of the full text, so neither
    half-line alone ever contained the whole pattern. Chunk marker lines are
    short enough that this hasn't been observed for them, but working in
    character offsets throughout (not just for the header) costs nothing and
    removes the whole class of failure rather than one instance of it.

    Raises ValueError (never silently returns partial data) if the BEGIN marker is
    missing entirely (the equivalent of pull.py's looks_like_leaked_launch_command
    guard -- the classic "printed a stale Notepad tab instead of the export file"
    failure), if END is missing (truncated PDF), or if the chunk count/order found
    doesn't match what the header declares.
    """
    header_match = _HEADER_RE.search(text)
    if header_match is None:
        raise ValueError(
            "ARCHIVE-BEGIN marker not found -- this PDF doesn't look like an "
            "export payload (wrong file printed, or a stale Notepad tab was printed "
            "instead of the export file)"
        )
    header = ExportHeader(
        project=header_match.group("project"),
        file_index=int(header_match.group("file_index")),
        num_files=int(header_match.group("num_files")),
        first_chunk=int(header_match.group("first_chunk")),
        last_chunk=int(header_match.group("last_chunk")),
        total_chunks=int(header_match.group("total_chunks")),
        total_sha256=header_match.group("total_sha256").lower(),
        zip_bytes=int(header_match.group("zip_bytes")),
    )

    end_match = _END_RE.search(text, header_match.end())
    if end_match is None:
        raise ValueError(
            f"ARCHIVE-END marker not found for project {header.project!r} "
            f"(file {header.file_index}/{header.num_files}) -- the PDF may be "
            "truncated/incomplete; reprint this export file"
        )

    chunk_matches = [
        m for m in _CHUNK_RE.finditer(text, header_match.end())
        if m.start() < end_match.start()
    ]
    chunks: list[ExportChunk] = []
    for i, m in enumerate(chunk_matches):
        content_end = (
            chunk_matches[i + 1].start() if i + 1 < len(chunk_matches) else end_match.start()
        )
        content = re.sub(r"\s+", "", text[m.end():content_end])
        chunks.append(
            ExportChunk(index=int(m.group("index")), sha256=m.group("sha256").lower(), text=content)
        )

    expected_indices = list(range(header.first_chunk, header.last_chunk + 1))
    if [c.index for c in chunks] != expected_indices:
        raise ValueError(
            f"export file {header.file_index}/{header.num_files} for project "
            f"{header.project!r} should contain chunks {expected_indices}, found "
            f"{[c.index for c in chunks]} -- the PDF extraction likely dropped or "
            "merged lines; reprint this export file"
        )

    return ExportChunkFile(header=header, chunks=chunks)


def verify_and_assemble(chunk_files: list[ExportChunkFile]) -> bytes:
    """Verify every chunk across ALL of a payload's export files and reassemble the
    original zip bytes.

    Requires every export file (1..num_files) to be present -- names exactly which
    are missing otherwise. Verifies each chunk's SHA-256 individually before
    concatenating, naming the exact chunk index AND which export file (so the
    caller knows precisely which single print job to redo) on the first mismatch,
    rather than only detecting corruption after decoding. The final base64-decode +
    whole-zip SHA-256/length check is a backstop, not the primary defense -- it
    should never fire if every per-chunk hash already passed.
    """
    if not chunk_files:
        raise ValueError("no export chunk files provided")

    first = chunk_files[0].header
    by_file_index: dict[int, ExportChunkFile] = {}
    for cf in chunk_files:
        h = cf.header
        if (h.project, h.num_files, h.total_chunks, h.total_sha256, h.zip_bytes) != (
            first.project, first.num_files, first.total_chunks, first.total_sha256, first.zip_bytes
        ):
            raise ValueError(
                f"export file {h.file_index}/{h.num_files} header disagrees with "
                f"file {first.file_index}/{first.num_files} on project/total_chunks/"
                "total_sha256/zip_bytes -- these don't belong to the same export batch"
            )
        if h.file_index in by_file_index:
            raise ValueError(f"export file index {h.file_index} was provided more than once")
        by_file_index[h.file_index] = cf

    missing_files = sorted(set(range(1, first.num_files + 1)) - set(by_file_index))
    if missing_files:
        raise ValueError(
            f"missing export file(s) {missing_files} of {first.num_files} for "
            f"project {first.project!r} -- print and decode those part_NNN.txt "
            "files too before assembling"
        )

    all_chunks: dict[int, ExportChunk] = {}
    chunk_to_file: dict[int, int] = {}
    for file_index, cf in by_file_index.items():
        for chunk in cf.chunks:
            if chunk.index in all_chunks:
                raise ValueError(f"chunk {chunk.index} appears in more than one export file")
            all_chunks[chunk.index] = chunk
            chunk_to_file[chunk.index] = file_index

    missing_chunks = sorted(set(range(1, first.total_chunks + 1)) - set(all_chunks))
    if missing_chunks:
        raise ValueError(
            f"missing chunk(s) {missing_chunks} of {first.total_chunks} for "
            f"project {first.project!r}"
        )

    pieces: list[str] = []
    for idx in range(1, first.total_chunks + 1):
        chunk = all_chunks[idx]
        actual = hashlib.sha256(chunk.text.encode("ascii", errors="replace")).hexdigest()
        if actual != chunk.sha256:
            raise ValueError(
                f"chunk {idx} failed SHA-256 verification (export file "
                f"{chunk_to_file[idx]}/{first.num_files}, project {first.project!r}) "
                f"-- reprint export file {chunk_to_file[idx]}"
            )
        pieces.append(chunk.text)

    try:
        raw = base64.b64decode("".join(pieces), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"base64 decode failed for project {first.project!r} despite all "
            f"per-chunk hashes passing -- {exc}"
        ) from exc

    if len(raw) != first.zip_bytes:
        raise ValueError(
            f"decoded {len(raw)} bytes, expected {first.zip_bytes} for project "
            f"{first.project!r}"
        )
    if hashlib.sha256(raw).hexdigest() != first.total_sha256:
        raise ValueError(
            f"decoded zip's SHA-256 doesn't match the declared total_sha256 for "
            f"project {first.project!r}, despite every per-chunk hash passing -- "
            "this should not happen; re-export and re-print from scratch"
        )
    return raw


def extract_zip_bytes(zip_bytes: bytes, dest_dir: Path) -> list[str]:
    """Extract `zip_bytes` into `dest_dir`, rejecting any entry that would land
    outside it (zip-slip) before extracting anything."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names: list[str] = []
        for info in zf.infolist():
            target = (dest_dir / info.filename).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(
                    f"zip entry {info.filename!r} would extract outside {dest_dir} "
                    "-- rejecting (zip-slip)"
                )
            if not info.is_dir():
                names.append(info.filename)
        zf.extractall(dest_dir)
    return names


def reconstruct_raw(pdf_path: Path) -> str:
    """`pdftotext -raw` only -- no `-table`/`-lineprinter` merge needed here (unlike
    pull.py's reconstruct_from_pdf): base64 has no indentation to guess, so a single
    plain extraction plus the hash checks above is sufficient."""
    return _pdftotext(pdf_path, "raw")


# --------------------------------------------------------------------------- #
# Manifest + completeness report (both pure, both offline-testable).          #
# --------------------------------------------------------------------------- #

_MANIFEST_RE = re.compile(
    r"===ARCHIVE-MANIFEST\s+project=(?P<project>.+?)\s+files=(?P<num_files>\d+)\s+"
    r"chunks=(?P<total_chunks>\d+)\s+total_sha256=(?P<total_sha256>[0-9a-fA-F]{64})\s+"
    r"zip_bytes=(?P<zip_bytes>\d+)\s+kept=(?P<kept>\d+)\s+skipped=(?P<skipped>\d+)\s+"
    r"report=(?P<report>\S+?)==="
)
_MANIFEST_END_RE = re.compile(r"===ARCHIVE-MANIFEST-END===")
_EXPORT_NAME_RE = re.compile(r"part_\d+\.txt")

_REPORT_BEGIN = "===ARCHIVE-CONTENTS==="
_REPORT_END = "===ARCHIVE-CONTENTS-END==="


@dataclass
class ExportManifest:
    project: str
    num_files: int
    total_chunks: int
    total_sha256: str
    zip_bytes: int
    kept: int
    skipped: int
    report_name: str
    names: list[str]


def parse_manifest_text(text: str) -> ExportManifest:
    """Parse manifest.txt as printed-and-extracted from a PDF.

    Same tolerance discipline as parse_export_text (and for the same field-tested
    reason): match markers by character offset over the WHOLE text rather than line
    by line, because the header line is long enough to wrap across a printed page
    break. Export names are then recovered by pattern (`part_NNN.txt`) rather than
    by splitting on newlines, so a reflowed page that packs two names onto one line
    parses identically.

    Raises ValueError rather than returning partial data when the markers are
    missing -- the manifest is the one file printed before any payload file, so a
    stale Notepad tab printed in its place would otherwise silently define the whole
    batch as empty.
    """
    m = _MANIFEST_RE.search(text)
    if m is None:
        raise ValueError(
            "ARCHIVE-MANIFEST marker not found -- this PDF doesn't look like an "
            "export manifest (wrong file printed, a stale Notepad tab printed instead "
            "of the manifest, or the remote export script is an older version that "
            "wrote a bare filename list)"
        )
    end = _MANIFEST_END_RE.search(text, m.end())
    if end is None:
        raise ValueError(
            "ARCHIVE-MANIFEST-END marker not found -- the manifest PDF may be "
            "truncated; reprint it"
        )
    names = _EXPORT_NAME_RE.findall(text[m.end():end.start()])
    manifest = ExportManifest(
        project=m.group("project"),
        num_files=int(m.group("num_files")),
        total_chunks=int(m.group("total_chunks")),
        total_sha256=m.group("total_sha256").lower(),
        zip_bytes=int(m.group("zip_bytes")),
        kept=int(m.group("kept")),
        skipped=int(m.group("skipped")),
        report_name=m.group("report"),
        names=names,
    )
    if len(names) != manifest.num_files:
        raise ValueError(
            f"manifest declares {manifest.num_files} export file(s) but lists "
            f"{len(names)} ({names}) -- the PDF extraction likely dropped a line; "
            "reprint the manifest"
        )
    return manifest


@dataclass
class ExportReport:
    """The remote's own inventory of what it put in the zip, and what it left out."""
    project: str
    root: str
    max_file_bytes: int
    exclude_dirs: list[str]
    kept: dict[str, tuple[int, str]]      # relpath -> (bytes, sha256)
    skipped: list[tuple[str, int, str]]   # (relpath, bytes, reason)
    excluded_dirs_hit: dict[str, int]     # excluded dir name -> files dropped


def parse_contents(text: str) -> ExportReport:
    """Parse the `_contents.txt` that rides back inside the zip.

    Byte-exact by construction (it travelled inside the SHA-256-verified zip, not
    through a print job), so this parser doesn't need the marker tolerance
    parse_manifest_text has -- it only needs to reject a same-named file that the
    project itself happened to contain, which the BEGIN marker does.
    """
    if not text.startswith(_REPORT_BEGIN):
        raise ValueError("not an export report (missing ARCHIVE-CONTENTS marker)")
    fields: dict[str, str] = {}
    kept: dict[str, tuple[int, str]] = {}
    skipped: list[tuple[str, int, str]] = []
    excluded: dict[str, int] = {}
    section = "head"
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line in (_REPORT_BEGIN, _REPORT_END) or not line:
            continue
        if line.startswith("---KEPT"):
            section = "kept"
            continue
        if line.startswith("---SKIPPED"):
            section = "skipped"
            continue
        if line.startswith("---EXCLUDED-DIRS"):
            section = "excluded"
            continue
        if section == "head":
            key, _, value = line.partition("=")
            fields[key.strip()] = value
        elif section == "kept":
            rel, _, rest = line.partition("\t")
            size, _, sha = rest.partition("\t")
            kept[rel.replace("\\", "/")] = (int(size or 0), sha.strip().lower())
        elif section == "skipped":
            rel, _, rest = line.partition("\t")
            size, _, reason = rest.partition("\t")
            skipped.append((rel.replace("\\", "/"), int(size or 0), reason))
        elif section == "excluded":
            name, _, count = line.partition("\t")
            excluded[name] = int(count or 0)
    return ExportReport(
        project=fields.get("project", ""),
        root=fields.get("root", ""),
        max_file_bytes=int(fields.get("max_file_bytes", 0) or 0),
        exclude_dirs=[d for d in fields.get("exclude_dirs", "").split(",") if d],
        kept=kept,
        skipped=skipped,
        excluded_dirs_hit=excluded,
    )


def find_contents(zip_bytes: bytes) -> ExportReport | None:
    """Locate and parse the export report inside an already-verified payload zip.

    Found by marker, not by name: the report is staged as `_contents.txt` at the
    zip root, but the export script renames it (`_contents_1.txt`, ...) if the
    project itself already contains that path, so trusting the name alone would parse
    a project file as a report. Returns None for a payload produced by an export
    script predating the report (the pull still succeeds -- it just can't offer a
    completeness verdict).
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir() or "/" in info.filename or "\\" in info.filename:
                continue
            if not info.filename.startswith("_contents"):
                continue
            try:
                text = zf.read(info).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 -- a corrupt entry just isn't the report
                continue
            if text.startswith(_REPORT_BEGIN):
                return parse_contents(text)
    return None


@dataclass
class CompletenessCheck:
    """Whether the local copy actually contains everything the remote said it sent."""
    report: ExportReport | None
    extracted: list[str]
    missing: list[str] = field(default_factory=list)     # in report, not extracted
    unexpected: list[str] = field(default_factory=list)  # extracted, not in report
    size_mismatches: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.report is not None

    @property
    def ok(self) -> bool:
        return self.available and not (
            self.missing or self.unexpected or self.size_mismatches
        )

    def summary(self) -> str:
        if not self.available:
            return (
                "completeness: UNKNOWN -- payload has no _contents.txt (remote "
                "export script predates it); byte-exactness is still SHA-256 proven"
            )
        r = self.report
        assert r is not None
        parts = [f"completeness: {len(r.kept)} file(s) declared, {len(self.extracted)} extracted"]
        if self.ok:
            parts[0] = "completeness: OK -- " + parts[0].split(": ", 1)[1]
        for label, items in (
            ("MISSING", self.missing),
            ("UNEXPECTED", self.unexpected),
            ("SIZE MISMATCH", self.size_mismatches),
        ):
            if items:
                shown = ", ".join(items[:8]) + (" ..." if len(items) > 8 else "")
                parts.append(f"{label} ({len(items)}): {shown}")
        if r.skipped:
            shown = ", ".join(f"{p} ({n} bytes)" for p, n, _ in r.skipped[:8])
            parts.append(
                f"SKIPPED for size ({len(r.skipped)}, NOT pulled -- raise "
                f"[pull_payload].max_file_bytes to include them): {shown}"
            )
        if r.excluded_dirs_hit:
            shown = ", ".join(f"{k}={v}" for k, v in sorted(r.excluded_dirs_hit.items()))
            parts.append(f"excluded by dir rule (files): {shown}")
        return "\n  ".join(parts)


def check_completeness(zip_bytes: bytes, extracted: list[str], dest_dir: Path) -> CompletenessCheck:
    """Compare what the remote said it staged against what actually landed on disk.

    The zip's SHA-256 already proves the BYTES are right; this proves nothing was
    silently left out on the way in -- a distinct property, and the one that
    `DECISIONS.md` flags as unproven for this transport.
    """
    report = find_contents(zip_bytes)
    extracted_norm = sorted(n.replace("\\", "/") for n in extracted)
    check = CompletenessCheck(report=report, extracted=extracted_norm)
    if report is None:
        return check
    declared = set(report.kept)
    # The report itself is staged into the zip after the inventory is taken, so it is
    # legitimately present on disk and legitimately absent from `kept`.
    found = {n for n in extracted_norm if not n.startswith("_contents")}
    check.missing = sorted(declared - found)
    check.unexpected = sorted(found - declared)
    for rel in sorted(declared & found):
        want_size, _ = report.kept[rel]
        local = dest_dir / rel
        if local.exists() and local.stat().st_size != want_size:
            check.size_mismatches.append(
                f"{rel} (remote {want_size} vs local {local.stat().st_size})"
            )
    return check


# --------------------------------------------------------------------------- #
# Remote-side script assembly.                                                #
# --------------------------------------------------------------------------- #


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ps_string_array(items: list[str]) -> str:
    return "@(" + ",".join(_ps_quote(i) for i in items) + ")"


def _load_script_body() -> str:
    script = _SCRIPT_PATH.read_text(encoding="utf-8")
    idx = script.find(_BODY_MARKER)
    if idx == -1:
        raise RuntimeError(f"{_SCRIPT_PATH} is missing the {_BODY_MARKER!r} split marker")
    return script[idx + len(_BODY_MARKER):].lstrip("\r\n")


def build_encoded_command_line(
    *, project_root: str, out_dir: str, project_name: str,
    exclude_dirs: list[str] | None, max_file_bytes: int, chunk_chars: int,
    max_chars_per_file: int,
) -> str:
    """One self-contained `powershell -EncodedCommand ...` line: the script's body
    (everything after #BODY-START#) with $ProjectRoot/$OutDir/etc. assigned as plain
    variables up front INSTEAD OF the file's param() block -- a script run via
    -EncodedCommand has no separate argument list to bind to param()."""
    body = _load_script_body()
    assignments = [
        f"$ProjectRoot = {_ps_quote(project_root)}",
        f"$OutDir = {_ps_quote(out_dir)}",
        f"$ProjectName = {_ps_quote(project_name)}",
        f"$MaxFileBytes = {int(max_file_bytes)}",
        f"$ChunkChars = {int(chunk_chars)}",
        f"$MaxCharsPerFile = {int(max_chars_per_file)}",
        f"$ExcludeDirs = {_ps_string_array(list(exclude_dirs) if exclude_dirs is not None else list(DEFAULT_EXCLUDE_DIRS))}",
    ]
    script_text = "\n".join(assignments) + "\n" + body
    encoded = base64.b64encode(script_text.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def build_file_command_line(
    script_remote_path: str, *, project_root: str, out_dir: str, project_name: str,
    exclude_dirs: list[str] | None, max_file_bytes: int, chunk_chars: int,
    max_chars_per_file: int,
) -> str:
    """Short one-line invocation of an already-deposited .ps1 file with normal
    named arguments -- the -File fallback (see module docstring)."""
    parts = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        f'"{script_remote_path}"',
        "-ProjectRoot", f'"{project_root}"',
        "-OutDir", f'"{out_dir}"',
        "-ProjectName", f'"{project_name}"',
        "-MaxFileBytes", str(int(max_file_bytes)),
        "-ChunkChars", str(int(chunk_chars)),
        "-MaxCharsPerFile", str(int(max_chars_per_file)),
    ]
    if exclude_dirs is not None:
        # Unquoted, comma-joined: powershell.exe's own command-line parameter
        # binder splits an unquoted "a,b,c" token into a string[] array. Quoting it
        # would instead bind ONE array element containing the whole joined string.
        parts += ["-ExcludeDirs", ",".join(exclude_dirs)]
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Remote-driving orchestration.                                               #
# --------------------------------------------------------------------------- #


@dataclass
class PayloadPullResult:
    local_dest: Path
    files: list[str]
    pdf_paths: list[Path]
    completeness: CompletenessCheck | None = None
    printed: int = 0
    reused: int = 0


class PayloadPuller:
    """Drives the whole export -> print -> decode -> extract pipeline.

    Composes an existing ProjectPuller rather than subclassing it: every remote
    action here (running the export command, printing each export file) is either
    ProjectPuller.run_remote_command or ProjectPuller.print_to_pdf, completely
    unchanged -- this class adds no new remote GUI-driving of its own beyond
    building the command line and depositing the script file (see
    ProjectPuller.deposit_text_file).
    """

    def __init__(
        self,
        puller: ProjectPuller,
        *,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        exclude_dirs: list[str] | None = None,
        staging_dir_name: str = DEFAULT_STAGING_DIR_NAME,
        manifest_poll_seconds: float = 6.0,
        manifest_timeout_seconds: float = 300.0,
        stop_flag_path: Path | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._puller = puller
        self._chunk_chars = chunk_chars
        self._max_chars_per_file = max_chars_per_file
        self._max_file_bytes = max_file_bytes
        self._exclude_dirs = exclude_dirs
        self._staging_dir_name = staging_dir_name
        self._manifest_poll = manifest_poll_seconds
        self._manifest_timeout = manifest_timeout_seconds
        # Cooperative emergency stop, same convention as main.py's pull-project loop
        # (an empty file at this path, checked between print jobs): "stop" typed in
        # chat is a request that has to be seen and acted on, not instant, so a
        # zero-cost file check between the slow, GUI-driven print_to_pdf() calls
        # gives a bounded-delay way to halt a multi-export-file batch. None disables
        # the check entirely (e.g. for callers other than the CLI).
        self._stop_flag_path = stop_flag_path
        self._log = on_log or (lambda _s: None)

    def staging_dir(self, project_root: str) -> str:
        """Remote staging directory: a SIBLING of the project root (never inside
        it), named after the project so concurrent pulls of different projects
        don't collide. Computed as a plain string (a remote Windows path, not
        necessarily this machine's own filesystem)."""
        root = project_root.rstrip("\\")
        parent, _, leaf = root.rpartition("\\")
        parent = parent or root
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", leaf or "project")
        return f"{parent}\\{self._staging_dir_name}_{safe}"

    def _script_deposit_path(self, project_root: str) -> str:
        """Where to deposit project_archive.ps1 for the -File fallback: a sibling
        FILE next to the project root, in the SAME parent directory as
        staging_dir() -- deliberately NOT inside staging_dir() itself. That
        directory doesn't exist yet at deposit time (only the script's own
        New-Item call, once it RUNS, creates it), so Notepad's Save-As dialog
        rejects saving into it with "Path does not exist" -- field-tested live.
        The project's own parent directory is guaranteed to already exist."""
        root = project_root.rstrip("\\")
        parent, _, leaf = root.rpartition("\\")
        parent = parent or root
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", leaf or "project")
        return f"{parent}\\{self._staging_dir_name}_{safe}.ps1"

    async def _run_export(self, project_root: str, staging_dir: str, project_name: str) -> None:
        cmd = build_encoded_command_line(
            project_root=project_root, out_dir=staging_dir, project_name=project_name,
            exclude_dirs=self._exclude_dirs, max_file_bytes=self._max_file_bytes,
            chunk_chars=self._chunk_chars, max_chars_per_file=self._max_chars_per_file,
        )
        if len(cmd) > ENCODED_COMMAND_MAX_LEN:
            self._log(
                f"pull-payload: -EncodedCommand would be {len(cmd)} chars (over "
                f"{ENCODED_COMMAND_MAX_LEN}) -- depositing project_archive.ps1 as a "
                "file on the remote instead"
            )
            script_remote_path = self._script_deposit_path(project_root)
            await self._puller.deposit_text_file(
                script_remote_path, _SCRIPT_PATH.read_text(encoding="utf-8")
            )
            cmd = build_file_command_line(
                script_remote_path, project_root=project_root, out_dir=staging_dir,
                project_name=project_name, exclude_dirs=self._exclude_dirs,
                max_file_bytes=self._max_file_bytes, chunk_chars=self._chunk_chars,
                max_chars_per_file=self._max_chars_per_file,
            )
        self._log(f"pull-payload: running remote export for {project_name!r}...")
        await self._puller.run_remote_command(cmd)

    async def _wait_for_manifest(
        self, staging_dir: str, local_scratch: Path, *, reuse_existing: bool = False
    ) -> ExportManifest:
        """Poll for manifest.txt (written LAST by the export script -- see
        its docstring) rather than listing the staging directory, so a partially
        written part_NNN.txt is never observed.

        Prints and extracts it with reconstruct_raw (NOT ProjectPuller.pull_file):
        pull_file routes through pull.py's `-table`/`-lineprinter` merge, whose whole
        job is guessing indentation from glyph positions -- the lossy step this entire
        module exists to avoid. The manifest is plain ASCII like the payload, so the
        same raw extraction that works for export files works here.
        """
        manifest_remote = f"{staging_dir}\\manifest.txt"
        pdf_path = local_scratch / "manifest.txt.pdf"
        if reuse_existing and pdf_path.exists():
            # Only ever true on --resume, where the export is NOT re-run, so the
            # manifest on the remote is byte-identical to the one already printed.
            # Worth reusing: it's one more GUI-driven print job on precisely the path
            # that already proved it can fail.
            try:
                manifest = parse_manifest_text(reconstruct_raw(pdf_path))
                self._log(
                    f"pull-payload: reusing the manifest already printed to "
                    f"{pdf_path.name} (--resume, export not re-run)"
                )
                return manifest
            except Exception as exc:  # noqa: BLE001 -- fall through and reprint it
                self._log(f"pull-payload: cached manifest unusable ({exc}) -- reprinting")
        # Deadline in REAL time, not accumulated poll intervals. The original
        # `waited += self._manifest_poll` counted 6s per iteration regardless of how
        # long the iteration actually took -- but each iteration PRINTS (opens
        # Notepad, drives the print dialog, waits on a Save dialog), which takes
        # 1-2 MINUTES when it fails. So a nominal 300s timeout really permitted ~50
        # attempts x ~1-2 min = one to two HOURS of silent retrying. That is the
        # whole explanation for the two "hangs" observed live on 2026-08-24: the run
        # was never blocked, it was retrying a failing print with a timeout that
        # could not fire. Measured elapsed time cannot drift like that.
        deadline = time.monotonic() + self._manifest_timeout
        attempt = 0
        last_error = ""
        while True:
            attempt += 1
            await self._puller.wait_ms(int(self._manifest_poll * 1000))
            try:
                await self._puller.print_to_pdf(manifest_remote, pdf_path)
                return parse_manifest_text(reconstruct_raw(pdf_path))
            except Exception as exc:  # noqa: BLE001 -- expected until the export finishes
                last_error = str(exc)
                # LOG every failure. Swallowing these silently is what made a
                # retry loop indistinguishable from a hang: the process looked
                # frozen for an hour while it was actually failing the same way
                # over and over, and nothing said so.
                remaining = max(0.0, deadline - time.monotonic())
                self._log(
                    f"pull-payload: manifest attempt {attempt} failed ({exc}) -- "
                    f"retrying, {remaining:.0f}s left before giving up"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"pull-payload: couldn't read a valid manifest.txt under "
                    f"{staging_dir} after {self._manifest_timeout:g}s and {attempt} "
                    f"attempt(s) -- the export script may have failed, or the print "
                    f"path is broken; screenshot the VDI. Last error: {last_error}"
                )

    def _reusable_pdf(self, pdf_path: Path, manifest: ExportManifest) -> bool:
        """True if `pdf_path` is an already-printed export file that provably belongs
        to THIS export batch and decodes cleanly -- i.e. reprinting it would be pure
        waste.

        This is what makes a failed run resumable, and it is the single highest-value
        reliability property here. Printing is the only slow, GUI-driven, failure-prone
        step (~2 min each, open-loop); a run of n prints that must ALL succeed goes as
        p**n, so at p=0.98 a 40-print project fails more often than it succeeds. Reusing
        verified PDFs turns that into retry-until-done, which converges.

        Safe because the check is total, not a heuristic: the file must parse, every
        chunk's SHA-256 must verify, and its header's total_sha256 must equal this
        batch's -- so a PDF left over from a DIFFERENT export of the same project (a
        re-zip changes total_sha256) is rejected rather than silently mixed in.
        """
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            return False
        try:
            parsed = parse_export_text(reconstruct_raw(pdf_path))
        except Exception:  # noqa: BLE001 -- unparseable simply means "reprint it"
            return False
        if parsed.header.total_sha256 != manifest.total_sha256:
            return False
        if parsed.header.zip_bytes != manifest.zip_bytes:
            return False
        for chunk in parsed.chunks:
            actual = hashlib.sha256(chunk.text.encode("ascii", errors="replace")).hexdigest()
            if actual != chunk.sha256:
                return False
        return True

    async def export_and_print(
        self, project_root: str, local_pdf_dir: Path, *, project_name: str = "",
        resume: bool = False,
    ) -> tuple[list[Path], ExportManifest, int, int]:
        """Run the remote export, wait for its manifest, and print every produced
        export file (via ProjectPuller.print_to_pdf, unchanged) into `local_pdf_dir`.

        With `resume=True` the remote export is NOT re-run: the existing staging
        directory from the interrupted run is reused as-is. That is deliberate --
        re-running the export re-zips the project, and Compress-Archive gives no
        byte-identical guarantee, so a fresh export would change total_sha256 and
        invalidate every PDF already printed. Resuming is only useful if it keeps
        them.

        Returns (pdf_paths, manifest, printed, reused).
        """
        local_pdf_dir.mkdir(parents=True, exist_ok=True)
        # Check the one environmental precondition BEFORE the ~20 minutes of remote
        # work, not on the first print job -- see ProjectPuller.preflight_explorer.
        await self._puller.preflight_explorer()
        staging_dir = self.staging_dir(project_root)
        name = project_name or project_root.rstrip("\\").rpartition("\\")[-1]
        if resume:
            self._log(
                f"pull-payload: --resume -- reusing the existing remote staging dir "
                f"{staging_dir} (not re-exporting, so already-printed PDFs stay valid)"
            )
        else:
            await self._run_export(project_root, staging_dir, name)
        manifest = await self._wait_for_manifest(
            staging_dir, local_pdf_dir, reuse_existing=resume
        )
        self._log(
            f"pull-payload: manifest OK -- {manifest.num_files} export file(s), "
            f"{manifest.total_chunks} chunks, {manifest.zip_bytes} zip bytes, "
            f"{manifest.kept} project file(s) staged"
            + (f", {manifest.skipped} skipped for size" if manifest.skipped else "")
        )
        pdf_paths: list[Path] = []
        printed = reused = 0
        for i, fname in enumerate(manifest.names, 1):
            pdf_path = local_pdf_dir / (fname + ".pdf")
            if self._reusable_pdf(pdf_path, manifest):
                reused += 1
                pdf_paths.append(pdf_path)
                self._log(
                    f"pull-payload: [{i}/{manifest.num_files}] reusing verified "
                    f"{pdf_path.name} (already printed, hashes check out)"
                )
                continue
            if self._stop_flag_path is not None and self._stop_flag_path.exists():
                self._stop_flag_path.unlink()
                raise RuntimeError(
                    f"pull-payload: STOP file found -- halting at export file {i}/"
                    f"{manifest.num_files} ({printed} printed this run, {reused} "
                    f"reused). The PDFs printed so far are kept and verified, and the "
                    f"remote staging dir is left in place: re-run with --resume to "
                    f"carry on from here."
                )
            self._log(
                f"pull-payload: [{i}/{manifest.num_files}] printing {fname} -> {pdf_path}"
            )
            await self._puller.print_to_pdf(f"{staging_dir}\\{fname}", pdf_path)
            printed += 1
            pdf_paths.append(pdf_path)
        return pdf_paths, manifest, printed, reused

    async def cleanup_remote_staging(self, project_root: str) -> None:
        staging_dir = self.staging_dir(project_root)
        try:
            await self._puller.run_remote_command(f'cmd /c rmdir /s /q "{staging_dir}"')
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never fails the pull
            self._log(f"pull-payload: couldn't clean up remote staging dir {staging_dir}")
        # Harmless no-op if the -EncodedCommand path was used and nothing was ever
        # deposited at this path -- `del` on a missing file has no visible effect
        # here (run_remote_command doesn't surface cmd.exe's own exit code/output).
        script_path = self._script_deposit_path(project_root)
        try:
            await self._puller.run_remote_command(f'cmd /c del "{script_path}"')
        except Exception:  # noqa: BLE001
            self._log(f"pull-payload: couldn't clean up deposited script {script_path}")

    def decode_pdfs(self, pdf_paths: list[Path]) -> bytes:
        """Local only, no VDI/network I/O -- re-runnable any time against PDFs
        already on disk, same spirit as pull.py's reconstruct_from_pdf."""
        chunk_files = [parse_export_text(reconstruct_raw(p)) for p in pdf_paths]
        return verify_and_assemble(chunk_files)

    async def pull_project_payload(
        self, project_root: str, local_dest: Path, *, project_name: str = "",
        pdf_dir: Path | None = None, resume: bool = False, keep_staging: bool = False,
    ) -> PayloadPullResult:
        """Full pipeline: export (unless resuming) -> print -> decode -> verify ->
        extract -> completeness-check -> clean up the remote staging dir.

        The remote staging dir is cleaned up ONLY on success. If anything above
        raises, it is deliberately left in place so `--resume` can reuse the exact
        same export (and therefore every PDF already printed from it).
        """
        pdf_dir = pdf_dir or (local_dest.parent / f"{local_dest.name}_pdfs")
        pdf_paths, _manifest, printed, reused = await self.export_and_print(
            project_root, pdf_dir, project_name=project_name, resume=resume
        )
        zip_bytes = self.decode_pdfs(pdf_paths)
        extracted = extract_zip_bytes(zip_bytes, local_dest)
        completeness = check_completeness(zip_bytes, extracted, local_dest)
        self._log("pull-payload: " + completeness.summary())
        if not keep_staging:
            await self.cleanup_remote_staging(project_root)
        else:
            self._log(
                "pull-payload: --keep-staging -- leaving the remote staging dir in "
                f"place at {self.staging_dir(project_root)}"
            )
        return PayloadPullResult(
            local_dest=local_dest, files=extracted, pdf_paths=pdf_paths,
            completeness=completeness, printed=printed, reused=reused,
        )
