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

    return app
