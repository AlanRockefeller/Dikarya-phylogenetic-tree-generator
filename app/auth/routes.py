from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from app.auth import bp
from app.extensions import db, limiter
from app.models import User
from app.services.security_utils import safe_next_url


def _safe_next(next_page):
    """Only honor the `next` query param if it's a same-origin path.

    Thin wrapper kept for readability at the call site; the rules live in
    security_utils.safe_next_url so every redirect target in the app is
    validated the same way. This version only rejected a scheme or netloc,
    which let `/\\evil.tld` through -- browsers rewrite the backslash to a
    slash and navigate off-site.
    """
    return safe_next_url(next_page)


def normalize_email(raw):
    """Canonical form of an account email: trimmed and lowercased.

    Addresses are case-insensitive in practice (the domain always, and no real
    mail provider distinguishes local parts by case), so treating `A@x.com` and
    `a@x.com` as two accounts only ever produces a user who cannot find their
    own jobs. New accounts are therefore stored in this form.
    """
    return (raw or '').strip().lower()


def find_user_by_email(raw):
    """Resolve a login identifier to exactly one account, or None.

    Deliberately *not* a plain `lower(email) = lower(:email)` lookup. Production
    already contains a pair of accounts that differ only in case, each with its
    own jobs, both created before registration normalized anything. A
    case-folded lookup would hand whichever row the database happened to return
    first to whoever typed either spelling.

    So: an exact match always wins, and the case-insensitive fallback applies
    only when it is unambiguous. That keeps both legacy accounts reachable by
    their own spelling while letting everybody else sign in regardless of case.
    """
    email = (raw or '').strip()
    if not email:
        return None
    exact = User.query.filter_by(email=email).first()
    if exact is not None:
        return exact
    matches = (User.query
               .filter(func.lower(User.email) == email.lower())
               .limit(2)
               .all())
    return matches[0] if len(matches) == 1 else None


@bp.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('user.user_jobs'))

    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password')

        if not email or not password:
            flash('Email and password are required.', 'danger')
            return redirect(url_for('auth.register'))

        # Case-insensitive, so `Alan@x.com` cannot become a second account
        # alongside `alan@x.com`.
        if User.query.filter(func.lower(User.email) == email).first():
            flash('Email already registered. Please log in.', 'warning')
            return redirect(url_for('auth.login'))

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # Two simultaneous registrations of the same address both pass the
            # lookup above and only one of them can insert. The unique index is
            # what actually guarantees uniqueness, so the loser is told to log
            # in rather than being handed a 500.
            db.session.rollback()
            flash('Email already registered. Please log in.', 'warning')
            return redirect(url_for('auth.login'))

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

        user = find_user_by_email(email)
        if user and password and user.check_password(password):
            login_user(user, remember=True)
            flash('Logged in successfully.', 'success')
            next_page = _safe_next(request.args.get('next'))
            return redirect(next_page or url_for('user.user_jobs'))
        else:
            flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html')

# POST only. As a GET this was reachable from any third-party page -- an
# <img src=".../auth/logout"> or a plain link was enough to sign a visitor out
# -- and GET is the wrong verb for something that changes server state anyway.
# POST puts it behind the app-wide CSRFProtect check; every caller is a form in
# the four nav templates.
@bp.route('/logout', methods=['POST'])
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('journal.home'))
