import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-please-change'
    WTF_CSRF_TIME_LIMIT = None  # No expiration; tokens remain valid for session lifetime
    
    # External Tools
    RAXML_BINARY = os.environ.get('RAXML_BINARY', 'raxml-ng')
    IQTREE_BINARY = os.environ.get('IQTREE_BINARY', 'iqtree2')
    MRBAYES_BINARY = os.environ.get('MRBAYES_BINARY', 'mb')
    MAFFT_BINARY = os.environ.get('MAFFT_BINARY', 'mafft')
    MUSCLE_BINARY = os.environ.get('MUSCLE_BINARY', 'muscle')
    CLUSTALO_BINARY = os.environ.get('CLUSTALO_BINARY', 'clustalo')
    TRIMAL_BINARY = os.environ.get('TRIMAL_BINARY', 'trimal')
    BMGE_BINARY = os.environ.get('BMGE_BINARY', 'bmge')
    FASTTREE_BINARY = os.environ.get('FASTTREE_BINARY', '/usr/local/bin/FastTree')

    
    # Paths
    BASE_DIR = Path(__file__).resolve().parent.parent
    JOB_DIR = Path(os.environ.get('JOB_DIR') or BASE_DIR / 'var' / 'jobs')
    BLAST_CACHE_DIR = Path(os.environ.get('BLAST_CACHE_DIR') or BASE_DIR / 'cache' / 'blast')
    BLAST_EMAIL = os.environ.get('BLAST_EMAIL', 'dikarya@dikarya.us')
    BLAST_MAX_QUERY_LENGTH = int(os.environ.get('BLAST_MAX_QUERY_LENGTH', '50000'))  # 50KB max

    # Global request body cap. Sequences via /api/v1/jobs can be up to 5 MB;
    # 16 MB leaves headroom for JSON overhead and other fields. Requests
    # larger than this are rejected by Flask with 413 before the route runs.
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', str(16 * 1024 * 1024)))
    BLAST_POLL_INTERVAL_SECONDS = int(os.environ.get('BLAST_POLL_INTERVAL_SECONDS', '60'))
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    WORKER_DIR = Path(os.environ.get('WORKER_DIR') or BASE_DIR / 'var' / 'workers')
    METRICS_FILE = Path(os.environ.get('METRICS_FILE') or BASE_DIR / 'var' / 'metrics' / 'system_metrics.jsonl')
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + str(BASE_DIR / 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Validate pooled connections on checkout (cheap SELECT 1) and recycle
    # them every 30 minutes. Eliminates the "SSL connection has been closed
    # unexpectedly" tracebacks we saw when Postgres or the network dropped
    # an idle connection between requests.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    
    # Defaults
    BEGINNER_DEFAULT_ALIGNER = os.environ.get('BEGINNER_DEFAULT_ALIGNER', 'mafft')
    BEGINNER_DEFAULT_TRIMMING = os.environ.get('BEGINNER_DEFAULT_TRIMMING', 'none')
    
    DEFAULT_ML_MODEL = os.environ.get("DEFAULT_ML_MODEL", "GTR+G")
    DEFAULT_BOOTSTRAPS = int(os.environ.get("DEFAULT_BOOTSTRAPS", "100"))
    DEFAULT_MCMC_GENERATIONS = int(os.environ.get("DEFAULT_MCMC_GENERATIONS", "50000"))
    DEFAULT_MCMC_NRNS = int(os.environ.get("DEFAULT_MCMC_NRNS", "2"))
    DEFAULT_MCMC_CHAINS = int(os.environ.get("DEFAULT_MCMC_CHAINS", "4"))

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

    # Cookie hardening. Only in ProductionConfig so dev (which may run over
    # plain HTTP on localhost) is unaffected.
    #   SECURE   : browser only sends the cookie over HTTPS.
    #   HTTPONLY : JS cannot read the cookie via document.cookie (mitigates
    #              session theft if an XSS bug slips through).
    #   SAMESITE : 'Lax' blocks cross-site POST/AJAX from carrying the cookie,
    #              providing a second line of defense against CSRF while still
    #              allowing normal top-level link navigation.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
