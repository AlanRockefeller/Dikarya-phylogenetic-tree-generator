"""Regressions for the CodeRabbit "probably fix" findings -- service layer.

Split from tests/test_coderabbit_probably_fix.py only for length; the same
convention applies, with each class naming the finding it covers.
"""
import logging
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask


# ---------------------------------------------------------------------------
# #24 -- canonical emails, and a registration race the database settles
# ---------------------------------------------------------------------------

class EmailCanonicalizationTests(unittest.TestCase):
    def setUp(self):
        from app.auth import routes
        self.routes = routes

    def test_registration_normalizes_the_stored_address(self):
        self.assertEqual(self.routes.normalize_email("  Alan@Example.COM "),
                         "alan@example.com")
        self.assertEqual(self.routes.normalize_email(None), "")

    def test_an_exact_match_wins_over_a_case_variant(self):
        # Production really does contain `Producelala1@yahoo.com` and
        # `producelala1@yahoo.com` as separate accounts, each with jobs. Typing
        # either spelling must reach that spelling's own account.
        upper = SimpleNamespace(email="Producelala1@yahoo.com")
        with patch.object(self.routes, "User") as user_model:
            user_model.query.filter_by.return_value.first.return_value = upper
            found = self.routes.find_user_by_email("Producelala1@yahoo.com")
        self.assertIs(found, upper)

    def test_a_case_insensitive_fallback_applies_only_when_unambiguous(self):
        one = SimpleNamespace(email="alan@example.com")
        two = SimpleNamespace(email="Alan@example.com")
        with patch.object(self.routes, "User") as user_model:
            user_model.query.filter_by.return_value.first.return_value = None
            chain = user_model.query.filter.return_value.limit.return_value
            chain.all.return_value = [one]
            self.assertIs(self.routes.find_user_by_email("ALAN@example.com"), one)

            # Two rows differ only in case: refuse to guess which one is meant.
            chain.all.return_value = [one, two]
            self.assertIsNone(self.routes.find_user_by_email("ALAN@example.com"))

    def test_a_concurrent_registration_is_caught_not_500ed(self):
        from sqlalchemy.exc import IntegrityError
        import inspect
        source = inspect.getsource(self.routes.register)
        self.assertIn("IntegrityError", source)
        self.assertIn("db.session.rollback()", source)
        self.assertIs(IntegrityError, self.routes.IntegrityError)


# ---------------------------------------------------------------------------
# #27 -- a failing metrics tick does not end the collector
# ---------------------------------------------------------------------------

class MetricsCollectorResilienceTests(unittest.TestCase):
    def _run_loop(self, collect_side_effect, ticks=2):
        """Run run-metrics for `ticks` iterations, then stop it via time.sleep."""
        from app import cli
        import app.extensions as extensions

        flask_app = Flask(__name__)
        flask_app.config["METRICS_FILE"] = "/dev/null"
        db = MagicMock()
        sleeps = []

        def _sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) >= ticks:
                raise SystemExit(0)

        with flask_app.app_context(), \
                patch("app.monitoring.services.collect_system_metrics",
                      side_effect=collect_side_effect), \
                patch("app.monitoring.services.emit_health_transitions"), \
                patch("app.services.log_rotation.rotate_runtime_logs"), \
                patch.object(extensions, "db", db), \
                patch("os.makedirs"), \
                patch("time.sleep", side_effect=_sleep):
            with self.assertRaises(SystemExit):
                # .__wrapped__ steps past @with_appcontext, which wants a
                # live click context we are not inside of.
                cli.run_metrics_command.callback.__wrapped__()
        return db, len(sleeps)

    def test_a_failing_tick_is_logged_and_the_next_one_still_runs(self):
        calls = []

        def _collect():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("psutil blew up")
            return {"disk_usage": 1}

        with self.assertLogs("app.cli", level="ERROR") as logs:
            _db, ticks = self._run_loop(_collect, ticks=2)

        self.assertIn("metrics.tick_failed", "\n".join(logs.output))
        self.assertEqual(ticks, 2)      # the loop kept going
        self.assertEqual(len(calls), 2)

    def test_the_session_is_released_after_a_failed_tick_too(self):
        # A tick that raised part-way through the health checks is exactly the
        # case that leaves an open transaction behind.
        def _boom():
            raise RuntimeError("database went away")

        with self.assertLogs("app.cli", level="ERROR"):
            db, ticks = self._run_loop(_boom, ticks=2)

        self.assertEqual(db.session.remove.call_count, ticks)

    def test_shutdown_signals_are_not_swallowed(self):
        # SystemExit is a BaseException, so the loop's `except Exception` must
        # let it through and stop the process -- which is how _run_loop itself
        # terminates every test above. Assert it directly.
        def _collect():
            raise SystemExit(0)

        from app import cli
        import app.extensions as extensions

        flask_app = Flask(__name__)
        flask_app.config["METRICS_FILE"] = "/dev/null"
        with flask_app.app_context(), \
                patch("app.monitoring.services.collect_system_metrics",
                      side_effect=_collect), \
                patch("app.monitoring.services.emit_health_transitions"), \
                patch("app.services.log_rotation.rotate_runtime_logs"), \
                patch.object(extensions, "db", MagicMock()), \
                patch("os.makedirs"), \
                patch("time.sleep",
                      side_effect=AssertionError("the loop should have exited")):
            with self.assertRaises(SystemExit):
                # .__wrapped__ steps past @with_appcontext, which wants a
                # live click context we are not inside of.
                cli.run_metrics_command.callback.__wrapped__()


