"""Scanner noise stays out of errors.log -- especially when things go wrong.

Two failures this covers.

**The fail-safe was backwards.** `install_scanner_log` set the level and
`propagate = False` as its last two statements, after `mkdir()` and
`WatchedFileHandler()`. A read-only or full var/logs raised OSError out of those
with the logger still propagating and still handler-less, so every scanner
record fell through to the root logger -- which is the WARNING+ `errors.log`
mirror. That is the opposite of the intended behaviour, and it fires exactly
when it hurts most: sweeps are heaviest when something is already wrong.

**The method was thrown away before classification.** `_safe_method()` mapped
anything outside the standard verb set to the literal string "OTHER", so a
PROPFIND sweep, a TRACE probe and a garbage verb arrived at the classifier
indistinguishable from each other. Those verbs *are* the signature.
"""

import logging
import unittest
from unittest import mock

from app.services import request_diagnostics
from app.services.log_context import (
    SCANNER_LOGGER_NAME,
    install_scanner_log,
    scanner_logger,
)
from app.services.security_events import (
    BUCKET_SCANNER,
    BUCKET_TARGETED,
    SERVED_METHODS,
    classify_request_failure,
    normalize_method,
)


class ScannerLoggerFailSafeTests(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger(SCANNER_LOGGER_NAME)
        self._handlers = list(logger.handlers)
        self._level = logger.level
        self._propagate = logger.propagate
        logger.handlers = []
        self.addCleanup(self._restore)

    def _restore(self):
        logger = logging.getLogger(SCANNER_LOGGER_NAME)
        logger.handlers = self._handlers
        logger.setLevel(self._level)
        logger.propagate = self._propagate

    def test_a_handler_failure_still_leaves_the_logger_silent(self):
        with mock.patch("logging.handlers.WatchedFileHandler",
                        side_effect=OSError("Read-only file system")):
            with self.assertRaises(OSError):
                install_scanner_log("/nonexistent/var/logs/scanner.log")

        logger = logging.getLogger(SCANNER_LOGGER_NAME)
        self.assertFalse(logger.propagate)
        self.assertEqual(logger.level, logging.INFO)

    def test_a_mkdir_failure_still_leaves_the_logger_silent(self):
        with mock.patch("pathlib.Path.mkdir", side_effect=OSError("Permission denied")):
            with self.assertRaises(OSError):
                install_scanner_log("/nonexistent/var/logs/scanner.log")

        self.assertFalse(logging.getLogger(SCANNER_LOGGER_NAME).propagate)

    def test_scanner_records_never_reach_the_root_logger_after_a_failure(self):
        """The behaviour the flag exists for, asserted end to end."""
        with mock.patch("logging.handlers.WatchedFileHandler",
                        side_effect=OSError("Read-only file system")):
            with self.assertRaises(OSError):
                install_scanner_log("/nonexistent/var/logs/scanner.log")

        root = logging.getLogger()
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = _Capture()
        root.addHandler(handler)
        previous_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            scanner_logger().info("event=security.scanner path=/.env")
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        self.assertEqual(captured, [])

    def test_installation_is_idempotent_and_reasserts_the_properties(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "logs" / "scanner.log"
            self.assertTrue(install_scanner_log(str(target)))
            # Something else reconfigures the logger -- a library, a later
            # basicConfig, a test.
            logging.getLogger(SCANNER_LOGGER_NAME).propagate = True
            self.assertFalse(install_scanner_log(str(target)))
            self.assertFalse(logging.getLogger(SCANNER_LOGGER_NAME).propagate)
            # And only one file handler, not two.
            file_handlers = [
                h for h in logging.getLogger(SCANNER_LOGGER_NAME).handlers
                if getattr(h, "baseFilename", None)
            ]
            self.assertEqual(len(file_handlers), 1)

    def test_records_do_reach_the_file_when_it_can_be_opened(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "logs" / "scanner.log"
            install_scanner_log(str(target))
            scanner_logger().info("event=security.scanner path=/.env")
            logging.getLogger(SCANNER_LOGGER_NAME).handlers[-1].flush()
            self.assertIn("event=security.scanner", target.read_text())


class MethodNormalizationTests(unittest.TestCase):
    def test_a_real_verb_survives_unchanged(self):
        for method in ("GET", "POST", "PROPFIND", "TRACE", "CONNECT", "SEARCH"):
            self.assertEqual(normalize_method(method), method)

    def test_case_is_normalized(self):
        self.assertEqual(normalize_method("propfind"), "PROPFIND")

    def test_a_log_injection_attempt_is_stripped(self):
        self.assertNotIn("\n", normalize_method("GET\nX-Injected: 1"))
        self.assertNotIn(" ", normalize_method("GET /etc/passwd"))

    def test_an_absurdly_long_verb_is_bounded(self):
        self.assertLessEqual(len(normalize_method("A" * 5000)), 24)

    def test_an_empty_or_unusable_verb_resolves_to_other(self):
        for value in ("", None, "1234", "!!!"):
            self.assertEqual(normalize_method(value), "OTHER")

    def test_other_is_not_a_served_verb(self):
        self.assertNotIn("OTHER", SERVED_METHODS)


class ScannerClassificationTests(unittest.TestCase):
    def test_propfind_is_reported_as_an_unsupported_method(self):
        bucket, reason = classify_request_failure(
            path="/", method="PROPFIND", status=405
        )
        self.assertEqual(bucket, BUCKET_SCANNER)
        self.assertEqual(reason, "unsupported_method")

    def test_the_verb_beats_the_status_code(self):
        """A PROPFIND sweep answers 405 on nearly every path it tries.

        Classifying on the status first would relabel the whole sweep as a path
        probe and lose the one thing that identifies it.
        """
        bucket, reason = classify_request_failure(
            path="/webdav/somewhere", method="PROPFIND", status=405
        )
        self.assertEqual((bucket, reason), (BUCKET_SCANNER, "unsupported_method"))

    def test_trace_and_connect_are_recognised(self):
        for method in ("TRACE", "TRACK", "CONNECT"):
            bucket, reason = classify_request_failure(
                path="/", method=method, status=400
            )
            self.assertEqual((bucket, reason),
                             (BUCKET_SCANNER, "unsupported_method"), method)

    def test_every_served_verb_is_left_alone(self):
        for method in sorted(SERVED_METHODS):
            bucket, _reason = classify_request_failure(
                path="/tree", method=method, status=404, matched_route="/tree",
                app_segments={"tree"},
            )
            self.assertIsNone(bucket, method)

    def test_a_known_scanner_path_is_still_a_path_probe(self):
        bucket, reason = classify_request_failure(path="/.env", status=404)
        self.assertEqual((bucket, reason), (BUCKET_SCANNER, "known_scanner_path"))

    def test_traversal_still_outranks_the_verb(self):
        # Traversal beats the sweep signature, whichever traversal reason it
        # earns. This one starts from /job, so it is the app-surface variant:
        # climbing out of a job artifact route is aimed at Dikarya, not at
        # whatever happens to answer on port 443.
        bucket, reason = classify_request_failure(
            path="/job/../../etc/passwd", method="PROPFIND", status=404
        )
        self.assertEqual(
            (bucket, reason), (BUCKET_TARGETED, "path_traversal_app_surface")
        )

    def test_generic_traversal_is_separated_from_traversal_aimed_at_us(self):
        # A sweep asks every host on the internet for /etc/passwd; only
        # somebody who has looked at this app climbs out of a job route. The
        # two are reported under different reasons because they are different
        # events, and the second one is worth far more to the actor score.
        generic, generic_reason = classify_request_failure(
            path="/etc/passwd", status=404
        )
        self.assertEqual((generic, generic_reason), (BUCKET_TARGETED, "path_traversal"))
        aimed, aimed_reason = classify_request_failure(
            path="/api/job/aq7c/download/../../../../proc/self/environ", status=404
        )
        self.assertEqual(
            (aimed, aimed_reason), (BUCKET_TARGETED, "path_traversal_app_surface")
        )

    def test_an_ordinary_user_error_is_neither(self):
        bucket, reason = classify_request_failure(
            path="/whats-new", method="GET", status=404, matched_route=None,
            app_segments={"whats-new", "tree", "job"},
        )
        self.assertEqual((bucket, reason), (None, ""))

    def test_an_authorization_failure_on_a_real_route_is_not_a_probe(self):
        # It is overwhelmingly an ordinary outcome, and reporting it as a probe
        # once short-circuited the normal diagnostics for matched routes.
        bucket, _reason = classify_request_failure(
            path="/api/job/aq7c/download", method="GET", status=403,
            matched_route="/api/job/<job_id>/download", job_id_valid=True,
        )
        self.assertIsNone(bucket)


class RequestDiagnosticsWiringTests(unittest.TestCase):
    def test_safe_method_no_longer_renames_unusual_verbs(self):
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/", environ_overrides={"REQUEST_METHOD": "PROPFIND"}):
            self.assertEqual(request_diagnostics._safe_method(), "PROPFIND")

    def test_a_matched_route_classified_as_scanner_is_still_filed(self):
        """A probe that happens to land on a real rule is still a probe.

        Scanner logging used to sit inside the unmatched-route branch, so a
        PROPFIND that Werkzeug answers 405 on, or a bot fetching a real page
        that 404s, was never written to scanner.log at all and the sweep looked
        smaller than it was.
        """
        source = open(request_diagnostics.__file__).read()
        classify_index = source.index("bucket, reason = _classify(app, status)")
        # Widened from 1200: the honeytoken check and the path-crowd lookup sit
        # between the classify call and the logging branches, and the actor
        # scoring sits between those branches and the early return. The
        # invariant under test is unchanged -- the scanner branch is still
        # beside the targeted one and still ahead of the unmatched-route
        # early return.
        window = source[classify_index:classify_index + 4200]
        self.assertIn("elif bucket == BUCKET_SCANNER:", window)
        self.assertLess(
            window.index("elif bucket == BUCKET_SCANNER:"),
            window.index("if status < 500 and ("),
        )

    def test_the_ordinary_failure_line_is_still_emitted_for_matched_routes(self):
        # The security record only ever ADDS. A matched route keeps its
        # http.request_failed line, which carries the developer reason code.
        source = open(request_diagnostics.__file__).read()
        self.assertIn("event=http.request_failed", source)
        self.assertLess(
            source.index("elif bucket == BUCKET_SCANNER:"),
            source.index("event=http.request_failed"),
        )


if __name__ == "__main__":
    unittest.main()
