from flask import render_template, redirect, url_for, abort, request, current_app, flash
from flask_login import current_user
from app.main import bp
from app.services.security_utils import validate_safe_file_path, validate_job_id
from app.services.access_control import check_job_access
from app.extensions import csrf, limiter, db
import os
import re
from collections import deque
from datetime import datetime

WHATS_NEW_EDITOR_EMAIL = "alaner@gmail.com"
TODO_ADMIN_DEFAULT_EMAILS = {"alaner@gmail.com", "mycology@dikarya.llc"}


def can_edit_whats_new():
    return (
        current_user.is_authenticated
        and (current_user.email or "").strip().lower() == WHATS_NEW_EDITOR_EMAIL
    )


def require_whats_new_editor():
    if not can_edit_whats_new():
        abort(404)


def is_todo_admin():
    if not current_user.is_authenticated:
        return False
    email = (getattr(current_user, "email", "") or "").strip().lower()
    raw_admins = os.environ.get("TODO_ADMIN_EMAILS")
    if raw_admins:
        admin_emails = {
            item.strip().lower()
            for item in raw_admins.split(",")
            if item.strip()
        }
    else:
        admin_emails = TODO_ADMIN_DEFAULT_EMAILS
    return email in admin_emails


def _sanitize_todo_input(name, suggestion):
    name = (name or "").strip()[:60]
    suggestion = (suggestion or "").strip()[:1000]

    # Preserve the original public todo character allowlist.
    name = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', name)
    suggestion = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', suggestion)

    name = re.sub(r'\s+', ' ', name).strip()[:60]
    suggestion = re.sub(r'\s+', ' ', suggestion).strip()[:1000]
    return name, suggestion


def _import_legacy_todos_if_needed():
    from app.models import TodoSuggestion

    if TodoSuggestion.query.first():
        return

    todo_file = os.path.join(current_app.root_path, 'static', 'todos.txt')
    if not os.path.exists(todo_file):
        return

    legacy_entries = []
    try:
        with open(todo_file, 'r', encoding='utf-8', errors='replace') as f:
            legacy_lines = list(deque((line.strip() for line in f), maxlen=200))
    except OSError:
        return

    for line in legacy_lines:
        if not line:
            continue
        if ': ' in line:
            raw_name, raw_suggestion = line.split(': ', 1)
        elif ':' in line:
            raw_name, raw_suggestion = line.split(':', 1)
        else:
            raw_name, raw_suggestion = "Anonymous", line
        name, suggestion = _sanitize_todo_input(raw_name or "Anonymous", raw_suggestion)
        if not suggestion:
            continue
        legacy_entries.append(TodoSuggestion(name=name or "Anonymous", suggestion=suggestion))

    if legacy_entries:
        db.session.add_all(legacy_entries)
        db.session.commit()


@bp.route('/tree')
def sequence_entry():
    return render_template('sequence_entry.html')


@bp.route('/whats-new')
def whats_new():
    from app.models import WhatsNewEntry, WhatsNewView

    entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()

    last_viewed = None
    if current_user.is_authenticated:
        view_record = WhatsNewView.query.filter_by(user_id=current_user.id).first()
    else:
        view_record = WhatsNewView.query.filter_by(ip_address=request.remote_addr).first()

    if view_record:
        last_viewed = view_record.last_viewed_at

    now = datetime.utcnow()
    if view_record:
        view_record.last_viewed_at = now
    else:
        if current_user.is_authenticated:
            view_record = WhatsNewView(user_id=current_user.id, last_viewed_at=now)
        else:
            view_record = WhatsNewView(ip_address=request.remote_addr, last_viewed_at=now)
        db.session.add(view_record)
    db.session.commit()

    return render_template(
        'whats_new.html',
        entries=entries,
        last_viewed=last_viewed,
        can_edit_whats_new=can_edit_whats_new(),
        edit_mode=False
    )


@bp.route('/whats-new/edit')
def whats_new_edit():
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()
    return render_template(
        'whats_new.html',
        entries=entries,
        last_viewed=None,
        can_edit_whats_new=True,
        edit_mode=True
    )


@bp.route('/whats-new/<int:entry_id>/edit', methods=['POST'])
def whats_new_update(entry_id):
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    entry = WhatsNewEntry.query.get_or_404(entry_id)
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    category = (request.form.get("category") or "update").strip().lower()

    if category not in {"feature", "fix", "improvement", "update"}:
        category = "update"

    if not title or not body:
        flash("Title and body are required.", "error")
        return redirect(url_for("main.whats_new_edit"))

    entry.title = title[:255]
    entry.body = body
    entry.category = category
    db.session.commit()
    flash("What's New item updated.", "success")
    return redirect(url_for("main.whats_new_edit"))


