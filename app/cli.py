import click
from flask.cli import with_appcontext
from flask import current_app

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
    from app.monitoring.services import collect_system_metrics
    import json
    
    metrics_file = current_app.config.get("METRICS_FILE", "var/metrics/system_metrics.jsonl")
    
    # Ensure dir
    import os
    os.makedirs(os.path.dirname(metrics_file), exist_ok=True)
    
    print(f"Collecting metrics to {metrics_file}...")
    while True:
        m = collect_system_metrics()
        with open(metrics_file, "a") as f:
            f.write(json.dumps(m) + "\n")
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
        click.echo(f"[{e.id}] ({e.category}) {e.published_at.strftime('%Y-%m-%d')} — {e.title}")


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


def register(app):
    app.cli.add_command(run_worker_command)
    app.cli.add_command(run_metrics_command)
    app.cli.add_command(whats_new_add_command)
    app.cli.add_command(whats_new_list_command)
    app.cli.add_command(api_token_group)
