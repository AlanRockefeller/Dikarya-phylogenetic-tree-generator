"""Structural guards for the tree viewer page (app/templates/job_viewer.html).

The viewer's controls are being regrouped (title row, Export menu, toolbar, side
panel). Every control is found by id from tree_viewer_controller.js, and many
from the mobile sheet by data-mobile-trigger, so a control that is dropped or
renamed during a move does not fail loudly: the button simply stops working.
These tests render the real template through Jinja, with a stub in place of
base_modern.html, and check the rendered page rather than the template source,
so a control hidden behind the wrong {% if %} is caught as well.
"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "app" / "templates"
CONTROLLER = REPO / "app" / "static" / "js" / "tree_viewer_controller.js"
CSS = REPO / "app" / "static" / "css" / "tree_viewer.css"

BASE_STUB = (
    "{% block head %}{% endblock %}"
    "{% block content %}{% endblock %}"
    "{% block scripts %}{% endblock %}"
)

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "source", "track", "wbr",
}


def _url_for(endpoint, **values):
    query = "&".join(f"{k}={v}" for k, v in sorted(values.items()))
    return f"/{endpoint}" + (f"?{query}" if query else "")


def render(view_only=False, claude_review_enabled=True, **job_details):
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base_modern.html": BASE_STUB}),
            FileSystemLoader(str(TEMPLATES)),
        ]),
        autoescape=True,
    )
    env.globals["url_for"] = _url_for
    details = {
        "alignment_method": "mafft",
        "tree_method": "iqtree",
        "trimming_method": "none",
    }
    details.update(job_details)
    return env.get_template("job_viewer.html").render(
        job_id="aq7c",
        view_only=view_only,
        claude_review_enabled=claude_review_enabled,
        job_details=details,
        generation_details={},
        tree_support_context={"tree_method": details["tree_method"]},
    )


# A job that switches on every optional block the page has, so a full render
# contains every control the controller can look up.
EVERYTHING = dict(
    tree_method="mrbayes",
    trimming_method="trimal_gappy",
    trimming_details={"report_file": "alignment/alignment_trimmed_report.html"},
    import_filter_details={
        "duplicates": {
            "removed_count": 2,
            "removed_records": [{"name": "a", "duplicate_of": "b"}],
        },
        "mycomap": {
            "counts": {"contaminant": 1},
            "filtered_records": [{"name": "c", "sequence": "ACGT"}],
        },
    },
)


class _Element:
    def __init__(self, tag, attrs, ancestors):
        self.tag = tag
        self.attrs = dict(attrs)
        # Ids of every enclosing element, innermost last.
        self.ancestors = ancestors

    @property
    def id(self):
        return self.attrs.get("id")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.elements = []

    def _record(self, tag, attrs):
        ancestors = [el.id for el in self.stack if el.id]
        element = _Element(tag, attrs, ancestors)
        self.elements.append(element)
        return element

    def handle_starttag(self, tag, attrs):
        element = self._record(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self._record(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


def parse(html):
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return parser.elements


def by_id(elements):
    return {el.id: el for el in elements if el.id}


class TreeViewerLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = parse(render(**EVERYTHING))
        cls.full_ids = by_id(cls.full)
        cls.view_only = parse(render(view_only=True, **EVERYTHING))
        cls.view_only_ids = by_id(cls.view_only)
        cls.minimal = parse(render(claude_review_enabled=False))
        cls.minimal_ids = by_id(cls.minimal)

    def test_ids_are_unique(self):
        for elements in (self.full, self.view_only, self.minimal):
            ids = [el.id for el in elements if el.id]
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            self.assertEqual(duplicates, [])

    def test_every_element_the_controller_looks_up_is_rendered(self):
        controller = CONTROLLER.read_text(encoding="utf-8")
        wanted = set(re.findall(r"getEl\('([^']+)'\)", controller))
        rendered = set(self.full_ids) | set(self.view_only_ids)
        self.assertEqual(sorted(wanted - rendered), [])

    def test_mobile_sheet_triggers_point_at_real_controls(self):
        for elements, ids in ((self.full, self.full_ids), (self.view_only, self.view_only_ids),
                              (self.minimal, self.minimal_ids)):
            for attr in ("data-mobile-trigger", "data-mobile-step", "data-mobile-value-for"):
                targets = [el.attrs[attr] for el in elements if attr in el.attrs]
                self.assertEqual(sorted(set(targets) - set(ids)), [], attr)
        triggers = [el.attrs["data-mobile-trigger"] for el in self.full
                    if "data-mobile-trigger" in el.attrs]
        self.assertTrue(triggers)

    def test_every_download_lives_in_the_export_menu(self):
        downloads = [el for el in self.full if el.tag == "a"
                     and "/download/" in (el.attrs.get("href") or "")]
        self.assertTrue(downloads)
        for link in downloads:
            self.assertIn("export-menu", link.ancestors, link.attrs["href"])
        for element_id in ("newick-link-pruned", "newick-link-original", "nexus-link",
                           "btn-save-svg", "btn-save-png", "btn-save-jpg",
                           "fasta-edited", "fasta-original"):
            self.assertIn("export-menu", self.full_ids[element_id].ancestors, element_id)

    def test_conditional_downloads_keep_their_conditions(self):
        def hrefs(elements):
            return {el.attrs.get("href") for el in elements if el.tag == "a"}

        full, minimal = hrefs(self.full), hrefs(self.minimal)
        for suffix in ("download/mrbayes", "download/fasta/trimmed",
                       "download/alignment/inspection"):
            href = f"/api/job/aq7c/{suffix}"
            self.assertIn(href, full)
            self.assertNotIn(href, minimal)
        self.assertIn("/api/job/aq7c/download/fasta/aligned", minimal)

    def test_edited_fasta_starts_unavailable(self):
        link = self.full_ids["fasta-edited"]
        classes = link.attrs["class"].split()
        self.assertIn("is-unavailable", classes)
        self.assertNotIn("is-available", classes)
        self.assertEqual(link.attrs.get("aria-disabled"), "true")
        self.assertNotIn("href", link.attrs)

    def test_status_message_is_visible_where_desktop_controls_are_hidden(self):
        # tree_viewer.css hides #tree-desktop-controls on touch devices, and the
        # status line used to live inside it, so phones never saw a status.
        ancestors = self.full_ids["status-message"].ancestors
        self.assertIn("tree-status-dock", ancestors)
        self.assertIn("tree-stage", ancestors)
        self.assertIn("tree-viewer-panel", ancestors)

    def test_page_actions_sit_in_the_title_row(self):
        for element_id in ("btn-alignment-viewer", "btn-annotations",
                           "export-menu-wrap", "btn-claude-review"):
            self.assertIn("tree-title-actions", self.full_ids[element_id].ancestors,
                          element_id)
        self.assertIn("tree-title-actions", self.view_only_ids["btn-make-copy"].ancestors)
        self.assertNotIn("btn-make-copy", self.full_ids)
        self.assertNotIn("btn-claude-review", self.minimal_ids)

    def test_title_actions_hide_with_the_desktop_controls_on_touch(self):
        css = CSS.read_text(encoding="utf-8")
        coarse = css.split("@media (pointer: coarse) {", 1)[1]
        self.assertRegex(coarse, r"#tree-title-actions\s*\{\s*display:\s*none;")
        self.assertRegex(
            coarse,
            r"body\.mobile-show-desktop-controls #tree-title-actions\s*\{\s*display:\s*flex;",
        )

    def test_rooting_notice_is_a_chip_under_the_title(self):
        chip = self.full_ids["notice-rooting"]
        self.assertIn("tree-title-chips", chip.ancestors)
        self.assertNotIn("tree-notices", chip.ancestors)
        self.assertIn("notice-rooting", self.full_ids["notice-rooting-text"].ancestors)

    def test_duplicate_notice_can_be_dismissed(self):
        button = self.full_ids["btn-dismiss-duplicates-notice"]
        self.assertIn("notice-duplicates", button.ancestors)
        self.assertNotIn("notice-duplicates", self.minimal_ids)

    TOOLBAR_CONTROLS = (
        "btn-layout-linear", "btn-layout-radial", "btn-zoom-out", "btn-zoom-in", "btn-fit",
        "btn-align-tips", "btn-ladderize", "rooting-mode-select",
        "btn-reroot", "btn-add-sequences-link", "btn-recompute", "btn-undo",
        "btn-prune", "btn-rename", "btn-deselect", "btn-set-soi",
        "btn-selection-more", "input-branch-filter", "btn-fullscreen",
        "btn-tree-shortcuts-help",
    )

    def test_selection_actions_follow_undo_in_the_toolbar(self):
        ids = [el.id for el in self.full if el.id and "tree-toolbar" in el.ancestors]
        self.assertLess(ids.index("btn-undo"), ids.index("btn-prune"))
        self.assertLess(ids.index("btn-prune"), ids.index("btn-rename"))
        self.assertLess(ids.index("btn-rename"), ids.index("btn-deselect"))


    def test_manual_reroot_can_be_chosen_again(self):
        # "manual" only reports the current state; a separate "pick" option is the
        # action, so choosing it fires a change even on a tree already rooted by hand.
        options = {el.attrs.get("value"): el for el in self.full
                   if el.tag == "option" and "rooting-mode-select" in el.ancestors}
        self.assertIn("disabled", options["manual"].attrs)
        self.assertNotIn("disabled", options["pick"].attrs)
        controller = CONTROLLER.read_text(encoding="utf-8")
        handler = controller.split("rootingModeSelect.addEventListener('change'", 1)[1][:1200]
        self.assertIn("mode === 'pick'", handler)

    def test_every_root_state_has_an_option(self):
        # An outgroup root, or one the viewer cannot name, must still select some
        # option; otherwise the menu keeps the last choice and re-choosing it
        # fires no change. They report state only, so they cannot be chosen.
        options = {el.attrs.get("value"): el for el in self.full
                   if el.tag == "option" and "rooting-mode-select" in el.ancestors}
        for state in ("outgroup", "current"):
            self.assertIn("disabled", options[state].attrs, state)

    def test_toolbar_lives_in_the_tree_panel(self):
        # Full screen expands #tree-viewer-panel, so a control outside it would be
        # left behind the full-screen tree.
        self.assertIn("tree-viewer-panel", self.full_ids["tree-toolbar"].ancestors)
        for element_id in self.TOOLBAR_CONTROLS:
            self.assertIn("tree-toolbar", self.full_ids[element_id].ancestors, element_id)
        self.assertNotIn("btn-add-sequences-link", self.view_only_ids)

    def test_on_off_controls_start_with_a_pressed_state(self):
        for element_id in ("btn-layout-linear", "btn-layout-radial", "btn-align-tips",
                           "btn-fullscreen"):
            self.assertIn(self.full_ids[element_id].attrs.get("aria-pressed"),
                          ("true", "false"), element_id)
        # Sort cycles through three modes, so it names one instead of being pressed.
        self.assertNotIn("aria-pressed", self.full_ids["btn-ladderize"].attrs)

    def test_midpoint_on_and_off_live_in_the_root_menu(self):
        self.assertNotIn("btn-midpoint", self.full_ids)
        options = [el.attrs.get("value") for el in self.full
                   if el.tag == "option" and "rooting-mode-select" in el.ancestors]
        for mode in ("auto", "midpoint", "original", "most_divergent_hit", "manual", "pick"):
            self.assertIn(mode, options)
        # The mobile sheet mirrors the toolbar menu in place of its Midpoint button.
        self.assertIn("tree-mobile-more", self.full_ids["mobile-rooting-mode-select"].ancestors)

    def test_selection_menu_holds_all_and_none(self):
        actions = {el.attrs["data-action"] for el in self.full
                   if "btn-selection-action" in (el.attrs.get("class") or "").split()
                   and "selection-more-menu" in el.ancestors}
        self.assertTrue({"all", "none", "all-internal", "all-leaves", "select-filtered",
                         "inverse", "select-ancestors", "select-descendants"} <= actions)

    def test_full_screen_shortcut_is_documented_and_handled(self):
        help_keys = [el for el in self.full if el.tag == "kbd"
                     and "modal-tree-shortcuts" in el.ancestors]
        self.assertTrue(help_keys)
        html = render(**EVERYTHING)
        help_block = html.split('id="modal-tree-shortcuts"', 1)[1].split("</dl>", 1)[0]
        self.assertIn(">F</kbd>", help_block)
        controller = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("key === 'f'", controller)

    def test_full_screen_layout_is_not_limited_to_touch_devices(self):
        css = CSS.read_text(encoding="utf-8")
        before_touch_block = css.split("@media (pointer: coarse) {", 1)[0]
        self.assertIn("body.tree-expanded #tree-viewer-panel {", before_touch_block)
        self.assertIn("body.tree-expanded #modal-alignment-viewer", before_touch_block)
        coarse = css.split("@media (pointer: coarse) {", 1)[1]
        self.assertRegex(coarse, r"#tree-toolbar\s*\{\s*display:\s*none;")


    SETTINGS_TABS = {
        "support": ("cb-show-support", "support-type-badge", "input-pp-threshold", "input-bs-threshold",
                    "input-min-tips", "cb-hide-low-support", "label-fade-by-support",
                    "cb-fade-by-support"),
        "sequences": ("sequence-filter-count", "slider-query-cover", "query-cover-value",
                      "slider-subject-cover", "subject-cover-value", "slider-identity",
                      "identity-value", "btn-sequence-filter-reset"),
        "colors": ("color-group-chips", "color-preset-swatches", "btn-new-selection-set",
                   "btn-edit-selection-set-color", "input-selection-set-color",
                   "btn-delete-selection-set", "btn-uncolor-selection", "color-group-popover",
                   "input-color-group-name", "btn-create-color-group"),
    }

    def test_old_controls_card_is_gone(self):
        self.assertNotIn("tree-desktop-controls", self.full_ids)

    def test_settings_panel_sits_beside_the_tree_and_starts_closed(self):
        panel = self.full_ids["tree-settings-panel"]
        self.assertIn("tree-workspace", panel.ancestors)
        self.assertIn("tree-viewer-panel", panel.ancestors)
        self.assertIn("hidden", panel.attrs)
        self.assertIn("tree-workspace", self.full_ids["tree-container"].ancestors)
        toggle = self.full_ids["btn-display-settings"]
        self.assertIn("tree-toolbar", toggle.ancestors)
        self.assertEqual(toggle.attrs.get("aria-controls"), "tree-settings-panel")
        self.assertEqual(toggle.attrs.get("aria-expanded"), "false")

    def test_each_settings_tab_holds_its_controls(self):
        for tab, controls in self.SETTINGS_TABS.items():
            button = self.full_ids[f"tab-settings-{tab}"]
            section_id = button.attrs["aria-controls"]
            self.assertEqual(self.full_ids[section_id].attrs.get("data-settings-panel"), tab)
            for element_id in controls:
                self.assertIn(section_id, self.full_ids[element_id].ancestors, element_id)

    LAYOUT_CONTROLS = ("input-support-font", "input-tip-font", "tip-label-gap-value",
                       "btn-tip-label-gap-dec", "btn-tip-label-gap-inc", "btn-spacing-y-dec",
                       "btn-spacing-y-inc", "btn-spacing-x-dec", "btn-spacing-x-inc")

    def test_layout_controls_are_one_click_from_the_toolbar(self):
        # Fonts, tip label gap and spacing are used often, so they live in a toolbar
        # popover rather than behind the settings panel.
        button = self.full_ids["btn-layout-menu"]
        self.assertIn("tree-toolbar", button.ancestors)
        self.assertEqual(button.attrs.get("aria-controls"), "layout-menu")
        self.assertIn("hidden", self.full_ids["layout-menu"].attrs["class"].split())
        for element_id in self.LAYOUT_CONTROLS:
            self.assertIn("layout-menu", self.full_ids[element_id].ancestors, element_id)
        self.assertNotIn("tab-settings-layout", self.full_ids)
        controller = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("const SETTINGS_TABS = ['support', 'sequences', 'colors'];", controller)

    def test_threshold_labels_are_the_first_span_beside_each_input(self):
        # updateSupportUI() relabels these through input.parentElement.querySelector('span'),
        # e.g. "BS >" becomes "UFBoot >" for an IQ-TREE tree.
        html = render(**EVERYTHING)
        for element_id in ("input-pp-threshold", "input-bs-threshold"):
            self.assertRegex(
                html,
                r'<div class="settings-field">\s*<span>[^<]+</span>\s*<input type="number" id="'
                + element_id + '"')

    def test_identity_is_labelled_as_a_maximum(self):
        html = render(**EVERYTHING)
        self.assertIn("Identity (maximum)", html)
        self.assertIn('&le; <span id="identity-value">', html)
        self.assertIn('&ge; <span id="query-cover-value">', html)

    def test_only_the_viewer_widens_the_page(self):
        base = (TEMPLATES / "base_modern.html").read_text(encoding="utf-8")
        self.assertIn("{% block main_width %}max-w-7xl{% endblock %}", base)
        viewer = (TEMPLATES / "job_viewer.html").read_text(encoding="utf-8")
        self.assertIn("{% block main_width %}max-w-none{% endblock %}", viewer)

    def test_settings_panel_is_remembered_and_hidden_on_touch(self):
        controller = CONTROLLER.read_text(encoding="utf-8")
        self.assertIn("setSettingsPanelOpen(prefs.open === true", controller)
        self.assertIn("SETTINGS_PANEL_PREFS_KEY", controller)
        coarse = CSS.read_text(encoding="utf-8").split("@media (pointer: coarse) {", 1)[1]
        self.assertRegex(coarse, r"#tree-settings-panel\s*\{\s*display:\s*none;")


    VIEW_ROW = ("btn-layout-linear", "btn-layout-radial", "btn-zoom-out", "btn-zoom-in",
                "btn-fit", "btn-align-tips", "btn-ladderize", "btn-layout-menu",
                "rooting-mode-select", "btn-display-settings", "btn-fullscreen",
                "btn-tree-shortcuts-help")
    EDIT_ROW = ("btn-add-sequences-link", "btn-recompute", "btn-undo", "btn-prune",
                "btn-rename", "btn-deselect", "btn-set-soi", "btn-selection-more",
                "input-branch-filter")

    def test_toolbar_rows_separate_viewing_from_editing(self):
        for element_id in self.VIEW_ROW:
            self.assertIn("tree-toolbar-view", self.full_ids[element_id].ancestors, element_id)
        for element_id in self.EDIT_ROW:
            self.assertIn("tree-toolbar-edit", self.full_ids[element_id].ancestors, element_id)
        # The edit row starts with Add seq, directly under Linear/Radial.
        edit_ids = [el.id for el in self.full if el.id and "tree-toolbar-edit" in el.ancestors]
        self.assertEqual(edit_ids[0], "btn-add-sequences-link")

    def test_show_support_values_is_a_checked_setting_not_a_toolbar_button(self):
        self.assertNotIn("btn-toggle-support", self.full_ids)
        box = self.full_ids["cb-show-support"]
        self.assertEqual(box.attrs.get("type"), "checkbox")
        self.assertIn("checked", box.attrs)
        self.assertIn("settings-panel-support", box.ancestors)

    def test_sequence_filters_tab_is_named_for_what_it_does(self):
        html = render(**EVERYTHING)
        self.assertRegex(html, r'id="tab-settings-sequences"[^>]*>Sequence filters</button>')

    def test_zoom_buttons_read_as_magnification_not_spacing(self):
        html = render(**EVERYTHING)
        zoom = html.split('aria-label="Zoom"', 1)[1].split("</div>", 1)[0]
        self.assertIn("fa-search-minus", zoom)
        self.assertIn("fa-search-plus", zoom)

    def test_mobile_sheet_follows_the_desktop_groups(self):
        html = render(**EVERYTHING)
        sheet = html.split('id="tree-mobile-more"', 1)[1]
        headings = [h for h in ("<h4>Edit</h4>", "<h4>View</h4>", "<h4>Layout</h4>",
                                "<h4>Page and downloads</h4>")]
        positions = [sheet.index(h) for h in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="mobile-show-support"', sheet)
        self.assertIn('data-mobile-step="input-tip-font"', sheet)

    def test_viewer_disables_animated_relayouts(self):
        # Code that measures the tree right after an update (radial framing, the
        # annotation canvas) read phylotree's animated half-way sizes.
        viewer = (REPO / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js").read_text(encoding="utf-8")
        draw = viewer.split("        _draw() {", 1)[1].split("        _applySpacingViaAPI() {", 1)[0]
        self.assertIn("'transitions': false", draw)
        self.assertIn("this._frameRadialTree();", draw)


if __name__ == "__main__":
    unittest.main()