@bp.route('/whats-new/<int:entry_id>/delete', methods=['POST'])
def whats_new_delete(entry_id):
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    entry = WhatsNewEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("What's New item deleted.", "success")
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("main.whats_new_edit"))

@bp.route('/job')
def job_redirect():
    return redirect(url_for('user.user_jobs'))

@bp.route('/job/<job_id>')
def job_status(job_id):
    # Reject malformed job_ids early so the template never renders a bogus
    # UUID into the page (defense in depth; Jinja autoescape already covers
    # the HTML contexts, but this avoids wasted API calls and gives a clean
    # 400 instead of a status page that 404s on every backend call).
    if not validate_job_id(job_id):
        abort(400)
    # Initial status check (optional, could just render template)
    from app.workers.queue import get_job_status
    status_info = get_job_status(job_id)
    return render_template('job_status.html', job_id=job_id, status=status_info.get('status', 'unknown'))

from app.config import Config
import json

@bp.route('/job/<job_id>/view')
def job_viewer(job_id):
    # Check access control (View Mode)
    db_job, error_msg, status_code = check_job_access(job_id, mode="view")
    if error_msg:
        # Check specific status codes to provide better UX/privacy
        if status_code in (401, 403):
            # Privacy: don't reveal existence of protected jobs
            abort(404)
        # Default abort for other errors (e.g. 400)
        abort(status_code)

    # Determine View-Only status logic
    # view_only = True if the job has an owner and the current user is NOT that owner.
    # Legacy/Anonymous jobs (user_id=None) remain mutable by public (view_only=False).
    view_only = False
    if db_job and db_job.user_id is not None:
        if not current_user.is_authenticated or current_user.id != db_job.user_id:
            view_only = True

    # Fetch job details for display
    job_dir = Config.JOB_DIR / job_id
    input_info_path = job_dir / "input_info.json"
    
    job_details = {}
    if validate_safe_file_path(input_info_path, job_dir):
        try:
            with open(input_info_path, 'r') as f:
                job_details = json.load(f)
        except Exception:
            pass

    mycomap_blast_url = job_details.get("mycomap_blast_url")
    if not mycomap_blast_url and db_job and isinstance(db_job.metrics, dict):
        mycomap_blast_url = db_job.metrics.get("mycomap_blast_url")
    if mycomap_blast_url:
        try:
            from app.services.mycomap_service import validate_mycomap_url
            mycomap_blast_url = str(mycomap_blast_url).strip()
            blast_id = validate_mycomap_url(mycomap_blast_url)
            if blast_id:
                job_details["mycomap_blast_url"] = mycomap_blast_url
            else:
                job_details.pop("mycomap_blast_url", None)
        except Exception:
            job_details.pop("mycomap_blast_url", None)
            
    return render_template('job_viewer.html', job_id=job_id, job_details=job_details, view_only=view_only)

# /health moved to the monitoring blueprint (app/monitoring/routes.py) where
# it does an actual DB + filesystem check. Keeping just one /health avoids
# shadowing it with the trivial {"ok": True} stub that used to live here.


@bp.route("/test/phylotree")
def test_phylotree():
    return render_template("test_phylotree.html")


# ---------------------------------------------------------------------------
# iNaturalist OAuth (site-wide authorized account). Restricted to admin
# emails because there is only one site-wide token. Tokens are stored
# server-side and never echoed back; only generic status is returned.
# ---------------------------------------------------------------------------

INAT_OAUTH_ADMIN_EMAILS = {"mycology@dikarya.llc", "alaner@gmail.com"}


def _require_inat_oauth_admin():
    """Return None if the current user is an iNat OAuth admin, else abort(404).

    404 (not 403) so unauthorized callers cannot tell whether the route
    exists.
    """
    if not current_user.is_authenticated:
        abort(404)
    email = (getattr(current_user, "email", "") or "").strip().lower()
    admins = set(current_app.config.get("INAT_OAUTH_ADMIN_EMAILS")
                 or INAT_OAUTH_ADMIN_EMAILS)
    if email not in admins:
        abort(404)


