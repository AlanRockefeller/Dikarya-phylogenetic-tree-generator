"""var/logs is kept forever, and `rotate -1` is how logrotate spells that.

The old policy was `rotate 14` with `maxage 30`, and `rotate 14` was what
actually bound: every stem kept 14 rotations plus the live file, which came to
13 days rather than the 30 the maxage implied, because `maxsize 25M` makes a
busy day rotate twice and burn two slots. Thirteen days of *all* of these logs
was 3.2 MB compressed against a 5.3 GB var/jobs, so the retention bought nothing
and cost history.

It was then replaced with `rotate 99999`, which only approximates "never remove
old logs". logrotate has a value that means it exactly, and this run verifies
against the installed binary rather than trusting the manual page.

`dateext` matters more with unlimited retention than it did with 14 slots:
without it every rotation renames the whole chain, so keeping years of files
would mean thousands of renames per run. Nothing reads these files by name --
the digest globs `<stem>.log*` and orders by mtime -- so the timestamped naming
is free.

var/metrics/system_metrics.jsonl deliberately keeps a bounded window: it is
machine telemetry nothing reads back, not a record of what happened.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "ops" / "logrotate" / "dikarya"


def _directive_lines(directives):
    """The directive lines of one block, comments and blanks dropped."""
    return [
        line.strip() for line in directives.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _blocks():
    """Split the config into (paths, directives) pairs."""
    text = CONFIG.read_text(encoding="utf-8")
    return [
        (match.group(1).split(), match.group(2))
        for match in re.finditer(r"^([^\s#][^{]*)\{(.*?)^\}", text, re.M | re.S)
    ]


class RetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.blocks = _blocks()
        self.assertEqual(len(self.blocks), 2, "expected the logs and metrics blocks")
        self.logs, self.metrics = self.blocks

    def test_the_log_block_keeps_every_rotation(self):
        paths, directives = self.logs
        self.assertIn("/var/www/dikarya/var/logs/errors.log", paths)
        self.assertIn("/var/www/dikarya/var/logs/scanner.log", paths)
        self.assertIn("rotate -1", _directive_lines(directives))

    def test_the_approximation_is_gone(self):
        # Directives only -- the comment above the block explains what it
        # replaced, and saying so is the point.
        for _paths, directives in self.blocks:
            self.assertNotIn("rotate 99999", _directive_lines(directives))

    def test_nothing_ages_the_logs_out(self):
        _paths, directives = self.logs
        self.assertFalse([line for line in _directive_lines(directives)
                          if line.startswith("maxage")])

    def test_rotated_files_are_named_by_date_not_by_index(self):
        lines = _directive_lines(self.logs[1])
        self.assertIn("dateext", lines)
        self.assertTrue([line for line in lines if line.startswith("dateformat")])

    def test_copytruncate_is_kept_for_the_appending_writers(self):
        # Gunicorn and the worker hold these files open; a rename would leave
        # them writing to the rotated inode.
        self.assertIn("copytruncate", _directive_lines(self.logs[1]))

    def test_the_metrics_stream_still_ages_out_on_purpose(self):
        paths, directives = self.metrics
        self.assertEqual(paths, ["/var/www/dikarya/var/metrics/system_metrics.jsonl"])
        lines = _directive_lines(directives)
        self.assertIn("maxage 30", lines)
        self.assertNotIn("rotate -1", lines)

    def test_every_stem_the_digest_reads_is_covered(self):
        paths, _directives = self.logs
        covered = {Path(path).name for path in paths}
        for name in ("access.log", "error.log", "errors.log", "worker.log",
                     "worker-bulk.log", "scanner.log"):
            self.assertIn(name, covered)


class LogrotateAcceptsTheConfigTests(unittest.TestCase):
    """Run the installed logrotate rather than trusting the manual page."""

    def setUp(self):
        self.binary = shutil.which("logrotate") or "/usr/sbin/logrotate"
        if not Path(self.binary).exists():
            raise unittest.SkipTest("logrotate is not installed")

    def test_the_shipped_config_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [self.binary, "--debug", "--state", str(Path(tmp) / "state"),
                 str(CONFIG)],
                capture_output=True, text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("error:", proc.stderr.lower())

    def test_rotate_minus_one_keeps_every_archive(self):
        """The behaviour, not the spelling: force a rotation and count files."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            for day in ("20260901", "20260902", "20260903"):
                (logs / f"t.log-{day}").write_text("old\n", encoding="utf-8")
                subprocess.run(["gzip", "-f", str(logs / f"t.log-{day}")], check=True)
            (logs / "t.log").write_text("live\n", encoding="utf-8")

            config = root / "conf"
            config.write_text(
                f"{logs / 't.log'} {{\n"
                "    daily\n"
                "    rotate -1\n"
                "    missingok\n"
                "    notifempty\n"
                "    compress\n"
                "    dateext\n"
                "    dateformat -%Y%m%d-%H%M%S\n"
                "    copytruncate\n"
                "}\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [self.binary, "--force", "--state", str(root / "state"), str(config)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            archives = sorted(path.name for path in logs.glob("t.log-*"))
            # The three pre-existing archives survive, and the rotation adds one.
            self.assertEqual(len(archives), 4, archives)
            for day in ("20260901", "20260902", "20260903"):
                self.assertIn(f"t.log-{day}.gz", archives)


if __name__ == "__main__":
    unittest.main()
