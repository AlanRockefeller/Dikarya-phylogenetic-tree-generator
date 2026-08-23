import logging
from datetime import datetime
from flask import (
    abort, flash, make_response, redirect, render_template, request, url_for,
)
from flask_login import login_required, current_user

from app.user import bp
from app.extensions import db
from app.models import Job, ApiToken
from app.api_v1.auth import generate_token, ALL_SCOPES

logger = logging.getLogger(__name__)


@bp.route('/jobs')
@login_required
def user_jobs():
    jobs = Job.query.filter_by(user_id=current_user.id).order_by(Job.created_at.desc()).all()
    return render_template('user_jobs.html', jobs=jobs)


@bp.route('/jobs/clear', methods=['POST'])
@login_required
def clear_jobs():
    """Remove the user's job history, and their files where that is possible.

    Two things happen here and they can succeed independently: the history row
    is deleted from the database, and the job's directory under var/jobs is
    deleted from disk. The old version reported "N jobs cleared successfully"
    for every run regardless, wrote directory failures to stdout with print()
    (so they went nowhere anybody reads), and left the user believing files had
    been removed that were still on disk. Now the flash says exactly what
    happened, and a failure is logged with the job id attached.
    """
    import shutil
    import os

    jobs = Job.query.filter_by(user_id=current_user.id).all()
    removed_rows = 0
    failed_dirs = []
    for job in jobs:
        if job.job_dir and os.path.exists(job.job_dir):
            try:
                if os.path.isdir(job.job_dir):
                    shutil.rmtree(job.job_dir)
                else:
                    os.remove(job.job_dir)
            except OSError as e:
                failed_dirs.append(job.id)
                logger.warning(
                    "event=jobs.clear_dir_failed job=%s dir=%s error=%s "
                    "The history row was still removed; the files remain on disk.",
                    job.id, job.job_dir, type(e).__name__,
                )
        db.session.delete(job)
        removed_rows += 1
    db.session.commit()

    if not removed_rows:
        flash('There were no jobs to clear.', 'info')
    elif failed_dirs:
        # Deliberately not "success": some of what the user asked to delete is
        # still there, and only an administrator can finish the job.
        flash(
            f'{removed_rows} job(s) removed from your history, but the files '
            f'for {len(failed_dirs)} of them could not be deleted and are still '
            f'on the server. This has been logged for the administrator.',
            'warning',
        )
    else:
        flash(f'{removed_rows} job(s) cleared, including their files.', 'success')
    return redirect(url_for('user.user_jobs'))


# ---------------------------------------------------------------------------
# API token management (web session only -- a leaked API token cannot mint
# more tokens, so these endpoints intentionally require @login_required and
# not the API bearer token.)
# ---------------------------------------------------------------------------

def _render_api_tokens_page(new_secret=None, new_token_name=None):
    """Render the token list, optionally revealing one just-created secret.

    `new_secret` is passed straight from the POST handler that minted it and is
    never stored anywhere. It used to travel through Flask's session, which is a
    signed -- not encrypted -- client-side cookie, so the plaintext bearer token
    was written to the user's browser and to anything that logged the cookie.
    """
    tokens = (ApiToken.query
              .filter_by(user_id=current_user.id)
              .order_by(ApiToken.created_at.desc())
              .all())
    html = render_template(
        'user/api_tokens.html',
        tokens=tokens,
        all_scopes=ALL_SCOPES,
        new_secret=new_secret,
        new_token_name=new_token_name,
    )
    response = make_response(html)
    if new_secret:
        # The only response that ever carries the plaintext token must not be
        # written to a browser or proxy cache.
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response


@bp.route('/tokens', methods=['GET'])
@login_required
def api_tokens():
    # A plain GET can never reveal a secret: the plaintext exists only in the
    # response to the POST that created it.
    return _render_api_tokens_page()


@bp.route('/tokens/create', methods=['POST'])
@login_required
def api_tokens_create():
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

    # Render the token page directly from this POST instead of redirecting.
    # The plaintext is shown exactly once, in this response only; it is not
    # stored anywhere, so a later GET (or a replayed session cookie) cannot
    # recover it. Only the SHA-256 hash reached the database.
    flash('Token created. Copy it now because it will only be shown once.', 'success')
    return _render_api_tokens_page(new_secret=plaintext, new_token_name=name)


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
