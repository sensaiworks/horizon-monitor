"""Regression tests for src/pull.py's reconstruction pipeline.

These test the PURE functions only (merge_table_and_lineprinter, verify_python,
repair_indentation, looks_like_leaked_launch_command, _indent_level,
_page_margins) against hand-built pdftotext-shaped fixtures -- no pdftotext
binary, no PDF, no VDI required. reconstruct_from_pdf() and everything in
ProjectPuller are NOT covered here since they need a real pdftotext install
and/or a live remote session; this only locks down the reconstruction logic
that's actually been the source of real, field-tested bugs.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from src.pull import (
    _indent_level,
    _page_margins,
    looks_like_leaked_launch_command,
    merge_table_and_lineprinter,
    repair_indentation,
    verify_python,
)


class TestIndentLevel(unittest.TestCase):
    def test_at_margin_is_level_zero(self):
        self.assertEqual(_indent_level(9, left_margin=9), 0)

    def test_one_full_unit_above_margin_is_level_one(self):
        self.assertEqual(_indent_level(13, left_margin=9), 1)

    def test_round_half_up_not_banker_rounding(self):
        # Exactly halfway between level 0 and level 1 (unit=4, margin=0) must
        # round UP, not to the nearest even number -- plain round() would give
        # level 0 here (banker's rounding), which is the exact bug that
        # collapsed a `def foo():` and its own body onto the same indent in a
        # real pulled file.
        self.assertEqual(_indent_level(2, left_margin=0), 1)

    def test_never_negative(self):
        self.assertEqual(_indent_level(0, left_margin=9), 0)


class TestLeakedLaunchCommand(unittest.TestCase):
    def test_exact_match_is_leaked(self):
        remote_path = r"c:\git\residesk\PAT\src\export\__init__.py"
        source = f'notepad "{remote_path}"'
        self.assertTrue(looks_like_leaked_launch_command(source, remote_path))

    def test_match_with_trailing_newline_is_leaked(self):
        remote_path = r"c:\git\residesk\PAT\Requirements\_extract\dfd_run.txt"
        source = f'notepad "{remote_path}"\n'
        self.assertTrue(looks_like_leaked_launch_command(source, remote_path))

    def test_real_content_is_not_leaked(self):
        remote_path = r"c:\git\residesk\PAT\src\export\price_export.py"
        source = "import pandas as pd\n\ndef export():\n    pass\n"
        self.assertFalse(looks_like_leaked_launch_command(source, remote_path))

    def test_content_that_merely_mentions_notepad_is_not_leaked(self):
        remote_path = r"c:\git\residesk\PAT\src\foo.py"
        source = 'LAUNCHER = \'notepad "%s"\' % path  # opens the file\n'
        self.assertFalse(looks_like_leaked_launch_command(source, remote_path))


class TestVerifyPython(unittest.TestCase):
    def test_valid_source_passes(self):
        ok, err = verify_python("def foo():\n    return 1\n")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_indentation_error_is_reported(self):
        ok, err = verify_python("def foo():\npass\n")
        self.assertFalse(ok)
        self.assertIn("IndentationError", err)


class TestPageMargins(unittest.TestCase):
    def test_single_page_uses_document_minimum(self):
        lp_pairs = [(9, "import os", 0), (17, "pack_call()", 0)]
        margins = _page_margins(lp_pairs)
        self.assertEqual(margins, {0: 9})

    def test_sub_unit_drift_across_page_break_is_corrected(self):
        # Reproduces the real bug found in a ~4300-line pulled file: content at
        # the same true indent level measured 2 raw columns higher on the page
        # after a break. The page's own minimum (19) must NOT become that
        # page's margin outright -- there's no level-0 content on this page,
        # only a drifted level-2 line -- it must instead resolve to a margin
        # that keeps the SAME level as the matching content on page 0.
        lp_pairs = [
            (9, "import os", 0),
            (17, "pack_call()", 0),
            (19, "bind_call()", 1),
            (19, "other_call()", 1),
        ]
        margins = _page_margins(lp_pairs)
        self.assertEqual(margins[0], 9)
        level_page0 = _indent_level(17, margins[0])
        level_page1 = _indent_level(19, margins[1])
        self.assertEqual(
            level_page0, level_page1,
            "the same conceptual indent level must survive the page break",
        )

    def test_empty_input_does_not_crash(self):
        self.assertEqual(_page_margins([]), {0: 0})


class TestMergeTableAndLineprinter(unittest.TestCase):
    def test_simple_reconstruction(self):
        table_text = "def foo():\nreturn 1\n"
        lineprinter_text = (" " * 9) + "def foo():\n" + (" " * 13) + "return 1\n"
        result = merge_table_and_lineprinter(table_text, lineprinter_text)
        self.assertEqual(result, "def foo():\n      return 1\n")
        ok, err = verify_python(result)
        self.assertTrue(ok, err)

    def test_mismatched_line_counts_raises(self):
        table_text = "line one\nline two\n"
        lineprinter_text = "         line one\n"
        with self.assertRaises(ValueError):
            merge_table_and_lineprinter(table_text, lineprinter_text)

    def test_page_break_drift_reconstructs_correctly(self):
        # End-to-end version of TestPageMargins' scenario, through the actual
        # merge entry point: a page break with a +2 raw-column drift must not
        # break compilation.
        table_text = (
            "def outer():\n"
            "if True:\n"
            "pack_call()\n"
            "bind_call()\n"
            "other_call()\n"
        )
        lineprinter_text = (
            (" " * 9) + "def outer():\n"
            + (" " * 13) + "if True:\n"
            + (" " * 17) + "pack_call()\n"
            + "\x0c\n"
            + (" " * 19) + "bind_call()\n"
            + (" " * 19) + "other_call()\n"
        )
        result = merge_table_and_lineprinter(table_text, lineprinter_text)
        ok, err = verify_python(result)
        self.assertTrue(ok, f"{err}\n---\n{result}")
        lines = result.split("\n")
        # pack_call(), bind_call(), other_call() must all land at the same
        # indent level despite bind_call/other_call being on the drifted page.
        def lead(s):
            return len(s) - len(s.lstrip(" "))
        pack_lead = next(lead(l) for l in lines if "pack_call" in l)
        bind_lead = next(lead(l) for l in lines if "bind_call" in l)
        other_lead = next(lead(l) for l in lines if "other_call" in l)
        self.assertEqual(pack_lead, bind_lead)
        self.assertEqual(pack_lead, other_lead)


class TestRepairIndentation(unittest.TestCase):
    def test_already_valid_source_is_untouched(self):
        source = "def foo():\n    return 1\n"
        repaired, ok, msg = repair_indentation(source)
        self.assertEqual(repaired, source)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_single_line_over_indent_is_repaired(self):
        # `y = 2` is one _INDENT_STEP too deep (12 spaces instead of 6) -- a
        # single isolated line, the kind of small noise this function targets
        # (the larger page-wide case is handled by _page_margins upstream).
        # Indentation must stay in _INDENT_STEP(6) multiples, matching what
        # merge_table_and_lineprinter actually produces.
        source = "def foo():\n      x = 1\n            y = 2\n      return x + y\n"
        repaired, ok, msg = repair_indentation(source)
        self.assertTrue(ok)
        self.assertNotEqual(repaired, source)
        self.assertIn("auto-repaired", msg)
        compiles, err = verify_python(repaired)
        self.assertTrue(compiles, err)

    def test_unfixable_syntax_error_falls_back_to_original(self):
        source = "def foo(:\n    return 1\n"  # broken syntax, not indentation
        repaired, ok, msg = repair_indentation(source)
        self.assertFalse(ok)
        self.assertEqual(repaired, source)
        self.assertNotEqual(msg, "")


if __name__ == "__main__":
    unittest.main()
