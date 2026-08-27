"""The public API's integer fields are integers, on both entry points.

`_validate_clamped_int()` used a bare `int()`, which accepts three shapes the
OpenAPI schema calls invalid and turns each into a different number: `1000.5`
became 1000, the string `"1000"` became 1000, and `True` became 1. A caller who
asked for 1000.5 bootstrap replicates got a tree built with 1000 and no
indication the request had been altered.

The fields are exercised through the real `POST /api/v1/jobs` and
`POST /api/v1/jobs/<id>/recompute` handlers rather than against the helper, so
a field that stops routing through the shared validator is caught here.
"""

import inspect
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, g

from app.api_v1 import routes as v1
from app.config import Config

JOB_ID = "6f7f0f8f-1d3a-4a26-9f2f-2f1c4b8d7a55"
FASTA = ">Sample_A\nACGTACGTACGTACGTACGT\n>Sample_B\nACGTACGTACGTACGTACGA\n"

# Every field that reaches `_validate_clamped_int`, with an in-range integer
# and a value one step outside the documented bounds.
INTEGER_FIELDS = {
    "bootstrap": (1000, 10_001),
    "alrt_replicates": (1000, 10_001),
    "mcmc_generations": (2_000_000, 999),
    "mcmc_nruns": (2, 9),
    "mcmc_nchains": (4, 17),
}

# Values `type: integer` excludes. `1000.0` is deliberately absent: JSON has one
# number type, so an integer-valued float is how many clients spell an integer.
NON_INTEGERS = [1000.5, "1000", True, False, float("nan"), float("inf"), [1000], None]


def _undecorated(view):
    return inspect.unwrap(view)


def _create(payload):
    """Run POST /api/v1/jobs with everything past validation stubbed out."""
    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json=payload),
        patch.object(v1, "enqueue_job", side_effect=lambda *a, **kw: kw.get("job_id")),
        patch.object(v1, "Job", side_effect=lambda **kw: SimpleNamespace(**kw)),
        patch.object(v1, "db"),
        patch.object(v1, "serialize_job", side_effect=lambda job: {"id": job.id}),
    ):
        g.api_user = SimpleNamespace(id=7)
        g.api_token = SimpleNamespace(id=3)
        return _undecorated(v1.create_job)()


def _recompute(tmp_path, overrides, *, stored=None):
    """Run POST /api/v1/jobs/<id>/recompute against a stored parameter set."""
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "input_info.json").write_text(
        json.dumps(stored or {"tree_method": "raxml"})
    )
    job = SimpleNamespace(status="completed", metrics={})

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json=overrides),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(v1, "get_owned_job_or_404", return_value=job),
        patch.object(v1, "enqueue_recompute_job", return_value=("rq-job", True)),
        patch.object(v1, "url_for", return_value="/api/v1/job"),
        patch.object(v1, "db"),
    ):
        return _undecorated(v1.recompute_job)(JOB_ID)


def _body(response):
    return response.get_json() if hasattr(response, "get_json") else response[0].get_json()


def _status(response):
    return response.status_code if hasattr(response, "status_code") else response[1]


@pytest.mark.parametrize("field", sorted(INTEGER_FIELDS))
@pytest.mark.parametrize("value", NON_INTEGERS[:-1])  # None is the "missing" case
def test_create_rejects_a_non_integer(field, value):
    response = _create(
        {"input_type": "pasted_sequence", "sequence": FASTA, field: value}
    )
    assert _status(response) == 422, f"{field}={value!r} was accepted"
    body = _body(response)
    assert body["error"]["details"]["field"] == field


@pytest.mark.parametrize("field", sorted(INTEGER_FIELDS))
@pytest.mark.parametrize("value", NON_INTEGERS[:-1])
def test_recompute_rejects_a_non_integer(tmp_path, field, value):
    response = _recompute(tmp_path, {field: value})
    assert _status(response) == 422, f"{field}={value!r} was accepted"
    assert _body(response)["error"]["details"]["field"] == field


