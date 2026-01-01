from flask import Blueprint

bp = Blueprint('journal', __name__)

from app.journal import routes
