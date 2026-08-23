"""Regressions for the CodeRabbit "probably fix" findings.

One file per batch rather than per finding: each of these is a handful of
assertions against an existing module, and scattering them across nineteen new
files would make the suite harder to read, not easier. Findings are called out
by their original number so a failure can be traced back to what it was for.
"""
import json
import os
import stat
import sys
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, g


def _undecorated(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# #6 -- failed API authentication is rate limited per client address
# ---------------------------------------------------------------------------

class _CountingStrategy:
    """Minimal stand-in for the flask-limiter strategy object."""

    def __init__(self, ceiling, namespace_ceilings=None):
        self.ceiling = ceiling
        self.namespace_ceilings = namespace_ceilings or {}
        self.counts = {}

    def _key(self, item, identifiers):
        return str(item), tuple(identifiers)

    def test(self, item, *identifiers):
        key = self._key(item, identifiers)
        namespace = identifiers[0] if identifiers else None
        ceiling = self.namespace_ceilings.get(namespace, self.ceiling)
        return self.counts.get(key, 0) < ceiling

    def hit(self, item, *identifiers):
        key = self._key(item, identifiers)
        self.counts[key] = self.counts.get(key, 0) + 1
        namespace = identifiers[0] if identifiers else None
        ceiling = self.namespace_ceilings.get(namespace, self.ceiling)
        return self.counts[key] <= ceiling

    def namespace_hits(self, namespace):
        return sum(
            count for (_item, identifiers), count in self.counts.items()
            if identifiers and identifiers[0] == namespace
        )


class FailedAuthThrottleTests(unittest.TestCase):
    def setUp(self):
        from app.api_v1 import auth
        self.auth = auth
        self.app = Flask(__name__)

    def _call(self, strategy, header=None, lookup=None):
        # The auth module imports the limiter lazily from app.extensions, so
        # that is where the double has to go.
        import app.extensions
        headers = {"Authorization": header} if header else {}
        with self.app.test_request_context(headers=headers), \
                patch.object(app.extensions, "limiter",
                             SimpleNamespace(limiter=strategy)), \
                patch.object(self.auth, "_lookup_token",
                             side_effect=lookup or (lambda plaintext: None)):
            view = self.auth.require_api_token(scope="jobs:read")(lambda: "ok")
            return view()

    def test_a_missing_token_is_charged_against_the_ip_budget(self):
        strategy = _CountingStrategy(ceiling=10)
        response = self._call(strategy)
        self.assertEqual(response.status_code, 401)
        # Two limits are configured (per minute and per hour), so one failed
        # request charges each of them once.
        self.assertEqual(
            strategy.namespace_hits(self.auth._FAILED_AUTH_NAMESPACE),
            len(self.auth.FAILED_AUTH_LIMITS),
        )

    def test_an_invalid_token_is_charged_too(self):
        strategy = _CountingStrategy(ceiling=10)
        response = self._call(strategy, header="Bearer dikarya_nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.get_data())["error"]["code"],
                         "invalid_token")
        self.assertEqual(
            strategy.namespace_hits(self.auth._FAILED_AUTH_NAMESPACE),
            len(self.auth.FAILED_AUTH_LIMITS),
        )

    def test_an_exhausted_budget_still_resolves_a_present_credential(self):
        strategy = _CountingStrategy(
            ceiling=10,
            namespace_ceilings={self.auth._FAILED_AUTH_NAMESPACE: 0},
        )
        lookup = MagicMock(return_value=None)
        response = self._call(strategy, header="Bearer dikarya_nope", lookup=lookup)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.get_data())["error"]["code"],
                         "too_many_failed_auth")
        lookup.assert_called_once_with("dikarya_nope")

    def test_a_valid_token_behind_an_exhausted_ip_still_succeeds(self):
        strategy = _CountingStrategy(
            ceiling=10,
            namespace_ceilings={self.auth._FAILED_AUTH_NAMESPACE: 0},
        )
        token = SimpleNamespace(id=1, is_active=True, last_used_at=datetime.utcnow(),
                                user=SimpleNamespace(id=2),
                                has_scope=lambda scope: True)
        response = self._call(strategy, header="Bearer dikarya_good",
                              lookup=lambda plaintext: token)
        self.assertEqual(response, "ok")
        self.assertEqual(
            strategy.namespace_hits(self.auth._FAILED_AUTH_NAMESPACE), 0
        )

    def test_a_valid_token_never_touches_the_failure_budget(self):
        strategy = _CountingStrategy(ceiling=10)
        token = SimpleNamespace(id=1, is_active=True, last_used_at=datetime.utcnow(),
                                user=SimpleNamespace(id=2),
                                has_scope=lambda scope: True)
        response = self._call(strategy, header="Bearer dikarya_good",
                              lookup=lambda plaintext: token)
        self.assertEqual(response, "ok")
        self.assertEqual(
            strategy.namespace_hits(self.auth._FAILED_AUTH_NAMESPACE), 0
        )

    def test_high_pre_auth_ceiling_bounds_random_token_lookups(self):
        strategy = _CountingStrategy(
            ceiling=10,
            namespace_ceilings={self.auth._PRE_AUTH_LOOKUP_NAMESPACE: 0},
        )
        lookup = MagicMock(return_value=None)
        response = self._call(
            strategy, header="Bearer dikarya_random", lookup=lookup
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            json.loads(response.get_data())["error"]["code"],
            "too_many_auth_attempts",
        )
        lookup.assert_not_called()

    def test_a_broken_limiter_backend_fails_open(self):
        class _Broken:
            def test(self, *a, **kw):
                raise RuntimeError("storage down")

            def hit(self, *a, **kw):
                raise RuntimeError("storage down")

        response = self._call(_Broken(), header="Bearer dikarya_nope")
        # 401 (the real answer), not 429 and not a 500.
        self.assertEqual(response.status_code, 401)


