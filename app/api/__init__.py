from importlib.util import find_spec

from flask import Blueprint

bp = Blueprint('api', __name__)

from app.api import routes

if find_spec("app.dosage") is not None:
    from app.dosage import routes as dosage_routes
