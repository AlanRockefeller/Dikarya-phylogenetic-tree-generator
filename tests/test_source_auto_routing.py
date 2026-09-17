"""Moving an identifier to the right box must never cost the user the other one.

The Tree Builder reads whatever is typed into a source box and moves it to the
box that wants it: an iNaturalist URL pasted into the MycoMap field goes to the
iNaturalist field, because MycoMap would otherwise reject it with an error that
explains nothing.

Two ways that went wrong:

* The move assigned `target.value` unconditionally. If the destination already
  held something -- a URL the user had queued up -- it was silently destroyed,
  with no undo and no message. Moving one value is a convenience; losing the
  other is not a trade worth making.

* A result panel revealing itself switched to that panel unconditionally. An
  iNaturalist search can take tens of seconds, and the user moves on: they
  switch to Paste, start typing a FASTA, and the arriving result used to hide
  the textarea mid-keystroke.

The digit heuristic itself is deliberately kept. Across 9,816 iNaturalist jobs
on disk the smallest observation id is seven digits and not one is six or fewer,
while Mushroom Observer is still in the six-digit range, so a bare six-digit
number is Mushroom Observer and seven-plus is iNaturalist. A rare wrong guess
costs one click; the rule saves a support round trip on every right one.

The behaviour is browser-only, so these run the *shipped* script out of
sequence_entry.html through a node harness. See tests/js/source_auto_routing.test.js.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JS_DIR = Path(__file__).resolve().parent / "js"
TEMPLATE = REPO / "app" / "templates" / "sequence_entry.html"

# Anchored on code rather than comment prose, so re-wording a comment cannot
# quietly stop the test from extracting the real implementation.
START = "    (function setupSourceSelector() {"
END = "    // =========================================================================\n    // Workflow failure telemetry"
ACCESSION_START = "    function isGenBankAccession(text) {"
ACCESSION_END = "    function parseGenBankAccessions(text) {"


def extracted_script():
    html = TEMPLATE.read_text(encoding="utf-8")
    accession_start = html.index(ACCESSION_START)
    accession_helper = html[accession_start:html.index(ACCESSION_END, accession_start)]
    start = html.index(START)
    return accession_helper + html[start:html.index(END, start)]


def run_scenario(steps):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "extracted.js"
        path.write_text(extracted_script(), encoding="utf-8")
        proc = subprocess.run(
            [node, str(JS_DIR / "source_auto_routing.test.js"), str(path),
             json.dumps({"steps": steps})],
            capture_output=True, text=True,
        )
    if proc.returncode != 0:
        raise AssertionError(f"node harness failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


class AutoRoutingTests(unittest.TestCase):
    def test_an_inaturalist_label_is_not_routed_as_a_genbank_accession(self):
        result = run_scenario([
            {"set": "mycomap_url", "value": "INAT125467754", "event": "change"},
        ])
        self.assertEqual(result["fields"]["mycomap_url"], "INAT125467754")
        self.assertEqual(result["fields"]["sequence_text"], "")

    def test_a_large_scale_accession_is_routed_to_paste(self):
        result = run_scenario([
            {"set": "mycomap_url", "value": "ABCD010000001", "event": "change"},
        ])
        self.assertEqual(result["fields"]["mycomap_url"], "")
        self.assertEqual(result["fields"]["sequence_text"], "ABCD010000001")
        self.assertEqual(result["selected"], "paste")

    def test_an_inaturalist_url_typed_into_mycomap_is_moved(self):
        result = run_scenario([
            {"set": "mycomap_url",
             "value": "https://www.inaturalist.org/observations/280384724",
             "event": "change"},
        ])
        self.assertEqual(result["fields"]["mycomap_url"], "")
        self.assertEqual(result["fields"]["inaturalist_url"],
                         "https://www.inaturalist.org/observations/280384724")
        self.assertEqual(result["selected"], "inaturalist")

    def test_a_six_digit_number_still_routes_to_mushroom_observer(self):
        result = run_scenario([
            {"set": "inaturalist_url", "value": "123456", "event": "change"},
        ])
        self.assertEqual(result["fields"]["mushroom_observer_input"], "123456")
        self.assertEqual(result["fields"]["inaturalist_url"], "")
        self.assertEqual(result["selected"], "mushroom_observer")

    def test_a_nine_digit_number_still_routes_to_inaturalist(self):
        result = run_scenario([
            {"set": "mushroom_observer_input", "value": "280384724",
             "event": "change"},
        ])
        self.assertEqual(result["fields"]["inaturalist_url"], "280384724")
        self.assertEqual(result["fields"]["mushroom_observer_input"], "")

    def test_a_short_number_is_left_where_it_was_typed(self):
        result = run_scenario([
            {"set": "inaturalist_url", "value": "1234", "event": "change"},
        ])
        self.assertEqual(result["fields"]["inaturalist_url"], "1234")
        self.assertEqual(result["fields"]["mushroom_observer_input"], "")


class NonDestructiveRoutingTests(unittest.TestCase):
    def test_an_occupied_destination_is_never_overwritten(self):
        result = run_scenario([
            {"set": "inaturalist_url", "value": "https://www.inaturalist.org/observations/111111111"},
            {"set": "mycomap_url",
             "value": "https://www.inaturalist.org/observations/280384724",
             "event": "change"},
        ])
        # Both survive.
        self.assertEqual(result["fields"]["inaturalist_url"],
                         "https://www.inaturalist.org/observations/111111111")
        self.assertEqual(result["fields"]["mycomap_url"],
                         "https://www.inaturalist.org/observations/280384724")

    def test_the_user_is_told_why_nothing_moved(self):
        result = run_scenario([
            {"set": "inaturalist_url", "value": "999888777"},
            {"set": "mycomap_url", "value": "280384724", "event": "change"},
        ])
        self.assertTrue(result["status"])
        message, _level = result["status"][-1]
        self.assertIn("already has something in it", message)
        self.assertIn("both entries are still there", message.lower())

    def test_the_destination_tab_is_still_revealed(self):
        # The user has to be able to see the conflict to resolve it.
        result = run_scenario([
            {"set": "inaturalist_url", "value": "999888777"},
            {"set": "mycomap_url", "value": "280384724", "event": "change"},
        ])
        self.assertEqual(result["selected"], "inaturalist")

    def test_whitespace_alone_does_not_count_as_occupied(self):
        result = run_scenario([
            {"set": "inaturalist_url", "value": "   "},
            {"set": "mycomap_url", "value": "280384724", "event": "change"},
        ])
        self.assertEqual(result["fields"]["inaturalist_url"], "280384724")
        self.assertEqual(result["fields"]["mycomap_url"], "")


class AsyncResultFocusTests(unittest.TestCase):
    def test_a_first_result_on_an_untouched_page_still_switches(self):
        result = run_scenario([{"reveal": "mycomap"}])
        self.assertEqual(result["selected"], "mycomap")
        self.assertEqual(result["badges"], [])

    def test_a_result_does_not_steal_the_panel_the_user_is_typing_in(self):
        result = run_scenario([
            {"clickTab": "paste"},
            {"typeIn": "sequence_text", "value": ">seq1\nACGTACGTACGT"},
            {"reveal": "inaturalist"},
        ])
        self.assertEqual(result["selected"], "paste")
        self.assertEqual(result["fields"]["sequence_text"], ">seq1\nACGTACGTACGT")

    def test_the_finished_source_is_marked_instead(self):
        result = run_scenario([
            {"clickTab": "paste"},
            {"typeIn": "sequence_text", "value": ">seq1\nACGT"},
            {"reveal": "inaturalist"},
        ])
        self.assertEqual(result["badges"], ["inaturalist"])

    def test_an_explicit_tab_choice_alone_is_enough_to_stop_the_switch(self):
        result = run_scenario([
            {"clickTab": "blast"},
            {"reveal": "inaturalist"},
        ])
        self.assertEqual(result["selected"], "blast")
        self.assertEqual(result["badges"], ["inaturalist"])

    def test_opening_the_marked_tab_clears_its_badge(self):
        result = run_scenario([
            {"clickTab": "paste"},
            {"reveal": "inaturalist"},
            {"clickTab": "inaturalist"},
        ])
        self.assertEqual(result["selected"], "inaturalist")
        self.assertEqual(result["badges"], [])

    def test_a_still_hidden_result_does_nothing(self):
        result = run_scenario([{"clickTab": "paste"}])
        self.assertEqual(result["selected"], "paste")
        self.assertEqual(result["badges"], [])


if __name__ == "__main__":
    unittest.main()
