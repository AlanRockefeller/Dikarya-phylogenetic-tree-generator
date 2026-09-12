"""Public API coverage for the server-side iNaturalist finder."""
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g

from app.api_v1 import routes as v1
from app.services import inat_finder_service as finder
from app.services.inaturalist_tree_service import InatTreeError


def _undecorated(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class InatFinderServiceTests(unittest.TestCase):
    def test_variations_cover_browser_finder_typo_classes(self):
        variations = finder.build_variations("123456789", 1)
        self.assertIn("123465789", variations)
        self.assertNotIn("123456789", variations)
        self.assertEqual(len(variations), len(set(variations)))

        short_variations = finder.build_variations("1234", 1)
        self.assertIn("129934", short_variations)
        self.assertFalse(any(len(item) > 1 and item.startswith("0") for item in short_variations))

    def test_search_returns_stable_matches_and_completeness_counts(self):
        taxon = {"id": 48419, "name": "Amanita", "rank": "genus"}
        original = {
            "id": 123456789,
            "observed_on": "2026-09-01",
            "place_guess": "California",
            "place_ids": [],
            "user": {"id": 7, "login": "observer"},
            "taxon": taxon,
            "photos": [{"url": "https://static.inaturalist.org/photo.jpg"}],
        }

        def fake_fetch(ids, mode, criteria):
            return [original] if ids == ["123456789"] else []

        with (
            patch.object(finder, "resolve_criteria", return_value={
                "label": "Amanita (taxon ID 48419)",
                "taxon_id": 48419,
                "taxon": taxon,
            }),
            patch.object(finder, "_fetch_matches", side_effect=fake_fetch),
        ):
            result = finder.find_observations(
                observation="123456789", mode="genus", term="Amanita", digits_off=1
            )

        self.assertTrue(result["complete"])
        self.assertEqual(result["checked_variations"], result["total_variations"])
        self.assertEqual(result["unchecked_variations"], 0)
        self.assertEqual(result["match_count"], 1)
        self.assertTrue(result["matches"][0]["is_original"])
        self.assertEqual(result["matches"][0]["user"]["login"], "observer")
        self.assertEqual(result["matches"][0]["taxon"]["id"], 48419)
        self.assertEqual(
            result["matches"][0]["url"],
            "https://www.inaturalist.org/observations/123456789",
        )

    def test_ambiguous_taxon_returns_machine_readable_candidates(self):
        responses = [
            {"results": [
                {"id": 1, "name": "Prunella", "rank": "genus", "iconic_taxon_name": "Plantae"},
                {"id": 2, "name": "Prunella", "rank": "genus", "iconic_taxon_name": "Aves"},
            ]}
        ]
        with patch.object(finder, "_api_get", side_effect=responses):
            with self.assertRaises(finder.FinderValidationError) as raised:
                finder.resolve_criteria("genus", "Prunella")
        self.assertEqual([item["id"] for item in raised.exception.details["candidates"]], [1, 2])

    def test_api_limit_is_checked_before_criteria_lookup(self):
        with patch.object(finder, "resolve_criteria") as resolve:
            with self.assertRaises(finder.FinderValidationError):
                finder.find_observations(
                    observation="123456789", mode="genus", term="Amanita", digits_off=3
                )
        resolve.assert_not_called()


class InatFinderRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_route_returns_the_standard_data_envelope(self):
        expected = {"matches": [], "complete": True}
        with (
            self.app.test_request_context(method="POST", json={
                "observation": "123456789", "mode": "genus", "term": "Amanita"
            }),
            patch("app.services.inat_finder_service.find_observations", return_value=expected),
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.tools_inaturalist_finder)()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"data": expected})

    def test_route_rejects_non_object_json(self):
        with self.app.test_request_context(method="POST", json=["bad"]):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.tools_inaturalist_finder)()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "bad_request")


