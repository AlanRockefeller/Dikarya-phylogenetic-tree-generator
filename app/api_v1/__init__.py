from flask import Blueprint

bp = Blueprint('api_v1', __name__)

from app.api_v1 import auth, envelope, routes  # noqa: F401,E402
