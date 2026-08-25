"""Regression coverage for the CodeRabbit PR #3 frontend/template audit.

Each test pins a property one of the fixes established, not the shape of the
code that provides it. The JavaScript-only fixes that no server-side assertion
can reach (EventSource lifecycle, the alignment-viewer abort, DOM-built log
lines) are covered by the manual checks in the accompanying report.
"""

import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("ALLOW_SQLITE_FALLBACK", "1")

from app import create_app
from app.main import routes as main_routes


REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "app" / "templates"
STATIC = REPO / "app" / "static"


def read(*parts):
    return (REPO / Path(*parts)).read_text(encoding="utf-8")


class TodoSuggestionCsrfTests(unittest.TestCase):
    """The public suggestion form used to be reachable from any origin."""

    def setUp(self):
        self.app = create_app("development")
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)

    def test_todo_view_is_not_csrf_exempt(self):
        view = self.app.view_functions["main.todo"]
        # Flask-WTF records exemptions on the function itself.
        self.assertFalse(getattr(view, "_csrf_exempt", False))
        self.assertNotIn("csrf.exempt", read("app", "main", "routes.py").split("def todo():")[0].splitlines()[-1])

    def test_post_without_a_token_is_rejected(self):
        client = self.app.test_client()
        resp = client.post("/todo", data={"name": "Someone", "suggestion": "Hi"})
        self.assertEqual(resp.status_code, 400)

    def test_the_form_ships_a_token_field(self):
        html = read("app", "templates", "todo.html")
        form = html.split("url_for('main.todo')")[1]
        self.assertIn('name="csrf_token"', form.split("</form>")[0])


class ApiTokenScopeDefaultTests(unittest.TestCase):
    """A token minted by just typing a name must not carry write authority."""

    def test_only_read_scopes_are_preselected(self):
        html = read("app", "templates", "user", "api_tokens.html")
        checkbox = [line for line in html.splitlines() if 'name="scopes"' in line]
        self.assertEqual(len(checkbox), 1)
        self.assertNotIn("checked", checkbox[0])
        # The conditional on the following line is what pre-selects read scopes.
        idx = html.splitlines().index(checkbox[0])
        self.assertIn("scope.endswith(':read')", html.splitlines()[idx + 1])

    def test_the_server_still_rejects_an_empty_or_unknown_scope_set(self):
        source = read("app", "user", "routes.py")
        self.assertIn("s for s in requested_scopes if s in ALL_SCOPES", source)
        self.assertIn("At least one scope must be selected.", source)


class SwaggerTokenPersistenceTests(unittest.TestCase):
    def test_bearer_tokens_are_not_written_to_local_storage(self):
        html = read("app", "templates", "api_v1", "docs.html")
        self.assertNotIn("persistAuthorization", html)


class DocsExampleTests(unittest.TestCase):
    """The published examples are what API users copy verbatim."""

    def test_the_shell_example_only_downloads_after_completion(self):
        html = read("app", "templates", "api_v1", "docs.html")
        self.assertIn("MAX_WAIT_SECONDS", html)
        self.assertNotIn("completed|failed|error) break", html)
        self.assertIn("curl -fS -H", html)

    def test_the_shell_polling_deadline_bounds_each_request_too(self):
        """MAX_WAIT_SECONDS has to be a real deadline.

        The loop checked the deadline before starting each curl, but a single
        status request could then block past it on its own, so the example could
        run indefinitely while claiming a configurable ceiling.
        """
        html = read("app", "templates", "api_v1", "docs.html")
        loop = html.split("deadline=$((SECONDS + MAX_WAIT_SECONDS))")[1]
        loop = loop.split("if [[ \"$STATUS\" != \"completed\" ]]")[0]

        self.assertIn("--connect-timeout", loop)
        # Bounded by what is left of the wall clock, not by a fixed number.
        self.assertIn("remaining=$(( deadline - SECONDS ))", loop)
        self.assertIn('--max-time "$remaining"', loop)
        # And a non-positive remainder never reaches curl.
        self.assertIn("(( remaining &lt; 1 ))", loop)
        # The existing jq/status handling is untouched.
        self.assertIn(".data.status // .error.message", loop)

    def test_every_python_request_carries_a_timeout(self):
        html = read("app", "templates", "api_v1", "docs.html")
        example = html.split("import requests")[1]
        calls = re.findall(r"requests\.(?:get|post)\((.*?)\n\)", example, re.S)
        calls += re.findall(r"requests\.(?:get|post)\([^\n]*\)", example)
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("timeout=", call)


