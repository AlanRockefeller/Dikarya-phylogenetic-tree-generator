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
    
    # Paths
    BASE_DIR = Path(__file__).resolve().parent.parent
    JOB_DIR = Path(os.environ.get('JOB_DIR') or BASE_DIR / 'var' / 'jobs')
    BLAST_CACHE_DIR = Path(os.environ.get('BLAST_CACHE_DIR') or BASE_DIR / 'cache' / 'blast')
    BLAST_EMAIL = os.environ.get('BLAST_EMAIL', 'dikarya@dikarya.us')
    BLAST_MAX_QUERY_LENGTH = int(os.environ.get('BLAST_MAX_QUERY_LENGTH', '50000'))  # 50KB max
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    WORKER_DIR = Path(os.environ.get('WORKER_DIR') or BASE_DIR / 'var' / 'workers')
    METRICS_FILE = Path(os.environ.get('METRICS_FILE') or BASE_DIR / 'var' / 'metrics' / 'system_metrics.jsonl')
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + str(BASE_DIR / 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
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

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
