"""Periodic rotation for the Gunicorn access and error logs."""

import subprocess
from pathlib import Path


LOGROTATE_BINARY = Path("/usr/sbin/logrotate")
LOGROTATE_TIMEOUT_SECONDS = 30


def rotate_runtime_logs(app) -> bool:
    """Run the repository-owned logrotate policy without stopping Gunicorn."""
    project_root = Path(app.root_path).resolve().parent
    config_path = project_root / "ops" / "logrotate" / "dikarya"
    state_path = project_root / "var" / "logs" / ".logrotate.status"

    if not LOGROTATE_BINARY.is_file():
        app.logger.warning("Log rotation skipped: %s is unavailable", LOGROTATE_BINARY)
        return False
    if not config_path.is_file():
        app.logger.warning("Log rotation skipped: config is missing at %s", config_path)
        return False

    try:
        result = subprocess.run(
            [
                str(LOGROTATE_BINARY),
                "--state",
                str(state_path),
                str(config_path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=LOGROTATE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.warning("Log rotation failed to run: %s", exc)
        return False

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        app.logger.warning("Log rotation failed: %s", detail[:1000])
        return False
    return True