class JobStatusTemplateTests(unittest.TestCase):
    def setUp(self):
        self.html = read("app", "templates", "job_status.html")

    def test_download_logs_points_at_the_pipeline_log_endpoint(self):
        block = self.html.split("Download Logs")[0]
        anchor = block[block.rindex("<a "):]
        self.assertIn("/api/job/{{ job_id }}/logs/pipeline", anchor)
        self.assertNotIn('href="#"', anchor)

    def test_the_downloads_menu_is_operable_without_hover(self):
        self.assertIn('id="downloads-menu-btn"', self.html)
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn('aria-haspopup="true"', self.html)
        self.assertIn('aria-controls="downloads-menu"', self.html)
        # The button must be a real button, not an implicit submit. Inspect the
        # whole opening tag as it is shipped: the previous version appended
        # 'type="button"' to its own haystack, so it passed whether or not the
        # template still carried the attribute.
        start = self.html.index('id="downloads-menu-btn"')
        opening = self.html[self.html.rindex("<button", 0, start):]
        opening = opening[:opening.index(">") + 1]
        self.assertRegex(opening, r'<button\b[^>]*\btype="button"')
        # Behaviour itself is covered by DownloadsDropdownBehaviourTests in
        # tests/test_coderabbit_review_fixes.py, which runs the shipped script.

    def test_the_dark_skipped_label_is_lighter_than_the_dark_card(self):
        rule = self.html.split(".dark #job-status-page .step-label.skipped {")[1].split("}")[0]
        declaration = [l.strip() for l in rule.splitlines() if l.strip().startswith("color:")]
        self.assertEqual(declaration, ["color: #6b7280;"])


class JobViewerTemplateTests(unittest.TestCase):
    def setUp(self):
        self.html = read("app", "templates", "job_viewer.html")

    def test_script_globals_are_serialized_with_tojson(self):
        self.assertIn("window.JOB_ID = {{ job_id|tojson }};", self.html)
        self.assertIn("window.TREE_METHOD = {{ tree_support_context.tree_method|tojson }};", self.html)
        self.assertNotRegex(self.html, r'window\.(JOB_ID|TREE_METHOD) = "\{\{')

    def test_add_sequences_is_hidden_on_a_view_only_tree(self):
        before = self.html.split('id="btn-add-sequences-link"')[0]
        # The nearest preceding Jinja conditional must gate the edit link.
        self.assertIn("{% if not view_only %}", before[-400:])
        self.assertNotIn("{% endif %}", before[before.rindex("{% if not view_only %}"):])


class ViewOnlyRootingControlsTests(unittest.TestCase):
    """Rooting persists an edit, so a view-only tree must not offer it."""

    def setUp(self):
        self.js = read("app", "static", "js", "tree_viewer_controller.js")

    def test_the_rooting_handler_is_guarded(self):
        handler = self.js.split("rootingModeSelect.addEventListener('change'")[1][:400]
        self.assertIn("window.VIEW_ONLY", handler)

    def test_the_rooting_select_is_disabled_in_view_only_mode(self):
        block = self.js.split("if (window.VIEW_ONLY) {")[1].split("return;")[0]
        self.assertIn("rootingModeSelect.disabled = true", block)
        self.assertIn("disableBtn(btnSetSoi)", block)

    def test_the_backend_remains_the_authority(self):
        api = read("app", "api", "routes.py")
        endpoint = api.split("def set_rooting_mode_endpoint(job_id):")[1][:600]
        self.assertIn('check_job_access(job_id, mode="edit")', endpoint)