# ---------------------------------------------------------------------------
# #8 -- a pending idempotency reservation outlives a slow handler
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, on_eval=None):
        self.values = {}
        self.expiries = {}
        self.evals = []
        self.on_eval = on_eval

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value.encode() if isinstance(value, str) else value
        self.expiries[key] = ex
        return True

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value.encode() if isinstance(value, str) else value
        self.expiries[key] = ttl

    def delete(self, key):
        self.values.pop(key, None)
        self.expiries.pop(key, None)

    def eval(self, script, numkeys, key, value, ttl):
        self.evals.append((key, value, ttl))
        if self.on_eval is not None:
            self.on_eval()
        current = self.values.get(key)
        if current == (value.encode() if isinstance(value, str) else value):
            self.expiries[key] = int(ttl)
            return 1
        return 0


class IdempotencyLeaseTests(unittest.TestCase):
    def setUp(self):
        from app.api_v1 import idempotency
        self.idem = idempotency
        self.app = Flask(__name__)

    def test_the_pending_ttl_covers_a_synchronous_request(self):
        # The handlers this protects can outlive a minute: /tools/inaturalist-tree
        # walks upstream pagination. A 60s reservation expired underneath them.
        self.assertGreaterEqual(self.idem.IDEM_PENDING_TTL_SECONDS, 120)
        self.assertLess(self.idem.IDEM_PENDING_REFRESH_SECONDS,
                        self.idem.IDEM_PENDING_TTL_SECONDS)
        self.assertFalse(hasattr(self.idem, "IDEM_PENDING_MAX_LEASE_SECONDS"))

    def test_the_reservation_is_renewed_while_the_handler_runs(self):
        import threading

        renewed = threading.Event()
        r = _FakeRedis(on_eval=renewed.set)
        # The handler blocks until a renewal actually happens, so this asserts
        # the behaviour rather than racing a wall clock.
        with patch.object(self.idem, "IDEM_PENDING_REFRESH_SECONDS", 0.01), \
                patch.object(self.idem, "_redis", return_value=r), \
                self.app.test_request_context(
                    method="POST", json={"a": 1},
                    headers={"Idempotency-Key": "abc123"}):
            g.api_user = SimpleNamespace(id=7)

            @self.idem.idempotent
            def handler():
                self.assertTrue(renewed.wait(timeout=10),
                                "the lease was never renewed under the handler")
                from flask import jsonify
                return jsonify({"ok": True})

            response = handler()

        self.assertEqual(response.status_code, 200)
        # Every renewal re-asserts the full TTL rather than letting the original
        # window run down under a still-running request.
        self.assertTrue(r.evals)
        self.assertTrue(all(ttl == self.idem.IDEM_PENDING_TTL_SECONDS
                            for _key, _value, ttl in r.evals))

    def test_a_renewal_never_extends_somebody_elses_value(self):
        r = _FakeRedis()
        r.set("k", "PENDING:mine")
        self.assertTrue(self.idem._refresh_pending(r, "k", "PENDING:mine"))
        r.set("k", "PENDING:theirs")
        self.assertFalse(self.idem._refresh_pending(r, "k", "PENDING:mine"))

    def test_renewal_has_no_nine_hundred_second_ceiling(self):
        lease = self.idem._PendingLease(None, "k", "PENDING:mine")

        class _StopAfter:
            def __init__(self, renewals):
                self.remaining = renewals

            def wait(self, _seconds):
                self.remaining -= 1
                return self.remaining < 0

        # At the production refresh interval this represents well over 900s.
        lease._stop = _StopAfter(40)
        with patch.object(self.idem, "_refresh_pending", return_value=True) as refresh:
            lease._run()
        self.assertEqual(refresh.call_count, 40)

    def test_the_completed_response_is_still_cached_for_a_day(self):
        r = _FakeRedis()
        with patch.object(self.idem, "_redis", return_value=r), \
                self.app.test_request_context(
                    method="POST", json={"a": 1},
                    headers={"Idempotency-Key": "abc123"}):
            g.api_user = SimpleNamespace(id=7)

            @self.idem.idempotent
            def handler():
                from flask import jsonify
                return jsonify({"ok": True})

            handler()
            key = self.idem._key(7, "POST", "/", "abc123")
            self.assertEqual(r.expiries[key], self.idem.IDEM_TTL_SECONDS)
            replay = handler()

        self.assertEqual(replay.headers.get("X-Idempotent-Replay"), "true")

    def test_a_handler_error_releases_its_reservation(self):
        r = _FakeRedis()
        with patch.object(self.idem, "_redis", return_value=r), \
                self.app.test_request_context(
                    method="POST", json={"a": 1},
                    headers={"Idempotency-Key": "abc123"}):
            g.api_user = SimpleNamespace(id=7)

            @self.idem.idempotent
            def handler():
                raise RuntimeError("handler failed")

            with self.assertRaises(RuntimeError):
                handler()

        self.assertFalse(r.values)

    def test_a_crashed_owner_leaves_only_a_finite_lease(self):
        r = _FakeRedis()
        self.assertTrue(r.set("k", "PENDING:mine", nx=True,
                              ex=self.idem.IDEM_PENDING_TTL_SECONDS))
        self.assertEqual(r.expiries["k"], self.idem.IDEM_PENDING_TTL_SECONDS)
        # Redis expires it after no renewal arrives; model that expiry and prove
        # the next request can reserve normally.
        r.delete("k")
        self.assertTrue(r.set("k", "PENDING:retry", nx=True,
                              ex=self.idem.IDEM_PENDING_TTL_SECONDS))

    def test_no_eval_fallback_compares_ownership_before_expiring(self):
        class _Pipeline:
            def __init__(self, owner):
                self.owner = owner
                self.pending_expire = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def watch(self, _key):
                return None

            def get(self, key):
                return self.owner.values.get(key)

            def unwatch(self):
                return None

            def multi(self):
                return None

            def expire(self, key, ttl):
                self.pending_expire = (key, ttl)

            def execute(self):
                key, ttl = self.pending_expire
                self.owner.expiries[key] = ttl
                return [1]

        class _NoEvalRedis:
            def __init__(self):
                self.values = {"k": b"PENDING:mine"}
                self.expiries = {}

            def pipeline(self):
                return _Pipeline(self)

        r = _NoEvalRedis()
        self.assertTrue(self.idem._refresh_pending(r, "k", "PENDING:mine"))
        r.values["k"] = b"PENDING:theirs"
        previous = dict(r.expiries)
        self.assertFalse(self.idem._refresh_pending(r, "k", "PENDING:mine"))
        self.assertEqual(r.expiries, previous)


