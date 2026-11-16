import unittest
import urllib.error
from unittest.mock import patch

from app.services.mushroom_observer_service import (
    MushroomObserverError,
    _api_request,
    _mycomap_blast_url_from_notes,
    prepare_tree_job,
)


class MushroomObserverMycoMapMessageTests(unittest.TestCase):
    def test_org_result_in_notes_is_preserved(self):
        url = "https://mycomap.org/blast-results/530840/136363"
        self.assertEqual(_mycomap_blast_url_from_notes(f"Search: {url}"), url)

    def test_upstream_http_error_logs_table_method_and_status(self):
        upstream_error = urllib.error.HTTPError(
            "https://mushroomobserver.org/api2/observations", 503,
            "Service Unavailable", None, None,
        )
        with (
            patch("urllib.request.urlopen", side_effect=upstream_error),
            self.assertLogs("app.services.mushroom_observer_service", level="WARNING") as logs,
            self.assertRaises(MushroomObserverError) as caught,
        ):
            _api_request("observations")

        self.assertEqual(caught.exception.status, 502)
        self.assertIn(
            "table=observations method=GET status=503", "\n".join(logs.output)
        )

    def test_creation_discovery_timeout_does_not_invent_hit_counts(self):
        preparation = {
            "observation_id": 123,
            "sequence_id": 456,
            "sequence": "ACGT" * 30,
            "consensus_name": "Example fungus",
        }

        def missing_lookup(_title, warnings=None, pending_out=None, fresh=False):
            warnings.append("lookup endpoint returned 503")
            return None

        with (
            patch(
                "app.services.mycomap_service.validate_mycomap_rerun_limit",
                return_value=(100, None),
            ),
            patch(
                "app.services.mycomap_service.validate_mycomap_url",
                return_value=None,
            ),
            patch(
                "app.services.mycomap_service.find_mycomap_blast_by_title",
                side_effect=missing_lookup,
            ),
            patch(
                "app.services.mycomap_service.get_mycomap_creation_discovery_max_attempts",
                return_value=2,
            ),
            patch(
                "app.services.mycomap_service.get_mycomap_creation_discovery_max_seconds",
                return_value=120,
            ),
        ):
            with self.assertRaises(MushroomObserverError) as caught:
                prepare_tree_job(
                    preparation,
                    skip_mycomap_refresh=True,
                    mycomap_rerun_details={
                        "creation_pending": True,
                        "creation_discovery_attempt": 1,
                    },
                )

        message = str(caught.exception)
        self.assertIn("results page had still not appeared", message)
        self.assertIn("lookup endpoint returned 503", message)
        self.assertNotIn("produced no NCBI hits", message)


if __name__ == "__main__":
    unittest.main()


class PostCompletedTreeCommentIdTests(unittest.TestCase):
    """MO's POST answer carries the new comment's id in three shapes."""

    def _post(self, posted):
        from flask import Flask
        from app.services import mushroom_observer_service as mo

        app = Flask(__name__)
        app.config["MUSHROOM_OBSERVER_API_KEY"] = "test-key"

        def fake_api(endpoint, method="GET", **_kwargs):
            return posted if method == "POST" else {"results": []}

        with app.app_context(), \
                patch.object(mo, "_public_tree_url", return_value="https://dikarya.us/job/aq7c/view"), \
                patch.object(mo, "_api_request", side_effect=fake_api):
            return mo.post_completed_tree_comment("aq7c", {"mo_observation_id": 123})

    def test_bare_id_in_results(self):
        # The default detail level: results is a list of ids, not objects.
        output = self._post({"results": [212293]})
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["mo_comment_id"], 212293)

    def test_object_in_results(self):
        output = self._post({"results": [{"id": 212294}]})
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["mo_comment_id"], 212294)

    def test_top_level_id_fallback(self):
        output = self._post({"id": 212295})
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["mo_comment_id"], 212295)
