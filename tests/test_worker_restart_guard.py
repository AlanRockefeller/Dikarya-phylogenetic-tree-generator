import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch, mock_open

import pytest


def load_guard():
    loader = importlib.machinery.SourceFileLoader("restart_guard", str(Path(__file__).parents[1] / "scripts/restart-dikarya-worker"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_busy_agent_gets_report_without_restart():
    guard = load_guard()
    with patch.object(guard.sys, "argv", ["restart-dikarya-worker"]), patch.object(guard.os, "geteuid", return_value=0), patch.object(guard, "run", side_effect=["mixed", "infinity"]), patch("builtins.open", mock_open(read_data="DATABASE_URL=postgresql://example\n")), patch.object(guard.subprocess, "check_output", return_value=json.dumps({"jobs": [{"id": "job-a"}]})), patch.object(guard.subprocess, "run") as mutate, patch.object(guard.sys, "stdin", io.StringIO("")), patch("select.select", return_value=([], [], [])):
        assert guard.main() == 75
        mutate.assert_not_called()


def test_unsafe_shutdown_config_refuses_even_idle_restart():
    guard = load_guard()
    with patch.object(guard.sys, "argv", ["restart-dikarya-worker"]), patch.object(guard.os, "geteuid", return_value=0), patch.object(guard, "run", side_effect=["control-group", "90s"]), patch.object(guard.subprocess, "run") as mutate:
        assert guard.main() == 78
        mutate.assert_not_called()


def test_bulk_entry_point_targets_only_bulk_service_and_queue():
    guard = load_guard()
    with patch.object(guard.sys, "argv", ["restart-dikarya-worker-bulk"]), patch.object(guard.os, "geteuid", return_value=0), patch.object(guard, "run", side_effect=["mixed", "infinity"]) as inspect, patch("builtins.open", mock_open(read_data="DATABASE_URL=postgresql://example\n")), patch.object(guard.subprocess, "check_output", return_value=json.dumps({"jobs": []})) as report, patch.object(guard.subprocess, "run") as mutate:
        assert guard.main(bulk=True) == 0
        assert all("dikarya-worker-bulk.service" in call.args for call in inspect.call_args_list)
        assert report.call_args.args[0][-1] == "phylo_bulk"
        mutate.assert_called_once_with(["/usr/bin/systemctl", "restart", "--no-block", "dikarya-worker-bulk.service"], check=True)


def test_changed_job_set_cancels_approved_interrupt_and_thaws():
    guard = load_guard()
    reports = [json.dumps({"jobs": [{"id": name}]}) for name in ("job-a", "job-b")]
    stream = io.StringIO("INTERRUPT job-a\n")
    with patch.object(guard.sys, "argv", ["restart-dikarya-worker"]), patch.object(guard.os, "geteuid", return_value=0), patch.object(guard, "run", side_effect=["mixed", "infinity"]), patch("builtins.open", mock_open(read_data="DATABASE_URL=postgresql://example\n")), patch.object(guard.subprocess, "check_output", side_effect=reports), patch.object(guard.subprocess, "run") as mutate, patch.object(guard.sys, "stdin", stream), patch("select.select", return_value=([stream], [], [])):
        assert guard.main() == 75
        assert [call.args[0][1] for call in mutate.call_args_list] == ["freeze", "thaw"]


@pytest.fixture
def graceful_guard(monkeypatch, tmp_path):
    guard = load_guard()
    monkeypatch.setattr(guard.sys, "argv", ["restart-dikarya-worker-bulk-when-idle"])
    monkeypatch.setattr(guard.os, "geteuid", lambda: 0)
    real_open = guard.os.open
    monkeypatch.setattr(guard.os, "open", lambda path, flags, mode: real_open(
        tmp_path / "restart.lock", flags, mode))
    state = {
        "LoadState": "loaded", "ActiveState": "active", "SubState": "running",
        "MainPID": "123", "Job": "", "KillMode": "mixed", "KillSignal": "15",
        "TimeoutStopUSec": "infinity", "ExecStop": "",
    }

    def inspect(*args):
        assert args[:3] == ("/usr/bin/systemctl", "show", "dikarya-worker-bulk.service")
        if "ExecStop" in args:
            return state.get("ExecStop", "")
        return "\n".join(f"{key}={value}" for key, value in state.items() if key != "ExecStop")

    monkeypatch.setattr(guard, "run", inspect)
    with patch.object(guard.subprocess, "run") as execute:
        execute.return_value.returncode = 0
        yield guard, state, execute


@pytest.mark.parametrize("rq_busy", [True, False])
def test_graceful_restart_delegates_busy_or_idle_wait_to_systemd(graceful_guard, rq_busy, capsys):
    guard, state, execute = graceful_guard
    # RQ's job state is deliberately not polled: systemd's parent-only SIGTERM
    # is safe whether a job is busy, idle, or begins during the request.
    with patch.object(guard.subprocess, "check_output", return_value=json.dumps({
        "jobs": [{"id": "running-job"}] if rq_busy else [],
    })) as report:
        assert guard.main(bulk=True, after_current=True) == 0
        report.assert_not_called()
    calls = execute.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == [
        "/usr/sbin/runuser", "-u", "dikarya", "--",
        "/var/www/dikarya/scripts/dikarya-preflight",
    ]
    assert calls[0].kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert calls[1].args[0] == [
        "/usr/bin/systemctl", "restart", "--no-block", "--job-mode=fail",
        "dikarya-worker-bulk.service",
    ]
    output = capsys.readouterr().out
    assert "restart completion is still pending" in output
    assert "Previous MainPID: 123" in output


@pytest.mark.parametrize("key,value", [
    ("KillMode", "control-group"), ("KillSignal", "9"),
    ("TimeoutStopUSec", "90s"), ("ExecStop", "/bin/kill"),
    ("LoadState", "not-found"),
])
def test_graceful_restart_refuses_unsafe_settings(graceful_guard, key, value):
    guard, state, execute = graceful_guard
    state[key] = value
    with pytest.raises(ValueError):
        guard.main(bulk=True, after_current=True)
    execute.assert_not_called()


def test_graceful_restart_refuses_incomplete_settings(graceful_guard):
    guard, state, execute = graceful_guard
    del state["KillSignal"]
    with pytest.raises(ValueError):
        guard.main(bulk=True, after_current=True)
    execute.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"Job": "42"}, {"ActiveState": "deactivating"}, {"ActiveState": "activating"},
])
def test_graceful_restart_leaves_pending_operation_alone(graceful_guard, changes):
    guard, state, execute = graceful_guard
    state.update(changes)
    assert guard.main(bulk=True, after_current=True) == 0
    execute.assert_not_called()


