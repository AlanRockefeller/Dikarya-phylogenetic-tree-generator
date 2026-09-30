from importlib.util import find_spec

from flask import Blueprint

bp = Blueprint('api', __name__)

from app.api import routes
from app.api import voucher_sync_routes  # noqa: E402,F401

if find_spec("app.dosage") is not None:
    from app.dosage import routes as dosage_routes
