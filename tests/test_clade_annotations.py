"""Focused regression coverage for layered clade annotations.

Covers the state/validation half of the feature, which is where every security
and correctness guarantee actually lives:

  * old tree state without annotation keys still loads and is not rewritten
  * a valid configuration round-trips, with order normalized so 1 == innermost
  * every documented rejection (font size / colour / font name / unknown layer /
    duplicate ids / oversized label / unresolvable member / ambiguous member)
  * membership cleanup on pruning and on recompute
  * annotation saves preserve unrelated tree-state keys
  * labels are stored verbatim as text, never interpreted as markup
  * an incomplete payload is refused instead of silently clearing what it omits
  * non-finite numbers are a 400-style validation error, never a 500

The final class drives the viewer's own annotation-resolution code under Node to
pin down the rule that decides what may be drawn: only a member set that is
still exactly one clade gets a bracket.
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.tree_annotation_service import (  # noqa: E402
    ANNOTATION_LAYERS_KEY,
    CLADE_ANNOTATIONS_KEY,
    MAX_LABEL_LENGTH,
    MAX_LABEL_LINES,
    MAX_HIGHLIGHT_SLOT,
    MAX_LAYERS,
    AnnotationValidationError,
    apply_annotation_config,
    build_leaf_identity_map,
    get_annotation_config,
    normalize_annotation_config,
    remove_pruned_members_from_annotations,
    restrict_annotations_to_current_leaves,
)
from app.services.tree_edit_service import (  # noqa: E402
    HAS_BIOPYTHON,
    load_tree_state,
    prune_taxa,
    save_tree_state,
)


def _state_with_tips(names):
    """Minimal tree_state whose tree_structure has one leaf per name."""
    return {
        "tree_structure": {
            "name": None,
            "original_name": None,
            "children": [{"name": n, "original_name": n} for n in names],
        }
    }


def _layer(layer_id="layer_a", name="Sections", order=1, **overrides):
    layer = {"id": layer_id, "name": name, "order": order, "visible": True}
    layer.update(overrides)
    return layer


def _annotation(annotation_id="annotation_a", layer_id="layer_a",
                label="Section X", members=("A", "B"), **overrides):
    annotation = {
        "id": annotation_id,
        "layer_id": layer_id,
        "label": label,
        "member_tip_ids": list(members),
    }
    annotation.update(overrides)
    return annotation


class LeafIdentityTests(unittest.TestCase):
    def test_canonical_identity_prefers_original_name_over_display_name(self):
        state = {
            "tree_structure": {
                "children": [
                    {"name": "Renamed label", "original_name": "A"},
                    {"name": "B", "original_name": "B"},
                ]
            }
        }
        resolvable, ambiguous = build_leaf_identity_map(state)
        # The renamed tip is still addressable by its original name only, which is
        # what keeps annotation membership intact across a display rename.
        self.assertEqual(resolvable, {"A", "B"})
        self.assertEqual(ambiguous, set())

    def test_duplicate_canonical_names_are_reported_as_ambiguous(self):
        resolvable, ambiguous = build_leaf_identity_map(_state_with_tips(["A", "A", "B"]))
        self.assertEqual(resolvable, {"B"})
        self.assertEqual(ambiguous, {"A"})


class BackwardCompatibilityTests(unittest.TestCase):
    def test_state_without_annotation_keys_reads_as_empty(self):
        config = get_annotation_config({"pruned_taxa": [], "renames": {}})
        self.assertEqual(config[ANNOTATION_LAYERS_KEY], [])
        self.assertEqual(config[CLADE_ANNOTATIONS_KEY], [])

    def test_reading_annotations_does_not_add_keys_to_state(self):
        state = {"renames": {"A": "Alpha"}}
        get_annotation_config(state)
        self.assertEqual(state, {"renames": {"A": "Alpha"}})


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.state = _state_with_tips(["A", "B", "C", "D"])

    def test_valid_configuration_round_trips(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer()],
            "annotations": [_annotation()],
        })
        self.assertEqual(len(config[ANNOTATION_LAYERS_KEY]), 1)
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(annotation["member_tip_ids"], ["A", "B"])
        # Unset style fields stay null so they keep inheriting from the layer.
        self.assertIsNone(annotation["font_size"])
        self.assertIsNone(annotation["text_color"])
        self.assertEqual(annotation["annotation_type"], "clade_line")
        self.assertIsNone(annotation["fill_color"])
        self.assertIsNone(annotation["fill_opacity"])

    def test_order_one_is_innermost_and_order_is_normalized(self):
        config = normalize_annotation_config(self.state, {
            "layers": [
                _layer("layer_outer", "Subgenera", order=90),
                _layer("layer_inner", "Subsections", order=3),
                _layer("layer_mid", "Sections", order=7),
            ],
            "annotations": [],
        })
        ordered = [(layer["order"], layer["id"]) for layer in config[ANNOTATION_LAYERS_KEY]]
        self.assertEqual(
            ordered, [(1, "layer_inner"), (2, "layer_mid"), (3, "layer_outer")]
        )

    def test_layer_defaults_are_filled_in_when_absent(self):
        config = normalize_annotation_config(self.state, {
            "layers": [{"id": "layer_a", "name": "Sections"}],
            "annotations": [],
        })
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["order"], 1)
        self.assertTrue(layer["visible"])
        self.assertEqual(layer["default_font_family"], "Arial")
        self.assertEqual(layer["default_fill_color"], "#ffffff")
        self.assertEqual(layer["default_fill_opacity"], 0.9)

    def test_all_three_annotation_types_round_trip(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer()],
            "annotations": [
                _annotation("clade", members=["A", "B"], annotation_type="clade_line"),
                _annotation("text", members=["C"], annotation_type="branch_text"),
                _annotation("bubble", members=["D"], annotation_type="branch_bubble"),
            ],
        })
        self.assertEqual(
            [item["annotation_type"] for item in config[CLADE_ANNOTATIONS_KEY]],
            ["clade_line", "branch_text", "branch_bubble"],
        )

    def test_old_type_aliases_normalize_only_on_explicit_save(self):
        old = _annotation(annotation_type="line")
        state = dict(self.state, **{ANNOTATION_LAYERS_KEY: [_layer()], CLADE_ANNOTATIONS_KEY: [old]})
        self.assertNotIn("fill_color", get_annotation_config(state)[CLADE_ANNOTATIONS_KEY][0])
        config = normalize_annotation_config(state, {"layers": [_layer()], "annotations": [old]})
        self.assertEqual(config[CLADE_ANNOTATIONS_KEY][0]["annotation_type"], "clade_line")

    def test_duplicate_member_ids_are_deduplicated(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer()],
            "annotations": [_annotation(members=["A", "B", "A"])],
        })
        self.assertEqual(config[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "B"])

    def test_same_layer_nested_annotations_are_accepted(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer()],
            "annotations": [
                _annotation("annotation_broad", label="Broad clade",
                            members=["A", "B", "C", "D"]),
                _annotation("annotation_narrow", label="Subgroup", members=["B", "C"]),
            ],
        })
        self.assertEqual(len(config[CLADE_ANNOTATIONS_KEY]), 2)

    def test_hidden_layers_are_preserved(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer(visible=False)],
            "annotations": [_annotation()],
        })
        self.assertFalse(config[ANNOTATION_LAYERS_KEY][0]["visible"])
        self.assertEqual(len(config[CLADE_ANNOTATIONS_KEY]), 1)

    def test_label_is_stored_as_plain_text(self):
        payload = "<svg onload=alert(1)>"
        config = normalize_annotation_config(self.state, {
            "layers": [_layer()],
            "annotations": [_annotation(label=payload)],
        })
        # Stored byte-for-byte; the renderer puts it through D3's .text(), so it
        # can only ever appear as literal characters.
        self.assertEqual(config[CLADE_ANNOTATIONS_KEY][0]["label"], payload)


class CladeHighlightTests(unittest.TestCase):
    """The fourth annotation style: a translucent band painted behind a clade.

    It reuses the clade-membership identity and the layer/annotation inheritance
    model wholesale, so what is worth pinning here is the part that is genuinely
    new: its OWN fill fields. Sharing ``fill_color``/``fill_opacity`` with branch
    bubbles would have given every highlight the bubble default of near-opaque
    white and hidden the tree it is supposed to sit behind.
    """

    def setUp(self):
        self.state = _state_with_tips(["A", "B", "C", "D"])

    def _normalize(self, annotations, layers=None):
        return normalize_annotation_config(self.state, {
            "layers": layers if layers is not None else [_layer()],
            "annotations": annotations,
        })

    def test_a_highlight_is_accepted_and_serialized(self):
        config = self._normalize([
            _annotation(members=["A", "B"], annotation_type="clade_highlight")
        ])
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(annotation["annotation_type"], "clade_highlight")
        self.assertEqual(annotation["member_tip_ids"], ["A", "B"])
        # The whole configuration has to survive json.dumps: it is written to
        # tree_state.json verbatim.
        self.assertEqual(json.loads(json.dumps(config)), config)

    def test_it_round_trips_through_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            config = self._normalize([_annotation(
                members=["A", "B"], annotation_type="clade_highlight",
                highlight_color="#B91C1C", highlight_opacity=0.4,
            )])
            state = apply_annotation_config(dict(self.state), config)
            save_tree_state(job_dir, state)

            reloaded = load_tree_state(job_dir)

        stored = reloaded[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(stored["annotation_type"], "clade_highlight")
        self.assertEqual(stored["highlight_color"], "#b91c1c")
        self.assertEqual(stored["highlight_opacity"], 0.4)
        self.assertEqual(stored["member_tip_ids"], ["A", "B"])
        # And normalizing what came back off disk is a fixed point.
        again = normalize_annotation_config(reloaded, {
            "layers": reloaded[ANNOTATION_LAYERS_KEY],
            "annotations": reloaded[CLADE_ANNOTATIONS_KEY],
        })
        self.assertEqual(again[CLADE_ANNOTATIONS_KEY], config[CLADE_ANNOTATIONS_KEY])

    def test_unset_highlight_style_stays_null_and_keeps_inheriting(self):
        config = self._normalize([
            _annotation(members=["A", "B"], annotation_type="clade_highlight")
        ])
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertIsNone(annotation["highlight_color"])
        self.assertIsNone(annotation["highlight_opacity"])

    def test_layer_defaults_are_the_subtle_gold_tint(self):
        config = self._normalize([], layers=[{"id": "layer_a", "name": "Sections"}])
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["default_highlight_color"], "#c9a962")
        # The opacity is deliberately left absent: it is the one layer style the
        # server does not backfill, so "nobody chose one" stays distinguishable
        # from a user who typed the shared default.
        self.assertIsNone(layer["default_highlight_opacity"])

    def test_highlight_defaults_are_independent_of_branch_bubble_fill(self):
        """The regression this data model exists to prevent.

        A layer whose bubbles are opaque white must not hand that to its
        highlights, or every band would paint the tree out.
        """
        config = self._normalize(
            [_annotation(members=["A", "B"], annotation_type="clade_highlight")],
            layers=[_layer(default_fill_color="#ffffff", default_fill_opacity=1.0)],
        )
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["default_fill_opacity"], 1.0)
        self.assertEqual(layer["default_highlight_color"], "#c9a962")
        self.assertIsNone(layer["default_highlight_opacity"])
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        # Nothing was copied down into the annotation either, so a later layer
        # edit still restyles it.
        self.assertIsNone(annotation["highlight_color"])
        self.assertIsNone(annotation["highlight_opacity"])

    def test_explicit_colour_and_opacity_survive_normalization(self):
        config = self._normalize([_annotation(
            members=["A", "B"], annotation_type="clade_highlight",
            highlight_color="#1F6FEB", highlight_opacity=0,
        )])
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        # Normalized to lowercase hex, exactly like every other annotation colour.
        self.assertEqual(annotation["highlight_color"], "#1f6feb")
        # Zero is a legitimate opacity and must not be read as "unset".
        self.assertEqual(annotation["highlight_opacity"], 0)

    def test_invalid_highlight_opacity_is_rejected(self):
        for bad in (-0.1, 1.5, "0.5", True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(AnnotationValidationError):
                    self._normalize([_annotation(
                        members=["A", "B"], annotation_type="clade_highlight",
                        highlight_opacity=bad,
                    )])

    def test_invalid_highlight_colour_is_rejected(self):
        # The same rule as every other annotation colour: a normalized #RRGGBB
        # literal only, so nothing can smuggle a style fragment into the SVG.
        for bad in ("red", "#fff", "rgb(1,2,3)", "url(#x)", "var(--x)",
                    "#12345g", 16711680, "#ffffff;stroke:red"):
            with self.subTest(bad=bad):
                with self.assertRaises(AnnotationValidationError):
                    self._normalize([_annotation(
                        members=["A", "B"], annotation_type="clade_highlight",
                        highlight_color=bad,
                    )])

    def test_invalid_layer_highlight_defaults_are_rejected(self):
        with self.assertRaises(AnnotationValidationError):
            self._normalize([], layers=[_layer(default_highlight_color="chartreuse")])
        with self.assertRaises(AnnotationValidationError):
            self._normalize([], layers=[_layer(default_highlight_opacity=2)])

    def test_annotations_saved_before_highlights_existed_still_load(self):
        """An old tree_state has neither field; nothing may crash or be invented."""
        old = {
            "id": "annotation_old",
            "layer_id": "layer_a",
            "label": "Section X",
            "annotation_type": "clade_line",
            "member_tip_ids": ["A", "B"],
            "font_size": None,
            "text_color": "#1f2937",
        }
        old_layer = {"id": "layer_a", "name": "Sections", "order": 1, "visible": True}
        state = dict(self.state, **{
            ANNOTATION_LAYERS_KEY: [old_layer], CLADE_ANNOTATIONS_KEY: [old],
        })

        # Read back untouched ...
        stored = get_annotation_config(state)[CLADE_ANNOTATIONS_KEY][0]
        self.assertNotIn("highlight_color", stored)

        # ... and the next explicit save fills the keys in as "inherit".
        config = normalize_annotation_config(
            state, {"layers": [old_layer], "annotations": [old]}
        )
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(annotation["annotation_type"], "clade_line")
        self.assertEqual(annotation["text_color"], "#1f2937")
        self.assertIsNone(annotation["highlight_color"])
        self.assertIsNone(annotation["highlight_opacity"])
        self.assertIsNone(
            config[ANNOTATION_LAYERS_KEY][0]["default_highlight_opacity"]
        )

    def test_existing_types_are_unaffected_by_the_new_fields(self):
        config = self._normalize([
            _annotation("clade", members=["A", "B"], annotation_type="clade_line"),
            _annotation("text", members=["C"], annotation_type="branch_text"),
            _annotation("bubble", members=["D"], annotation_type="branch_bubble",
                        fill_color="#ffffff", fill_opacity=0.9),
        ])
        types = [item["annotation_type"] for item in config[CLADE_ANNOTATIONS_KEY]]
        self.assertEqual(types, ["clade_line", "branch_text", "branch_bubble"])
        bubble = config[CLADE_ANNOTATIONS_KEY][2]
        self.assertEqual(bubble["fill_color"], "#ffffff")
        self.assertEqual(bubble["fill_opacity"], 0.9)
        # A bubble does not acquire a highlight, and a highlight does not acquire
        # a bubble fill.
        self.assertIsNone(bubble["highlight_color"])

    def test_membership_not_the_type_is_the_identity(self):
        """Switching an existing annotation to a highlight must not touch its tips."""
        members = ["B", "A"]
        as_line = self._normalize([_annotation(
            "annotation_a", members=members, annotation_type="clade_line")])
        as_highlight = self._normalize([_annotation(
            "annotation_a", members=members, annotation_type="clade_highlight")])
        self.assertEqual(
            as_line[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"],
            as_highlight[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"],
        )
        self.assertEqual(as_highlight[CLADE_ANNOTATIONS_KEY][0]["id"], "annotation_a")

    def test_nested_highlights_are_accepted_and_keep_distinct_identities(self):
        config = self._normalize([
            _annotation("annotation_outer", label="Section X",
                        members=["A", "B", "C", "D"], annotation_type="clade_highlight"),
            _annotation("annotation_inner", label="Subsection Y",
                        members=["A", "B"], annotation_type="clade_highlight"),
        ])
        stored = config[CLADE_ANNOTATIONS_KEY]
        self.assertEqual([item["id"] for item in stored],
                         ["annotation_outer", "annotation_inner"])
        self.assertEqual(stored[0]["member_tip_ids"], ["A", "B", "C", "D"])
        self.assertEqual(stored[1]["member_tip_ids"], ["A", "B"])

    def test_editing_a_highlight_replaces_it_rather_than_duplicating_it(self):
        """The editor sends the WHOLE configuration; a same-id edit must not stack.

        The duplicate-annotation regression this guards was on the clade-line
        path, and a highlight goes through exactly the same save, so it is worth
        pinning that a re-save of the same id stays one annotation.
        """
        first = self._normalize([_annotation(
            "annotation_a", label="Section X", members=["A", "B"],
            annotation_type="clade_highlight")])
        edited = dict(first[CLADE_ANNOTATIONS_KEY][0])
        edited["label"] = "Section X (revised)"
        edited["highlight_opacity"] = 0.3

        second = normalize_annotation_config(self.state, {
            "layers": first[ANNOTATION_LAYERS_KEY],
            "annotations": [edited],
        })
        self.assertEqual(len(second[CLADE_ANNOTATIONS_KEY]), 1)
        self.assertEqual(second[CLADE_ANNOTATIONS_KEY][0]["label"], "Section X (revised)")
        self.assertEqual(second[CLADE_ANNOTATIONS_KEY][0]["highlight_opacity"], 0.3)

        # Two annotations sharing one id is a client bug and stays a 400 rather
        # than silently persisting a duplicate.
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(self.state, {
                "layers": first[ANNOTATION_LAYERS_KEY],
                "annotations": [edited, edited],
            })

    def test_pruning_a_member_narrows_a_highlight_without_deleting_it(self):
        state = dict(self.state, **{
            ANNOTATION_LAYERS_KEY: [_layer()],
            CLADE_ANNOTATIONS_KEY: [_annotation(
                members=["A", "B", "C"], annotation_type="clade_highlight")],
        })
        removed = remove_pruned_members_from_annotations(state, {"C"})
        self.assertEqual(removed, 0)
        self.assertEqual(
            state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "B"]
        )
        self.assertEqual(
            state[CLADE_ANNOTATIONS_KEY][0]["annotation_type"], "clade_highlight"
        )


class HighlightColorModePersistenceTests(unittest.TestCase):
    """The stored form of Auto-vs-Fixed and of the pinned palette slot.

    Both fields are optional additions to ``tree_state.json``. Nothing existing
    carries them, so the normalizer has to give old state the behaviour it had
    before they existed while letting new state say what it means outright.
    """

    def setUp(self):
        self.state = _state_with_tips(["A", "B", "C", "D"])
        self.gold = "#c9a962"

    def _normalize(self, annotations, layers=None):
        return normalize_annotation_config(self.state, {
            "layers": layers if layers is not None else [_layer()],
            "annotations": annotations,
        })

    def _highlight(self, **overrides):
        return _annotation(members=["A", "B"],
                           annotation_type="clade_highlight", **overrides)

    def test_a_new_layer_defaults_to_automatic(self):
        config = self._normalize([], layers=[{"id": "layer_a", "name": "Sections"}])
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["default_highlight_color_mode"], "auto")
        self.assertEqual(layer["default_highlight_color"], self.gold)

    def test_a_legacy_gold_layer_is_normalized_to_automatic(self):
        config = self._normalize([], layers=[_layer(default_highlight_color=self.gold)])
        self.assertEqual(
            config[ANNOTATION_LAYERS_KEY][0]["default_highlight_color_mode"], "auto"
        )

    def test_a_legacy_layer_with_a_chosen_colour_is_normalized_to_fixed(self):
        """Anything other than the shared default was a deliberate pick."""
        config = self._normalize([], layers=[_layer(default_highlight_color="#334455")])
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["default_highlight_color_mode"], "fixed")
        self.assertEqual(layer["default_highlight_color"], "#334455")

    def test_an_explicit_mode_is_never_overridden_by_the_inference(self):
        # The case the sentinel made impossible: gold, chosen on purpose.
        config = self._normalize([], layers=[_layer(
            default_highlight_color=self.gold,
            default_highlight_color_mode="fixed",
        )])
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["default_highlight_color_mode"], "fixed")
        self.assertEqual(layer["default_highlight_color"], self.gold)

    def test_a_layer_may_keep_a_colour_while_saying_automatic(self):
        """The control keeps the last fixed colour so switching back restores it."""
        config = self._normalize([], layers=[_layer(
            default_highlight_color="#334455",
            default_highlight_color_mode="auto",
        )])
        layer = config[ANNOTATION_LAYERS_KEY][0]
        self.assertEqual(layer["default_highlight_color_mode"], "auto")
        self.assertEqual(layer["default_highlight_color"], "#334455")

    def test_an_annotation_mode_is_null_when_it_inherits(self):
        config = self._normalize([self._highlight()])
        self.assertIsNone(config[CLADE_ANNOTATIONS_KEY][0]["highlight_color_mode"])

    def test_annotation_modes_round_trip(self):
        for mode in ("auto", "fixed"):
            with self.subTest(mode=mode):
                config = self._normalize([self._highlight(highlight_color_mode=mode)])
                self.assertEqual(
                    config[CLADE_ANNOTATIONS_KEY][0]["highlight_color_mode"], mode
                )

    def test_an_auto_to_fixed_to_auto_round_trip_returns_to_automatic(self):
        auto = self._normalize([self._highlight(highlight_color_mode="auto")])
        annotation = dict(auto[CLADE_ANNOTATIONS_KEY][0])

        annotation["highlight_color_mode"] = "fixed"
        annotation["highlight_color"] = self.gold
        fixed = self._normalize([annotation])
        stored = fixed[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(stored["highlight_color_mode"], "fixed")
        self.assertEqual(stored["highlight_color"], self.gold)

        annotation = dict(stored)
        annotation["highlight_color_mode"] = "auto"
        annotation["highlight_color"] = None
        back = self._normalize([annotation])[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(back["highlight_color_mode"], "auto")
        self.assertIsNone(back["highlight_color"])
        # Still one annotation, same id: editing never duplicates.
        self.assertEqual(back["id"], "annotation_a")

    def test_an_invalid_mode_is_rejected(self):
        for bad in ("automatic", "off", "", 1, True, ["auto"]):
            with self.subTest(bad=bad):
                with self.assertRaises(AnnotationValidationError):
                    self._normalize([self._highlight(highlight_color_mode=bad)])

    def test_case_and_padding_are_normalized_like_every_other_choice_field(self):
        config = self._normalize([self._highlight(highlight_color_mode=" AUTO ")])
        self.assertEqual(
            config[CLADE_ANNOTATIONS_KEY][0]["highlight_color_mode"], "auto"
        )

    def test_an_invalid_layer_mode_is_rejected(self):
        with self.assertRaises(AnnotationValidationError):
            self._normalize([], layers=[_layer(default_highlight_color_mode="sometimes")])

    def test_the_palette_slot_round_trips(self):
        config = self._normalize([self._highlight(automatic_highlight_slot=3)])
        self.assertEqual(
            config[CLADE_ANNOTATIONS_KEY][0]["automatic_highlight_slot"], 3
        )

    def test_a_missing_slot_stays_null(self):
        config = self._normalize([self._highlight()])
        self.assertIsNone(
            config[CLADE_ANNOTATIONS_KEY][0]["automatic_highlight_slot"]
        )

    def test_slot_zero_is_kept_rather_than_read_as_unset(self):
        config = self._normalize([self._highlight(automatic_highlight_slot=0)])
        self.assertEqual(
            config[CLADE_ANNOTATIONS_KEY][0]["automatic_highlight_slot"], 0
        )

    def test_an_invalid_slot_is_rejected(self):
        for bad in (-1, 1.5, "2", True, MAX_HIGHLIGHT_SLOT + 1,
                    float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(AnnotationValidationError):
                    self._normalize([self._highlight(automatic_highlight_slot=bad)])

    def test_a_pinned_slot_does_not_make_the_annotation_fixed(self):
        """It is a cache of the automatic decision, not a colour the user chose."""
        config = self._normalize([self._highlight(automatic_highlight_slot=2)])
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(annotation["automatic_highlight_slot"], 2)
        self.assertIsNone(annotation["highlight_color"])
        self.assertIsNone(annotation["highlight_color_mode"])

    def test_state_saved_before_either_field_existed_still_loads(self):
        old_layer = {"id": "layer_a", "name": "Sections", "order": 1, "visible": True,
                     "default_highlight_color": self.gold,
                     "default_highlight_opacity": 0.15}
        old = {
            "id": "annotation_old", "layer_id": "layer_a", "label": "Section X",
            "annotation_type": "clade_highlight", "member_tip_ids": ["A", "B"],
            "highlight_color": None, "highlight_opacity": None,
        }
        state = dict(self.state, **{
            ANNOTATION_LAYERS_KEY: [old_layer], CLADE_ANNOTATIONS_KEY: [old],
        })
        # Read back untouched ...
        stored = get_annotation_config(state)[CLADE_ANNOTATIONS_KEY][0]
        self.assertNotIn("automatic_highlight_slot", stored)
        self.assertNotIn("highlight_color_mode", stored)
        # ... and the next explicit save fills both in with the old behaviour.
        config = normalize_annotation_config(
            state, {"layers": [old_layer], "annotations": [old]}
        )
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertIsNone(annotation["highlight_color_mode"])
        self.assertIsNone(annotation["automatic_highlight_slot"])
        self.assertEqual(
            config[ANNOTATION_LAYERS_KEY][0]["default_highlight_color_mode"], "auto"
        )

    def test_the_whole_configuration_still_survives_json(self):
        config = self._normalize([self._highlight(
            highlight_color_mode="fixed", highlight_color=self.gold,
            automatic_highlight_slot=1,
        )])
        self.assertEqual(json.loads(json.dumps(config)), config)

    def test_other_annotation_types_gain_the_keys_without_changing_behaviour(self):
        config = self._normalize([
            _annotation("line", members=["A", "B"], annotation_type="clade_line"),
            _annotation("bubble", members=["C"], annotation_type="branch_bubble",
                        fill_color="#ffffff", fill_opacity=0.9),
        ])
        line, bubble = config[CLADE_ANNOTATIONS_KEY]
        self.assertEqual(line["annotation_type"], "clade_line")
        self.assertIsNone(line["highlight_color_mode"])
        self.assertIsNone(line["automatic_highlight_slot"])
        self.assertEqual(bubble["fill_color"], "#ffffff")
        self.assertEqual(bubble["fill_opacity"], 0.9)


class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.state = _state_with_tips(["A", "B", "C"])

    def _reject(self, payload):
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(self.state, payload)

    def test_invalid_font_size_is_rejected(self):
        for bad_size in (5, 73, "14", True):
            with self.subTest(size=bad_size):
                self._reject({
                    "layers": [_layer(default_font_size=bad_size)],
                    "annotations": [],
                })

    def test_invalid_color_is_rejected(self):
        for bad_color in ("red", "#12345", "url(#x)", "var(--c)", "#1f2937; fill:red"):
            with self.subTest(color=bad_color):
                self._reject({
                    "layers": [_layer(default_text_color=bad_color)],
                    "annotations": [],
                })

    def test_invalid_font_name_is_rejected(self):
        self._reject({
            "layers": [_layer(default_font_family="Comic Sans MS")],
            "annotations": [],
        })

    def test_invalid_font_style_and_weight_are_rejected(self):
        self._reject({"layers": [_layer(default_font_style="oblique")], "annotations": []})
        self._reject({"layers": [_layer(default_font_weight="900")], "annotations": []})

    def test_annotation_referencing_a_nonexistent_layer_is_rejected(self):
        self._reject({
            "layers": [_layer("layer_a")],
            "annotations": [_annotation(layer_id="layer_missing")],
        })

    def test_duplicate_layer_ids_are_rejected(self):
        self._reject({
            "layers": [_layer("layer_a", "One"), _layer("layer_a", "Two", order=2)],
            "annotations": [],
        })

    def test_duplicate_annotation_ids_are_rejected(self):
        self._reject({
            "layers": [_layer()],
            "annotations": [
                _annotation("annotation_a", members=["A"]),
                _annotation("annotation_a", members=["B"]),
            ],
        })

    def test_excessive_label_length_is_rejected(self):
        self._reject({
            "layers": [_layer()],
            "annotations": [_annotation(label="x" * (MAX_LABEL_LENGTH + 1))],
        })

    def test_control_characters_are_rejected(self):
        self._reject({"layers": [_layer(name="Bad\x07name")], "annotations": []})
        self._reject({
            "layers": [_layer()],
            "annotations": [_annotation(label="Bad\x00label")],
        })

    def test_more_than_ten_lines_is_rejected(self):
        self._reject({
            "layers": [_layer()],
            "annotations": [_annotation(label="\n".join(["x"] * (MAX_LABEL_LINES + 1)))],
        })

    def test_multiline_and_tabs_are_normalized(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer()],
            "annotations": [_annotation(label="A\r\nB\tC")],
        })
        self.assertEqual(config[CLADE_ANNOTATIONS_KEY][0]["label"], "A\nB    C")

    def test_fill_color_and_opacity_are_validated(self):
        config = normalize_annotation_config(self.state, {
            "layers": [_layer(default_fill_color="#ABCDEF", default_fill_opacity=0)],
            "annotations": [_annotation(
                annotation_type="branch_bubble", fill_color="#123456", fill_opacity=1,
            )],
        })
        layer = config[ANNOTATION_LAYERS_KEY][0]
        annotation = config[CLADE_ANNOTATIONS_KEY][0]
        self.assertEqual(layer["default_fill_color"], "#abcdef")
        self.assertEqual(layer["default_fill_opacity"], 0)
        self.assertEqual(annotation["fill_opacity"], 1)
        for bad in (-0.01, 1.01, float("nan"), "0.5", True):
            with self.subTest(opacity=bad):
                self._reject({
                    "layers": [_layer(default_fill_opacity=bad)],
                    "annotations": [],
                })

    def test_unknown_annotation_type_is_rejected(self):
        self._reject({
            "layers": [_layer()],
            "annotations": [_annotation(annotation_type="speech_balloon")],
        })

    def test_member_ids_must_resolve_to_real_leaves(self):
        self._reject({
            "layers": [_layer()],
            "annotations": [_annotation(members=["A", "NotATip"])],
        })

    def test_duplicate_tip_names_cannot_bind_membership(self):
        state = _state_with_tips(["A", "A", "B"])
        with self.assertRaises(AnnotationValidationError) as ctx:
            normalize_annotation_config(state, {
                "layers": [_layer()],
                "annotations": [_annotation(members=["A"])],
            })
        # It must refuse rather than silently bind to whichever leaf comes first.
        self.assertIn("more than one tip", str(ctx.exception))

    def test_annotation_with_no_resolvable_members_is_rejected(self):
        self._reject({"layers": [_layer()], "annotations": [_annotation(members=[])]})

    def test_too_many_layers_are_rejected(self):
        # MAX_LAYERS + 1, taken from the validator rather than a copy of its
        # current value: a hardcoded 21 stops testing the limit the moment the
        # limit moves, and does so without failing.
        self._reject({
            "layers": [
                _layer(f"layer_{i}", f"L{i}", order=i + 1)
                for i in range(MAX_LAYERS + 1)
            ],
            "annotations": [],
        })

    def test_malformed_payload_shapes_are_rejected(self):
        self._reject({"layers": "nope", "annotations": []})
        self._reject({"layers": [_layer()], "annotations": "nope"})
        self._reject({"layers": [_layer()], "annotations": [_annotation(member_tip_ids="A")]})
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(self.state, ["not", "an", "object"])


class CompletePayloadTests(unittest.TestCase):
    """The endpoint replaces the whole configuration, so it must be sent whole.

    An omitted collection used to normalize to ``[]``, which turned a merely
    incomplete request into a destructive one: POSTing ``{"layers": [...]}``
    erased every annotation.
    """

    def setUp(self):
        self.state = _state_with_tips(["A", "B", "C"])
        self.state[ANNOTATION_LAYERS_KEY] = [_layer()]
        self.state[CLADE_ANNOTATIONS_KEY] = [_annotation()]

    def _reject_without_touching_state(self, payload):
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(self.state, payload)
        # The stored configuration is still exactly what it was.
        self.assertEqual(len(self.state[ANNOTATION_LAYERS_KEY]), 1)
        self.assertEqual(len(self.state[CLADE_ANNOTATIONS_KEY]), 1)
        self.assertEqual(
            self.state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "B"]
        )

    def test_missing_annotations_cannot_erase_existing_annotations(self):
        self._reject_without_touching_state({"layers": []})
        self._reject_without_touching_state({"layers": [_layer()]})

    def test_missing_layers_cannot_erase_existing_layers(self):
        self._reject_without_touching_state({"annotations": []})

    def test_empty_payload_is_rejected(self):
        self._reject_without_touching_state({})

    def test_null_collections_are_rejected(self):
        self._reject_without_touching_state({"layers": None, "annotations": []})
        self._reject_without_touching_state({"layers": [], "annotations": None})

    def test_explicit_empty_collections_still_clear_everything(self):
        config = normalize_annotation_config(self.state, {
            "layers": [], "annotations": [],
        })
        apply_annotation_config(self.state, config)
        self.assertEqual(self.state[ANNOTATION_LAYERS_KEY], [])
        self.assertEqual(self.state[CLADE_ANNOTATIONS_KEY], [])

    def test_internal_key_names_are_accepted_as_aliases(self):
        config = normalize_annotation_config(self.state, {
            ANNOTATION_LAYERS_KEY: [_layer()],
            CLADE_ANNOTATIONS_KEY: [_annotation()],
        })
        self.assertEqual(len(config[ANNOTATION_LAYERS_KEY]), 1)
        self.assertEqual(len(config[CLADE_ANNOTATIONS_KEY]), 1)

    def test_an_alias_still_has_to_supply_both_collections(self):
        self._reject_without_touching_state({ANNOTATION_LAYERS_KEY: [_layer()]})
        self._reject_without_touching_state({CLADE_ANNOTATIONS_KEY: []})


class NonFiniteNumberTests(unittest.TestCase):
    """NaN/Infinity must be an ordinary validation error, not a 500.

    ``json.loads`` accepts the JavaScript-only literals ``NaN``, ``Infinity``
    and ``-Infinity`` by default, so they really can arrive in a request body,
    and ``round()`` on them raises ValueError/OverflowError.
    """

    NON_FINITE = (float("nan"), float("inf"), float("-inf"))

    def setUp(self):
        self.state = _state_with_tips(["A", "B"])

    def test_non_finite_layer_order_is_rejected(self):
        for value in self.NON_FINITE:
            with self.subTest(order=value):
                with self.assertRaises(AnnotationValidationError):
                    normalize_annotation_config(self.state, {
                        "layers": [_layer(order=value)], "annotations": [],
                    })

    def test_non_finite_layer_font_size_is_rejected(self):
        for value in self.NON_FINITE:
            with self.subTest(font_size=value):
                with self.assertRaises(AnnotationValidationError):
                    normalize_annotation_config(self.state, {
                        "layers": [_layer(default_font_size=value)], "annotations": [],
                    })

    def test_non_finite_annotation_font_size_is_rejected(self):
        for value in self.NON_FINITE:
            with self.subTest(font_size=value):
                with self.assertRaises(AnnotationValidationError):
                    normalize_annotation_config(self.state, {
                        "layers": [_layer()],
                        "annotations": [_annotation(font_size=value)],
                    })

    def test_a_body_parsed_from_javascript_style_json_is_rejected(self):
        # Exactly what request.get_json() produces for this body.
        payload = json.loads(
            '{"layers": [{"id": "layer_a", "name": "Sections", "order": NaN}],'
            ' "annotations": []}'
        )
        self.assertTrue(math.isnan(payload["layers"][0]["order"]))
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(self.state, payload)

    def test_an_integer_too_large_for_a_float_is_rejected(self):
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(self.state, {
                "layers": [_layer(order=10 ** 400)], "annotations": [],
            })


class MembershipCleanupTests(unittest.TestCase):
    def _state(self):
        state = _state_with_tips(["A", "B", "C", "D"])
        state[ANNOTATION_LAYERS_KEY] = [_layer()]
        state[CLADE_ANNOTATIONS_KEY] = [
            _annotation("annotation_a", members=["A", "B", "C"]),
            _annotation("annotation_b", label="Just D", members=["D"]),
        ]
        return state

    def test_pruning_removes_only_pruned_members(self):
        state = self._state()
        remove_pruned_members_from_annotations(state, {"B"})
        self.assertEqual(
            state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "C"]
        )
        self.assertEqual(len(state[CLADE_ANNOTATIONS_KEY]), 2)

    def test_one_remaining_member_is_preserved(self):
        state = self._state()
        remove_pruned_members_from_annotations(state, {"A", "B"})
        self.assertEqual(state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["C"])

    def test_annotation_is_deleted_only_when_zero_members_remain(self):
        state = self._state()
        remove_pruned_members_from_annotations(state, {"A", "B", "C"})
        remaining = [a["id"] for a in state[CLADE_ANNOTATIONS_KEY]]
        self.assertEqual(remaining, ["annotation_b"])

    def test_recompute_drops_only_members_whose_leaves_disappeared(self):
        state = self._state()
        # Recompute rebuilt the tree without C.
        state["tree_structure"] = _state_with_tips(["A", "B", "D"])["tree_structure"]
        restrict_annotations_to_current_leaves(state)
        self.assertEqual(state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "B"])
        self.assertEqual(len(state[CLADE_ANNOTATIONS_KEY]), 2)

    def test_unparseable_structure_leaves_annotations_alone(self):
        state = self._state()
        state["tree_structure"] = {}
        restrict_annotations_to_current_leaves(state)
        self.assertEqual(len(state[CLADE_ANNOTATIONS_KEY]), 2)


class StatePreservationTests(unittest.TestCase):
    def test_applying_a_config_preserves_unrelated_tree_state_keys(self):
        state = _state_with_tips(["A", "B"])
        state.update({
            "pruned_taxa": ["Z"],
            "renames": {"A": "Alpha"},
            "root_mode": "midpoint",
            "selection_sets": {"Default": ["A"]},
            "sequence_of_interest": "A",
        })
        config = normalize_annotation_config(state, {
            "layers": [_layer()],
            "annotations": [_annotation()],
        })
        apply_annotation_config(state, config)

        self.assertEqual(state["pruned_taxa"], ["Z"])
        self.assertEqual(state["renames"], {"A": "Alpha"})
        self.assertEqual(state["root_mode"], "midpoint")
        self.assertEqual(state["selection_sets"], {"Default": ["A"]})
        self.assertEqual(state["sequence_of_interest"], "A")
        self.assertEqual(len(state[CLADE_ANNOTATIONS_KEY]), 1)

    def test_rejected_configuration_leaves_existing_annotations_untouched(self):
        state = _state_with_tips(["A", "B"])
        state[ANNOTATION_LAYERS_KEY] = [_layer()]
        state[CLADE_ANNOTATIONS_KEY] = [_annotation()]
        with self.assertRaises(AnnotationValidationError):
            normalize_annotation_config(state, {
                "layers": [_layer()],
                "annotations": [_annotation(members=["NotATip"])],
            })
        # Nothing is written before validation completes.
        self.assertEqual(state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "B"])

    def test_annotations_survive_a_save_and_reload_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            state = _state_with_tips(["A", "B"])
            config = normalize_annotation_config(state, {
                "layers": [_layer()],
                "annotations": [_annotation()],
            })
            apply_annotation_config(state, config)
            save_tree_state(job_dir, state)

            reloaded = load_tree_state(job_dir)
            self.assertEqual(len(reloaded[CLADE_ANNOTATIONS_KEY]), 1)
            self.assertEqual(
                reloaded[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"], ["A", "B"]
            )


@unittest.skipUnless(HAS_BIOPYTHON, "BioPython is required for pruning")
class PruneIntegrationTests(unittest.TestCase):
    """prune_taxa() must clean annotations the same way it cleans selection sets."""

    def test_prune_shrinks_an_annotation_instead_of_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "tree").mkdir(parents=True)
            (job_dir / "tree" / "tree_original.newick").write_text(
                "(((A:0.1,B:0.1):0.1,C:0.1):0.1,D:0.1);"
            )
            state = load_tree_state(job_dir)
            state[ANNOTATION_LAYERS_KEY] = [_layer()]
            state[CLADE_ANNOTATIONS_KEY] = [
                _annotation("annotation_a", members=["A", "B", "C"]),
                _annotation("annotation_b", label="Only A", members=["A"]),
            ]

            state = prune_taxa(job_dir, state, ["A"])

            by_id = {a["id"]: a for a in state[CLADE_ANNOTATIONS_KEY]}
            # The multi-member annotation keeps its remaining tips ...
            self.assertEqual(by_id["annotation_a"]["member_tip_ids"], ["B", "C"])
            # ... and only the annotation left with nothing is removed.
            self.assertNotIn("annotation_b", by_id)


class IncompletePayloadRouteTests(unittest.TestCase):
    """The same contract seen from the HTTP boundary: incomplete means 400."""

    # Has to satisfy validate_job_id(), which insists on a real UUID4 (version
    # nibble 4, variant nibble 8-b) before any path is built from it.
    JOB_ID = "12345678-1234-4234-8234-123456789abc"

    def _stored_state(self):
        state = _state_with_tips(["A", "B"])
        state[ANNOTATION_LAYERS_KEY] = [_layer()]
        state[CLADE_ANNOTATIONS_KEY] = [_annotation()]
        return state

    def _post(self, tmp, body):
        from unittest.mock import patch

        from flask import Flask

        from app.api import routes
        from app.config import Config

        app = Flask(__name__)
        with (
            app.test_request_context(method="POST", json=body),
            patch.object(Config, "JOB_DIR", tmp),
            patch.object(routes, "check_job_access", return_value=(None, None, 200)),
        ):
            result = routes.save_clade_annotations(self.JOB_ID)
        # The success path returns a bare Response; every refusal returns
        # (Response, status).
        if isinstance(result, tuple):
            return result[1], result[0].get_json()
        return 200, result.get_json()

    def test_half_a_configuration_is_rejected_and_changes_nothing(self):
        for body in ({"layers": []}, {"annotations": []}, {}):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / self.JOB_ID
                job_dir.mkdir()
                save_tree_state(job_dir, self._stored_state())

                status, payload = self._post(Path(tmp), body)

                self.assertEqual(status, 400)
                self.assertEqual(payload["status"], "error")
                reloaded = load_tree_state(job_dir)
                self.assertEqual(len(reloaded[CLADE_ANNOTATIONS_KEY]), 1)
                self.assertEqual(len(reloaded[ANNOTATION_LAYERS_KEY]), 1)

    def test_explicit_clear_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / self.JOB_ID
            job_dir.mkdir()
            save_tree_state(job_dir, self._stored_state())

            status, payload = self._post(Path(tmp), {"layers": [], "annotations": []})

            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "ok")
            reloaded = load_tree_state(job_dir)
            self.assertEqual(reloaded[CLADE_ANNOTATIONS_KEY], [])
            self.assertEqual(reloaded[ANNOTATION_LAYERS_KEY], [])


# --- renderer: what may be drawn ------------------------------------------
#
# The rule lives in tree_viewer_phylotree_v2.js, so it is tested there rather
# than restated in Python. The harness loads that file in Node with a handful of
# browser stubs, hands the viewer a synthetic tip order and node tree, and asks
# it to resolve annotations -- no DOM, no D3, no new JS test framework.

# The browser stubs both Node harnesses need, defined once. They used to be
# copied verbatim into each harness, so a viewer that started touching a new
# global had to be accommodated in both -- and updating only one failed with a
# ReferenceError pointing at the viewer rather than at the harness that was
# missing the stub.
_HARNESS_PRELUDE_JS = r"""
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const sandbox = {
    console, setTimeout, clearTimeout, URLSearchParams,
    document: { getElementById: () => null },
    window: {
        location: { search: '' },
        addEventListener: () => {},
        removeEventListener: () => {}
    }
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
"""

# Instantiated without running the constructor: these tests exercise pure
# methods, and the real constructor wants a DOM.
_HARNESS_VIEWER_JS = r"""
const viewer = Object.create(sandbox.window.DikaryaTreeViewer.prototype);
"""


_RENDER_HARNESS_JS = _HARNESS_PRELUDE_JS + r"""
// Build a node tree from a nested array spec, or an object carrying a branch
// length for soft-polytomy cases. Same shape the viewer walks in production.
const allNodes = [];
function build(spec, parent) {
    const objectSpec = spec && !Array.isArray(spec) && typeof spec === 'object';
    const node = {
        parent: parent || null,
        children: [],
        data: { attribute: objectSpec ? spec.length : null }
    };
    const childrenSpec = Array.isArray(spec) ? spec : (objectSpec ? spec.children : null);
    if (typeof spec === 'string' || (objectSpec && !childrenSpec)) {
        node.id = typeof spec === 'string' ? spec : spec.id;
        node.__leafCount = 1;
    } else {
        node.children = childrenSpec.map((child) => build(child, node));
        node.__leafCount = node.children.reduce((sum, child) => sum + child.__leafCount, 0);
    }
    allNodes.push(node);
    return node;
}
const root = build(input.tree, null);

const positions = new Map();
input.tipOrder.forEach((id, index) => positions.set(id, { y: index * 10, index }));
""" + _HARNESS_VIEWER_JS + r"""
viewer.allNodes = allNodes;
viewer._getNodeId = (node) => node.id || null;
viewer.tree = {
    traverse_and_compute(fn) {
        const walk = (node) => { fn(node); node.children.forEach(walk); };
        walk(root);
    }
};
viewer.annotationLayers = input.layers;
viewer.cladeAnnotations = input.annotations;
// Persistent colour groups, exactly as the viewer stores them.
viewer.selectionSets = {};
Object.entries(input.selectionSets || {}).forEach(([name, ids]) => {
    viewer.selectionSets[name] = new Set(ids);
});
viewer.selectionSetColors = Object.assign({}, input.selectionSetColors);
viewer.activeSelectionSet = input.activeSelectionSet || 'Default';
viewer.getSelectionSetColor = (name) => viewer.selectionSetColors[name] || null;
viewer._isDarkTheme = () => !!input.dark;
viewer.getSelectedNodes = () => allNodes.filter((node) =>
    !node.children.length && (input.selectedIds || []).includes(node.id)
);

let incomingBranchResult = null;
let incomingBranchDescendantWalks = null;
if (input.incomingBranchMembers) {
    const getDescendantLeafIds = viewer.getDescendantLeafIds.bind(viewer);
    incomingBranchDescendantWalks = 0;
    viewer.getDescendantLeafIds = (node) => {
        incomingBranchDescendantWalks += 1;
        return getDescendantLeafIds(node);
    };
    incomingBranchResult = viewer.hasIncomingBranchForMemberIds(input.incomingBranchMembers);
    viewer.getDescendantLeafIds = getDescendantLeafIds;
}

let tipLabelOffsets = null;
if (input.tipLabelGap !== undefined && input.tipLabelGap !== null) {
    viewer.tipLabelGap = input.tipLabelGap;
    tipLabelOffsets = {
        start: viewer._tipLabelDx({ text_align: 'start' }, 2),
        end: viewer._tipLabelDx({ text_align: 'end' }, 2)
    };
}

let groupedTipOrder = null;
if (input.autoGroup) {
    viewer._groupZeroLengthPolytomies();
    groupedTipOrder = viewer._tipOrderFromModel();
}

const { cladeBlocks, branchNodes, cladeNodes } =
    viewer._buildAnnotationTopologyIndexes(positions);
const layerById = new Map(input.layers.map((layer) => [layer.id, layer]));
const { validity, resolved } = viewer._resolveAnnotationsForRender(
    positions, cladeBlocks, layerById, branchNodes, cladeNodes
);
// Exactly the per-item preparation the renderer does before it draws anything:
// the resolved type, and every style property resolved through the annotation,
// then its layer, then the shared default.
const STYLE_FIELDS = sandbox.window.DikaryaCladeAnnotations.STYLE_FIELDS;
const NUMERIC_STYLE_FIELDS = new Set(
    sandbox.window.DikaryaCladeAnnotations.NUMERIC_STYLE_FIELDS
);
resolved.forEach((item) => {
    item.type = viewer._annotationType(item.annotation);
    const style = {};
    for (const field of STYLE_FIELDS) {
        const entry = viewer._resolveAnnotationStyleEntry(item.annotation, item.layer, field);
        style[field] = NUMERIC_STYLE_FIELDS.has(field) ? Number(entry.value) : entry.value;
        style[field + '_is_default'] = entry.isDefault;
    }
    item.style = style;
});

// Alan 8/24/26 - What the editor previews for an unsaved draft, and what the same draft
// resolves to once it has been saved over the annotation it is editing. The two must agree:
// the whole point of the preview is that the user approves the colour they will get.
viewer._lastAnnotationPositions = positions;
let draftPreview = null;
if (input.draftPreview) {
    const draft = input.draftPreview;
    const draftLayer = input.layers.find((layer) => layer.id === draft.layer_id) || null;
    const styleFor = (annotation, layer) => {
        const style = {};
        for (const field of STYLE_FIELDS) {
            const entry = viewer._resolveAnnotationStyleEntry(annotation, layer, field);
            style[field] = NUMERIC_STYLE_FIELDS.has(field) ? Number(entry.value) : entry.value;
            style[field + '_is_default'] = entry.isDefault;
        }
        return style;
    };
    const draftItem = {
        annotation: draft, layer: draftLayer, style: styleFor(draft, draftLayer)
    };
    // No assignments map: exactly the path renderAnnotationPreview() takes.
    const preview = viewer._effectiveHighlightStyle(draftItem, null);
    const swatch = viewer.resolveDraftHighlightColor(draft, draftLayer);

    const before = viewer.cladeAnnotations.slice();
    const index = viewer.cladeAnnotations.findIndex((entry) => entry.id === draft.id);
    if (index >= 0) viewer.cladeAnnotations[index] = draft;
    else viewer.cladeAnnotations.push(draft);
    const saved = viewer._effectiveHighlightStyle(
        draftItem, viewer._automaticHighlightAssignments(positions).colors
    );
    const savedCount = viewer.cladeAnnotations
        .filter((entry) => entry.id === draft.id).length;
    viewer.cladeAnnotations = before;
    draftPreview = { preview, swatch, saved, savedCount };
}

let annotationsForNode = null;
if (input.lookupMembers) {
    const lookupKey = viewer._annotationMembershipKey(input.lookupMembers);
    const lookupNode = allNodes.find((node) =>
        node.parent && viewer._annotationMembershipKey(viewer.getDescendantLeafIds(node)) === lookupKey
    );
    annotationsForNode = lookupNode
        ? viewer.getAnnotationsForNode(lookupNode).map((annotation) => annotation.id)
        : [];
}

let contextBranchTargets = null;
let contextDispatchedTargets = null;
if (input.branchTargetMembers) {
    const targetKey = viewer._annotationMembershipKey(input.branchTargetMembers);
    const distalNode = allNodes.find((node) =>
        node.parent && viewer._annotationMembershipKey(viewer.getDescendantLeafIds(node)) === targetKey
    );
    const branchElement = { __datum: { source: distalNode?.parent, target: distalNode } };
    sandbox.window.d3v7 = {
        select: (element) => ({ datum: () => element?.__datum || null })
    };
    const makeClickTarget = () => ({
        __datum: branchElement.__datum,
        tagName: 'path',
        classList: { contains: () => false },
        closest: (selector) => selector.includes('path.branch') ? branchElement : null
    });
    const clickTargets = [makeClickTarget(), makeClickTarget()];
    contextBranchTargets = clickTargets.map((target) => {
        const resolvedNode = viewer._getContextMenuNode(target);
        return viewer._annotationMembershipKey(viewer.getDescendantLeafIds(resolvedNode));
    });
    const listeners = {};
    viewer.container = {
        addEventListener: (type, handler) => { listeners[type] = handler; },
        removeEventListener: () => {}
    };
    let dispatchedNode = null;
    viewer.tree = { display: { handle_node_click: (node) => { dispatchedNode = node; } } };
    viewer._overrideClickBehavior();
    contextDispatchedTargets = clickTargets.map((target) => {
        dispatchedNode = null;
        listeners.contextmenu({
            target,
            preventDefault: () => {},
            stopPropagation: () => {}
        });
        return viewer._annotationMembershipKey(viewer.getDescendantLeafIds(dispatchedNode));
    });
}

process.stdout.write(JSON.stringify({
    blocks: Array.from(cladeBlocks).sort(),
    branchBlocks: Array.from(branchNodes.keys()).sort(),
    validity: Object.fromEntries(validity),
    drawn: resolved.map((item) => item.annotation.id),
    drawnTypes: resolved.map((item) => viewer._annotationType(item.annotation)),
    highlightOrder: viewer._orderCladeHighlights(resolved)
        .map((item) => item.annotation.id),
    // A highlight has to know which node it starts at, root included.
    highlightNodeSpans: resolved
        .filter((item) => item.type === 'clade_highlight')
        .map((item) => (item.cladeNode
            ? viewer.getDescendantLeafIds(item.cladeNode).slice().sort().join(',')
            : null)),
    // How far down the tree the node backing each band is. A unary chain gives several
    // nodes the same descendant set; the deepest is the one drawn nearest the clade.
    highlightNodeDepths: resolved
        .filter((item) => item.type === 'clade_highlight')
        .map((item) => {
            let depth = 0;
            let node = item.cladeNode;
            while (node && node.parent) { depth += 1; node = node.parent; }
            return item.cladeNode ? depth : null;
        }),
    // The colour each band is actually painted with, resolved exactly as the renderer does.
    highlightStyles: viewer._orderCladeHighlights(resolved).map((item) => {
        const effective = viewer._effectiveHighlightStyle(
            item, viewer._automaticHighlightAssignments(positions).colors
        );
        return { id: item.annotation.id, color: effective.color, opacity: effective.opacity };
    }),
    automaticColors: Object.fromEntries(
        viewer._automaticHighlightAssignments(positions).colors
    ),
    // The palette slot each automatic highlight would be saved with.
    automaticSlots: Object.fromEntries(
        viewer._automaticHighlightAssignments(positions).slots
    ),
    reservedSlot: input.reserveSlotFor
        ? viewer.reserveAutomaticHighlightSlot(
            input.reserveSlotFor.id, input.reserveSlotFor.memberIds)
        : null,
    annotationsForNode,
    selectedClade: input.selectedIds ? viewer.getSelectedCladeLeafIds() : null,
    selectedHasIncomingBranch: input.selectedIds
        ? viewer.hasIncomingBranchForMemberIds(input.selectedIds) : null,
    draftPreview,
    contextBranchTargets,
    contextDispatchedTargets,
    incomingBranchResult,
    incomingBranchDescendantWalks,
    tipLabelOffsets
    ,groupedTipOrder
}));
"""


def _node_available():
    return shutil.which("node") is not None


class _RenderHarnessMixin:
    """Drives the shipped viewer's annotation resolution under Node.

    Shared by the classes below rather than inherited from one of them, so a
    second suite of cases does not silently re-run the first one's.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    # ((A,B),(C,D)) -- so A+B and C+D are clades, but B+C is not, even though
    # B and C are neighbours in the tip order.
    TREE = [["A", "B"], ["C", "D"]]
    TIP_ORDER = ["A", "B", "C", "D"]

    def _resolve(self, annotations, layers=None, tree=None, tip_order=None,
                 lookup_members=None, selected_ids=None, branch_target_members=None,
                 incoming_branch_members=None, tip_label_gap=None,
                 selection_sets=None, selection_set_colors=None,
                 active_selection_set=None, dark=False, reserve_slot_for=None,
                 draft_preview=None, auto_group=False):
        layers = layers if layers is not None else [_layer()]
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "resolve_annotations.js"
            harness.write_text(_RENDER_HARNESS_JS)
            payload = json.dumps({
                "tree": tree if tree is not None else self.TREE,
                "tipOrder": tip_order if tip_order is not None else self.TIP_ORDER,
                "layers": layers,
                "annotations": annotations,
                "lookupMembers": lookup_members,
                "selectedIds": selected_ids,
                "branchTargetMembers": branch_target_members,
                "incomingBranchMembers": incoming_branch_members,
                "tipLabelGap": tip_label_gap,
                "selectionSets": selection_sets,
                "selectionSetColors": selection_set_colors,
                "activeSelectionSet": active_selection_set,
                "dark": dark,
                "reserveSlotFor": reserve_slot_for,
                "draftPreview": draft_preview,
                "autoGroup": auto_group,
            })
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=payload, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class AnnotationRenderDecisionTests(_RenderHarnessMixin, unittest.TestCase):
    """Only exact clades draw; branch types additionally require an incoming branch.

    Contiguity alone is not enough. After a reroot an annotation's tips can sit
    together in the tip order while no node has that exact descendant set; the
    renderer used to draw those as one continuous (dashed) bracket, which still
    reads as "these taxa are a group". They are now flagged in the manager and
    left off the figure entirely, and they come back by themselves when the
    topology makes them a clade again.
    """

    def test_clade_block_index_matches_the_topology(self):
        out = self._resolve([_annotation(members=["A", "B"])])
        self.assertEqual(
            out["blocks"], ["0:0", "0:1", "0:3", "1:1", "2:2", "2:3", "3:3"]
        )
        self.assertEqual(
            out["branchBlocks"], ["0:0", "0:1", "1:1", "2:2", "2:3", "3:3"]
        )

    def test_valid_clade_is_drawn(self):
        out = self._resolve([_annotation("ann_ab", members=["A", "B"])])
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["drawn"], ["ann_ab"])

    def test_contiguous_but_not_a_clade_is_invalid_and_not_drawn(self):
        out = self._resolve([_annotation("ann_bc", members=["B", "C"])])
        # Vertically adjacent on screen ...
        self.assertEqual(out["validity"]["ann_bc"]["present"], 2)
        # ... but not the descendant set of any node, so no bracket.
        self.assertFalse(out["validity"]["ann_bc"]["valid"])
        self.assertEqual(out["drawn"], [])

    def test_non_contiguous_membership_is_invalid_and_not_drawn(self):
        out = self._resolve([_annotation("ann_ac", members=["A", "C"])])
        self.assertFalse(out["validity"]["ann_ac"]["valid"])
        self.assertEqual(out["drawn"], [])

    def test_invalid_annotations_are_retained_in_state_and_reported(self):
        out = self._resolve([
            _annotation("ann_ab", members=["A", "B"]),
            _annotation("ann_bc", members=["B", "C"]),
            _annotation("ann_ac", members=["A", "C"]),
        ])
        # All three are still known -- the manager needs them to show its warning.
        self.assertEqual(
            sorted(out["validity"]), ["ann_ab", "ann_ac", "ann_bc"]
        )
        # Only the real clade reaches the figure.
        self.assertEqual(out["drawn"], ["ann_ab"])

    def test_whole_tree_clade_line_and_terminal_branch_are_valid(self):
        out = self._resolve([
            _annotation("ann_all", members=["A", "B", "C", "D"],
                        annotation_type="clade_line"),
            _annotation("ann_one", members=["C"]),
        ])
        self.assertTrue(out["validity"]["ann_all"]["valid"])
        self.assertTrue(out["validity"]["ann_one"]["valid"])
        self.assertEqual(out["drawn"], ["ann_all", "ann_one"])

    def test_root_branch_types_are_invalid_without_an_incoming_branch(self):
        for annotation_type in ("branch_text", "branch_bubble"):
            with self.subTest(annotation_type=annotation_type):
                out = self._resolve([_annotation(
                    "ann_all", members=["A", "B", "C", "D"],
                    annotation_type=annotation_type,
                )])
                self.assertFalse(out["validity"]["ann_all"]["valid"])
                self.assertEqual(out["drawn"], [])

    def test_selecting_every_tip_resolves_the_root_clade(self):
        out = self._resolve(
            [_annotation("ann_all", members=["A", "B", "C", "D"])],
            selected_ids=["A", "B", "C", "D"],
        )
        self.assertEqual(set(out["selectedClade"]), {"A", "B", "C", "D"})
        self.assertFalse(out["selectedHasIncomingBranch"])

    def test_non_root_selected_clade_has_an_incoming_branch(self):
        out = self._resolve([], selected_ids=["A", "B"])
        self.assertEqual(set(out["selectedClade"]), {"A", "B"})
        self.assertTrue(out["selectedHasIncomingBranch"])

    def test_complete_zero_length_polytomy_resolves_as_one_clade(self):
        # X hangs from the arbitrary binary resolution on a positive branch. A, B and C
        # are the complete zero-length connected component and are adjacent on screen.
        tree = {
            "children": [
                {"id": "X", "length": 0.01},
                {"children": [
                    {"id": "A", "length": 6e-9},
                    {"children": [
                        {"id": "B", "length": 6e-9},
                        {"id": "C", "length": 1.2e-8},
                    ], "length": 6e-9},
                ], "length": 6e-9},
            ]
        }
        out = self._resolve(
            [_annotation("soft", members=["A", "B", "C"])],
            tree=tree, tip_order=["X", "A", "B", "C"],
            selected_ids=["A", "B", "C"],
        )
        self.assertEqual(out["selectedClade"], ["A", "B", "C"])
        self.assertTrue(out["validity"]["soft"]["valid"])
        self.assertEqual(out["drawn"], ["soft"])

    def test_partial_zero_length_polytomy_is_not_a_clade(self):
        tree = {
            "children": [
                {"id": "X", "length": 0.01},
                {"children": [
                    {"id": "A", "length": 6e-9},
                    {"children": [
                        {"id": "B", "length": 6e-9},
                        {"id": "C", "length": 6e-9},
                    ], "length": 6e-9},
                ], "length": 6e-9},
            ]
        }
        out = self._resolve(
            [], tree=tree, tip_order=["X", "A", "B", "C"],
            selected_ids=["A", "B"],
        )
        self.assertIsNone(out["selectedClade"])

    def test_zero_length_polytomy_tips_are_grouped_automatically(self):
        tree = {
            "children": [
                {"children": [
                    {"id": "A", "length": 6e-9},
                    {"id": "X", "length": 0.01},
                ], "length": 6e-9},
                {"children": [
                    {"id": "B", "length": 6e-9},
                    {"id": "C", "length": 6e-9},
                ], "length": 6e-9},
            ]
        }
        out = self._resolve(
            [], tree=tree, tip_order=["A", "X", "B", "C"], auto_group=True,
        )
        order = out["groupedTipOrder"]
        selected_indices = sorted(order.index(name) for name in ("A", "B", "C"))
        self.assertEqual(selected_indices[-1] - selected_indices[0] + 1, 3)


    def test_whole_tree_branch_check_skips_all_descendant_walks(self):
        out = self._resolve([], incoming_branch_members=["A", "B", "C", "D"])
        self.assertFalse(out["incomingBranchResult"])
        self.assertEqual(out["incomingBranchDescendantWalks"], 0)

    def test_tip_label_gap_points_outward_on_both_radial_halves(self):
        out = self._resolve([], tip_label_gap=80)
        self.assertEqual(out["tipLabelOffsets"], {"start": 40, "end": -40})

    def test_rotation_preserves_exact_descendant_membership(self):
        out = self._resolve(
            [_annotation("ann_ab", members=["A", "B"], annotation_type="branch_text")],
            tree=[["B", "A"], ["D", "C"]],
            tip_order=["B", "A", "D", "C"],
        )
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["drawnTypes"], ["branch_text"])

    def test_type_switching_does_not_change_membership(self):
        for annotation_type in ("clade_line", "branch_text", "branch_bubble"):
            with self.subTest(annotation_type=annotation_type):
                out = self._resolve([
                    _annotation("ann_ab", members=["A", "B"], annotation_type=annotation_type)
                ])
                self.assertTrue(out["validity"]["ann_ab"]["valid"])
                self.assertEqual(out["drawnTypes"], [annotation_type])

    def test_context_lookup_finds_existing_annotations_by_exact_membership(self):
        out = self._resolve([
            _annotation("ann_ab_first", members=["A", "B"]),
            _annotation("ann_cd", members=["C", "D"]),
            _annotation("ann_ab_second", members=["B", "A"], annotation_type="branch_text"),
        ], lookup_members=["B", "A"])
        self.assertEqual(
            out["annotationsForNode"], ["ann_ab_first", "ann_ab_second"]
        )

    def test_context_lookup_does_not_match_a_different_descendant_set(self):
        out = self._resolve([
            _annotation("ann_ab", members=["A", "B"]),
        ], lookup_members=["C", "D"])
        self.assertEqual(out["annotationsForNode"], [])

    def test_terminal_branch_context_resolves_its_distal_tip_at_both_ends(self):
        out = self._resolve([], branch_target_members=["C"])
        expected = "C"
        self.assertEqual(out["contextBranchTargets"], [expected, expected])
        self.assertEqual(out["contextDispatchedTargets"], [expected, expected])

    def test_internal_branch_context_resolves_its_distal_clade_at_both_ends(self):
        out = self._resolve([], branch_target_members=["A", "B"])
        expected = "A\u0000B"
        self.assertEqual(out["contextBranchTargets"], [expected, expected])
        self.assertEqual(out["contextDispatchedTargets"], [expected, expected])

    def test_members_missing_from_the_tree_are_reported_as_absent(self):
        out = self._resolve([_annotation("ann_gone", members=["Z"])])
        self.assertEqual(out["validity"]["ann_gone"], {"present": 0, "valid": False})
        self.assertEqual(out["drawn"], [])

    def test_a_hidden_layer_keeps_validity_but_draws_nothing(self):
        out = self._resolve(
            [_annotation("ann_ab", members=["A", "B"])],
            layers=[_layer(visible=False)],
        )
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["drawn"], [])


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class CladeHighlightRenderDecisionTests(_RenderHarnessMixin, unittest.TestCase):
    """A highlight follows the same topology rule as every other clade annotation."""

    def test_a_highlight_on_a_real_clade_is_drawn(self):
        out = self._resolve([_annotation(
            "ann_ab", members=["A", "B"], annotation_type="clade_highlight")])
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["drawn"], ["ann_ab"])
        self.assertEqual(out["drawnTypes"], ["clade_highlight"])

    def test_a_highlight_whose_tips_no_longer_form_one_clade_is_not_drawn(self):
        """No rectangle from the first matching tip to the last one.

        B and C are neighbours in the tip order but are not any node's
        descendant set. Painting a band across them would assert a group the
        topology does not contain, so the annotation is kept, flagged, and left
        off the figure -- exactly as a clade line is.
        """
        out = self._resolve([_annotation(
            "ann_bc", members=["B", "C"], annotation_type="clade_highlight")])
        self.assertEqual(out["validity"]["ann_bc"]["present"], 2)
        self.assertFalse(out["validity"]["ann_bc"]["valid"])
        self.assertEqual(out["drawn"], [])
        self.assertEqual(out["highlightOrder"], [])

    def test_a_non_contiguous_highlight_is_not_drawn(self):
        out = self._resolve([_annotation(
            "ann_ac", members=["A", "C"], annotation_type="clade_highlight")])
        self.assertFalse(out["validity"]["ann_ac"]["valid"])
        self.assertEqual(out["drawn"], [])

    def test_a_whole_tree_highlight_is_valid_without_an_incoming_branch(self):
        # Unlike branch text and bubbles: a band needs a clade, not a branch.
        out = self._resolve([_annotation(
            "ann_all", members=["A", "B", "C", "D"], annotation_type="clade_highlight")])
        self.assertTrue(out["validity"]["ann_all"]["valid"])
        self.assertEqual(out["drawn"], ["ann_all"])
        # And it still resolves the node it starts at, which is the root.
        self.assertEqual(out["highlightNodeSpans"], ["A,B,C,D"])

    def test_the_band_starts_at_the_node_owning_the_exact_descendant_set(self):
        out = self._resolve([_annotation(
            "ann_ab", members=["A", "B"], annotation_type="clade_highlight")])
        self.assertEqual(out["highlightNodeSpans"], ["A,B"])

    def test_rotating_children_does_not_break_a_highlight(self):
        out = self._resolve(
            [_annotation("ann_ab", members=["A", "B"],
                         annotation_type="clade_highlight")],
            tree=[["B", "A"], ["D", "C"]],
            tip_order=["B", "A", "D", "C"],
        )
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["drawn"], ["ann_ab"])

    def test_a_hidden_layer_keeps_a_highlight_valid_but_paints_nothing(self):
        out = self._resolve(
            [_annotation("ann_ab", members=["A", "B"],
                         annotation_type="clade_highlight")],
            layers=[_layer(visible=False)],
        )
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["highlightOrder"], [])

    def test_nested_highlights_paint_the_larger_clade_first(self):
        """Parent behind child, from clade size rather than creation order.

        The inner annotation is deliberately listed FIRST, so an implementation
        that painted in creation order would put the whole-tree band on top of
        it and hide it.
        """
        out = self._resolve([
            _annotation("ann_inner", members=["A", "B"],
                        annotation_type="clade_highlight"),
            _annotation("ann_outer", members=["A", "B", "C", "D"],
                        annotation_type="clade_highlight"),
        ])
        self.assertEqual(out["highlightOrder"], ["ann_outer", "ann_inner"])

    def test_three_levels_of_nesting_paint_outermost_first(self):
        out = self._resolve(
            [
                _annotation("ann_ab", members=["A", "B"],
                            annotation_type="clade_highlight"),
                _annotation("ann_all", members=["A", "B", "C", "D"],
                            annotation_type="clade_highlight"),
                _annotation("ann_a", members=["A"],
                            annotation_type="clade_highlight"),
            ],
            tree=[[["A", "B"], "C"], "D"],
            tip_order=["A", "B", "C", "D"],
        )
        self.assertEqual(out["highlightOrder"], ["ann_all", "ann_ab", "ann_a"])

    def test_equal_sized_highlights_order_deterministically(self):
        # Same span size on disjoint clades: top-to-bottom, then save order, so
        # two loads of the same tree never disagree.
        out = self._resolve([
            _annotation("ann_cd", members=["C", "D"], annotation_type="clade_highlight"),
            _annotation("ann_ab", members=["A", "B"], annotation_type="clade_highlight"),
        ])
        self.assertEqual(out["highlightOrder"], ["ann_ab", "ann_cd"])

    def test_a_highlight_is_never_stacked_over_the_branch_as_well(self):
        """Highlights take the clade lane, so they must not also be branch items."""
        out = self._resolve([_annotation(
            "ann_ab", members=["A", "B"], annotation_type="clade_highlight")])
        self.assertEqual(out["drawnTypes"], ["clade_highlight"])
        self.assertEqual(out["highlightOrder"], ["ann_ab"])

    def test_mixed_types_coexist_on_the_same_clade(self):
        out = self._resolve([
            _annotation("ann_line", members=["A", "B"], annotation_type="clade_line"),
            _annotation("ann_band", members=["A", "B"], annotation_type="clade_highlight"),
            _annotation("ann_text", members=["A", "B"], annotation_type="branch_text"),
        ])
        self.assertEqual(sorted(out["drawn"]), ["ann_band", "ann_line", "ann_text"])
        self.assertEqual(out["highlightOrder"], ["ann_band"])


