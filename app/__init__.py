import sys
try:
    __import__('pysqlite3')
    sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
    pass

from flask import Flask
from app.config import config

def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Initialize extensions
    from app.extensions import db, login_manager, migrate
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    # Register Blueprints
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
    
    # Register CLI commands
    from app import cli
    cli.register(app)

    return app
