from flask import render_template
from flask_login import login_required, current_user
from app.user import bp
from app.models import Job

@bp.route('/jobs')
@login_required
def user_jobs():
    jobs = Job.query.filter_by(user_id=current_user.id).order_by(Job.created_at.desc()).all()
    return render_template('user_jobs.html', jobs=jobs)

@bp.route('/jobs/clear', methods=['POST'])
@login_required
def clear_jobs():
    import shutil
    import os
    from flask import flash, redirect, url_for
    from app.extensions import db
    
    jobs = Job.query.filter_by(user_id=current_user.id).all()
    count = 0
    for job in jobs:
        if job.job_dir and os.path.exists(job.job_dir):
            try:
                if os.path.isdir(job.job_dir):
                    shutil.rmtree(job.job_dir)
                else:
                    os.remove(job.job_dir)
            except Exception as e:
                # Log error but continue
                print(f"Error deleting job dir {job.job_dir}: {e}")
        
        db.session.delete(job)
        count += 1
        
    db.session.commit()
    flash(f'{count} jobs cleared successfully.', 'success')
    return redirect(url_for('user.user_jobs'))
