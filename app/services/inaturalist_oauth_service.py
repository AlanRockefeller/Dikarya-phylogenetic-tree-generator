"""iNaturalist OAuth service.

Handles loading the site-wide app credentials, building the OAuth
authorization URL, exchanging the callback code for an OAuth access token,
fetching an API/JWT token, and persisting tokens server-side with
restrictive permissions. Never logs or returns secrets.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from flask import current_app

logger = logging.getLogger(__name__)

INAT_AUTHORIZE_URL = "https://www.inaturalist.org/oauth/authorize"
INAT_TOKEN_URL = "https://www.inaturalist.org/oauth/token"
INAT_JWT_URL = "https://www.inaturalist.org/users/api_token"
USER_AGENT = "Dikarya Phylogenetic Tree Builder 1.0"
REQUEST_TIMEOUT = 30
JWT_TTL_SECONDS = 60 * 50  # iNat JWTs last ~24h; refresh well before that


class InatAuthError(Exception):
    pass


def _read_credentials_file(path: str) -> Dict[str, str]:
    """Parse the iNat credentials file.

    Accepts three formats:
      - JSON object: {"client_id": "...", "client_secret": "..."}
      - key=value lines (client_id=..., client_secret=...)
      - a single line containing only the client secret
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            raw = f.read().strip()
    except OSError as exc:
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(logger, "oauth_credentials_unreadable", "OAuth credentials file could not be read", exception=type(exc).__name__)
        return {}
    if not raw:
        return {}
    if raw.startswith('{'):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v is not None}
        except json.JSONDecodeError as exc:
            from app.services.log_context import log_degradation_rate_limited
            log_degradation_rate_limited(logger, "oauth_credentials_malformed", "OAuth credentials JSON was malformed", exception=type(exc).__name__)
            return {}
    out: Dict[str, str] = {}
    if '=' in raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            out[k.strip()] = v.strip()
        if out:
            return out
    # Fallback: whole file is the secret.
    return {'client_secret': raw}


def get_client_credentials() -> Dict[str, str]:
    cfg = current_app.config
    creds = _read_credentials_file(cfg.get('INAT_CREDENTIALS_FILE') or '')
    client_id = cfg.get('INAT_CLIENT_ID') or creds.get('client_id')
    client_secret = cfg.get('INAT_CLIENT_SECRET') or creds.get('client_secret')
    if not client_id or not client_secret:
        raise InatAuthError("iNaturalist app credentials are not configured.")
    return {'client_id': client_id, 'client_secret': client_secret}


def _require_redirect_uri() -> str:
    redirect_uri = (current_app.config.get('INAT_OAUTH_REDIRECT_URI') or '').strip()
    if not redirect_uri:
        raise InatAuthError("iNaturalist OAuth redirect URI is not configured.")
    return redirect_uri


def _token_path() -> str:
    path = current_app.config['INAT_TOKEN_FILE']
    return str(path)


def load_tokens() -> Dict[str, Any]:
    path = _token_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError) as exc:
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(logger, "oauth_token_unreadable", "OAuth token file was unreadable or malformed", exception=type(exc).__name__)
        return {}