# --- renderer: label geometry and style inheritance ------------------------
#
# Same approach as above: the rules live in tree_viewer_phylotree_v2.js and are
# pure functions, so Node evaluates them directly. No DOM, no D3, no new JS test
# framework.

_LAYOUT_HARNESS_JS = _HARNESS_PRELUDE_JS + _HARNESS_VIEWER_JS + r"""

// Character-count stand-in for getComputedTextLength(): deterministic, and all
// the layout rules care about is which string is the widest.
viewer._measureAnnotationText = (svgNode, text, style) => text.length * style.font_size;

const out = {};

if (input.tipGaps) {
    viewer._applyTextSizingFromZoom = () => {};
    out.tipGaps = input.tipGaps.map((value) => viewer.setTipLabelGap(value));
}

if (input.label !== undefined) out.lines = viewer._annotationLabelLines(input.label);

if (input.style) {
    out.style = {};
    for (const field of Object.keys(input.style.fields)) {
        out.style[field] = viewer._resolveAnnotationStyleEntry(
            input.style.annotation, input.style.layer, field
        );
    }
}

// A fake leaf <g>: a tip label plus whatever else phylotree left in the group.
function fakeLeafGroup(spec) {
    const label = spec.label === null ? null : {
        getBBox: () => spec.label,
        transform: spec.labelTransform === undefined ? undefined : {
            baseVal: {
                consolidate: () => (spec.labelTransform === null ? null
                    : { matrix: { e: spec.labelTransform } })
            }
        }
    };
    return {
        querySelector: (selector) =>
            (selector === 'text.phylotree-node-text' ? label : null),
        getBBox: () => {
            if (!spec.group) throw new Error('detached');
            return spec.group;
        }
    };
}

if (input.tipLabelEdges) {
    out.tipLabelEdges = input.tipLabelEdges.map((spec) =>
        viewer._tipLabelRightEdge(fakeLeafGroup(spec), spec.nodeX));
}

if (input.geometryLeaves) {
    // Stand in for the one d3 idiom _buildAnnotationGeometry uses.
    const svg = {
        selectAll: () => ({
            each(fn) {
                input.geometryLeaves.forEach((spec) => {
                    fn.call(fakeLeafGroup(spec), {
                        screen_x: spec.nodeX, screen_y: spec.y, children: []
                    });
                });
            }
        })
    };
    viewer._getNodeId = (node) => {
        const match = input.geometryLeaves.find((spec) => spec.nodeX === node.screen_x
            && spec.y === node.screen_y);
        return match ? match.id : null;
    };
    const geometry = viewer._buildAnnotationGeometry(svg);
    const metrics = viewer._annotationLaneMetrics(1 / (input.zoom || 1));
    out.geometry = {
        labelRight: geometry.labelRight,
        rowPitch: geometry.rowPitch,
        tipOrder: geometry.tipOrder,
        laneStart: geometry.labelRight + metrics.GAP_FROM_TREE,
        metrics
    };
}

if (input.localLabelRight) {
    const geometry = {
        tipRights: input.localLabelRight.tipRights,
        labelRight: input.localLabelRight.labelRight
    };
    out.localLabelRight = input.localLabelRight.cases.map(
        (indices) => viewer._localTipLabelRight(indices, geometry));
}

if (input.laneLayout) {
    // The whole horizontal chain a clade annotation goes through: its own descendants'
    // label edges -> its preferred lane -> the lane it is packed into -> that lane's x.
    const spec = input.laneLayout;
    const metrics = viewer._annotationLaneMetrics(1 / (spec.zoom || 1));
    const geometry = {
        tipRights: spec.tipRights || [],
        labelRight: spec.labelRight === undefined ? 0 : spec.labelRight
    };
    const items = spec.items.map((entry) => {
        const localLabelRight = viewer._localTipLabelRight(entry.indices, geometry);
        return {
            annotation: { id: entry.id },
            type: entry.type || 'clade_highlight',
            indices: entry.indices,
            savedIndex: entry.savedIndex || 0,
            scaledFontSize: entry.fontSize || 10,
            localLabelRight,
            preferredLaneX: localLabelRight + metrics.GAP_FROM_TREE,
            metrics: {
                renderTop: entry.renderTop,
                renderBottom: entry.renderBottom,
                laneWidth: entry.laneWidth
            }
        };
    });
    const placement = viewer._layoutAnnotationLanes(
        items, spec.rowPitch || 20, metrics.GAP_BETWEEN_LANES,
        spec.cursorX === undefined ? undefined : spec.cursorX
    );
    out.laneLayout = {
        gapFromTree: metrics.GAP_FROM_TREE,
        gapBetweenLanes: metrics.GAP_BETWEEN_LANES,
        cursorX: placement.cursorX,
        lanes: placement.lanes.map((lane) => ({
            index: lane.index, x: lane.x, width: lane.width,
            items: lane.items.map((item) => item.annotation.id)
        })),
        items: items.map((item) => ({
            id: item.annotation.id,
            localLabelRight: item.localLabelRight,
            preferredLaneX: item.preferredLaneX
        }))
    };
}

if (input.alignRightEdges) {
    const items = input.alignRightEdges.map((spec) => ({
        annotation: { id: spec.id },
        highlightRight: spec.right,
        highlightLayerId: spec.layerId,
        highlightLaneId: spec.laneId
    }));
    viewer._alignHighlightRightEdges(items);
    out.alignedRightEdges = items.map((item) => ({
        id: item.annotation.id, right: item.highlightRight
    }));
}

if (input.drawHighlightRect) {
    // Records exactly what the shared band primitive writes onto the rect.
    const calls = { attrs: {}, styles: {} };
    const fake = {
        append: () => fake,
        attr(name, value) { calls.attrs[name] = value; return fake; },
        style(name, value) { calls.styles[name] = value; return fake; }
    };
    const spec = input.drawHighlightRect;
    viewer._appendHighlightRect(fake, spec.rect, spec.effective, spec.annotationId || null);
    out.drawnRect = calls;
}

if (input.highlights) {
    out.highlights = input.highlights.map((spec) => {
        const node = { screen_x: spec.nodeX, screen_y: 0, parent: null };
        if (spec.parentX !== undefined && spec.parentX !== null) {
            node.parent = { screen_x: spec.parentX, screen_y: 0 };
        }
        const item = {
            cladeNode: node,
            top: spec.top,
            bottom: spec.bottom,
            highlightRight: spec.highlightRight,
            laneX: spec.laneX,
            metrics: {
                renderTop: spec.renderTop !== undefined ? spec.renderTop : spec.top,
                renderBottom: spec.renderBottom !== undefined ? spec.renderBottom : spec.bottom,
                laneWidth: spec.laneWidth
            }
        };
        return viewer._cladeHighlightRect(
            item, spec.rowPitch, spec.padX, spec.fallbackRight
        );
    });
}

if (input.steppedHighlights) {
    // Both pieces of the stepped band, plus what the shared primitive writes for each,
    // so screen and export attributes are asserted on the same geometry.
    out.steppedHighlights = input.steppedHighlights.map((spec) => {
        const node = { screen_x: spec.nodeX, screen_y: 0, parent: null };
        if (spec.parentX !== undefined && spec.parentX !== null) {
            node.parent = { screen_x: spec.parentX, screen_y: 0 };
        }
        const item = {
            cladeNode: node,
            top: spec.top,
            bottom: spec.bottom,
            highlightRight: spec.highlightRight,
            laneX: spec.laneX,
            metrics: {
                renderTop: spec.renderTop !== undefined ? spec.renderTop : spec.top,
                renderBottom: spec.renderBottom !== undefined ? spec.renderBottom : spec.bottom,
                laneWidth: spec.laneWidth
            }
        };
        // With `label` the metrics come from the SAME pipeline the renderer uses, rather
        // than from hand-written renderTop/renderBottom -- a hand-written pair can agree
        // with the band by accident where the real text block does not.
        if (spec.label !== undefined) {
            item.lines = viewer._annotationLabelLines(spec.label);
            item.scaledFontSize = spec.fontSize;
            item.style = { font_size: spec.fontSize };
            item.textWidth = viewer._measureAnnotationLabel(
                null, item.lines, { font_size: spec.fontSize }
            );
            item.metrics = viewer._annotationLayoutMetrics(item, spec.textGap || 6);
            if (spec.laneWidth !== undefined) item.metrics.laneWidth = spec.laneWidth;
        }
        const pieces = viewer._cladeHighlightRects(
            item, spec.rowPitch, spec.padX, spec.fallbackRight
        );
        const drawn = [];
        const effective = spec.effective || { color: '#3b6fb6', opacity: 0.2 };
        [pieces.band, pieces.label].forEach((rect) => {
            if (!rect) return;
            const calls = { attrs: {}, styles: {} };
            const fake = {
                append: () => fake,
                attr(name, value) { calls.attrs[name] = value; return fake; },
                style(name, value) { calls.styles[name] = value; return fake; }
            };
            viewer._appendHighlightRect(fake, rect, effective, spec.annotationId || null);
            drawn.push(calls);
        });
        return {
            band: pieces.band, label: pieces.label, drawn, metrics: item.metrics
        };
    });
}

if (input.previewHighlights) {
    // Exactly what renderAnnotationPreview() composes: the preview's synthetic clade
    // geometry, the real layout metrics, and the real stepped primitive.
    out.previewHighlights = input.previewHighlights.map((spec) => {
        const geom = viewer._previewHighlightGeometry(spec.width, spec.height);
        const lines = viewer._annotationLabelLines(spec.label);
        const item = {
            lines,
            type: 'clade_highlight',
            scaledFontSize: spec.fontSize,
            style: { font_size: spec.fontSize },
            textWidth: viewer._measureAnnotationLabel(
                null, lines, { font_size: spec.fontSize }
            ),
            top: geom.top,
            bottom: geom.bottom
        };
        item.metrics = viewer._annotationLayoutMetrics(item, 7);
        item.cladeNode = {
            screen_x: geom.nodeX, screen_y: (item.top + item.bottom) / 2, parent: null
        };
        item.laneX = geom.laneX;
        item.highlightRight = geom.laneX + item.metrics.laneWidth + geom.padX;
        const pieces = viewer._cladeHighlightRects(
            item, geom.rowPitch, geom.padX, geom.fallbackRight
        );
        return {
            geom,
            lineCount: lines.length,
            rects: [pieces.band, pieces.label].filter(Boolean)
        };
    });
}

if (input.items) {
    out.items = input.items.map((spec) => {
        const lines = viewer._annotationLabelLines(spec.label);
        const item = {
            lines,
            type: spec.type,
            top: spec.top,
            bottom: spec.bottom,
            scaledFontSize: spec.font_size,
            style: { font_size: spec.font_size },
            textWidth: viewer._measureAnnotationLabel(null, lines, { font_size: spec.font_size })
        };
        const metrics = spec.type === 'clade_line'
            ? viewer._annotationLayoutMetrics(item, spec.textGap || 6)
            : viewer._branchAnnotationMetrics(item);
        return { textWidth: item.textWidth, lineCount: lines.length, metrics };
    });
}

process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class AnnotationLayoutTests(unittest.TestCase):
    """Multiline labels, bubble vs line geometry, and inherited-vs-chosen ink."""

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _run(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_label_is_split_into_the_lines_the_svg_draws(self):
        out = self._run({"label": "Galerina\nsect. Mycenopsis"})
        self.assertEqual(out["lines"], ["Galerina", "sect. Mycenopsis"])

    def test_tip_label_gap_rounds_to_two_pixels_and_clamps(self):
        out = self._run({"tipGaps": [-3, 1, 79, 100, "bad"]})
        self.assertEqual(out["tipGaps"], [0, 2, 80, 80, 2])

    def test_trailing_newline_does_not_add_an_empty_row(self):
        out = self._run({"label": "Galerina\n"})
        self.assertEqual(out["lines"], ["Galerina"])
        # An interior blank line is deliberate spacing and is kept.
        self.assertEqual(
            self._run({"label": "A\n\nB"})["lines"], ["A", "", "B"]
        )

    def test_multiline_width_comes_from_the_widest_line(self):
        out = self._run({"items": [{
            "label": "short\nmuch longer line", "type": "clade_line",
            "top": 0, "bottom": 40, "font_size": 10,
        }]})
        item = out["items"][0]
        self.assertEqual(item["lineCount"], 2)
        # len("much longer line") == 16, not the 22 characters of the joined string.
        self.assertEqual(item["textWidth"], 16 * 10)

    def test_multiline_block_is_centred_and_reserves_its_own_height(self):
        # A two-line label on a clade with no vertical extent must still reserve
        # room for both lines, or packing would overlap the next annotation.
        out = self._run({"items": [{
            "label": "one\ntwo", "type": "clade_line",
            "top": 100, "bottom": 100, "font_size": 10,
        }]})
        metrics = out["items"][0]["metrics"]
        self.assertEqual(metrics["midY"], 100)
        self.assertEqual(metrics["blockHeight"], 2 * metrics["lineHeight"])
        self.assertAlmostEqual(metrics["renderTop"], 100 - metrics["blockHeight"] / 2)
        self.assertAlmostEqual(metrics["renderBottom"], 100 + metrics["blockHeight"] / 2)

    def test_branch_bubble_padding_exceeds_branch_text_geometry(self):
        out = self._run({"items": [
            {"label": "Section X", "type": "branch_text", "top": 0, "bottom": 40, "font_size": 10},
            {"label": "Section X", "type": "branch_bubble", "top": 0, "bottom": 40, "font_size": 10},
        ]})
        text, bubble = out["items"][0]["metrics"], out["items"][1]["metrics"]
        self.assertFalse(text["isBubble"])
        self.assertTrue(bubble["isBubble"])
        self.assertGreater(bubble["boxWidth"], text["boxWidth"])
        self.assertGreater(bubble["boxHeight"], text["boxHeight"])
        self.assertGreater(bubble["padX"], 0)
        self.assertGreater(bubble["padY"], 0)

    def test_inherited_default_ink_is_distinguishable_from_the_same_colour_chosen(self):
        default_ink = "#1f2937"
        out = self._run({"style": {
            "annotation": {"text_color": default_ink, "line_color": None,
                           "fill_color": None, "fill_opacity": 0.4},
            "layer": {"default_text_color": default_ink, "default_line_color": default_ink,
                      "default_fill_color": "#ffffff", "default_fill_opacity": 0.9},
            "fields": {"text_color": True, "line_color": True,
                       "fill_color": True, "fill_opacity": True},
        }})
        # Explicitly chosen on the annotation: same value, but NOT a default, so
        # dark mode must leave it exactly as the user picked it.
        self.assertEqual(out["style"]["text_color"], {"value": default_ink, "isDefault": False})
        # Inherited from a layer that still carries the shared default: lightenable.
        self.assertEqual(out["style"]["line_color"], {"value": default_ink, "isDefault": True})
        self.assertEqual(out["style"]["fill_color"], {"value": "#ffffff", "isDefault": True})
        self.assertEqual(out["style"]["fill_opacity"], {"value": 0.4, "isDefault": False})

    def test_highlight_style_resolves_through_its_own_fields_only(self):
        """A layer with opaque white bubbles must not paint highlights white."""
        out = self._run({"style": {
            "annotation": {"highlight_color": None, "highlight_opacity": None},
            "layer": {"default_fill_color": "#ffffff", "default_fill_opacity": 1.0},
            "fields": {"highlight_color": True, "highlight_opacity": True,
                       "fill_color": True, "fill_opacity": True},
        }})
        self.assertEqual(out["style"]["fill_color"], {"value": "#ffffff", "isDefault": True})
        self.assertEqual(out["style"]["fill_opacity"], {"value": 1.0, "isDefault": False})
        # The layer says nothing about highlights, so the shared highlight
        # defaults apply -- not the bubble fill sitting next to them.
        self.assertEqual(out["style"]["highlight_color"], {"value": "#c9a962", "isDefault": True})
        self.assertEqual(out["style"]["highlight_opacity"], {"value": 0.2, "isDefault": True})

    def test_a_chosen_highlight_colour_overrides_its_layer(self):
        out = self._run({"style": {
            "annotation": {"highlight_color": "#1f6feb"},
            "layer": {"default_highlight_color": "#c9a962", "default_highlight_opacity": 0.5},
            "fields": {"highlight_color": True, "highlight_opacity": True},
        }})
        self.assertEqual(out["style"]["highlight_color"], {"value": "#1f6feb", "isDefault": False})
        # Not overridden on the annotation, so it follows the layer.
        self.assertEqual(out["style"]["highlight_opacity"], {"value": 0.5, "isDefault": False})

    def test_a_chosen_non_default_colour_is_never_treated_as_default(self):
        out = self._run({"style": {
            "annotation": {"text_color": "#b91c1c"},
            "layer": {"default_text_color": "#1f2937"},
            "fields": {"text_color": True},
        }})
        self.assertEqual(out["style"]["text_color"], {"value": "#b91c1c", "isDefault": False})


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class AutomaticHighlightColorTests(_RenderHarnessMixin, unittest.TestCase):
    """A highlight nobody styled picks its own colour.

    Two sources, in order: the persistent colour group the clade's tips already
    belong to, softened for use as a large translucent area; otherwise the next
    palette colour, chosen so successive and neighbouring highlights differ.
    Every case here must be a pure function of the saved state, because the
    editor preview computes the same answer before anything is saved.
    """

    # Six tips as ((A,B),(C,D),(E,F)) so there are three sibling clades to
    # colour and room to nest.
    TREE = [[["A", "B"], ["C", "D"]], ["E", "F"]]
    TIP_ORDER = ["A", "B", "C", "D", "E", "F"]

    PALETTE = ["#3b6fb6", "#7a5aa6", "#2f8f83", "#b8536b",
               "#c08a2e", "#4a8b4a", "#a8503c", "#2b7f9e"]

    def _highlight(self, ident, members, **overrides):
        return _annotation(ident, members=members,
                           annotation_type="clade_highlight", **overrides)

    def test_the_first_unstyled_highlight_gets_a_palette_colour(self):
        out = self._resolve([self._highlight("ann_ab", ["A", "B"])])
        color = out["highlightStyles"][0]["color"]
        self.assertIn(color, self.PALETTE)
        self.assertEqual(color, self.PALETTE[0])

    def test_successive_highlights_get_different_colours(self):
        out = self._resolve([
            self._highlight("ann_ab", ["A", "B"]),
            self._highlight("ann_cd", ["C", "D"]),
            self._highlight("ann_ef", ["E", "F"]),
        ])
        colors = [entry["color"] for entry in out["highlightStyles"]]
        self.assertEqual(len(set(colors)), 3, colors)
        self.assertEqual(out["automaticColors"]["ann_ab"], self.PALETTE[0])
        self.assertEqual(out["automaticColors"]["ann_cd"], self.PALETTE[1])
        self.assertEqual(out["automaticColors"]["ann_ef"], self.PALETTE[2])

    def test_assignment_is_deterministic(self):
        annotations = [
            self._highlight("ann_ab", ["A", "B"]),
            self._highlight("ann_cd", ["C", "D"]),
        ]
        first = self._resolve(annotations)["automaticColors"]
        second = self._resolve(annotations)["automaticColors"]
        self.assertEqual(first, second)

    def test_nested_highlights_do_not_share_a_colour(self):
        out = self._resolve([
            self._highlight("ann_abcd", ["A", "B", "C", "D"]),
            self._highlight("ann_ab", ["A", "B"]),
        ])
        colors = {entry["id"]: entry["color"] for entry in out["highlightStyles"]}
        self.assertNotEqual(colors["ann_abcd"], colors["ann_ab"])

    def test_adjacent_highlights_do_not_share_a_colour_when_the_palette_wraps(self):
        """Nine unstyled highlights on an eight-colour palette must still differ
        from their neighbours where a reuse would land next door."""
        tree = [[["A", "B"], ["C", "D"]], [["E", "F"], ["G", "H"]]]
        order = ["A", "B", "C", "D", "E", "F", "G", "H"]
        pairs = [("A", "B"), ("C", "D"), ("E", "F"), ("G", "H")]
        annotations = []
        for index, pair in enumerate(pairs):
            annotations.append(self._highlight("ann_%d" % index, list(pair)))
        # Four more on the enclosing clades, so the palette is under pressure.
        annotations.append(self._highlight("ann_left", ["A", "B", "C", "D"]))
        annotations.append(self._highlight("ann_right", ["E", "F", "G", "H"]))
        annotations.append(self._highlight("ann_all", list(order)))
        out = self._resolve(annotations, tree=tree, tip_order=order)
        colors = {entry["id"]: entry["color"] for entry in out["highlightStyles"]}
        # Every one of these overlaps or touches ann_all, so none may match it.
        for key in ("ann_0", "ann_1", "ann_2", "ann_3", "ann_left", "ann_right"):
            self.assertNotEqual(colors[key], colors["ann_all"], key)
        # Vertically adjacent siblings differ from each other too.
        self.assertNotEqual(colors["ann_0"], colors["ann_1"])
        self.assertNotEqual(colors["ann_2"], colors["ann_3"])
        self.assertNotEqual(colors["ann_left"], colors["ann_right"])

    def test_a_uniformly_coloured_clade_derives_its_highlight_from_that_group(self):
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"])],
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        color = out["highlightStyles"][0]["color"]
        self.assertNotIn(color, self.PALETTE, "a group colour must not be a palette pick")
        # Same hue family as the group, softened for use as a large area rather
        # than reproduced at full strength.
        self.assertEqual(color, "#397dac")

    def test_a_derived_group_colour_keeps_the_hue_of_its_group(self):
        for group_color, expected_channel in (
            ("#1f77b4", "b"),   # blue: blue channel dominates
            ("#2ca02c", "g"),   # green
            ("#d62728", "r"),   # red
        ):
            with self.subTest(group_color=group_color):
                out = self._resolve(
                    [self._highlight("ann_ab", ["A", "B"])],
                    selection_sets={"Group": ["A", "B"]},
                    selection_set_colors={"Group": group_color},
                    active_selection_set="Group",
                )
                hexed = out["highlightStyles"][0]["color"]
                r, g, b = (int(hexed[i:i + 2], 16) for i in (1, 3, 5))
                channels = {"r": r, "g": g, "b": b}
                self.assertEqual(
                    max(channels, key=channels.get), expected_channel, hexed
                )

    def test_mixed_group_colours_fall_back_to_the_palette(self):
        """18 blue tips and 5 orange ones must not average into a muddy brown."""
        out = self._resolve(
            [self._highlight("ann_abcd", ["A", "B", "C", "D"])],
            selection_sets={"Blues": ["A", "B"], "Oranges": ["C", "D"]},
            selection_set_colors={"Blues": "#1f77b4", "Oranges": "#ff7f0e"},
            active_selection_set="Blues",
        )
        self.assertIn(out["highlightStyles"][0]["color"], self.PALETTE)

    def test_a_partially_coloured_clade_falls_back_to_the_palette(self):
        # Only some members are in the group, so there is no unambiguous clade
        # colour to inherit.
        out = self._resolve(
            [self._highlight("ann_abcd", ["A", "B", "C", "D"])],
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertIn(out["highlightStyles"][0]["color"], self.PALETTE)

    def test_a_parent_and_child_on_the_same_group_may_share_its_hue(self):
        # A legitimate case: both clades are entirely inside one colour group.
        out = self._resolve(
            [
                self._highlight("ann_abcd", ["A", "B", "C", "D"]),
                self._highlight("ann_ab", ["A", "B"]),
            ],
            selection_sets={"Blues": ["A", "B", "C", "D"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        colors = {entry["id"]: entry["color"] for entry in out["highlightStyles"]}
        self.assertEqual(colors["ann_abcd"], colors["ann_ab"])
        self.assertEqual(colors["ann_ab"], "#397dac")

    def test_an_explicit_colour_beats_both_automatic_sources(self):
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"], highlight_color="#b91c1c")],
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertEqual(out["highlightStyles"][0]["color"], "#b91c1c")
        # And it is not handed a palette slot, so it cannot shift anyone else.
        self.assertNotIn("ann_ab", out["automaticColors"])

    def test_a_manual_colour_is_kept_even_when_it_matches_a_neighbour(self):
        """Automatic colours are defaults; they never override an intentional pick."""
        out = self._resolve([
            self._highlight("ann_ab", ["A", "B"], highlight_color="#3b6fb6"),
            self._highlight("ann_cd", ["C", "D"], highlight_color="#3b6fb6"),
        ])
        colors = [entry["color"] for entry in out["highlightStyles"]]
        self.assertEqual(colors, ["#3b6fb6", "#3b6fb6"])

    def test_a_layer_colour_the_user_chose_applies_to_its_highlights(self):
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"])],
            layers=[_layer(default_highlight_color="#334455")],
        )
        self.assertEqual(out["highlightStyles"][0]["color"], "#334455")
        self.assertNotIn("ann_ab", out["automaticColors"])

    def test_a_layer_still_carrying_the_old_gold_default_is_automatic(self):
        """Backwards compatibility with layers saved before automatic colours.

        Those layers stored the shared default gold. Treating that as a
        deliberate choice would make every old highlight gold again, so a layer
        that still carries the shared default counts as untouched -- the same
        inheritance test the dark-mode ink decision already uses.
        """
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"])],
            layers=[_layer(default_highlight_color="#c9a962")],
        )
        self.assertEqual(out["highlightStyles"][0]["color"], self.PALETTE[0])

    def test_an_unresolvable_highlight_still_holds_its_colour_slot(self):
        """A reroot must not renumber the colours of everything after it."""
        stable = self._resolve([
            self._highlight("ann_bc", ["B", "C"]),   # not a clade in this tree
            self._highlight("ann_ef", ["E", "F"]),
        ])
        self.assertFalse(stable["validity"]["ann_bc"]["valid"])
        # ann_ef is still the SECOND automatic highlight and keeps the second colour.
        self.assertEqual(stable["automaticColors"]["ann_ef"], self.PALETTE[1])
        self.assertEqual(stable["highlightStyles"][0]["id"], "ann_ef")
        self.assertEqual(stable["highlightStyles"][0]["color"], self.PALETTE[1])

    def test_a_hidden_layer_also_holds_its_slot(self):
        out = self._resolve(
            [
                self._highlight("ann_ab", ["A", "B"], layer_id="layer_hidden"),
                self._highlight("ann_ef", ["E", "F"]),
            ],
            layers=[_layer("layer_hidden", "Hidden", order=1, visible=False),
                    _layer("layer_a", "Sections", order=2)],
        )
        self.assertEqual(out["automaticColors"]["ann_ef"], self.PALETTE[1])

    def test_rotating_children_does_not_change_the_assigned_colours(self):
        annotations = [
            self._highlight("ann_ab", ["A", "B"]),
            self._highlight("ann_cd", ["C", "D"]),
        ]
        upright = self._resolve(annotations)["automaticColors"]
        rotated = self._resolve(
            annotations,
            tree=[[["D", "C"], ["B", "A"]], ["F", "E"]],
            tip_order=["D", "C", "B", "A", "F", "E"],
        )["automaticColors"]
        self.assertEqual(upright, rotated)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class HighlightColorClashTests(_RenderHarnessMixin, unittest.TestCase):
    """Colours that are already decided still take part in clash avoidance.

    A group-derived colour and a manually chosen one do not consume a palette
    slot -- the palette is for clades with no colour of their own -- but they
    used to be invisible to the neighbour rule entirely, so a clade softening a
    persistent blue group to #397dac could sit directly beside an automatic
    clade handed the first palette blue #3b6fb6. As translucent washes those are
    one colour.
    """

    TREE = [[["A", "B"], ["C", "D"]], ["E", "F"]]
    TIP_ORDER = ["A", "B", "C", "D", "E", "F"]
    BLUE = "#3b6fb6"

    def _highlight(self, ident, members, **overrides):
        return _annotation(ident, members=members,
                           annotation_type="clade_highlight", **overrides)

    def _colors(self, out):
        return {entry["id"]: entry["color"] for entry in out["highlightStyles"]}

    def test_a_derived_blue_blocks_an_adjacent_automatic_blue(self):
        out = self._resolve(
            [
                self._highlight("ann_ab", ["A", "B"]),
                self._highlight("ann_cd", ["C", "D"]),
            ],
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        colors = self._colors(out)
        self.assertEqual(colors["ann_ab"], "#397dac")
        # The neighbour must not be handed the palette blue that reads the same.
        self.assertNotEqual(colors["ann_cd"], self.BLUE)
        self.assertIn(colors["ann_cd"], HighlightColorClashTests._palette())

    @staticmethod
    def _palette():
        return ["#3b6fb6", "#7a5aa6", "#2f8f83", "#b8536b",
                "#c08a2e", "#4a8b4a", "#a8503c", "#2b7f9e"]

    def test_a_derived_green_does_not_block_a_sufficiently_different_colour(self):
        """Avoidance must not become "never reuse anything"."""
        out = self._resolve(
            [
                self._highlight("ann_ab", ["A", "B"]),
                self._highlight("ann_cd", ["C", "D"]),
            ],
            selection_sets={"Greens": ["A", "B"]},
            selection_set_colors={"Greens": "#2ca02c"},
            active_selection_set="Greens",
        )
        colors = self._colors(out)
        self.assertEqual(colors["ann_ab"], "#39ac39")
        # Nothing about a green neighbour should stop the first palette blue.
        self.assertEqual(colors["ann_cd"], self.BLUE)

    def test_a_manual_colour_is_respected_but_still_warns_off_its_neighbour(self):
        out = self._resolve([
            self._highlight("ann_ab", ["A", "B"], highlight_color="#3b6fb6"),
            self._highlight("ann_cd", ["C", "D"]),
        ])
        colors = self._colors(out)
        # Never altered ...
        self.assertEqual(colors["ann_ab"], "#3b6fb6")
        # ... and the automatic neighbour picks something else.
        self.assertNotEqual(colors["ann_cd"], "#3b6fb6")

    def test_a_fixed_colour_does_not_consume_a_palette_slot(self):
        """A distant automatic highlight may still take the first palette colour."""
        out = self._resolve([
            self._highlight("ann_ab", ["A", "B"], highlight_color="#b91c1c"),
            self._highlight("ann_ef", ["E", "F"]),
        ])
        self.assertEqual(self._colors(out)["ann_ef"], self.BLUE)
        self.assertNotIn("ann_ab", out["automaticSlots"])

    def test_a_distant_clash_is_allowed(self):
        # ann_ab and ann_ef do not touch in tip order, so the blue group next to
        # one of them says nothing about the other.
        out = self._resolve(
            [
                self._highlight("ann_ab", ["A", "B"]),
                self._highlight("ann_ef", ["E", "F"]),
            ],
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertEqual(self._colors(out)["ann_ef"], self.BLUE)

    def test_nested_automatic_highlights_still_differ(self):
        out = self._resolve([
            self._highlight("ann_abcd", ["A", "B", "C", "D"]),
            self._highlight("ann_ab", ["A", "B"]),
        ])
        colors = self._colors(out)
        self.assertNotEqual(colors["ann_abcd"], colors["ann_ab"])

    def test_clash_avoidance_is_deterministic(self):
        annotations = [
            self._highlight("ann_ab", ["A", "B"]),
            self._highlight("ann_cd", ["C", "D"]),
            self._highlight("ann_ef", ["E", "F"]),
        ]
        kwargs = {
            "selection_sets": {"Blues": ["A", "B"]},
            "selection_set_colors": {"Blues": "#1f77b4"},
            "active_selection_set": "Blues",
        }
        first = self._colors(self._resolve(annotations, **kwargs))
        second = self._colors(self._resolve(annotations, **kwargs))
        self.assertEqual(first, second)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class PersistentGroupHighlightRefreshTests(_RenderHarnessMixin, unittest.TestCase):
    """Automatic highlights follow their colour group as the group changes.

    The resolution is a pure function of the current group state, so each case
    here is the state before and after one mutation. The other half of the fix --
    that the mutation actually schedules a repaint -- is pinned by
    PersistentColorRefreshWiringTests below.
    """

    TREE = [[["A", "B"], ["C", "D"]], ["E", "F"]]
    TIP_ORDER = ["A", "B", "C", "D", "E", "F"]

    def _highlight(self, ident, members):
        return _annotation(ident, members=members, annotation_type="clade_highlight")

    def _color(self, **kwargs):
        out = self._resolve([self._highlight("ann_ab", ["A", "B"])], **kwargs)
        return out["highlightStyles"][0]["color"]

    def test_adding_a_clade_to_a_group_adopts_that_hue(self):
        before = self._color()
        after = self._color(
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertEqual(before, "#3b6fb6")
        self.assertEqual(after, "#397dac")

    def test_changing_a_group_colour_recomputes_the_highlight(self):
        blue = self._color(
            selection_sets={"Group": ["A", "B"]},
            selection_set_colors={"Group": "#1f77b4"},
            active_selection_set="Group",
        )
        red = self._color(
            selection_sets={"Group": ["A", "B"]},
            selection_set_colors={"Group": "#d62728"},
            active_selection_set="Group",
        )
        self.assertEqual(blue, "#397dac")
        self.assertEqual(red, "#ac393a")

    def test_removing_a_tip_from_the_group_falls_back_to_the_palette(self):
        partial = self._color(
            selection_sets={"Blues": ["A"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertEqual(partial, "#3b6fb6")

    def test_deleting_the_group_falls_back_to_the_palette(self):
        gone = self._color(selection_sets={}, selection_set_colors={})
        self.assertEqual(gone, "#3b6fb6")

    def test_group_precedence_follows_the_active_group(self):
        """A tip in two groups takes the active one's colour, and so does the band."""
        common = {
            "selection_sets": {"Blues": ["A", "B"], "Reds": ["A", "B"]},
            "selection_set_colors": {"Blues": "#1f77b4", "Reds": "#d62728"},
        }
        self.assertEqual(self._color(active_selection_set="Blues", **common), "#397dac")
        self.assertEqual(self._color(active_selection_set="Reds", **common), "#ac393a")

    def test_a_fixed_colour_ignores_the_group_entirely(self):
        out = self._resolve(
            [_annotation("ann_ab", members=["A", "B"],
                         annotation_type="clade_highlight", highlight_color="#b91c1c")],
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertEqual(out["highlightStyles"][0]["color"], "#b91c1c")


class PersistentColorRefreshWiringTests(unittest.TestCase):
    """Every persistent-group mutation must repaint the annotations too.

    Group membership, precedence and colour feed both the tip labels and the
    automatic highlight colour, but only the labels were being repainted, so a
    recoloured group left its band showing the previous colour until something
    unrelated forced an annotation redraw.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    # Every method that can change persistent group membership, precedence or colour.
    MUTATORS = (
        "removeIdsFromSelectionSets",
        "addCurrentSelectionToActiveColorGroup",
        "clearCurrentSelectionColorGroups",
        "deleteSelectionSet",
        "setActiveSelectionSet",
        "setSelectionSetColor",
        "restoreSelectionSets",
    )

    @classmethod
    def setUpClass(cls):
        cls.source = cls.VIEWER_JS.read_text(encoding="utf-8")

    def _body(self, name):
        """The source of one method, up to the start of the next one.

        The file declares an abstract stub for several of these before the real
        class, so take the longest match rather than the first: an empty `{ }`
        stub would let every assertion below pass vacuously.
        """
        bodies = []
        for match in re.finditer(r"\n        %s\(" % re.escape(name), self.source):
            rest = self.source[match.start() + 1:]
            end = re.search(r"\n        [A-Za-z_][A-Za-z0-9_]*\(", rest)
            bodies.append(rest[: end.start()] if end else rest)
        self.assertTrue(bodies, "no method named %s in the viewer" % name)
        return max(bodies, key=len)

    def test_the_refresh_helper_repaints_both_labels_and_annotations(self):
        body = self._body("_refreshPersistentColorVisuals")
        self.assertIn("_updateNodeStylesOnly()", body)
        self.assertIn("_scheduleAnnotationRedraw()", body)

    def test_every_group_mutation_goes_through_the_helper(self):
        for name in self.MUTATORS:
            with self.subTest(method=name):
                body = self._body(name)
                self.assertIn(
                    "_refreshPersistentColorVisuals()", body,
                    "%s changes persistent group state but does not refresh "
                    "automatic highlights" % name,
                )
                # And does not also do the label-only repaint it replaced.
                self.assertNotIn("this._updateNodeStylesOnly();", body)

    def test_ordinary_transient_selection_still_only_repaints_labels(self):
        """The helper is for persistent groups; temporary selection is unaffected."""
        body = self._body("clearActiveSelection")
        self.assertIn("this._updateNodeStylesOnly();", body)
        self.assertNotIn("_refreshPersistentColorVisuals()", body)

    def test_the_redraw_is_the_debounced_one_not_a_tree_rebuild(self):
        body = self._body("_refreshPersistentColorVisuals")
        for expensive in ("render(", "_draw()", "_cacheNodes("):
            self.assertNotIn(expensive, body)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class StableAutomaticHighlightColorTests(_RenderHarnessMixin, unittest.TestCase):
    """A published highlight keeps its colour when its neighbours come and go.

    The palette colour used to be derived from the annotation's position among
    the automatic highlights, so deleting one silently recoloured every highlight
    after it. The slot is now pinned on the annotation when it is first saved.
    """

    TREE = [[["A", "B"], ["C", "D"]], [["E", "F"], ["G", "H"]]]
    TIP_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H"]
    PALETTE = ["#3b6fb6", "#7a5aa6", "#2f8f83", "#b8536b",
               "#c08a2e", "#4a8b4a", "#a8503c", "#2b7f9e"]

    def _saved(self, ident, members, slot):
        return _annotation(ident, members=members, annotation_type="clade_highlight",
                           automatic_highlight_slot=slot)

    def _colors(self, annotations, **kwargs):
        out = self._resolve(annotations, **kwargs)
        return {entry["id"]: entry["color"] for entry in out["highlightStyles"]}

    def _four(self):
        return [
            self._saved("ann_a", ["A", "B"], 0),
            self._saved("ann_b", ["C", "D"], 1),
            self._saved("ann_c", ["E", "F"], 2),
            self._saved("ann_d", ["G", "H"], 3),
        ]

    def test_saved_slots_are_honoured_in_order(self):
        colors = self._colors(self._four())
        self.assertEqual(
            [colors["ann_a"], colors["ann_b"], colors["ann_c"], colors["ann_d"]],
            self.PALETTE[:4],
        )

    def test_deleting_an_earlier_highlight_does_not_recolour_the_rest(self):
        before = self._colors(self._four())
        after = self._colors([a for a in self._four() if a["id"] != "ann_b"])
        for key in ("ann_a", "ann_c", "ann_d"):
            self.assertEqual(before[key], after[key], key)

    def test_inserting_a_highlight_does_not_recolour_the_existing_ones(self):
        before = self._colors(self._four())
        # A new one arrives with no slot yet and is assigned around the others.
        extra = self._four() + [
            _annotation("ann_new", members=["A", "B", "C", "D"],
                        annotation_type="clade_highlight")
        ]
        after = self._colors(extra)
        for key in ("ann_a", "ann_b", "ann_c", "ann_d"):
            self.assertEqual(before[key], after[key], key)
        self.assertIn(after["ann_new"], self.PALETTE)

    def test_a_new_highlight_avoids_its_neighbours_saved_colours(self):
        extra = self._four() + [
            _annotation("ann_new", members=["A", "B", "C", "D"],
                        annotation_type="clade_highlight")
        ]
        colors = self._colors(extra)
        self.assertNotIn(colors["ann_new"], [colors["ann_a"], colors["ann_b"]])

    def test_hiding_a_layer_does_not_recolour_anything(self):
        annotations = self._four()
        annotations[0] = dict(annotations[0], layer_id="layer_hidden")
        visible = self._colors(
            annotations,
            layers=[_layer("layer_hidden", "Hidden", order=1),
                    _layer("layer_a", "Sections", order=2)],
        )
        hidden = self._colors(
            annotations,
            layers=[_layer("layer_hidden", "Hidden", order=1, visible=False),
                    _layer("layer_a", "Sections", order=2)],
        )
        for key in ("ann_b", "ann_c", "ann_d"):
            self.assertEqual(visible[key], hidden[key], key)

    def test_rotating_children_does_not_recolour_anything(self):
        upright = self._colors(self._four())
        rotated = self._colors(
            self._four(),
            tree=[[["D", "C"], ["B", "A"]], [["H", "G"], ["F", "E"]]],
            tip_order=["D", "C", "B", "A", "H", "G", "F", "E"],
        )
        self.assertEqual(upright, rotated)

    def test_an_unresolvable_highlight_does_not_recolour_the_others(self):
        annotations = self._four() + [
            self._saved("ann_broken", ["B", "C"], 4)  # not a clade in this tree
        ]
        colors = self._colors(annotations)
        for index, key in enumerate(("ann_a", "ann_b", "ann_c", "ann_d")):
            self.assertEqual(colors[key], self.PALETTE[index], key)

    def test_a_saved_slot_still_yields_to_the_clade_colour_group(self):
        """Pinning the slot must not stop a highlight following its group."""
        colors = self._colors(
            self._four(),
            selection_sets={"Blues": ["A", "B"]},
            selection_set_colors={"Blues": "#1f77b4"},
            active_selection_set="Blues",
        )
        self.assertEqual(colors["ann_a"], "#397dac")
        # And the others keep their own slots regardless.
        self.assertEqual(colors["ann_b"], self.PALETTE[1])

    def test_slots_beyond_the_palette_wrap_deterministically(self):
        colors = self._colors([
            self._saved("ann_a", ["A", "B"], 0),
            self._saved("ann_wrapped", ["E", "F"], 8),
        ])
        self.assertEqual(colors["ann_wrapped"], self.PALETTE[0])

    def test_a_reserved_slot_matches_the_colour_the_preview_showed(self):
        out = self._resolve(
            self._four(),
            reserve_slot_for={"id": "ann_new", "memberIds": ["A", "B", "C", "D"]},
        )
        slot = out["reservedSlot"]
        self.assertIsInstance(slot, int)
        # Saving with that slot must produce the colour the editor previewed.
        saved = self._colors(self._four() + [self._saved(
            "ann_new", ["A", "B", "C", "D"], slot)])
        preview = self._colors(self._four() + [
            _annotation("ann_new", members=["A", "B", "C", "D"],
                        annotation_type="clade_highlight")
        ])
        self.assertEqual(saved["ann_new"], preview["ann_new"])

    def test_legacy_annotations_without_slots_are_still_assigned(self):
        colors = self._colors([
            _annotation("ann_a", members=["A", "B"], annotation_type="clade_highlight"),
            _annotation("ann_b", members=["E", "F"], annotation_type="clade_highlight"),
        ])
        self.assertEqual(colors["ann_a"], self.PALETTE[0])
        self.assertEqual(colors["ann_b"], self.PALETTE[1])


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class HighlightColorModeTests(_RenderHarnessMixin, unittest.TestCase):
    """Automatic is a stored choice, not a colour value that happens to be gold.

    Reading "the value is still #c9a962" as "automatic" meant a user could never
    deliberately keep that gold. The mode is now explicit, and inferred only for
    state saved before it existed.
    """

    TREE = [["A", "B"], ["C", "D"]]
    TIP_ORDER = ["A", "B", "C", "D"]
    GOLD = "#c9a962"
    BLUE = "#3b6fb6"

    def _color(self, annotation_overrides=None, layer_overrides=None):
        out = self._resolve(
            [_annotation("ann_ab", members=["A", "B"],
                         annotation_type="clade_highlight",
                         **(annotation_overrides or {}))],
            layers=[_layer(**(layer_overrides or {}))],
        )
        return out["highlightStyles"][0]["color"]

    def test_a_legacy_gold_layer_with_no_mode_is_automatic(self):
        self.assertEqual(
            self._color(layer_overrides={"default_highlight_color": self.GOLD}),
            self.BLUE,
        )

    def test_a_layer_that_explicitly_fixes_the_gold_keeps_it(self):
        """The requirement the sentinel used to make impossible."""
        self.assertEqual(
            self._color(layer_overrides={
                "default_highlight_color": self.GOLD,
                "default_highlight_color_mode": "fixed",
            }),
            self.GOLD,
        )

    def test_an_annotation_that_explicitly_fixes_the_gold_keeps_it(self):
        self.assertEqual(
            self._color(annotation_overrides={
                "highlight_color": self.GOLD,
                "highlight_color_mode": "fixed",
            }),
            self.GOLD,
        )

    def test_an_annotation_colour_alone_is_still_a_fixed_choice(self):
        # Older annotations carry a colour and no mode.
        self.assertEqual(
            self._color(annotation_overrides={"highlight_color": self.GOLD}),
            self.GOLD,
        )

    def test_an_auto_annotation_overrides_a_fixed_layer(self):
        self.assertEqual(
            self._color(
                annotation_overrides={"highlight_color_mode": "auto"},
                layer_overrides={"default_highlight_color": "#334455",
                                 "default_highlight_color_mode": "fixed"},
            ),
            self.BLUE,
        )

    def test_a_fixed_annotation_with_no_colour_takes_the_layer_colour(self):
        self.assertEqual(
            self._color(
                annotation_overrides={"highlight_color_mode": "fixed"},
                layer_overrides={"default_highlight_color": "#334455"},
            ),
            "#334455",
        )

    def test_a_layer_marked_auto_ignores_the_colour_it_still_carries(self):
        # The layer control keeps the last fixed colour so switching back restores
        # it; while the mode says auto it must not be drawn.
        self.assertEqual(
            self._color(layer_overrides={
                "default_highlight_color": "#334455",
                "default_highlight_color_mode": "auto",
            }),
            self.BLUE,
        )

    def test_an_inheriting_annotation_follows_a_fixed_layer(self):
        self.assertEqual(
            self._color(layer_overrides={
                "default_highlight_color": "#334455",
                "default_highlight_color_mode": "fixed",
            }),
            "#334455",
        )

    def test_a_fixed_highlight_keeps_the_automatic_theme_opacity(self):
        """Fixing the colour says nothing about the opacity."""
        for dark, expected in ((False, 0.2), (True, 0.26)):
            with self.subTest(dark=dark):
                out = self._resolve(
                    [_annotation("ann_ab", members=["A", "B"],
                                 annotation_type="clade_highlight",
                                 highlight_color=self.GOLD)],
                    dark=dark,
                )
                self.assertEqual(out["highlightStyles"][0]["opacity"], expected)

    def test_an_automatic_highlight_with_a_saved_slot_is_still_automatic(self):
        """Pinning the slot must not turn it into a fixed colour."""
        out = self._resolve([_annotation(
            "ann_ab", members=["A", "B"], annotation_type="clade_highlight",
            automatic_highlight_slot=1)], dark=True)
        self.assertEqual(out["highlightStyles"][0]["color"], "#7a5aa6")
        # Still takes the dark-mode automatic opacity, not a fixed one.
        self.assertEqual(out["highlightStyles"][0]["opacity"], 0.26)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class HighlightThemeAndExportParityTests(_RenderHarnessMixin, unittest.TestCase):
    """What is on screen is what lands in the exported SVG.

    The effective opacity used to come partly from a dark-mode CSS rule, which
    the export path -- a clone of the live SVG with no page stylesheet -- could
    not see, so a default highlight exported paler than it looked.
    """

    def _highlight(self, ident, members, **overrides):
        return _annotation(ident, members=members,
                           annotation_type="clade_highlight", **overrides)

    def test_the_automatic_opacity_is_stronger_in_dark_mode(self):
        light = self._resolve([self._highlight("ann_ab", ["A", "B"])], dark=False)
        dark = self._resolve([self._highlight("ann_ab", ["A", "B"])], dark=True)
        light_opacity = light["highlightStyles"][0]["opacity"]
        dark_opacity = dark["highlightStyles"][0]["opacity"]
        self.assertGreater(dark_opacity, light_opacity)
        # Visible without dominating, in both themes.
        self.assertGreaterEqual(light_opacity, 0.15)
        self.assertLessEqual(dark_opacity, 0.35)

    def test_both_themes_resolve_to_a_concrete_inline_value(self):
        """Not a class for CSS to reinterpret: the number itself, ready to export."""
        for dark in (False, True):
            with self.subTest(dark=dark):
                out = self._resolve([self._highlight("ann_ab", ["A", "B"])], dark=dark)
                style = out["highlightStyles"][0]
                self.assertIsInstance(style["opacity"], float)
                self.assertRegex(style["color"], r"^#[0-9a-f]{6}$")

    def test_an_explicit_opacity_is_never_altered_by_the_theme(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                out = self._resolve(
                    [self._highlight("ann_ab", ["A", "B"], highlight_opacity=0.35)],
                    dark=dark,
                )
                self.assertEqual(out["highlightStyles"][0]["opacity"], 0.35)

    def test_an_explicit_zero_opacity_is_respected_rather_than_read_as_unset(self):
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"], highlight_opacity=0)], dark=True
        )
        self.assertEqual(out["highlightStyles"][0]["opacity"], 0)

    def test_a_layer_opacity_the_user_chose_is_not_altered_by_the_theme(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                out = self._resolve(
                    [self._highlight("ann_ab", ["A", "B"])],
                    layers=[_layer(default_highlight_opacity=0.4)],
                    dark=dark,
                )
                self.assertEqual(out["highlightStyles"][0]["opacity"], 0.4)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class UnaryNodeHighlightTests(_RenderHarnessMixin, unittest.TestCase):
    """A unary chain gives several nodes the same descendant set.

    The band starts at its clade's internal node, so of the nodes sharing one
    descendant block it has to use the DEEPEST -- the one drawn nearest the
    clade. Setting the map entry on the way back up the traversal put the
    shallowest node back instead, starting the band at the top of the chain and
    dragging it left across the whole unary run.
    """

    def _highlight(self, ident, members):
        return _annotation(ident, members=members, annotation_type="clade_highlight")

    def test_the_deepest_node_of_a_unary_chain_backs_the_band(self):
        # root -> knuckle -> knuckle -> (A,B); plus a sibling so the root is not
        # itself the annotated clade.
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"])],
            tree=[[[["A", "B"]]], "C"],
            tip_order=["A", "B", "C"],
        )
        self.assertTrue(out["validity"]["ann_ab"]["valid"])
        self.assertEqual(out["highlightNodeSpans"], ["A,B"])
        # root(0) -> knuckle(1) -> knuckle(2) -> (A,B)(3). The deepest node
        # carrying the block {A,B} is at depth 3.
        self.assertEqual(out["highlightNodeDepths"], [3])

    def test_a_unary_root_resolves_to_the_deepest_node_too(self):
        out = self._resolve(
            [self._highlight("ann_all", ["A", "B"])],
            tree=[[["A", "B"]]],
            tip_order=["A", "B"],
        )
        self.assertTrue(out["validity"]["ann_all"]["valid"])
        self.assertEqual(out["highlightNodeDepths"], [2])

    def test_a_unary_parent_of_a_single_tip_resolves_to_the_tip(self):
        out = self._resolve(
            [self._highlight("ann_a", ["A"])],
            tree=[[["A"]], "B"],
            tip_order=["A", "B"],
        )
        self.assertTrue(out["validity"]["ann_a"]["valid"])
        self.assertEqual(out["highlightNodeSpans"], ["A"])
        # root(0) -> knuckle(1) -> knuckle(2) -> A(3)
        self.assertEqual(out["highlightNodeDepths"], [3])

    def test_ordinary_clade_resolution_is_unchanged_by_the_fix(self):
        out = self._resolve(
            [self._highlight("ann_ab", ["A", "B"])],
            tree=[["A", "B"], ["C", "D"]],
            tip_order=["A", "B", "C", "D"],
        )
        self.assertEqual(out["highlightNodeSpans"], ["A,B"])
        # No unary nodes: the clade's own node is one hop below the root.
        self.assertEqual(out["highlightNodeDepths"], [1])
        self.assertEqual(
            out["blocks"], ["0:0", "0:1", "0:3", "1:1", "2:2", "2:3", "3:3"]
        )


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class AnnotationLanePlacementTests(unittest.TestCase):
    """Where the annotation lane starts, relative to the tip labels.

    The lane used to be placed off the bounding box of the whole leaf ``<g>``.
    That box is the union of everything phylotree left in the group -- the node
    bubble, and the branch-tracer line it runs out to ``right_most_leaf`` for
    aligned tips and then never removes -- so the measured edge could sit far
    beyond the text and the figure came out with a broad empty band between the
    sequence labels and the bracket.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _run(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def _leaf(node_x, y, width, ident="tip", group_width=None, **extra):
        """One leaf whose label is `width` wide, in a group `group_width` wide."""
        spec = {
            "id": ident,
            "nodeX": node_x,
            "y": y,
            "label": {"x": 0, "y": -5, "width": width, "height": 10},
            "group": {"x": 0, "y": -5,
                      "width": group_width if group_width is not None else width,
                      "height": 10},
        }
        spec.update(extra)
        return spec

    def test_the_edge_comes_from_the_label_not_the_rest_of_the_group(self):
        """The regression itself: a wide tracer line must not move the lane."""
        out = self._run({"tipLabelEdges": [{
            "nodeX": 100,
            "label": {"x": 2, "y": -5, "width": 60, "height": 10},
            # What the group bbox would have reported, with a tracer running back
            # across the tree and far past the text.
            "group": {"x": -400, "y": -5, "width": 500, "height": 10},
        }]})
        self.assertEqual(out["tipLabelEdges"], [162])

    def test_a_label_carrying_its_own_transform_is_measured_where_it_is_drawn(self):
        # Tip alignment translates the label out to the right; getBBox reports a
        # child's geometry before its own transform, so that shift is added back.
        out = self._run({"tipLabelEdges": [{
            "nodeX": 100,
            "label": {"x": 0, "y": -5, "width": 60, "height": 10},
            "labelTransform": 40,
            "group": {"x": 0, "y": -5, "width": 100, "height": 10},
        }]})
        self.assertEqual(out["tipLabelEdges"], [200])

    def test_it_falls_back_to_the_group_when_there_is_no_drawn_label(self):
        out = self._run({"tipLabelEdges": [
            {"nodeX": 100, "label": None,
             "group": {"x": 0, "y": -5, "width": 30, "height": 10}},
            # A label element with nothing rendered in it is not a measurement.
            {"nodeX": 100, "label": {"x": 0, "y": 0, "width": 0, "height": 0},
             "group": {"x": 0, "y": -5, "width": 30, "height": 10}},
        ]})
        self.assertEqual(out["tipLabelEdges"], [130, 130])

    def test_a_leaf_with_no_measurable_geometry_falls_back_to_its_branch_tip(self):
        out = self._run({"tipLabelEdges": [
            {"nodeX": 100, "label": None, "group": None},
        ]})
        self.assertEqual(out["tipLabelEdges"], [100])

    def test_ordinary_tip_names_leave_only_a_modest_gap(self):
        """No hundreds of pixels of empty figure before the bracket."""
        out = self._run({"geometryLeaves": [
            self._leaf(300, 10, 80, "A", group_width=520),
            self._leaf(280, 30, 60, "B", group_width=540),
            self._leaf(310, 50, 70, "C", group_width=510),
        ]})
        geometry = out["geometry"]
        # Longest DRAWN label edge: tip A at 300 + 80.
        self.assertEqual(geometry["labelRight"], 380)
        gap = geometry["laneStart"] - geometry["labelRight"]
        self.assertGreaterEqual(gap, 15)
        self.assertLessEqual(gap, 30)

    def test_long_tip_names_push_the_lane_right_by_exactly_their_extra_width(self):
        short = self._run({"geometryLeaves": [
            self._leaf(300, 10, 80, "A"), self._leaf(300, 30, 60, "B"),
        ]})["geometry"]
        long = self._run({"geometryLeaves": [
            self._leaf(300, 10, 380, "A"), self._leaf(300, 30, 60, "B"),
        ]})["geometry"]
        self.assertEqual(long["labelRight"] - short["labelRight"], 300)
        # The gap after the longest label is the same either way -- long names get
        # the room they need, they do not get a bigger gap as well.
        self.assertEqual(long["laneStart"] - long["labelRight"],
                         short["laneStart"] - short["labelRight"])

    def test_the_gap_holds_its_apparent_size_as_the_tree_is_zoomed(self):
        """Annotation text is held at constant screen size, so gaps scale with 1/k."""
        for zoom in (0.25, 1, 4):
            with self.subTest(zoom=zoom):
                out = self._run({
                    "zoom": zoom,
                    "geometryLeaves": [self._leaf(300, 10, 80, "A"),
                                       self._leaf(300, 30, 60, "B")],
                })["geometry"]
                gap = out["laneStart"] - out["labelRight"]
                # Same number of SCREEN pixels at every zoom level.
                self.assertAlmostEqual(gap * zoom, 18)

    def test_lanes_stack_left_to_right_without_overlapping(self):
        metrics = self._run({"geometryLeaves": [
            self._leaf(300, 10, 80, "A"), self._leaf(300, 30, 60, "B"),
        ]})["geometry"]["metrics"]
        # A second lane clears the first by its full width plus the lane gap, so
        # multiple layers keep stacking outward rather than drawing on top of
        # each other.
        self.assertGreater(metrics["GAP_BETWEEN_LANES"], 0)
        self.assertGreater(metrics["GAP_FROM_TREE"], metrics["LINE_TO_TEXT_GAP"])

    def test_tip_order_and_row_pitch_still_come_from_the_rendered_rows(self):
        geometry = self._run({"geometryLeaves": [
            self._leaf(300, 50, 40, "C"),
            self._leaf(300, 10, 40, "A"),
            self._leaf(300, 30, 40, "B"),
        ]})["geometry"]
        self.assertEqual(geometry["tipOrder"], ["A", "B", "C"])
        self.assertEqual(geometry["rowPitch"], 20)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class CladeHighlightRectAttributeTests(unittest.TestCase):
    """What the shared band primitive actually writes onto the rect."""

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _draw(self, rect=None, effective=None, annotation_id="ann_a"):
        payload = {"drawHighlightRect": {
            "rect": rect or {"x": 10, "y": 20, "width": 300, "height": 80},
            "effective": effective or {"color": "#3b6fb6", "opacity": 0.2},
            "annotationId": annotation_id,
        }}
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["drawnRect"]

    def test_corners_are_square(self):
        """A rounded band reads as a UI card, not as a region of a figure."""
        drawn = self._draw()
        self.assertNotIn("rx", drawn["attrs"])
        self.assertNotIn("ry", drawn["attrs"])
        self.assertNotIn("rx", drawn["styles"])

    def test_the_rectangle_is_drawn_at_the_geometry_it_was_given(self):
        drawn = self._draw(rect={"x": 10, "y": 20, "width": 300, "height": 80})
        self.assertEqual(drawn["attrs"]["x"], 10)
        self.assertEqual(drawn["attrs"]["y"], 20)
        self.assertEqual(drawn["attrs"]["width"], 300)
        self.assertEqual(drawn["attrs"]["height"], 80)

    def test_colour_and_opacity_are_written_inline_so_they_export(self):
        drawn = self._draw(effective={"color": "#7a5aa6", "opacity": 0.26})
        self.assertEqual(drawn["styles"]["fill"], "#7a5aa6")
        self.assertEqual(drawn["styles"]["fill-opacity"], 0.26)

    def test_there_is_no_border_and_no_pointer_target(self):
        drawn = self._draw()
        self.assertEqual(drawn["styles"]["stroke"], "none")
        # A band spans a whole clade; if it took pointer events it would swallow
        # every right-click meant for the branches underneath it.
        self.assertEqual(drawn["styles"]["pointer-events"], "none")

    def test_the_band_is_tagged_with_its_annotation(self):
        self.assertEqual(self._draw()["attrs"]["data-annotation-id"], "ann_a")
        # The editor preview draws an unsaved annotation and passes no id.
        self.assertNotIn("data-annotation-id", self._draw(annotation_id=None)["attrs"])


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class NestedHighlightRightEdgeTests(unittest.TestCase):
    """Bands in one layer end flush rather than each stopping at its own label.

    Each band is first sized to clear its own annotation text, which left nested
    highlights ragged: a child clade whose name happened to be longer than its
    parent's stuck out past the parent band, reading as the child escaping the
    group that contains it.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _align(self, specs):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps({"alignRightEdges": specs}),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return {entry["id"]: entry["right"]
                for entry in json.loads(result.stdout)["alignedRightEdges"]}

    def test_a_child_no_longer_sticks_out_past_its_parent(self):
        edges = self._align([
            {"id": "parent", "right": 400, "layerId": "layer_a"},
            {"id": "child", "right": 560, "layerId": "layer_a"},
        ])
        self.assertEqual(edges["parent"], edges["child"])

    def test_the_shared_edge_clears_the_longest_label(self):
        """Flushing must widen the short ones, never clip the long one."""
        edges = self._align([
            {"id": "short", "right": 400, "layerId": "layer_a"},
            {"id": "long", "right": 560, "layerId": "layer_a"},
            {"id": "middle", "right": 480, "layerId": "layer_a"},
        ])
        self.assertEqual(set(edges.values()), {560})

    def test_separate_layers_keep_their_own_edges(self):
        # Layers are drawn as separate columns further and further right; pulling
        # a layer-1 band out to a layer-2 label would smear it across the column
        # in between.
        edges = self._align([
            {"id": "inner", "right": 400, "layerId": "layer_a"},
            {"id": "outer", "right": 700, "layerId": "layer_b"},
        ])
        self.assertEqual(edges["inner"], 400)
        self.assertEqual(edges["outer"], 700)

    def test_a_band_with_no_lane_is_left_alone(self):
        edges = self._align([
            {"id": "placed", "right": 400, "layerId": "layer_a"},
            {"id": "unplaced", "right": None, "layerId": "layer_b"},
        ])
        self.assertEqual(edges["placed"], 400)
        self.assertIsNone(edges["unplaced"])


class HighlightStylesheetTests(unittest.TestCase):
    """No themed highlight styling may live in CSS.

    Export clones the live SVG without the page stylesheet, so a themed CSS rule
    makes the exported figure differ from the one the user was looking at. The
    renderer resolves the effective colour and opacity and writes both inline.
    """

    CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "tree_viewer.css"

    def test_no_dark_mode_override_of_the_highlight_fill(self):
        css = self.CSS.read_text(encoding="utf-8")
        self.assertIn("clade-annotation-highlight", css)
        for line in css.splitlines():
            stripped = line.strip()
            if stripped.startswith("/*") or stripped.startswith("*"):
                continue
            if "clade-annotation-highlight" in stripped and ".dark" in stripped:
                self.fail("themed highlight rule would not survive export: " + stripped)
        self.assertNotIn("clade-annotation-default-highlight", css)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class CladeHighlightGeometryTests(unittest.TestCase):
    """The band's rectangle, asserted structurally rather than pixel-for-pixel.

    What matters is that it covers every descendant row, starts at the clade's
    own node without swallowing the parent branch, and reaches past the label
    that names the clade. The exact padding is a design choice and is free to
    change without failing these.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    # Four rows 20px apart; the clade spans the middle two.
    BASE = {
        "nodeX": 100, "parentX": 60,
        "top": 20, "bottom": 40,
        "rowPitch": 20, "padX": 8,
        "highlightRight": 300, "fallbackRight": 250,
    }

    def _rects(self, specs):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps({"highlights": specs}),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["highlights"]

    def _rect(self, **overrides):
        return self._rects([dict(self.BASE, **overrides)])[0]

    def test_the_band_spans_every_descendant_row_with_room_to_spare(self):
        rect = self._rect()
        self.assertLess(rect["y"], self.BASE["top"])
        self.assertGreater(rect["y"] + rect["height"], self.BASE["bottom"])
        # About half a row beyond each end, so it does not clip onto the
        # branch strokes of the first and last tips.
        self.assertAlmostEqual(rect["y"], self.BASE["top"] - 10)
        self.assertAlmostEqual(rect["y"] + rect["height"], self.BASE["bottom"] + 10)

    def test_it_starts_at_the_clade_node_without_covering_the_parent_branch(self):
        rect = self._rect()
        self.assertLessEqual(rect["x"], self.BASE["nodeX"])
        # The incoming branch runs from x=60 to x=100. Covering it would say the
        # ancestral lineage belongs to the highlighted clade.
        self.assertGreater(rect["x"], self.BASE["parentX"])
        self.assertGreaterEqual(rect["x"], 90)

    def test_a_very_short_incoming_branch_is_not_swallowed(self):
        rect = self._rect(parentX=99)
        self.assertGreater(rect["x"], 99)

    def test_the_root_has_no_parent_branch_to_protect(self):
        rect = self._rect(parentX=None)
        self.assertAlmostEqual(rect["x"], self.BASE["nodeX"] - self.BASE["padX"])

    def test_the_band_reaches_past_the_annotation_label(self):
        near = self._rect(highlightRight=300)
        far = self._rect(highlightRight=520)
        self.assertAlmostEqual(near["x"] + near["width"], 300)
        # A longer label pushes its lane further right and the band follows it,
        # instead of stopping at the tip-label boundary.
        self.assertAlmostEqual(far["x"] + far["width"], 520)
        self.assertGreater(far["width"], near["width"])

    def test_without_a_lane_it_falls_back_to_just_outside_the_tip_labels(self):
        rect = self._rect(highlightRight=None)
        self.assertAlmostEqual(rect["x"] + rect["width"], self.BASE["fallbackRight"])

    def test_a_tall_label_never_grows_the_biological_band(self):
        """Reversal of an earlier rule, which was the wrong semantics.

        The band across the tree says which taxa are in the clade. A two- or
        three-line caption on a single sequence used to stretch it over the rows
        above and below, which reads as those neighbours being members. The
        caption gets its own backing piece in the annotation lane instead; see
        SteppedHighlightTests.
        """
        one_line = self._rect(top=100, bottom=100)
        tall = self._rect(top=100, bottom=100, renderTop=85, renderBottom=115)
        self.assertAlmostEqual(tall["y"], one_line["y"])
        self.assertAlmostEqual(tall["height"], one_line["height"])
        # Half a row above and below the single tip row, and no more.
        self.assertAlmostEqual(tall["y"], 100 - self.BASE["rowPitch"] / 2)
        self.assertAlmostEqual(tall["y"] + tall["height"], 100 + self.BASE["rowPitch"] / 2)

    def test_the_band_height_is_the_same_for_one_and_three_line_labels(self):
        line_height = 12.5
        heights = []
        for lines in (1, 3):
            block = lines * line_height
            heights.append(self._rect(
                top=100, bottom=100,
                renderTop=100 - block / 2, renderBottom=100 + block / 2,
                laneX=260, laneWidth=120,
            )["height"])
        self.assertAlmostEqual(heights[0], heights[1])
        # One tip row plus half a row of air at each end -- not three lines of text.
        self.assertAlmostEqual(heights[0], self.BASE["rowPitch"])
        # The three-line text block itself is far taller than the band it names.
        self.assertGreater(3 * line_height, heights[1])

    def test_a_nested_band_sits_inside_its_parent_band(self):
        outer, inner = self._rects([
            dict(self.BASE, nodeX=100, parentX=60, top=20, bottom=80),
            dict(self.BASE, nodeX=150, parentX=100, top=20, bottom=40),
        ])
        # The child starts further right (its node is deeper) and covers fewer
        # rows, so the parent stays visible around it.
        self.assertGreater(inner["x"], outer["x"])
        self.assertLess(inner["height"], outer["height"])

    def test_a_degenerate_span_still_produces_a_drawable_rectangle(self):
        rect = self._rect(top=50, bottom=50, highlightRight=100, nodeX=100, parentX=100)
        self.assertGreaterEqual(rect["width"], 1)
        self.assertGreaterEqual(rect["height"], 1)

    def test_a_missing_clade_node_draws_nothing(self):
        """No node, no band -- never a rectangle guessed from the tips alone."""
        result = subprocess.run(
                ["node", "-e", (
                    "const fs=require('fs'),vm=require('vm');"
                    "const sandbox={console,setTimeout,clearTimeout,URLSearchParams,"
                    "document:{getElementById:()=>null},"
                    "window:{location:{search:''},addEventListener:()=>{},"
                    "removeEventListener:()=>{}}};"
                    "sandbox.window.window=sandbox.window;vm.createContext(sandbox);"
                    "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);"
                    "const v=Object.create(sandbox.window.DikaryaTreeViewer.prototype);"
                    "process.stdout.write(JSON.stringify("
                    "[v._cladeHighlightRect({cladeNode:null,top:0,bottom:1},20,8,100),"
                    "v._cladeHighlightRect({},20,8,100)]));"
                ), str(self.VIEWER_JS)],
                capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [None, None])


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class SteppedHighlightTests(unittest.TestCase):
    """A multiline caption gets its own backing piece instead of a taller band.

    The colored region that crosses the tree is a claim about which taxa belong
    to the clade, so its vertical extent is the descendant tip rows and nothing
    else. The translucent color still encloses the annotation text, but only in
    the annotation lane, where widening it says nothing about any sequence.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    # One tip at y=100, rows 20px apart. The lane starts at x=260; the label
    # block is three 12.5px lines centred on the tip.
    BASE = {
        "nodeX": 100, "parentX": 60,
        "top": 100, "bottom": 100,
        "rowPitch": 20, "padX": 8,
        "laneX": 260, "laneWidth": 120,
        "highlightRight": 388, "fallbackRight": 250,
    }
    THREE_LINES = {"renderTop": 100 - 18.75, "renderBottom": 100 + 18.75}

    def _pieces(self, **overrides):
        spec = dict(self.BASE, **overrides)
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps({"steppedHighlights": [spec]}),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["steppedHighlights"][0]

    def test_the_biological_band_is_one_row_whatever_the_label_says(self):
        plain = self._pieces()["band"]
        tall = self._pieces(**self.THREE_LINES)["band"]
        self.assertAlmostEqual(plain["y"], tall["y"])
        self.assertAlmostEqual(plain["height"], tall["height"])
        self.assertAlmostEqual(tall["height"], self.BASE["rowPitch"])
        # The rows above and below this single tip are 20px away and stay clear.
        self.assertGreater(tall["y"], 100 - 20)
        self.assertLess(tall["y"] + tall["height"], 100 + 20)

    def test_a_one_line_label_produces_no_extra_geometry(self):
        pieces = self._pieces()
        self.assertIsNone(pieces["label"])
        self.assertEqual(len(pieces["drawn"]), 1)
        # ...and the single rectangle still reaches past its label as before.
        self.assertAlmostEqual(pieces["band"]["x"] + pieces["band"]["width"],
                               self.BASE["highlightRight"])

    def test_a_multiline_label_gets_a_backing_piece_that_covers_it(self):
        pieces = self._pieces(**self.THREE_LINES)
        label = pieces["label"]
        self.assertIsNotNone(label)
        self.assertLessEqual(label["y"], self.THREE_LINES["renderTop"])
        self.assertGreaterEqual(label["y"] + label["height"],
                                self.THREE_LINES["renderBottom"])
        # It is allowed to be taller than the clade band; that is its whole job.
        self.assertGreater(label["height"], pieces["band"]["height"])

    def test_the_backing_piece_stays_in_the_annotation_lane(self):
        """It must never reach back across the tree or the tip labels."""
        pieces = self._pieces(**self.THREE_LINES)
        band, label = pieces["band"], pieces["label"]
        # Starts at the lane, not at the clade node and not among the tip labels.
        self.assertGreaterEqual(label["x"], self.BASE["laneX"] - self.BASE["padX"])
        self.assertGreater(label["x"], self.BASE["nodeX"])
        self.assertGreater(label["x"], self.BASE["fallbackRight"])
        # Together they cover the same right edge the single rectangle did.
        self.assertAlmostEqual(label["x"] + label["width"], self.BASE["highlightRight"])
        self.assertAlmostEqual(band["x"] + band["width"], label["x"])

    def test_the_two_pieces_meet_and_read_as_one_region(self):
        pieces = self._pieces(**self.THREE_LINES)
        band, label = pieces["band"], pieces["label"]
        # Touching edge to edge: no gap, and no overlap either -- overlapping two
        # translucent washes would paint the join darker than the resolved color.
        self.assertAlmostEqual(band["x"] + band["width"], label["x"])
        self.assertLessEqual(label["y"], band["y"])
        self.assertGreaterEqual(label["y"] + label["height"], band["y"] + band["height"])

    def test_both_pieces_are_drawn_with_the_same_colour_and_opacity(self):
        pieces = self._pieces(effective={"color": "#7a5aa6", "opacity": 0.26},
                              **self.THREE_LINES)
        self.assertEqual(len(pieces["drawn"]), 2)
        for drawn in pieces["drawn"]:
            self.assertEqual(drawn["styles"]["fill"], "#7a5aa6")
            self.assertEqual(drawn["styles"]["fill-opacity"], 0.26)

    def test_both_pieces_are_square_borderless_and_inert(self):
        pieces = self._pieces(annotationId="ann_a", **self.THREE_LINES)
        for drawn in pieces["drawn"]:
            self.assertNotIn("rx", drawn["attrs"])
            self.assertNotIn("ry", drawn["attrs"])
            self.assertEqual(drawn["styles"]["stroke"], "none")
            self.assertEqual(drawn["styles"]["pointer-events"], "none")
            # Inline, so the exported clone carries them without the stylesheet.
            self.assertIn("fill", drawn["styles"])
            self.assertEqual(drawn["attrs"]["data-annotation-id"], "ann_a")

    def test_both_pieces_survive_the_export_clone(self):
        pieces = self._pieces(**self.THREE_LINES)
        source = self.VIEWER_JS.read_text(encoding="utf-8")
        for drawn in pieces["drawn"]:
            # Both are ordinary band rects, not the invisible right-click target
            # the export clone strips out, and both carry their own geometry and
            # paint inline -- the clone is taken without the page stylesheet.
            self.assertEqual(drawn["attrs"]["class"], "clade-annotation-highlight")
            for key in ("x", "y", "width", "height"):
                self.assertIn(key, drawn["attrs"])
            self.assertIn("fill", drawn["styles"])
            self.assertIn("fill-opacity", drawn["styles"])
        # The only thing the export removes from the annotation groups.
        self.assertIn("clone.querySelectorAll('.clade-annotation-hit')", source)
        self.assertNotIn("querySelectorAll('.clade-annotation-highlight')", source)

    def test_a_clade_taller_than_its_label_stays_a_single_rectangle(self):
        pieces = self._pieces(top=60, bottom=140, **self.THREE_LINES)
        self.assertIsNone(pieces["label"])
        self.assertAlmostEqual(pieces["band"]["y"], 60 - 10)
        self.assertAlmostEqual(pieces["band"]["y"] + pieces["band"]["height"], 140 + 10)

    # ------------------------------------------------------------------
    # The cases above hand the primitive a renderTop/renderBottom pair. These
    # ask _annotationLayoutMetrics() for it from real annotation text instead,
    # because that is where the false split lived: a hand-written pair could
    # agree with the band while the real one-line text block, once the label
    # padding was (wrongly) added to it, did not.
    # ------------------------------------------------------------------

    def _real(self, label, font_size=12, **overrides):
        return self._pieces(label=label, fontSize=font_size, **overrides)

    def test_a_real_one_line_label_on_one_tip_is_a_single_rectangle(self):
        pieces = self._real("Amanita section Vaginatae")
        metrics = pieces["metrics"]
        # The real text block is shorter than the row it sits in...
        self.assertAlmostEqual(metrics["blockHeight"], 12 * 1.25)
        self.assertGreater(metrics["renderTop"], pieces["band"]["y"])
        self.assertLess(metrics["renderBottom"],
                        pieces["band"]["y"] + pieces["band"]["height"])
        # ...so there is nothing for a second rectangle to back.
        self.assertIsNotNone(pieces["band"])
        self.assertIsNone(pieces["label"])
        self.assertEqual(len(pieces["drawn"]), 1)

    def test_a_real_three_line_label_on_the_same_tip_gets_a_backing(self):
        one = self._real("Amanita section Vaginatae")
        three = self._real("Amanita section\nVaginatae\nsensu stricto")
        self.assertIsNone(one["label"])
        self.assertIsNotNone(three["label"])
        # The biological band is byte-for-byte the same clade in both cases: a
        # longer caption may not add rows to the statement about membership.
        self.assertAlmostEqual(one["band"]["y"], three["band"]["y"])
        self.assertAlmostEqual(one["band"]["height"], three["band"]["height"])
        self.assertAlmostEqual(three["band"]["height"], self.BASE["rowPitch"])
        # And the split is caused by the text itself, not by the padding: the
        # real block genuinely reaches outside the band.
        self.assertLess(three["metrics"]["renderTop"], three["band"]["y"])

    def test_a_real_label_that_exactly_fits_the_band_does_not_split_it(self):
        """labelTop == bandTop and labelBottom == bandBottom is a FIT."""
        # One 16px line is 20px tall, exactly the padded one-tip row.
        pieces = self._real("Boletus", font_size=16)
        band, metrics = pieces["band"], pieces["metrics"]
        self.assertAlmostEqual(metrics["renderTop"], band["y"])
        self.assertAlmostEqual(metrics["renderBottom"], band["y"] + band["height"])
        self.assertIsNone(pieces["label"])
        self.assertEqual(len(pieces["drawn"]), 1)

    def test_a_real_short_label_on_a_multi_tip_clade_is_one_rectangle(self):
        pieces = self._real("Russulaceae", top=100, bottom=140)
        self.assertIsNone(pieces["label"])
        self.assertAlmostEqual(pieces["band"]["x"] + pieces["band"]["width"],
                               self.BASE["highlightRight"])

    def test_a_real_overflowing_label_on_a_multi_tip_clade_splits(self):
        tall = "\n".join(f"line {n}" for n in range(1, 10))
        pieces = self._real(tall, top=100, bottom=140)
        band, label = pieces["band"], pieces["label"]
        self.assertIsNotNone(label)
        # The band is still exactly the three descendant rows.
        self.assertAlmostEqual(band["y"], 90)
        self.assertAlmostEqual(band["y"] + band["height"], 150)
        # The backing stays in the lane and meets the band without overlapping.
        self.assertGreaterEqual(label["x"], self.BASE["laneX"] - self.BASE["padX"])
        self.assertGreater(label["x"], self.BASE["nodeX"])
        self.assertAlmostEqual(band["x"] + band["width"], label["x"])
        self.assertAlmostEqual(label["x"] + label["width"], self.BASE["highlightRight"])
        self.assertLessEqual(label["y"], pieces["metrics"]["renderTop"])
        self.assertGreaterEqual(label["y"] + label["height"],
                                pieces["metrics"]["renderBottom"])


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class HighlightPreviewGeometryTests(unittest.TestCase):
    """The editor preview has to SHOW the stepped behaviour, not just use it.

    It draws over a synthetic clade because it has no topology, but that clade
    used to span the whole preview box -- taller than any label -- so a
    multiline highlight previewed as one plain band while the figure drew two
    pieces. The preview is a style preview: it invents a small clade, and it
    invents no neighbouring taxa.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _preview(self, label, font_size=12, width=420, height=120):
        spec = {"label": label, "fontSize": font_size,
                "width": width, "height": height}
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps({"previewHighlights": [spec]}),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["previewHighlights"][0]

    def test_a_one_line_preview_draws_a_single_band(self):
        preview = self._preview("Amanita section Vaginatae")
        self.assertEqual(len(preview["rects"]), 1)

    def test_a_three_line_preview_draws_the_stepped_backing(self):
        preview = self._preview("Amanita section\nVaginatae\nsensu stricto")
        self.assertEqual(preview["lineCount"], 3)
        self.assertEqual(len(preview["rects"]), 2)
        band, label = preview["rects"]
        self.assertAlmostEqual(band["x"] + band["width"], label["x"])
        self.assertGreater(label["height"], band["height"])

    def test_the_preview_clade_is_small_enough_to_show_the_step(self):
        preview = self._preview("Amanita")
        geom = preview["geom"]
        # A couple of rows in the middle of the box, not the whole box: a clade
        # spanning 24..height-24 could never be overflowed by a label.
        self.assertAlmostEqual(geom["bottom"] - geom["top"], geom["rowPitch"])
        self.assertAlmostEqual((geom["top"] + geom["bottom"]) / 2, 60)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class LocalAnnotationPlacementTests(unittest.TestCase):
    """Each clade annotation is placed from ITS OWN descendants' labels.

    A single very long sequence name elsewhere in the tree used to define the
    lane for every annotation, so a small nested clade's bracket was pushed
    across a wide empty band instead of sitting just beyond its own labels.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _run(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def _item(ident, indices, lane_width=100, top=None, bottom=None, **extra):
        spec = {
            "id": ident,
            "indices": indices,
            "laneWidth": lane_width,
            "renderTop": top if top is not None else indices[0] * 20,
            "renderBottom": bottom if bottom is not None else indices[-1] * 20,
        }
        spec.update(extra)
        return spec

    def _layout(self, items, tip_rights, zoom=1, **extra):
        payload = {"laneLayout": dict({
            "items": items, "tipRights": tip_rights,
            "labelRight": max(v for v in tip_rights if v is not None),
            "zoom": zoom, "rowPitch": 20,
        }, **extra)}
        return self._run(payload)["laneLayout"]

    # Rows 0-2 are a local clade with short labels; row 5 is an unrelated tip
    # with an enormous name.
    TIP_RIGHTS = [300, 310, 305, 300, 300, 900]

    def test_an_unrelated_long_label_does_not_move_a_local_annotation(self):
        with_long = self._layout(
            [self._item("local", [0, 1, 2])], self.TIP_RIGHTS)
        without_long = self._layout(
            [self._item("local", [0, 1, 2])], [300, 310, 305, 300, 300, 320])
        self.assertEqual(with_long["items"][0]["preferredLaneX"],
                         without_long["items"][0]["preferredLaneX"])
        self.assertEqual(with_long["lanes"][0]["x"], without_long["lanes"][0]["x"])
        # It sits just past its OWN longest label (310), not past the 900 one.
        self.assertAlmostEqual(with_long["lanes"][0]["x"],
                               310 + with_long["gapFromTree"])

    def test_a_long_label_inside_the_clade_does_move_it(self):
        layout = self._layout([self._item("local", [0, 1, 5])], self.TIP_RIGHTS)
        self.assertAlmostEqual(layout["lanes"][0]["x"], 900 + layout["gapFromTree"])

    def test_an_unmeasurable_clade_falls_back_to_the_tree_wide_edge(self):
        layout = self._layout([self._item("local", [0, 1])], [None, None, 500])
        # Nothing of its own could be measured, so the safe edge is used: never
        # narrower than its own labels, so the lane cannot land on the tips.
        self.assertAlmostEqual(layout["items"][0]["localLabelRight"], 500)

    def test_a_nested_annotation_sits_nearest_the_tree(self):
        """The 'sensu stricto' case: the inner label stays compact."""
        layout = self._layout([
            self._item("outer", [0, 1, 2], lane_width=400, top=0, bottom=40),
            self._item("inner", [0, 1], lane_width=60, top=0, bottom=20),
        ], self.TIP_RIGHTS)
        lanes = {lane["items"][0]: lane for lane in layout["lanes"]}
        # Innermost first, beside its own labels ...
        self.assertEqual(layout["lanes"][0]["items"], ["inner"])
        self.assertAlmostEqual(lanes["inner"]["x"], 310 + layout["gapFromTree"])
        # ... and the containing clade's long label is what gets pushed out,
        # not the short one that names the nested clade.
        self.assertGreater(lanes["outer"]["x"], lanes["inner"]["x"])

    def test_lanes_clear_each_other_by_exactly_the_inter_lane_gap(self):
        layout = self._layout([
            self._item("outer", [0, 1, 2], lane_width=400, top=0, bottom=40),
            self._item("inner", [0, 1], lane_width=60, top=0, bottom=20),
        ], self.TIP_RIGHTS)
        first, second = layout["lanes"][0], layout["lanes"][1]
        self.assertAlmostEqual(second["x"],
                               first["x"] + first["width"] + layout["gapBetweenLanes"])

    def test_a_later_lane_is_not_pushed_out_when_it_already_clears(self):
        """Two clades far apart vertically share one lane and stay local."""
        layout = self._layout([
            self._item("top", [0, 1], lane_width=60, top=0, bottom=20),
            self._item("bottom", [4, 5], lane_width=60, top=200, bottom=220),
        ], self.TIP_RIGHTS)
        self.assertEqual(len(layout["lanes"]), 1)
        # The lane serves both, so it clears the widest of their own labels.
        self.assertAlmostEqual(layout["lanes"][0]["x"], 900 + layout["gapFromTree"])

    def test_lanes_never_overlap_vertically(self):
        layout = self._layout([
            self._item("a", [0, 1, 2], lane_width=100, top=0, bottom=40),
            self._item("b", [0, 1], lane_width=100, top=0, bottom=20),
            self._item("c", [4, 5], lane_width=100, top=200, bottom=220),
        ], self.TIP_RIGHTS)
        # b and c are far apart, so they share the inner lane; a contains b and
        # has to take its own rather than drawing over it.
        self.assertEqual(sorted(layout["lanes"][0]["items"]), ["b", "c"])
        self.assertEqual(layout["lanes"][1]["items"], ["a"])

    def test_two_annotations_on_touching_rows_take_separate_lanes(self):
        layout = self._layout([
            self._item("upper", [0, 1], lane_width=100, top=0, bottom=20),
            self._item("lower", [1, 2], lane_width=100, top=20, bottom=40),
        ], self.TIP_RIGHTS)
        # Their text blocks would collide at the join, so they are kept apart.
        self.assertEqual(len(layout["lanes"]), 2)

    def test_the_gap_before_the_lane_holds_its_screen_size_under_zoom(self):
        for zoom in (0.25, 1, 4):
            with self.subTest(zoom=zoom):
                layout = self._layout(
                    [self._item("local", [0, 1, 2])], self.TIP_RIGHTS, zoom=zoom)
                gap = layout["lanes"][0]["x"] - 310
                self.assertAlmostEqual(gap * zoom, 18)

    def test_clade_lines_and_highlights_are_placed_identically(self):
        by_type = {}
        for annotation_type in ("clade_line", "clade_highlight"):
            layout = self._layout(
                [self._item("local", [0, 1, 2], type=annotation_type)],
                self.TIP_RIGHTS)
            by_type[annotation_type] = layout["lanes"][0]["x"]
        self.assertEqual(by_type["clade_line"], by_type["clade_highlight"])

    def test_an_earlier_layer_still_pushes_the_next_one_outward(self):
        """Layers stack outward; only the FIRST lane is free to sit locally."""
        layout = self._layout(
            [self._item("local", [0, 1, 2])], self.TIP_RIGHTS, cursorX=1200)
        self.assertAlmostEqual(layout["lanes"][0]["x"], 1200)


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class LaneHighlightRightEdgeTests(unittest.TestCase):
    """Bands are flushed per lane, not across a whole layer."""

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    def _align(self, specs):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "annotation_layout.js"
            harness.write_text(_LAYOUT_HARNESS_JS)
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=json.dumps({"alignRightEdges": specs}),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return {entry["id"]: entry["right"]
                for entry in json.loads(result.stdout)["alignedRightEdges"]}

    def test_two_bands_in_one_lane_end_flush(self):
        edges = self._align([
            {"id": "first", "right": 400, "layerId": "layer_a", "laneId": "layer_a#0"},
            {"id": "second", "right": 560, "layerId": "layer_a", "laneId": "layer_a#0"},
        ])
        self.assertEqual(edges["first"], 560)
        self.assertEqual(edges["second"], 560)

    def test_a_nested_lane_is_not_stretched_to_another_lanes_label(self):
        edges = self._align([
            {"id": "inner", "right": 400, "layerId": "layer_a", "laneId": "layer_a#0"},
            {"id": "outer", "right": 900, "layerId": "layer_a", "laneId": "layer_a#1"},
        ])
        # Same layer, different lanes: the inner band keeps its own edge instead
        # of being dragged across the lane in between.
        self.assertEqual(edges["inner"], 400)
        self.assertEqual(edges["outer"], 900)

    def test_the_shared_edge_still_clears_the_longest_label_in_the_lane(self):
        edges = self._align([
            {"id": "short", "right": 400, "layerId": "layer_a", "laneId": "layer_a#0"},
            {"id": "long", "right": 560, "layerId": "layer_a", "laneId": "layer_a#0"},
            {"id": "middle", "right": 480, "layerId": "layer_a", "laneId": "layer_a#0"},
        ])
        self.assertEqual(set(edges.values()), {560})

    def test_a_band_with_no_lane_is_left_alone(self):
        edges = self._align([
            {"id": "placed", "right": 400, "layerId": "layer_a", "laneId": "layer_a#0"},
            {"id": "unplaced", "right": None, "layerId": "layer_a", "laneId": "layer_a#1"},
        ])
        self.assertEqual(edges["placed"], 400)
        self.assertIsNone(edges["unplaced"])


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class EditorDraftPreviewTests(_RenderHarnessMixin, unittest.TestCase):
    """The editor previews the DRAFT, not the annotation still saved under its id.

    While "Fixed red" is being switched to "Automatic" the saved annotation still
    says fixed and still carries the palette slot it was saved with. Resolving the
    preview against that stale object showed a color the save would not produce,
    because a fixed entry consumes no palette slot and an automatic one does.
    """

    TREE = [[["A", "B"], ["C", "D"]], ["E", "F"]]
    TIP_ORDER = ["A", "B", "C", "D", "E", "F"]

    def _draft(self, draft, annotations, **kwargs):
        out = self._resolve(annotations, draft_preview=draft, **kwargs)
        return out["draftPreview"]

    @staticmethod
    def _highlight(annotation_id, members, **overrides):
        annotation = _annotation(annotation_id, label="Section",
                                 members=list(members),
                                 annotation_type="clade_highlight")
        annotation.update(overrides)
        return annotation

    def test_fixed_to_auto_uses_the_draft_not_the_saved_annotation(self):
        saved_fixed = self._highlight("ann_a", ["A", "B"], highlight_color="#b91c1c",
                                      highlight_color_mode="fixed",
                                      automatic_highlight_slot=2)
        draft = self._highlight("ann_a", ["A", "B"], highlight_color=None,
                                highlight_color_mode="auto",
                                automatic_highlight_slot=2)
        result = self._draft(draft, [saved_fixed])
        # Not the red still sitting in the saved annotation ...
        self.assertNotEqual(result["preview"]["color"], "#b91c1c")
        # ... and exactly what the same draft resolves to once saved.
        self.assertEqual(result["preview"]["color"], result["saved"]["color"])
        self.assertEqual(result["swatch"], result["saved"]["color"])
        # The draft replaces the saved entry; it is never counted twice.
        self.assertEqual(result["savedCount"], 1)

    def test_the_saved_palette_slot_is_reflected_in_the_preview(self):
        draft = self._highlight("ann_a", ["A", "B"], highlight_color_mode="auto",
                                automatic_highlight_slot=3)
        other = self._highlight("ann_b", ["E", "F"], highlight_color_mode="auto",
                                automatic_highlight_slot=0)
        result = self._draft(draft, [
            self._highlight("ann_a", ["A", "B"], highlight_color="#b91c1c",
                            highlight_color_mode="fixed",
                            automatic_highlight_slot=3),
            other,
        ])
        slot_three = self._draft(
            self._highlight("ann_a", ["A", "B"], highlight_color_mode="auto",
                            automatic_highlight_slot=3),
            [other],
        )
        self.assertEqual(result["preview"]["color"], slot_three["preview"]["color"])
        self.assertEqual(result["preview"]["color"], result["saved"]["color"])
        self.assertEqual(draft["automatic_highlight_slot"], 3)

    def test_fixed_to_auto_on_a_clade_that_derives_a_group_colour(self):
        saved_fixed = self._highlight("ann_a", ["A", "B"], highlight_color="#b91c1c",
                                      highlight_color_mode="fixed",
                                      automatic_highlight_slot=1)
        draft = self._highlight("ann_a", ["A", "B"], highlight_color=None,
                                highlight_color_mode="auto",
                                automatic_highlight_slot=1)
        result = self._draft(
            draft, [saved_fixed],
            selection_sets={"Default": ["A", "B"]},
            selection_set_colors={"Default": "#2f7d32"},
            active_selection_set="Default",
        )
        # The clade's colour group wins over the stored palette slot, in the
        # preview and after saving alike.
        derived = self._resolve(
            [self._highlight("ann_a", ["A", "B"], highlight_color_mode="auto",
                             automatic_highlight_slot=1)],
            selection_sets={"Default": ["A", "B"]},
            selection_set_colors={"Default": "#2f7d32"},
            active_selection_set="Default",
        )["automaticColors"]["ann_a"]
        self.assertEqual(result["preview"]["color"], derived)
        self.assertEqual(result["saved"]["color"], derived)
        self.assertNotEqual(result["preview"]["color"], "#b91c1c")

    def test_auto_to_fixed_previews_the_chosen_colour(self):
        saved_auto = self._highlight("ann_a", ["A", "B"],
                                     highlight_color_mode="auto",
                                     automatic_highlight_slot=0)
        draft = self._highlight("ann_a", ["A", "B"], highlight_color="#b91c1c",
                                highlight_color_mode="fixed",
                                automatic_highlight_slot=0)
        result = self._draft(draft, [saved_auto])
        self.assertEqual(result["preview"]["color"], "#b91c1c")
        self.assertEqual(result["saved"]["color"], "#b91c1c")

    def test_inherit_from_an_automatic_layer_is_automatic(self):
        layer = _layer(default_highlight_color_mode="auto")
        saved_fixed = self._highlight("ann_a", ["A", "B"], highlight_color="#b91c1c",
                                      highlight_color_mode="fixed")
        draft = self._highlight("ann_a", ["A", "B"], highlight_color=None,
                                highlight_color_mode=None)
        result = self._draft(draft, [saved_fixed], layers=[layer])
        self.assertNotEqual(result["preview"]["color"], "#b91c1c")
        self.assertEqual(result["preview"]["color"], result["saved"]["color"])

    def test_inherit_from_a_fixed_layer_takes_the_layer_colour(self):
        layer = _layer(default_highlight_color="#7a5aa6",
                       default_highlight_color_mode="fixed")
        saved_auto = self._highlight("ann_a", ["A", "B"],
                                     highlight_color_mode="auto",
                                     automatic_highlight_slot=0)
        draft = self._highlight("ann_a", ["A", "B"], highlight_color=None,
                                highlight_color_mode=None)
        result = self._draft(draft, [saved_auto], layers=[layer])
        self.assertEqual(result["preview"]["color"], "#7a5aa6")
        self.assertEqual(result["saved"]["color"], "#7a5aa6")

    def test_a_draft_does_not_recolour_its_neighbours_by_being_previewed(self):
        neighbour = self._highlight("ann_b", ["C", "D"],
                                    highlight_color_mode="auto",
                                    automatic_highlight_slot=1)
        draft = self._highlight("ann_a", ["A", "B"], highlight_color=None,
                                highlight_color_mode="auto")
        result = self._draft(draft, [
            self._highlight("ann_a", ["A", "B"], highlight_color="#b91c1c",
                            highlight_color_mode="fixed"),
            neighbour,
        ])
        # Adjacent clades, so the draft must not land on the neighbour's colour --
        # in the preview or after saving, and the two must agree.
        neighbour_color = self._resolve([neighbour])["automaticColors"]["ann_b"]
        self.assertNotEqual(result["preview"]["color"], neighbour_color)
        self.assertEqual(result["preview"]["color"], result["saved"]["color"])
        self.assertEqual(result["savedCount"], 1)


if __name__ == "__main__":
    unittest.main()
