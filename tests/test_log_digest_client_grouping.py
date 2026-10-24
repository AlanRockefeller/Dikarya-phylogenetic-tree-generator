from datetime import datetime, timedelta

from scripts import dikarya_log_digest as digest


def test_client_fetch_reports_group_across_job_ids_and_fingerprints(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "LOG_DIR", tmp_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    for index, job in enumerate(("9raa", "abcd", "6682d309-0a8b-47eb-8976-23024071453a")):
        lines.append(
            f"[{timestamp}] [ERROR] event=client.ui_action_failed Browser failure "
            f"pathname=/job/{job}/view job_id={job} "
            f"action=network:/api/job/{job}/tree/state message=Failed to fetch "
            f"fingerprint={index:016x} release=git:abc duration_ms={index} "
            f"[req=request{index} user=anon]\n"
        )
    # Different actions and failure messages must remain distinct findings.
    lines.append(lines[0].replace("/tree/state", "/tree/undo").replace("request0", "request3"))
    lines.append(lines[0].replace("Failed to fetch", "HTTP 403").replace("request0", "request4"))
    (tmp_path / "errors.log").write_text("".join(lines))
    findings = digest.analyze_errors(datetime.now() - timedelta(hours=1))
    assert sorted(findings["exceptions"].values()) == [1, 1, 3]
    key = next(key for key, count in findings["exceptions"].items() if count == 3)
    assert "/job/<id>/view" in key
    assert "/api/job/<id>/tree/state" in key
    assert "fingerprint" not in key
