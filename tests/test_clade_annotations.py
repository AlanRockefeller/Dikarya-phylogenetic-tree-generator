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
        self._reject({
            "layers": [_layer(f"layer_{i}", f"L{i}", order=i + 1) for i in range(21)],
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

_RENDER_HARNESS_JS = r"""
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

// Build a node tree from a nested array spec: a string is a leaf, an array is
// an internal node. Same shape the viewer walks when it indexes clade blocks.
const allNodes = [];
function build(spec, parent) {
    const node = { parent: parent || null, children: [] };
    if (typeof spec === 'string') {
        node.id = spec;
        node.__leafCount = 1;
    } else {
        node.children = spec.map((child) => build(child, node));
        node.__leafCount = node.children.reduce((sum, child) => sum + child.__leafCount, 0);
    }
    allNodes.push(node);
    return node;
}
build(input.tree, null);

const positions = new Map();
input.tipOrder.forEach((id, index) => positions.set(id, { y: index * 10, index }));

const viewer = Object.create(sandbox.window.DikaryaTreeViewer.prototype);
viewer.allNodes = allNodes;
viewer._getNodeId = (node) => node.id || null;
viewer.annotationLayers = input.layers;
viewer.cladeAnnotations = input.annotations;
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

const { cladeBlocks, branchNodes } = viewer._buildAnnotationTopologyIndexes(positions);
const layerById = new Map(input.layers.map((layer) => [layer.id, layer]));
const { validity, resolved } = viewer._resolveAnnotationsForRender(
    positions, cladeBlocks, layerById, branchNodes
);

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
    annotationsForNode,
    selectedClade: input.selectedIds ? viewer.getSelectedCladeLeafIds() : null,
    selectedHasIncomingBranch: input.selectedIds
        ? viewer.hasIncomingBranchForMemberIds(input.selectedIds) : null,
    contextBranchTargets,
    contextDispatchedTargets,
    incomingBranchResult,
    incomingBranchDescendantWalks,
    tipLabelOffsets
}));
"""


def _node_available():
    return shutil.which("node") is not None


@unittest.skipUnless(_node_available(), "Node is required to exercise the renderer")
class AnnotationRenderDecisionTests(unittest.TestCase):
    """Only exact clades draw; branch types additionally require an incoming branch.

    Contiguity alone is not enough. After a reroot an annotation's tips can sit
    together in the tip order while no node has that exact descendant set; the
    renderer used to draw those as one continuous (dashed) bracket, which still
    reads as "these taxa are a group". They are now flagged in the manager and
    left off the figure entirely, and they come back by themselves when the
    topology makes them a clade again.
    """

    VIEWER_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "tree_viewer_phylotree_v2.js"

    # ((A,B),(C,D)) -- so A+B and C+D are clades, but B+C is not, even though
    # B and C are neighbours in the tip order.
    TREE = [["A", "B"], ["C", "D"]]
    TIP_ORDER = ["A", "B", "C", "D"]

    def _resolve(self, annotations, layers=None, tree=None, tip_order=None,
                 lookup_members=None, selected_ids=None, branch_target_members=None,
                 incoming_branch_members=None, tip_label_gap=None):
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
            })
            result = subprocess.run(
                ["node", str(harness), str(self.VIEWER_JS)],
                input=payload, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

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


# --- renderer: label geometry and style inheritance ------------------------
#
# Same approach as above: the rules live in tree_viewer_phylotree_v2.js and are
# pure functions, so Node evaluates them directly. No DOM, no D3, no new JS test
# framework.

_LAYOUT_HARNESS_JS = r"""
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

const viewer = Object.create(sandbox.window.DikaryaTreeViewer.prototype);

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

    def test_a_chosen_non_default_colour_is_never_treated_as_default(self):
        out = self._run({"style": {
            "annotation": {"text_color": "#b91c1c"},
            "layer": {"default_text_color": "#1f2937"},
            "fields": {"text_color": True},
        }})
        self.assertEqual(out["style"]["text_color"], {"value": "#b91c1c", "isDefault": False})


if __name__ == "__main__":
    unittest.main()
