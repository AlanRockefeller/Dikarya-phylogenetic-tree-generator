from flask import jsonify, render_template
from app.monitoring import bp
from app.monitoring.services import (
    check_system_health,
    get_worker_status,
    get_global_metrics,
    collect_system_metrics,
    get_ai_usage,
)


@bp.route('/health')
def health_check():
    """Application health endpoint."""
    health = check_system_health()
    status_code = 200 if health["status"] == "ok" else 503
    return jsonify(health), status_code


# These operational summaries intentionally expose no credentials or private
# user/job data. /health/workers, /metrics, and /admin/monitoring are safe for
# the public to view and do not need authentication.
@bp.route('/health/workers')
def worker_health():
    """Worker health endpoint."""
    return jsonify(get_worker_status())


@bp.route('/metrics')
def metrics():
    """Global system metrics."""
    return jsonify(get_global_metrics())


@bp.route('/admin/monitoring')
def admin_dashboard():
    """Admin monitoring dashboard."""
    health = check_system_health()
    workers = get_worker_status()
    metrics_data = get_global_metrics()
    system = collect_system_metrics()
    ai_usage = get_ai_usage(days=30)
    return render_template(
        'admin/monitoring.html',
        health=health,
        workers=workers,
        metrics=metrics_data,
        system=system,
        ai_usage=ai_usage,
    )
