"""The weekly type-specimen refresh reports what it added, and the digest lists it.

The refresh writes event=type_specimens.* lines to its own log; the log digest
(the CLAUDE.md log review) reads them back. These tests pin the round trip:
a new accession named by the refresh must come out of the digest by accession,
status and organism, with organism names containing spaces and quotes intact.
"""

import contextlib
import importlib.util
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


refresh = _load("dikarya_refresh_type_specimens", "dikarya_refresh_type_specimens.py")
digest = _load("dikarya_log_digest", "dikarya_log_digest.py")


class RefreshLogRoundTrip(unittest.TestCase):
    def _write_log(self, path, emit):
        with open(path, "a", encoding="utf-8") as handle:
            refresh._refresh_log = handle
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    emit()
            finally:
                refresh._refresh_log = None

    def test_mycomap_changes_are_itemised_and_read_back(self):
        old = {
            "MK000001": {"status": "type", "organism": "Amanita muscaria"},
            "MK000002": {"status": "holotype", "organism": "Russula emetica"},
        }
        new = {
            "MK000001": {"status": "holotype", "organism": "Amanita muscaria"},
            "NR_173927": {"status": "holotype", "organism": 'Gymnopilus "ochraceus"',
                          "voucher": "O:F-72838"},
        }
        with TemporaryDirectory() as d:
            path = Path(d) / "refresh.log"
            self._write_log(path, lambda: (
                refresh.event("refresh_started", passes="mycomap"),
                refresh.report_mycomap_changes(old, new, 3, {"MK000002"}),
                refresh.event("refresh_finished", outcome="ok", marked_before=2, marked_after=2),
            ))
            now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
            data = digest.analyze_type_specimens(now - timedelta(hours=1), now + timedelta(seconds=5), path)

        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["mycomap"]["added"], "1")
        self.assertEqual([f["accession"] for f in data["added"]], ["NR_173927"])
        self.assertEqual(data["added"][0]["organism"], 'Gymnopilus "ochraceus"')
        self.assertEqual(data["added"][0]["voucher"], "O:F-72838")
        self.assertEqual([f["accession"] for f in data["removed"]], ["MK000002"])
        self.assertEqual(data["removed"][0]["still_marked"], "yes")
        self.assertEqual(data["reclassified"][0]["previous_status"], "type")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            digest.print_type_specimens(data, now)
        text = out.getvalue()
        self.assertIn("New type sequences (1)", text)
        self.assertIn('NR_173927    holotype     Gymnopilus "ochraceus"', text)
        self.assertNotIn("WARNING", text)

    def test_first_snapshot_is_summarised_not_itemised(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "refresh.log"
            self._write_log(path, lambda: refresh.report_mycomap_changes(
                {}, {"MK000001": {"status": "type"}}, 1, set()))
            text = path.read_text()
        self.assertIn("initial=yes", text)
        self.assertNotIn("event=type_specimens.added", text)

    def test_missing_or_stale_log_is_a_warning(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        with TemporaryDirectory() as d:
            data = digest.analyze_type_specimens(now - timedelta(hours=24), now, Path(d) / "none.log")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            digest.print_type_specimens(data, now)
        self.assertIn("never run here", out.getvalue())


if __name__ == "__main__":
    unittest.main()
