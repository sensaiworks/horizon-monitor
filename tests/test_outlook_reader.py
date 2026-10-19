"""Tests for src/outlook_reader.py's PURE side: dump parsing -> MessageEvent,
launch-command construction, and the shape of the remote script itself. No VDI,
no Outlook, no MCP needed. OutlookReader.read_folder() (the live drive) is NOT
covered -- it composes ProjectPuller/DocReader, which need a real remote session.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from src.outlook_reader import (
    build_launch_command,
    folder_slug,
    parse_dump,
    script_path,
)

CLEAN_DUMP = """\
===MAIL-DUMP folder=LoanCAD items=3 of 57 generated=2026-10-16 09:12===
===MSG 1/3===
Received: 2026-10-15 14:03
From: Jane Doe
To: Mike Lezinsky; John Smith
Subject: Re: LoanCAD extract
Body:
The extract ran clean last night.

Two rows were rejected for missing CUSIPs.
===END-MSG===
===MSG 2/3===
Received: 2026-10-14 08:30
From: John Smith
To: Jane Doe
Subject: LoanCAD release notes
Body:
Release 4.2 goes to UAT Friday.
===END-MSG===
===MSG 3/3===
Received: 2026-10-13 17:45
From: Build Bot
To: Mike Lezinsky
Subject:
Body:
Nightly build succeeded.
===END-MSG===
===MAIL-DUMP-END items=3===
"""

# What the same dump tends to look like after Windows OCR: "=" runs mangled or
# dropped, spaces inside markers, a missing END-MSG, Notepad chrome around it.
OCR_NOISY_DUMP = """\
mail_LoanCAD.txt - Notepad
File Edit View
== MAIL-DUMP folder=LoanCAD items=2 of 57 generated=2026-10-16 09:12 ==
=== MSG 1 / 2 ===
Received : 2026-10-15 14:03
From : Jane Doe
To : Mike Lezinsky; John Smith
Subject : Re: LoanCAD extract
Body :
The extract ran clean last night.
— MSG 2/2 —
Received: 2026-10-14 08:30
From: John Smith
To: Jane Doe
Subject: LoanCAD release notes
Body:
Release 4.2 goes to UAT Friday.
=== END-MSG ===
=== MAIL-DUMP-END items=2 ===
Ln 1, Col 1   100%   Windows (CRLF)   UTF-8
"""


class TestParseDumpClean(unittest.TestCase):
    def setUp(self):
        self.parse = parse_dump(CLEAN_DUMP, "LoanCAD", user_name="Mike Lezinsky")

    def test_counts_and_completeness(self):
        self.assertEqual(len(self.parse.events), 3)
        self.assertEqual(self.parse.declared_items, 3)
        self.assertTrue(self.parse.header_found)
        self.assertTrue(self.parse.footer_found)
        self.assertTrue(self.parse.complete)
        self.assertEqual(self.parse.errors, 0)

    def test_field_mapping(self):
        e = self.parse.events[0]
        self.assertEqual(e.speaker, "Jane Doe")
        self.assertEqual(e.chat_time, "2026-10-15 14:03")
        self.assertEqual(e.channel, "LoanCAD")
        self.assertEqual(e.app, "outlook")
        self.assertIn("LoanCAD", e.window_title)
        self.assertTrue(e.message.startswith("Subject: Re: LoanCAD extract\n"))
        self.assertIn("Two rows were rejected", e.message)
        # blank line inside the body survives as a paragraph break
        self.assertIn("night.\n\nTwo rows", e.message)

    def test_directed_at_user_from_to_line(self):
        self.assertTrue(self.parse.events[0].directed_at_user)   # Mike in To
        self.assertFalse(self.parse.events[1].directed_at_user)  # Jane only
        self.assertTrue(self.parse.events[2].directed_at_user)

    def test_empty_subject_still_yields_event_with_body_only(self):
        e = self.parse.events[2]
        self.assertEqual(e.message, "Nightly build succeeded.")
        self.assertEqual(e.speaker, "Build Bot")

    def test_doc_ids_stable_and_distinct(self):
        again = parse_dump(CLEAN_DUMP, "LoanCAD", user_name="Mike Lezinsky")
        ids_a = [e.doc_id() for e in self.parse.events]
        ids_b = [e.doc_id() for e in again.events]
        self.assertEqual(ids_a, ids_b)            # re-run dedups against itself
        self.assertEqual(len(set(ids_a)), 3)      # distinct messages stay distinct

    def test_no_user_name_means_never_directed(self):
        p = parse_dump(CLEAN_DUMP, "LoanCAD")
        self.assertFalse(any(e.directed_at_user for e in p.events))


class TestParseDumpOcrNoise(unittest.TestCase):
    def test_noisy_markers_still_parse(self):
        p = parse_dump(OCR_NOISY_DUMP, "LoanCAD", user_name="Mike Lezinsky")
        self.assertEqual(len(p.events), 2)
        self.assertTrue(p.header_found)
        self.assertTrue(p.footer_found)
        self.assertTrue(p.complete)
        self.assertEqual(p.events[0].speaker, "Jane Doe")
        self.assertEqual(p.events[0].chat_time, "2026-10-15 14:03")
        self.assertEqual(p.events[1].message,
                         "Subject: LoanCAD release notes\nRelease 4.2 goes to UAT Friday.")

    def test_missing_end_msg_does_not_swallow_next_message(self):
        # Message 1 has no END-MSG; the next MSG marker must still split them.
        p = parse_dump(OCR_NOISY_DUMP, "LoanCAD")
        self.assertNotIn("Release 4.2", p.events[0].message)

    def test_notepad_chrome_ignored(self):
        p = parse_dump(OCR_NOISY_DUMP, "LoanCAD")
        for e in p.events:
            self.assertNotIn("Notepad", e.message)
            self.assertNotIn("Windows (CRLF)", e.message)


class TestParseDumpIncomplete(unittest.TestCase):
    def test_truncated_dump_is_flagged_incomplete(self):
        cut = CLEAN_DUMP.split("===MSG 3/3===")[0]
        p = parse_dump(cut, "LoanCAD")
        self.assertEqual(len(p.events), 2)
        self.assertFalse(p.footer_found)
        self.assertFalse(p.complete)
        self.assertIn("INCOMPLETE", p.summary())

    def test_msg_error_marker_counted_and_completeness_honours_it(self):
        text = CLEAN_DUMP.replace(
            "===MSG 3/3===\nReceived: 2026-10-13 17:45\nFrom: Build Bot\nTo: Mike Lezinsky\n"
            "Subject:\nBody:\nNightly build succeeded.\n===END-MSG===\n",
            "===MSG-ERROR index=3 Operation aborted===\n",
        )
        p = parse_dump(text, "LoanCAD")
        self.assertEqual(len(p.events), 2)
        self.assertEqual(p.errors, 1)
        self.assertTrue(p.complete)   # 2 parsed + 1 unreadable == 3 declared

    def test_empty_text(self):
        p = parse_dump("", "LoanCAD")
        self.assertEqual(p.events, [])
        self.assertFalse(p.complete)


class TestLaunchCommand(unittest.TestCase):
    def test_readable_file_invocation_not_encoded(self):
        cmd = build_launch_command(
            r"C:\Temp\outlook_folder_dump.ps1", "Loan CAD", r"C:\Temp\mail_Loan_CAD.txt",
            max_items=150, body_max_chars=3000,
        )
        self.assertTrue(cmd.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File "))
        self.assertIn(r"-File C:\Temp\outlook_folder_dump.ps1", cmd)
        self.assertIn('-Folder "Loan CAD"', cmd)
        self.assertIn(r"-Out C:\Temp\mail_Loan_CAD.txt", cmd)
        self.assertIn("-MaxItems 150 -BodyMaxChars 3000", cmd)
        self.assertNotIn("EncodedCommand", cmd)
        self.assertNotIn("FromBase64", cmd)

    def test_rejects_quote_in_folder_name(self):
        with self.assertRaises(ValueError):
            build_launch_command("s.ps1", 'bad"name', "o.txt", max_items=1, body_max_chars=1)

    def test_folder_slug(self):
        self.assertEqual(folder_slug("LoanCAD"), "LoanCAD")
        self.assertEqual(folder_slug("Inbox/Clients/Loan CAD"), "Loan_CAD")
        self.assertEqual(folder_slug("***"), "mail")


class TestRemoteScript(unittest.TestCase):
    """Lock down the constraints the script must honour (see memory
    vdi-command-channel-constraints / vdi-outlook-com-reader)."""

    def setUp(self):
        self.text = script_path().read_text(encoding="utf-8")
        # The header comment EXPLAINS why -EncodedCommand is avoided, so the
        # obfuscation check must look at executable code only.
        self.code = self.text.split("#>", 1)[1] if "#>" in self.text else self.text

    def test_script_exists_and_binds_named_params(self):
        self.assertIn("[string]$Folder", self.text)
        self.assertIn("[string]$Out", self.text)
        self.assertIn("$BodyMaxChars", self.text)

    def test_no_obfuscation(self):
        self.assertNotIn("EncodedCommand", self.code)
        self.assertNotIn("FromBase64String", self.code)
        self.assertNotIn("Invoke-Expression", self.code)
        self.assertNotIn("DownloadString", self.code)

    def test_avoids_security_prompt_properties(self):
        self.assertIn("SenderName", self.text)
        self.assertNotRegex(self.text, r"\$\w+\.SenderEmailAddress")
        self.assertNotRegex(self.text, r"\$\w+\.HTMLBody")

    def test_markers_match_parser(self):
        for marker in ("===MAIL-DUMP folder=", "===MSG $written/$take===",
                       "===END-MSG===", "===MAIL-DUMP-END items=", "===MSG-ERROR"):
            self.assertIn(marker, self.text)


if __name__ == "__main__":
    unittest.main()
