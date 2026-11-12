"""When ActorScorer.record() reports an escalation."""

from app.services import security_actors as sa

ACTOR = "203.0.113.0/24"


def _tokens(start, stop):
    return [("job_enumeration", f"job{n}") for n in range(start, stop)]


def _scorer(monkeypatch, redis_answer):
    scorer = sa.ActorScorer(lambda: None)
    monkeypatch.setattr(scorer, "_redis_record", lambda actor, adds: redis_answer)
    monkeypatch.setattr(scorer, "_claim_alert", lambda actor: True)
    return scorer


def test_a_crossing_made_of_tokens_held_during_a_redis_outage_still_alerts(monkeypatch):
    # Eight tokens reached only the per-process fallback while Redis was down.
    scorer = _scorer(monkeypatch, ({"job_enumeration": 1}, {}))
    scorer.fallback.record(ACTOR, _tokens(0, 8))
    assert sa.score_signals({"job_enumeration": 8}) < scorer.threshold
    # After recovery Redis holds only the ninth, so its own score is flat;
    # the merged counts cross the threshold and that must alert.
    escalation = scorer.record(ACTOR, _tokens(8, 9))
    assert escalation is not None
    assert escalation.counts["job_enumeration"] == 9


def test_an_actor_already_over_the_line_is_not_re_escalated_by_a_flat_request(monkeypatch):
    held = {"job_enumeration": 12}
    # Redis answers with the same counts before and after: nothing was raised.
    scorer = _scorer(monkeypatch, (dict(held), dict(held)))
    scorer.fallback.record(ACTOR, _tokens(0, 12))
    assert sa.score_signals(held) >= scorer.threshold
    # A repeat of a token already counted raises neither store.
    assert scorer.record(ACTOR, _tokens(0, 1)) is None


def test_a_redis_side_rise_alerts_even_when_the_fallback_already_had_more(monkeypatch):
    scorer = _scorer(monkeypatch, ({"job_enumeration": 10}, {"job_enumeration": 9}))
    scorer.fallback.record(ACTOR, _tokens(0, 12))
    assert scorer.record(ACTOR, _tokens(20, 21)) is not None
