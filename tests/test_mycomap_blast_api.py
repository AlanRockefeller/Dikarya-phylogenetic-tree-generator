"""MycoMap.org search uses the ordinary queue import and background polling."""

from copy import deepcopy

from flask import Flask, g
import pytest

from app.api import routes
from app.extensions import limiter
from app.services import background_requests, mycomap_org_service as org


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.config.update(SECRET_KEY="test", RATELIMIT_ENABLED=False,
                      RATELIMIT_STORAGE_URI="memory://")
    limiter.init_app(app)
    app.register_blueprint(routes.bp, url_prefix="/api")

    @app.before_request
    def request_context():
        g.request_id = "test-search"

    monkeypatch.setattr(routes, "_fill_missing_genbank_locations", lambda *a, **k: 0)
    monkeypatch.setattr("app.services.inaturalist_places.fill_missing_inat_locations",
                        lambda *a, **k: 0)
    return app.test_client()


def search_result(sequences=None):
    return {
        "sequences": sequences or [], "ncbi_count": 0,
        "local_count": len(sequences or []), "errors": [],
        "failed_sources": [], "pending_sources": [],
        "ncbi_queue_position": None, "metrics_by_key": {},
        "result_id": "123", "result_url": "https://mycomap.org/mycoblast/123",
        "warnings": [],
    }


def test_search_reuses_import_filters_and_preserves_metrics(client, monkeypatch):
    hit = {
        "name": "MycoMap#1234 Example fungus", "sequence": "ACGT" * 30,
        "source": "mycomap", "hit_source": "local", "taxon": "Example fungus",
        "location": "California US", "mycomap_sequence_id": "1234",
        "identity": 97.5, "query_cover": 95, "subject_cover": 98,
        "blast_metrics_available": True,
    }
    contaminant = dict(hit, name="MycoMap#1235 contaminant", is_contaminant=True)
    seen = {}

    def search(query, **kwargs):
        seen.update(query=query, **kwargs)
        return search_result(deepcopy([hit, contaminant]))

    monkeypatch.setattr(org, "search_from_query", search)
    monkeypatch.setattr(org, "fetch_results", lambda *a, **k: pytest.fail("duplicate fetch"))
    response = client.post("/api/mycomap/blast", json={
        "query": "ACGT" * 15, "min_identity": "95", "max_sequences": "100",
    })
    assert response.status_code == 200
    data = response.get_json()
    assert seen == {"query": "ACGT" * 15, "min_identity": 95,
                    "max_sequences": 100, "time_budget": 200}
    assert data["mycomap_blast_url"] == "https://mycomap.org/mycoblast/123"
    assert len(data["sequences"]) == 1
    assert data["sequences"][0]["source"] == "mycomap"
    assert data["sequences"][0]["identity"] == 97.5
    assert data["sequences"][0]["mycomap_sequence_id"] == "1234"
    assert "_org_metric" not in data["sequences"][0]
    assert data["import_filter_details"]["mycomap"]["counts"]["contaminant"] == 1


def test_completed_search_with_no_hits_is_success(client, monkeypatch):
    monkeypatch.setattr(org, "search_from_query", lambda *a, **k: search_result())
    response = client.post("/api/mycomap/blast", json={"query": "OR807397"})
    assert response.status_code == 200
    assert response.get_json()["sequences"] == []


@pytest.mark.parametrize("data", [[], None, {"query": 5}, {"query": " "},
    {"query": "OR807397", "min_identity": "nan"},
    {"query": "OR807397", "min_identity": "inf"},
    {"query": "OR807397", "max_sequences": "many"}])
def test_bad_request_does_not_start_a_search(client, monkeypatch, data):
    monkeypatch.setattr(org, "search_from_query", lambda *a, **k: pytest.fail("invalid query"))
    assert client.post("/api/mycomap/blast", json=data).status_code == 400


@pytest.mark.parametrize("error,status", [
    (ValueError("Only one query sequence is allowed."), 400),
    (org.OrgResultError("MycoMap.org refused the request.", 502, retryable=False), 502),
    (TimeoutError(), 504),
])
def test_search_errors_are_actionable_json(client, monkeypatch, error, status):
    def fail(*a, **k):
        raise error

    monkeypatch.setattr(org, "search_from_query", fail)
    response = client.post("/api/mycomap/blast", json={"query": "OR807397"})
    assert response.status_code == status
    assert response.get_json()["status"] == "error"
    assert response.get_json()["error"]


def test_search_uses_session_owned_background_protocol(client, monkeypatch):
    seen = {}

    def start(kind, handler, data):
        seen.update(kind=kind, handler=handler, data=data)
        return "a-background-task-token"

    monkeypatch.setattr(background_requests, "start", start)
    monkeypatch.setattr(org, "search_from_query", lambda *a, **k: pytest.fail("inline search"))
    response = client.post("/api/mycomap/blast", json={"query": "OR807397"},
                           headers={"X-Dikarya-Background": "1"})
    assert response.status_code == 202
    assert response.get_json()["task_id"] == "a-background-task-token"
    assert seen["kind"] == "mycomap_blast"
    assert seen["handler"] is routes._run_mycomap_blast_request
