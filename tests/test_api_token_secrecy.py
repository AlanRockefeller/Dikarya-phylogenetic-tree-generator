"""A newly minted API bearer token must never enter the Flask session.

Flask's session is a *signed* cookie, not an encrypted one: anything put in it
is readable by the browser and by anything that captures the cookie. The
database deliberately stores only the SHA-256 hash, so writing the plaintext
into the session gave the secret a second, weaker home. It is now returned
directly in the response to the POST that created it, once, with no-store.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, session

from app.user import routes as user_routes


def _undecorated(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class TokenCreationSecrecyTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"
        self.plaintext = "dikarya_pat_supersecretvalue"

    def _create(self, form):
        rendered = {}

        def _fake_render(template, **context):
            rendered.update(context)
            rendered["template"] = template
            return f"<html>{context.get('new_secret') or ''}</html>"

        with (
            self.app.test_request_context(method="POST", data=form),
            patch.object(user_routes, "render_template", _fake_render),
            patch.object(user_routes, "ApiToken", MagicMock()),
            patch.object(user_routes, "db", MagicMock()),
            patch.object(user_routes, "generate_token",
                         return_value=(self.plaintext, "h" * 64, "dikarya_pat_")),
            patch.object(user_routes, "current_user",
                         SimpleNamespace(id=1, is_authenticated=True)),
        ):
            response = _undecorated(user_routes.api_tokens_create)()
            session_snapshot = dict(session)
        return response, rendered, session_snapshot

    def test_plaintext_never_reaches_the_session(self):
        response, rendered, session_snapshot = self._create(
            {"name": "laptop", "scopes": "jobs:read"})

        flat = repr(session_snapshot)
        self.assertNotIn(self.plaintext, flat)
        self.assertNotIn("new_token_secret", session_snapshot)
        self.assertNotIn("new_token_name", session_snapshot)
        # ...and it is shown exactly once, in this response.
        self.assertEqual(rendered["new_secret"], self.plaintext)
        self.assertIn(self.plaintext.encode(), response.get_data())

    def test_the_response_carrying_the_secret_is_not_cacheable(self):
        response, _rendered, _session = self._create(
            {"name": "laptop", "scopes": "jobs:read"})

        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.status_code, 200)

    def test_only_the_hash_is_stored(self):
        with (
            self.app.test_request_context(
                method="POST", data={"name": "laptop", "scopes": "jobs:read"}),
            patch.object(user_routes, "render_template", lambda *a, **k: "x"),
            patch.object(user_routes, "ApiToken") as fake_model,
            patch.object(user_routes, "db", MagicMock()),
            patch.object(user_routes, "generate_token",
                         return_value=(self.plaintext, "h" * 64, "dikarya_pat_")),
            patch.object(user_routes, "current_user",
                         SimpleNamespace(id=1, is_authenticated=True)),
        ):
            _undecorated(user_routes.api_tokens_create)()

        kwargs = fake_model.call_args.kwargs
        self.assertEqual(kwargs["token_hash"], "h" * 64)
        self.assertNotIn(self.plaintext, repr(kwargs))

    def test_a_plain_get_never_reveals_a_secret(self):
        rendered = {}

        def _fake_render(template, **context):
            rendered.update(context)
            return "<html></html>"

        query = MagicMock()
        query.filter_by.return_value.order_by.return_value.all.return_value = []
        with (
            self.app.test_request_context(method="GET"),
            patch.object(user_routes, "render_template", _fake_render),
            patch.object(user_routes, "ApiToken",
                         SimpleNamespace(query=query, created_at=MagicMock())),
            patch.object(user_routes, "current_user",
                         SimpleNamespace(id=1, is_authenticated=True)),
        ):
            response = _undecorated(user_routes.api_tokens)()
            session_snapshot = dict(session)

        self.assertIsNone(rendered["new_secret"])
        self.assertIsNone(rendered["new_token_name"])
        self.assertEqual(session_snapshot, {})
        self.assertNotIn("Cache-Control", response.headers)

    def test_validation_failures_still_redirect_without_creating_anything(self):
        with (
            self.app.test_request_context(method="POST", data={"name": ""}),
            patch.object(user_routes, "url_for", return_value="/user/tokens"),
            patch.object(user_routes, "generate_token") as gen,
        ):
            response = _undecorated(user_routes.api_tokens_create)()
        self.assertEqual(response.status_code, 302)
        gen.assert_not_called()

    def test_source_no_longer_stashes_the_secret_in_the_session(self):
        source = open(user_routes.__file__).read()
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("session['new_token_secret']", code)
        self.assertNotIn('session["new_token_secret"]', code)
        self.assertNotIn("session.pop('new_token_secret'", code)


if __name__ == "__main__":
    unittest.main()
