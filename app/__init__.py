import sys
try:
    __import__('pysqlite3')
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    pass

from flask import Flask, abort, send_from_directory
from app.config import config


def _install_logging(app, under_gunicorn=False):
    """Attach request context to log records and split WARNING+ into its own file.

    Alan 8/14/26 - Two findings from reading production logs:

    * error.log was 98.5% INFO (263 of 267 lines), and a single line --
      "Validated Mycomap URL, extracted blast_id" -- was 37% of the whole file.
      Real errors were buried, which is a large part of why the internal-node
      reroot bug went unnoticed across two users and a week. WARNING and above now
      also go to var/logs/errors.log, which is the "what is broken" view. Nothing
      is removed from error.log, so this cannot lose information.
    * Error lines had no job/user/request, making "who hit this?" a multi-step
      correlation. RequestContextFilter fixes that for every handler.

    Alan 8/15/26 - Attaching that mirror to the root logger silently switched off
    worker logging: it left the root logger at its default WARNING (killing every
    app.* INFO record, the per-job pipeline.log capture, and job.started /
    job.completed), and it made RQ's _has_effective_handler() believe RQ logging
    was already configured, so RQ never installed the stdout/stderr handlers that
    fill worker.log. Both are now set explicitly instead of being inherited from
    a logging.basicConfig() call that had become a no-op.
    """
    import logging
    from app.services.log_context import (
        ContextFormatter, ensure_root_level, install_console_logging,
        install_error_mirror, install_record_factory, install_rq_logging,
        new_request_id,
    )

    # Stamp context onto every record created anywhere in this process.
    install_record_factory()

    root = logging.getLogger()

    # Show the context in the existing logs too (error.log via Gunicorn's handler),
    # keeping each handler's own format and only appending the context suffix.
    for handler in list(root.handlers) + list(app.logger.handlers):
        existing = handler.formatter
        if isinstance(existing, ContextFormatter):
            continue
        try:
            handler.setFormatter(ContextFormatter(
                getattr(existing, '_fmt', None) or "%(message)s",
                getattr(existing, 'datefmt', None),
            ))
        except Exception:
            # Never let a formatting tweak stop the app from starting.
            pass

    # INFO must reach the root logger for the per-job pipeline.log handlers and
    # the worker console to see anything. Handler levels, not the root level, are
    # what keep errors.log at WARNING and above.
    ensure_root_level(logging.INFO)

    errors_path = app.config.get('ERROR_LOG_PATH')
    if errors_path:
        try:
            install_error_mirror(errors_path)
        except OSError as exc:
            app.logger.warning("Could not open errors log %s: %s", errors_path, exc)

    # Gunicorn already owns stdout/stderr for the web process and app.logger
    # already points at its handlers, so a console handler there would duplicate
    # every line into error.log. The worker, the metrics collector and CLI
    # commands have no such owner and need one.
    if not under_gunicorn:
        install_console_logging(logging.INFO)
        install_rq_logging(logging.INFO)
        # Flask attaches its own stderr handler to app.logger. Left in place it
        # writes every app.logger record to worker.log a second time, in a
        # different format, which the digest then counts twice. app.logger
        # propagates to the root logger, so nothing is lost by dropping it.
        try:
            from flask.logging import default_handler

            app.logger.removeHandler(default_handler)
        except Exception:
            pass

    @app.before_request
    def _assign_request_id():
        from flask import g
        g.request_id = new_request_id()


