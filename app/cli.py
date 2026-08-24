import logging
from importlib.util import find_spec

import click
from flask.cli import with_appcontext
from flask import current_app

logger = logging.getLogger(__name__)

@click.command("run-worker")
@with_appcontext
def run_worker_command():
    """Run the worker with heartbeat monitoring."""
    from app.workers.worker_monitor import run_worker_with_heartbeat
    print("Starting worker with heartbeat...")
    run_worker_with_heartbeat(current_app)

@click.command("run-metrics")
@with_appcontext
def run_metrics_command():
    """Run the system metrics collector."""
    import time
    from app.monitoring.services import collect_system_metrics, emit_health_transitions
    from app.services.log_rotation import rotate_runtime_logs
    from app.extensions import db
    import json

    metrics_file = current_app.config.get("METRICS_FILE", "var/metrics/system_metrics.jsonl")
    
    # Ensure dir. dirname() is empty when METRICS_FILE is a bare filename in the
    # working directory, and os.makedirs("") raises FileNotFoundError.
    import os
    metrics_dir = os.path.dirname(metrics_file)
    if metrics_dir:
        os.makedirs(metrics_dir, exist_ok=True)

    logger.info("event=metrics.started Collecting metrics to %s", metrics_file)
    next_log_rotation = 0.0
    while True:
        # One bad tick must not end the process. psutil can raise on a
        # disappearing mount, the database can be briefly unreachable, and the
        # metrics file lives on the same disk whose exhaustion we are trying to
        # report -- all transient, and all of which used to kill the collector
        # outright, so the health transitions that would have said so stopped
        # being emitted at exactly the moment they mattered.
        #
        # `except Exception` on purpose: KeyboardInterrupt and SystemExit are
        # BaseExceptions and still stop the process, so systemd can shut this
        # down normally.
        try:
            now = time.monotonic()
            if now >= next_log_rotation:
                rotate_runtime_logs(current_app)
                next_log_rotation = now + 3600
            m = collect_system_metrics()
            emit_health_transitions(m)
            with open(metrics_file, "a") as f:
                f.write(json.dumps(m) + "\n")
        except Exception:
            logger.exception(
                "event=metrics.tick_failed A metrics tick failed; the collector "
                "will retry on the next interval."
            )
        finally:
            # Alan 8/15/26 - Release the DB session between ticks.
            #
            # emit_health_transitions() runs SELECT 1 and a Job count every minute,
            # and this loop holds a single app context for the life of the process,
            # so the session's transaction never ended: production showed this
            # connection "idle in transaction" for 4h41m, holding ACCESS SHARE on
            # jobs and blocking any migration that wants an ALTER TABLE.
            #
            # In `finally` because a failed tick is the case that most needs it:
            # a tick that raised part-way through emit_health_transitions() leaves
            # exactly the open transaction this is here to close.
            try:
                db.session.remove()
            except Exception:
                logger.warning(
                    "event=metrics.session_release_failed Could not release the "
                    "database session after a metrics tick.", exc_info=True,
                )
        time.sleep(60)

@click.command("whats-new-add")
@click.option('--title', required=True, help='Entry title')
@click.option('--body', required=True, help='Entry body text (supports plain text or HTML)')
@click.option('--category', default='update', show_default=True,
              type=click.Choice(['feature', 'fix', 'improvement', 'update']),
              help='Entry category')
@with_appcontext
def whats_new_add_command(title, body, category):
    """Add a What's New changelog entry."""
    from app.models import WhatsNewEntry
    from app.extensions import db
    entry = WhatsNewEntry(title=title, body=body, category=category)
    db.session.add(entry)
    db.session.commit()
    click.echo(f"Added entry #{entry.id}: {title}")


@click.command("whats-new-list")
@with_appcontext
def whats_new_list_command():
    """List all What's New entries."""
    from app.models import WhatsNewEntry
    entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()
    if not entries:
        click.echo("No entries.")
        return
    for e in entries:
        click.echo(f"[{e.id}] ({e.category}) {e.published_at.strftime('%Y-%m-%d')}: {e.title}")


