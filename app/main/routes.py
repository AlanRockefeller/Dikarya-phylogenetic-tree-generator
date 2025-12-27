from flask import render_template
from app.main import bp

@bp.route('/')
def index():
    return render_template('index.html')

@bp.route('/beginner')
def beginner():
    return render_template('beginner.html')

@bp.route('/advanced')
def advanced():
    return render_template('advanced.html')

@bp.route('/job/<job_id>')
def job_status(job_id):
    # Initial status check (optional, could just render template)
    from app.workers.queue import get_job_status
    status_info = get_job_status(job_id)
    return render_template('job_status.html', job_id=job_id, status=status_info.get('status', 'unknown'))

@bp.route('/job/<job_id>/view')
def job_viewer(job_id):
    return render_template('job_viewer.html', job_id=job_id)

@bp.route('/health')
def health():
    return {"ok": True}


@bp.route("/test/phylotree")
def test_phylotree():
    return render_template("test_phylotree.html")
