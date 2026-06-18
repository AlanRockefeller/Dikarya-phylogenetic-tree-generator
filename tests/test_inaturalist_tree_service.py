"""
Unit tests for inaturalist_tree_service.py

These tests cover the source-tip label formatter used by the iNaturalist
tree flow. They avoid network access by exercising the pure helper
functions directly.
"""

import importlib.util
import os
import unittest


SERVICE_PATH = os.path.join(os.path.dirname(__file__), "../app/services/inaturalist_tree_service.py")
spec = importlib.util.spec_from_file_location("inaturalist_tree_service", SERVICE_PATH)
inaturalist_tree_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inaturalist_tree_service)


build_inat_source_display_name = inaturalist_tree_service._build_inat_source_display_name
find_observation_source_tip_name = inaturalist_tree_service._find_observation_source_tip_name
maybe_add_inat_its_sequence = inaturalist_tree_service._maybe_add_inat_its_sequence
source_display_label_for_tip = inaturalist_tree_service._source_display_label_for_tip
from app.services.tree_edit_service import rename_tip


class TestInaturalistTreeSourceLabel(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
