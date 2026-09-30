"""A clean install can mark type tips before the first scheduled refresh."""

import json

from app.services import type_specimen_service as tss


def test_bundled_snapshot_is_used_until_live_snapshot_exists(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps({"specimens": [
        {"id": 1, "accessionNumber": "PX215422.1",
         "typeMaterial": "Holotype of Example fungus", "organism": "Example fungus"},
    ]}))
    monkeypatch.setattr(tss, "BUNDLED_MYCOMAP_PATH", bundled)

    assert tss.mycomap_index()["PX215422"]["status"] == "holotype"

    live = tss.DATA_DIR / tss.MYCOMAP_SNAPSHOT_NAME
    live.write_text(json.dumps({"records": {"PX215422": {"status": "epitype"}}}))
    assert tss.mycomap_index()["PX215422"]["status"] == "epitype"
