"""Unifurcation collapse: the surviving node keeps the summed branch length.

Both tests here used to print SUCCESS/FAILURE and assert nothing, so pytest
reported them green whether _collapse_unifurcations() produced the right tree or
not. Branch lengths are the measurement in a phylogeny -- losing 0.2 substitutions
per site to a silently wrong collapse is a wrong tree, not a cosmetic defect --
so each check is now an assertion that fails the suite.
"""

import io
import math

from Bio import Phylo

from app.services.tree_edit_service import _collapse_unifurcations


def test_root_collapse_promotes_the_only_child_and_keeps_its_length():
    # Root has one child 'A' with length 0.1; collapsing must promote A to root
    # rather than dropping either the node or its branch.
    tree = Phylo.read(io.StringIO("(A:0.1);"), "newick")

    _collapse_unifurcations(tree)

    assert tree.root.name == "A"
    assert tree.root.branch_length is not None, "the promoted root lost its branch length"
    assert math.isclose(tree.root.branch_length, 0.1, rel_tol=1e-9)


def test_internal_collapse_sums_the_two_branches_it_replaces():
    # Root -> (Internal -> (A), B). Removing 'Internal' must give A the sum of
    # its own 0.1 and Internal's 0.2, or the tip moves 0.2 closer to the root.
    tree = Phylo.read(io.StringIO("((A:0.1)Internal:0.2, B:1.0)Root;"), "newick")

    _collapse_unifurcations(tree)

    names = [clade.name for clade in tree.find_clades()]
    assert "Internal" not in names, f"the unifurcation survived: {names}"

    clade_a = next(clade for clade in tree.find_clades() if clade.name == "A")
    # Not getattr(..., 0): Bio.Phylo clades always define branch_length, so the
    # default never applied, and a None left behind by a broken collapse raised
    # TypeError inside math.isclose instead of failing with a readable message.
    assert clade_a.branch_length is not None, "A lost its branch length in the collapse"
    assert math.isclose(clade_a.branch_length, 0.3, rel_tol=1e-9)

    # B is untouched by the collapse and must stay exactly where it was.
    clade_b = next(clade for clade in tree.find_clades() if clade.name == "B")
    assert math.isclose(clade_b.branch_length, 1.0, rel_tol=1e-9)
