"""Per-user iNaturalist credentials for Voucher Sync.

The site-wide OAuth grant in ``inaturalist_oauth_service`` belongs to one
admin account and lives in a file. Voucher Sync writes to *each user's own*
observations, so every Dikarya user connects their own iNaturalist account.
Those grants are stored in ``inat_user_credential`` (one row per user) with
the access token encrypted at rest, and are read by both the web process
(connect/disconnect/status) and the RQ worker (scan/apply jobs) -- which is
why they are not in the Flask session.

Nothing in this module ever logs or returns the raw token to a caller other
than the worker that needs it for the Bearer header.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Any, Dict, Optional

from flask import current_app

from app.extensions import db
from app.models import InatUserCredential
from app.services.inaturalist_oauth_service import (
    JWT_TTL_SECONDS, InatAuthError, mint_api_jwt,
)

logger = logging.getLogger(__name__)


def _fernet():
    from cryptography.fernet import Fernet

    key = (current_app.config.get("INAT_TOKEN_ENCRYPTION_KEY") or "").strip()
    if not key:
        # Derive from SECRET_KEY so a deployment without the dedicated key
        # still encrypts. Rotating SECRET_KEY then just forces a reconnect.
        secret = str(current_app.config.get("SECRET_KEY") or "")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()).decode()
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def get_credential(user_id: int) -> Optional[InatUserCredential]:
    return InatUserCredential.query.filter_by(user_id=user_id).first()


def upsert_credential(user_id: int, access_token: str, *, inat_login: str,
                      inat_user_id: Optional[int], scope: str = "",
                      jwt: Optional[str] = None) -> InatUserCredential:
    cred = get_credential(user_id)
    if cred is None:
        cred = InatUserCredential(user_id=user_id)
        db.session.add(cred)
    cred.access_token_enc = encrypt_secret(access_token)
    cred.inat_login = inat_login
    cred.inat_user_id = inat_user_id
    cred.scope = scope or ""
    if jwt:
        cred.jwt_enc = encrypt_secret(jwt)
        cred.jwt_created_at = int(time.time())
    else:
        cred.jwt_enc = None
        cred.jwt_created_at = None
    db.session.commit()
    return cred


def clear_credential(user_id: int) -> bool:
    cred = get_credential(user_id)
    if cred is None:
        return False
    db.session.delete(cred)
    db.session.commit()
    return True


def get_user_jwt(user_id: int) -> str:
    """Return a usable API JWT for the user, minting a new one when the cached
    JWT is older than ``JWT_TTL_SECONDS``. A 401 from iNaturalist means the
    grant was revoked: the row is deleted and InatAuthError raised so the UI
    prompts for a reconnect."""
    cred = get_credential(user_id)
    if cred is None:
        raise InatAuthError("Connect your iNaturalist account first.")
    cached = decrypt_secret(cred.jwt_enc)
    created = cred.jwt_created_at or 0
    if cached and (time.time() - created) < JWT_TTL_SECONDS:
        return cached
    access_token = decrypt_secret(cred.access_token_enc)
    if not access_token:
        db.session.delete(cred)
        db.session.commit()
        raise InatAuthError("Stored iNaturalist credential could not be read; please reconnect.")
    try:
        jwt = mint_api_jwt(access_token)
    except InatAuthError as exc:
        if "401" in str(exc):
            db.session.delete(cred)
            db.session.commit()
            raise InatAuthError("iNaturalist revoked this connection; please reconnect.") from exc
        raise
    cred.jwt_enc = encrypt_secret(jwt)
    cred.jwt_created_at = int(time.time())
    db.session.commit()
    return jwt


def credential_status(user_id: int) -> Dict[str, Any]:
    cred = get_credential(user_id)
    if cred is None:
        return {"connected": False, "inat_login": None}
    return {"connected": True, "inat_login": cred.inat_login,
            "connected_at": cred.created_at.isoformat() if cred.created_at else None}
