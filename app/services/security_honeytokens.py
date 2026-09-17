"""Paths that exist only in this app's own output, so asking for one tells us.

Every other signal in the security modules is statistical: enough stale job
ids, enough validation rejections, enough routes without a stylesheet. A
honeytoken is not. An internet-wide scanner arrives with a dictionary that was
fixed before it ever contacted this host, so it cannot ask for something it
learned *here*. These paths are published in exactly one place each, are
referenced by nothing, are linked from nothing, and no user flow reaches them:

* ``/admin/db-export``    -- a ``Disallow:`` line in ``robots.txt``. A crawler
  that obeys robots.txt never fetches it; a client that reads robots.txt to
  find out what we are hiding fetches it immediately. That is the entire test.
* ``/api/v1/internal/diagnostics-export`` -- a deprecated stub in the OpenAPI
  document. Only reachable by reading the spec and deciding to try the one
  endpoint marked internal.
* ``/job/q0x9`` and anything under it -- a decoy job id in an HTML comment in
  the tree viewer. Reaching it means the client read the page source and went
  looking for job ids to try.

The id is reserved in ``job_id_service.RESERVED_JOB_IDS`` so minting can never
hand it to a real job; if that reservation is ever lost, a real user's job
would be reported as an attacker, which is why the two are asserted equal by
``tests/test_security_honeytokens.py``.

Rules for adding one:

* It must be plausible. A path nobody would try catches nobody.
* It must be published exactly once, somewhere a person has to read our output
  to see. A path that leaks into a template, a sitemap or a link is a
  false-positive generator, not a tripwire.
* It must never do anything. These are matched before routing and answered
  with an ordinary 404, so the response is indistinguishable from any other
  missing path -- someone probing must not be able to tell they tripped it.
"""

# The decoy job id. Shaped exactly like a real short id, reserved at mint time.
DECOY_JOB_ID = "q0x9"

# Exact paths, and prefixes under which everything counts. Kept lowercase;
# matching is case-insensitive.
HONEYTOKEN_EXACT_PATHS = frozenset({
    "/admin/db-export",
    "/api/v1/internal/diagnostics-export",
})

HONEYTOKEN_PREFIXES = (
    f"/job/{DECOY_JOB_ID}",
    f"/api/job/{DECOY_JOB_ID}",
)


def honeytoken_hit(path):
    """Return the token that ``path`` tripped, or None.

    Returns the matched token rather than a bool so the log line and the actor
    signal can say *which* one -- three tokens in three different places tell
    you what the client read, not just that it read something.
    """
    lower = str(path or "").split("?", 1)[0].rstrip("/").lower() or "/"
    if lower in HONEYTOKEN_EXACT_PATHS:
        return lower
    for prefix in HONEYTOKEN_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "/"):
            return prefix
    return None
