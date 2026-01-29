from flask import render_template, redirect, url_for, abort, request, current_app
from flask_login import current_user
from app.main import bp
from app.services.security_utils import validate_safe_file_path
from app.services.access_control import check_job_access
from app.extensions import csrf, limiter
import os
import re
from collections import deque


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

@bp.route('/health')
def health():
    return {"ok": True}


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
