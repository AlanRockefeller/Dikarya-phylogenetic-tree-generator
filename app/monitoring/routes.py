from flask import Blueprint, jsonify, render_template, request
from app.monitoring.services import (
    check_system_health,
    get_worker_status,
    get_global_metrics,
    collect_system_metrics
)

bp = Blueprint('monitoring', __name__)

@bp.route('/health')
def health_check():
    """Application health endpoint."""
    health = check_system_health()
    status_code = 200 if health["status"] == "ok" else 503
    return jsonify(health), status_code

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
    # Add authentication check here later (e.g., @login_required, @admin_required)
    
    # Collect initial data for server-side rendering
    health = check_system_health()
    workers = get_worker_status()
    metrics = get_global_metrics()
    system = collect_system_metrics()
    
    return render_template(
        'admin/monitoring.html',
        health=health,
        workers=workers,
        metrics=metrics,
        system=system
    )