@pytest.mark.parametrize("field", sorted(INTEGER_FIELDS))
def test_an_in_range_integer_is_still_accepted(tmp_path, field):
    good = INTEGER_FIELDS[field][0]
    assert _status(_create(
        {"input_type": "pasted_sequence", "sequence": FASTA, field: good})) == 202
    assert _status(_recompute(tmp_path, {field: good})) == 202


@pytest.mark.parametrize("field", sorted(INTEGER_FIELDS))
def test_an_integer_valued_float_is_accepted(tmp_path, field):
    """JSON's single number type means 1000.0 is how some clients write 1000."""
    good = float(INTEGER_FIELDS[field][0])
    assert _status(_create(
        {"input_type": "pasted_sequence", "sequence": FASTA, field: good})) == 202
    assert _status(_recompute(tmp_path, {field: good})) == 202


@pytest.mark.parametrize("field", sorted(INTEGER_FIELDS))
def test_an_out_of_range_integer_is_still_a_422(tmp_path, field):
    bad = INTEGER_FIELDS[field][1]
    response = _create(
        {"input_type": "pasted_sequence", "sequence": FASTA, field: bad})
    assert _status(response) == 422
    assert "outside the allowed range" in _body(response)["error"]["message"]
    assert _status(_recompute(tmp_path, {field: bad})) == 422


@pytest.mark.parametrize("field", sorted(INTEGER_FIELDS))
def test_a_missing_field_still_takes_the_default(tmp_path, field):
    """None means "not supplied"; the default applies and nothing 422s."""
    assert _status(_create(
        {"input_type": "pasted_sequence", "sequence": FASTA, field: None})) == 202
    # Recompute only looks at fields actually present in the body, and an
    # explicit null is present -- it must still fall through to the default
    # rather than being read as a non-integer.
    assert _status(_recompute(tmp_path, {field: None})) == 202


def test_bootstrap_is_strict_even_on_the_iqtree_path(tmp_path):
    """The IQ-TREE normalizer runs first and accepts stored string forms.

    `validate_iqtree_ufboot_count()` deliberately parses `"1000"`, because jobs
    on disk carry values written before the field was typed. That leniency is
    for inherited values only: what the caller sends is screened before it gets
    there, or the public API would be strict for RAxML and lenient for IQ-TREE.
    """
    for value in ("1000", True, 1000.5):
        response = _create({
            "input_type": "pasted_sequence", "sequence": FASTA,
            "tree_method": "iqtree", "bootstrap": value,
        })
        assert _status(response) == 422, f"iqtree bootstrap={value!r} was accepted"
        assert _body(response)["error"]["details"]["field"] == "bootstrap"

        response = _recompute(
            tmp_path, {"tree_method": "iqtree", "bootstrap": value})
        assert _status(response) == 422, f"iqtree bootstrap={value!r} was accepted"


def test_an_inherited_bootstrap_is_still_normalized_not_rejected(tmp_path):
    """A value the caller never mentioned keeps its lenient path.

    Recompute re-validates the whole stored set. Screening an *inherited*
    string the way a submitted one is screened would make every job that
    predates the typed field permanently unrecomputable.
    """
    response = _recompute(
        tmp_path, {"notes": "unrelated"},
        stored={"tree_method": "iqtree", "bootstrap": 500},
    )
    assert _status(response) == 202


def test_the_error_names_the_type_it_received():
    response = _create(
        {"input_type": "pasted_sequence", "sequence": FASTA, "mcmc_nruns": "2"})
    message = _body(response)["error"]["message"]
    assert "must be an integer" in message
    assert "str" in message


def test_the_openapi_schema_still_says_integer():
    """The runtime was brought up to the documentation, not the reverse."""
    spec = v1.build_spec()
    schemas = spec["components"]["schemas"]
    for schema_name in ("CreateJobRequest", "RecomputeRequest"):
        for field in INTEGER_FIELDS:
            assert schemas[schema_name]["properties"][field]["type"] == "integer"
