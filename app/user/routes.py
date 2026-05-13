from datetime import datetime
from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.user import bp
from app.extensions import db
from app.models import Job, ApiToken
from app.api_v1.auth import generate_token, ALL_SCOPES


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
                print(f"Error deleting job dir {job.job_dir}: {e}")
        db.session.delete(job)
        count += 1
    db.session.commit()
    flash(f'{count} jobs cleared successfully.', 'success')
    return redirect(url_for('user.user_jobs'))


# ---------------------------------------------------------------------------
# API token management (web session only -- a leaked API token cannot mint
# more tokens, so these endpoints intentionally require @login_required and
# not the API bearer token.)
# ---------------------------------------------------------------------------

@bp.route('/tokens', methods=['GET'])
@login_required
def api_tokens():
    tokens = (ApiToken.query
              .filter_by(user_id=current_user.id)
              .order_by(ApiToken.created_at.desc())
              .all())
    # `new_token_secret` is populated in the session by /tokens/create after
    # a successful POST and shown exactly once.
    from flask import session
    new_secret = session.pop('new_token_secret', None)
    new_token_name = session.pop('new_token_name', None)
    return render_template(
        'user/api_tokens.html',
        tokens=tokens,
        all_scopes=ALL_SCOPES,
        new_secret=new_secret,
        new_token_name=new_token_name,
    )


@bp.route('/tokens/create', methods=['POST'])
@login_required
def api_tokens_create():
    from flask import session

    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Token name is required.', 'danger')
        return redirect(url_for('user.api_tokens'))
    if len(name) > 80:
        flash('Token name must be 80 characters or fewer.', 'danger')
        return redirect(url_for('user.api_tokens'))

    requested_scopes = request.form.getlist('scopes')
    scopes = [s for s in requested_scopes if s in ALL_SCOPES]
    if not scopes:
        flash('At least one scope must be selected.', 'danger')
        return redirect(url_for('user.api_tokens'))

    plaintext, token_hash, prefix = generate_token()
    token = ApiToken(
        user_id=current_user.id,
        name=name,
        token_hash=token_hash,
        token_prefix=prefix,
        scopes=scopes,
    )
    db.session.add(token)
    db.session.commit()

    # Stash the plaintext in the session so the next page render can show it
    # exactly once. After that it is unrecoverable.
    session['new_token_secret'] = plaintext
    session['new_token_name'] = name
    flash('Token created. Copy it now — it will only be shown once.', 'success')
    return redirect(url_for('user.api_tokens'))


@bp.route('/tokens/<int:token_id>/revoke', methods=['POST'])
@login_required
def api_tokens_revoke(token_id):
    token = ApiToken.query.get_or_404(token_id)
    if token.user_id != current_user.id:
        abort(404)
    if token.revoked_at is None:
        token.revoked_at = datetime.utcnow()
        db.session.commit()
        flash(f'Token "{token.name}" revoked.', 'success')
    return redirect(url_for('user.api_tokens'))


@bp.route('/tokens/<int:token_id>/delete', methods=['POST'])
@login_required
def api_tokens_delete(token_id):
    token = ApiToken.query.get_or_404(token_id)
    if token.user_id != current_user.id:
        abort(404)
    db.session.delete(token)
    db.session.commit()
    flash(f'Token "{token.name}" deleted.', 'success')
    return redirect(url_for('user.api_tokens'))
