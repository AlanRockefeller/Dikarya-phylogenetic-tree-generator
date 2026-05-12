import sys
try:
    __import__('pysqlite3')
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    pass

from flask import Flask, send_from_directory
from app.config import config

def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])

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

    @app.route('/favicon.ico/<path:filename>')
    def favicon_asset(filename):
        return send_from_directory(app.static_folder + '/favicon.ico', filename)

    # Configure Logging for Gunicorn
    import logging
    if __name__ != '__main__':
        gunicorn_logger = logging.getLogger('gunicorn.error')
        if gunicorn_logger.handlers:
            app.logger.handlers = gunicorn_logger.handlers
            app.logger.setLevel(gunicorn_logger.level)
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

    # Security headers. CSP is intentionally NOT set here because templates
    # use inline scripts that would need nonces — that's a bigger task. The
    # four below are free wins:
    #   X-Content-Type-Options: stops MIME-sniffing-based XSS
    #   X-Frame-Options       : blocks clickjacking via iframes
    #   Referrer-Policy       : strips referer on cross-origin nav
    #   Strict-Transport-Security: forces HTTPS for a year on all subdomains
    @app.after_request
    def _set_security_headers(response):
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