@click.command("dosage-rebuild-db")
@click.option("--csv-dir", type=click.Path(file_okay=False), help="Directory containing dosage CSV files.")
@click.option("--db-path", type=click.Path(dir_okay=False), help="SQLite database path to create/replace.")
@with_appcontext
def dosage_rebuild_db_command(csv_dir, db_path):
    """Rebuild the alkaloid estimator SQLite database from CSV files."""
    from app.dosage.db import get_csv_directory, get_database_path
    from app.dosage.importer import DosageImportError, rebuild_database

    try:
        result = rebuild_database(
            csv_dir or get_csv_directory(),
            db_path or get_database_path(),
        )
    except DosageImportError as exc:
        click.echo(f"Dosage import failed: {exc}", err=True)
        raise click.Abort()
    click.echo(
        "Imported {references} references, {species} species rows, "
        "{test_results} test results into {database}".format(**result)
    )


@click.command("grant-admin")
@click.option('--email', required=True, help='Email of the user to promote')
@click.option('--revoke', is_flag=True, help='Remove admin rights instead of granting them.')
@with_appcontext
def grant_admin_command(email, revoke):
    """Grant (or revoke) admin rights, which gate What's New and TODO editing."""
    from app.models import User
    from app.extensions import db

    user = User.query.filter_by(email=email).first()
    if not user:
        click.echo(f"No user with email '{email}'.", err=True)
        raise click.Abort()
    user.is_admin = not revoke
    db.session.commit()
    click.echo(f"{email} is_admin = {user.is_admin}")


@click.command("list-admins")
@with_appcontext
def list_admins_command():
    """List accounts that currently have admin rights."""
    from app.models import User

    admins = User.query.filter_by(is_admin=True).order_by(User.email).all()
    if not admins:
        click.echo("No admin accounts. Use 'flask grant-admin --email ...' to create one.")
        return
    for user in admins:
        click.echo(f"{user.id}\t{user.email}")


@click.group("api-token")
def api_token_group():
    """Manage API tokens (administrative)."""
    pass


@api_token_group.command("create")
@click.option('--email', required=True, help='Email of the owning user')
@click.option('--name', required=True, help='Label for the token (e.g. "ci-runner")')
@click.option('--scope', 'scopes', multiple=True,
              help='Repeat to add multiple scopes. Defaults to all four.')
@with_appcontext
def api_token_create_command(email, name, scopes):
    """Create an API token for a user. Prints the secret to stdout (shown once)."""
    from app.models import User, ApiToken
    from app.extensions import db
    from app.api_v1.auth import generate_token, ALL_SCOPES

    user = User.query.filter_by(email=email).first()
    if not user:
        click.echo(f"No user with email '{email}'.", err=True)
        raise click.Abort()
    chosen = list(scopes) if scopes else list(ALL_SCOPES)
    invalid = [s for s in chosen if s not in ALL_SCOPES]
    if invalid:
        click.echo(f"Invalid scopes: {invalid}. Valid: {ALL_SCOPES}", err=True)
        raise click.Abort()

    plaintext, token_hash, prefix = generate_token()
    token = ApiToken(user_id=user.id, name=name, token_hash=token_hash,
                     token_prefix=prefix, scopes=chosen)
    db.session.add(token)
    db.session.commit()
    click.echo(f"Token created for {email} ({name}). Scopes: {','.join(chosen)}")
    click.echo("Copy this secret now -- it will NOT be shown again:")
    click.echo("")
    click.echo(plaintext)


@api_token_group.command("list")
@click.option('--email', help='Filter by user email (optional)')
@with_appcontext
def api_token_list_command(email):
    """List API tokens (optionally filtered by user)."""
    from app.models import User, ApiToken
    query = ApiToken.query
    if email:
        user = User.query.filter_by(email=email).first()
        if not user:
            click.echo(f"No user with email '{email}'.", err=True)
            raise click.Abort()
        query = query.filter_by(user_id=user.id)
    tokens = query.order_by(ApiToken.created_at.desc()).all()
    if not tokens:
        click.echo("(no tokens)")
        return
    for t in tokens:
        owner = User.query.get(t.user_id)
        state = "REVOKED" if t.revoked_at else "active"
        last = t.last_used_at.strftime('%Y-%m-%d %H:%M') if t.last_used_at else 'never'
        click.echo(f"[{t.id}] {owner.email if owner else '?'} '{t.name}' prefix={t.token_prefix}… "
                   f"scopes={','.join(t.scopes or [])} {state} last_used={last}")


