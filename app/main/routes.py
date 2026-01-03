from flask import render_template, redirect, url_for
from app.main import bp


@bp.route('/tree')
def sequence_entry():
    return render_template('sequence_entry.html')

@bp.route('/job')
def job_redirect():
    return redirect(url_for('user.user_jobs'))

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
