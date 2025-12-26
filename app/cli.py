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

def register(app):
    app.cli.add_command(run_worker_command)
    app.cli.add_command(run_metrics_command)