@api_token_group.command("revoke")
@click.argument('token_id', type=int)
@with_appcontext
def api_token_revoke_command(token_id):
    """Revoke a token by id."""
    from app.models import ApiToken
    from app.extensions import db
    from datetime import datetime
    t = ApiToken.query.get(token_id)
    if not t:
        click.echo(f"No token with id {token_id}.", err=True)
        raise click.Abort()
    if t.revoked_at:
        click.echo(f"Token {token_id} ('{t.name}') was already revoked at {t.revoked_at}.")
        return
    t.revoked_at = datetime.utcnow()
    db.session.commit()
    click.echo(f"Token {token_id} ('{t.name}') revoked.")


@click.command("reap-stuck-jobs")
@click.option("--older-than-days", default=1, show_default=True,
              help="Only reap non-terminal jobs older than this many days.")
@click.option("--dry-run", is_flag=True, help="Report what would change, then exit.")
@with_appcontext
def reap_stuck_jobs_command(older_than_days, dry_run):
    """Verify and mark long-abandoned queued/running jobs as failed.

    A job left in a non-terminal state keeps its SSE stream alive, and each open
    stream holds one of the (workers x threads) request slots. Enough of them and
    the site stops answering, so these have to be cleaned up rather than left.
    """
    from datetime import datetime, timedelta
    from app.extensions import db
    from app.models import Job
    from sqlalchemy import func
    from app.services.job_reconcile_service import (
        classify_reap_candidate,
        inspect_rq_job,
        reconcile_job_statuses,
    )
    from app.workers.queue import get_redis_connection

    # Ask RQ first. Anything it knows is dead gets corrected regardless of age,
    # which is both more accurate and less blunt than reaping purely on age.
    # Let the age-based section below own verified-missing jobs so the report
    # clearly distinguishes ordinary terminal RQ reconciliation from reaping.
    reconciled = reconcile_job_statuses(
        dry_run=dry_run, reconcile_missing_records=False
    )
    if reconciled:
        verb = "Would reconcile" if dry_run else "Reconciled"
        click.echo(f"{verb} {len(reconciled)} job(s) through ordinary RQ state handling:")
        for entry in reconciled:
            click.echo(
                f"  {entry['job_id']}  {entry['from_status']} -> "
                f"{entry['action']}  (rq={entry['rq_status']})"
            )
        click.echo("")

    reconciled_ids = {entry["job_id"] for entry in reconciled}

    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    stale = (
        Job.query
        .filter(Job.status.in_(("queued", "running")))
        .filter(func.coalesce(Job.updated_at, Job.created_at) < cutoff)
        .order_by(Job.created_at)
        .all()
    )

    if not stale:
        click.echo(f"No queued/running jobs older than {older_than_days} day(s).")
        return

    try:
        redis_conn = get_redis_connection()
        # A ping distinguishes a constructed client from a working connection.
        redis_conn.ping()
    except Exception as exc:
        click.echo(
            f"RQ status could not be verified ({exc}); skipped all {len(stale)} "
            "candidate job(s)."
        )
        return

    would_reap = []
    skipped_live = []
    skipped_unverified = []
    ordinary_rq = []

    for job in stale:
        activity_at = job.updated_at or job.created_at or datetime.utcnow()
        age = datetime.utcnow() - activity_at
        inspection = inspect_rq_job(redis_conn, job.id)
        item = (job, age, inspection)

        classification = classify_reap_candidate(
            inspection, already_reconciled=job.id in reconciled_ids
        )
        if classification == "ordinary_rq":
            ordinary_rq.append(item)
        elif classification == "live":
            skipped_live.append(item)
        elif classification == "reap":
            # Only a positively verified absence may be reaped.  Age identifies
            # this candidate; it does not override any live RQ state.
            would_reap.append(item)
        else:
            skipped_unverified.append(item)

    def _print_group(label, items):
        click.echo(f"{label}: {len(items)}")
        for job, age, inspection in items:
            rq_status = inspection.status or ("missing" if inspection.missing else "unknown")
            click.echo(
                f"  {job.id}  db={job.status:<8} rq={rq_status:<10} "
                f"inactive={str(age).split('.')[0]}  user={job.user_id}"
            )

    _print_group("Would reap" if dry_run else "Verified abandoned", would_reap)
    _print_group("Skipped because live in RQ", skipped_live)
    _print_group("Skipped because RQ status could not be verified", skipped_unverified)
    _print_group("Reconciled/skipped through ordinary RQ state handling", ordinary_rq)

    if dry_run:
        click.echo("\nDry run: nothing changed.")
        return

    now = datetime.utcnow()
    for job, _age, _inspection in would_reap:
        metrics = dict(job.metrics or {})
        metrics["reaped_at"] = now.isoformat()
        metrics["reaped_from_status"] = job.status
        metrics["reaped_reason"] = (
            f"No RQ job record exists and the database was inactive in "
            f"'{job.status}' for more than {older_than_days} day(s); marked "
            "failed so its status stream cannot hold a request slot open."
        )
        job.metrics = metrics
        job.status = "failed"
        job.updated_at = now

    if would_reap:
        db.session.commit()
    click.echo(f"\nMarked {len(would_reap)} verified abandoned job(s) as failed.")