@bp.route("/tree/oauth/connect")
def inat_oauth_connect():
    from flask import session, redirect as _redirect
    from app.services.inaturalist_oauth_service import (
        InatAuthError, authorize_url, new_oauth_state,
    )
    _require_inat_oauth_admin()
    try:
        state = new_oauth_state()
        session["inat_oauth_state"] = state
        return _redirect(authorize_url(state))
    except InatAuthError as e:
        flash(f"iNaturalist OAuth not configured: {e}", "error")
        return _redirect(url_for("main.sequence_entry"))


@bp.route("/tree/oauth/callback")
def inat_oauth_callback():
    from flask import session, redirect as _redirect
    from app.services.inaturalist_oauth_service import (
        InatAuthError, exchange_code_for_token,
    )
    _require_inat_oauth_admin()
    expected_state = session.pop("inat_oauth_state", None)
    state = request.args.get("state")
    code = request.args.get("code")
    if not expected_state or not state or state != expected_state:
        flash("OAuth state mismatch — please retry.", "error")
        return _redirect(url_for("main.sequence_entry"))
    if not code:
        flash(
            "iNaturalist did not return an authorization code.",
            "error",
        )
        return _redirect(url_for("main.sequence_entry"))
    try:
        exchange_code_for_token(code)
    except InatAuthError as e:
        flash(f"iNaturalist authorization failed: {e}", "error")
        return _redirect(url_for("main.sequence_entry"))
    flash(
        "iNaturalist authorization succeeded. The site can now post "
        "Phylogenetic Tree links back to observations.",
        "success",
    )
    return _redirect(url_for("main.sequence_entry"))


@bp.route("/tree/oauth/status")
def inat_oauth_status():
    from flask import jsonify as _jsonify
    from app.services.inaturalist_oauth_service import is_authorized
    _require_inat_oauth_admin()
    return _jsonify({"authorized": bool(is_authorized())})


@bp.route('/todo', methods=['GET', 'POST'])
@csrf.exempt
@limiter.limit("10 per minute")
def todo():
    from app.models import TodoSuggestion
    
    if request.method == 'POST':
        name = request.form.get('name', '')
        suggestion = request.form.get('suggestion', '')
        name, suggestion = _sanitize_todo_input(name, suggestion)

        # Only append if both are non-empty after sanitization
        if name and suggestion:
            db.session.add(TodoSuggestion(name=name, suggestion=suggestion, status='open'))
            db.session.commit()
                
        return redirect(url_for('main.todo'))

    _import_legacy_todos_if_needed()

    todo_admin = is_todo_admin()
    default_status_filter = 'open' if todo_admin else 'all'
    status_filter = (request.args.get('status') or default_status_filter).strip().lower()
    if status_filter not in {'open', 'done', 'all'}:
        status_filter = default_status_filter

    query = TodoSuggestion.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    todos = query.order_by(TodoSuggestion.created_at.desc(), TodoSuggestion.id.desc()).limit(200).all()
    return render_template(
        'todo.html',
        todos=todos,
        status_filter=status_filter,
        is_todo_admin=todo_admin,
    )


@bp.route('/todo/<int:suggestion_id>/status', methods=['POST'])
def todo_status(suggestion_id):
    from app.models import TodoSuggestion

    if not is_todo_admin():
        abort(404)

    next_status = (request.form.get('status') or '').strip().lower()
    if next_status not in {'open', 'done'}:
        abort(400)

    suggestion = TodoSuggestion.query.get_or_404(suggestion_id)
    now = datetime.utcnow()
    suggestion.status = next_status
    suggestion.updated_at = now
    if next_status == 'done':
        suggestion.completed_at = now
        suggestion.completed_by_id = current_user.id
    else:
        suggestion.completed_at = None
        suggestion.completed_by_id = None
    db.session.commit()

    return_status = (request.form.get('return_status') or 'open').strip().lower()
    if return_status not in {'open', 'done', 'all'}:
        return_status = 'open'
    return redirect(url_for('main.todo', status=return_status))


@bp.route('/todo/<int:suggestion_id>/delete', methods=['POST'])
def todo_delete(suggestion_id):
    from app.models import TodoSuggestion

    if not is_todo_admin():
        abort(404)

    suggestion = TodoSuggestion.query.get_or_404(suggestion_id)
    db.session.delete(suggestion)
    db.session.commit()
    flash("ToDo suggestion deleted.", "success")

    return_status = (request.form.get('return_status') or 'open').strip().lower()
    if return_status not in {'open', 'done', 'all'}:
        return_status = 'open'
    return redirect(url_for('main.todo', status=return_status))
