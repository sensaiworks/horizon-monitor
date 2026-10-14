"""Regression tests for src/pull_payload.py's decode/verify/extract pipeline.

Covers, with no pdftotext binary, no PDF and no VDI:
  * The pure functions -- parse_export_text, verify_and_assemble, extract_zip_bytes,
    parse_manifest_text, parse_contents, check_completeness -- against synthetic
    export text built by the test-only _build_export_texts helper below. This locks
    down the marker format both sides agree on and the verification logic that is the
    entire point of the pivot away from the old reconstruction pipeline.
  * PayloadPuller's ORCHESTRATION (TestResumeOrchestration), via _FakePuller plus a
    monkeypatched reconstruct_raw that treats each export .txt as its own "PDF". The
    remote-driving primitives underneath (print_to_pdf and friends) still need a live
    session, but the resume/cleanup decisions layered on top of them do not -- and
    those are exactly the logic that decides whether a long run survives one bad
    print, so leaving them untested would leave the riskiest part uncovered.

project_archive.ps1 is not covered here, but it is NOT untestable: it is pure
PowerShell + filesystem, so it runs locally against a throwaway folder with no VDI
(see CLAUDE.md's Verification section for the exact invocation).

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.pull_payload import (
    check_completeness,
    extract_zip_bytes,
    find_contents,
    parse_contents,
    parse_export_text,
    parse_manifest_text,
    verify_and_assemble,
)


def _build_export_texts(
    payload: bytes, *, project: str = "TestProj", chunk_chars: int = 16,
    max_chunks_per_file: int = 3,
) -> list[str]:
    """Build synthetic export file text(s) in exactly the marker format
    project_archive.ps1 writes, so parse_export_text/verify_and_assemble can be
    round-tripped without PowerShell, a PDF, or a VDI."""
    b64 = base64.b64encode(payload).decode("ascii")
    total_sha = hashlib.sha256(payload).hexdigest()
    chunks = []
    pos, idx = 0, 1
    while pos < len(b64):
        text = b64[pos:pos + chunk_chars]
        sha = hashlib.sha256(text.encode("ascii")).hexdigest()
        chunks.append((idx, sha, text))
        pos += chunk_chars
        idx += 1
    total_chunks = len(chunks)

    file_groups = []
    for start in range(0, total_chunks, max_chunks_per_file):
        end = min(start + max_chunks_per_file, total_chunks) - 1
        file_groups.append((start, end))
    num_files = len(file_groups)

    texts = []
    for f, (start, end) in enumerate(file_groups, 1):
        first_chunk, last_chunk = chunks[start][0], chunks[end][0]
        lines = [
            f"===ARCHIVE-BEGIN project={project} file={f}/{num_files} "
            f"chunks={first_chunk}-{last_chunk}/{total_chunks} "
            f"total_sha256={total_sha} zip_bytes={len(payload)}==="
        ]
        for c in range(start, end + 1):
            cidx, csha, ctext = chunks[c]
            lines.append(f"---CHUNK {cidx} sha256={csha}---")
            lines.append(ctext)
        lines.append("===ARCHIVE-END===")
        texts.append("\n".join(lines))
    return texts


def _make_zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestRoundTrip(unittest.TestCase):
    def test_single_export_file_round_trip(self):
        payload = b"hello world, this is a test payload!" * 3
        texts = _build_export_texts(payload, chunk_chars=1000, max_chunks_per_file=10)
        self.assertEqual(len(texts), 1)
        chunk_files = [parse_export_text(t) for t in texts]
        self.assertEqual(verify_and_assemble(chunk_files), payload)

    def test_multi_export_file_round_trip(self):
        payload = bytes(range(256)) * 4
        texts = _build_export_texts(payload, chunk_chars=16, max_chunks_per_file=3)
        self.assertGreater(len(texts), 1)
        chunk_files = [parse_export_text(t) for t in texts]
        self.assertEqual(verify_and_assemble(chunk_files), payload)

    def test_full_pipeline_extracts_expected_files(self):
        zip_bytes = _make_zip({"a.txt": "hello", "sub/b.txt": "world"})
        texts = _build_export_texts(zip_bytes, chunk_chars=20, max_chunks_per_file=5)
        chunk_files = [parse_export_text(t) for t in texts]
        raw = verify_and_assemble(chunk_files)
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out"
            names = extract_zip_bytes(raw, dest)
            self.assertEqual(sorted(names), ["a.txt", "sub/b.txt"])
            self.assertEqual((dest / "a.txt").read_text(), "hello")
            self.assertEqual((dest / "sub" / "b.txt").read_text(), "world")


class TestWhitespaceTolerance(unittest.TestCase):
    def test_chunk_text_wrapped_across_printed_lines_still_verifies(self):
        """A chunk's base64 text can span many printed/extracted lines once it
        goes through Notepad's print engine -- verify that splitting one chunk's
        content across two lines (simulating that wrap) doesn't break anything,
        since all whitespace is stripped before hashing."""
        payload = b"A" * 60
        texts = _build_export_texts(payload, chunk_chars=40, max_chunks_per_file=10)
        lines = texts[0].splitlines()
        for i, line in enumerate(lines):
            if line and not line.startswith(("===", "---")):
                mid = len(line) // 2
                lines[i:i + 1] = [line[:mid], line[mid:]]
                break
        chunk_file = parse_export_text("\n".join(lines))
        self.assertEqual(verify_and_assemble([chunk_file]), payload)


class TestMissingMarkers(unittest.TestCase):
    def test_missing_begin_marker_is_reported_clearly(self):
        with self.assertRaises(ValueError) as ctx:
            parse_export_text("some unrelated printed text\nwith no markers at all\n")
        self.assertIn("ARCHIVE-BEGIN", str(ctx.exception))

    def test_missing_end_marker_is_reported_clearly(self):
        text = _build_export_texts(b"short", chunk_chars=1000, max_chunks_per_file=10)[0]
        truncated = text.rsplit("===ARCHIVE-END===", 1)[0]
        with self.assertRaises(ValueError) as ctx:
            parse_export_text(truncated)
        self.assertIn("ARCHIVE-END", str(ctx.exception))


class TestCorruption(unittest.TestCase):
    def test_bad_chunk_hash_names_the_exact_chunk(self):
        payload = b"x" * 100
        texts = _build_export_texts(payload, chunk_chars=16, max_chunks_per_file=10)
        chunk_file = parse_export_text(texts[0])
        chunk_file.chunks[1].text = "Z" * len(chunk_file.chunks[1].text)
        with self.assertRaises(ValueError) as ctx:
            verify_and_assemble([chunk_file])
        self.assertIn(f"chunk {chunk_file.chunks[1].index}", str(ctx.exception))

    def test_missing_export_file_is_reported_clearly(self):
        payload = b"y" * 200
        texts = _build_export_texts(payload, chunk_chars=16, max_chunks_per_file=3)
        self.assertGreater(len(texts), 1)
        chunk_files = [parse_export_text(t) for t in texts[:-1]]  # drop the last file
        with self.assertRaises(ValueError) as ctx:
            verify_and_assemble(chunk_files)
        self.assertIn("missing export file", str(ctx.exception))


class TestZipSlip(unittest.TestCase):
    def test_path_traversal_entry_is_rejected(self):
        zip_bytes = _make_zip({"../evil.txt": "pwned"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out"
            with self.assertRaises(ValueError) as ctx:
                extract_zip_bytes(zip_bytes, dest)
            self.assertIn("zip-slip", str(ctx.exception).lower())
            self.assertFalse((Path(d) / "evil.txt").exists())


_MANIFEST = (
    "===ARCHIVE-MANIFEST project=Pricing Tool files=3 chunks=7 "
    f"total_sha256={'a' * 64} zip_bytes=1469364 kept=11 skipped=2 "
    "report=_contents.txt===\n"
    "part_001.txt\npart_002.txt\npart_003.txt\n"
    "===ARCHIVE-MANIFEST-END===\n"
)


class TestParseManifest(unittest.TestCase):
    def test_parses_every_header_field(self):
        m = parse_manifest_text(_MANIFEST)
        self.assertEqual(m.project, "Pricing Tool")
        self.assertEqual(m.num_files, 3)
        self.assertEqual(m.total_chunks, 7)
        self.assertEqual(m.total_sha256, "a" * 64)
        self.assertEqual(m.zip_bytes, 1469364)
        self.assertEqual((m.kept, m.skipped), (11, 2))
        self.assertEqual(m.report_name, "_contents.txt")
        self.assertEqual(m.names, ["part_001.txt", "part_002.txt", "part_003.txt"])

    def test_tolerates_header_wrapped_across_a_page_break(self):
        """pdftotext splits the long header line mid-field -- field-tested on a real
        printed PDF (see parse_export_text's docstring, same failure)."""
        wrapped = _MANIFEST.replace("zip_bytes=", "\r\nzip_bytes=", 1)
        self.assertEqual(parse_manifest_text(wrapped).zip_bytes, 1469364)

    def test_tolerates_names_reflowed_onto_one_line(self):
        packed = _MANIFEST.replace(
            "part_001.txt\npart_002.txt\npart_003.txt",
            "part_001.txt part_002.txt part_003.txt",
        )
        self.assertEqual(len(parse_manifest_text(packed).names), 3)

    def test_rejects_a_stale_notepad_tab_printed_instead(self):
        with self.assertRaises(ValueError) as ctx:
            parse_manifest_text('notepad "C:\\git\\residesk\\_export\\manifest.txt"')
        self.assertIn("ARCHIVE-MANIFEST marker not found", str(ctx.exception))

    def test_rejects_truncated_manifest(self):
        with self.assertRaises(ValueError) as ctx:
            parse_manifest_text(_MANIFEST.replace("===ARCHIVE-MANIFEST-END===", ""))
        self.assertIn("END marker not found", str(ctx.exception))

    def test_rejects_dropped_name_line(self):
        """A name silently lost in extraction must fail loudly -- otherwise the batch
        would decode as complete while missing a whole export file."""
        with self.assertRaises(ValueError) as ctx:
            parse_manifest_text(_MANIFEST.replace("part_002.txt\n", ""))
        self.assertIn("declares 3 export file(s) but lists 2", str(ctx.exception))


_REPORT = (
    "===ARCHIVE-CONTENTS===\n"
    "project=TestProj\nroot=C:\\git\\residesk\\TestProj\nmax_file_bytes=10000\n"
    "exclude_dirs=.git,build,__pycache__\nkept=2\nskipped=1\n"
    "report_name=_contents.txt\n"
    "---KEPT relpath<TAB>bytes<TAB>sha256---\n"
    f"src\\a.py\t21\t{'1' * 64}\n"
    f"b.txt\t12\t{'2' * 64}\n"
    "---SKIPPED relpath<TAB>bytes<TAB>reason---\n"
    "huge.dat\t20000\tover MaxFileBytes=10000\n"
    "---EXCLUDED-DIRS name<TAB>files---\n"
    "build\t1\n__pycache__\t3\n"
    "===ARCHIVE-CONTENTS-END===\n"
)


class TestParseContents(unittest.TestCase):
    def test_parses_all_sections(self):
        r = parse_contents(_REPORT)
        self.assertEqual(r.project, "TestProj")
        self.assertEqual(r.max_file_bytes, 10000)
        self.assertEqual(r.exclude_dirs, [".git", "build", "__pycache__"])
        self.assertEqual(r.kept["src/a.py"], (21, "1" * 64))
        self.assertEqual(r.skipped, [("huge.dat", 20000, "over MaxFileBytes=10000")])
        self.assertEqual(r.excluded_dirs_hit, {"build": 1, "__pycache__": 3})

    def test_normalises_backslash_paths_to_forward(self):
        """Zip entry names use '/', the remote report uses '\\' -- reconciliation
        would report every nested file as both missing AND unexpected otherwise."""
        self.assertIn("src/a.py", parse_contents(_REPORT).kept)

    def test_rejects_a_project_file_of_the_same_name(self):
        with self.assertRaises(ValueError):
            parse_contents("just some file the project happened to contain\n")

    def test_find_contents_ignores_same_named_project_file(self):
        """The export script renames its report on collision, so a project file
        called _contents.txt must not be parsed as the report."""
        decoy = _make_zip({"_contents.txt": "I am a project file, not the report"})
        self.assertIsNone(find_contents(decoy))

    def test_find_contents_locates_a_renamed_report(self):
        found = _make_zip({"_contents_1.txt": _REPORT, "a.py": "x = 1"})
        report = find_contents(found)
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.project, "TestProj")


