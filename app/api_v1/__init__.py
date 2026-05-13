from flask import Blueprint
from flask_limiter.util import get_remote_address

from app.extensions import limiter

bp = Blueprint('api_v1', __name__)

# Pre-auth IP-keyed limit applied to every /api/v1 route. This sits in
# parallel with the per-token limits on individual routes (different key
# function, different bucket) and exists specifically to bound abuse before
# the token has been validated: a flood of `Authorization: Bearer
# dikarya_<garbage>` requests would otherwise hit the DB token-hash lookup
# on every request without ever incrementing a per-token counter (because
# no token is associated yet).
#
# 120/min per IP is generous enough that real clients sharing a NAT don't
# trip it under normal use, but more than tight enough to stop credential-
# stuffing-style probes.
_ip_limit = limiter.shared_limit(
    "120 per minute",
    scope="api_v1_ip",
    key_func=get_remote_address,
)

from app.api_v1 import auth, envelope, routes  # noqa: F401,E402

# `shared_limit` returns a decorator; applying it to the blueprint registers
# the limit against every endpoint in the blueprint, including unauthenticated
# ones (/health, /openapi.json, /docs).
_ip_limit(bp)
