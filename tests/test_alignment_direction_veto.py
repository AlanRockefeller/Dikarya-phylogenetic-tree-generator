"""The k-mer veto over MAFFT's direction call.

Job e4d73c31 is the case this guards: a 1742 bp rDNA read among ~512 bp ITS
barcodes, homologous over its first 530 bp only, which
``--adjustdirectionaccurately`` reverse-complemented despite overwhelming
forward k-mer agreement. It reached the tree as a 0.83 terminal branch beside a
next-longest of 0.017; left forward, the same tip sits at 8e-9.

Job 8d7a0d15 is the case that must keep working: a genuinely reversed sequence
ORIENT had declined to call, where MAFFT was right and the veto must stay out
of the way.
"""

import logging
import unittest

from app.services import alignment_service as A
from app.services.orientation_service import revcomp


def _seq(seed: int, length: int = 600) -> str:
    """A deterministic pseudo-random sequence with no shared k-mers by luck."""
    import random

    rng = random.Random(seed)
    return "".join(rng.choice("ACGT") for _ in range(length))


class KmerOrientationVetoTests(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("test")
        self.core = _seq(1)
        # A family of near-identical barcodes: shared core, private tails.
        self.records = [(f"ref{i}", self.core + _seq(100 + i, 40)) for i in range(10)]

    def test_a_forward_sequence_with_a_long_unrelated_tail_is_not_flipped(self):
        """The e4d73c31 shape: homologous head, 1200 bp of nothing else."""
        target = ("target", self.core + _seq(999, 1200))
        records = self.records + [target]

        self.assertEqual(
            A.kmer_orientation_veto(records, {"target"}, self.log), {"target"}
        )

    def test_a_genuinely_reversed_sequence_is_left_alone(self):
        """MAFFT is usually right, and the veto must not second-guess it."""
        target = ("target", revcomp(self.core))
        records = self.records + [target]

        self.assertEqual(A.kmer_orientation_veto(records, {"target"}, self.log), set())

    def test_a_sequence_sharing_nothing_is_left_alone(self):
        """No evidence either way is not evidence against MAFFT."""
        target = ("target", _seq(4242))
        records = self.records + [target]

        self.assertEqual(A.kmer_orientation_veto(records, {"target"}, self.log), set())

    def test_no_veto_without_at_least_two_unflipped_references(self):
        """The reference frame is the sequences MAFFT left alone."""
        records = [("a", self.core), ("b", self.core)]

        self.assertEqual(A.kmer_orientation_veto(records, {"a", "b"}, self.log), set())
        self.assertEqual(A.kmer_orientation_veto(records, {"a"}, self.log), set())

    def test_ambiguity_codes_do_not_produce_phantom_matches(self):
        """A run of N cannot be packed into two bits per base, so it ends the k-mer."""
        self.assertEqual(A._encode_kmers("N" * 50), set())
        self.assertEqual(A._encode_kmers("ACGT" * 2), A._encode_kmers("ACGT" * 2))
        self.assertEqual(len(A._encode_kmers("A" * (A.KMER_VETO_K - 1))), 0)
        self.assertEqual(len(A._encode_kmers("A" * A.KMER_VETO_K)), 1)

    def test_reference_pool_is_capped(self):
        """A large submission must not build a pool of every k-mer it contains."""
        records = [(f"r{i}", _seq(i, 5000)) for i in range(500)]
        pool = A._reference_kmer_pool(records, set())

        self.assertLessEqual(
            len(pool),
            A.KMER_VETO_MAX_REFERENCES * 5000,
        )


if __name__ == "__main__":
    unittest.main()
