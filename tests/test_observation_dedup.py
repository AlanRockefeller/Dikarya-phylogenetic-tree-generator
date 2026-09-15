"""A GenBank deposit and its iNaturalist record are one collection.

The observation number that ties them together is not in the FASTA anywhere:
MycoMap's DB39 export gives accession/taxon/voucher/location, and NCBI's header
gives the DEFINITION line. It is in the GenBank *record* -- PX860295 says
"isolate S. D. Russell iNat # 280384724" -- so the dedup has to read the
annotation before it can group the two, and the surviving tip has to keep both
identifiers or the link the user just saw disappears from the tree.

The lookup itself is stubbed here; these tests are about what the dedup does
with the answer, not about NCBI being reachable.
"""

import unittest
from pathlib import Path

from app.services import sequence_dedup_service as dedup
from app.services.genbank_observation_service import (
    observation_reference_from_record,
)

# A concrete read, so the overlap comparison has something real to anchor on: a
# repeating motif lines up in several frames at once and scores as a mismatch.
SEQUENCE = (
    "TCCGTAGGTGAACCTGCGGAAGGATCATTATTGAATTTAGGGCACTTTCTGGCTTTGGCTGAGCCATCTC"
    "ACCCTTTGTGCACCATTTGTAGACCTTGGTGTGATAAGTTGTCTGTGCTTTTACACACATGTGCATGTTT"
    "GAGTGTCATTAAATTCTCAACCTCTAACAGTCTTTGAGGCAATTTGGATTTGGGGGTTTGCTGGCTTTAT"
    "ATGAGTCAGCTCCTCTTAAATGCATTAGCGGGATCTCTGCGTTTCAATGTGTAGGTGTTTTCTGCTTTAT"
    "TTGCTATATCCATGTGTCTATGTAAGTTTATATAGCTTCTAACCGTCTCTTGAAGAGTTTGGAGATTGAT"
    "CTTGGCTTTCCTCTTGCACAGGACAATTAAATGTCTTTGAAAGAAGTCCTTGCAGGTTTTGGTCTAAGAA"
)


def _fasta(*records):
    return "".join(f">{name}\n{sequence}\n" for name, sequence in records)


