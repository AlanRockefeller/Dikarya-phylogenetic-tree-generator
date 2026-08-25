"""Regression coverage for the second review pass over the PR #3 fixes.

Six defects were found in the first pass's own work: a voucher parser that
truncated identifiers, CodeRabbit fixes applied to a stylesheet nothing loads, a
resize separator exposed without a value, a Downloads menu whose CSS fought its
own toggle, an email address still reaching the background job logs, and
trailing whitespace.

Where the behaviour lives in the browser, the tests run the *shipped* script out
of the template through a small node harness rather than a copy of it, so they
cannot drift away from what is actually served.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.main import routes as main_routes
from app.services import log_context

REPO = Path(__file__).resolve().parents[1]
JS_DIR = Path(__file__).resolve().parent / "js"


def read(*parts):
    return (REPO / Path(*parts)).read_text(encoding="utf-8")


def extract_js(html, start_marker, end_marker, include_end=False):
    """Pull a verbatim slice of inline script out of a template."""
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    if include_end:
        end += len(end_marker)
    return html[start:end]


def run_node(harness, script, *args, expect_json=True):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "extracted.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, str(JS_DIR / harness), str(path), *args],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        raise AssertionError(
            "{} failed:\n{}\n{}".format(harness, proc.stdout, proc.stderr)
        )
    return json.loads(proc.stdout) if expect_json else proc.stdout


class VoucherNumberingTests(unittest.TestCase):
    """A voucher number is an identifier: reproduced exactly, or refused.

    Two earlier attempts were wrong. ``parseInt`` silently repeated values past
    2**53, and capping the digit run with ``slice(-12)`` silently dropped
    leading digits, so the browser previewed a *different* identifier from the
    one the server printed. Neither may come back.
    """

    # (start_number field, prefix, offset)
    CASES = [
        ("001", "AR-", 0),
        ("001", "AR-", 1),
        ("001", "AR-", 999),
        ("9007199254740992", "V", 0),
        ("9007199254740992", "V", 1),  # parseInt repeated the previous value here
        ("123456789012345678901234567890", "X", 5),  # exactly at the limit
        ("1234567890123456789012345678901", "X", 5),  # one past the limit
        ("abc", "P", 2),
        ("", "P", 2),
        ("  042xyz ", "P", 8),
        ("000000000001", "Z", 0),
        ("999999999999", "Z", 1),
        # A starting number must be digits and nothing else. Extracting the
        # first digit run turned "ABC123" into 123 -- a number the user never
        # typed, with the letters dropped rather than moved to the prefix field.
        ("ABC123", "P", 0),
        ("123ABC", "P", 0),
        ("12 34", "P", 0),
        ("\u0661\u0662\u0663", "P", 0),
        # Surrounding whitespace around a valid number is still fine, and
        # leading zeros still set the padding width.
        ("  0042  ", "P", 8),
        ("007", "P", 995),
        # One past the digit limit: refused on both sides, never truncated.
        ("1" * (main_routes.MAX_VOUCHER_NUMBER_DIGITS + 1), "P", 1),
    ]

    def test_the_browser_parser_never_truncates(self):
        html = read("app", "templates", "voucher_labels.html")
        self.assertNotIn("slice(-12)", html)
        self.assertIn("BigInt(match[0])", html)
        # One parser still backs both call sites.
        self.assertEqual(html.count("function startNumberParts()"), 1)
        self.assertNotIn("const start = match ? parseInt(match[0], 10) : 1;", html)

    def test_the_limit_is_the_same_number_on_both_sides(self):
        html = read("app", "templates", "voucher_labels.html")
        client = int(
            re.search(r"const MAX_VOUCHER_NUMBER_DIGITS = (\d+);", html).group(1)
        )
        self.assertEqual(client, main_routes.MAX_VOUCHER_NUMBER_DIGITS)

    def test_the_server_preserves_a_long_number_exactly(self):
        digits = "1" * main_routes.MAX_VOUCHER_NUMBER_DIGITS
        prefix, start, width = main_routes._voucher_number_parts(
            {"prefix": "AR-", "start_number": digits}
        )
        self.assertEqual(start, int(digits))
        self.assertEqual(
            main_routes._voucher_format_label(prefix, start, width, 1),
            "AR-" + str(int(digits) + 1),
        )

    def test_a_number_past_the_limit_falls_back_instead_of_truncating(self):
        digits = "9" * (main_routes.MAX_VOUCHER_NUMBER_DIGITS + 1)
        prefix, start, width = main_routes._voucher_number_parts(
            {"prefix": "AR-", "start_number": digits}
        )
        self.assertEqual((start, width), (1, 3))
        label = main_routes._voucher_format_label(prefix, start, width, 0)
        # Emphatically not some tail of what was entered.
        self.assertNotIn(digits[-12:], label)

    def test_an_oversized_number_cannot_crash_the_generator(self):
        # int() refuses a string past sys.get_int_max_str_digits(); the explicit
        # bound has to stop that before it becomes a 500.
        _, start, width = main_routes._voucher_number_parts(
            {"prefix": "", "start_number": "9" * 6000}
        )
        self.assertEqual((start, width), (1, 3))

    def test_the_browser_says_so_instead_of_silently_renumbering(self):
        html = read("app", "templates", "voucher_labels.html")
        self.assertIn('id="voucher_start_number_notice"', html)
        self.assertIn("function renderStartNumberNotice()", html)

    def test_browser_and_server_produce_identical_labels(self):
        script = extract_js(
            read("app", "templates", "voucher_labels.html"),
            # Anchored on code, not on comment prose, so re-wording a comment
            # cannot quietly stop this test from extracting the real parser.
            "    const MAX_VOUCHER_NUMBER_DIGITS =",
            "    function renderStartNumberNotice()",
        )
        rows = run_node("voucher_number.test.js", script, json.dumps(self.CASES))
        self.assertEqual(len(rows), len(self.CASES))
        for (raw, prefix, offset), row in zip(self.CASES, rows):
            prefix_out, start, width = main_routes._voucher_number_parts(
                {"prefix": prefix, "start_number": raw}
            )
            expected = main_routes._voucher_format_label(
                prefix_out, start, width, offset
            )
            self.assertEqual(
                row["label"],
                expected,
                "client/server disagree for start={!r} offset={}".format(raw, offset),
            )

    def test_the_over_limit_case_is_flagged_to_the_user(self):
        script = extract_js(
            read("app", "templates", "voucher_labels.html"),
            # Anchored on code, not on comment prose, so re-wording a comment
            # cannot quietly stop this test from extracting the real parser.
            "    const MAX_VOUCHER_NUMBER_DIGITS =",
            "    function renderStartNumberNotice()",
        )
        cases = [
            ("1" * main_routes.MAX_VOUCHER_NUMBER_DIGITS, "X", 0),
            ("1" * (main_routes.MAX_VOUCHER_NUMBER_DIGITS + 1), "X", 0),
        ]
        rows = run_node("voucher_number.test.js", script, json.dumps(cases))
        self.assertFalse(rows[0]["tooLong"])
        self.assertTrue(rows[1]["tooLong"])


class LogLineStreamClassTests(unittest.TestCase):
    """Command lines must stay green, and no other stream may become a class.

    The safe-DOM refactor replaced ``className = `log-line ${event.stream}` `` with
    a two-value check for stdout/stderr, which silently dropped the ``cmd`` class
    that publish_command() relies on to render "$ mafft ..." in green.
    """

    def test_the_worker_still_emits_the_cmd_stream(self):
        events = read("app", "workers", "events.py")
        self.assertIn('"stream": "cmd"', events)

    def test_the_page_still_styles_the_cmd_class(self):
        html = read("app", "templates", "job_status.html")
        self.assertIn("#job-status-page .log-line.cmd", html)

    def test_only_the_known_streams_become_classes(self):
        js = read("app", "static", "js", "job_status.js")
        self.assertIn("LOG_STREAM_CLASSES", js)
        self.assertIn("'stdout', 'stderr', 'cmd'", js)

    def test_class_assignment_behaviour(self):
        # Runs the shipped job_status.js: cmd/stdout/stderr are applied, and a
        # hostile stream value contributes nothing.
        run_node(
            "log_line_classes.test.js",
            read("app", "static", "js", "job_status.js"),
            expect_json=False,
        )


class DownloadsDropdownBehaviourTests(unittest.TestCase):
    """Click, keyboard, Escape, outside click and touch must open AND close it."""

    def test_no_css_variant_can_override_the_toggle(self):
        html = read("app", "templates", "job_status.html")
        menu_tag = html.split('id="downloads-menu"')[1].split(">")[0]
        # A group-hover:/group-focus-within: rule forces `display` and beats the
        # `hidden` class, which is what left the menu unclosable while the
        # trigger held focus, with aria-expanded="false" announced over it.
        self.assertNotIn("group-hover:block", menu_tag)
        self.assertNotIn("group-focus-within:block", menu_tag)
        self.assertIn("hidden", menu_tag)

    def test_visible_state_and_aria_expanded_never_disagree(self):
        script = extract_js(
            read("app", "templates", "job_status.html"),
            "    // Downloads dropdown.",
            "    })();",
            include_end=True,
        )
        # The harness asserts every open/close path; a mismatch exits non-zero.
        run_node("downloads_dropdown.test.js", script, expect_json=False)


class AlignmentSplitterAriaTests(unittest.TestCase):
    """A focusable role="separator" is a range widget and needs its values."""

    def setUp(self):
        self.js = read("app", "static", "js", "alignment_viewer.js")
        self.apply_body = self.js.split("function applyNamesWidth(")[1].split("\n    }")[0]

    def test_the_values_exist(self):
        self.assertIn("function updateResizerAriaValues(", self.js)
        for attr in ("aria-valuemin", "aria-valuemax", "aria-valuenow"):
            self.assertIn(attr, self.js)

    def test_they_are_refreshed_wherever_the_width_changes(self):
        # applyNamesWidth is the single funnel for drag, arrow keys, Home/Enter,
        # the restored preference and container resizes.
        self.assertIn("updateResizerAriaValues(", self.apply_body)

    def test_the_bounds_match_the_clamp_that_is_actually_applied(self):
        self.assertIn("Math.max(NAMES_MIN_WIDTH, total - CELLS_MIN_WIDTH)", self.apply_body)
        helper = self.js.split("function updateResizerAriaValues(")[1].split("\n    }")[0]
        self.assertIn("String(NAMES_MIN_WIDTH)", helper)
        self.assertIn("String(Math.round(max))", helper)
        self.assertIn("String(width)", helper)


class DeadStylesheetTests(unittest.TestCase):
    """job_status.css was served to nobody; loading it would have collided."""

    def test_the_dead_stylesheet_is_gone(self):
        self.assertFalse((REPO / "app" / "static" / "css" / "job_status.css").exists())

    def test_no_live_source_references_it(self):
        """Scan the application tree only.

        The first version shelled out to a repo-wide ``git grep``. That passed
        only for as long as this file was untracked: ``git grep`` skips
        untracked files, so once the test was committed it matched its own
        source and failed. Walking ``app/`` instead is independent of whether
        anything is tracked, and it also catches a stale reference in a file
        that has not been added to the index yet -- which ``git grep`` would
        miss entirely.
        """
        needle = "job_status" + ".css"
        app_dir = REPO / "app"
        scanned = 0
        offenders = []
        for path in app_dir.rglob("*"):
            if not path.is_file() or path.suffix not in {".html", ".py", ".js", ".css", ".txt"}:
                continue
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(str(path.relative_to(REPO)))
        # Guard against a glob that silently matches nothing and passes vacuously.
        self.assertGreater(scanned, 100, "the scan inspected suspiciously few files")
        self.assertEqual(offenders, [], "stale reference to the deleted stylesheet")

    def test_the_scan_would_catch_a_reintroduced_reference(self):
        """The assertion above must be able to fail."""
        needle = "job_status" + ".css"
        probe = REPO / "app" / "templates" / "_stale_stylesheet_probe.html"
        self.assertFalse(probe.exists())
        probe.write_text(
            '<link rel="stylesheet" href="/static/css/{}">\n'.format(needle),
            encoding="utf-8",
        )
        try:
            with self.assertRaises(AssertionError):
                self.test_no_live_source_references_it()
        finally:
            probe.unlink()

    def test_the_live_page_keeps_its_own_scoped_styles(self):
        # The page's real styles are inline and scoped under #job-status-page;
        # that is where the dark-mode fix has to live.
        html = read("app", "templates", "job_status.html")
        self.assertIn(".dark #job-status-page .step-label.skipped", html)


class BackgroundJobLogIdentityTests(unittest.TestCase):
    """Background job logs must not carry the user's email address."""

    def test_identity_is_the_internal_id_or_anon(self):
        self.assertEqual(
            log_context.background_user_identity(SimpleNamespace(user_id=42)), "id:42"
        )
        self.assertEqual(
            log_context.background_user_identity(SimpleNamespace(user_id=None)), "anon"
        )
        self.assertEqual(log_context.background_user_identity(None), "anon")

    def test_an_email_on_the_job_is_never_preferred(self):
        job = SimpleNamespace(
            user_id=7, user=SimpleNamespace(email="someone@example.com")
        )
        self.assertEqual(log_context.background_user_identity(job), "id:7")

    def test_an_anonymous_job_with_a_stray_user_object_is_still_anon(self):
        job = SimpleNamespace(
            user_id=None, user=SimpleNamespace(email="someone@example.com")
        )
        self.assertEqual(log_context.background_user_identity(job), "anon")

    def test_no_background_binder_reaches_for_the_email(self):
        for parts in (
            ("app", "workers", "tasks.py"),
            ("app", "services", "inaturalist_tree_service.py"),
        ):
            source = read(*parts)
            for call in re.findall(r"bind_background_context\(user=[^\n]*", source):
                self.assertNotIn("email", call, "{}: {!r}".format("/".join(parts), call))


