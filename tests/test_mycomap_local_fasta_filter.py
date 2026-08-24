"""Pure helper coverage for optional MycoMap local FASTA conflict filtering."""

import unittest
from unittest.mock import patch

from app.api.routes import (
    _mycomap_label_has_query_token,
    _mycomap_local_fasta_metric_conflict_detail,
    gather_mycomap_sequences_for_queue,
)


QUERY_SEQUENCE = "ACGT" * 30
QUERY_TOKENS = {"inat123456"}
QUERY_RECORDS = [{
    "sequence": QUERY_SEQUENCE,
    "location": "California US",
}]
LOW_IDENTITY_METRIC = {
    "identity": 88.0,
    "mycomap_location": "Oregon US",
}
MOCK_LOCAL_FASTA = (
    ">iNat123456 Amanita example California US\n"
    f"{QUERY_SEQUENCE}\n"
    ">iNat987654 Amanita example Oregon US\n"
    f"{QUERY_SEQUENCE}\n"
)
MOCK_FASTA_RESULT = {
    "fasta_content": MOCK_LOCAL_FASTA,
    "ncbi_count": 0,
    "local_count": 2,
    "errors": [],
}
MOCK_METRICS = {"iNat987654": LOW_IDENTITY_METRIC}
MOCK_BLAST_URL = "https://mycomap.com/genetics/blast-search/c01-inat123456-r42"


def _local_hit(*, location="Oregon US", name="iNat987654 Amanita example Oregon US"):
    return {
        "hit_source": "local",
        "name": name,
        "sequence": QUERY_SEQUENCE,
        "location": location,
    }