def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Alan 8/14/26 - Trust exactly one proxy hop (nginx on this host).
    #
    # Without this, request.remote_addr is 127.0.0.1 for EVERY request, because
    # that is the address nginx proxies from. Every IP-keyed rate limit was
    # therefore one bucket shared by the entire internet: the "20 per hour"
    # anonymous limit on /api/inaturalist/tree/preview was consumed collectively,
    # which is why real users were served 429s after a handful of previews -- and
    # equally, one abusive client could rate-limit everyone else off the site.
    # It also means per-IP view dedup (WhatsNewView) was counting all visitors as
    # a single viewer.
    #
    # x_for=1 takes the rightmost X-Forwarded-For entry. nginx sets that header
    # with $proxy_add_x_forwarded_for, which appends the real peer address, so the
    # rightmost value is the one nginx observed and cannot be spoofed by a client.
    # Gunicorn binds 127.0.0.1 only, so nginx is the sole possible source.
    if app.config.get('TRUST_PROXY_HEADERS', True):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Fail loud if SECRET_KEY is missing or still the dev fallback in production.
    # SECRET_KEY signs session cookies, CSRF tokens, and remember-me cookies; a
    # weak or known value lets attackers forge any user's session. Refuse to
    # boot rather than silently run insecure.
    if not app.debug and not app.testing:
        secret = app.config.get('SECRET_KEY')
        if not secret or secret == 'dev-key-please-change':
            raise RuntimeError(
                "SECRET_KEY is not set to a strong random value. "
                "Generate one with `python -c \"import secrets; print(secrets.token_urlsafe(64))\"` "
                "and set it in the environment before starting the app."
            )

    # Alan 8/14/26 - Refuse to run against the SQLite fallback unless asked to.
    #
    # Without DATABASE_URL, SQLALCHEMY_DATABASE_URI silently becomes a local
    # app.db file. On the live host that is always wrong and never obvious: the
    # app boots, every query succeeds, and every lookup returns nothing, so a
    # maintenance script reports "job not found" for jobs that plainly exist.
    # Failing here converts that silent wrong answer into an immediate, fixable
    # error. Genuine local dev sets ALLOW_SQLITE_FALLBACK=1 (or just points
    # DATABASE_URL at a sqlite:/// path, which counts as explicit).
    # Read the opt-in from the environment here rather than trusting the value
    # Config captured at import time: the config module is imported the first time
    # anything touches `app`, which can be well before a caller gets to set it.
    import os as _os
    _allow_sqlite = (
        app.config.get('ALLOW_SQLITE_FALLBACK')
        or _os.environ.get('ALLOW_SQLITE_FALLBACK', '') in ('1', 'true', 'True', 'yes')
    )
    if not app.config.get('DATABASE_URL_IS_EXPLICIT') and not _allow_sqlite:
        raise RuntimeError(
            "DATABASE_URL is not set, so the app would fall back to the local "
            f"SQLite file {app.config.get('SQLALCHEMY_DATABASE_URI')!r}. On this "
            "host that is a stale scratch database: it boots fine and returns "
            "empty results for everything, which looks like missing data rather "
            "than a misconfiguration.\n"
            "  Running a script or CLI command? Load the environment first:\n"
            "    set -a && . ~/.dikarya/env && set +a\n"
            "  Really want the local SQLite database? Set ALLOW_SQLITE_FALLBACK=1."
        )

    @app.route('/favicon.ico/<path:filename>')
    def favicon_asset(filename):
        return send_from_directory(app.static_folder + '/favicon.ico', filename)

    # Browsers and pinned/mobile launchers probe these conventional root URLs
    # even when the page declares explicit icon links.
    @app.route('/favicon.ico')
    def favicon_root():
        return send_from_directory(app.static_folder + '/favicon.ico', 'favicon.ico')

    @app.route('/apple-touch-icon.png')
    @app.route('/apple-touch-icon-precomposed.png')
    def apple_touch_icon_root():
        return send_from_directory(
            app.static_folder + '/favicon.ico', 'apple-touch-icon.png'
        )

    @app.route('/site.webmanifest')
    def web_manifest_root():
        return send_from_directory(
            app.static_folder + '/favicon.ico', 'site.webmanifest'
        )

    @app.route('/files/<path:filename>')
    def public_file(filename):
        parts = [part for part in filename.split('/') if part]
        if not parts or any(part.startswith('.') for part in parts):
            abort(404)
        return send_from_directory(app.static_folder + '/files', filename)

    # Configure Logging for Gunicorn
    import logging
    under_gunicorn = False
    if __name__ != '__main__':
        gunicorn_logger = logging.getLogger('gunicorn.error')
        if gunicorn_logger.handlers:
            under_gunicorn = True
            app.logger.handlers = gunicorn_logger.handlers
            app.logger.setLevel(gunicorn_logger.level)

    # Alan 8/14/26 - Request-scoped log context + an errors-only log file.
    _install_logging(app, under_gunicorn=under_gunicorn)
    app.logger.info(
        "event=application.start release=%s config=%s",
        app.config.get("RELEASE_VERSION", "unknown"), config_name,
    )

    # Initialize extensions
    from app.extensions import db, login_manager, migrate, csrf, limiter
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)
    
    # Configure Limiter storage from config
    app.config.setdefault("RATELIMIT_STORAGE_URI", "memory://")
    if app.config.get('REDIS_URL'):
        app.config["RATELIMIT_STORAGE_URI"] = app.config['REDIS_URL']
    limiter.init_app(app)

    # CSRF error handler - return JSON for API routes
    from flask_wtf.csrf import CSRFError
    from flask import request, jsonify

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error="CSRF token missing or invalid", message=e.description), 400
        return "CSRF error", 400

    # 413 handler: return JSON envelope for /api/v1 callers so they don't see
    # a generic HTML "Request Entity Too Large" page. MAX_CONTENT_LENGTH is
    # enforced before routing, so we register on the app, not the blueprint.
    from werkzeug.exceptions import RequestEntityTooLarge

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(e):
        if request.path.startswith("/api/v1/"):
            limit_bytes = app.config.get("MAX_CONTENT_LENGTH") or 0
            limit_mb = limit_bytes / (1024 * 1024)
            return jsonify({
                "error": {
                    "code": "payload_too_large",
                    "message": (
                        f"Request body exceeds the maximum allowed size of "
                        f"{limit_mb:.1f} MB. If you are submitting a FASTA "
                        f"`sequence`, note that the per-field cap is 5 MB; "
                        f"larger inputs should be split into multiple jobs."
                    ),
                }
            }), 413
        return "Request entity too large", 413

    # Register Blueprints
    # Journal blueprint handles the root route and static pages
    from app.journal import bp as journal_bp
    app.register_blueprint(journal_bp)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api')

    from app.auth import bp as auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from app.user import bp as user_bp
    app.register_blueprint(user_bp, url_prefix='/user')

    from app.monitoring import bp as monitoring_bp
    app.register_blueprint(monitoring_bp)

    # Public token-authenticated API. CSRF-exempt as a whole -- protection
    # comes from the bearer token requirement, not from CSRF cookies. The
    # internal /api/ blueprint above stays CSRF-protected for the browser UI.
    from app.api_v1 import bp as api_v1_bp
    csrf.exempt(api_v1_bp)
    app.register_blueprint(api_v1_bp, url_prefix='/api/v1')

    # Inject What's New badge indicator into all templates
    @app.context_processor
    def inject_whats_new_badge():
        try:
            from app.models import WhatsNewEntry, WhatsNewView
            from flask_login import current_user
            from flask import request as req
            latest = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).first()
            if not latest:
                return {'whats_new_has_new': False}
            view_record = None
            if current_user.is_authenticated:
                view_record = WhatsNewView.query.filter_by(user_id=current_user.id).first()
            else:
                view_record = WhatsNewView.query.filter_by(ip_address=req.remote_addr).first()
            has_new = view_record is None or latest.published_at > view_record.last_viewed_at
            return {'whats_new_has_new': has_new}
        except Exception:
            return {'whats_new_has_new': False}

    # Register CLI commands
    from app import cli
    cli.register(app)

    # Alan 8/15/26 - Bind the worker task module at boot, not at first submission.
    #
    # enqueue_job() imports app.workers.tasks lazily. That import runs against the
    # process's sys.modules, so a long-running process whose source changed on disk
    # underneath it resolves a half-updated module. On 2026-08-14 JobContextFilter
    # was added to app/services/log_context.py while web and worker were running:
    # log_context was already imported (stale, no JobContextFilter), so the first
    # lazy import of tasks raised ImportError. For four and a half hours three
    # users got a 500 on POST /api/job and seventeen jobs were accepted with 202
    # and then died in the worker with "Invalid attribute name: run_phylo_job",
    # never creating a var/jobs/<id>/ directory. Two of the four gunicorn workers
    # were unaffected, so the same request succeeded or failed by luck of routing.
    #
    # Importing here makes each process consistent with the source it booted from:
    # it either serves the old code correctly until restarted, or fails to boot at
    # all. It stays after _install_logging() so the import-time records it emits
    # already carry context; tasks.py no longer configures logging itself.
    from app.workers import tasks as _worker_tasks  # noqa: F401

    # Security headers. CSP is intentionally NOT set here because templates
    # use inline scripts that would need nonces, which is a bigger task. The
    # four below are free wins:
    #   X-Content-Type-Options: stops MIME-sniffing-based XSS
    #   X-Frame-Options       : blocks clickjacking via iframes
    #   Referrer-Policy       : strips referer on cross-origin nav
    #   Strict-Transport-Security: forces HTTPS for a year on all subdomains
    @app.after_request
    def _set_security_headers(response):
        from flask import g
        response.headers.setdefault("X-Request-Id", getattr(g, "request_id", "-"))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Only assert HSTS in production (HTTPS); avoid trapping dev users
        # on http://localhost into an HTTPS-only state.
        if not app.debug and not app.testing:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains"
            )
        return response

    return app
