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
    """The Fernet cipher used for tokens at rest."""
    from cryptography.fernet import Fernet

    key = (current_app.config.get("INAT_TOKEN_ENCRYPTION_KEY") or "").strip()
    if not key:
        # Fail closed in production. These rows hold live OAuth grants that can
        # write to a user's iNaturalist account, and deriving their key from
        # SECRET_KEY would tie them to a value chosen for session signing and
        # never checked for strength -- so a weak or shared SECRET_KEY would
        # silently become the encryption key too. Dev and tests still derive one
        # so the feature runs without extra setup.
        if not current_app.debug and not current_app.testing:
            raise InatAuthError(
                "INAT_TOKEN_ENCRYPTION_KEY is not set. Generate one with "
                "`python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"` and set it before "
                "connecting an iNaturalist account."
            )
        secret = str(current_app.config.get("SECRET_KEY") or "")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()).decode()
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt_secret(value: str) -> str:
    """Encrypt a token for storage."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """Decrypt a stored token, returning None if it cannot be read."""
    if not value:
        return None
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def get_credential(user_id: int) -> Optional[InatUserCredential]:
    """The stored iNaturalist grant for a Dikarya user, if any."""
    return InatUserCredential.query.filter_by(user_id=user_id).first()


def upsert_credential(user_id: int, access_token: str, *, inat_login: str,
                      inat_user_id: Optional[int], scope: str = "",
                      jwt: Optional[str] = None) -> InatUserCredential:
    """Store or replace a user's iNaturalist grant."""
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
    """Delete a user's grant. True if one was removed."""
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
    """Connection summary for the page: connected flag and iNat login."""
    cred = get_credential(user_id)
    if cred is None:
        return {"connected": False, "inat_login": None}
    return {"connected": True, "inat_login": cred.inat_login,
            "connected_at": cred.created_at.isoformat() if cred.created_at else None}
