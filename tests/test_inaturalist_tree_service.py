"""
Unit tests for inaturalist_tree_service.py

These tests cover the source-tip label formatter used by the iNaturalist
tree flow. They avoid network access by exercising the pure helper
functions directly.
"""

import importlib.util
import os
import unittest
from unittest.mock import patch


SERVICE_PATH = os.path.join(os.path.dirname(__file__), "../app/services/inaturalist_tree_service.py")
spec = importlib.util.spec_from_file_location("inaturalist_tree_service", SERVICE_PATH)
inaturalist_tree_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inaturalist_tree_service)


build_inat_source_display_name = inaturalist_tree_service._build_inat_source_display_name
build_inat_job_title = inaturalist_tree_service._build_inat_job_title
extract_inat_genus = inaturalist_tree_service._extract_inat_genus
extract_genus_from_inat_tip = inaturalist_tree_service._extract_genus_from_inat_tip
find_observation_source_tip_name = inaturalist_tree_service._find_observation_source_tip_name
maybe_add_inat_its_sequence = inaturalist_tree_service._maybe_add_inat_its_sequence
source_display_label_for_tip = inaturalist_tree_service._source_display_label_for_tip
from app.services.tree_edit_service import rename_tip


class TestInaturalistTreeSourceLabel(unittest.TestCase):
    def test_job_title_uses_tagged_species_genus(self):
        observation = {
            "ofvs": [
                {
                    "name": "Tagged NZ Fungal Species",
                    "value": "Viridopsathyra sp. 'Monro Beach (PDD 107303)'",
                },
            ],
            "taxon": {"name": "Psathyrellaceae", "rank": "family"},
        }

        genus = extract_inat_genus(observation)

        self.assertEqual(genus, "Viridopsathyra")
        self.assertEqual(
            build_inat_job_title(110793649, genus),
            "iNat # 110793649 - Viridopsathyra → Phylogenetic Tree",
        )

    def test_genus_uses_named_taxonomy_ancestor_for_section(self):
        observation = {
            "taxon": {
                "name": "Pseudofirmae",
                "rank": "section",
                "ancestors": [
                    {"name": "Hygrophoraceae", "rank": "family"},
                    {"name": "Hygrocybe", "rank": "genus"},
                ],
            },
        }

        self.assertEqual(extract_inat_genus(observation), "Hygrocybe")

    def test_genus_can_fall_back_to_source_tip(self):
        genus = extract_genus_from_inat_tip(
            "OQ225633 iNat360921334 Laccaria proxima voucher DAVFP:29730",
            360921334,
        )

        self.assertEqual(genus, "Laccaria")

    def test_source_label_uses_species_and_location(self):
        observation = {
            "taxon": {"name": "Daldinia loculata"},
            "place_guess": "Albuquerque, New Mexico, US",
        }

        label = build_inat_source_display_name(observation, 238849364)

        self.assertEqual(label, "iNat238849364 Daldinia loculata New Mexico US")

    def test_source_label_preserves_full_region_name(self):
        observation = {
            "taxon": {"name": "Daldinia loculata"},
            "place_guess": "New Mexico, NM, United States",
        }

        label = build_inat_source_display_name(observation, 238849364)

        self.assertEqual(label, "iNat238849364 Daldinia loculata New Mexico US")

    def test_source_label_prefers_species_override(self):
        observation = {
            "ofvs": [
                {"name": "Species Name Override", "value": "Amanita muscaria"},
            ],
            "taxon": {"name": "Amanita cf. muscaria"},
            "private_place_guess": "Ketchikan, Alaska, United States",
        }

        label = build_inat_source_display_name(observation, 123456789)

        self.assertEqual(label, "iNat123456789 Amanita muscaria Alaska US")

    def test_source_tip_prefers_raw_mycomap_label_over_synthetic_inat_label(self):
        sequences = [
            {"name": "iNat238849364 (observation DNA Barcode ITS)"},
            {"name": "HFSONT56_ITS4-7_04_E1-DIK-A55-238849364-Daldinia_-1 ric=405 - 531373"},
        ]

        name = find_observation_source_tip_name(sequences, 238849364)

        self.assertEqual(
            name,
            "HFSONT56_ITS4-7_04_E1-DIK-A55-238849364-Daldinia_-1 ric=405 - 531373",
        )

    def test_source_tip_dedupes_exact_accession_and_inat_records(self):
        its = "ACGT" * 30
        observation = {
            "ofvs": [
                {"name": "DNA Barcode ITS", "value": its},
            ],
        }
        sequences = [
            {
                "name": "PX375095 iNat317417169 Sarcosphaera sp. 'KM7' Colorado US",
                "sequence": its,
            },
            {
                "name": "iNat317417169 Sarcosphaera sp. 'KM7' Colorado US",
                "sequence": its,
            },
            {
                "name": "Other exact hit",
                "sequence": its,
            },
        ]

        added, matched = maybe_add_inat_its_sequence(observation, 317417169, sequences)

        self.assertIsNone(added)
        self.assertEqual(
            matched,
            "PX375095 iNat317417169 Sarcosphaera sp. 'KM7' Colorado US",
        )
        self.assertEqual(len(sequences), 2)
        self.assertEqual(
            sequences[0]["name"],
            "PX375095 iNat317417169 Sarcosphaera sp. 'KM7' Colorado US",
        )
        self.assertEqual(sequences[1]["name"], "Other exact hit")

    def test_source_tip_dedupes_terminally_trimmed_inat_barcode(self):
        its = "ACGT" * 30
        source_name = (
            "iNat148840430 Merismodes sp. 'CA02' "
            "Lakeport California US RiC 140"
        )
        observation = {
            "ofvs": [
                {"name": "DNA Barcode ITS", "value": its},
            ],
        }
        sequences = [
            {
                "name": source_name,
                "sequence": f"GTCG{its}AGGTGGGACTACCCGCTGAACTT",
            },
        ]

        added, matched = maybe_add_inat_its_sequence(
            observation, 148840430, sequences
        )

        self.assertIsNone(added)
        self.assertEqual(matched, source_name)
        self.assertEqual(len(sequences), 1)

    def test_source_display_label_preserves_accession(self):
        label = source_display_label_for_tip(
            "PX375095 iNat317417169 Sarcosphaera sp. 'KM7' Colorado US",
            "iNat317417169 Sarcosphaera sp. 'KM7' Grand Mesa US",
            317417169,
        )

        self.assertEqual(
            label,
            "PX375095 iNat317417169 Sarcosphaera sp. 'KM7' Grand Mesa US",
        )

    def test_rename_tip_updates_visible_name_and_display_name(self):
        tree_json = {
            "tree_structure": {
                "name": "old tip",
                "original_name": "old tip",
            }
        }

        rename_tip(tree_json, "old tip", "new tip")

        node = tree_json["tree_structure"]
        self.assertEqual(node["name"], "new tip")
        self.assertEqual(node["display_name"], "new tip")
        self.assertEqual(tree_json["renames"]["old tip"], "new tip")

    def test_ncbi_rebuild_queues_before_observation_fetch(self):
        """The NCBI grace period must never run in the HTTP request process."""
        class FakeSession:
            def __init__(self):
                self.added = []
                self.commit_count = 0

            def add(self, record):
                self.added.append(record)

            def commit(self):
                self.commit_count += 1

        class FakeJob:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        fake_session = FakeSession()
        fake_db = type("FakeDb", (), {"session": fake_session})()

        def enqueue_preparation(_job_params, **kwargs):
            return kwargs["job_id"]

        with (
            patch.object(inaturalist_tree_service, "parse_single_observation_input", return_value=123456789),
            patch.object(
                inaturalist_tree_service,
                "fetch_observation",
                side_effect=AssertionError("request path must not fetch the observation"),
            ),
            patch("app.extensions.db", fake_db),
            patch("app.models.Job", FakeJob),
            patch("app.workers.queue.enqueue_job", side_effect=enqueue_preparation) as enqueue_job,
        ):
            result = inaturalist_tree_service.create_job_from_inat_observation(
                "123456789",
                rebuild_ncbi_blast=True,
            )

        self.assertEqual(result["status"], "queued")
        self.assertTrue(result["mycomap_ncbi_blast_rebuild_requested"])
        self.assertEqual(len(fake_session.added), 1)
        self.assertEqual(fake_session.added[0].id, result["job_id"])
        self.assertEqual(fake_session.added[0].status, "queued")
        self.assertEqual(enqueue_job.call_args.kwargs["job_id"], result["job_id"])
        self.assertEqual(enqueue_job.call_args.kwargs["job_timeout"], 7200)

    def test_local_refresh_queues_before_observation_fetch(self):
        """The automatic local rerun must not run in the HTTP request process."""
        class FakeSession:
            def __init__(self):
                self.added = []

            def add(self, record):
                self.added.append(record)

            def commit(self):
                pass

        class FakeJob:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        fake_session = FakeSession()
        fake_db = type("FakeDb", (), {"session": fake_session})()

        with (
            patch.object(inaturalist_tree_service, "parse_single_observation_input", return_value=123456789),
            patch.object(
                inaturalist_tree_service,
                "fetch_observation",
                side_effect=AssertionError("request path must not fetch the observation"),
            ),
            patch("app.extensions.db", fake_db),
            patch("app.models.Job", FakeJob),
            patch(
                "app.workers.queue.enqueue_job",
                side_effect=lambda _params, **kwargs: kwargs["job_id"],
            ) as enqueue_job,
        ):
            result = inaturalist_tree_service.create_job_from_inat_observation("123456789")

        queued_params = enqueue_job.call_args.args[0]
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["mycomap_local_blast_rebuilt"])
        self.assertFalse(result["mycomap_ncbi_blast_rebuild_requested"])
        self.assertFalse(queued_params["_inat_tree_preparation"]["rebuild_ncbi_blast"])
        self.assertEqual(len(fake_session.added), 1)

    def test_local_rerun_failure_falls_back_to_saved_results(self):
        from app.services.mycomap_service import MycoMapRerunError

        with patch(
            "app.services.mycomap_service.rerun_mycomap_blast",
            side_effect=MycoMapRerunError("temporary outage"),
        ):
            details = inaturalist_tree_service._refresh_mycomap_blast_results(
                "42",
                rebuild_ncbi_blast=False,
                mycomap_local_limit=50,
                mycomap_ncbi_limit=100,
            )

        self.assertEqual(details["local_status"], "failed")
        self.assertEqual(details["local_error"], "temporary outage")
        self.assertIn("saved MycoMap results", details["warnings"][0])

    def test_ncbi_rerun_returns_before_saved_results_are_fetched(self):
        mycomap_url = "https://mycomap.com/genetics/blast-search/c01-inat123456789-r42"
        rerun_details = {
            "local_status": "completed",
            "local_limit": 50,
            "ncbi_limit": 100,
            "ncbi": {"status_code": 202},
            "warnings": [],
        }
        with (
            patch.object(inaturalist_tree_service, "fetch_observation", return_value={"id": 123456789}),
            patch.object(
                inaturalist_tree_service,
                "extract_observation_field_value",
                return_value=mycomap_url,
            ),
            patch.object(
                inaturalist_tree_service,
                "_refresh_mycomap_blast_results",
                return_value=rerun_details,
            ),
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch(
                "app.api.routes.gather_mycomap_sequences_for_queue",
                side_effect=AssertionError("NCBI results must be fetched after the delayed retry"),
            ),
        ):
            prepared = inaturalist_tree_service.prepare_inat_tree_job(
                123456789,
                rebuild_ncbi_blast=True,
                defer_after_ncbi_rerun=True,
            )

        self.assertEqual(prepared["status"], "waiting_for_ncbi")
        self.assertEqual(prepared["mycomap_rerun_details"], rerun_details)

    def test_preview_accepts_its_when_mycomap_url_is_missing(self):
        observation = {
            "id": 123456789,
            "ofvs": [{"name": "DNA Barcode ITS", "value": "ACGT" * 40}],
        }
        with (
            patch.object(
                inaturalist_tree_service,
                "parse_inaturalist_tree_input",
                return_value={"type": "single_observation", "observation_id": 123456789},
            ),
            patch.object(
                inaturalist_tree_service, "fetch_observation", return_value=observation
            ),
        ):
            preview = inaturalist_tree_service.preview_inaturalist_tree_input("123456789")

        self.assertEqual(preview["eligible_tree_count"], 1)
        self.assertTrue(preview["has_dna_barcode_its"])
        self.assertTrue(preview["will_create_mycomap_blast"])
        self.assertIn("start a Mycomap BLAST", preview["message"])

    def test_preview_offers_recreation_when_tree_and_source_exist(self):
        observation = {
            "id": 123456789,
            "ofvs": [
                {
                    "name": "Mycomap BLAST Results",
                    "value": "https://mycomap.com/genetics/blast-search/r42/",
                },
                {
                    "name": "Phylogenetic Tree",
                    "value": "https://dikarya.us/job/old-tree/view",
                },
            ],
        }
        with (
            patch.object(
                inaturalist_tree_service,
                "parse_inaturalist_tree_input",
                return_value={"type": "single_observation", "observation_id": 123456789},
            ),
            patch.object(
                inaturalist_tree_service, "fetch_observation", return_value=observation
            ),
        ):
            preview = inaturalist_tree_service.preview_inaturalist_tree_input("123456789")

        self.assertEqual(preview["eligible_tree_count"], 0)
        self.assertTrue(preview["has_phylogenetic_tree"])
        self.assertEqual(
            preview["phylogenetic_tree_url"],
            "https://dikarya.us/job/old-tree/view",
        )
        self.assertTrue(preview["can_recreate_phylogenetic_tree"])
        self.assertIn("replace the field's current URL", preview["message"])

    def test_tree_preparation_requires_recreation_consent_for_existing_tree(self):
        observation = {
            "id": 123456789,
            "ofvs": [
                {
                    "name": "Phylogenetic Tree",
                    "value": "https://dikarya.us/job/old-tree/view",
                },
            ],
        }
        with patch.object(
            inaturalist_tree_service, "fetch_observation", return_value=observation
        ):
            with self.assertRaises(inaturalist_tree_service.InatTreeError) as raised:
                inaturalist_tree_service.prepare_inat_tree_job(123456789)

        self.assertEqual(raised.exception.status, 409)
        self.assertIn("Re-create phylogenetic tree", str(raised.exception))

    def test_missing_mycomap_url_creates_blast_and_updates_inaturalist(self):
        observation = {
            "id": 123456789,
            "ofvs": [{"name": "DNA Barcode ITS", "value": "ACGT" * 40}],
        }
        created = {
            "blast_id": "42",
            "url": "https://mycomap.com/genetics/blast-search/r42/",
            "local_limit": 50,
            "ncbi_limit": 100,
        }
        with (
            patch.object(
                inaturalist_tree_service, "fetch_observation", return_value=observation
            ),
            patch(
                "app.services.mycomap_service.create_mycomap_blast",
                return_value=created,
            ) as create_blast,
            patch(
                "app.services.mycomap_service.find_mycomap_blast_by_title",
                return_value=None,
            ),
            patch.object(
                inaturalist_tree_service,
                "set_observation_field_value",
                return_value={"id": 99},
            ) as set_field,
            patch(
                "app.api.routes.gather_mycomap_sequences_for_queue",
                side_effect=AssertionError("tree input must wait for NCBI results"),
            ),
        ):
            prepared = inaturalist_tree_service.prepare_inat_tree_job(123456789)

        self.assertEqual(prepared["status"], "waiting_for_ncbi")
        self.assertTrue(prepared["mycomap_rerun_details"]["auto_created"])
        create_blast.assert_called_once()
        set_field.assert_called_once_with(
            123456789,
            inaturalist_tree_service.MYCOMAP_BLAST_FIELD_NAME,
            created["url"],
        )

    def test_missing_mycomap_url_reuses_existing_exact_title(self):
        observation = {
            "id": 378687760,
            "ofvs": [{"name": "DNA Barcode ITS", "value": "ACGT" * 40}],
        }
        existing = {
            "blast_id": "590133",
            "url": (
                "https://mycomap.com/genetics/blast-search/"
                "inat378687760-dna-barcode-its-r590133/"
            ),
        }
        with (
            patch.object(
                inaturalist_tree_service, "fetch_observation", return_value=observation
            ),
            patch(
                "app.services.mycomap_service.find_mycomap_blast_by_title",
                return_value=existing,
            ),
            patch(
                "app.services.mycomap_service.create_mycomap_blast",
                side_effect=AssertionError("an existing search must not be duplicated"),
            ),
            patch.object(
                inaturalist_tree_service,
                "set_observation_field_value",
                return_value={"id": 100},
            ) as set_field,
        ):
            prepared = inaturalist_tree_service.prepare_inat_tree_job(378687760)

        self.assertEqual(prepared["status"], "waiting_for_ncbi")
        self.assertTrue(
            prepared["mycomap_rerun_details"]["local"]["reused_existing"]
        )
        set_field.assert_called_once_with(
            378687760,
            inaturalist_tree_service.MYCOMAP_BLAST_FIELD_NAME,
            existing["url"],
        )

    def test_accepted_blast_waits_for_result_page_without_resubmitting(self):
        observation = {
            "id": 365269897,
            "ofvs": [{"name": "DNA Barcode ITS", "value": "ACGT" * 40}],
        }
        pending = {
            "auto_created": True,
            "creation_pending": True,
            "creation_discovery_attempt": 1,
            "created_title": "iNat365269897 DNA Barcode ITS",
            "created_mycomap_url": "",
            "ncbi_poll_attempt": 0,
        }
        with (
            patch(
                "app.services.mycomap_service.find_mycomap_blast_by_title",
                return_value=None,
            ),
            patch(
                "app.services.mycomap_service.create_mycomap_blast",
                side_effect=AssertionError("an accepted search must not be resubmitted"),
            ),
        ):
            details = (
                inaturalist_tree_service._create_mycomap_blast_from_observation(
                    observation,
                    365269897,
                    pending_creation_details=pending,
                )
            )

        self.assertTrue(details["creation_pending"])
        self.assertEqual(details["creation_discovery_attempt"], 2)

    def test_auto_created_blast_waits_until_ncbi_results_exist(self):
        mycomap_url = "https://mycomap.com/genetics/blast-search/r42/"
        details = {
            "auto_created": True,
            "created_mycomap_url": mycomap_url,
            "ncbi_poll_attempt": 0,
            "warnings": [],
        }
        with (
            patch.object(
                inaturalist_tree_service,
                "fetch_observation",
                return_value={
                    "id": 123456789,
                    "taxon": {"name": "Amanita muscaria", "rank": "species"},
                },
            ),
            patch.object(
                inaturalist_tree_service,
                "extract_observation_field_value",
                return_value=mycomap_url,
            ),
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch(
                "app.services.mycomap_service.get_mycomap_ncbi_result_count",
                return_value=(0, []),
            ),
            patch(
                "app.api.routes.gather_mycomap_sequences_for_queue",
                side_effect=AssertionError("tree input must wait for an NCBI hit"),
            ),
        ):
            prepared = inaturalist_tree_service.prepare_inat_tree_job(
                123456789,
                skip_mycomap_refresh=True,
                mycomap_rerun_details=details,
            )

        self.assertEqual(prepared["status"], "waiting_for_ncbi")
        self.assertEqual(prepared["inat_genus"], "Amanita")
        self.assertEqual(
            prepared["notes"],
            "iNat # 123456789 - Amanita → Phylogenetic Tree",
        )
        self.assertEqual(prepared["mycomap_rerun_details"]["ncbi_poll_attempt"], 1)
        self.assertEqual(prepared["mycomap_rerun_details"]["ncbi_status"], "waiting")

    def test_auto_created_blast_builds_tree_after_ncbi_results_exist(self):
        mycomap_url = "https://mycomap.com/genetics/blast-search/r42/"
        details = {
            "auto_created": True,
            "created_mycomap_url": mycomap_url,
            "local_limit": 50,
            "ncbi_limit": 100,
            "ncbi_poll_attempt": 1,
            "warnings": [],
        }
        payload = {
            "sequences": [
                {"name": "AB123456 Example one", "sequence": "ACGT" * 40},
                {"name": "CD123456 Example two", "sequence": "TGCA" * 40},
            ]
        }
        with (
            patch.object(
                inaturalist_tree_service,
                "fetch_observation",
                return_value={"id": 123456789},
            ),
            patch.object(
                inaturalist_tree_service,
                "extract_observation_field_value",
                return_value=mycomap_url,
            ),
            patch("app.services.mycomap_service.validate_mycomap_url", return_value="42"),
            patch(
                "app.services.mycomap_service.get_mycomap_ncbi_result_count",
                return_value=(2, []),
            ),
            patch(
                "app.api.routes.gather_mycomap_sequences_for_queue",
                return_value=(payload, None),
            ),
        ):
            prepared = inaturalist_tree_service.prepare_inat_tree_job(
                123456789,
                skip_mycomap_refresh=True,
                mycomap_rerun_details=details,
            )

        self.assertEqual(prepared["job_params"]["input_type"], "pasted_sequence")
        self.assertTrue(prepared["metrics"]["mycomap_blast_auto_created"])
        self.assertTrue(prepared["metrics"]["mycomap_ncbi_blast_rebuilt"])
        self.assertIn(">AB123456 Example one", prepared["job_params"]["sequence"])

    def test_batch_admission_returns_successful_ids_after_one_failure(self):
        collected = {
            "observations": [{"id": 101}, {"id": 102}, {"id": 103}],
            "skipped_existing_tree_count": 0,
            "skipped_missing_mycomap_count": 0,
        }

        def create_one(raw_input, **_kwargs):
            if raw_input == "102":
                raise inaturalist_tree_service.InatTreeError("Unable to queue observation 102.")
            return {"job_id": f"job-{raw_input}"}

        with (
            patch.object(inaturalist_tree_service, "parse_inaturalist_tree_input", return_value={}),
            patch.object(
                inaturalist_tree_service,
                "resolve_inaturalist_user_or_project",
                return_value={
                    "type": "project",
                    "scope": {"type": "project", "value": "example"},
                },
            ),
            patch.object(
                inaturalist_tree_service,
                "_collect_tree_eligible_observations",
                return_value=collected,
            ),
            patch.object(
                inaturalist_tree_service,
                "create_job_from_inat_observation",
                side_effect=create_one,
            ),
        ):
            result = inaturalist_tree_service.create_jobs_from_inat_scope(
                "example",
                resolved_type="project",
            )

        self.assertEqual(result["job_ids"], ["job-101", "job-103"])
        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertTrue(result["partial"])
        self.assertEqual(result["failed_observations"][0]["observation_id"], 102)
        self.assertIn("2 bulk tree jobs", result["message"])


if __name__ == "__main__":
    unittest.main()
