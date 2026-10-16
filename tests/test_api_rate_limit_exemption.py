from types import SimpleNamespace

from flask import Flask, g

from app.api_v1.auth import api_user_is_rate_limit_exempt
from app.config import Config


def test_harte_account_is_exempt_by_default():
    assert "mycology@dikarya.llc" in Config.API_RATE_LIMIT_EXEMPT_EMAILS


def test_api_exemption_matches_authenticated_email_case_insensitively():
    app = Flask(__name__)
    app.config["API_RATE_LIMIT_EXEMPT_EMAILS"] = ["mycology@dikarya.llc"]

    with app.test_request_context():
        g.api_user = SimpleNamespace(email=" Mycology@Dikarya.LLC ")
        assert api_user_is_rate_limit_exempt()


def test_other_api_users_remain_limited():
    app = Flask(__name__)
    app.config["API_RATE_LIMIT_EXEMPT_EMAILS"] = ["mycology@dikarya.llc"]

    with app.test_request_context():
        g.api_user = SimpleNamespace(email="someone@example.com")
        assert not api_user_is_rate_limit_exempt()
