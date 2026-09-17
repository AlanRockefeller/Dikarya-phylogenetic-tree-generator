"""A probe is reported once, in the section that exists for probes.

`analyze_errors` sorts every WARNING+ record into one of three tallies: the
App-targeted probes section, the DEGRADED section, and the generic top-warnings
section. A `security.suspicious` record used to land in *two* of them -- counted
under its reason code, and then counted again as a generic exception where
`meaningful_error_key()` rendered it as an unreadable
"event=security.suspicious method=<...> path=<...>" row that crowded out the
real failures the section exists to surface.

`SECURITY_RE` also parsed the reason with `\\w+`, which silently truncates any
slug containing a dot or a dash. No reason code uses one today, which is
precisely why it would have gone unnoticed: the parser would have reported
"job" for a future "job_id.malformed" and split one reason across two rows.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_digest():
    spec = importlib.util.spec_from_file_location(
        "dikarya_log_digest", REPO / "scripts" / "dikarya_log_digest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("dikarya_log_digest", module)
    spec.loader.exec_module(module)
    return module


digest = _load_digest()


SUSPICIOUS = (
    "[2026-09-13 10:00:0{n}] [WARNING] [app] event=security.suspicious "
    "method=GET path=/api/%2e%2e%2fetc%2fpasswd status=404 "
    "reason=path_traversal client=203.0.113.9 agent=curl/8.5.0"
)
ORDINARY = (
    "[2026-09-13 10:00:0{n}] [WARNING] [app] event=http.request_failed "
    "method=POST route=/api/job status=400 reason=invalid_dna_fasta "
    "duration_ms=12.0 request_bytes=900"
)
DEGRADED = (
    "[2026-09-13 10:00:0{n}] [WARNING] [app] DEGRADED "
    "event=degraded.ncbi_accessions_unresolved failed_count=3"
)


class DigestSecurityCountingTests(unittest.TestCase):
    def _analyze(self, lines, tmp):
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "errors.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (log_dir / "error.log").write_text("", encoding="utf-8")

        original = digest.LOG_DIR
        digest.LOG_DIR = log_dir
        try:
            from datetime import datetime

            return digest.analyze_errors(datetime(2026, 9, 13, 0, 0, 0))
        finally:
            digest.LOG_DIR = original

    def test_a_probe_is_counted_in_the_security_section_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = self._analyze([SUSPICIOUS.format(n=1)], tmp)

        self.assertEqual(dict(result["security"]), {"path_traversal": 1})
        self.assertEqual(result["security_clients"]["path_traversal"],
                         {"203.0.113.9"})
        # and nowhere else
        self.assertEqual(sum(result["exceptions"].values()), 0)
        self.assertEqual(sum(result["degradations"].values()), 0)

    def test_ordinary_warnings_still_reach_the_exception_section(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = self._analyze(
                [SUSPICIOUS.format(n=1), ORDINARY.format(n=2), DEGRADED.format(n=3)],
                tmp,
            )

        self.assertEqual(sum(result["security"].values()), 1)
        self.assertEqual(sum(result["exceptions"].values()), 1)
        self.assertEqual(sum(result["degradations"].values()), 1)

    def test_several_probes_from_one_client_are_grouped_by_reason(self):
        import tempfile

        lines = [SUSPICIOUS.format(n=n) for n in range(1, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            result = self._analyze(lines, tmp)

        self.assertEqual(result["security"]["path_traversal"], 3)
        self.assertEqual(sum(result["exceptions"].values()), 0)


class SecurityPatternTests(unittest.TestCase):
    def test_every_shipped_reason_code_parses_whole(self):
        from app.services.security_events import TARGETED_REASONS

        for reason in TARGETED_REASONS:
            line = SUSPICIOUS.format(n=1).replace("path_traversal", reason)
            match = digest.SECURITY_RE.search(line)
            self.assertIsNotNone(match, reason)
            self.assertEqual(match.group("reason"), reason)

    def test_a_slug_with_a_dot_or_a_dash_is_not_truncated(self):
        for reason in ("job_id.malformed", "api-surface-probe", "a.b-c_d"):
            line = SUSPICIOUS.format(n=1).replace("path_traversal", reason)
            match = digest.SECURITY_RE.search(line)
            self.assertIsNotNone(match, reason)
            self.assertEqual(match.group("reason"), reason)

    def test_the_client_and_status_are_still_captured(self):
        match = digest.SECURITY_RE.search(SUSPICIOUS.format(n=1))
        self.assertEqual(match.group("client"), "203.0.113.9")
        self.assertEqual(match.group("status"), "404")
        self.assertEqual(match.group("method"), "GET")

    def test_an_unusual_verb_is_captured_intact(self):
        line = SUSPICIOUS.format(n=1).replace("method=GET", "method=PROPFIND")
        match = digest.SECURITY_RE.search(line)
        self.assertEqual(match.group("method"), "PROPFIND")

    def test_the_digest_and_the_app_share_one_scanner_surface(self):
        # The digest imports the lists rather than keeping a second copy, so
        # its idea of noise and the classifier's cannot drift.
        from app.services import security_events

        self.assertIs(digest.SCANNER_EXACT_PATHS, security_events.SCANNER_EXACT_PATHS)


if __name__ == "__main__":
    unittest.main()
