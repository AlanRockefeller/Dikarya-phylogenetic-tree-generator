import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

from app.workers.tasks import (
    _check_and_maybe_fix_orientation,
    dedupe_and_uniquify_fasta,
    parse_fasta_records,
    uniquify_fasta_identifiers,
)


class WorkerInputCleanupTests(unittest.TestCase):
    def test_orientation_opt_out_classifies_without_rewriting_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input_raw.fasta"
            input_path.write_text(">sample\nAAAA\n")
            with patch(
                "app.services.orientation_service.fix_sequence_orientation",
                return_value=(">sample\nTTTT\n", {"reverse": 1}),
            ):
                stats = _check_and_maybe_fix_orientation(input_path, False)

            self.assertEqual(input_path.read_text(), ">sample\nAAAA\n")
            self.assertEqual(stats["reverse"], 1)

    def test_orientation_default_rewrites_input_with_corrected_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input_raw.fasta"
            input_path.write_text(">sample\nAAAA\n")
            with patch(
                "app.services.orientation_service.fix_sequence_orientation",
                return_value=(">sample\nTTTT\n", {"reverse": 1}),
            ):
                _check_and_maybe_fix_orientation(input_path, True)

            self.assertEqual(input_path.read_text(), ">sample\nTTTT\n")

    def test_duplicate_rebuild_mode_keeps_identical_header_and_sequence(self):
        fasta = ">same original header\nACGT\n>same original header\nACGT\n"

        rebuilt, stats = uniquify_fasta_identifiers(fasta)
        records = parse_fasta_records(rebuilt)

        self.assertEqual(len(records), 2)
        self.assertEqual([sequence for _header, sequence in records], ["ACGT", "ACGT"])
        self.assertEqual(len({header.split()[0] for header, _sequence in records}), 2)
        self.assertEqual(stats["dropped_exact_duplicates"], 0)

    def test_ordinary_cleanup_still_drops_exact_duplicate_record(self):
        fasta = ">same original header\nACGT\n>same original header\nACGT\n"

        cleaned, stats = dedupe_and_uniquify_fasta(fasta)

        self.assertEqual(len(parse_fasta_records(cleaned)), 1)
        self.assertEqual(stats["dropped_exact_duplicates"], 1)

    def test_rebuild_route_enqueues_both_identical_original_records(self):
        from app.api import routes
        from app.config import Config

        source_params = {
            "input_type": "pasted_sequence",
            "sequence": ">same original header\nACGT\n",
            "sequence_metadata": [{
                "name": "same original header",
                "fasta_header": "same original header",
                "display_label": "Original collection A",
            }],
            "import_filter_details": {
                "duplicates": {
                    "removed_records": [{
                        "name": "same original header",
                        "sequence": "ACGT",
                        "metadata": {
                            "name": "same original header",
                            "fasta_header": "same original header",
                            "display_label": "Original collection B",
                        },
                    }],
                },
            },
            "tree_method": "fasttree",
            "alignment_method": "mafft",
            "trimming_method": "trimal_gappy",
        }
        captured = {}

        def fake_enqueue(params, *args, **kwargs):
            captured.update(params)
            return kwargs.get("job_id") or "new-job"

        fake_session = SimpleNamespace(add=Mock(), commit=Mock())
        def fake_job_model(**kwargs):
            return SimpleNamespace(**kwargs)

        source_db_job = SimpleNamespace(user_id=7)
        app = Flask(__name__)
        app.secret_key = "test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source-job"
            source_dir.mkdir()
            (source_dir / "input_info.json").write_text(json.dumps(source_params))
            endpoint = routes.rebuild_with_duplicates.__wrapped__
            with (
                app.test_request_context(method="POST", json={}),
                patch.object(Config, "JOB_DIR", Path(tmp)),
                patch.object(
                    routes, "check_job_access",
                    return_value=(source_db_job, None, 200),
                ),
                patch.object(routes, "enqueue_job", side_effect=fake_enqueue),
                patch.object(routes, "Job", fake_job_model),
                patch.object(routes, "db", SimpleNamespace(session=fake_session)),
            ):
                response, status = endpoint("source-job")

        records = parse_fasta_records(captured["sequence"])
        self.assertEqual(status, 202)
        self.assertEqual(response.get_json()["restored_count"], 1)
        self.assertEqual(len(records), 2)
        self.assertEqual([sequence for _header, sequence in records], ["ACGT", "ACGT"])
        self.assertEqual(len({header.split()[0] for header, _sequence in records}), 2)
        self.assertTrue(captured["skip_observation_dedup"])
        self.assertTrue(captured["preserve_exact_duplicate_records"])
        self.assertEqual(
            [item["display_label"] for item in captured["sequence_metadata"]],
            ["Original collection A", "Original collection B"],
        )


if __name__ == "__main__":
    unittest.main()