class VoucherCustomLayoutTests(unittest.TestCase):
    """A custom sheet that cannot hold the requested grid used to print off-page."""

    def _layout(self, **form):
        base = {
            "label_size": "custom",
            "custom_width": "8.5",
            "custom_height": "1",
            "custom_columns": "2",
            "custom_rows": "40",
            "custom_margin_left": "0.25",
            "custom_margin_top": "0.5",
            "custom_gap_x": "0.125",
            "custom_gap_y": "0",
        }
        base.update(form)
        return main_routes._voucher_layout_from_form(base)["preset"]

    def test_columns_are_clamped_to_what_the_page_holds(self):
        preset = self._layout()
        self.assertEqual(preset["columns"], 1)

    def test_rows_are_clamped_to_what_the_page_holds(self):
        preset = self._layout(custom_rows="40", custom_height="1")
        # 11in page less a 0.5in top margin -> 10.5in usable, 1in labels, no gap.
        self.assertEqual(preset["rows"], 10)

    def test_the_three_column_custom_default_still_fits(self):
        # 0.25 + 3x2.625 + 2x0.125 = 8.375in, inside the 8.5in page. The clamp
        # must not treat the leading margin as if it were mirrored on the right.
        preset = self._layout(custom_width="2.625", custom_columns="3", custom_rows="10")
        self.assertEqual((preset["columns"], preset["rows"]), (3, 10))

    def test_the_client_preview_applies_the_same_clamp(self):
        html = read("app", "templates", "voucher_labels.html")
        custom = html.split("if (presetKey === 'custom')")[1].split("return preset;")[0]
        self.assertIn("fitCount(usableWidth, preset.label_width, preset.gap_x)", custom)
        self.assertIn("fitCount(usableHeight, preset.label_height, preset.gap_y)", custom)


class JournalTemplateTests(unittest.TestCase):
    def test_the_footer_ethics_link_has_a_target(self):
        footer = read("app", "templates", "journal", "base_journal.html")
        self.assertIn("journal.about') }}#ethics", footer)
        self.assertIn('id="ethics"', read("app", "templates", "journal", "about.html"))

    def test_the_display_only_contact_form_cannot_submit(self):
        html = read("app", "templates", "journal", "contact.html")
        button = html.split("Send Message")[0]
        button = button[button.rindex("<button"):]
        self.assertIn('type="button"', button)
        self.assertIn("disabled", button)

    def test_the_archive_search_input_has_a_programmatic_label(self):
        html = read("app", "templates", "journal", "archive.html")
        self.assertIn('<label for="article-search"', html)
        self.assertIn('id="article-search"', html)

    def test_nomenclatural_resources_are_linked_over_https(self):
        html = read("app", "templates", "journal", "taxonomy.html")
        self.assertNotIn('href="http://', html)
        self.assertIn("https://www.indexfungorum.org/", html)
        self.assertIn("https://www.speciesfungorum.org/", html)

    def test_the_font_import_precedes_every_style_rule(self):
        css = read("app", "static", "css", "journal.css")
        import_at = css.index("@import")
        first_rule = min(css.index(":root {"), css.index("box-sizing"))
        self.assertLess(import_at, first_rule)


class StatusToastTests(unittest.TestCase):
    """Callers pass warning/danger and an explicit duration; both were ignored."""

    def setUp(self):
        self.html = read("app", "templates", "base_modern.html")

    def test_the_helper_accepts_a_duration(self):
        self.assertIn("function showStatus(message, type = 'info', durationMs = 5000)", self.html)

    def test_every_variant_used_by_callers_is_rendered_distinctly(self):
        table = self.html.split("STATUS_VARIANT_CLASSES = {")[1].split("};")[0]
        for variant in ("success", "error", "danger", "warning", "info"):
            self.assertIn(f"{variant}:", table)
        self.assertIn("bg-amber-500", table)
        self.assertIn("bg-red-500", table)


class LegacySubmissionTemplateTests(unittest.TestCase):
    """beginner.html / advanced.html are currently unrouted but still shipped."""

    def test_a_bootstrap_bootstrap_value_of_zero_survives(self):
        html = read("app", "templates", "advanced.html")
        self.assertIn('intOr("bootstrap", 1000)', html)
        self.assertNotIn('parseInt(document.getElementById("bootstrap").value) || 100', html)

    def test_the_file_read_error_uses_a_real_bootstrap_class(self):
        html = read("app", "templates", "advanced.html")
        self.assertNotIn("allow-danger", html)

    def test_the_beginner_submit_button_is_disabled_while_posting(self):
        html = read("app", "templates", "beginner.html")
        self.assertIn('const btn = document.getElementById("submit_btn");', html)
        self.assertIn("if (btn.disabled) return;", html)


if __name__ == "__main__":
    unittest.main()