class GenBankObservationReferenceTests(unittest.TestCase):
    def test_reference_is_read_from_the_definition_line(self):
        record = {
            "definition": ("Panaeolus cinctulus isolate S. D. Russell iNat # "
                           "280384724 small subunit ribosomal RNA gene, partial"),
            "source_features": {},
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_reference_is_read_from_an_isolate_qualifier(self):
        record = {
            "definition": "Panaeolus cinctulus internal transcribed spacer",
            "source_features": {"isolate": "S. D. Russell iNat # 280384724"},
            "blob": "",
        }
        self.assertEqual(observation_reference_from_record(record), "inat:280384724")

    def test_a_record_with_no_observation_number_resolves_to_nothing(self):
        record = {
            "definition": "Amanita muscaria voucher MICH 12345 ITS region",
            "source_features": {"specimen_voucher": "MICH 12345"},
            "blob": "Amanita muscaria voucher MICH 12345 ITS region",
        }
        self.assertEqual(observation_reference_from_record(record), "")


class ObservationDedupTests(unittest.TestCase):
    def setUp(self):
        self._real_lookup = dedup._resolve_genbank_references

    def tearDown(self):
        dedup._resolve_genbank_references = self._real_lookup

    def _stub_lookup(self, mapping):
        """Answer as NCBI would for `mapping`, without going near the network."""
        def _resolve(records, references, accessions):
            filled = 0
            for index, accession in enumerate(accessions):
                if accession and not references[index] and accession in mapping:
                    references[index] = mapping[accession]
                    filled += 1
            return filled

        dedup._resolve_genbank_references = _resolve

    def test_genbank_and_inat_records_of_one_observation_collapse(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(
            fasta, resolve_genbank_references=True
        )

        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["observation_reference"], "inat:280384724")
        # The longer read survives, not whichever came first.
        self.assertEqual(text.count(">"), 1)
        self.assertIn(">PX860295 iNat280384724 Panaeolus cinctulus South Carolina US",
                      text)

    def test_the_surviving_tip_keeps_the_accession_when_the_import_is_longer(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE[20:-20]),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(
            fasta, resolve_genbank_references=True
        )

        self.assertEqual(len(removed), 1)
        self.assertIn(
            ">iNat280384724 PX860295 Panaeolus cinctulus Greenwood South Carolina US",
            text,
        )
        # Both names travel with the removal so the rebuild-with-duplicates
        # action can put the original label back.
        self.assertEqual(
            removed[0]["kept_original_name"],
            "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
        )

    def test_metadata_follows_the_merged_label(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )
        metadata = [
            {"name": "PX860295 Panaeolus cinctulus South Carolina US",
             "fasta_header": "PX860295 Panaeolus cinctulus South Carolina US",
             "accession": "PX860295"},
            {"name": "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             "fasta_header": "iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             "internal_id": "iNat280384724"},
        ]

        _text, kept_metadata, _removed = dedup.dedupe_by_observation(
            fasta, metadata, resolve_genbank_references=True
        )

        self.assertEqual(len(kept_metadata), 1)
        self.assertEqual(kept_metadata[0]["fasta_header"],
                         "PX860295 iNat280384724 Panaeolus cinctulus South Carolina US")
        # The label the record arrived with is still recoverable.
        self.assertEqual(kept_metadata[0]["raw_fasta_header"],
                         "PX860295 Panaeolus cinctulus South Carolina US")

    def test_two_different_observations_are_never_merged(self):
        self._stub_lookup({"PX860295": "inat:280384724",
                           "PX860296": "inat:999999999"})
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("PX860296 Panaeolus cinctulus South Carolina US", SEQUENCE),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(
            fasta, resolve_genbank_references=True
        )

        self.assertEqual(removed, [])
        self.assertEqual(text.count(">"), 2)

    def test_an_identifier_already_in_the_label_is_not_repeated(self):
        self._stub_lookup({"PX860295": "inat:280384724"})
        fasta = _fasta(
            ("PX860295 iNat280384724 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )

        text, _metadata, removed = dedup.dedupe_by_observation(
            fasta, resolve_genbank_references=True
        )

        self.assertEqual(len(removed), 1)
        self.assertEqual(text.count("iNat280384724"), 1)

    def test_the_lookup_can_be_switched_off(self):
        def _explode(*args, **kwargs):
            raise AssertionError("NCBI must not be consulted")

        dedup._resolve_genbank_references = _explode
        fasta = _fasta(
            ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
            ("iNat280384724 Panaeolus cinctulus Greenwood South Carolina US",
             SEQUENCE[20:-20]),
        )

        _text, _metadata, removed = dedup.dedupe_by_observation(
            fasta, resolve_genbank_references=False
        )

        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()


class LookupPlacementTests(unittest.TestCase):
    """The NCBI annotation lookup belongs in the worker, not in the request.

    Alan 9/14/26 - `dedupe_by_observation` used to reach the internet by
    default, and `prepare_phylo_job_params` calls it from inside POST
    /api/job. Every submission containing an accession could therefore spend up
    to DEFAULT_LOOKUP_SECONDS of efetch holding one of the (workers x threads)
    request slots -- for an enrichment that changes nothing a user sees until
    the worker builds the tree anyway.

    The submit path still does the offline grouping, so the payload that
    reaches the worker is already deduped on the references its own FASTA
    carries; the worker pass adds only the ones that live in GenBank's
    annotation.
    """

    def test_the_low_level_default_is_offline(self):
        import inspect

        signature = inspect.signature(dedup.dedupe_by_observation)
        self.assertIs(
            signature.parameters["resolve_genbank_references"].default, False
        )
        self.assertIs(
            inspect.signature(dedup.apply_observation_dedup)
            .parameters["resolve_genbank_references"].default,
            False,
        )

    def test_the_submit_path_never_asks_ncbi(self):
        from app.workers import queue as worker_queue

        def _explode(*args, **kwargs):
            raise AssertionError("NCBI must not be consulted at submit time")

        real = dedup._resolve_genbank_references
        self.addCleanup(setattr, dedup, "_resolve_genbank_references", real)
        dedup._resolve_genbank_references = _explode

        job_params = {
            "sequence": _fasta(
                ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
                ("iNat280384724 Panaeolus cinctulus Greenwood", SEQUENCE[20:-20]),
            ),
            "sequence_metadata": [],
            "accessions": [],
        }
        worker_queue.prepare_phylo_job_params(job_params)

        # Nothing was collapsed, because only GenBank's annotation ties these
        # two together -- and that is the worker's job now.
        self.assertEqual(job_params["sequence"].count(">"), 2)

    def test_the_worker_runs_the_resolving_pass_for_every_job(self):
        source = open(
            Path(__file__).resolve().parents[1] / "app" / "workers" / "tasks.py"
        ).read()
        self.assertIn(
            "apply_observation_dedup(\n                job_params, "
            "resolve_genbank_references=True\n            )",
            source,
        )
        # And it runs before the pipeline touches the sequences.
        self.assertLess(
            source.index("resolve_genbank_references=True"),
            source.index('publish_step_start(job_id, STEP_INPUT'),
        )

    def test_the_prepared_import_pass_is_still_there(self):
        # iNaturalist and Mushroom Observer assemble their sequences in the
        # worker, so the submit-time pass saw an empty payload.
        source = open(
            Path(__file__).resolve().parents[1] / "app" / "workers" / "tasks.py"
        ).read()
        self.assertIn("removed_duplicates = apply_observation_dedup(job_params)", source)


class InputWarningRefreshTests(unittest.TestCase):
    """Warnings describe the set the pipeline will align, not the one submitted.

    Alan 9/15/26 - describe_degenerate_input() has always run *after* the
    observation dedup, because its warnings depend on the record count and its
    text quotes it. Moving the GenBank-resolving pass into the worker broke
    that: a three-record submission whose second and third records are the same
    observation -- linked only by GenBank's annotation, so invisible at submit
    time -- reaches the worker, collapses to two, and the "a two-sequence tree
    cannot show grouping" warning is never produced. The status page reads the
    warnings off Job.metrics, so the user simply never sees it.
    """

    def setUp(self):
        self._real_lookup = dedup._resolve_genbank_references
        self.addCleanup(
            setattr, dedup, "_resolve_genbank_references", self._real_lookup
        )

        def _resolve(records, references, accessions):
            filled = 0
            for index, accession in enumerate(accessions):
                if accession == "PX860295" and not references[index]:
                    references[index] = "inat:280384724"
                    filled += 1
            return filled

        dedup._resolve_genbank_references = _resolve

    @staticmethod
    def _warnings(job_params):
        from app.workers.queue import apply_input_warnings

        return apply_input_warnings(job_params)

    def test_three_records_warrant_no_warning(self):
        params = {"sequence": _fasta(
            ("A", SEQUENCE), ("B", SEQUENCE[5:]), ("C", SEQUENCE[:-5])
        )}
        self.assertEqual(self._warnings(params), [])
        self.assertNotIn("input_warnings", params)

    def test_two_records_warrant_the_grouping_warning(self):
        params = {"sequence": _fasta(("A", SEQUENCE), ("B", SEQUENCE[5:]))}
        warnings = self._warnings(params)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Only two sequences", warnings[0])
        self.assertEqual(params["input_warnings"], warnings)

    def test_a_refresh_retracts_a_warning_that_no_longer_applies(self):
        # Warnings are recomputed wholesale, not appended to, so a stale one is
        # replaced rather than accumulated.
        params = {
            "sequence": _fasta(("A", SEQUENCE), ("B", SEQUENCE[5:]), ("C", SEQUENCE)),
            "input_warnings": ["Only two sequences were submitted. Stale."],
        }
        self.assertEqual(self._warnings(params), [])
        self.assertNotIn("input_warnings", params)

    def test_a_refresh_corrects_the_count_quoted_in_the_text(self):
        params = {"sequence": _fasta(("A", SEQUENCE), ("B", SEQUENCE), ("C", SEQUENCE))}
        self.assertIn("All 3 submitted sequences are identical",
                      " ".join(self._warnings(params)))
        params["sequence"] = _fasta(("A", SEQUENCE), ("B", SEQUENCE))
        refreshed = " ".join(self._warnings(params))
        self.assertIn("All 2 submitted sequences are identical", refreshed)
        self.assertNotIn("All 3", refreshed)

    def test_the_regression_end_to_end(self):
        """Submit leaves three records; the worker's pass collapses them to two."""
        from app.workers.queue import prepare_phylo_job_params

        job_params = {
            "sequence": _fasta(
                ("PX860295 Panaeolus cinctulus South Carolina US", SEQUENCE),
                ("iNat280384724 Panaeolus cinctulus Greenwood", SEQUENCE[20:-20]),
                ("OR807397 Panaeolus olivaceus Oregon US", SEQUENCE[:-40]),
            ),
            "sequence_metadata": [],
            "accessions": [],
        }

        # Submit time: offline, so the GenBank/iNat pair is not yet linked.
        prepare_phylo_job_params(job_params)
        self.assertEqual(job_params["sequence"].count(">"), 3)
        self.assertEqual(job_params.get("input_warnings"), None)

        # Worker: the annotation lookup links them, leaving two tips.
        removed = dedup.apply_observation_dedup(
            job_params, resolve_genbank_references=True
        )
        self.assertEqual(removed, 1)
        self.assertEqual(job_params["sequence"].count(">"), 2)

        refreshed = self._warnings(job_params)
        self.assertEqual(len(refreshed), 1)
        self.assertIn("Only two sequences", refreshed[0])
        self.assertEqual(job_params["input_warnings"], refreshed)

    def test_the_worker_refreshes_both_copies_after_a_removal(self):
        source = open(
            Path(__file__).resolve().parents[1] / "app" / "workers" / "tasks.py"
        ).read()
        index = source.index("resolved_duplicates = apply_observation_dedup(")
        window = source[index:index + 2000]
        # job_params on disk...
        self.assertIn("refreshed_warnings = apply_input_warnings(job_params)", window)
        self.assertIn("_save_job_params(input_info_path, job_params)", window)
        # ...and the Job.metrics copy the status page actually renders.
        self.assertIn('metrics["input_warnings"] = refreshed_warnings', window)
        self.assertIn("db.session.commit()", window)
        # Advisory metadata must never be the thing that fails a job.
        self.assertIn("db.session.rollback()", window)

    def test_the_submit_path_uses_the_same_helper(self):
        # One definition of "what counts as degenerate input", so the two
        # passes cannot disagree about it.
        source = open(
            Path(__file__).resolve().parents[1] / "app" / "workers" / "queue.py"
        ).read()
        self.assertIn("apply_input_warnings(job_params)", source)
        # Exactly one call site, inside the helper both passes go through.
        self.assertEqual(source.count("describe_degenerate_input("), 1)