class AuthorFeePolicyTests(unittest.TestCase):
    """One published fee policy, stated the same way everywhere.

    CodeRabbit finding 35: support.html advertised a $200-$400 APC while
    home.html and about.html said authors are charged nothing. The maintainer
    resolved it on 2026-08-24 -- there are no fees -- so support.html was
    corrected to match. These pin the three pages together.
    """

    PAGES = ("home.html", "about.html", "support.html")

    def test_no_page_advertises_a_charge(self):
        for page in self.PAGES:
            html = read("app", "templates", "journal", page)
            self.assertNotRegex(
                html, r"\$\s?\d{2,}",
                "{} still quotes a price".format(page),
            )
            self.assertNotIn("we charge", html.lower(), page)

    def test_support_states_the_no_fee_policy_explicitly(self):
        html = read("app", "templates", "journal", "support.html")
        self.assertIn("Author Fees", html)
        self.assertIn("There are none.", html)

    def test_nothing_on_the_support_page_implies_fees_exist(self):
        # The sponsorship blurb used to promise sponsors "help reduce author
        # fees", which only makes sense if there are some.
        html = read("app", "templates", "journal", "support.html")
        self.assertNotIn("reduce author fees", html)


class WhitespaceHygieneTests(unittest.TestCase):
    def test_git_diff_check_is_clean(self):
        proc = subprocess.run(
            ["git", "diff", "--check"], cwd=REPO, capture_output=True, text=True
        )
        self.assertEqual(
            proc.returncode, 0, "git diff --check reported:\n" + proc.stdout
        )


if __name__ == "__main__":
    unittest.main()
