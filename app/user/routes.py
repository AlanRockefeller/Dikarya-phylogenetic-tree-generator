from flask import render_template
from flask_login import login_required, current_user
from app.user import bp
from app.models import Job

@bp.route('/jobs')
@login_required
def user_jobs():
    jobs = Job.query.filter_by(user_id=current_user.id).order_by(Job.created_at.desc()).all()
    return render_template('user_jobs.html', jobs=jobs)
