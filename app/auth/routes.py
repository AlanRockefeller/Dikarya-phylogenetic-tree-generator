import logging
import re

from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from app.auth import bp
from app.extensions import db, limiter
from app.models import User
from app.services.security_utils import safe_next_url

logger = logging.getLogger(__name__)


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


# Alan 10/7/26 - Auth log lines now name the email. They used to carry only an
# account id, and identifying a user locked out of their account took a
# production database query. Only an email-shaped value is ever written: a
# password typed into the email box has no "@", and a control character could
# forge a log line, so anything else is recorded as "-".
_LOGGABLE_EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{1,63}$")


def loggable_email(user=None, typed=None):
    """The account's stored email, else the typed one, if email-shaped, else "-"."""
    stored = getattr(user, "email", None) if user is not None else None
    # The stored address gets the same check: registration only requires it to
    # be non-empty, so an account created with a control character in its
    # email would otherwise forge a log line on every later login.
    if isinstance(stored, str) and stored:
        candidate = stored
    else:
        candidate = normalize_email(typed)
    if _LOGGABLE_EMAIL_RE.match(candidate) and candidate.isprintable():
        return candidate
    return "-"


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
    # Alan 9/22/26 - Carry `next` through registration the same way login does,
    # so a visitor sent here from the Tree Builder (e.g. to queue a large
    # iNaturalist batch) lands back where they started.
    next_page = _safe_next(request.args.get('next'))
    if current_user.is_authenticated:
        return redirect(next_page or url_for('user.user_jobs'))

    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password')

        if not email or not password:
            logger.warning("event=auth.registration_failed reason=missing_credentials email=%s",
                           loggable_email(typed=email))
            flash('Email and password are required.', 'danger')
            return redirect(url_for('auth.register', next=next_page))

        # Case-insensitive, so `Alan@x.com` cannot become a second account
        # alongside `alan@x.com`.
        if User.query.filter(func.lower(User.email) == email).first():
            logger.warning("event=auth.registration_failed reason=already_registered email=%s",
                           loggable_email(typed=email))
            flash('Email already registered. Please log in.', 'warning')
            return redirect(url_for('auth.login', next=next_page))

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
            logger.warning("event=auth.registration_failed reason=registration_conflict email=%s",
                           loggable_email(typed=email))
            flash('Email already registered. Please log in.', 'warning')
            return redirect(url_for('auth.login', next=next_page))

        login_user(user, remember=True)
        logger.info("event=auth.registered account_id=%s email=%s",
                    user.id, loggable_email(user))
        flash('Registration successful!', 'success')
        return redirect(next_page or url_for('user.user_jobs'))

    return render_template('auth/register.html')

@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 30 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_safe_next(request.args.get('next')) or url_for('user.user_jobs'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        user = find_user_by_email(email)
        if user and password and user.check_password(password):
            login_user(user, remember=True)
            logger.info("event=auth.login_succeeded account_id=%s email=%s",
                        user.id, loggable_email(user))
            flash('Logged in successfully.', 'success')
            next_page = _safe_next(request.args.get('next'))
            return redirect(next_page or url_for('user.user_jobs'))
        else:
            # Login failures return HTTP 200, so access/error-status logging
            # cannot see them. The password is never logged; the email is
            # (see loggable_email), including the typed one when it matched no
            # account, since a mistyped address is a common reason for a lockout.
            logger.warning(
                "event=auth.login_failed reason=%s account_id=%s email=%s",
                "invalid_credentials" if user else "unknown_account",
                user.id if user else "-",
                loggable_email(user, email),
            )
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