def save_tokens(data: Dict[str, Any]) -> None:
    """Write the token file atomically, private from the moment it exists.

    The temp file used to be opened with the default umask and only chmod'd
    after `json.dump` had already written the OAuth access token into it. The
    directory is 0700, so this is hardening rather than a known exposure -- but
    a window where a credential file is world-readable is not something to
    leave in place when closing it costs one call. mkstemp creates at 0600.
    """
    path = _token_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    # dir=parent (never the system temp dir) so os.replace stays a rename
    # within one filesystem, which is what makes it atomic.
    fd, tmp = tempfile.mkstemp(dir=parent or '.',
                               prefix=os.path.basename(path) + '.',
                               suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        # Never leave a half-written file holding a live token behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_authorized() -> bool:
    return bool(load_tokens().get('access_token'))


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(state: str) -> str:
    creds = get_client_credentials()
    params = {
        'client_id': creds['client_id'],
        'redirect_uri': _require_redirect_uri(),
        'response_type': 'code',
        'state': state,
    }
    return INAT_AUTHORIZE_URL + '?' + urllib.parse.urlencode(params)


def _log_upstream_error(method: str, url: str, exc) -> None:
    """Record an upstream OAuth failure without recording what it contained.

    The response body of a token exchange is the last place to take text from
    and hand to a user: it is attacker-influencable, it is rendered into a flash
    message, and on some error paths providers echo request parameters -- which
    on this endpoint means the client secret or the authorization code. It is
    equally not something to write to the log verbatim, since these logs are
    read (and pasted) freely.

    So: the status, the length, and a fingerprint. Two identical failures share
    a fingerprint, which is enough to tell "the same error, repeatedly" from
    "something new" without the content ever being stored.
    """
    from app.services.log_context import stable_fingerprint
    body = b''
    try:
        body = exc.read() or b''
    except Exception:
        pass
    logger.warning(
        "event=inat_oauth.upstream_error method=%s url=%s status=%s "
        "body_bytes=%s body_fingerprint=%s",
        method, url, exc.code, len(body),
        stable_fingerprint(body.decode('utf-8', errors='replace')),
    )


def _http_post_json(url: str, form: Dict[str, str], *, bearer: Optional[str] = None,
                    json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if json_body is not None:
        data = json.dumps(json_body).encode('utf-8')
        content_type = 'application/json'
    else:
        data = urllib.parse.urlencode(form).encode('utf-8')
        content_type = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', content_type)
    req.add_header('Accept', 'application/json')
    req.add_header('User-Agent', USER_AGENT)
    if bearer:
        req.add_header('Authorization', f'Bearer {bearer}')
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        _log_upstream_error('POST', url, e)
        raise InatAuthError(f"iNaturalist returned HTTP {e.code}.")
    except urllib.error.URLError as e:
        logger.warning("iNat POST %s failed: %s", url, type(e.reason).__name__)
        raise InatAuthError("iNaturalist could not be reached.")


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    creds = get_client_credentials()
    form = {
        'client_id': creds['client_id'],
        'client_secret': creds['client_secret'],
        'code': code,
        'redirect_uri': _require_redirect_uri(),
        'grant_type': 'authorization_code',
    }
    payload = _http_post_json(INAT_TOKEN_URL, form)
    if 'access_token' not in payload:
        raise InatAuthError("iNaturalist did not return an access token.")
    tokens = load_tokens()
    tokens.update({
        'access_token': payload['access_token'],
        'token_type': payload.get('token_type', 'Bearer'),
        'scope': payload.get('scope', ''),
        'created_at': int(time.time()),
    })
    # Discard any stale JWT.
    tokens.pop('jwt', None)
    tokens.pop('jwt_created_at', None)
    save_tokens(tokens)
    return tokens


def _http_get_json(url: str, bearer: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, method='GET')
    req.add_header('Authorization', f'Bearer {bearer}')
    req.add_header('Accept', 'application/json')
    req.add_header('User-Agent', USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        _log_upstream_error('GET', url, e)
        raise InatAuthError(f"iNaturalist returned HTTP {e.code}.")
    except urllib.error.URLError as e:
        logger.warning("iNat GET %s failed: %s", url, type(e.reason).__name__)
        raise InatAuthError("iNaturalist could not be reached.")


def get_api_jwt() -> str:
    """Return a fresh iNaturalist API JWT, refreshing if needed.

    The /users/api_token endpoint accepts a Bearer OAuth access token and
    returns a short-lived JWT that must be used for write operations on
    observation_field_values.
    """
    tokens = load_tokens()
    access_token = tokens.get('access_token')
    if not access_token:
        raise InatAuthError(
            "iNaturalist is not authorized yet. Visit /tree/oauth/connect "
            "while logged in as an admin to grant access."
        )
    jwt = tokens.get('jwt')
    created = tokens.get('jwt_created_at') or 0
    if jwt and (time.time() - created) < JWT_TTL_SECONDS:
        return jwt
    payload = _http_get_json(INAT_JWT_URL, bearer=access_token)
    new_jwt = payload.get('api_token') or payload.get('access_token')
    if not new_jwt:
        raise InatAuthError("iNaturalist did not return an API token.")
    tokens['jwt'] = new_jwt
    tokens['jwt_created_at'] = int(time.time())
    save_tokens(tokens)
    return new_jwt


def clear_tokens() -> None:
    path = _token_path()
    if os.path.exists(path):
        os.remove(path)