@click.command("jobs-in-flight")
@with_appcontext
def jobs_in_flight_command():
    """List jobs RQ still considers live. Fail unless safety is verified.

    Run this BEFORE `sudo /usr/local/sbin/restart-dikarya-worker`: a restart kills
    the running work horse, losing that job's work and (until the worker's startup
    reconciliation runs) leaving its row stuck.

    Exit codes: 0 = safe to restart, 1 = a job is genuinely in flight and a
    restart would destroy its work, 2 = nothing is confirmed running but some
    non-terminal row could not be checked against RQ. Both non-zero codes mean
    "do not restart", so a plain `if ! flask jobs-in-flight` is still correct;
    they are distinct because 2 is routine (an expired RQ record self-clears on
    the worker's next reconciliation pass) while 1 means live work is at stake.
    """
    from app.services.job_reconcile_service import count_jobs_in_flight

    result = count_jobs_in_flight()
    in_flight = result["in_flight"]
    unknown = result["unknown"]

    if unknown:
        click.echo(
            "Cannot establish that the worker is safe to restart: "
            f"{len(unknown)} non-terminal database job(s) could not be "
            "verified against RQ:"
        )
        for job_id in unknown:
            click.echo(f"  {job_id}")

        if in_flight:
            click.echo(
                f"\nAdditionally, {len(in_flight)} job(s) are confirmed in flight:"
            )
            for entry in in_flight:
                click.echo(
                    f"  {entry['job_id']}  db={entry['db_status']}  "
                    f"rq={entry['rq_status']}  created={entry['created_at']}"
                )
            click.echo(
                "\nDo not restart the worker until every job can be verified as terminal."
            )
            raise SystemExit(1)

        click.echo(
            "\nDo not restart the worker until every job can be verified as terminal.\n"
            "Nothing is confirmed running, though. A row lands here when RQ has "
            "no record of it, which is normal once RQ's result TTL expires; the "
            "worker's reconciliation pass marks such rows failed after "
            "MISSING_RECORD_GRACE. Re-run this after that pass, or use "
            "`flask reap-stuck-jobs --dry-run` to inspect them."
        )
        raise SystemExit(2)

    if not in_flight:
        click.echo("No jobs in flight. Safe to restart the worker.")
        return

    click.echo(f"{len(in_flight)} job(s) IN FLIGHT -- restarting the worker will kill them:")
    for entry in in_flight:
        click.echo(f"  {entry['job_id']}  db={entry['db_status']}  rq={entry['rq_status']}  created={entry['created_at']}")
    click.echo("\nWait for these to finish, or accept that their work is lost.")
    raise SystemExit(1)


def register(app):
    app.cli.add_command(jobs_in_flight_command)
    app.cli.add_command(reap_stuck_jobs_command)
    app.cli.add_command(run_worker_command)
    app.cli.add_command(run_metrics_command)
    app.cli.add_command(whats_new_add_command)
    app.cli.add_command(whats_new_list_command)
    app.cli.add_command(grant_admin_command)
    app.cli.add_command(list_admins_command)
    if find_spec("app.dosage") is not None:
        app.cli.add_command(dosage_rebuild_db_command)
    app.cli.add_command(api_token_group)
