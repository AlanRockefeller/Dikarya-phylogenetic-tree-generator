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
    bootstrap: int = 1000   # ML bootstrap replicates (ignored for NJ if not applicable)
    mcmc_generations: int = 50000   # for MrBayes defaults
    mcmc_nruns: int = 2
    mcmc_nchains: int = 4
    threads: Optional[int] = None   # autodetect if None
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
