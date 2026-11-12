"""The tree viewer's Refresh MycoMap route resolves tips by saved provenance."""

import json
from types import SimpleNamespace

import pytest
from flask import Flask

from app.api import routes
from app.config import Config
from app.extensions import limiter
from app.services import mycomap_service, tree_edit_service

GENBANK_MO_TIP = "MO123456 Amanita muscaria GenBank record"
INAT_TIP = "iNat154644100 Podaxis inyoensis Death Valley"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    job_id = "mo1x"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text(json.dumps({"sequence_metadata": [
        # An unversioned MO123456 whose saved source says GenBank: an
        # accession, not Mushroom Observer observation 123456.
        {"name": GENBANK_MO_TIP, "source": "genbank", "taxon": "Amanita muscaria"},
        {"name": INAT_TIP, "source": "inaturalist", "taxon": "Podaxis inyoensis"},
    ]}))
    state = {
        "tree_structure": {"children": [{"name": GENBANK_MO_TIP}, {"name": INAT_TIP}]},
        "renames": {},
    }
    calls = []

    def refresh(references):
        calls.append(list(references))
        return {"references": references, "before": {}, "after": {},
                "warnings": [], "not_refreshed": [], "message": ""}

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test", RATELIMIT_ENABLED=False,
                      RATELIMIT_STORAGE_URI="memory://")
    limiter.init_app(app)
    app.register_blueprint(routes.bp, url_prefix="/api")
    monkeypatch.setattr(Config, "JOB_DIR", tmp_path)
    monkeypatch.setattr(routes, "check_job_access",
                        lambda *a, **k: (SimpleNamespace(status="completed"), None, None))
    monkeypatch.setattr(tree_edit_service, "load_tree_state", lambda *_a, **_k: state)
    monkeypatch.setattr(mycomap_service, "refresh_mycomap_observation_records", refresh)
    return app.test_client(), job_id, calls


def test_genbank_compact_mo_tip_never_refreshes_an_observation(setup):
    client, job_id, calls = setup
    response = client.post(f"/api/job/{job_id}/tree/refresh-mycomap-records",
                           json={"tip_names": [GENBANK_MO_TIP]})
    assert response.status_code == 422
    assert calls == []


def test_genbank_compact_mo_tip_is_left_out_of_a_mixed_refresh(setup):
    client, job_id, calls = setup
    response = client.post(f"/api/job/{job_id}/tree/refresh-mycomap-records",
                           json={"tip_names": [GENBANK_MO_TIP, INAT_TIP]})
    assert response.status_code == 200, response.get_json()
    assert calls == [["inat:154644100"]]
    assert response.get_json()["refreshed_count"] == 1