# ---------------------------------------------------------------------------
# #33 -- the public voucher export is rate limited
# ---------------------------------------------------------------------------

class VoucherExportLimitTests(unittest.TestCase):
    def test_the_export_endpoint_carries_a_rate_limit(self):
        # It is public, unauthenticated, and answers with a generated document,
        # so an unmetered caller can make the server build PDFs all day.
        import inspect
        from app.main import routes

        source = inspect.getsource(routes.voucher_labels_export)
        self.assertIn("@limiter.limit(", source)

    def test_the_label_count_is_already_bounded_by_the_form_parsers(self):
        # Why no separate total-label cap: the existing page/column/row limits
        # cap one export at well under CodeRabbit's proposed 5,000, so such a
        # cap could only ever reject input the form cannot even produce.
        from werkzeug.datastructures import MultiDict
        from app.main import routes

        form = MultiDict({
            "preset": "custom", "custom_width": "0.5", "custom_height": "0.25",
            "custom_columns": "8", "custom_rows": "40", "pages": "100",
            "prefix": "AR",
        })
        labels, _layout = routes._voucher_labels_and_layout(form, output_format="pdf")
        self.assertLessEqual(len(labels), 5000)


# ---------------------------------------------------------------------------
# #36 -- anonymous What's New state lives in the session, not in a row per IP
# ---------------------------------------------------------------------------

class WhatsNewAnonymousStateTests(unittest.TestCase):
    def setUp(self):
        from app.main import routes
        self.routes = routes
        self.app = Flask(__name__)
        self.app.secret_key = "test"

    def test_an_unseen_browser_reads_as_none(self):
        with self.app.test_request_context():
            self.assertIsNone(self.routes.read_anonymous_whats_new_view())

    def test_a_written_timestamp_round_trips(self):
        from datetime import datetime
        seen = datetime(2026, 8, 20, 12, 0, 0)
        with self.app.test_request_context():
            self.routes.write_anonymous_whats_new_view(seen)
            self.assertEqual(self.routes.read_anonymous_whats_new_view(), seen)

    def test_a_corrupt_cookie_value_is_treated_as_unseen(self):
        from flask import session
        with self.app.test_request_context():
            session[self.routes.WHATS_NEW_SESSION_KEY] = "not-a-timestamp"
            self.assertIsNone(self.routes.read_anonymous_whats_new_view())

    def test_the_anonymous_path_no_longer_writes_a_row_keyed_by_ip(self):
        import inspect
        source = inspect.getsource(self.routes.whats_new)
        self.assertNotIn("WhatsNewView(ip_address=", source)
        self.assertNotIn("filter_by(ip_address=", source)
        self.assertIn("write_anonymous_whats_new_view", source)

    def test_the_badge_uses_the_session_for_anonymous_visitors(self):
        import inspect
        import app as app_package
        source = inspect.getsource(app_package.create_app)
        self.assertIn("read_anonymous_whats_new_view", source)
        self.assertNotIn("filter_by(ip_address=", source)


