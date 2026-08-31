"""Focused regressions for the coarse-pointer tree-viewer interaction layer."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "js" / "mobile_touch_interactions.test.js"


class MobileTreeViewerTests(unittest.TestCase):
    def test_touch_interaction_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        proc = subprocess.run(
            [node, str(HARNESS), str(REPO)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS mobile touch interactions", proc.stdout)

    def test_mobile_toolbar_and_explicit_spacing_controls_ship(self):
        template = (REPO / "app/templates/job_viewer.html").read_text()
        for element_id in (
            "tree-mobile-toolbar",
            "btn-mobile-select",
            "btn-mobile-more",
            "btn-mobile-expand",
            "btn-mobile-node-actions",
        ):
            self.assertIn(f'id="{element_id}"', template)
        for desktop_control in (
            "btn-spacing-x-dec",
            "btn-spacing-x-inc",
            "btn-spacing-y-dec",
            "btn-spacing-y-inc",
            "btn-tip-label-gap-dec",
            "btn-tip-label-gap-inc",
        ):
            self.assertIn(f'data-mobile-trigger="{desktop_control}"', template)

    def test_mobile_layout_depends_on_pointer_capability(self):
        css = (REPO / "app/static/css/tree_viewer.css").read_text()
        self.assertIn("@media (pointer: coarse)", css)
        self.assertIn("min-height: 76dvh !important", css)
        self.assertIn("body.tree-expanded #tree-viewer-panel", css)
        self.assertIn("min-width: 100%", css)
        self.assertIn("min-height: 100%", css)
        self.assertNotIn("@media (max-width:", css)

    def test_changed_mobile_assets_have_current_cache_versions(self):
        template = (REPO / "app/templates/job_viewer.html").read_text()
        minimum_versions = {
            "css/tree_viewer.css": 11,
            "js/phylotree.js": 12,
            "js/tree_viewer_phylotree_v2.js": 80,
            "js/tree_viewer_controller.js": 75,
        }
        for asset, minimum in minimum_versions.items():
            match = re.search(
                rf"filename='{re.escape(asset)}'\) \}}\}}\?v=(\d+)", template
            )
            self.assertIsNotNone(match, f"{asset} has no cache version")
            self.assertGreaterEqual(int(match.group(1)), minimum, asset)

    def test_mobile_cleanup_paths_restore_body_and_aria_state(self):
        controller = (REPO / "app/static/js/tree_viewer_controller.js").read_text()
        cleanup = controller.split("function cleanupMobileViewerUI() {", 1)[1].split("\n    }", 1)[0]
        for expected in (
            "setMobileMoreOpen(false)",
            "setExpandedTree(false)",
            "setMobileInteractionMode('navigate')",
            "viewer?.closeMobileNodeActions()",
            "'tree-mobile-node-menu-open', 'mobile-show-desktop-controls'",
        ):
            self.assertIn(expected, cleanup)
        self.assertIn("window.addEventListener('pagehide', cleanupMobileViewerUI)", controller)
        self.assertIn("setMobileMoreOpen(false);\n        document.body.classList.toggle('tree-expanded', open)", controller)

    def test_camera_controls_do_not_route_through_spacing(self):
        controller = (REPO / "app/static/js/tree_viewer_controller.js").read_text()
        zoom = controller.split("function triggerZoom(delta) {", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("updateSpacing", zoom)
        viewer = (REPO / "app/static/js/tree_viewer_phylotree_v2.js").read_text()
        touch = viewer.split("_handleTouchPointerDown(event) {", 1)[1].split(
            "// Alan 5/11/26 - Start box-select", 1
        )[0]
        self.assertNotIn("updateSpacing", touch)
        self.assertNotIn("localStorage", touch)

    def test_zoom_handler_does_not_mutate_authoritative_d3_transform(self):
        phylotree = (REPO / "app/static/js/phylotree.js").read_text()
        zoom_handler = phylotree.split('.on("zoom", (event) => {', 1)[1].split(
            "this.svg.call(zoom$1)", 1
        )[0]
        self.assertNotIn("toTransform = event.transform", zoom_handler)
        self.assertNotRegex(zoom_handler, r"event\.transform\.(?:x|y|k)\s*[-+*/]?=")
        self.assertIn("event.transform.y - 10", zoom_handler)
        self.assertIn("this.zoom_behavior.scaleBy", phylotree)

    def test_button_zoom_uses_shared_camera_api(self):
        controller = (REPO / "app/static/js/tree_viewer_controller.js").read_text()
        zoom = controller.split("function triggerZoom(delta) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("viewer?.zoomCamera(factor)", zoom)
        self.assertNotIn("WheelEvent", zoom)
        self.assertNotIn("updateSpacing", zoom)

    def test_mobile_menu_taps_are_not_suppressed_as_tree_gestures(self):
        viewer = (REPO / "app/static/js/tree_viewer_phylotree_v2.js").read_text()
        pointer_down = viewer.split("_handleTouchPointerDown(event) {", 1)[1].split(
            "_handleTouchPointerMove(event) {", 1
        )[0]
        self.assertIn(".phylotree-context-menu", pointer_down)
        self.assertLess(
            pointer_down.index(".phylotree-context-menu"),
            pointer_down.index("mobileTouchState.pointers.set"),
        )

    def test_fit_is_still_the_deferred_redraw_implementation(self):
        viewer = (REPO / "app/static/js/tree_viewer_phylotree_v2.js").read_text()
        fit = viewer.split("fitToView() {", 2)[2].split("}", 1)[0]
        self.assertIn("this._draw();", fit)
        self.assertNotIn("getBBox", fit)
        self.assertNotIn("fit_to_view", viewer)


if __name__ == "__main__":
    unittest.main()