# ---------------------------------------------------------------------------
# #12 -- the OpenAPI job schema tracks what the runtime actually does
# ---------------------------------------------------------------------------

class OpenAPIDriftTests(unittest.TestCase):
    def test_job_params_documents_trim_terminal_overhangs(self):
        from app.api_v1.openapi import _schemas
        params = _schemas()["Job"]["properties"]["params"]["properties"]
        self.assertIn("trim_terminal_overhangs", params)

    def test_job_params_documents_everything_serialize_job_returns(self):
        # Parsed from the source rather than executed, so this needs no request
        # context and still fails the moment the two lists diverge.
        import ast
        import inspect
        from app.api_v1 import jobs
        from app.api_v1.openapi import _schemas

        tree = ast.parse(inspect.getsource(jobs.serialize_job))
        returned = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys
                        if isinstance(k, ast.Constant)]
                if "params" in keys:
                    returned = node.values[keys.index("params")]
                    break
        self.assertIsNotNone(returned, "could not locate serialize_job's params dict")
        serialized = {k.value for k in returned.keys if isinstance(k, ast.Constant)}
        documented = set(_schemas()["Job"]["properties"]["params"]["properties"])
        self.assertEqual(serialized - documented, set())

    def test_documented_defaults_are_the_defaults_the_route_applies(self):
        from app.api_v1.job_defaults import (
            DEFAULT_ALIGNMENT_METHOD, DEFAULT_BOOTSTRAP, DEFAULT_TREE_METHOD,
        )
        from app.api_v1.openapi import _schemas
        from app.config import Config

        create = _schemas()["CreateJobRequest"]["properties"]
        self.assertEqual(create["alrt_replicates"]["default"], Config.DEFAULT_IQTREE_ALRT)
        self.assertEqual(create["trimming_method"]["default"], Config.DEFAULT_TRIMMING_METHOD)
        self.assertEqual(create["bootstrap"]["default"], DEFAULT_BOOTSTRAP)
        self.assertEqual(create["tree_method"]["default"], DEFAULT_TREE_METHOD)
        self.assertEqual(create["alignment_method"]["default"], DEFAULT_ALIGNMENT_METHOD)


