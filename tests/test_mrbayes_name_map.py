"""The MrBayes download ships a key that describes the run it sits beside.

Every taxon in a MrBayes run is a SEQnnnnnn id, so the download bundles a
sequence_names.tsv decoder. Recompute renumbers those ids -- dropping an early
sequence shifts every later one -- so a map or an alignment from the wrong
generation decodes the run to the wrong sequences.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.services.fasta_utils import NAME_MAP_FILENAME

ORIGINAL = [
    ("Amanita muscaria one", "ACGTACGT"),
    ("Amanita pantherina two", "ACGTACGA"),
    ("Russula emetica three", "ACGTACGC"),
]
# The recompute pruned the FIRST record, so what was SEQ000002 is now SEQ000001.
PRUNED = ORIGINAL[1:]


def write_fasta(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f">{h}\n{s}\n" for h, s in records), encoding="utf-8")


def write_nexus(path, ntax):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#NEXUS\nbegin data;\n  dimensions ntax={ntax} nchar=8;\n", encoding="utf-8"
    )


def recomputed_job(root, *, stored_map=None):
    job_dir = Path(root) / "job"
    write_fasta(job_dir / "alignment" / "alignment_raw.fasta", ORIGINAL)
    write_fasta(job_dir / "alignment" / "alignment_trimmed.fasta", ORIGINAL)
    write_fasta(job_dir / "alignment" / "alignment_pruned_aligned.fasta", PRUNED)
    write_fasta(job_dir / "alignment" / "alignment_pruned_trimmed.fasta", PRUNED)
    (job_dir / "tree").mkdir(parents=True, exist_ok=True)
    (job_dir / "tree" / "tree_pruned.newick").write_text("(a,b);")
    (job_dir / "tree" / "tree_pruned_metadata.json").write_text("{}")
    write_nexus(job_dir / "tree" / "mrbayes_input.nex", len(PRUNED))
    if stored_map is not None:
        (job_dir / "tree" / NAME_MAP_FILENAME).write_text(stored_map, encoding="utf-8")
    return job_dir


class MrBayesNameMapTests(unittest.TestCase):
    def setUp(self):
        from app.api.routes import _mrbayes_name_map_text

        self.resolve = _mrbayes_name_map_text
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_a_legacy_recompute_is_reconstructed_from_its_pruned_alignment(self):
        text = self.resolve(recomputed_job(self.root))
        self.assertIn("SEQ000001\tAmanita pantherina two", text)
        self.assertIn("SEQ000002\tRussula emetica three", text)
        self.assertNotIn("Amanita muscaria one", text)

    def test_a_stale_map_from_the_previous_generation_is_not_used(self):
        stale = (
            "SEQ000001\tAmanita muscaria one\n"
            "SEQ000002\tAmanita pantherina two\n"
            "SEQ000003\tRussula emetica three\n"
        )
        text = self.resolve(recomputed_job(self.root, stored_map=stale))
        self.assertIn("SEQ000001\tAmanita pantherina two", text)
        self.assertNotIn("Amanita muscaria one", text)

    def test_a_stale_map_with_the_right_row_count_is_not_used(self):
        """A recompute that drops one sequence and adds another keeps the count.

        Every id after the drop then stands for a different sequence, so a row
        count is no evidence at all that the map describes this run.
        """
        job_dir = Path(self.root) / "swapped"
        write_fasta(job_dir / "alignment" / "alignment_trimmed.fasta", ORIGINAL)
        # The recompute dropped record one and added a new one: still three taxa.
        current = ORIGINAL[1:] + [("Lactarius rufus four", "ACGTACGG")]
        write_fasta(job_dir / "alignment" / "alignment_pruned_trimmed.fasta", current)
        (job_dir / "tree").mkdir(parents=True, exist_ok=True)
        (job_dir / "tree" / "tree_pruned.newick").write_text("(a,b,c);")
        (job_dir / "tree" / "tree_pruned_metadata.json").write_text("{}")
        write_nexus(job_dir / "tree" / "mrbayes_input.nex", len(current))
        (job_dir / "tree" / NAME_MAP_FILENAME).write_text(
            "SEQ000001\tAmanita muscaria one\n"
            "SEQ000002\tAmanita pantherina two\n"
            "SEQ000003\tRussula emetica three\n",
            encoding="utf-8",
        )

        text = self.resolve(job_dir)

        self.assertIn("SEQ000001\tAmanita pantherina two", text)
        self.assertIn("SEQ000003\tLactarius rufus four", text)
        self.assertNotIn("Amanita muscaria one", text)

    def test_a_map_is_still_served_when_no_alignment_can_check_it(self):
        """Reclaimed alignments leave the stored map as the only evidence."""
        job_dir = Path(self.root) / "no-alignment"
        write_nexus(job_dir / "tree" / "mrbayes_input.nex", 2)
        stored = "SEQ000001\tOnly evidence\nSEQ000002\tSecond row\n"
        (job_dir / "tree" / NAME_MAP_FILENAME).write_text(stored, encoding="utf-8")

        self.assertEqual(self.resolve(job_dir), stored)

    def test_the_run_s_own_map_is_served_verbatim(self):
        current = (
            "# key\nSEQ000001\tAmanita pantherina two\n"
            "SEQ000002\tRussula emetica three\n"
        )
        self.assertEqual(
            self.resolve(recomputed_job(self.root, stored_map=current)), current
        )

    def test_a_gzipped_pruned_alignment_still_decodes(self):
        """Cold artifacts are stored gzipped; the key must survive that."""
        import gzip

        job_dir = recomputed_job(self.root)
        for name in ("alignment_pruned_trimmed.fasta", "alignment_pruned_aligned.fasta"):
            plain = job_dir / "alignment" / name
            with gzip.open(str(plain) + ".gz", "wt") as handle:
                handle.write(plain.read_text())
            plain.unlink()

        text = self.resolve(job_dir)
        self.assertIn("SEQ000001\tAmanita pantherina two", text)
        self.assertNotIn("Amanita muscaria one", text)

    def test_an_unrecomputed_job_still_uses_the_original_alignment(self):
        job_dir = Path(self.root) / "plain"
        write_fasta(job_dir / "alignment" / "alignment_trimmed.fasta", ORIGINAL)
        write_nexus(job_dir / "tree" / "mrbayes_input.nex", len(ORIGINAL))
        text = self.resolve(job_dir)
        self.assertIn("SEQ000001\tAmanita muscaria one", text)
        self.assertIn("SEQ000003\tRussula emetica three", text)


if __name__ == "__main__":
    unittest.main()
