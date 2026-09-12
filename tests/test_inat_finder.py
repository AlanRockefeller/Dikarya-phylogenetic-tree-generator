"""Browser-side regressions for the iNaturalist observation finder.

The finder is a browser application: /finder serves a template and a script, and
the visitor's browser talks to iNaturalist directly, so Flask never sees the
query. That means the behaviour worth protecting lives in JavaScript, and these
tests mostly drive the two Node harnesses under tests/js/. What stays here is
what Python can check better: that the page still defaults to the automatic
search, that the manual modes are still reachable, and that the cross-language
parity fixture still describes the CLI actually vendored in this repository.
"""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESSES = (
    REPO / "tests" / "js" / "inat_finder_variation_limit.test.js",
    REPO / "tests" / "js" / "inat_finder_auto_mode.test.js",
)
EXPECTED_BANNERS = {
    "inat_finder_variation_limit.test.js": "PASS iNat Finder browser regressions",
    "inat_finder_auto_mode.test.js": "PASS iNat Finder auto mode",
}
TEMPLATE = REPO / "app" / "templates" / "inat_finder.html"
SCRIPT = REPO / "app" / "static" / "js" / "inat_finder.js"
FIXTURE = REPO / "tests" / "fixtures" / "inat_finder_candidate_parity.json"


