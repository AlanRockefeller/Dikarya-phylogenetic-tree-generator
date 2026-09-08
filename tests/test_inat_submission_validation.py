from unittest.mock import patch

import pytest

from app.services import inaturalist_tree_service as service


def test_missing_input_rejected_before_enqueue():
    with patch.object(service, "fetch_observation", return_value={"ofvs": []}), patch("app.workers.queue.enqueue_job") as enqueue:
        with pytest.raises(service.InatTreeError, match="Add a DNA Barcode ITS"):
            service.create_job_from_inat_observation("74600029")
        enqueue.assert_not_called()


def test_tree_appearing_in_queue_does_not_abort_preparation():
    observation = {"ofvs": [
        {"name": "Phylogenetic Tree", "value": "https://dikarya.us/job/previous/view"},
        {"name": "DNA Barcode ITS", "value": "ACGT" * 100},
    ]}
    with patch.object(service, "fetch_observation", return_value=observation), patch.object(service, "_resolve_inat_genus", return_value="Verpa"), patch.object(service, "_create_mycomap_blast_from_observation", return_value={"created_mycomap_url": "https://mycomap.com/r123"}):
        assert service.prepare_inat_tree_job(74600029)["status"] == "waiting_for_ncbi"


def test_completion_preserves_tree_that_appeared_after_submission():
    with patch.object(service, "fetch_observation", return_value={"ofvs": [{"name": "Phylogenetic Tree", "value": "existing"}]}), patch.object(service, "set_observation_field_value") as write:
        result = service.post_completed_tree_to_inaturalist("new", {"inat_observation_id": 74600029})
        assert result["status"] == "skipped"
        write.assert_not_called()


def test_explicit_replacement_still_writes():
    from flask import Flask
    app = Flask(__name__)
    with app.app_context(), patch.object(service, "set_observation_field_value", return_value={"id": 1}) as write:
        result = service.post_completed_tree_to_inaturalist("new", {
            "inat_observation_id": 74600029, "inat_replace_existing_tree": True,
            "inat_public_base_url": "https://dikarya.us",
        })
        assert result["status"] == "success"
        write.assert_called_once()
