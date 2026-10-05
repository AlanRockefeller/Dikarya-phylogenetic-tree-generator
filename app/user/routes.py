import json
import logging
import re
import secrets
from datetime import datetime
from flask import (
    abort, flash, make_response, redirect, render_template, request, url_for,
)
from flask_login import login_required, current_user

from app.user import bp
from app.extensions import db
from app.models import Job, ApiToken
from app.api_v1.auth import generate_token, ALL_SCOPES
from app.services.security_utils import validate_job_id

logger = logging.getLogger(__name__)


@bp.route('/jobs')
@login_required
def user_jobs():
    from app.services.job_trash_service import (
        JOB_DELETE_GRACE_DAYS, PURGE_BATCH_LIMIT, expired_deleted_jobs,
        purge_cutoff, purge_jobs,
    )

    # Jobs past their grace period are purged lazily, when their owner next
    # opens this page. Best effort: a failure is logged and retried next time.
    expired = expired_deleted_jobs(current_user.id, limit=PURGE_BATCH_LIMIT)
    if expired:
        purge_jobs(expired)

    jobs = (Job.query.filter_by(user_id=current_user.id)
            .filter(Job.deleted_at.is_(None))
            .order_by(Job.created_at.desc()).all())
    deleted_jobs = (Job.query.filter_by(user_id=current_user.id)
                    .filter(Job.deleted_at.isnot(None))
                    .order_by(Job.deleted_at.desc()).all())
    cutoff = purge_cutoff()
    for job in deleted_jobs:
        job.days_left = max(0, (job.deleted_at - cutoff).days)
    return render_template('user_jobs.html', jobs=jobs, deleted_jobs=deleted_jobs,
                           grace_days=JOB_DELETE_GRACE_DAYS)


def _owned_job_or_404(job_id):
    if not validate_job_id(job_id):
        abort(404)
    job = db.session.get(Job, job_id)
    if job is None or job.user_id != current_user.id:
        abort(404)
    return job


@bp.route('/jobs/clear', methods=['POST'])
@login_required
def clear_jobs():
    """Move the user's unprotected jobs to "Recently deleted".

    Nothing is destroyed here: the rows and files are kept for the grace
    period and can be restored. See app/services/job_trash_service.py.
    """
    from app.services.job_trash_service import (
        JOB_DELETE_GRACE_DAYS, soft_delete_user_jobs,
    )

    try:
        deleted, skipped = soft_delete_user_jobs(current_user.id)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("event=jobs.clear_failed user=%s error=%s",
                         current_user.id, type(e).__name__)
        flash('Your jobs could not be cleared because of a database error. '
              'Nothing was changed. Please try again.', 'error')
        return redirect(url_for('user.user_jobs'))

    kept = f' {skipped} protected job(s) were kept.' if skipped else ''
    if not deleted:
        flash('There were no unprotected jobs to clear.' + kept, 'info')
    else:
        flash(f'{deleted} job(s) moved to Recently deleted. You can restore them '
              f'for {JOB_DELETE_GRACE_DAYS} days.' + kept, 'success')
    return redirect(url_for('user.user_jobs'))


@bp.route('/jobs/<job_id>/protect', methods=['POST'])
@login_required
def toggle_job_protection(job_id):
    job = _owned_job_or_404(job_id)
    job.protected = not job.protected
    db.session.commit()
    flash('Job protected: it will be kept when you clear your jobs.'
          if job.protected else 'Job unprotected.', 'success')
    return redirect(url_for('user.user_jobs'))


@bp.route('/jobs/<job_id>/restore', methods=['POST'])
@login_required
def restore_job(job_id):
    job = _owned_job_or_404(job_id)
    if job.deleted_at is not None:
        job.deleted_at = None
        db.session.commit()
    flash('Job restored.', 'success')
    return redirect(url_for('user.user_jobs'))


@bp.route('/jobs/restore-all', methods=['POST'])
@login_required
def restore_all_jobs():
    restored = (Job.query.filter_by(user_id=current_user.id)
                .filter(Job.deleted_at.isnot(None))
                .update({Job.deleted_at: None}, synchronize_session=False))
    db.session.commit()
    flash(f'{restored} job(s) restored.', 'success')
    return redirect(url_for('user.user_jobs'))