class InatFinderBrowserHarnessTests(unittest.TestCase):
    def _run_harness(self, harness):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        try:
            proc = subprocess.run(
                [node, str(harness), str(REPO)],
                capture_output=True,
                text=True,
                # The harnesses run several vm contexts and a fake-timer search
                # loop; 10s was tight enough to fail on a loaded box, and a bare
                # TimeoutExpired reads as an error rather than as this check.
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(
                f"{harness.name} did not finish within {exc.timeout}s:\n"
                f"{(exc.stdout or b'').decode('utf-8', 'replace')}\n"
                f"{(exc.stderr or b'').decode('utf-8', 'replace')}"
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(EXPECTED_BANNERS[harness.name], proc.stdout)

    def test_browser_finder_regressions(self):
        self._run_harness(HARNESSES[0])

    def test_browser_finder_auto_mode(self):
        self._run_harness(HARNESSES[1])


class InatFinderTemplateTests(unittest.TestCase):
    """The shape of the page, which the Node harnesses cannot see."""

    def setUp(self):
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.script = SCRIPT.read_text(encoding="utf-8")

    def test_automatic_search_is_the_default_experience(self):
        # The visitor should be able to type a number and press the button
        # without first deciding what kind of search they need.
        self.assertIn('id="search-mode-auto"', self.template)
        self.assertRegex(self.template, r'id="search-mode-auto"[^>]*checked')
        self.assertNotRegex(self.template, r'id="search-mode-manual"[^>]*checked')
        self.assertIn('id="auto-panel"', self.template)
        # The auto panel is the one that is NOT hidden on first paint.
        self.assertNotIn('id="auto-panel" class="hidden', self.template)
        self.assertIn('id="manual-panel" class="hidden', self.template)
        self.assertIn("Find the correct iNaturalist observation", self.template)

    def test_every_clue_is_optional_and_none_is_exclusive(self):
        for clue in ("genus", "family", "taxon", "user", "project"):
            self.assertIn(f'id="clue-{clue}"', self.template)
        # A clue box is a plain input, never a radio in a group of one-of-five,
        # and never required.
        for clue in ("genus", "family", "taxon", "user", "project"):
            field = self.template.split(f'id="clue-{clue}"')[1].split(">")[0]
            self.assertNotIn("required", field, f"the {clue} clue must stay optional")
        self.assertIn("You can leave", self.template)

    def test_manual_single_criterion_modes_survive_behind_advanced(self):
        self.assertIn('id="advanced-options"', self.template)
        self.assertIn("Advanced search options", self.template)
        self.assertIn('name="search-mode" value="manual"', self.template)
        for mode in ("genus", "family", "taxon", "user", "project"):
            self.assertIn(f'name="mode" value="{mode}"', self.template)
        # The historical single-term box and its validation path are untouched.
        self.assertIn('id="search-term"', self.template)
        self.assertIn("async function runManualSearch", self.script)
        self.assertIn("sm:grid-cols-2 lg:grid-cols-5", self.template)
        self.assertIn('id="taxon-choice-list"', self.template)

    def test_advanced_section_holds_the_technical_controls(self):
        advanced = self.template.split('id="advanced-options"')[1].split("</details>")[0]
        self.assertIn('id="digits-off"', advanced)
        self.assertIn('id="verbose"', advanced)
        self.assertIn('name="search-mode"', advanced)

    def test_the_early_stop_rule_matches_the_cli(self):
        """1.8.1: only a stage too big to finish aborts on its first hit."""
        self.assertIn("const EARLY_STOP_MIN_CANDIDATES = 5000;", self.script)
        self.assertIn("stopOnFullMatch: stage.total > earlyStopMin", self.script)
        # And the reason is recorded where the next reader will look.
        self.assertIn("upload order", self.script)

    def test_ranking_breaks_ties_the_way_the_cli_does(self):
        """Score, then stage, then distance from the number typed, then ID."""
        self.assertIn("function rankMatches(matches, origin = null)", self.script)
        ranking = self.script.split("function rankMatches(")[1].split("\n    }")[0]
        for term in ("byScore", "byStage", "byDistance"):
            self.assertIn(term, ranking)
        # The on-screen order must be ranked against the same origin the ladder
        # used, or the list and the result disagree about which match is best.
        self.assertIn("function reorderResults(criteria, origin = null)", self.script)
        self.assertNotIn("reorderResults(criteria);", self.script)

    def test_maximum_wrong_digits_defaults_match_the_cli(self):
        # 1.8.0: --auto caps the ladder at three by default; a normal search
        # keeps the historical one.
        self.assertIn("const AUTO_DEFAULT_MAX_DIGITS = 3;", self.script)
        self.assertIn("const MANUAL_DEFAULT_MAX_DIGITS = 1;", self.script)
        self.assertRegex(self.template, r'<option value="3" selected>')

    def test_accessibility_affordances_are_not_regressed(self):
        self.assertIn('id="taxon-suggestions"', self.template)
        self.assertIn('id="taxon-pinned-label"', self.template)
        self.assertIn('aria-autocomplete="list"', self.template)
        self.assertIn(".finder-mode:focus-visible + label", self.template)
        self.assertIn(".finder-search-mode:focus-visible + label", self.template)
        self.assertIn("#advanced-options > summary:focus-visible", self.template)
        self.assertNotIn('id="progress-panel" aria-live=', self.template)
        self.assertIn('id="progress-status" aria-live="polite"', self.template)
        self.assertIn("prefers-reduced-motion", self.template)
        # The clue autocompletes get the same keyboard and screen-reader
        # treatment as the single-criterion one, not a lesser version.
        for clue in ("genus", "family"):
            self.assertIn(f'id="clue-{clue}-suggestions"', self.template)
            self.assertIn(f'id="clue-{clue}-suggestion-status"', self.template)
            self.assertIn(f'aria-controls="clue-{clue}-suggestions"', self.template)
        self.assertIn('aria-live="polite"', self.template)

    def test_the_way_out_is_one_obvious_button_at_the_top_of_the_results(self):
        """The search stops on a match that may well be the wrong observation.

        iNaturalist numbers observations in upload order, so with a single clue a
        neighbour of a mistyped number satisfies every clue by coincidence. Only
        the reader can tell, so the way to carry on has to be impossible to miss:
        one button, inside the results, above the cards.
        """
        self.assertIn('id="keep-looking"', self.template)
        self.assertIn('id="keep-looking-button"', self.template)
        self.assertIn("Not the observation you were looking for?", self.template)
        results = self.template.split('id="results-section"')[1]
        self.assertLess(
            results.index('id="keep-looking"'),
            results.index('id="results-list"'),
            "the way to keep looking must come before the result cards",
        )
        # It is a real button with a label, reachable by keyboard and readable
        # without relying on the icon.
        self.assertIn('id="keep-looking-label"', self.template)
        self.assertIn('aria-labelledby="keep-looking-heading"', self.template)
        self.assertIn('aria-hidden="true"', self.template)
        # The old two-button mid-search prompt is gone: one affordance, not two.
        self.assertNotIn("stage-confirm", self.template)
        self.assertNotIn("stage-confirm", self.script)
        # window.confirm() belongs to the manual search's up-front sizing check
        # only; the auto ladder must never block the page on a dialog. Comment
        # lines are stripped first, because the code that replaced the dialog
        # says so in a comment right above itself.
        auto = self.script.split("// ---- section:auto-ui ----")[1]
        code = "\n".join(
            line for line in auto.splitlines() if not line.lstrip().startswith("//")
        )
        self.assertNotIn("window.confirm", code)

    def test_the_page_text_describes_the_automatic_search(self):
        # The credited release is checked against the vendored CLI in
        # InatFinderParityFixtureTests; here it only has to be current.
        self.assertIn("v1.8.1", self.template)
        self.assertNotIn("v1.8.0", self.template)
        self.assertNotIn("v1.7.5", self.template)
        # The privacy statement is still true and still stated.
        self.assertIn("runs in your browser", self.template)
        self.assertIn("Dikarya does not store your query", self.template)
        # Auto mode is the recommended default, not an experiment.
        self.assertIn("Recommended", self.template)
        for word in ("experimental", "beta", "CandidatePlan", "--auto", "resume token"):
            self.assertNotIn(word, self.template, f"user-facing copy should not say {word!r}")

    def test_the_finder_still_talks_to_inaturalist_from_the_browser(self):
        # No server-side search endpoint may creep in behind the finder page.
        self.assertIn("const API = 'https://api.inaturalist.org/v1';", self.script)
        routes = (REPO / "app" / "main" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("return render_template('inat_finder.html')", routes)


class InatFinderParityFixtureTests(unittest.TestCase):
    """The fixture the JS ladder is checked against must describe this CLI.

    Syncing inat_finder.py without regenerating the fixture would leave the
    browser port pinned to the previous release's candidate order, which is
    exactly the silent drift the fixture exists to prevent.
    """

    @staticmethod
    def _load_cli():
        if "tqdm" not in sys.modules:
            module = types.ModuleType("tqdm")
            module.tqdm = lambda *args, **kwargs: None
            sys.modules["tqdm"] = module
        spec = importlib.util.spec_from_file_location(
            "inat_finder_cli_under_test", REPO / "inat_finder.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        return cli

    def test_fixture_matches_the_vendored_cli(self):
        try:
            cli = self._load_cli()
        except ImportError as exc:  # pragma: no cover - depends on the environment
            self.skipTest(f"the vendored CLI needs {exc.name}")
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["generated_from"], f"inat_finder.py {cli.VERSION}")
        self.assertEqual(payload["large_search_threshold"], cli.LARGE_SEARCH_THRESHOLD)
        self.assertEqual(payload["auto_default_max_digits"], cli.AUTO_DEFAULT_MAX_DIGITS)
        self.assertEqual(payload["batch_size"], cli.BATCH_SIZE)

        for case in payload["cases"]:
            number = case["number"]
            for plan_fixture in case["plans"]:
                plan = cli.build_candidate_plan(number, plan_fixture["digits_off"])
                digest = hashlib.sha256()
                for candidate in plan:
                    digest.update(candidate.encode("ascii"))
                    digest.update(b"\n")
                where = f"{number} / digits {plan_fixture['digits_off']}"
                self.assertEqual(plan.total, plan_fixture["total"], where)
                self.assertEqual(digest.hexdigest(), plan_fixture["sha256"], where)

            # The ladder's stages share one seen set, so stage k is plan k minus
            # everything the earlier stages already yielded.
            seen = set()
            for stage_fixture in case["stages"]:
                stage = cli.AutoStage(
                    stage_fixture["index"],
                    cli.build_candidate_plan(number, stage_fixture["index"]),
                    seen,
                )
                digest = hashlib.sha256()
                for candidate in stage:
                    digest.update(candidate.encode("ascii"))
                    digest.update(b"\n")
                where = f"{number} / stage {stage_fixture['index']}"
                self.assertEqual(stage.total, stage_fixture["total"], where)
                self.assertEqual(digest.hexdigest(), stage_fixture["sha256"], where)
                self.assertEqual(len(seen), stage_fixture["seen_after"], where)

    def test_the_vendored_cli_is_the_release_the_page_credits(self):
        try:
            cli = self._load_cli()
        except ImportError as exc:  # pragma: no cover - depends on the environment
            self.skipTest(f"the vendored CLI needs {exc.name}")
        self.assertEqual(cli.VERSION, "1.8.1")
        self.assertIn(f"v{cli.VERSION}", TEMPLATE.read_text(encoding="utf-8"))
