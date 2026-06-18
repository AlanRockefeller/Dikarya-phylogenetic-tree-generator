#!/usr/bin/env python3
"""Add or list What's New changelog entries from the command line.

This is a thin wrapper around the WhatsNewEntry SQLAlchemy model that
sidesteps `flask whats-new-add`. The Flask CLI path fails for users who
can't read the production env file (`/etc/dikarya/web.env` is mode 0700
nobody:nogroup) because `create_app()` rejects boot without SECRET_KEY.

This script loads DATABASE_URL from a tree-user-accessible location and
stubs a CLI-only SECRET_KEY so the validator passes. It never starts a
web server, never serves requests, and the stub key is not used to sign
anything — it just lets create_app() succeed for the duration of the
DB write.

Env resolution order (first hit wins):
  1. Existing process environment.
  2. $DIKARYA_ENV_FILE if set.
  3. /var/www/dikarya/.env if present.
  4. ~/.dikarya/env if present.

Usage:
  scripts/dikarya_whats_new.py add --title "…" --body "…" --category feature
  scripts/dikarya_whats_new.py list
"""
import argparse
import os
import secrets
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _reexec_in_project_venv() -> None:
    """Use the project virtualenv when the script is launched directly."""
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if (
        venv_python.exists()
        and Path(sys.executable).absolute() != venv_python.absolute()
        and os.environ.get("DIKARYA_WHATS_NEW_VENV_REEXEC") != "1"
    ):
        os.environ["DIKARYA_WHATS_NEW_VENV_REEXEC"] = "1"
        os.execv(str(venv_python), [str(venv_python), *sys.argv])


def _load_env_file_fallback(path: Path) -> None:
    """Load simple KEY=value lines when python-dotenv is unavailable."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip("'\"")
        os.environ[key] = value


def _load_env() -> None:
    """Source DATABASE_URL (and friends) from tree-user-accessible env files."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    candidates = []
    if os.environ.get("DIKARYA_ENV_FILE"):
        candidates.append(Path(os.environ["DIKARYA_ENV_FILE"]))
    candidates.append(REPO_ROOT / ".env")
    candidates.append(Path.home() / ".dikarya" / "env")
    for path in candidates:
        if path.is_file():
            if load_dotenv:
                load_dotenv(path, override=False)
            else:
                _load_env_file_fallback(path)


def _ensure_secret_key() -> None:
    """Provide a one-shot SECRET_KEY so create_app() validates.

    The value is never persisted and never used to sign anything user-facing —
    we only open an app context to write one row and exit.
    """
    if not os.environ.get("SECRET_KEY") or os.environ["SECRET_KEY"] == "dev-key-please-change":
        os.environ["SECRET_KEY"] = "cli-stub-" + secrets.token_urlsafe(32)


def _require_database_url() -> None:
    if os.environ.get("DATABASE_URL"):
        return
    sys.stderr.write(
        "ERROR: DATABASE_URL is not set.\n\n"
        "Set it in one of:\n"
        f"  - {REPO_ROOT}/.env\n"
        "  - ~/.dikarya/env\n"
        "  - the shell environment\n\n"
        "The production value lives in /etc/dikarya/web.env (root-only). Ask an\n"
        "admin to copy the DATABASE_URL line into one of the locations above.\n"
    )
    sys.exit(2)


def _build_app():
    sys.path.insert(0, str(REPO_ROOT))
    from app import create_app
    return create_app()


def cmd_add(args: argparse.Namespace) -> int:
    app = _build_app()
    from app.extensions import db
    from app.models import WhatsNewEntry

    with app.app_context():
        entry = WhatsNewEntry(title=args.title, body=args.body, category=args.category)
        db.session.add(entry)
        db.session.commit()
        print(f"Added entry #{entry.id}: {entry.title}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    app = _build_app()
    from app.models import WhatsNewEntry

    with app.app_context():
        entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()
        if not entries:
            print("No entries.")
            return 0
        for e in entries:
            print(f"[{e.id}] ({e.category}) {e.published_at.strftime('%Y-%m-%d')} — {e.title}")
    return 0


def main(argv=None) -> int:
    _reexec_in_project_venv()

    parser = argparse.ArgumentParser(description="Manage Dikarya What's New entries.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Add a What's New entry")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--body", required=True, help="Plain text or simple HTML")
    p_add.add_argument("--category", default="update",
                       choices=["feature", "fix", "improvement", "update"])
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="List existing entries")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)

    _load_env()
    _require_database_url()
    _ensure_secret_key()

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
