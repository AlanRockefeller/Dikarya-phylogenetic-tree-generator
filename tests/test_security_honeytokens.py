"""The decoy paths, and the reservations that keep them from catching a user."""

import pathlib
import unittest

from app.api_v1.openapi import build_spec
from app.services.job_id_service import RESERVED_JOB_IDS
from app.services.security_honeytokens import (
    DECOY_JOB_ID, HONEYTOKEN_EXACT_PATHS, honeytoken_hit,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class HoneytokenReservationTests(unittest.TestCase):
    def test_the_decoy_job_id_can_never_be_minted(self):
        """The one way a honeytoken could accuse a real user.

        If minting ever handed this id to a real job, every visitor following
        that job's link would be reported as having read the page source and
        gone looking for ids to try. The reservation is the whole safety
        property, so it is asserted rather than trusted.
        """
        self.assertIn(DECOY_JOB_ID, RESERVED_JOB_IDS)

    def test_each_token_is_published_exactly_where_it_is_supposed_to_be(self):
        """A tripwire only works while it is reachable by reading our output.

        Each of these is planted in one place. If a plant is removed the token
        becomes undiscoverable and silently stops catching anyone, which is a
        failure that no error would otherwise report.
        """
        robots = (REPO_ROOT / "app" / "static" / "robots.txt").read_text()
        self.assertIn("Disallow: /admin/db-export", robots)

        viewer = (REPO_ROOT / "app" / "templates" / "job_viewer.html").read_text()
        self.assertIn(DECOY_JOB_ID, viewer)

        self.assertIn("/internal/diagnostics-export", build_spec()["paths"])

    def test_a_planted_token_is_matched_where_it_is_published(self):
        # robots.txt and the OpenAPI document publish absolute paths; the
        # matcher has to agree with them exactly, including the /api/v1 prefix
        # the v1 blueprint is mounted under.
        self.assertEqual(honeytoken_hit("/admin/db-export"), "/admin/db-export")
        self.assertEqual(
            honeytoken_hit("/api/v1/internal/diagnostics-export"),
            "/api/v1/internal/diagnostics-export",
        )
        for path in (f"/job/{DECOY_JOB_ID}", f"/job/{DECOY_JOB_ID}/view",
                     f"/api/job/{DECOY_JOB_ID}/download/tree"):
            self.assertIsNotNone(honeytoken_hit(path), path)

    def test_ordinary_traffic_trips_nothing(self):
        for path in ("/", "/tree", "/job/aq7c/view", "/admin/monitoring",
                     "/api/v1/jobs", "/admin", "/whats-new"):
            self.assertIsNone(honeytoken_hit(path), path)

    def test_no_token_is_a_path_the_app_actually_serves(self):
        """A decoy that collides with a real route reports real users.

        Builds the URL map only. ALLOW_SQLITE_FALLBACK keeps create_app() from
        refusing the unconfigured database -- nothing here issues a query, and
        the flag exists for exactly this kind of offline inspection.
        """
        import os
        from unittest import mock

        from app import create_app

        with mock.patch.dict(os.environ, {"ALLOW_SQLITE_FALLBACK": "1"}):
            app = create_app("development")
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        rules |= {rule.rstrip("/") for rule in rules}
        for token in HONEYTOKEN_EXACT_PATHS:
            self.assertNotIn(token, rules, token)


if __name__ == "__main__":
    unittest.main()
