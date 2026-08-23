import hmac
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Dict, Any, Optional
from datetime import datetime
from flask import current_app
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.ext.mutable import MutableDict, MutableList

from app.extensions import db, login_manager


def _session_auth_hash(password_hash: str) -> str:
    """Derive the session token that binds a cookie to the current password.

    Remember cookies are long-lived, so changing the password has to be able to
    revoke them. Deriving the token from the stored password hash gives that
    for free: rotating the password rotates this value and every outstanding
    cookie for the account stops authenticating.
    """
    secret = current_app.config.get("SECRET_KEY") or ""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(
        secret,
        b"dikarya.session:" + (password_hash or "").encode("utf-8"),
        sha256,
    ).hexdigest()[:32]


@login_manager.user_loader
def load_user(user_id):
    identifier, separator, token = str(user_id or "").partition(".")
    if not identifier.isdigit():
        return None
    user = User.query.get(int(identifier))
    if user is None:
        return None
    # Reject cookies minted before session tokens existed, and any cookie whose
    # token no longer matches the account's current password hash.
    if not separator or not hmac.compare_digest(token, _session_auth_hash(user.password_hash)):
        return None
    return user

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_id(self):
        return f"{self.id}.{_session_auth_hash(self.password_hash)}"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Job(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="queued")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # onupdate is what makes this differ from created_at. Without it the column
    # was set once at insert and never moved again, so every job looked like it
    # had not been touched since submission.
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    job_dir = db.Column(db.String(512), nullable=False)
    input_type = db.Column(db.String(64), nullable=False)
    # MutableDict, not a bare db.JSON: several worker paths read `db_job.metrics`,
    # mutate the dict in place and assign the *same object* back. SQLAlchemy
    # compares the new value against the committed one, finds them equal (they
    # are the same object), records no history and emits no UPDATE -- so those
    # metrics were silently lost. Tracking mutation fixes it without touching
    # the PostgreSQL column, which stays plain JSON.
    metrics = db.Column(MutableDict.as_mutable(db.JSON), default=dict)
    
    user = db.relationship("User", backref=db.backref("jobs", lazy=True))

class ApiToken(db.Model):
    """A revocable, scoped bearer token tied to a User.

    The plaintext secret is never stored; only its SHA-256 hash is kept. The token
    `prefix` (first 12 chars of the secret) is stored separately so the UI
    can show users which token is which without exposing the secret again
    after creation.
    """
    __tablename__ = 'api_token'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    token_prefix = db.Column(db.String(20), nullable=False)
    # JSON list of scope strings, e.g. ["jobs:read","jobs:write","tools:read","account:read"]
    scopes = db.Column(MutableList.as_mutable(db.JSON), nullable=False, default=list)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('api_tokens', lazy=True, cascade='all, delete-orphan'))

    @property
    def is_active(self):
        return self.revoked_at is None

    def has_scope(self, scope):
        return scope in (self.scopes or [])


class WhatsNewEntry(db.Model):
    __tablename__ = 'whats_new_entry'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(64), nullable=False, default='update')
    published_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WhatsNewView(db.Model):
    __tablename__ = 'whats_new_view'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, unique=True)
    ip_address = db.Column(db.String(45), nullable=True, unique=True)
    last_viewed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User', backref=db.backref('whats_new_view', uselist=False))


class TodoSuggestion(db.Model):
    __tablename__ = 'todo_suggestion'
    __table_args__ = (
        db.CheckConstraint("status in ('open', 'done')", name='ck_todo_suggestion_status'),
    )
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    suggestion = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default='open', index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    completed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    completed_by = db.relationship('User', backref=db.backref('completed_todo_suggestions', lazy=True))


@dataclass
class AlignmentParams:
    method: str  # "mafft", "muscle", "clustalo", "iqtree_builtin", "default"
    advanced_options: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TrimmingParams:
    method: str  # "none", "trimal", "bmge"
    # Trim ragged terminal alignment columns before tree building. First-class
    # so both the worker and recompute paths model it the same way instead of
    # smuggling it through `options`.
    trim_terminal_overhangs: bool = True
    options: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TreeBuilderParams:
    method: str            # "nj", "raxml", "iqtree", "mrbayes", "fasttree"
    model: str = "GTR+G"   # default model for ML
    bootstrap: int = 1000   # ML bootstrap replicates (Ignored for new RAxML workflow in favor of bootstrap_cap/p)
    # MrBayes defaults. mcmc_generations is a *maximum* whenever
    # mcmc_stop_early is on: MrBayes ends the run as soon as the independent
    # runs agree on split frequencies to within DEFAULT_MCMC_STOPVAL.
    mcmc_generations: int = 1000000
    mcmc_nruns: int = 2
    mcmc_nchains: int = 4
    mcmc_burnin_fraction: float = 0.25
    # MrBayes-only convergence stop rule. Deliberately separate from
    # `early_stopping` below, which is RAxML's unrelated bootstrap-convergence
    # test. Requires mcmc_nruns >= 2; with a single run there are no split
    # frequencies to compare and no stop rule is emitted.
    mcmc_stop_early: bool = True
    threads: Optional[int] = None   # autodetect if None

    # IQ-TREE SH-aLRT replicates (-alrt). 0/None disables it. When enabled,
    # IQ-TREE writes dual "SH-aLRT/UFBoot" node labels into its .treefile.
    alrt_replicates: Optional[int] = None
    
    # New RAxML-NG specific fields
    run_preset: str = "fast_good"          # fast_good, standard, publication, maximum
    bootstrap_preset: str = "standard"     # standard, publication, maximum
    bootstrap_cap: Optional[int] = None    # User override for caps
    enable_bootstrap: bool = True          # New explicit toggle
    start_tree_override: Optional[str] = None
    moose_enabled: bool = False
    early_stopping: bool = False
    seed: Optional[int] = None
    outgroup: Optional[str] = None
    
    advanced_options: Dict[str, Any] = field(default_factory=dict)

@dataclass
class JobParams:
    input_type: str
    notes: str = ""
    sequence: str = ""
    accessions: list = field(default_factory=list)
    alignment_params: Optional[AlignmentParams] = None
    trimming_params: Optional[TrimmingParams] = None
    tree_builder_params: Optional[TreeBuilderParams] = None
    allow_recompute: bool = True
    validation_warnings: list = field(default_factory=list)
