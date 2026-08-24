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

import app.services.tree_io as tree_io
from app.services.tree_edit_service import (
    NEWICK_BRANCH_LENGTH_FORMAT,
    load_tree_state,
    midpoint_root,
    prune_taxa,
    reroot_tree,
    write_tree_file,
)
from app.services.tree_io import (
    tree_to_newick_string,
    validate_nexus_file,
    write_nexus_tree,
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


def test_an_absent_branch_length_stays_absent(tmp_path):
    """D carries no branch length, and must not acquire one.

    "No length was measured" and "this branch has length zero" are different
    claims, and the second is what makes a terminal branch read as an identical
    sequence -- the same false inference the precision tests above exist to
    prevent, arriving by a different route.

    Biopython renders every node as ``clade.branch_length or 0.0``, so it turned
    the first statement into the second. `_render_newick` writes no ``:length``
    token for such a clade, which is how Newick spells "unspecified".
    """
    _, lengths = _roundtrip(tmp_path)

    assert lengths["D"] is None


def test_absent_and_explicit_zero_are_not_the_same_value(tmp_path):
    """The distinction has to survive in one file, side by side.

    C is written ``C:0.0`` in the fixture and D has no length at all. If the
    writer ever collapses them again, this fails while the two single-value
    tests above could both still pass.
    """
    path, lengths = _roundtrip(tmp_path)

    assert lengths["C"] == 0.0
    assert lengths["D"] is None
    assert lengths["C"] is not lengths["D"]

    text = path.read_text()
    assert "C:0.0000000000" in text, text
    # D appears with no colon after it: either "D," or "D)" depending on where
    # it sits in the tree.
    assert "D:" not in text, text


def test_a_length_less_root_does_not_gain_a_zero(tmp_path):
    """297 of 300 production trees have a root with no branch length.

    Every one of them used to be rewritten with a trailing ``:0.0000000000``
    the builder never produced.
    """
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    assert tree.root.branch_length is None

    path = tmp_path / "root.newick"
    write_tree_file(tree, path, "newick")

    assert path.read_text().strip().endswith(");")
    assert Phylo.read(str(path), "newick").root.branch_length is None


def test_serializing_does_not_mutate_the_caller_s_tree(tmp_path):
    """The absent lengths are carried through Biopython as a sentinel value.

    That sentinel must never be visible to the caller, including when writing
    raises partway.
    """
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    before = [(clade.name, clade.branch_length) for clade in tree.find_clades()]

    write_tree_file(tree, tmp_path / "a.newick", "newick")
    write_tree_file(tree, tmp_path / "b.nexus", "nexus")

    after = [(clade.name, clade.branch_length) for clade in tree.find_clades()]
    assert before == after


def test_a_label_containing_the_sentinel_token_is_not_corrupted(tmp_path):
    """The sentinel substitution is exact, not merely improbable.

    A taxon label is copied into the output verbatim, and quoting does not
    protect it from a plain string replacement, so a label that happens to
    contain the sentinel's rendered token must push the writer onto a different
    sentinel rather than have the text cut out of its name.
    """
    hostile = "weird:-1.0000000000 isolate"
    newick = f"(('{hostile}':0.5,B)90:0.02,C:0.25);"
    tree = Phylo.read(StringIO(newick), "newick")

    path = tmp_path / "hostile.newick"
    write_tree_file(tree, path, "newick")

    reloaded = Phylo.read(str(path), "newick")
    names = {clade.name for clade in reloaded.get_terminals()}
    assert hostile in names, names
    lengths = {clade.name: clade.branch_length for clade in reloaded.get_terminals()}
    assert lengths[hostile] == pytest.approx(0.5)
    assert lengths["B"] is None


def test_a_real_negative_branch_length_does_not_collide_with_the_sentinel(tmp_path):
    """Biopython's NJ can emit negative branch lengths.

    The sentinel is negative too, so it is chosen against the tree's actual
    values rather than assumed to be unused.
    """
    tree = Phylo.read(StringIO("((A:-1.0,B)90:0.02,C:-2.0);"), "newick")

    path = tmp_path / "negative.newick"
    write_tree_file(tree, path, "newick")

    lengths = {
        clade.name: clade.branch_length
        for clade in Phylo.read(str(path), "newick").get_terminals()
    }
    assert lengths["A"] == pytest.approx(-1.0)
    assert lengths["C"] == pytest.approx(-2.0)
    assert lengths["B"] is None


def test_every_initial_sentinel_collision_still_preserves_absent_lengths(tmp_path):
    """Collision avoidance cannot have a finite semantic failure path."""
    real_lengths = ",".join(
        f"N{index}:{-float(index)}" for index in range(1, 13)
    )
    tree = Phylo.read(StringIO(f"({real_lengths},Missing);"), "newick")

    path = tmp_path / "many-negative-lengths.newick"
    write_tree_file(tree, path, "newick")
    reloaded = Phylo.read(str(path), "newick")
    lengths = {tip.name: tip.branch_length for tip in reloaded.get_terminals()}

    assert lengths["Missing"] is None
    for index in range(1, 13):
        assert lengths[f"N{index}"] == pytest.approx(-float(index))


def test_comments_containing_sentinel_text_are_preserved(tmp_path):
    tree = Phylo.read(StringIO("(A[keep:-1.0000000000],B);"), "newick")

    path = tmp_path / "comment.newick"
    write_tree_file(tree, path, "newick")

    text = path.read_text()
    assert "[keep:-1.0000000000]" in text
    reloaded = Phylo.read(str(path), "newick")
    tips = {tip.name: tip for tip in reloaded.get_terminals()}
    assert tips["A"].comment == "keep:-1.0000000000"
    assert tips["B"].branch_length is None


def test_newick_renderer_restores_absent_lengths_when_biopython_raises(monkeypatch):
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    before = [(clade.name, clade.branch_length) for clade in tree.find_clades()]

    def fail(_tree):
        raise OSError("simulated serializer failure")

    monkeypatch.setattr(tree_io, "_biopython_newick", fail)
    with pytest.raises(OSError, match="simulated serializer failure"):
        tree_io.tree_to_newick_string(tree)

    assert [(clade.name, clade.branch_length) for clade in tree.find_clades()] == before


def test_write_tree_file_does_not_mutate_tree_when_disk_write_raises(
    tmp_path, monkeypatch
):
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    before = [(clade.name, clade.branch_length) for clade in tree.find_clades()]

    def fail_write_text(self, data, encoding=None):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(tree_io.Path, "write_text", fail_write_text)
    with pytest.raises(OSError, match="simulated disk failure"):
        write_tree_file(tree, tmp_path / "failed.newick", "newick")

    assert [(clade.name, clade.branch_length) for clade in tree.find_clades()] == before


def test_nexus_omits_the_token_for_an_absent_length(tmp_path):
    """The same distinction has to reach the NEXUS file.

    Asserted on the file text rather than on a Biopython round trip: its NEXUS
    *reader* substitutes 0.0 for a missing length, which is a reader limitation
    and not something this writer can do anything about.
    """
    path = tmp_path / "tree.nexus"
    tree = Phylo.read(StringIO(SYNTHETIC), "newick")
    write_tree_file(tree, path, "nexus")

    text = path.read_text()
    tree_line = next(line for line in text.splitlines() if line.strip().startswith("TREE "))
    # Tips are integers in the TRANSLATE form; D is the fourth terminal.
    assert "3:0.0000000000" in tree_line, tree_line   # C, an explicit zero
    assert "4:" not in tree_line, tree_line           # D, no length at all
    ok, reason = validate_nexus_file(path)
    assert ok, reason


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


def _internal_annotations(path, fmt="newick"):
    tree = Phylo.read(str(path), fmt)
    all_tips = {tip.name for tip in tree.get_terminals()}
    found = {}
    for clade in tree.get_nonterminals():
        descendants = {tip.name for tip in clade.get_terminals()}
        complement = all_tips - descendants
        if not complement:
            continue
        side = min(
            (tuple(sorted(descendants)), tuple(sorted(complement))),
            key=lambda names: (len(names), names),
        )
        annotation = clade.confidence if clade.confidence is not None else clade.name
        if annotation is not None:
            found[side] = annotation
    return found


@pytest.mark.parametrize(
    ("newick", "expected"),
    [
        ("((A:0.1,B:0.1)95:0.2,(C:0.1,D:0.1)80:0.2,E:0.2);",
         {("A", "B"): 95.0, ("C", "D"): 80.0}),
        ("((A:0.1,B:0.1)82.7/87:0.2,(C:0.1,D:0.1)70/91:0.2,E:0.2);",
         {("A", "B"): "82.7/87", ("C", "D"): "70/91"}),
        ("((A:0.1,B:0.1)CladeAB:0.2,(C:0.1,D:0.1)CladeCD:0.2,E:0.2);",
         {("A", "B"): "CladeAB", ("C", "D"): "CladeCD"}),
    ],
)
def test_support_and_internal_labels_survive_repeated_root_write_read_cycles(
    tmp_path, newick, expected
):
    tree_dir = tmp_path / "tree"
    tree_dir.mkdir()
    original = tree_dir / "tree_original.newick"
    original.write_text(newick)

    # Initial state creation applies the site's default midpoint root and writes
    # the editable tree. Exercise two more independent read/edit/write cycles.
    state = load_tree_state(tmp_path)
    for operation in (
        lambda current: reroot_tree(tmp_path, current, "E"),
        lambda current: midpoint_root(tmp_path, current),
    ):
        newick_path = tree_dir / "tree_pruned.newick"
        nexus_path = tree_dir / "tree_pruned.nexus"
        assert _internal_annotations(newick_path) == expected
        assert _internal_annotations(nexus_path, "nexus") == expected
        assert "Node_" not in newick_path.read_text()
        state = operation(state)

    assert _internal_annotations(tree_dir / "tree_pruned.newick") == expected

    # Pruning destroys the A/B split, but the independent C/D branch survives
    # another read/edit/write cycle with its annotation intact.
    state = prune_taxa(tmp_path, state, ["A"])
    surviving = set(_internal_annotations(tree_dir / "tree_pruned.newick").values())
    assert expected[("C", "D")] in surviving
    assert set(_internal_annotations(tree_dir / "tree_pruned.nexus", "nexus").values()) == surviving
    assert original.read_text() == newick


def test_newick_writer_rejects_name_plus_confidence_instead_of_concatenating():
    tree = Phylo.read(StringIO("((A,B)CladeAB,C);"), "newick")
    internal = next(clade for clade in tree.get_nonterminals() if clade.name)
    internal.confidence = 95
    with pytest.raises(ValueError, match="both a name and a confidence"):
        tree_to_newick_string(tree)


@pytest.mark.parametrize(
    "newick, message",
    [
        ("(A,A);", "duplicate terminal taxon label"),
        ("(A,:0.1);", "has no label"),
    ],
)
def test_nexus_writer_rejects_ambiguous_terminal_taxa(tmp_path, newick, message):
    tree = Phylo.read(StringIO(newick), "newick")
    with pytest.raises(ValueError, match=message):
        write_nexus_tree(tree, tmp_path / "invalid.nexus")


def test_nexus_writer_round_trips_punctuated_and_quoted_taxa(tmp_path):
    labels = ["O'Brien sample", "Zeng3026(FHMU1987)", "A;B", "comma,label"]
    tree = Phylo.read(StringIO("(A:0.1,B:0.2,C:0.3,D:0.4);"), "newick")
    for tip, label in zip(tree.get_terminals(), labels):
        tip.name = label

    path = tmp_path / "punctuation.nexus"
    write_nexus_tree(tree, path)
    reloaded = Phylo.read(str(path), "nexus")

    # Biopython's NEXUS reader retains the legal quote delimiters in translated
    # labels, but it still parses the full tree and every taxon remains distinct.
    assert len(reloaded.get_terminals()) == len(labels)
    text = path.read_text()
    assert "'O''Brien sample'" in text
    assert "'Zeng3026(FHMU1987)'" in text
    assert "'A;B'" in text
    assert "'comma,label'" in text


def test_nexus_writer_round_trips_ordinary_taxa_exactly(tmp_path):
    tree = Phylo.read(StringIO("(Alpha:0.1,Beta:0.2,Gamma:0.3);"), "newick")
    path = tmp_path / "ordinary.nexus"
    write_nexus_tree(tree, path)

    reloaded = Phylo.read(str(path), "nexus")
    assert [tip.name for tip in reloaded.get_terminals()] == ["Alpha", "Beta", "Gamma"]
