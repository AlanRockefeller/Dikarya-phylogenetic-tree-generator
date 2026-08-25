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

    Ordering matters too, for the same reason it does in the v1 API's
    single-job delete. Destroying the directories first and committing the row
    deletions afterwards meant a failed commit left every history row live with
    its artifacts already gone for good. So each directory is *moved aside*
    into ``var/jobs/.trash`` first -- cheap, atomic and reversible -- the rows
    are deleted and committed, and only then are the moved copies reclaimed. A
    commit failure puts every staged directory back -- and when one of those
    restores itself fails, the user is told so rather than told that nothing
    was deleted, because the surviving row now points at a path whose contents
    are still in the staging area.
    """
    import shutil
    import os
    import time
    from pathlib import Path

    from app.config import Config

    def _inside_job_dir(candidate: Path) -> bool:
        """Never move or delete anything outside the job storage area."""
        try:
            return candidate.resolve().is_relative_to(Config.JOB_DIR.resolve())
        except OSError:
            return False

    jobs = Job.query.filter_by(user_id=current_user.id).all()
    removed_rows = 0
    failed_dirs = []
    staged_entries = []  # (job_id, source, staged)
    trash_root = Config.JOB_DIR / ".trash"

    for job in jobs:
        if job.job_dir and os.path.exists(job.job_dir):
            candidate = Path(job.job_dir)
            if not _inside_job_dir(candidate):
                failed_dirs.append(job.id)
                logger.warning(
                    "event=jobs.clear_dir_outside_job_dir job=%s dir=%s "
                    "The history row was still removed; the path is outside "
                    "var/jobs and was left untouched.",
                    job.id, job.job_dir,
                )
            else:
                try:
                    trash_root.mkdir(parents=True, exist_ok=True)
                    staged = trash_root / f"{job.id}.{int(time.time() * 1000)}"
                    candidate.rename(staged)
                    staged_entries.append((job.id, candidate, staged))
                except OSError as e:
                    # Staging failed, so nothing was destroyed: the row goes
                    # anyway (as it always has) and the files are reported as
                    # still present rather than silently leaked.
                    failed_dirs.append(job.id)
                    logger.warning(
                        "event=jobs.clear_dir_failed job=%s dir=%s error=%s "
                        "The history row was still removed; the files remain on disk.",
                        job.id, job.job_dir, type(e).__name__,
                    )
        db.session.delete(job)
        removed_rows += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        # The rows survive, so their artifacts must too: put them all back.
        restore_failures = []
        for job_id, source, staged in staged_entries:
            try:
                staged.rename(source)
            except OSError:
                restore_failures.append(job_id)
                # Both ends of the move, because recovering this by hand means
                # moving `staged` back to `source` and nothing else identifies
                # the pair once the request is over. The staged copy is
                # deliberately left where it is -- it is the only copy.
                logger.exception(
                    "event=jobs.clear_restore_failed job=%s staged=%s source=%s "
                    "The staged copy was left in place for manual recovery; "
                    "move it back to source to complete the rollback.",
                    job_id, staged, source,
                )
        logger.exception(
            "event=jobs.clear_commit_failed user=%s jobs=%s restore_failed=%s "
            "error=%s",
            current_user.id, len(jobs), len(restore_failures), type(e).__name__,
        )
        if restore_failures:
            # "Nothing was deleted" is false here. The rows were kept, but some
            # of their artifacts are sitting in the staging area rather than at
            # the path the surviving row points to, so those jobs are broken
            # until an administrator moves them back. Saying nothing happened
            # would send the user to a job that cannot open its own files with
            # no reason to report it. The staging path itself stays in the log:
            # var/jobs internals are not shown to ordinary users anywhere else.
            from app.services.log_context import log_degradation
            log_degradation(
                logger, "job_clear_rollback_incomplete",
                "Clear-jobs rolled the database back but could not restore "
                "every staged job directory",
                user_id=current_user.id, jobs=len(restore_failures),
            )
            flash(
                'Your jobs could not be cleared because of a database error. '
                'Your job history was kept, but the files for '
                f'{len(restore_failures)} of them could not be put back '
                'automatically and need an administrator to recover them. '
                'This has been logged. Please do not retry until it is fixed.',
                'error',
            )
        else:
            flash(
                'Your jobs could not be cleared because of a database error. '
                'Nothing was deleted. Please try again.',
                'error',
            )
        return redirect(url_for('user.user_jobs'))

    # Logical deletion is committed. Reclaiming the bytes is now best effort; a
    # leftover under var/jobs/.trash is visible evidence, not lost data.
    for job_id, _source, staged in staged_entries:
        failure = None
        try:
            if staged.is_dir():
                shutil.rmtree(staged)
            else:
                staged.unlink(missing_ok=True)
        except OSError as exc:
            failure = type(exc).__name__
        if failure or staged.exists():
            from app.services.log_context import log_degradation
            log_degradation(
                logger, "job_trash_cleanup_failed",
                "Cleared job files could not be removed from var/jobs/.trash",
                job_id=job_id, staged=str(staged),
                error=failure or "still_present",
            )
            # The row is gone but the bytes are still on the server, which is
            # exactly what the warning below tells the user. Reporting success
            # here would be the old lie in a new place.
            failed_dirs.append(job_id)

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