# ---------------------------------------------------------------------------
# #19 -- ?since= / ?until= are normalized to the naive UTC the column holds
# ---------------------------------------------------------------------------

class QueryTimestampTests(unittest.TestCase):
    def setUp(self):
        from app.api_v1.routes import parse_utc_query_timestamp
        self.parse = parse_utc_query_timestamp

    def test_an_offset_is_converted_to_utc_and_stripped(self):
        parsed = self.parse("2026-08-01T00:00:00-07:00")
        self.assertIsNone(parsed.tzinfo)
        self.assertEqual(parsed, datetime(2026, 8, 1, 7, 0, 0))

    def test_a_trailing_z_is_utc(self):
        parsed = self.parse("2026-08-01T12:30:00Z")
        self.assertIsNone(parsed.tzinfo)
        self.assertEqual(parsed, datetime(2026, 8, 1, 12, 30, 0))

    def test_a_naive_timestamp_is_taken_as_utc_and_not_shifted(self):
        # Documented assumption: no offset means the caller already meant UTC,
        # which is what created_at is returned in.
        parsed = self.parse("2026-08-01T12:30:00")
        self.assertEqual(parsed, datetime(2026, 8, 1, 12, 30, 0))

    def test_equivalent_spellings_produce_the_same_instant(self):
        self.assertEqual(self.parse("2026-08-01T00:00:00-07:00"),
                         self.parse("2026-08-01T07:00:00Z"))

    def test_junk_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("yesterday")


# ---------------------------------------------------------------------------
# #22 -- one shared same-origin redirect validator
# ---------------------------------------------------------------------------

class SafeNextUrlTests(unittest.TestCase):
    def setUp(self):
        from app.services.security_utils import safe_next_url
        self.safe = safe_next_url

    def test_internal_paths_survive(self):
        for value in ("/", "/user/jobs", "/whats-new?edit=1", "/job/abc#tree",
                      "/a%20b"):
            self.assertEqual(self.safe(value), value)

    def test_external_targets_are_rejected(self):
        for value in ("https://evil.tld/phish", "http://evil.tld",
                      "//evil.tld/phish", "javascript:alert(1)",
                      "mailto:a@b.c", "evil.tld"):
            self.assertIsNone(self.safe(value), value)

    def test_backslash_normalization_tricks_are_rejected(self):
        # A browser rewrites '\' to '/' before navigating, so these become
        # protocol-relative URLs -- i.e. off-site -- after validation.
        for value in ("/\\evil.tld", "\\\\evil.tld", "/\\/evil.tld"):
            self.assertIsNone(self.safe(value), value)

    def test_control_characters_are_rejected(self):
        for value in ("/a\nb", "/a\rb", "/a\tb", "/a b", "/a\x00b"):
            self.assertIsNone(self.safe(value), value)

    def test_empty_input_is_none(self):
        self.assertIsNone(self.safe(None))
        self.assertIsNone(self.safe(""))

    def test_both_call_sites_use_the_shared_helper(self):
        import inspect
        from app.auth import routes as auth_routes
        from app.main import routes as main_routes

        self.assertIn("safe_next_url", inspect.getsource(auth_routes._safe_next))
        self.assertIn("safe_next_url",
                      inspect.getsource(main_routes.whats_new_delete))


