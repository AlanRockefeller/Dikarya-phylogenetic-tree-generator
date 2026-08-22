"""Branch lengths must survive a viewer edit.

Biopython's Newick writer defaults to "%1.5f", which rounds every branch below
5e-6 to a hard zero. Dikarya's trees carry nine decimal places and RAxML-NG's
minimum branch length is 1e-6, so a single prune or reroot used to manufacture
hundreds of zero-length branches that the tree builder never produced -- and a
zero-length terminal branch reads as "these sequences are identical".
"""

from io import StringIO

import pytest

from Bio import Phylo

from app.services.tree_edit_service import (
    NEWICK_BRANCH_LENGTH_FORMAT,
    write_tree_file,
)


# Normal, very small positive, exactly zero, and one tip with no length at all.
SYNTHETIC = "((A:0.523456789,B:0.000000006)90:0.02,(C:0.0,D)80:0.000001,E:0.0000004);"


def _roundtrip(tmp_path, fmt="newick"):
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    path = tmp_path / f"tree.{fmt}"
    write_tree_file(tree, path, fmt)
    reloaded = Phylo.read(str(path), fmt)
    return path, {
        str(clade.name): clade.branch_length
        for clade in reloaded.get_terminals()
    }


def test_small_positive_branch_lengths_survive_serialization(tmp_path):
    _, lengths = _roundtrip(tmp_path)

    assert lengths["A"] == pytest.approx(0.523456789, rel=1e-9)
    assert lengths["B"] == pytest.approx(6e-9, rel=1e-6)
    assert lengths["E"] == pytest.approx(4e-7, rel=1e-6)


def test_genuine_zero_stays_zero(tmp_path):
    _, lengths = _roundtrip(tmp_path)

    assert lengths["C"] == 0.0


def test_default_biopython_format_would_have_lost_them(tmp_path):
    """Pin the reason this helper exists, so nobody drops it as redundant."""
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    path = tmp_path / "default.newick"
    Phylo.write(tree, str(path), "newick")  # deliberately not write_tree_file
    reloaded = Phylo.read(str(path), "newick")
    lengths = {str(c.name): c.branch_length for c in reloaded.get_terminals()}

    assert lengths["B"] == 0.0
    assert lengths["E"] == 0.0


def test_nexus_output_uses_the_same_precision(tmp_path):
    _, lengths = _roundtrip(tmp_path, "nexus")

    assert lengths["B"] == pytest.approx(6e-9, rel=1e-6)


def test_serialized_lengths_avoid_exponent_notation(tmp_path):
    """FigTree and MEGA are the destinations for a downloaded Newick."""
    path, _ = _roundtrip(tmp_path)

    text = path.read_text()
    assert "e-" not in text.lower()
    assert NEWICK_BRANCH_LENGTH_FORMAT == "%1.10f"