class InatFinderCliParityTests(unittest.TestCase):
    """The API ladder must stay identical to the vendored inat_finder.py.

    Membership alone is not enough: a resume cursor is a position in the
    candidate sequence, so the ORDER is part of the contract. If the CLI is
    synced without updating this module, these hashes move and the test says so.
    """

    @staticmethod
    def _cli():
        import importlib.util
        import sys
        import types

        if "tqdm" not in sys.modules:
            module = types.ModuleType("tqdm")
            module.tqdm = lambda *args, **kwargs: None
            sys.modules["tqdm"] = module
        repo = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("inat_finder_cli_api", repo / "inat_finder.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        return cli

    @staticmethod
    def _digest(sequence):
        digest = hashlib.sha256()
        for candidate in sequence:
            digest.update(str(candidate).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    # Lengths chosen to exercise the insertion (< 9) and removal (> 5) switches,
    # repeated adjacent digits, and a number whose removals collide.
    NUMBERS = ("12345", "123456", "1223334", "123456789", "1000000000", "111")

    def test_candidate_plans_match_the_cli_exactly(self):
        try:
            cli = self._cli()
        except ImportError as exc:  # pragma: no cover - depends on the environment
            self.skipTest(f"the vendored CLI needs {exc.name}")
        for number in self.NUMBERS:
            for digits in (1, 2, 3):
                with self.subTest(number=number, digits=digits):
                    theirs = cli.build_candidate_plan(number, digits)
                    ours = finder.build_candidate_plan(number, digits)
                    self.assertEqual(ours.total, theirs.total)
                    self.assertEqual(ours.replacement_count, theirs.replacement_count)
                    self.assertEqual(len(ours.additions), len(theirs.additions))
                    self.assertEqual(len(ours.removals), len(theirs.removals))
                    self.assertEqual(len(ours.transpositions), len(theirs.transpositions))
                    self.assertEqual(self._digest(ours), self._digest(theirs))
                    self.assertEqual(
                        finder.auto_stage_label(digits, ours),
                        cli.auto_stage_label(digits, theirs),
                    )

    def test_the_ladder_and_its_cursors_match_the_cli(self):
        try:
            cli = self._cli()
        except ImportError as exc:  # pragma: no cover - depends on the environment
            self.skipTest(f"the vendored CLI needs {exc.name}")
        for number in self.NUMBERS:
            with self.subTest(number=number):
                ours_seen, theirs_seen = set(), set()
                for index in (1, 2, 3):
                    ours = finder.AutoStage(index, finder.build_candidate_plan(number, index), ours_seen)
                    theirs = cli.AutoStage(index, cli.build_candidate_plan(number, index), theirs_seen)
                    self.assertEqual(ours.total, theirs.total)
                    self.assertEqual(self._digest(ours), self._digest(theirs))
                    self.assertEqual(len(ours_seen), len(theirs_seen))
                # A cursor replays to exactly the set the original run had tried.
                # Ours holds canonical strings and the CLI holds ints; the values
                # are what has to agree.
                for stage, offset in ((1, 0), (2, 50), (3, 1234)):
                    self.assertEqual(
                        {int(item) for item in finder.restore_seen_ids(number, stage, offset)},
                        cli.restore_seen_ids(number, stage, offset),
                    )
                    self.assertEqual(
                        finder.build_resume_token(stage, offset, "abcd1234"),
                        cli.build_resume_token(stage, offset, "abcd1234"),
                    )

    def test_the_documented_large_stage_threshold_is_the_cli_threshold(self):
        try:
            cli = self._cli()
        except ImportError as exc:  # pragma: no cover - depends on the environment
            self.skipTest(f"the vendored CLI needs {exc.name}")
        self.assertEqual(finder.LARGE_SEARCH_THRESHOLD, cli.LARGE_SEARCH_THRESHOLD)
        self.assertEqual(finder.AUTO_DEFAULT_MAX_DIGITS, cli.AUTO_DEFAULT_MAX_DIGITS)
        self.assertEqual(finder.BATCH_SIZE, cli.BATCH_SIZE)


TAXON = {"id": 48419, "name": "Amanita", "rank": "genus"}


def _observation(observation_id, login="alan", taxon_id=48419):
    return {
        "id": int(observation_id),
        "place_ids": [],
        "user": {"id": 1, "login": login},
        "taxon": {"id": taxon_id, "name": "Amanita muscaria", "ancestor_ids": [taxon_id]},
    }


def _fake_resolve(mode, term):
    if mode in ("genus", "family"):
        return {"label": f"{term} (taxon ID 48419)", "taxon_id": 48419, "taxon": TAXON}
    if mode == "taxon":
        return {"label": f"Taxon {term}", "taxon_id": int(term), "taxon": TAXON}
    if mode == "user":
        return {"label": term}
    return {"label": "BioBlitz", "project_id": "42"}


class InatFinderAutoModeTests(unittest.TestCase):
    """Bounded auto mode: the stop rule, the budget, and the honesty rules."""

    def _run(self, universe, *, membership=None, fail_observations=None,
             fail_membership=False, resolve=_fake_resolve, **kwargs):
        self.requested = []
        self.membership_calls = []
        calls = {"n": 0}

        def fetch(ids, project_id=None):
            calls["n"] += 1
            if fail_observations and fail_observations(calls["n"]):
                raise InatTreeError("iNaturalist is unreachable")
            if project_id is not None:
                self.membership_calls.append([str(i) for i in ids])
                if fail_membership:
                    raise InatTreeError("membership lookup failed")
                return [universe[str(i)] for i in ids
                        if str(i) in universe and str(i) in (membership or set())]
            self.requested.extend(str(i) for i in ids)
            return [universe[str(i)] for i in ids if str(i) in universe]

        with (
            patch.object(finder, "resolve_criteria", side_effect=resolve),
            patch.object(finder, "_fetch_observations", side_effect=fetch),
            patch.object(finder, "_locations", return_value={}),
        ):
            return finder.find_observations_auto(**kwargs)

    def test_several_clues_may_be_combined(self):
        result = self._run(
            {"123456789": _observation(123456789)},
            observation="123456789",
            clues={"genus": "Amanita", "user": "alan"},
        )
        self.assertEqual([item["kind"] for item in result["criteria"]], ["genus", "user"])
        self.assertEqual(result["status"], "match_found")
        self.assertTrue(result["matches"][0]["score"]["is_full_match"])

    def test_a_stage_zero_full_match_stops_before_any_candidate(self):
        result = self._run(
            {"123456789": _observation(123456789)},
            observation="123456789",
            clues={"genus": "Amanita"},
        )
        self.assertEqual(result["stop_reason"], "full_match")
        self.assertEqual(result["checked_variations"], 0)
        self.assertTrue(result["complete"])
        self.assertEqual(str(result["original"]["id"]), "123456789")

    def test_no_clues_checks_only_the_supplied_number(self):
        result = self._run(
            {"123456789": _observation(123456789)},
            observation="123456789",
            clues={},
        )
        self.assertEqual(result["stop_reason"], "no_clues")
        self.assertEqual(result["checked_variations"], 0)
        self.assertEqual(result["match_count"], 0)
        # It still says what the number really points at.
        self.assertEqual(str(result["original"]["id"]), "123456789")

    def test_a_partial_match_is_kept_and_the_ladder_keeps_widening(self):
        # The number as supplied exists but was observed by someone else, so it
        # matches one clue of two. That is a reason to keep looking, not to stop.
        result = self._run(
            {
                "123456789": _observation(123456789, login="someone-else"),
                "123454589": _observation(123454589),
            },
            observation="123456789",
            clues={"genus": "Amanita", "user": "alan"},
            digits_off=2,
        )
        self.assertEqual(result["status"], "match_found")
        self.assertEqual(result["match_count"], 2)
        # Ranked best-first, and the full match came from stage 2.
        self.assertTrue(result["matches"][0]["score"]["is_full_match"])
        self.assertEqual(str(result["matches"][0]["id"]), "123454589")
        self.assertEqual(result["matches"][0]["stage"], 2)
        self.assertFalse(result["matches"][1]["score"]["is_full_match"])
        self.assertEqual(result["full_match_count"], 1)

    def test_no_observation_id_is_requested_twice_across_stages(self):
        result = self._run({}, observation="123456789",
                           clues={"genus": "Amanita"}, digits_off=2)
        self.assertEqual(len(self.requested), len(set(self.requested)))
        self.assertEqual(result["status"], "no_match")
        self.assertTrue(result["complete"])

    def test_a_large_stage_is_never_started_unasked(self):
        result = self._run({}, observation="123456789",
                           clues={"genus": "Amanita"}, digits_off=3)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["stop_reason"], "large_stage")
        # It was not searched, so this is explicitly not a completed search.
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_stage"]["stage"], 3)
        self.assertEqual(result["next_stage"]["estimated_candidates"], 58968)
        self.assertEqual(result["resume"]["stage"], 3)
        self.assertEqual(result["resume"]["offset"], 0)

    def test_resuming_continues_without_repeating_any_id(self):
        first = self._run({}, observation="123456789",
                          clues={"genus": "Amanita"}, digits_off=3)
        already = set(self.requested)
        second = self._run({}, observation="123456789", clues={"genus": "Amanita"},
                           digits_off=3, resume=first["resume"]["token"],
                           confirm=True, budget=400)
        self.assertEqual(second["status"], "needs_confirmation")
        self.assertEqual(second["stop_reason"], "budget_exhausted")
        self.assertEqual(second["checked_variations"], 400)
        self.assertFalse(already & set(self.requested), "an earlier stage's IDs were re-requested")
        self.assertEqual(len(self.requested), len(set(self.requested)))
        # And the new cursor moves forward rather than repeating itself.
        self.assertGreater(second["resume"]["offset"], first["resume"]["offset"])

    def test_a_cursor_from_another_search_is_refused(self):
        first = self._run({}, observation="123456789",
                          clues={"genus": "Amanita"}, digits_off=3)
        with self.assertRaises(finder.FinderValidationError) as raised:
            self._run({}, observation="123456789", clues={"user": "alan"},
                      digits_off=3, resume=first["resume"]["token"], confirm=True)
        self.assertIn("different search", str(raised.exception))

    def test_a_malformed_cursor_is_refused(self):
        for token in ("nonsense", "v9:1:0:abcd1234", "v1:x:0:abcd1234"):
            with self.subTest(token=token):
                with self.assertRaises(finder.FinderValidationError):
                    self._run({}, observation="123456789",
                              clues={"genus": "Amanita"}, resume=token)

    def test_the_budget_stops_the_request_rather_than_the_search(self):
        result = self._run({}, observation="123456789", clues={"genus": "Amanita"},
                           digits_off=2, budget=400)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertEqual(result["checked_variations"], 400)
        self.assertFalse(result["complete"])
        self.assertIsNotNone(result["resume"])

    def test_a_full_match_stops_inside_a_stage(self):
        # The hit is reachable in stage 1, whose 133 candidates are one batch.
        result = self._run(
            {"123456788": _observation(123456788)},
            observation="123456789", clues={"genus": "Amanita"}, digits_off=3,
        )
        self.assertEqual(result["stop_reason"], "full_match")
        # Stage 2 and the very large stage 3 were never touched.
        self.assertLessEqual(result["checked_variations"], 133)
        self.assertEqual(result["matches"][0]["stage"], 1)

    def test_an_unresolvable_clue_is_reported_and_the_rest_carry_on(self):
        def resolve(mode, term):
            if mode == "genus":
                raise finder.FinderValidationError(
                    f"Genus `{term}` was not found in the iNaturalist taxonomy.",
                    details={"field": "term"},
                )
            return _fake_resolve(mode, term)

        result = self._run({"123456789": _observation(123456789)},
                           observation="123456789", resolve=resolve,
                           clues={"genus": "Amanata", "user": "alan"})
        self.assertEqual([item["kind"] for item in result["criteria"]], ["user"])
        self.assertEqual(result["unusable_clues"][0]["kind"], "genus")
        self.assertIn("was not found", result["unusable_clues"][0]["reason"])
        self.assertEqual(result["status"], "match_found")

    def test_malformed_input_is_still_fatal_in_auto_mode(self):
        with self.assertRaises(finder.FinderValidationError) as raised:
            self._run({}, observation="123456789", clues={"taxon": "abc"},
                      resolve=finder.resolve_criteria)
        self.assertTrue(raised.exception.malformed)

    def test_an_outage_while_resolving_a_clue_is_not_an_unusable_clue(self):
        def resolve(mode, term):
            raise InatTreeError("iNaturalist is unreachable")

        with self.assertRaises(InatTreeError):
            self._run({}, observation="123456789", clues={"genus": "Amanita"}, resolve=resolve)

    def test_a_project_alone_filters_server_side(self):
        with patch.object(finder, "resolve_criteria", side_effect=_fake_resolve):
            resolved = finder.resolve_auto_criteria({"project": "bioblitz"})
        self.assertEqual(resolved["project_id_param"], "42")
        self.assertIsNone(resolved["membership_project_id"])

    def test_a_project_with_another_clue_is_checked_separately(self):
        with patch.object(finder, "resolve_criteria", side_effect=_fake_resolve):
            resolved = finder.resolve_auto_criteria({"genus": "Amanita", "project": "bioblitz"})
        self.assertIsNone(resolved["project_id_param"])
        self.assertEqual(resolved["membership_project_id"], "42")

    def test_a_supplied_but_unusable_clue_still_rules_out_the_filter(self):
        def resolve(mode, term):
            if mode == "genus":
                raise finder.FinderValidationError("nope", details={})
            return _fake_resolve(mode, term)

        with patch.object(finder, "resolve_criteria", side_effect=resolve):
            resolved = finder.resolve_auto_criteria({"genus": "Amanata", "project": "bioblitz"})
        self.assertIsNone(resolved["project_id_param"])
        self.assertEqual(resolved["membership_project_id"], "42")

    def test_failed_project_membership_keeps_other_evidence_and_is_incomplete(self):
        result = self._run(
            {"123456789": _observation(123456789)},
            observation="123456789",
            clues={"genus": "Amanita", "project": "bioblitz"},
            fail_membership=True,
            digits_off=1,
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["complete"])
        # The genus evidence survived; only the project is unknown.
        self.assertEqual(result["original_score"]["matched"], ["genus"])
        self.assertEqual(result["original_score"]["unknown"], ["project"])
        self.assertFalse(result["original_score"]["is_full_match"])
        # A search with an unanswered question is never handed a cursor.
        self.assertIsNone(result["resume"])

    def test_the_original_and_the_matches_report_the_same_score_shape(self):
        """Both are documented as InaturalistFinderScore, so both must match it."""
        result = self._run(
            {
                "123456789": _observation(123456789, login="someone-else"),
                "123456788": _observation(123456788),
            },
            observation="123456789",
            clues={"genus": "Amanita", "user": "alan"},
        )
        expected = {"matched", "unknown", "matched_count", "unknown_count", "total", "is_full_match"}
        self.assertEqual(set(result["original_score"]), expected)
        for match in result["matches"]:
            self.assertEqual(set(match["score"]), expected)
        self.assertEqual(result["original_score"]["matched_count"], 1)
        self.assertEqual(result["original_score"]["total"], 2)
        self.assertFalse(result["original_score"]["is_full_match"])

    def test_a_small_stage_runs_to_the_end_instead_of_stopping_at_its_first_hit(self):
        """1.8.1's fix, and the reason it mattered.

        iNaturalist numbers observations in upload order, so the numbers either
        side of a mistyped one very often share an uploader. With a single clue
        that coincidence satisfies a full match, and the stage used to abort on it
        while the observation actually wanted sat further down the same stage.
        """
        result = self._run(
            {
                "123456788": _observation(123456788),
                "123456709": _observation(123456709),
            },
            observation="123456789",
            clues={"user": "alan"},
            digits_off=1,
        )
        # Stage 1 of a nine-digit number is 133 candidates; all of them ran.
        self.assertEqual(result["stages"][1]["attempted"], 133)
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["full_match_count"], 2)

    def test_a_single_clue_matching_several_neighbours_says_so(self):
        result = self._run(
            {
                "123456788": _observation(123456788),
                "123456709": _observation(123456709),
            },
            observation="123456789",
            clues={"user": "alan"},
            digits_off=1,
        )
        self.assertTrue(result["notices"])
        self.assertIn("nearby observations all match the only clue", result["notices"][0])
        # Not raised when the clues can actually separate them.
        quiet = self._run(
            {"123456788": _observation(123456788)},
            observation="123456789", clues={"user": "alan"}, digits_off=1,
        )
        self.assertEqual(quiet["notices"], [])

    def test_a_large_stage_still_stops_at_the_batch_that_matched(self):
        """Finishing one of those costs minutes rather than seconds.

        Stage 2 of a nine-digit number is 2,836 candidates, which is below the
        shipped threshold and so runs to the end. The threshold is lowered here
        rather than driving 295 fake batches through stage 3 to reach a genuinely
        large one.
        """
        universe = {"123454589": _observation(123454589)}
        with patch.object(finder, "EARLY_STOP_MIN_CANDIDATES", 200):
            result = self._run(
                universe, observation="123456789", clues={"genus": "Amanita"},
                digits_off=2, confirm=True,
            )
        self.assertEqual(result["stop_reason"], "full_match")
        self.assertLess(result["stages"][2]["attempted"], 2836)

    def test_the_early_stop_threshold_is_the_one_the_cli_ships(self):
        # Stage 2 of a nine-digit number sits below it, which is exactly why the
        # common case now runs to completion.
        self.assertEqual(finder.EARLY_STOP_MIN_CANDIDATES, 5_000)
        self.assertLess(finder.build_candidate_plan("123456789", 2).total, finder.EARLY_STOP_MIN_CANDIDATES)

    def test_ties_break_by_stage_then_distance_from_the_number_supplied(self):
        ranked = finder._rank_auto_matches(
            [
                {"observation": _observation(500), "matched": ["user"], "unknown": [], "stage": 2},
                {"observation": _observation(300), "matched": ["user"], "unknown": [], "stage": 1},
                {"observation": _observation(120), "matched": ["user"], "unknown": [], "stage": 1},
                {"observation": _observation(100), "matched": ["user", "genus"], "unknown": [], "stage": 3},
            ],
            "110",
            [{"kind": "user"}, {"kind": "genus"}],
        )
        self.assertEqual(
            [str(item["id"]) for item in ranked],
            # Score first; then the lower stage; then the nearer number.
            ["100", "120", "300", "500"],
        )

    def test_the_supplied_number_sorts_first_when_it_matched(self):
        result = self._run(
            {
                "123456789": _observation(123456789),
                "123456788": _observation(123456788),
            },
            observation="123456789", clues={"user": "alan"}, digits_off=1,
        )
        self.assertEqual(str(result["matches"][0]["id"]), "123456789")
        self.assertTrue(result["matches"][0]["is_original"])

    def test_an_outage_leaves_the_search_incomplete_not_empty(self):
        result = self._run({}, observation="123456789", clues={"genus": "Amanita"},
                           digits_off=1, fail_observations=lambda call: call > 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["complete"])
        self.assertGreater(result["unchecked_variations"], 0)
        self.assertIsNone(result["resume"])


class InatFinderAutoRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def _post(self, payload):
        with (
            self.app.test_request_context(method="POST", json=payload),
            patch("app.services.inat_finder_service.find_observations_auto",
                  return_value={"status": "no_match"}) as auto,
            patch("app.services.inat_finder_service.find_observations",
                  return_value={"complete": True}) as legacy,
        ):
            g.api_user = SimpleNamespace(id=7)
            g.api_token = SimpleNamespace(id=3)
            response = _undecorated(v1.tools_inaturalist_finder)()
        return response, auto, legacy

    def test_clue_fields_run_the_automatic_search(self):
        response, auto, legacy = self._post({"observation": "123456789", "genus": "Amanita"})
        self.assertEqual(response.status_code, 200)
        auto.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(auto.call_args.kwargs["clues"]["genus"], "Amanita")
        # 1.8.0's auto default, not the single-criterion default of 1.
        self.assertEqual(auto.call_args.kwargs["digits_off"], 3)

    def test_no_clues_at_all_still_runs_the_automatic_search(self):
        response, auto, legacy = self._post({"observation": "123456789"})
        self.assertEqual(response.status_code, 200)
        auto.assert_called_once()
        legacy.assert_not_called()

    def test_mode_and_term_still_run_the_single_criterion_search(self):
        response, auto, legacy = self._post(
            {"observation": "123456789", "mode": "genus", "term": "Amanita"}
        )
        self.assertEqual(response.status_code, 200)
        legacy.assert_called_once()
        auto.assert_not_called()
        # The historical default is preserved for this path.
        self.assertEqual(legacy.call_args.kwargs["digits_off"], 1)

    def test_mixing_the_two_shapes_is_refused(self):
        response, auto, legacy = self._post(
            {"observation": "123456789", "mode": "genus", "term": "Amanita", "user": "alan"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "validation_failed")
        auto.assert_not_called()
        legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