# ---------------------------------------------------------------------------
# #51 -- bounded synchronous waits on external APIs
# ---------------------------------------------------------------------------

class BlastPollBudgetTests(unittest.TestCase):
    def test_the_rtoe_wait_counts_against_the_total_budget(self):
        from app.services import blast_service

        slept = []
        clock = {"now": 0.0}

        def _sleep(seconds):
            slept.append(seconds)
            clock["now"] += seconds

        poll = MagicMock(side_effect=AssertionError("polled past the budget"))
        # NCBI's RTOE is upstream-controlled: pretend the response says half an
        # hour. It must be clamped to the budget, not slept off in full and then
        # followed by the whole polling window.
        with patch.object(blast_service.time, "sleep", side_effect=_sleep), \
                patch.object(blast_service.time, "monotonic",
                             side_effect=lambda: clock["now"]), \
                patch.object(blast_service, "_ncbi_request", poll):
            with self.assertRaises(TimeoutError):
                blast_service._poll_blast(
                    "RID1", rtoe=1800, config=SimpleNamespace(),
                    logger=logging.getLogger("test"), max_wait=30)

        self.assertEqual(slept, [30])
        poll.assert_not_called()

    def test_poll_sleeps_are_clamped_to_the_remaining_budget(self):
        from app.services import blast_service

        slept = []
        clock = {"now": 0.0}

        def _sleep(seconds):
            slept.append(seconds)
            clock["now"] += seconds

        waiting = SimpleNamespace(status_code=200, text="Status=WAITING")
        with patch.object(blast_service.time, "sleep", side_effect=_sleep), \
                patch.object(blast_service.time, "monotonic",
                             side_effect=lambda: clock["now"]), \
                patch.object(blast_service, "_ncbi_request", return_value=waiting):
            with self.assertRaises(TimeoutError):
                blast_service._poll_blast(
                    "RID1", rtoe=0, config=SimpleNamespace(),
                    logger=logging.getLogger("test"), max_wait=100)

        self.assertEqual(sum(slept), 100)
        self.assertTrue(all(s <= 100 for s in slept))

    def test_a_ready_result_returns_immediately(self):
        from app.services import blast_service

        ready = SimpleNamespace(status_code=200, text="Status=READY\nThereAreHits=yes")
        with patch.object(blast_service.time, "sleep"), \
                patch.object(blast_service, "_ncbi_request", return_value=ready):
            blast_service._poll_blast("RID1", rtoe=0, config=SimpleNamespace(),
                                      logger=logging.getLogger("test"))


