"""ORIENT's reversal rule, on synthetic ITS-like reads."""
import random

from app.services import orientation_service as o

ITS1F, CORE, ITS4 = o.FWD_MOTIFS


def _filler(n, seed):
    rng = random.Random(seed)
    return "".join(rng.choice("ACGT") for _ in range(n))


def _with_mismatches(motif, positions):
    swap = {"A": "C", "C": "A", "G": "T", "T": "G"}
    return "".join(swap[b] if i in positions else b for i, b in enumerate(motif))


def test_reversed_read_with_only_a_core_is_reversed():
    # Trimmed inside ITS1 and ITS2: no primer site on either strand, so the
    # 5.8S core is the only evidence (job y4y8's records).
    forward_read = _filler(150, 1) + CORE + _filler(150, 2)

    call, fwd, _ = o.decide_orientation(o.revcomp(forward_read))

    assert not fwd.hits
    assert call == "reverse"


def test_reverse_winner_without_flank_is_uncertain_when_forward_has_a_hit():
    # Reverse wins on mismatches (exact core vs a 4-mismatch ITS1-F), but with
    # forward evidence present the reversal still needs a flank.
    weak_its1f = _with_mismatches(ITS1F, {2, 7, 11, 16})
    seq = _filler(80, 3) + weak_its1f + _filler(120, 4) + o.revcomp(CORE) + _filler(80, 5)

    call, fwd, rev = o.decide_orientation(seq)

    assert fwd.hits and rev.hits
    assert call == "uncertain"

    fixed, stats = o.fix_sequence_orientation(f">rec1\n{seq}\n")
    assert o.fasta_reader(fixed) == [("rec1", seq)]
    assert stats["uncertain_headers"] == ["rec1"]
    assert stats["reverse"] == 0


def test_forward_read_with_only_a_core_stays_forward():
    seq = _filler(150, 6) + CORE + _filler(150, 7)

    call, _, _ = o.decide_orientation(seq)

    assert call == "forward"
