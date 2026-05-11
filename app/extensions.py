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
# 'strong' = flask-login deletes the session if the request fingerprint
# (IP + UA) changes after login. Stolen cookies used from a different
# network get invalidated instead of impersonating the original user.
login_manager.session_protection = 'strong'

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[]
)