# ---------------------------------------------------------------------------
# #46 -- health and the reaper agree about what "stuck" means
# ---------------------------------------------------------------------------

class StuckJobDetectionTests(unittest.TestCase):
    def test_health_coalesces_updated_at_with_created_at(self):
        # A legacy row with a NULL updated_at made `updated_at < cutoff` NULL,
        # so the health check could not see the very rows the reaper lists.
        import inspect
        from app.monitoring import services
        source = inspect.getsource(services.emit_health_transitions)
        self.assertIn("func.coalesce(Job.updated_at, Job.created_at)", source)

    def test_health_and_the_reaper_use_the_same_expression(self):
        import inspect
        from app import cli
        from app.monitoring import services
        expression = "func.coalesce(Job.updated_at, Job.created_at) < cutoff"
        self.assertIn(expression, inspect.getsource(services.emit_health_transitions))
        self.assertIn(expression,
                      inspect.getsource(cli.reap_stuck_jobs_command.callback))

    def test_the_one_day_threshold_is_unchanged(self):
        import inspect
        from app.monitoring import services
        self.assertIn("timedelta(days=1)",
                      inspect.getsource(services.emit_health_transitions))


# ---------------------------------------------------------------------------
# #57 -- gap-only "DNA" is not DNA
# ---------------------------------------------------------------------------

class CleanDnaSequenceTests(unittest.TestCase):
    def setUp(self):
        from app.services.fasta_utils import clean_dna_sequence
        self.clean = clean_dna_sequence

    def test_a_long_run_of_gaps_is_not_a_sequence(self):
        self.assertEqual(self.clean("-" * 500), "")

    def test_a_long_run_of_ns_is_not_a_sequence(self):
        self.assertEqual(self.clean("N" * 500), "")

    def test_ambiguity_codes_alone_are_not_a_sequence(self):
        self.assertEqual(self.clean("RYSWKM" * 40), "")

    def test_a_real_sequence_still_passes(self):
        real = "ACGT" * 40
        self.assertEqual(self.clean(real), real)

    def test_a_heavily_ambiguous_but_real_read_is_kept(self):
        # Deliberately no percentage threshold: this is a poor read, not a
        # non-sequence, and users do submit them.
        messy = ("N" * 99) + "A" + ("N" * 100)
        self.assertEqual(self.clean(messy), messy.upper())

    def test_gaps_inside_a_real_sequence_are_kept(self):
        aligned = "ACGT" + ("-" * 20) + ("ACGT" * 30)
        self.assertEqual(self.clean(aligned), aligned)

    def test_a_gap_run_never_beats_a_shorter_real_run(self):
        # The gap block is longer, but only the run with actual bases counts.
        real = "ACGT" * 30
        text = ("-" * 400) + "!" + real
        self.assertEqual(self.clean(text), real)


# ---------------------------------------------------------------------------
# #61 -- the temporary OAuth token file is private from the moment it exists
# ---------------------------------------------------------------------------

class OAuthTokenFilePermissionTests(unittest.TestCase):
    def test_the_temp_file_is_created_0600_before_secrets_are_written(self):
        from app.services import inaturalist_oauth_service as svc

        observed = {}
        real_fdopen = os.fdopen

        def _spy(fd, *args, **kwargs):
            # Inspect the descriptor's mode *before* json.dump runs.
            observed["mode"] = stat.S_IMODE(os.fstat(fd).st_mode)
            return real_fdopen(fd, *args, **kwargs)

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inat", "tokens.json")
            with patch.object(svc, "_token_path", return_value=path), \
                    patch.object(os, "fdopen", side_effect=_spy):
                svc.save_tokens({"access_token": "super-secret"})

            self.assertEqual(observed["mode"], 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as f:
                self.assertEqual(json.load(f)["access_token"], "super-secret")
            # Atomic replace leaves nothing behind.
            self.assertEqual(os.listdir(os.path.dirname(path)), ["tokens.json"])

    def test_a_failed_write_leaves_no_partial_token_file(self):
        from app.services import inaturalist_oauth_service as svc

        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tokens.json")
            with patch.object(svc, "_token_path", return_value=path), \
                    patch.object(svc.json, "dump", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    svc.save_tokens({"access_token": "super-secret"})
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
