"""Decide what is boring by counting who asks for it, not by keeping a list.

``SCANNER_EXACT_PATHS`` in ``security_events.py`` is ~80 hand-maintained
entries and grows every week, because it is trying to enumerate the internet's
vulnerability dictionaries one sweep at a time. That list is still the fast
path, but the maintenance of it is the wrong job for a person, and the thing it
is really trying to express is a fact that can simply be measured:

    A path requested by many unrelated clients is a dictionary entry.
    A path requested by exactly one client, ever, is a guess about *us*.

Scanners work from a list fixed before they ever contacted this host, so they
all ask for the same things -- ``/api/.env`` came from nine distinct clients in
a single day. Nobody else in the world is asking for
``/api/job/aq7c/download/alignment_raw``, because that shape exists only here.

So this module tracks distinct clients per normalised path over a rolling week
and returns one of three verdicts:

* ``dictionary``  -- seen from enough unrelated clients to be sweep vocabulary.
  Demote it: no WARNING, and no score against the actor either.
* ``singleton``   -- nobody else has ever asked for this. If it also looks like
  this app's surface, it is reconnaissance and scores.
* ``emerging``    -- somewhere in between; no opinion, treat as before.

Normalisation matters: ids and digit runs are collapsed so that walking a
thousand job ids does not create a thousand "singleton" paths, each looking
novel. The normalised form is what is counted, and only ever a *hash* of it is
stored, because these paths are attacker-controlled text and some of them
carry a null byte or a traversal string.

Flask-free and side-effect-free, like its two neighbours, so the digest can
import the constants.
"""

import hashlib
import re

# Distinct clients before a path is sweep vocabulary. Low on purpose: three
# unrelated networks asking for the same made-up path is already a dictionary,
# and the cost of being wrong is only that one probe scores nothing.
DICTIONARY_CLIENTS = 5

# Stop counting members here. The verdict cannot change above this, and an
# unbounded set per path is a memory cost an attacker chooses the size of.
CLIENT_CAP = 8

WINDOW_SECONDS = 7 * 24 * 3600

KEY_PREFIX = "dikarya:pathcrowd"

VERDICT_DICTIONARY = "dictionary"
VERDICT_SINGLETON = "singleton"
VERDICT_EMERGING = "emerging"

# Collapse the parts that vary per attempt so enumeration does not look like a
# thousand different novel paths. Job ids are the important case: both shapes
# (UUID and short base36) fold to one token.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_DIGITS_RE = re.compile(r"\d+")
_SHORT_ID_RE = re.compile(r"^[a-z0-9]{4,12}$")

MAX_PATH_LENGTH = 200


def normalize_path(path):
    """A stable, comparable shape for one requested path.

    Lowercased, trailing slash dropped, ids and numbers replaced by tokens.
    The result is never logged or returned to anyone -- it is hashed into a key
    -- so it does not need to be safe to print, only consistent.
    """
    text = str(path or "/").split("?", 1)[0].lower()[:MAX_PATH_LENGTH]
    text = _UUID_RE.sub("<id>", text)
    segments = []
    for segment in text.split("/"):
        if not segment:
            continue
        if _DIGITS_RE.fullmatch(segment):
            segment = "<n>"
        elif _SHORT_ID_RE.fullmatch(segment) and any(ch.isdigit() for ch in segment):
            # A short base36 job id. Requiring a digit keeps real word
            # segments ("alignment", "download", "view") intact, which is what
            # makes /job/<id>/download distinguishable from /job/<id>/view.
            segment = "<id>"
        else:
            segment = _DIGITS_RE.sub("<n>", segment)
        segments.append(segment)
    return "/" + "/".join(segments)


def path_key(path):
    """Redis key for a path. Hashed: the raw path is attacker-controlled."""
    digest = hashlib.sha256(normalize_path(path).encode("utf-8", "replace"))
    return f"{KEY_PREFIX}:{digest.hexdigest()[:24]}"


def verdict_for(distinct_clients):
    if distinct_clients >= DICTIONARY_CLIENTS:
        return VERDICT_DICTIONARY
    if distinct_clients <= 1:
        return VERDICT_SINGLETON
    return VERDICT_EMERGING


# Add this client and report how many distinct ones have asked, atomically and
# without letting the set grow past the cap or lose its TTL.
_OBSERVE_SCRIPT = """
local cap = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
if redis.call('SCARD', KEYS[1]) < cap then
    redis.call('SADD', KEYS[1], ARGV[3])
end
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], window)
end
return redis.call('SCARD', KEYS[1])
"""


class PathCrowd:
    """Rolling distinct-client count per normalised path.

    Without Redis every path looks novel, which would turn ordinary sweeps into
    singletons and defeat the point -- so the no-Redis answer is ``emerging``
    (no opinion), never ``singleton``. Failing closed here means failing
    *quiet*, which is the right direction for a signal whose whole purpose is
    to suppress noise.
    """

    def __init__(self, redis_factory=None, *, window_seconds=WINDOW_SECONDS,
                 client_cap=CLIENT_CAP, on_error=None):
        self.redis_factory = redis_factory
        self.window_seconds = window_seconds
        self.client_cap = client_cap
        self.on_error = on_error

    def observe(self, path, client):
        """Record that ``client`` asked for ``path``; return a verdict."""
        if self.redis_factory is None:
            return VERDICT_EMERGING
        try:
            count = self.redis_factory().eval(
                _OBSERVE_SCRIPT, 1, path_key(path),
                self.client_cap, self.window_seconds,
                hashlib.sha256(str(client or "-").encode()).hexdigest()[:16],
            )
            return verdict_for(int(count or 0))
        except Exception as exc:  # noqa: BLE001 - any client/socket failure
            if self.on_error is not None:
                try:
                    self.on_error(exc)
                except Exception:
                    pass
            return VERDICT_EMERGING
