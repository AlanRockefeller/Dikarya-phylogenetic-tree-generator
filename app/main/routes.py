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


def can_edit_whats_new():
    return (
        current_user.is_authenticated
        and (current_user.email or "").strip().lower() == WHATS_NEW_EDITOR_EMAIL
    )


def require_whats_new_editor():
    if not can_edit_whats_new():
        abort(404)


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
            
    return render_template('job_viewer.html', job_id=job_id, job_details=job_details, view_only=view_only)

# /health moved to the monitoring blueprint (app/monitoring/routes.py) where
# it does an actual DB + filesystem check. Keeping just one /health avoids
# shadowing it with the trivial {"ok": True} stub that used to live here.


@bp.route("/test/phylotree")
def test_phylotree():
    return render_template("test_phylotree.html")


@bp.route('/todo', methods=['GET', 'POST'])
@csrf.exempt
@limiter.limit("10 per minute")
def todo():
    todo_file = os.path.join(current_app.root_path, 'static', 'todos.txt')
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
    
    if request.method == 'POST':
        # Ensure static directory exists only on POST
        os.makedirs(os.path.dirname(todo_file), exist_ok=True)
        
        name = request.form.get('name', '')
        suggestion = request.form.get('suggestion', '')

        # Trim leading/trailing whitespace
        name = name.strip()
        suggestion = suggestion.strip()

        # Input caps BEFORE sanitizing
        name = name[:60]
        suggestion = suggestion[:1000]

        # Sanitize input: allow only alphanumeric, spaces, and some punctuation, including Spanish characters, /, and :
        name = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', name)
        suggestion = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', suggestion)

        # Collapse repeated whitespace inside the string to single spaces
        name = re.sub(r'\s+', ' ', name)
        suggestion = re.sub(r'\s+', ' ', suggestion)
        
        # Trim again
        name = name.strip()
        suggestion = suggestion.strip()

        # Re-apply final caps
        name = name[:60]
        suggestion = suggestion[:1000]

        # Only append if both are non-empty after sanitization
        if name and suggestion:
            try:
                # File-size cap (5 MB)
                if os.path.exists(todo_file) and os.path.getsize(todo_file) > MAX_FILE_SIZE:
                    # Fail closed: don't append if file is too large
                    pass
                else:
                    with open(todo_file, 'a', encoding='utf-8', newline='\n') as f:
                        f.write(f'{name}: {suggestion}\n')
            except OSError:
                # Handle OSError safely (fail closed)
                pass
                
        return redirect(url_for('main.todo'))

    todos = []
    if os.path.exists(todo_file):
        try:
            # Defense-in-depth: errors='replace' to avoid breaking on malformed byte sequences
            with open(todo_file, 'r', encoding='utf-8', errors='replace') as f:
                # Read only last 200 lines to optimize memory usage
                todos = list(deque((line.strip() for line in f), maxlen=200))
        except Exception:
            pass
    return render_template('todo.html', todos=todos)
