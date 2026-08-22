from urllib.parse import urlparse
from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user
from app.auth import bp
from app.extensions import db, limiter
from app.models import User


def _safe_next(next_page):
    """Only honor the `next` query param if it's a same-origin path.
    Prevents open-redirect via crafted ?next=https://evil.tld/phish links."""
    if not next_page:
        return None
    parsed = urlparse(next_page)
    # Must have no scheme / host (i.e., a relative URL) and must start with '/'.
    if parsed.scheme or parsed.netloc:
        return None
    if not next_page.startswith('/'):
        return None
    return next_page


@bp.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('user.user_jobs'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not email or not password:
            flash('Email and password are required.', 'danger')
            return redirect(url_for('auth.register'))
            
        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please log in.', 'warning')
            return redirect(url_for('auth.login'))
            
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        login_user(user, remember=True)
        flash('Registration successful!', 'success')
        return redirect(url_for('user.user_jobs'))
        
    return render_template('auth/register.html')

@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('user.user_jobs'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            flash('Logged in successfully.', 'success')
            next_page = _safe_next(request.args.get('next'))
            return redirect(next_page or url_for('user.user_jobs'))
        else:
            flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html')

@bp.route('/logout')
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('journal.home'))