class INaturalistPaginationBudgetTests(unittest.TestCase):
    def _page(self, count, total):
        return {"results": [{"id": i} for i in range(count)], "total_results": total}

    def test_a_deadline_stops_pagination_and_says_so(self):
        from app.services import inaturalist_service as svc

        clock = {"now": 0.0}

        def _request(_url):
            clock["now"] += 10  # each page "costs" ten seconds
            return self._page(200, 100000)

        with patch.object(svc, "_make_api_request", side_effect=_request), \
                patch.object(svc.time, "monotonic", side_effect=lambda: clock["now"]), \
                patch.object(svc.time, "sleep"):
            result = svc.fetch_observations_with_field_filter(
                {"per_page": 200}, "DNA Barcode ITS", deadline=25)

        self.assertTrue(result["timed_out"])
        self.assertTrue(result["truncated"])
        self.assertLess(result["fetched_count"], result["total_available"])

    def test_no_deadline_still_walks_the_whole_result_set(self):
        # Worker callers pass None and must keep their larger budget.
        from app.services import inaturalist_service as svc

        pages = [self._page(200, 400), self._page(200, 400)]
        with patch.object(svc, "_make_api_request", side_effect=pages), \
                patch.object(svc.time, "sleep"):
            result = svc.fetch_observations_with_field_filter(
                {"per_page": 200}, "DNA Barcode ITS")

        self.assertFalse(result["timed_out"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["fetched_count"], 400)

    def test_the_web_route_passes_an_interactive_budget(self):
        import inspect
        from app.api import routes
        source = inspect.getsource(routes.fetch_inaturalist)
        self.assertIn("INTERACTIVE_FETCH_BUDGET_SECONDS", source)
        self.assertGreater(
            __import__("app.services.inaturalist_service", fromlist=["x"])
            .INTERACTIVE_FETCH_BUDGET_SECONDS, 0)


# ---------------------------------------------------------------------------
# #55 -- the fallback NCBI FASTA fetch is batched like the primary path
# ---------------------------------------------------------------------------

class FallbackFastaBatchingTests(unittest.TestCase):
    def test_the_fallback_is_chunked_and_reports_per_chunk(self):
        from app.services import blast_service

        accessions = [f"OR{800000 + i}" for i in range(120)]
        posted = []

        def _request(method, url, **kwargs):
            ids = kwargs.get("data", {}).get("id", "")
            posted.append(ids.split(","))
            # Fail one chunk to prove the others survive it.
            status = 500 if len(posted) == 2 else 200
            return SimpleNamespace(status_code=status, text=">x\nACGT")

        reported = []
        with patch.object(blast_service, "_fetch_genbank_xml_batch", return_value=[]), \
                patch.object(blast_service, "_ncbi_request", side_effect=_request), \
                patch.object(blast_service, "_report_unresolved_accessions",
                             side_effect=lambda failed, recovered: reported.append(list(failed))):
            blast_service.fetch_fasta_for_accessions(accessions)

        # The XML pass batches at 50, and so does the fallback.
        self.assertEqual([len(chunk) for chunk in posted], [50, 50, 20])
        # Only the chunk that actually failed is reported unresolved.
        self.assertEqual(len(reported), 1)
        self.assertEqual(len(reported[0]), 50)


# ---------------------------------------------------------------------------
# #59 -- "NCBI did not answer" is not "this record has no location"
# ---------------------------------------------------------------------------

class GenBankLocationAvailabilityTests(unittest.TestCase):
    def setUp(self):
        from app.services import genbank_location_service as svc
        self.svc = svc
        svc._location_cache.clear()

    def test_a_failed_batch_is_reported_as_unavailable_not_missing(self):
        with patch.object(self.svc, "_fetch_annotation_xml", return_value=None):
            locations, missing, unavailable = self.svc.lookup_locations(
                ["OR807397", "OR807398"])

        self.assertEqual(locations, {})
        self.assertEqual(missing, [])
        self.assertEqual(unavailable, ["OR807397", "OR807398"])

    def test_an_accession_ncbi_had_no_record_for_is_still_missing(self):
        # The batch was fetched successfully and simply did not contain this
        # accession -- a fact about the record, so it stays in `missing`.
        parsed = {"by_acc": {}}
        with patch.object(self.svc, "_fetch_annotation_xml", return_value="<xml/>"), \
                patch.object(self.svc, "_parse_genbank_xml", return_value=parsed):
            locations, missing, unavailable = self.svc.lookup_locations(["OR807397"])

        self.assertEqual(locations, {})
        self.assertEqual(unavailable, [])
        self.assertEqual(missing, ["OR807397"])

    def test_a_record_answered_with_no_location_is_neither(self):
        # NCBI answered and the record genuinely carries no collection site.
        # Nothing failed, so this is not "unavailable"; the record was found,
        # so it is not "missing" either.
        parsed = {"by_acc": {"OR807397": {"accession": "OR807397",
                                          "version": "OR807397.1",
                                          "source_features": {}}}}
        with patch.object(self.svc, "_fetch_annotation_xml", return_value="<xml/>"), \
                patch.object(self.svc, "_parse_genbank_xml", return_value=parsed):
            locations, missing, unavailable = self.svc.lookup_locations(["OR807397"])

        self.assertEqual(locations, {})
        self.assertEqual(missing, [])
        self.assertEqual(unavailable, [])

    def test_a_found_location_is_returned_under_both_forms(self):
        parsed = {"by_acc": {"OR807397": {
            "accession": "OR807397", "version": "OR807397.1",
            "source_features": {"geo_loc_name": "USA: Arizona"}}}}
        with patch.object(self.svc, "_fetch_annotation_xml", return_value="<xml/>"), \
                patch.object(self.svc, "_parse_genbank_xml", return_value=parsed):
            locations, missing, unavailable = self.svc.lookup_locations(["OR807397.1"])

        self.assertEqual(locations["OR807397.1"], "USA: Arizona")
        self.assertEqual((missing, unavailable), ([], []))


# ---------------------------------------------------------------------------
# #60 -- an upstream OAuth body never reaches the user, or the log
# ---------------------------------------------------------------------------

class INatOAuthErrorTests(unittest.TestCase):
    def _http_error(self, body):
        class _Error(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("https://www.inaturalist.org/oauth/token", 400,
                                 "Bad Request", None, None)
                self._body = body

            def read(self):
                return self._body

        return _Error()

    def test_the_user_facing_error_carries_only_the_status(self):
        from app.services import inaturalist_oauth_service as svc

        secret_body = b'{"error":"invalid_client","client_secret":"hunter2"}'
        with patch("urllib.request.urlopen", side_effect=self._http_error(secret_body)), \
                self.assertLogs("app.services.inaturalist_oauth_service",
                                level="WARNING") as logs, \
                self.assertRaises(svc.InatAuthError) as caught:
            svc._http_post_json("https://www.inaturalist.org/oauth/token", {"a": "b"})

        message = str(caught.exception)
        self.assertIn("400", message)
        self.assertNotIn("hunter2", message)
        self.assertNotIn("invalid_client", message)

        logged = "\n".join(logs.output)
        # The log gets a fingerprint and a length, never the body itself.
        self.assertNotIn("hunter2", logged)
        self.assertNotIn("invalid_client", logged)
        self.assertIn("body_fingerprint=", logged)
        self.assertIn(f"body_bytes={len(secret_body)}", logged)

    def test_a_network_failure_says_so_without_echoing_internals(self):
        from app.services import inaturalist_oauth_service as svc

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused to 10.0.0.1")), \
                self.assertRaises(svc.InatAuthError) as caught:
            svc._http_get_json("https://www.inaturalist.org/users/api_token", "tok")

        self.assertNotIn("10.0.0.1", str(caught.exception))


# ---------------------------------------------------------------------------
# #67 / #68 -- Mushroom Observer upstream errors and malformed payloads
# ---------------------------------------------------------------------------

class MushroomObserverStatusMappingTests(unittest.TestCase):
    def test_upstream_auth_failures_do_not_masquerade_as_ours(self):
        from app.services.mushroom_observer_service import _map_upstream_status

        for upstream in (400, 401, 403, 422):
            status, message = _map_upstream_status(upstream)
            self.assertEqual(status, 502, f"upstream {upstream}")
            self.assertIn("not with your account", message)

    def test_a_genuine_not_found_stays_a_404(self):
        from app.services.mushroom_observer_service import _map_upstream_status
        status, message = _map_upstream_status(404)
        self.assertEqual(status, 404)
        self.assertIn("not found", message.lower())

    def test_upstream_throttling_becomes_503(self):
        from app.services.mushroom_observer_service import _map_upstream_status
        status, message = _map_upstream_status(429)
        self.assertEqual(status, 503)
        self.assertIn("try again", message.lower())

    def test_upstream_server_errors_become_502(self):
        from app.services.mushroom_observer_service import _map_upstream_status
        for upstream in (500, 502, 503):
            self.assertEqual(_map_upstream_status(upstream)[0], 502)

    def test_a_timeout_says_it_timed_out(self):
        from app.services.mushroom_observer_service import (
            MushroomObserverError, _api_request,
        )

        with patch("urllib.request.urlopen", side_effect=TimeoutError()), \
                self.assertLogs("app.services.mushroom_observer_service",
                                level="WARNING"), \
                self.assertRaises(MushroomObserverError) as caught:
            _api_request("observations")

        message = str(caught.exception)
        self.assertIn("did not respond", message)
        self.assertNotIn("invalid response", message)
        self.assertEqual(caught.exception.status, 504)


class MushroomObserverMalformedSequenceTests(unittest.TestCase):
    def _observation(self, sequences):
        return {"id": 575883, "sequences": sequences, "owner": {},
                "consensus": {"name": "Amanita muscaria"}, "location": {}}

    def test_a_malformed_sequence_id_is_skipped_not_fatal(self):
        from app.services import mushroom_observer_service as svc

        observation = self._observation([
            {"id": None, "locus": "ITS", "bases": "ACGT" * 40},
            {"id": "not-a-number", "locus": "ITS", "bases": "ACGT" * 40},
            "a bare string where an object should be",
            {"id": 42, "locus": "ITS", "bases": "ACGT" * 40},
        ])

        with patch.object(svc, "fetch_observation", return_value=observation), \
                patch.object(svc, "_fetch_sequence_details", return_value={}), \
                self.assertLogs("app.services.mushroom_observer_service",
                                level="WARNING") as logs:
            result = svc.analyze_observation("575883")

        # The one good sequence survives the three unusable rows.
        self.assertEqual([c["id"] for c in result["its_sequences"]], [42])
        self.assertIn("skipped 3 sequence record(s)", "\n".join(logs.output))

    def test_only_valid_ids_are_sent_upstream(self):
        from app.services import mushroom_observer_service as svc

        observation = self._observation([
            {"id": "7", "locus": "ITS", "bases": "ACGT" * 40},
            {"id": {"nested": "object"}, "locus": "ITS", "bases": "ACGT" * 40},
        ])
        requested = {}

        def _details(ids):
            requested["ids"] = list(ids)
            return {}

        with patch.object(svc, "fetch_observation", return_value=observation), \
                patch.object(svc, "_fetch_sequence_details", side_effect=_details), \
                self.assertLogs("app.services.mushroom_observer_service",
                                level="WARNING"):
            svc.analyze_observation("575883")

        self.assertEqual(requested["ids"], [7])

    def test_the_id_coercion_rejects_everything_unusable(self):
        from app.services.mushroom_observer_service import _coerce_sequence_id

        self.assertEqual(_coerce_sequence_id(42), 42)
        self.assertEqual(_coerce_sequence_id(" 42 "), 42)
        for bad in (None, "", "abc", -1, 0, True, {"a": 1}, [1]):
            self.assertIsNone(_coerce_sequence_id(bad), repr(bad))


# ---------------------------------------------------------------------------
# #76 -- "Clear jobs" reports what actually happened
# ---------------------------------------------------------------------------

class ClearJobsReportingTests(unittest.TestCase):
    def _run(self, rmtree_error=None):
        from app.user import routes

        jobs = [SimpleNamespace(id="job-a", job_dir="/tmp/does-not-matter/a"),
                SimpleNamespace(id="job-b", job_dir="/tmp/does-not-matter/b")]
        flashes = []
        db = MagicMock()

        app = Flask(__name__)
        with app.test_request_context(), \
                patch.object(routes, "Job") as job_model, \
                patch.object(routes, "db", db), \
                patch.object(routes, "current_user", SimpleNamespace(id=1)), \
                patch.object(routes, "flash",
                             side_effect=lambda msg, cat="message": flashes.append((msg, cat))), \
                patch.object(routes, "redirect", side_effect=lambda target: target), \
                patch.object(routes, "url_for", side_effect=lambda endpoint: endpoint), \
                patch("os.path.exists", return_value=True), \
                patch("os.path.isdir", return_value=True), \
                patch("shutil.rmtree", side_effect=rmtree_error):
            job_model.query.filter_by.return_value.all.return_value = jobs
            with patch.object(routes.logger, "warning") as warn:
                routes.clear_jobs.__wrapped__()
        return flashes, db, warn

    def test_a_clean_run_reports_success(self):
        flashes, db, warn = self._run()
        message, category = flashes[0]
        self.assertEqual(category, "success")
        self.assertIn("2 job(s) cleared", message)
        self.assertEqual(db.session.delete.call_count, 2)
        warn.assert_not_called()

    def test_a_failed_directory_removal_is_not_reported_as_success(self):
        flashes, db, warn = self._run(rmtree_error=PermissionError("nope"))
        message, category = flashes[0]
        self.assertEqual(category, "warning")
        self.assertIn("still", message)
        self.assertIn("could not be deleted", message)
        # The history rows are still removed; that half did succeed.
        self.assertEqual(db.session.delete.call_count, 2)
        # And the failure goes to the application logger, not to print().
        self.assertEqual(warn.call_count, 2)
        self.assertIn("jobs.clear_dir_failed", warn.call_args[0][0])

    def test_failures_go_to_the_logger_not_to_stdout(self):
        import inspect
        from app.user import routes
        # Strip the docstring, which describes the old print() on purpose.
        source = inspect.getsource(routes.clear_jobs)
        body = source.split('"""')[-1]
        self.assertNotIn("print(", body)
        self.assertIn("logger.warning(", body)


if __name__ == "__main__":
    unittest.main()
