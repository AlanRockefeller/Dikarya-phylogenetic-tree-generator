from flask import jsonify, render_template
from app.extensions import limiter
from app.monitoring import bp
from app.monitoring.services import (
    check_system_health,
    get_worker_status,
    get_global_metrics,
    collect_system_metrics,
    get_ai_usage,
    get_active_jobs,
    get_worker_details,
    get_recent_jobs,
)


def get_live_snapshot():
    """The payload the dashboard renders and then re-fetches on a timer.

    Assembled in one place so the initial server-rendered state and the polled
    refresh can never drift into two different shapes.
    """
    snapshot = get_active_jobs()
    snapshot["workers"] = get_worker_details().get("workers", [])
    return snapshot


@bp.route('/health')
def health_check():
    """Application health endpoint."""
    health = check_system_health()
    status_code = 200 if health["status"] == "ok" else 503
    return jsonify(health), status_code


# These operational summaries intentionally expose no credentials or private
# user/job data. /health/workers, /health/jobs, /metrics, and /admin/monitoring
# are safe for the public to view and do not need authentication. The live job
# views deliberately carry only truncated job references and never any part of
# a submission -- see the privacy note in services.py before adding a field.
@bp.route('/health/workers')
def worker_health():
    """Worker health endpoint."""
    return jsonify(get_worker_status())


@bp.route('/health/jobs')
# The dashboard polls this every 5s per open tab, which a single viewer stays
# well under. The limit is here because the endpoint is unauthenticated and does
# real work per call (Redis reads, a stat() sweep of the active job dirs,
# /proc reads), and each call holds one of the site's 32 request slots.
@limiter.limit("60 per minute; 1200 per hour")
def job_activity():
    """Live detail for running and queued jobs.

    Polled by the monitoring dashboard every few seconds, so it must stay cheap:
    RQ reads plus a stat() sweep of the active job directories, no full job
    table scan.
    """
    return jsonify(get_live_snapshot())


@bp.route('/metrics')
def metrics():
    """Global system metrics."""
    return jsonify(get_global_metrics())


@bp.route('/admin/monitoring')
def admin_dashboard():
    """Admin monitoring dashboard."""
    health = check_system_health()
    live = get_live_snapshot()
    metrics_data = get_global_metrics()
    system = collect_system_metrics()
    ai_usage = get_ai_usage(days=30)
    recent = get_recent_jobs()
    return render_template(
        'admin/monitoring.html',
        health=health,
        metrics=metrics_data,
        system=system,
        ai_usage=ai_usage,
        live=live,
        recent_jobs=recent,
    )