@bp.route('/jobs/<job_id>/purge', methods=['POST'])
@login_required
def purge_job(job_id):
    """Delete one already-deleted job permanently, before its grace period ends."""
    from app.services.job_trash_service import purge_jobs

    job = _owned_job_or_404(job_id)
    if job.deleted_at is None:
        flash('Only a job in Recently deleted can be deleted permanently.', 'error')
        return redirect(url_for('user.user_jobs'))
    result = purge_jobs([job])
    if result['failed']:
        flash('The job could not be deleted because of a database error. '
              'Nothing was deleted. Please try again.', 'error')
    elif result['files_left']:
        flash('The job was deleted, but its files could not be removed from the '
              'server. This has been logged for the administrator.', 'warning')
    else:
        flash('Job deleted permanently.', 'success')
    return redirect(url_for('user.user_jobs'))


# ---------------------------------------------------------------------------
# API token management (web session only -- a leaked API token cannot mint
# more tokens, so these endpoints intentionally require @login_required and
# not the API bearer token.)
# ---------------------------------------------------------------------------

# A just-created plaintext token is handed to the following GET through Redis,
# never through the session cookie (which is signed, not encrypted) and never
# by rendering the page straight from the POST (which made an ordinary browser
# refresh mint a second token). The stash is keyed by the owning user, popped
# on first read, and expires on its own if the redirect is never followed.
_REVEAL_TTL_SECONDS = 300
_REVEAL_ID_RE = re.compile(r'\A[0-9a-f]{32}\Z')


def _reveal_key(user_id, reveal_id):
    return f'dikarya:token_reveal:{user_id}:{reveal_id}'


def _stash_new_secret(plaintext, name):
    """Park the plaintext for one following GET. Returns None if Redis is down."""
    from app.workers.queue import get_redis_connection

    reveal_id = secrets.token_hex(16)
    try:
        get_redis_connection().set(
            _reveal_key(current_user.id, reveal_id),
            json.dumps({'secret': plaintext, 'name': name}),
            ex=_REVEAL_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning(
            'event=token.reveal_stash_failed error=%s '
            'The token was created; its plaintext could not be handed off.',
            type(e).__name__,
        )
        return None
    return reveal_id


def _pop_new_secret(reveal_id):
    """Read and destroy a stashed plaintext. Returns (secret, name)."""
    from app.workers.queue import get_redis_connection

    if not reveal_id or not _REVEAL_ID_RE.match(reveal_id):
        return None, None
    try:
        raw = get_redis_connection().getdel(_reveal_key(current_user.id, reveal_id))
    except Exception as e:
        logger.warning(
            'event=token.reveal_read_failed error=%s', type(e).__name__,
        )
        return None, None
    if not raw:
        return None, None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, None
    return payload.get('secret'), payload.get('name')


def _render_api_tokens_page(new_secret=None, new_token_name=None):
    """Render the token list, optionally revealing one just-created secret.

    `new_secret` is never stored in the session, which is a signed -- not
    encrypted -- client-side cookie, so the plaintext bearer token would
    otherwise be written to the user's browser and to anything that logged the
    cookie.
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
    # A GET reveals a secret only when it carries the one-time id handed out by
    # the redirect from the creation POST, and only the first time: the stash is
    # popped here, so a refresh of this URL shows the list without the token.
    new_secret, new_token_name = _pop_new_secret(request.args.get('reveal'))
    return _render_api_tokens_page(new_secret=new_secret, new_token_name=new_token_name)


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

    # Post/redirect/GET: rendering the page straight from this POST made a
    # browser refresh (or a back/forward resubmission) mint a second live token
    # every time. The plaintext travels to the redirect target through a
    # single-use, user-scoped, five-minute Redis stash -- never the session
    # cookie -- and only the SHA-256 hash reached the database.
    reveal_id = _stash_new_secret(plaintext, name)
    if reveal_id is None:
        # Redis is unreachable. The token exists and is usable, but the only
        # copy of its plaintext is in this process, so it has to be shown now.
        flash('Token created. Copy it now because it will only be shown once.', 'success')
        return _render_api_tokens_page(new_secret=plaintext, new_token_name=name)

    flash('Token created. Copy it now because it will only be shown once.', 'success')
    return redirect(url_for('user.api_tokens', reveal=reveal_id))


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
