from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'
# Keep fingerprint changes from logging users out when their IP address or
# browser changes. Basic protection only marks the session as non-fresh.
login_manager.session_protection = 'basic'

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[]
)
