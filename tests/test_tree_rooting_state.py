"""
Focused tests for tree rooting / sequence-of-interest state hygiene.

Covers the ship-readiness fixes:
  * set_sequence_of_interest() no longer mutates the user-editable "Default"
    color group (focal state lives in sequence_of_interest only).
  * reroot_tree() clears is_midpoint_rooted / needs_sequence_of_interest so a
    manual reroot does not leave a stale Midpoint "(on)" or SOI prompt.
  * prune_taxa() only flags needs_sequence_of_interest when an auto-style
    rooting mode is active.

Pure-state tests run everywhere; the reroot/prune tests need BioPython and use
a temporary job directory with a tiny Newick tree.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.tree_edit_service import (  # noqa: E402
    HAS_BIOPYTHON,
    _reapply_rooting_after_recompute,
    _stable_internal_node_id_from_names,
    prune_taxa,
    reroot_tree,
    rotate_node,
    set_sequence_of_interest,
)
from app.services.tree_rooting_service import (  # noqa: E402
    _assess_candidate,
    _load_alignment,
    choose_auto_root_target,
)


def _tree_json_with_tips(names):
    """Minimal tree_state with a tree_structure whose tips are `names`."""
    children = [{"name": n, "original_name": n} for n in names]
    return {"tree_structure": {"name": None, "original_name": None, "children": children}}


def _write_job_tree(job_dir: Path, newick: str):
    (job_dir / "tree").mkdir(parents=True, exist_ok=True)
    (job_dir / "tree" / "tree_original.newick").write_text(newick)


def _write_pruned_tree(job_dir: Path, newick: str):
    (job_dir / "tree").mkdir(parents=True, exist_ok=True)
    (job_dir / "tree" / "tree_pruned.newick").write_text(newick)


def _write_job_alignment(job_dir: Path, records):
    (job_dir / "alignment").mkdir(parents=True, exist_ok=True)
    fasta = "\n".join(f">{name}\n{seq}" for name, seq in records)
    (job_dir / "alignment" / "alignment_raw.fasta").write_text(fasta + "\n")


def _write_pruned_alignment(job_dir: Path, records):
    (job_dir / "alignment").mkdir(parents=True, exist_ok=True)
    fasta = "\n".join(f">{name}\n{seq}" for name, seq in records)
    (job_dir / "alignment" / "alignment_pruned_trimmed.fasta").write_text(fasta + "\n")


def _quote_newick_label(label):
    return "'" + label.replace("'", "''") + "'"


def _json_tip_names(node):
    if node.get("children"):
        out = []
        for child in node["children"]:
            out.extend(_json_tip_names(child))
        return out
    return [node.get("original_name") or node.get("name")]


class TestSetSequenceOfInterestDoesNotTouchDefault(unittest.TestCase):
    def test_set_soi_does_not_create_or_mutate_default(self):
        tj = _tree_json_with_tips(["A", "B"])
        set_sequence_of_interest(tj, "A")
        self.assertEqual(tj["sequence_of_interest"], "A")
        self.assertEqual(tj["sequence_of_interest_source"], "user_selected")
        self.assertFalse(tj["needs_sequence_of_interest"])
        # The Default color group must not be created or populated.
        self.assertNotIn("selection_sets", tj)
        self.assertNotIn("selection_set_colors", tj)

    def test_set_soi_preserves_user_populated_default(self):
        tj = _tree_json_with_tips(["A", "B", "C"])
        tj["selection_sets"] = {"Default": ["B", "C"]}
        tj["selection_set_colors"] = {"Default": "#ff0000"}

        set_sequence_of_interest(tj, "A")
        # User's Default membership and color are untouched.
        self.assertEqual(tj["selection_sets"]["Default"], ["B", "C"])
        self.assertEqual(tj["selection_set_colors"]["Default"], "#ff0000")
        self.assertEqual(tj["sequence_of_interest"], "A")

        # Changing the focal tip must NOT remove a previous focal tip from Default
        # (the old destructive behavior) nor add the new one.
        set_sequence_of_interest(tj, "B")
        self.assertEqual(tj["selection_sets"]["Default"], ["B", "C"])
        self.assertEqual(tj["sequence_of_interest"], "B")

    def test_clear_soi_leaves_default_untouched(self):
        tj = _tree_json_with_tips(["A", "B"])
        tj["selection_sets"] = {"Default": ["A"]}
        set_sequence_of_interest(tj, "A")
        set_sequence_of_interest(tj, None)
        self.assertIsNone(tj["sequence_of_interest"])
        self.assertIsNone(tj["sequence_of_interest_source"])
        self.assertEqual(tj["selection_sets"]["Default"], ["A"])

    def test_non_string_tip_name_raises_valueerror(self):
        tj = _tree_json_with_tips(["A", "B"])
        with self.assertRaises(ValueError):
            set_sequence_of_interest(tj, 123)  # endpoint maps ValueError -> 400


class TestAutoRootCandidateQuality(unittest.TestCase):
    def test_high_overlap_short_candidate_in_long_alignment_is_acceptable(self):
        focal = ("-" * 100) + ("A" * 100) + ("-" * 900)
        candidate = ("-" * 100) + ("A" * 95) + ("-" * 905)

        ok, reason = _assess_candidate(focal, 100, candidate)

        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_pruned_alignment_is_preferred_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_job_alignment(job_dir, [("A", "AAAA")])
            _write_pruned_alignment(job_dir, [("A", "CCCC")])

            alignment = _load_alignment(job_dir)

        self.assertEqual(alignment["A"], "CCCC")


@unittest.skipUnless(HAS_BIOPYTHON, "requires BioPython")
class TestAutoRootTaxonPreference(unittest.TestCase):
    def test_auto_prefers_distinct_named_taxon_over_farther_same_species(self):
        focal = "iNat1 Amanita bisporigera Colorado US"
        same_species = "MW1 Amanita bisporigera unusually divergent"
        other_species = "KY1 Amanita pallidorosea sample"

        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_job_tree(
                job_dir,
                (
                    f"({_quote_newick_label(focal)}:0.1,"
                    f"{_quote_newick_label(same_species)}:0.9,"
                    f"{_quote_newick_label(other_species)}:0.5)R:0.0;"
                ),
            )
            seq = "A" * 100
            _write_job_alignment(
                job_dir,
                [(focal, seq), (same_species, seq), (other_species, seq)],
            )
            state = _tree_json_with_tips([focal, same_species, other_species])
            state["sequence_of_interest"] = focal

            auto_choice = choose_auto_root_target(job_dir, state, mode="auto")
            divergent_choice = choose_auto_root_target(job_dir, state, mode="most_divergent_hit")

        self.assertEqual(auto_choice.target_name, other_species)
        self.assertIn("taxon_distinct", auto_choice.reason)
        self.assertEqual(divergent_choice.target_name, same_species)

    def test_recompute_reapplies_auto_root_to_fresh_pruned_tree(self):
        focal = "iNat1 Amanita bisporigera Colorado US"
        same_species = "MW1 Amanita bisporigera sample"
        other_species = "KY1 Amanita pallidorosea sample"

        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_pruned_tree(
                job_dir,
                (
                    f"({_quote_newick_label(focal)}:0.1,"
                    f"{_quote_newick_label(same_species)}:0.2,"
                    f"{_quote_newick_label(other_species)}:0.5)R:0.0;"
                ),
            )
            seq = "A" * 100
            _write_pruned_alignment(
                job_dir,
                [(focal, seq), (same_species, seq), (other_species, seq)],
            )
            state = _tree_json_with_tips([focal, same_species, other_species])
            state["sequence_of_interest"] = focal

            out = _reapply_rooting_after_recompute(job_dir, state, "auto", None)

        self.assertEqual(out["root_mode"], "auto")
        self.assertEqual(out["root_target"], other_species)
        root_children = out["tree_structure"]["children"]
        self.assertTrue(any(child.get("name") == other_species for child in root_children))

    def test_auto_roots_on_distinct_taxon_clade_after_recompute(self):
        focal = "iNat1 Amanita bisporigera Colorado US"
        same_species = "MW1 Amanita bisporigera sample"
        other_1 = "KY1 Amanita pallidorosea sample"
        other_2 = "KY2 Amanita pallidorosea distant"
        other_3 = "KF1 Amanita fuliginea sample"

        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_pruned_tree(
                job_dir,
                (
                    f"(({_quote_newick_label(focal)}:0.1,"
                    f"{_quote_newick_label(same_species)}:0.2):0.1,"
                    f"({_quote_newick_label(other_1)}:0.2,"
                    f"({_quote_newick_label(other_2)}:0.5,"
                    f"{_quote_newick_label(other_3)}:0.1):0.1):0.4)R:0.0;"
                ),
            )
            seq = "A" * 100
            _write_pruned_alignment(
                job_dir,
                [(focal, seq), (same_species, seq), (other_1, seq), (other_2, seq), (other_3, seq)],
            )
            state = _tree_json_with_tips([focal, same_species, other_1, other_2, other_3])
            state["sequence_of_interest"] = focal

            out = _reapply_rooting_after_recompute(job_dir, state, "auto", None)

        self.assertEqual(out["root_target"], other_2)
        self.assertEqual(out["rooting_info"]["root_clade"]["rooted_on"], "taxon_distinct_clade")
        root_child_sets = [set(_json_tip_names(child)) for child in out["tree_structure"]["children"]]
        distinct_taxon_root_children = [
            names for names in root_child_sets
            if focal not in names and same_species not in names
        ]
        self.assertEqual(set().union(*distinct_taxon_root_children), {other_1, other_2, other_3})


@unittest.skipUnless(HAS_BIOPYTHON, "requires BioPython")
class TestRerootClearsMidpointState(unittest.TestCase):
    NEWICK = "((A:1,B:1)I1:1,(C:1,D:1)I2:1)R:0.0;"

    def test_manual_reroot_clears_midpoint_and_soi_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_job_tree(job_dir, self.NEWICK)
            tj = {"is_midpoint_rooted": True, "needs_sequence_of_interest": True}
            out = reroot_tree(job_dir, tj, "A")
            self.assertFalse(out["is_midpoint_rooted"])
            self.assertFalse(out["needs_sequence_of_interest"])
            self.assertEqual(out["root_target"], "A")
            self.assertEqual(out["rooting_info"]["chosen_by"], "manual")


@unittest.skipUnless(HAS_BIOPYTHON, "requires BioPython")
class TestPruneFocalNeedsFlag(unittest.TestCase):
    NEWICK = "((A:1,B:1)I1:1,(C:1,D:1)I2:1)R:0.0;"

    def _prune_focal(self, root_mode):
        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_job_tree(job_dir, self.NEWICK)
            tj = {"sequence_of_interest": "A", "root_mode": root_mode}
            prune_taxa(job_dir, tj, ["A"])
            return tj

    def test_prune_focal_in_midpoint_mode_does_not_prompt(self):
        tj = self._prune_focal("midpoint")
        self.assertIsNone(tj["sequence_of_interest"])
        self.assertFalse(tj["needs_sequence_of_interest"])

    def test_prune_focal_in_auto_mode_prompts(self):
        tj = self._prune_focal("auto")
        self.assertIsNone(tj["sequence_of_interest"])
        self.assertTrue(tj["needs_sequence_of_interest"])


@unittest.skipUnless(HAS_BIOPYTHON, "requires BioPython")
class TestRotateIdentityAfterRename(unittest.TestCase):
    NEWICK = "((A:1,B:1)I1:1,(C:1,D:1)I2:1)R:0.0;"

    def test_rotate_recognized_by_original_name_id_when_descendant_renamed(self):
        # The backend hashes ORIGINAL tip names from the Newick; renames are display-only,
        # so an internal node stays addressable by an id built from original names.
        with tempfile.TemporaryDirectory() as d:
            job_dir = Path(d)
            _write_job_tree(job_dir, self.NEWICK)
            tree_json = {"renames": {"A": "A renamed"}}
            node_id = _stable_internal_node_id_from_names(["A", "B"])
            out = rotate_node(job_dir, tree_json, node_id)
            self.assertEqual(out["current_tree"], "pruned")
            self.assertEqual(out["last_rotated_node_id"], node_id)
            # Original Newick must be left intact; rotation writes only the pruned copy.
            self.assertTrue((job_dir / "tree" / "tree_pruned.newick").exists())
            self.assertEqual((job_dir / "tree" / "tree_original.newick").read_text(), self.NEWICK)


if __name__ == "__main__":
    unittest.main()