def test_graceful_restart_rechecks_state_after_preflight(graceful_guard):
    guard, state, execute = graceful_guard

    def preflight(*args, **kwargs):
        state["Job"] = "42"
        return guard.subprocess.CompletedProcess(args, 0)

    execute.side_effect = preflight
    assert guard.main(bulk=True, after_current=True) == 0
    assert execute.call_count == 1


def test_graceful_restart_repeated_request_does_not_send_another_signal(graceful_guard):
    guard, state, execute = graceful_guard
    assert guard.main(bulk=True, after_current=True) == 0
    state.update(Job="42", ActiveState="deactivating")
    assert guard.main(bulk=True, after_current=True) == 0
    assert execute.call_count == 2  # Only the first request's preflight/restart.


def test_graceful_restart_serializes_concurrent_requests(graceful_guard):
    guard, state, execute = graceful_guard
    fd = guard.os.open("unused", guard.os.O_CREAT | guard.os.O_RDWR, 0o600)
    with guard.os.fdopen(fd, "w") as lock:
        guard.fcntl.flock(lock, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        assert guard.main(bulk=True, after_current=True) == 75
    execute.assert_not_called()


def test_graceful_restart_stops_on_preflight_failure(graceful_guard):
    guard, state, execute = graceful_guard
    execute.return_value.returncode = 1
    assert guard.main(bulk=True, after_current=True) == 78
    assert execute.call_count == 1


def test_graceful_restart_reports_systemd_failure(graceful_guard, capsys):
    guard, state, execute = graceful_guard
    execute.side_effect = [guard.subprocess.CompletedProcess([], 0),
                           guard.subprocess.CompletedProcess([], 1)]
    assert guard.main(bulk=True, after_current=True) == 70
    assert "Bulk restart scheduled" not in capsys.readouterr().out


def test_graceful_restart_is_bulk_only_and_accepts_no_arguments(graceful_guard):
    guard, state, execute = graceful_guard
    assert guard.main(bulk=False, after_current=True) == 64
    guard.sys.argv.append("--force")
    assert guard.main(bulk=True, after_current=True) == 64
    execute.assert_not_called()