class TestMycomapLocalFastaConflictFilter(unittest.TestCase):
    """Exercise the strict opt-in conflict policy without fetching MycoMap data."""

    def test_strict_direct_conflict_returns_filter_detail(self):
        detail = _mycomap_local_fasta_metric_conflict_detail(
            _local_hit(),
            LOW_IDENTITY_METRIC,
            QUERY_RECORDS,
            QUERY_TOKENS,
            allow_identical_sequences_different_locations=False,
        )

        self.assertEqual(detail["reason"], "local_fasta_identity_conflict")
        self.assertEqual(detail["reported_identity"], 88.0)
        self.assertEqual(detail["query_similarity"], 100.0)

    def test_identical_sequence_at_distinct_locations_is_allowed(self):
        detail = _mycomap_local_fasta_metric_conflict_detail(
            _local_hit(),
            LOW_IDENTITY_METRIC,
            QUERY_RECORDS,
            QUERY_TOKENS,
            allow_identical_sequences_different_locations=True,
        )

        self.assertIsNone(detail)

    def test_same_or_unknown_location_stays_filterable_when_allowed(self):
        cases = (
            (
                "California US",
                {"identity": 88.0, "mycomap_location": "California US"},
                "iNat987654 Amanita example Oregon US",
            ),
            ("", {"identity": 88.0}, "iNat987654 Amanita example"),
        )

        for location, metric, name in cases:
            with self.subTest(location=location or "unknown"):
                detail = _mycomap_local_fasta_metric_conflict_detail(
                    _local_hit(location=location, name=name),
                    metric,
                    QUERY_RECORDS,
                    QUERY_TOKENS,
                    allow_identical_sequences_different_locations=True,
                )

                self.assertEqual(detail["reason"], "local_fasta_identity_conflict")

    def test_distinct_location_is_filterable_when_allowance_is_disabled(self):
        detail = _mycomap_local_fasta_metric_conflict_detail(
            _local_hit(),
            LOW_IDENTITY_METRIC,
            QUERY_RECORDS,
            QUERY_TOKENS,
            allow_identical_sequences_different_locations=False,
        )

        self.assertEqual(detail["reason"], "local_fasta_identity_conflict")

    def test_exact_match_requires_every_query_location_to_differ(self):
        query_records = [
            *QUERY_RECORDS,
            {"sequence": QUERY_SEQUENCE, "location": "Oregon US"},
        ]
        detail = _mycomap_local_fasta_metric_conflict_detail(
            _local_hit(),
            LOW_IDENTITY_METRIC,
            query_records,
            QUERY_TOKENS,
            allow_identical_sequences_different_locations=True,
        )

        self.assertEqual(detail["reason"], "local_fasta_identity_conflict")

    def test_query_token_matching_does_not_accept_partial_inat_ids(self):
        self.assertTrue(_mycomap_label_has_query_token("iNaturalist # 123456", QUERY_TOKENS))
        self.assertFalse(_mycomap_label_has_query_token("iNat9123456", QUERY_TOKENS))

    def test_gather_default_keeps_conflict_but_strict_mode_drops_it(self):
        with (
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch("app.services.mycomap_service.fetch_mycomap_fasta", return_value=MOCK_FASTA_RESULT),
            patch("app.services.mycomap_service.fetch_mycomap_blast_metrics", return_value=MOCK_METRICS),
            patch(
                "app.services.mycomap_service.improve_mycomap_sequence_name",
                side_effect=lambda name, *_, **__: name,
            ),
            patch("app.services.blast_service.fetch_fasta_for_accessions", return_value=""),
        ):
            default_payload, default_error = gather_mycomap_sequences_for_queue(
                MOCK_BLAST_URL,
                include_ncbi=False,
                include_local=True,
            )
            strict_payload, strict_error = gather_mycomap_sequences_for_queue(
                MOCK_BLAST_URL,
                include_ncbi=False,
                include_local=True,
                filter_conflicting_local_fasta=True,
                allow_identical_sequences_different_locations=False,
            )

        self.assertIsNone(default_error)
        self.assertFalse(default_payload["conflicting_local_filter_enabled"])
        self.assertEqual(default_payload["conflicting_local_count"], 0)
        self.assertEqual(len(default_payload["sequences"]), 2)
        self.assertIsNone(strict_error)
        self.assertTrue(strict_payload["conflicting_local_filter_enabled"])
        self.assertEqual(strict_payload["conflicting_local_count"], 1)
        self.assertEqual(len(strict_payload["sequences"]), 1)
        self.assertEqual(strict_payload["sequences"][0]["name"], "iNat123456 Amanita example California US")

    def test_queued_ncbi_import_uses_local_results_without_fetching_ncbi_export(self):
        with (
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch(
                "app.services.mycomap_service.get_mycomap_ncbi_queue_position",
                return_value=2741,
            ),
            patch(
                "app.services.mycomap_service.fetch_mycomap_fasta",
                return_value=MOCK_FASTA_RESULT,
            ) as fetch_fasta,
            patch("app.services.mycomap_service.fetch_mycomap_blast_metrics", return_value={}),
            patch(
                "app.services.mycomap_service.improve_mycomap_sequence_name",
                side_effect=lambda name, *_, **__: name,
            ),
            patch("app.services.blast_service.fetch_fasta_for_accessions", return_value=""),
        ):
            payload, error = gather_mycomap_sequences_for_queue(
                MOCK_BLAST_URL, include_ncbi=True, include_local=True,
            )

        self.assertIsNone(error)
        fetch_fasta.assert_called_once_with(
            "42", False, True, time_budget=None,
        )
        self.assertEqual(payload["pending_sources"], ["ncbi"])
        self.assertEqual(payload["ncbi_queue_position"], 2741)
        self.assertIn("local results were imported", payload["message"])

    def test_queued_ncbi_only_import_returns_retryable_pending_response(self):
        with (
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch(
                "app.services.mycomap_service.get_mycomap_ncbi_queue_position",
                return_value=2741,
            ),
            patch("app.services.mycomap_service.fetch_mycomap_fasta") as fetch_fasta,
        ):
            payload, error = gather_mycomap_sequences_for_queue(
                MOCK_BLAST_URL, include_ncbi=True, include_local=False,
            )

        self.assertIsNone(payload)
        body, status = error
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "pending")
        self.assertTrue(body["retryable"])
        self.assertEqual(body["ncbi_queue_position"], 2741)
        fetch_fasta.assert_not_called()

    def test_gather_does_not_treat_same_state_labels_as_distinct_locations(self):
        same_state_fasta = (
            ">iNat123456 Amanita example California US\n"
            f"{QUERY_SEQUENCE}\n"
            ">iNat987654 Amanita another California US\n"
            f"{QUERY_SEQUENCE}\n"
        )
        same_state_result = {
            "fasta_content": same_state_fasta,
            "ncbi_count": 0,
            "local_count": 2,
            "errors": [],
        }
        same_state_metrics = {
            "iNat987654": {"identity": 88.0, "mycomap_location": "California US"},
        }

        with (
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch("app.services.mycomap_service.fetch_mycomap_fasta", return_value=same_state_result),
            patch("app.services.mycomap_service.fetch_mycomap_blast_metrics", return_value=same_state_metrics),
            patch(
                "app.services.mycomap_service.improve_mycomap_sequence_name",
                side_effect=lambda name, *_, **__: name,
            ),
            patch("app.services.blast_service.fetch_fasta_for_accessions", return_value=""),
        ):
            payload, error = gather_mycomap_sequences_for_queue(
                MOCK_BLAST_URL,
                include_ncbi=False,
                include_local=True,
                filter_conflicting_local_fasta=True,
                allow_identical_sequences_different_locations=True,
            )

        self.assertIsNone(error)
        self.assertEqual(payload["conflicting_local_count"], 1)
        self.assertEqual(len(payload["sequences"]), 1)


if __name__ == "__main__":
    unittest.main()
