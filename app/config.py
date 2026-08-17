import os
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv


def _load_environment():
    env_file = (
        os.environ.get("DIKARYA_ENV_FILE")
        or os.environ.get("ENV_FILE")
        or None
    )
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()


_load_environment()


def _csv_env(name, default=""):
    raw = os.environ.get(name, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _release_version(base_dir):
    """Resolve the deployment identifier through the one canonical resolver.

    app.services.log_context owns this (it stamps `release` onto every log
    record and caches the answer for the life of the process); this shim keeps
    the existing RELEASE_VERSION config key working without a second copy of the
    .git-reading logic that could drift out of step with it.
    """
    from app.services.log_context import configured_release

    return configured_release(base_dir)


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-please-change'
    WTF_CSRF_TIME_LIMIT = None  # No expiration; tokens remain valid for session lifetime

    # Keep signed-in users remembered across browser and machine restarts. The
    # lifetime is rolling and refreshed on every request, so anyone who visits
    # even once a year stays signed in, while a cookie that was copied off a
    # machine and then goes unused stops working. Changing an account password
    # rotates its session token and revokes every outstanding cookie
    # immediately (see User.get_id in app/models.py).
    REMEMBER_COOKIE_DURATION = timedelta(days=int(os.environ.get('REMEMBER_COOKIE_DAYS', 365)))
    REMEMBER_COOKIE_REFRESH_EACH_REQUEST = True
    
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

    # Resource ceilings applied to every external tool we spawn (see
    # subprocess_utils.run_command). The host has 15 GB of RAM and no swap, so
    # an alignment or tree run that grows without bound gets the machine OOM
    # killed rather than just failing its own job -- and the kernel picks the
    # victim, which may well be Gunicorn or Redis rather than the culprit.
    # These limits make the offending process die on its own instead.
    #
    # 9 GB leaves headroom beneath the worker cgroup's 10 GB ceiling, so the
    # child receives a diagnosable allocation failure before systemd has to
    # kill the whole worker. The host's remaining 5 GB stays available to the
    # OS, Redis, and Gunicorn. Generic CPU limiting is disabled by default:
    # RLIMIT_CPU accumulates across threads, so a fixed value can expire before
    # the advertised wall-clock allowance for a multithreaded tool. RAxML
    # explicitly supplies a thread-scaled CPU allowance. Set either resource
    # value to 0 to disable that limit.
    SUBPROCESS_MEMORY_LIMIT_MB = int(os.environ.get('SUBPROCESS_MEMORY_LIMIT_MB', '9216'))
    SUBPROCESS_CPU_LIMIT_SECONDS = int(os.environ.get('SUBPROCESS_CPU_LIMIT_SECONDS', '0'))

    # Ordinary jobs previously had a one-hour RQ deadline. That was too short
    # for legitimate large MUSCLE/MAFFT alignments even though the host still
    # had ample memory. RAxML keeps its separate, longer allowance below.
    GENERAL_JOB_TIME_LIMIT_HOURS = float(os.environ.get('GENERAL_JOB_TIME_LIMIT_HOURS', '8'))

    # RAxML-NG with --all and autoMRE bootstrapping routinely needs far more
    # than the default 1h wall clock, and used to die at exactly one hour with
    # an unexplained failure. It now gets its own budget, which has to be
    # honoured in three places or the shortest one still wins:
    #   1. the RQ job_timeout   (app/workers/queue.py)
    #   2. the subprocess wait  (_run_raxml)
    #   3. RLIMIT_CPU via prlimit (_run_raxml, scaled by thread count)
    RAXML_TIME_LIMIT_HOURS = float(os.environ.get('RAXML_TIME_LIMIT_HOURS', '15'))


    # Paths
    BASE_DIR = Path(__file__).resolve().parent.parent
    JOB_DIR = Path(os.environ.get('JOB_DIR') or BASE_DIR / 'var' / 'jobs')
    BLAST_CACHE_DIR = Path(os.environ.get('BLAST_CACHE_DIR') or BASE_DIR / 'cache' / 'blast')
    # ITSx HMM profiles, used by pyitsx for optional ITS1/5.8S/ITS2 extraction.
    ITSX_HMM_DIR = Path(os.environ.get('ITSX_HMM_DIR') or BASE_DIR / 'cache' / 'itsx' / 'HMMs')
    BLAST_EMAIL = os.environ.get('BLAST_EMAIL', '')
    # Reverse geocoder used when a GenBank record has lat_lon coordinates but no
    # textual geo_loc_name/country. Nominatim is free and needs no key; its usage
    # policy caps us at 1 request/second (enforced in genbank_location_service).
    REVERSE_GEOCODE_URL = os.environ.get(
        'REVERSE_GEOCODE_URL', 'https://nominatim.openstreetmap.org/reverse'
    )
    REVERSE_GEOCODE_ENABLED = os.environ.get('REVERSE_GEOCODE_ENABLED', '1') not in ('0', 'false', 'False')
    BLAST_MAX_QUERY_LENGTH = int(os.environ.get('BLAST_MAX_QUERY_LENGTH', '50000'))  # 50KB max

    # Global request body cap. Sequences via /api/v1/jobs can be up to 5 MB;
    # 16 MB leaves headroom for JSON overhead and other fields. Requests
    # larger than this are rejected by Flask with 413 before the route runs.
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', str(16 * 1024 * 1024)))
    BLAST_POLL_INTERVAL_SECONDS = int(os.environ.get('BLAST_POLL_INTERVAL_SECONDS', '60'))
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

    # Claude review of a finished alignment + tree (app/services/tree_analysis_service.py).
    # Unconfigured, the button is hidden and the endpoint returns 503, so a
    # deployment without it behaves as if the feature does not exist.
    #
    # Two backends:
    #   cli - shell out through a root-owned sudo wrapper to the `tree` account's
    #         Claude Code CLI. No API key needed; requires the wrapper to be
    #         installed (see ops/sudoers/dikarya-claude).
    #   api - call the Anthropic API directly with ANTHROPIC_API_KEY.
    CLAUDE_REVIEW_BACKEND = os.environ.get('CLAUDE_REVIEW_BACKEND', 'cli')
    CLAUDE_REVIEW_WRAPPER = os.environ.get(
        'CLAUDE_REVIEW_WRAPPER', '/usr/local/sbin/dikarya-claude-review'
    )
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    CLAUDE_REVIEW_MODEL = os.environ.get('CLAUDE_REVIEW_MODEL', 'claude-opus-5')
    # Measured on a 147-sequence job: low = 61s/$0.25, medium = 156s/$0.35, for
    # the same verdict. The request is synchronous and nginx cuts it off at 300s,
    # so low is the setting that fits; raise it if reviews read as too shallow.
    CLAUDE_REVIEW_EFFORT = os.environ.get('CLAUDE_REVIEW_EFFORT', 'low')
    CLAUDE_REVIEW_MAX_TOKENS = int(os.environ.get('CLAUDE_REVIEW_MAX_TOKENS', '32000'))
    # Hard wall-clock cap. A review runs inside a Gunicorn request slot (4 workers
    # x 2 threads = 8 total) and behind nginx's proxy_read_timeout 300s, so it must
    # finish well inside that or the user gets a 504 instead of an error.
    CLAUDE_REVIEW_TIMEOUT_SECONDS = float(os.environ.get('CLAUDE_REVIEW_TIMEOUT_SECONDS', '240'))
    # Per-invocation spend ceiling, enforced by the CLI's own --max-budget-usd.
    CLAUDE_REVIEW_MAX_BUDGET_USD = os.environ.get('CLAUDE_REVIEW_MAX_BUDGET_USD', '1.00')
    # Ceiling on reviews running at once, enforced with a Redis counter. Rate limits
    # are per-client and cannot stop eight different users from taking every slot.
    CLAUDE_REVIEW_MAX_CONCURRENT = int(os.environ.get('CLAUDE_REVIEW_MAX_CONCURRENT', '2'))
    WORKER_DIR = Path(os.environ.get('WORKER_DIR') or BASE_DIR / 'var' / 'workers')
    METRICS_FILE = Path(os.environ.get('METRICS_FILE') or BASE_DIR / 'var' / 'metrics' / 'system_metrics.jsonl')
    DOSAGE_CSV_DIR = Path(os.environ.get('DOSAGE_CSV_DIR') or BASE_DIR / 'dosage-calculator')
    DOSAGE_DB_PATH = Path(os.environ.get('DOSAGE_DB_PATH') or BASE_DIR / 'instance' / 'dosage_calculator.sqlite')
    
    # Database
    #
    # Alan 8/14/26 - The SQLite fallback below is a footgun on the live host: a
    # maintenance script run without DATABASE_URL in its environment silently
    # connects to a stale local app.db instead of production Postgres, and every
    # query returns a plausible-looking empty result rather than an error. That
    # cost real debugging time (a job lookup returned None because the shell had
    # no DATABASE_URL, not because the job was missing). create_app() now refuses
    # to boot on the fallback unless it is explicitly opted into.
    DATABASE_URL_IS_EXPLICIT = bool(os.environ.get('DATABASE_URL'))
    ALLOW_SQLITE_FALLBACK = os.environ.get('ALLOW_SQLITE_FALLBACK', '') in ('1', 'true', 'True', 'yes')
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
    # IQ-TREE gets its own default because it ships ModelFinder. "MFP" picks the
    # best-fit substitution model by BIC and then infers the tree with it, which
    # is the right default for a tool that can do the selection itself -- a fixed
    # GTR+G silently ignores whether the data want +I, +R, or a simpler matrix.
    # Costs seconds on typical ITS datasets. Set to a concrete model name (e.g.
    # "GTR+G") to go back to a fixed model for every IQ-TREE job.
    DEFAULT_IQTREE_MODEL = os.environ.get("DEFAULT_IQTREE_MODEL", "MFP")
    DEFAULT_BOOTSTRAPS = int(os.environ.get("DEFAULT_BOOTSTRAPS", "100"))
    # IQ-TREE runs SH-aLRT alongside Ultrafast Bootstrap by default, giving dual
    # "SH-aLRT/UFBoot" node labels. Set to 0 to report UFBoot only.
    DEFAULT_IQTREE_ALRT = int(os.environ.get("DEFAULT_IQTREE_ALRT", "1000"))

    # Default alignment trimmer. "trimal_gappy" (trimAl -gt 0.9) drops columns
    # that are >90% gaps -- alignment junk -- while leaving the variable ITS1/ITS2
    # regions intact. Deliberately NOT "trimal" (-automated1), which strips ~43% of
    # ITS1/ITS2 and produced fewer well-supported nodes than no trimming at all.
    DEFAULT_TRIMMING_METHOD = os.environ.get("DEFAULT_TRIMMING_METHOD", "trimal_gappy")

    # WARNING-and-above mirror of error.log. error.log stays as-is (nothing is
    # removed); this is the low-noise view for "what is actually broken", since
    # error.log runs ~98% INFO and buries real failures.
    ERROR_LOG_PATH = Path(os.environ.get('ERROR_LOG_PATH') or BASE_DIR / 'var' / 'logs' / 'errors.log')
    RELEASE_VERSION = _release_version(BASE_DIR)

    # Trust one proxy hop (nginx on this host) for X-Forwarded-For/-Proto/-Host, so
    # request.remote_addr is the real client rather than 127.0.0.1. Rate limiting is
    # keyed on it. Set to 0 only if the app is ever exposed without nginx in front.
    TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "1") not in ("0", "false", "False")

    # SSE (/api/job/<id>/events) safety limits. Gunicorn runs a fixed
    # workers x threads pool, so any stream that outlives its client holds a
    # request slot. These caps bound that: the client's EventSource reconnects
    # automatically and receives a fresh snapshot, so a live viewer sees no
    # interruption while an orphaned stream cannot pin a thread forever.
    #
    # Alan 8/14/26 - Raised from 30 minutes to 6 hours. The old cap punished long
    # jobs for being long: a RAxML "publication" run streaming normal progress was
    # cut every 30 minutes purely because of its age. Slot protection now comes from
    # SSE_MAX_IDLE_SECONDS below (which targets streams that are actually doing
    # nothing) plus a per-IP limit_conn in nginx, so this is only a final ceiling.
    SSE_MAX_STREAM_SECONDS = int(os.environ.get("SSE_MAX_STREAM_SECONDS", "21600"))
    # Close a stream that has seen no event, progress, or status change for this
    # long while its job is still non-terminal. That is the orphaned-viewer and
    # stuck-job case -- an actively progressing job keeps resetting this, so it
    # streams for as long as it genuinely runs.
    SSE_MAX_IDLE_SECONDS = int(os.environ.get("SSE_MAX_IDLE_SECONDS", "1800"))
    # How long to hold a stream open for a job that was already finished when the
    # client connected (catches events still settling), before closing.
    SSE_TERMINAL_LINGER_SECONDS = int(
        os.environ.get("SSE_TERMINAL_LINGER_SECONDS", "10")
    )
    DEFAULT_MCMC_GENERATIONS = int(os.environ.get("DEFAULT_MCMC_GENERATIONS", "50000"))
    DEFAULT_MCMC_NRNS = int(os.environ.get("DEFAULT_MCMC_NRNS", "2"))
    DEFAULT_MCMC_CHAINS = int(os.environ.get("DEFAULT_MCMC_CHAINS", "4"))

    # iNaturalist OAuth. The site-wide authorized account writes the
    # "Phylogenetic Tree" observation field back when a tree job finishes.
    INAT_CLIENT_ID = os.environ.get('INAT_CLIENT_ID')
    INAT_CLIENT_SECRET = os.environ.get('INAT_CLIENT_SECRET')
    INAT_CREDENTIALS_FILE = os.environ.get('INAT_CREDENTIALS_FILE', '')
    INAT_TOKEN_FILE = Path(
        os.environ.get('INAT_TOKEN_FILE')
        or (BASE_DIR / 'var' / 'private' / 'inaturalist_token.json')
    )
    INAT_OAUTH_REDIRECT_URI = os.environ.get(
        'INAT_OAUTH_REDIRECT_URI', ''
    )
    INAT_PUBLIC_BASE_URL = os.environ.get('INAT_PUBLIC_BASE_URL', '')
    INAT_OAUTH_ADMIN_EMAILS = _csv_env('INAT_OAUTH_ADMIN_EMAILS')

    # Site-wide Mushroom Observer account used to post completed tree links.
    MUSHROOM_OBSERVER_API_KEY = os.environ.get('MUSHROOM_OBSERVER_API_KEY', '')

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
