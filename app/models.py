from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db, login_manager

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Job(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="queued")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    job_dir = db.Column(db.String(512), nullable=False)
    input_type = db.Column(db.String(64), nullable=False)
    metrics = db.Column(db.JSON, default=dict)
    
    user = db.relationship("User", backref=db.backref("jobs", lazy=True))

class ApiToken(db.Model):
    """A revocable, scoped bearer token tied to a User.

    The plaintext secret is never stored — only its SHA-256 hash. The token
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
    scopes = db.Column(db.JSON, nullable=False, default=list)
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


@dataclass
class AlignmentParams:
    method: str  # "mafft", "muscle", "clustalo", "iqtree_builtin", "default"
    advanced_options: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TrimmingParams:
    method: str  # "none", "trimal", "bmge"
    options: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TreeBuilderParams:
    method: str            # "nj", "raxml", "iqtree", "mrbayes", "fasttree"
    model: str = "GTR+G"   # default model for ML
    bootstrap: int = 1000   # ML bootstrap replicates (Ignored for new RAxML workflow in favor of bootstrap_cap/p)
    mcmc_generations: int = 50000   # for MrBayes defaults
    mcmc_nruns: int = 2
    mcmc_nchains: int = 4
    threads: Optional[int] = None   # autodetect if None
    
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
