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


def register(app):
    app.cli.add_command(run_worker_command)
    app.cli.add_command(run_metrics_command)
    app.cli.add_command(whats_new_add_command)
    app.cli.add_command(whats_new_list_command)
