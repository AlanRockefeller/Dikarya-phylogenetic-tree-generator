import copy
from io import StringIO

import pytest
from Bio import Phylo

from app.services.tree_annotation_service import (
    ANNOTATION_LAYERS_KEY,
    CLADE_ANNOTATIONS_KEY,
)
from app.services.tree_edit_service import _stable_internal_node_id, prune_taxa
from app.services.tree_io import write_tree_file


TREE = "((A:0.1,B:0.2)AB:0.3,C:0.4)ROOT:0.0;\n"


def _setup(tmp_path):
    tree_dir = tmp_path / "tree"
    tree_dir.mkdir()
    tree = Phylo.read(StringIO(TREE), "newick")
    newick = tree_dir / "tree_pruned.newick"
    nexus = tree_dir / "tree_pruned.nexus"
    write_tree_file(tree, newick, "newick")
    write_tree_file(tree, nexus, "nexus")
    state = {
        "current_tree": "pruned",
        "pruned_taxa": ["STALE"],
        "selection_sets": {"selected": ["A", "B", "C"]},
        "selection_set_colors": {"selected": "#123456"},
        ANNOTATION_LAYERS_KEY: [{"id": "layer", "name": "Layer", "order": 1}],
        CLADE_ANNOTATIONS_KEY: [{
            "id": "annotation", "layer_id": "layer", "label": "All",
            "member_tip_ids": ["A", "B", "C"],
        }],
        "sequence_of_interest": "A",
        "needs_sequence_of_interest": False,
        "root_mode": "auto",
        "root_target": "A",
        "rooting_info": {"chosen_by": "auto"},
        "tree_structure": {"sentinel": "unchanged"},
        "prune_unresolved": ["old transient value"],
    }
    return tree, newick, nexus, state


def _tip_names(path):
    return {tip.name for tip in Phylo.read(str(path), "newick").get_terminals()}


def test_prune_one_of_three_succeeds(tmp_path):
    _tree, newick, _nexus, state = _setup(tmp_path)
    prune_taxa(tmp_path, state, ["A"])
    assert _tip_names(newick) == {"B", "C"}


def test_prune_two_of_three_succeeds_and_leaves_one_tip(tmp_path):
    _tree, newick, nexus, state = _setup(tmp_path)
    prune_taxa(tmp_path, state, ["A", "B"])
    assert _tip_names(newick) == {"C"}
    nexus_text = nexus.read_text()
    assert "DIMENSIONS NTAX=1;" in nexus_text
    assert "1 C;" in nexus_text


@pytest.mark.parametrize("target_kind", ["tips", "root", "overlap"])
def test_prune_every_remaining_tip_fails_atomically(tmp_path, target_kind):
    tree, newick, nexus, state = _setup(tmp_path)
    root_id = _stable_internal_node_id(tree.root)
    ab = next(clade for clade in tree.get_nonterminals() if clade.name == "AB")
    ab_id = _stable_internal_node_id(ab)
    targets = {
        "tips": ["A", "B", "C", "A"],
        "root": [root_id],
        "overlap": [ab_id, "A", "C"],
    }[target_kind]
    before_state = copy.deepcopy(state)
    before_newick = newick.read_bytes()
    before_nexus = nexus.read_bytes()

    with pytest.raises(ValueError, match="every remaining taxon"):
        prune_taxa(tmp_path, state, targets)

    # Assert the full supplied viewer state as well as every independently
    # meaningful prune/root/annotation field. The field-level checks make a
    # future partial mutation obvious even if unrelated state is later added.
    assert state == before_state
    for key in (
        "current_tree",
        "tree_structure",
        "pruned_taxa",
        "selection_sets",
        "selection_set_colors",
        ANNOTATION_LAYERS_KEY,
        CLADE_ANNOTATIONS_KEY,
        "sequence_of_interest",
        "needs_sequence_of_interest",
        "root_mode",
        "root_target",
        "rooting_info",
        "prune_unresolved",
    ):
        assert state[key] == before_state[key]
    assert newick.read_bytes() == before_newick
    assert nexus.read_bytes() == before_nexus
