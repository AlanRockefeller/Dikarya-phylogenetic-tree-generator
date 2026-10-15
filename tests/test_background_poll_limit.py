"""Concurrent read-only result polling must fit within its own rate limit."""
from flask import Flask
from app.api import routes
from app.extensions import limiter
from app.services import background_requests


def test_six_concurrent_previews_fit_but_polling_stays_bounded(monkeypatch):
    app = Flask(__name__)
    app.config.update(SECRET_KEY='test', RATELIMIT_STORAGE_URI='memory://', RATELIMIT_ENABLED=True)
    limiter.init_app(app)
    app.register_blueprint(routes.bp, url_prefix='/api')
    monkeypatch.setattr(background_requests, 'fetch', lambda _id: {'state': 'running'})
    client = app.test_client()
    for n in range(600):
        response = client.get(f'/api/background/task{n % 6}', environ_overrides={'REMOTE_ADDR': '192.0.2.245'})
        assert response.status_code == 202
    assert client.get('/api/background/task0', environ_overrides={'REMOTE_ADDR': '192.0.2.245'}).status_code == 429
