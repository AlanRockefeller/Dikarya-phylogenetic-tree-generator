"""The tree viewer's browser bootstrap must actually run.

On 2026-08-24 a ``const`` in ``tree_viewer_controller.js`` was read sixteen
lines above its declaration. The whole controller lives inside a single
``DOMContentLoaded`` callback, so that temporal-dead-zone ReferenceError aborted
the entire bootstrap and ``/job/<id>/view`` sat on "loading" forever for every
visitor. It reached production because nothing in this suite executed that file:
``test_phylotree_invariants.py`` covers the phylotree bundle, not the controller
that drives it.

The behaviour is browser-only, so it is exercised by a node harness that loads
the *shipped* scripts in the order ``job_viewer.html`` loads them and fires the
bootstrap against a stub DOM. See ``tests/js/viewer_init_smoke.test.js``.

This proves the bootstrap RUNS. It does not prove a tree is drawn correctly -
the DOM is a stub, so a wrong SVG transform passes. That is what
``scripts/dikarya_viewer_smoke.py`` checks against the live site.
"""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urljoin

from scripts import dikarya_viewer_smoke as live_smoke

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "js" / "viewer_init_smoke.test.js"


def run_harness():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    proc = subprocess.run(
        [node, str(HARNESS), str(REPO), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "the viewer init harness could not run:\n{}\n{}".format(
                proc.stdout, proc.stderr
            )
        )
    return json.loads(proc.stdout)


class ViewerInitSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {r["name"]: r for r in run_harness()}

    def _assert_ok(self, name):
        result = self.results.get(name)
        self.assertIsNotNone(
            result,
            "the harness did not report '{}'; it stopped early. Reported: {}".format(
                name, sorted(self.results)
            ),
        )
        self.assertTrue(result["ok"], "{}\n{}".format(name, result["detail"]))

    def test_every_viewer_script_evaluates(self):
        """Each shipped script must parse and run its top level."""
        loads = [n for n in self.results if n.startswith("load:")]
        self.assertTrue(loads, "the harness loaded no scripts at all")
        for name in loads:
            with self.subTest(script=name):
                self._assert_ok(name)

    def test_controller_registers_bootstrap(self):
        """A controller that wires up nothing would leave the page inert."""
        self._assert_ok("registers-DOMContentLoaded")

    def test_bootstrap_runs_without_throwing(self):
        """The regression that took /job/<id>/view down on 2026-08-24.

        Any init-time throw - TDZ, a typo, a bad destructure, a missing global -
        aborts the whole bootstrap and presents to the user as a page that loads
        forever. They are all caught here.
        """
        self._assert_ok("bootstrap-runs-without-throwing")


class ScriptSrcExtractionTests(unittest.TestCase):
    """Every external script the page names has to be discovered.

    A src the extractor misses is never fetched, never integrity-checked and
    never executed -- so a broken asset shipped that way passes the smoke test
    silently, which is the exact failure mode this script exists to catch.
    """

    PAGE = (
        '<script src="/static/js/quoted.js?v=3"></script>\n'
        "<script src='/static/js/single.js'></script>\n"
        "<script src=/static/js/unquoted.js></script>\n"
        '<script defer SRC=/static/js/attrs_reordered.js type="module"></script>\n'
        '<script>var s = "<script src=/static/js/inline.js></script>";</script>\n'
        '<script src="https://cdn.example/lib.js"></script>\n'
        "<script></script>\n"
        "<script src></script>\n"
    )

    def test_quoted_single_quoted_and_unquoted_srcs_are_all_found(self):
        self.assertEqual(
            live_smoke.script_srcs(self.PAGE),
            [
                "/static/js/quoted.js?v=3",
                "/static/js/single.js",
                "/static/js/unquoted.js",
                "/static/js/attrs_reordered.js",
                "https://cdn.example/lib.js",
            ],
        )

    def test_an_unquoted_static_script_enters_the_same_integrity_path(self):
        """The regression: only quoted srcs used to reach local_path_for()."""
        mapped = [
            live_smoke.local_path_for(
                urljoin("https://dikarya.us/job/x/view", src), "https://dikarya.us"
            )
            for src in live_smoke.script_srcs(self.PAGE)
        ]
        self.assertEqual(
            mapped,
            [
                "app/static/js/quoted.js",
                "app/static/js/single.js",
                "app/static/js/unquoted.js",
                "app/static/js/attrs_reordered.js",
                None,  # cross-origin CDN asset: fetched by nobody, mirrored never
            ],
        )

    def test_inline_scripts_are_never_treated_as_external_assets(self):
        self.assertEqual(live_smoke.script_srcs("<script>let a = 1;</script>"), [])


class ServedAssetIntegrityTests(unittest.TestCase):
    def test_cross_origin_static_looking_script_is_rejected(self):
        self.assertIsNone(
            live_smoke.local_path_for(
                "https://attacker.example/static/js/tree_viewer_controller.js",
                "https://dikarya.us",
            )
        )
        self.assertEqual(
            live_smoke.local_path_for(
                "https://dikarya.us/static/js/tree_viewer_controller.js?v=1",
                "https://dikarya.us",
            ),
            "app/static/js/tree_viewer_controller.js",
        )

    def test_an_ordinary_static_script_still_maps_onto_the_checkout(self):
        rel = live_smoke.local_path_for(
            "https://dikarya.us/static/js/tree_viewer_controller.js",
            "https://dikarya.us",
        )
        self.assertEqual(rel, "app/static/js/tree_viewer_controller.js")
        # Not merely well-formed: it is the file the harness goes on to execute.
        self.assertTrue((REPO / rel).is_file())

    def test_a_rooted_suffix_cannot_escape_app_static(self):
        """`/static//tmp/example.js` leaves the suffix `/tmp/example.js`.

        Path.joinpath() honours that leading "/" as an absolute path, throwing
        away "app/static" -- so the mapping used to hand mirror_verified_asset
        a path outside the repository, where REPO / rel and workdir / rel name
        the same file and copyfile is given its own source.
        """
        for url in (
            "https://dikarya.us/static//tmp/example.js",
            "https://dikarya.us/static///etc/passwd",
            "https://dikarya.us/static//",
        ):
            with self.subTest(url=url):
                self.assertIsNone(
                    live_smoke.local_path_for(url, "https://dikarya.us")
                )

    def test_traversal_and_empty_suffixes_are_rejected(self):
        for url in (
            "https://dikarya.us/static/../../etc/passwd",
            "https://dikarya.us/static/js/../../../etc/passwd",
            "https://dikarya.us/static/./js/tree_viewer_controller.js",
            "https://dikarya.us/static/js//tree_viewer_controller.js",
            "https://dikarya.us/static/",
            "https://dikarya.us/static",
            "https://dikarya.us/staticky/js/x.js",
        ):
            with self.subTest(url=url):
                self.assertIsNone(
                    live_smoke.local_path_for(url, "https://dikarya.us")
                )

    def test_mirroring_refuses_a_path_outside_app_static(self):
        """Defence in depth, in case a future caller skips local_path_for()."""
        with (
            tempfile.TemporaryDirectory() as repo_tmp,
            tempfile.TemporaryDirectory() as mirror_tmp,
        ):
            repo = Path(repo_tmp)
            outsider = repo / "outside.js"
            body = b"globalThis.executed = 'outside';\n"
            outsider.write_bytes(body)

            with patch.object(live_smoke, "REPO", repo):
                for rel in (str(outsider), "outside.js", "app/static/../outside.js"):
                    with self.subTest(rel=rel):
                        self.assertFalse(
                            live_smoke.mirror_verified_asset(
                                rel, body, Path(mirror_tmp)
                            )
                        )

    def test_mismatched_remote_bytes_are_never_mirrored_for_execution(self):
        with (
            tempfile.TemporaryDirectory() as repo_tmp,
            tempfile.TemporaryDirectory() as mirror_tmp,
        ):
            repo = Path(repo_tmp)
            rel = "app/static/js/example.js"
            trusted = repo / rel
            trusted.parent.mkdir(parents=True)
            trusted.write_bytes(b"globalThis.executed = 'trusted';\n")

            with patch.object(live_smoke, "REPO", repo):
                matched = live_smoke.mirror_verified_asset(
                    rel, b"globalThis.executed = 'remote';\n", Path(mirror_tmp)
                )

            self.assertFalse(matched)
            self.assertFalse((Path(mirror_tmp) / rel).exists())

    def test_matching_remote_bytes_are_mirrored_from_the_trusted_checkout(self):
        with (
            tempfile.TemporaryDirectory() as repo_tmp,
            tempfile.TemporaryDirectory() as mirror_tmp,
        ):
            repo = Path(repo_tmp)
            rel = "app/static/js/example.js"
            trusted = repo / rel
            trusted.parent.mkdir(parents=True)
            body = b"globalThis.executed = 'trusted';\n"
            trusted.write_bytes(body)

            with patch.object(live_smoke, "REPO", repo):
                matched = live_smoke.mirror_verified_asset(rel, body, Path(mirror_tmp))

            self.assertTrue(matched)
            self.assertEqual((Path(mirror_tmp) / rel).read_bytes(), body)


class HarnessFailureReportingTests(unittest.TestCase):
    """A broken harness run is a FAIL, not a traceback out of the script.

    `subprocess.run(..., timeout=180)` raises `TimeoutExpired` and
    `json.loads()` raises `JSONDecodeError`; both escaped `main()`, so a cron
    invocation got a stack trace instead of a verdict and `--json` printed
    nothing a caller could parse.
    """

    def _run(self, side_effect):
        results = []
        with patch.object(live_smoke.subprocess, "run", side_effect=side_effect):
            live_smoke.run_served_harness(results, "/usr/bin/node", Path("/nonexistent"))
        return {r["name"]: r for r in results}

    def test_a_timed_out_harness_is_reported_as_a_failed_check(self):
        results = self._run(subprocess.TimeoutExpired(
            cmd=["node"], timeout=live_smoke.HARNESS_TIMEOUT, output=b"partial"))

        self.assertIn("served-bundle-boots", results)
        self.assertFalse(results["served-bundle-boots"]["ok"])
        detail = results["served-bundle-boots"]["detail"]
        self.assertIn(str(live_smoke.HARNESS_TIMEOUT), detail)
        self.assertIn("timeout", detail)

    def test_malformed_harness_output_is_reported_as_a_failed_check(self):
        results = self._run(lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 0, stdout="not json at all", stderr=""))

        self.assertIn("served-bundle-boots", results)
        self.assertFalse(results["served-bundle-boots"]["ok"])
        detail = results["served-bundle-boots"]["detail"]
        self.assertIn("malformed JSON", detail)
        self.assertIn("not json at all", detail)

    def test_a_nonzero_exit_is_still_reported_the_way_it_was(self):
        results = self._run(lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="ReferenceError"))

        self.assertFalse(results["served-bundle-boots"]["ok"])
        self.assertIn("could not run", results["served-bundle-boots"]["detail"])

    def test_a_good_run_still_records_each_harness_check(self):
        rows = [{"name": "boots", "ok": True, "detail": ""},
                {"name": "wires", "ok": False, "detail": "no handler"}]
        results = self._run(lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 0, stdout=json.dumps(rows), stderr=""))

        self.assertTrue(results["served/boots"]["ok"])
        self.assertFalse(results["served/wires"]["ok"])

    def test_json_mode_still_emits_parseable_json_after_a_harness_failure(self):
        """The whole point: --json has to answer even when the harness died."""
        for side_effect in (
            subprocess.TimeoutExpired(cmd=["node"], timeout=live_smoke.HARNESS_TIMEOUT),
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="{", stderr=""),
        ):
            with self.subTest(side_effect=type(side_effect).__name__):
                results = []
                with patch.object(
                    live_smoke.subprocess, "run", side_effect=side_effect
                ):
                    live_smoke.run_served_harness(
                        results, "/usr/bin/node", Path("/nonexistent"))

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = live_smoke.report(
                        results, SimpleNamespace(json=True, quiet=True))

                self.assertEqual(code, 1)
                parsed = json.loads(buf.getvalue())
                self.assertEqual(parsed[0]["name"], "served-bundle-boots")
                self.assertFalse(parsed[0]["ok"])

    def test_text_mode_prints_a_useful_failure_message(self):
        results = []
        with patch.object(live_smoke.subprocess, "run", side_effect=(
                subprocess.TimeoutExpired(cmd=["node"],
                                          timeout=live_smoke.HARNESS_TIMEOUT))):
            live_smoke.run_served_harness(results, "/usr/bin/node", Path("/nonexistent"))

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = live_smoke.report(results, SimpleNamespace(json=False, quiet=True))

        self.assertEqual(code, 1)
        self.assertIn("served-bundle-boots", err.getvalue())
        self.assertIn(str(live_smoke.HARNESS_TIMEOUT), err.getvalue())

    def test_the_runner_does_not_catch_unrelated_exceptions(self):
        """A bug in this script must still surface, not become a viewer FAIL."""
        with self.assertRaises(MemoryError):
            self._run(MemoryError("not a harness failure"))



if __name__ == "__main__":
    unittest.main()