class TestCompleteness(unittest.TestCase):
    def _zip_with_report(self, entries: dict[str, str]) -> bytes:
        return _make_zip({**entries, "_contents.txt": _REPORT})

    def test_clean_when_everything_declared_arrived(self):
        zip_bytes = self._zip_with_report({"src/a.py": "def f():\n    return 1", "b.txt": "hello\nworld\n"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            names = extract_zip_bytes(zip_bytes, dest)
            check = check_completeness(zip_bytes, names, dest)
            self.assertTrue(check.ok, check.summary())
            self.assertEqual(check.missing, [])
            self.assertEqual(check.unexpected, [])

    def test_report_itself_is_not_counted_as_unexpected(self):
        zip_bytes = self._zip_with_report({"src/a.py": "def f():\n    return 1", "b.txt": "hello\nworld\n"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            check = check_completeness(zip_bytes, extract_zip_bytes(zip_bytes, dest), dest)
            self.assertNotIn("_contents.txt", check.unexpected)

    def test_flags_a_declared_file_that_never_arrived(self):
        zip_bytes = self._zip_with_report({"src/a.py": "def f():\n    return 1"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            check = check_completeness(zip_bytes, extract_zip_bytes(zip_bytes, dest), dest)
            self.assertFalse(check.ok)
            self.assertEqual(check.missing, ["b.txt"])

    def test_flags_a_size_mismatch(self):
        zip_bytes = self._zip_with_report({"src/a.py": "WRONG LENGTH", "b.txt": "hello\nworld\n"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            check = check_completeness(zip_bytes, extract_zip_bytes(zip_bytes, dest), dest)
            self.assertFalse(check.ok)
            self.assertEqual(len(check.size_mismatches), 1)
            self.assertIn("src/a.py", check.size_mismatches[0])

    def test_summary_surfaces_size_skips_and_dir_exclusions(self):
        """The whole point of the report: a file dropped for size and a directory
        erased by an exclude rule must be VISIBLE, not silent."""
        zip_bytes = self._zip_with_report({"src/a.py": "def f():\n    return 1", "b.txt": "hello\nworld\n"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            summary = check_completeness(zip_bytes, extract_zip_bytes(zip_bytes, dest), dest).summary()
        self.assertIn("huge.dat", summary)
        self.assertIn("SKIPPED for size", summary)
        self.assertIn("build=1", summary)

    def test_unknown_when_payload_predates_the_report(self):
        zip_bytes = _make_zip({"a.py": "x = 1"})
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            check = check_completeness(zip_bytes, extract_zip_bytes(zip_bytes, dest), dest)
            self.assertFalse(check.available)
            self.assertFalse(check.ok)
            self.assertIn("UNKNOWN", check.summary())


class _FakePuller:
    """Stands in for ProjectPuller so PayloadPuller's orchestration can be tested
    without a VDI, a printer, or pdftotext. Records what it was asked to do."""

    def __init__(self, remote_dir: Path, fail_on: set[str] | None = None):
        self.remote_dir = remote_dir
        self.fail_on = fail_on or set()
        self.commands: list[str] = []
        self.deposited: list[str] = []
        self.printed: list[str] = []
        self.preflighted = False

    async def preflight_explorer(self) -> tuple[int, int]:
        self.preflighted = True
        return (700, 252)

    async def run_remote_command(self, command: str) -> None:
        self.commands.append(command)

    async def deposit_text_file(self, remote_path: str, content: str) -> None:
        self.deposited.append(remote_path)

    async def wait_ms(self, ms: int) -> None:
        pass

    async def print_to_pdf(self, remote_path: str, pdf_path) -> None:
        name = remote_path.rsplit("\\", 1)[-1]
        if name in self.fail_on:
            raise RuntimeError(f"simulated print failure on {name}")
        Path(pdf_path).write_text(
            (self.remote_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.printed.append(name)


class TestResumeOrchestration(unittest.TestCase):
    """`--resume` is the property that makes this transport survive scale: printing is
    the only fragile step, so an all-or-nothing run of n prints goes as p**n. These
    lock down that a failed run stays resumable and that resuming can't mix batches."""

    def setUp(self):
        import src.pull_payload as pp

        self.pp = pp
        # The 'PDF' each print produces is just the export text, so no pdftotext needed.
        self._real_raw = pp.reconstruct_raw
        pp.reconstruct_raw = lambda p: Path(p).read_text(encoding="utf-8")
        self.addCleanup(setattr, pp, "reconstruct_raw", self._real_raw)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()

        self.payload = _make_zip({"a.py": "x = 1\n", "sub/b.txt": "hello\n"})
        texts = _build_export_texts(self.payload, chunk_chars=16, max_chunks_per_file=2)
        self.num_files = len(texts)
        names = []
        for i, text in enumerate(texts, 1):
            name = f"part_{i:03d}.txt"
            (self.remote / name).write_text(text, encoding="utf-8")
            names.append(name)
        total_chunks = sum(len(parse_export_text(t).chunks) for t in texts)
        (self.remote / "manifest.txt").write_text(
            f"===ARCHIVE-MANIFEST project=TestProj files={self.num_files} "
            f"chunks={total_chunks} total_sha256={hashlib.sha256(self.payload).hexdigest()} "
            f"zip_bytes={len(self.payload)} kept=2 skipped=0 report=_contents.txt===\n"
            + "\n".join(names)
            + "\n===ARCHIVE-MANIFEST-END===\n",
            encoding="utf-8",
        )

    def _pull(self, *, fail_on=None, resume=False, pdf_dir=None, dest=None):
        fake = _FakePuller(self.remote, fail_on)
        puller = self.pp.PayloadPuller(
            fake, manifest_poll_seconds=0.0,
            # Short on purpose: the default 300s deadline means a name/marker
            # mismatch spins for five real minutes instead of failing. That is the
            # 2026-08-25 bug in miniature -- a retry loop outliving its usefulness.
            manifest_timeout_seconds=5.0,
        )
        result = asyncio.run(puller.pull_project_payload(
            r"C:\git\residesk\TestProj", dest, project_name="TestProj",
            pdf_dir=pdf_dir, resume=resume,
        ))
        return fake, result

    def test_fresh_run_prints_everything_and_cleans_up(self):
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        fake, result = self._pull(pdf_dir=pdfs, dest=dest)
        self.assertEqual(result.printed, self.num_files)
        self.assertEqual(result.reused, 0)
        self.assertTrue(any("rmdir" in c for c in fake.commands),
                        "staging dir must be cleaned up after a SUCCESSFUL run")

    def test_explorer_precondition_is_checked_before_any_remote_work(self):
        """An hour was lost live to this precondition failing silently on the first
        print job instead of up front -- see ProjectPuller.preflight_explorer."""
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        fake, _result = self._pull(pdf_dir=pdfs, dest=dest)
        self.assertTrue(fake.preflighted)

    def test_failed_run_keeps_staging_and_printed_pdfs(self):
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        with self.assertRaises(RuntimeError):
            self._pull(fail_on={"part_002.txt"}, pdf_dir=pdfs, dest=dest)
        self.assertTrue((pdfs / "part_001.txt.pdf").exists(),
                        "PDFs printed before the failure must survive for --resume")

    def test_failed_run_does_not_clean_up_staging(self):
        """Cleanup on failure would delete the export --resume needs, and a re-export
        would change total_sha256 and invalidate every PDF already printed."""
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        fake = _FakePuller(self.remote, {"part_002.txt"})
        puller = self.pp.PayloadPuller(
            fake, manifest_poll_seconds=0.0,
            # Short on purpose: the default 300s deadline means a name/marker
            # mismatch spins for five real minutes instead of failing. That is the
            # 2026-08-25 bug in miniature -- a retry loop outliving its usefulness.
            manifest_timeout_seconds=5.0,
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(puller.pull_project_payload(
                r"C:\git\residesk\TestProj", dest, project_name="TestProj", pdf_dir=pdfs))
        self.assertFalse(any("rmdir" in c for c in fake.commands))

    def test_resume_reuses_verified_pdfs_and_skips_the_export(self):
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        with self.assertRaises(RuntimeError):
            self._pull(fail_on={"part_002.txt"}, pdf_dir=pdfs, dest=dest)
        fake, result = self._pull(resume=True, pdf_dir=pdfs, dest=dest)
        self.assertEqual(fake.deposited, [], "resume must NOT re-run the remote export")
        self.assertEqual(result.reused, 1)
        self.assertEqual(result.printed, self.num_files - 1)

    def test_resume_rejects_pdfs_from_a_different_export_batch(self):
        """A leftover PDF from an earlier export of the SAME project parses fine and
        has valid chunk hashes -- only total_sha256 reveals it doesn't belong."""
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        pdfs.mkdir(parents=True)
        stale = (self.remote / "part_001.txt").read_text(encoding="utf-8")
        real_total = hashlib.sha256(self.payload).hexdigest()
        (pdfs / "part_001.txt.pdf").write_text(
            stale.replace(real_total, "d" * 64), encoding="utf-8"
        )
        _fake, result = self._pull(resume=True, pdf_dir=pdfs, dest=dest)
        self.assertEqual(result.reused, 0, "stale-batch PDF must not be reused")
        self.assertEqual(result.printed, self.num_files)

    def test_resume_reuses_the_manifest_pdf_too(self):
        pdfs, dest = self.root / "pdfs", self.root / "dest"
        self._pull(pdf_dir=pdfs, dest=dest)
        fake, _result = self._pull(resume=True, pdf_dir=pdfs, dest=dest)
        self.assertNotIn("manifest.txt", fake.printed)
        self.assertEqual(fake.printed, [], "a full resume should print nothing at all")


if __name__ == "__main__":
    unittest.main()
