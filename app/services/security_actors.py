"""Score a *client* across requests, instead of judging each request alone.

``security_events.py`` answers one question per request: does this URL contain
something no legitimate Dikarya URL contains? That catches sweeps, and it is
exactly why it cannot catch anyone competent. Somebody who reads
``/api_v1/openapi.json``, notices that job ids are four base36 characters, and
starts walking ``/api/job/<id>/download/alignment`` sends requests that are
individually indistinguishable from a real user's: no traversal, no injection,
valid routes, plausible ids. Every one of those requests classifies as an
ordinary 4xx, correctly, and that is the whole problem.

What separates the two is not the request, it is the actor:

* A user with a stale link hits one missing job. Nobody hits nine.
* A user with a bad FASTA gets one validation rejection. Nobody collects
  fifteen different ones.
* A browser fetches stylesheets. A script reading the URL map never does.
* An internet-wide scanner arrives with a dictionary fixed before it ever
  contacted this host, so it cannot ask for anything it learned *here*.

So each signal below is worth little on its own and a lot repeated, which is
what ``free`` encodes: the first few occurrences score nothing at all, because
the first few are what ordinary use looks like. One WARNING is emitted when the
total crosses ``ESCALATION_THRESHOLD``, and then that actor is silent for
``ALERT_COOLDOWN_SECONDS``, so an escalation is one line to read rather than the
2,617 that yesterday's fake-Googlebot sweep produced.

Deliberately free of Flask, of app config and of import side effects, for the
same reason ``security_events.py`` is: ``scripts/dikarya_log_digest.py``
imports the weights so the digest's idea of a score cannot drift from the app's.
The Redis client is passed in rather than constructed here.

**Nothing derived from a submission may become a signal token.** Tokens land in
Redis and their *counts* reach the log, so they are held to the same rule as
the monitoring views: a job id is fine (ids are not secrets here), a route
pattern is fine, a path is not, and a query string is never touched.
"""

import ipaddress
import logging
import re
from collections import OrderedDict, namedtuple
from threading import Lock
from time import monotonic, time

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# The signals, and what each one is worth
# --------------------------------------------------------------------------

# weight -- points per occurrence beyond the free allowance.
# free   -- occurrences that score nothing, because that many is normal use.
# cap    -- stop counting here; the score is already saturated and an unbounded
#           set is a memory leak an attacker controls the size of.
# kind   -- "distinct" counts distinct tokens (ids, routes, verbs); "count"
#           counts occurrences.
Signal = namedtuple("Signal", "weight free cap kind")

SIGNALS = {
    # --- knowledge nobody arrives with ---
    # A honeytoken exists only in this app's own output (see
    # security_honeytokens.py), so fetching one proves the client read a
    # response and acted on it. One is enough.
    "honeytoken": Signal(weight=100, free=0, cap=8, kind="distinct"),

    # --- the payload shapes security_events.py already recognises ---
    # Still worth immediate points: a traversal attempt is not ambiguous.
    "path_traversal": Signal(weight=40, free=0, cap=16, kind="distinct"),
    "null_byte": Signal(weight=40, free=0, cap=16, kind="distinct"),
    "injection_probe": Signal(weight=40, free=0, cap=16, kind="distinct"),
    "job_id_malformed": Signal(weight=10, free=1, cap=32, kind="distinct"),

    # --- attacks aimed at this application specifically ---
    # Traversal that starts from a job artifact route rather than from the
    # site root. A sweep asks every host on the internet for /etc/passwd; only
    # someone who has looked at Dikarya climbs out of /job/<id>/download/.
    # Escalates on its own, like a honeytoken, because there is no innocent
    # reading of it.
    "path_traversal_app_surface": Signal(weight=100, free=0, cap=16, kind="distinct"),
    # Naming the on-disk artifact layout (input_info.json, tree_state.json,
    # var/jobs/...). Those names are in this repository and in AGENTS.md, and
    # in no scanner dictionary, so the client read something of ours first.
    "artifact_path_probe": Signal(weight=60, free=0, cap=16, kind="distinct"),
    # Naming or trying to feed the binaries the pipeline executes -- MAFFT,
    # RAxML-NG, IQ-TREE, trimAl, MrBayes, BLAST. This is the part of Dikarya
    # that actually runs things, so a probe against it is the most serious
    # shape short of a confirmed escape.
    "tool_exploit_probe": Signal(weight=60, free=0, cap=16, kind="distinct"),
    # Reported by the code that refused the attempt rather than by inspecting
    # a URL: a path that resolved outside var/jobs, a symlink planted in a job
    # directory, a value that would have reached a tool command line as a
    # flag. A refusal here means a real attempt got as far as the check that
    # stopped it, so one is enough.
    "path_escape_refused": Signal(weight=100, free=0, cap=16, kind="distinct"),
    "argument_injection_refused": Signal(weight=100, free=0, cap=16, kind="distinct"),

    # --- enumeration: the attack the short-id design actually invites ---
    # Job ids are guessable by design and the job surface is deliberately not
    # throttled (see the job-id conventions in AGENTS.md), so walking the id
    # space is the expected move. A single valid-shaped id that 404s stays an
    # ordinary stale link, which is why this counts DISTINCT ids and forgives
    # the first two.
    "job_enumeration": Signal(weight=15, free=2, cap=64, kind="distinct"),
    # Distinct jobs whose edit check refused this client. A 401/403 on one job
    # is an ordinary authorization outcome and is NOT reported on its own (see
    # the note in security_events.py); being refused on many different objects
    # is enumeration against jobs that exist.
    "job_edit_denied": Signal(weight=20, free=1, cap=32, kind="distinct"),
    # Distinct (route, developer reason code) pairs that answered 400. Real
    # users produce a couple; a fuzzer produces a catalogue.
    "validation_fuzzing": Signal(weight=8, free=3, cap=32, kind="distinct"),
    # A verb the app serves somewhere, aimed at a route that does not accept
    # it. The UI only ever calls endpoints it knows, so this is hand-driven.
    # Distinct from security_events' "unsupported_method", which is a verb
    # Flask serves nowhere -- that one is sweep signature and stays noise.
    "odd_verb_real_route": Signal(weight=25, free=0, cap=16, kind="distinct"),

    # --- reconnaissance shape ---
    # An unmatched path under this app's own surface that no other client has
    # ever asked for (see security_path_crowd.py). A dictionary entry is by
    # definition requested by many unrelated clients; a guess about *our*
    # routes is requested by one.
    "unique_app_shaped_path": Signal(weight=20, free=1, cap=32, kind="distinct"),
    # Distinct app routes touched by a client that has never fetched a static
    # asset. Browsers fetch CSS. Scripts reading the URL map do not.
    "route_breadth": Signal(weight=6, free=6, cap=64, kind="distinct"),
    # /health/jobs is unauthenticated and publishes live job-id prefixes, which
    # makes it an oracle for which ids exist. The monitoring page polls it
    # every 5s legitimately -- and fetches static assets, so it is exempt by
    # the same browser test route_breadth uses.
    "health_scrape": Signal(weight=3, free=20, cap=None, kind="count"),
    # Distinct user-agent families from one network. 34.27.83.69 rotated
    # YouBot/Amazonbot/ChatGLM-Spider within a single second on 2026-09-15;
    # no real client changes what it is.
    "ua_rotation": Signal(weight=10, free=2, cap=16, kind="distinct"),

    # --- the cheap stuff, kept only so a sweep can still add up ---
    # These fire on internet-wide junk constantly (37 api_surface_probe hits in
    # yesterday's window, all of it `.env` hunting), so they are worth almost
    # nothing each and no longer earn a WARNING of their own.
    # Weight 1 and a hard cap on purpose: the two of them saturated together
    # must stay below ESCALATION_THRESHOLD, so a big dumb .env sweep can never
    # escalate on volume alone. They exist to push an actor over the line when
    # something real is already scoring, not to be the reason for a line.
    "api_surface_probe": Signal(weight=1, free=5, cap=40, kind="distinct"),
    "admin_probe": Signal(weight=1, free=5, cap=40, kind="distinct"),

    # --- evidence, not score ---
    # Which addresses inside the network prefix took part. Zero weight: it
    # exists so the escalation line can say "3 clients" rather than making the
    # reader go back to access.log.
    "client_ips": Signal(weight=0, free=0, cap=16, kind="distinct"),
}

# Fixed order so the Lua script's KEYS layout is stable.
SIGNAL_ORDER = tuple(SIGNALS)

ESCALATION_THRESHOLD = 100

# How long signals accumulate. Long enough that slow, careful mapping adds up;
# short enough that a shared NAT address does not collect a week of strangers.
WINDOW_SECONDS = 6 * 3600

# One line per actor per hour, however hard they keep trying.
ALERT_COOLDOWN_SECONDS = 3600

KEY_PREFIX = "dikarya:actor"

# Tokens are attacker-influenced (a route pattern, a verb, an id), so they are
# bounded and stripped before they become a Redis member or reach a log line.
_TOKEN_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._:/<>-]")
TOKEN_MAX_LENGTH = 80


def sanitize_token(value):
    """Bound a signal token so it cannot inject a log line or bloat a key."""
    text = _TOKEN_UNSAFE_RE.sub("", str(value or ""))[:TOKEN_MAX_LENGTH]
    return text or "-"


def actor_key(remote_addr):
    """The network an actor is scored under, not the single address.

    A competent attacker rotates addresses; yesterday's logs already show one
    rotating user agents. Scoring an exact IP makes that rotation free, so the
    unit is the allocation it is easy to get several addresses inside of: /24
    for IPv4, /64 for IPv6. The exact addresses are still recorded, as the
    zero-weight ``client_ips`` signal, so an escalation can be traced back.

    The cost is that a large NAT is scored as one actor. That is acceptable for
    a WARNING line whose evidence names the addresses involved, and it is the
    right trade against making evasion a matter of asking for another address.
    """
    try:
        address = ipaddress.ip_address(str(remote_addr or "").strip())
    except ValueError:
        return "unknown"
    if address.version == 4:
        return str(ipaddress.ip_network(f"{address}/24", strict=False))
    return str(ipaddress.ip_network(f"{address}/64", strict=False))


def ua_family(user_agent):
    """The product token of a user agent: 'mozilla', 'curl', 'python-requests'.

    Enough to notice rotation between unrelated clients without keeping the
    full string, which is long, attacker-controlled and already in access.log.
    """
    text = str(user_agent or "").strip()
    if not text:
        return "none"
    return sanitize_token(text.split("/", 1)[0].split(" ", 1)[0].lower())[:32]


def score_signals(counts):
    """Total score for a mapping of signal name -> observed count.

    Shared with the digest so a score printed in a report is the same number
    the app computed.
    """
    total = 0
    for name, count in counts.items():
        signal = SIGNALS.get(name)
        if signal is None or not count:
            continue
        chargeable = max(0, int(count) - signal.free)
        if signal.cap is not None:
            chargeable = min(chargeable, signal.cap - signal.free)
        total += signal.weight * chargeable
    return total


def format_evidence(counts, limit=6):
    """The scoring signals, biggest contribution first, for the log line."""
    contributions = []
    for name, count in counts.items():
        signal = SIGNALS.get(name)
        if signal is None or not count or not signal.weight:
            continue
        contribution = score_signals({name: count})
        if contribution:
            contributions.append((contribution, f"{name}={int(count)}"))
    contributions.sort(reverse=True)
    return ",".join(text for _, text in contributions[:limit]) or "-"


Escalation = namedtuple("Escalation", "actor score counts")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

# Add every token and read every counter in one round trip, atomically. The
# adds must not leave a key without a TTL (the bug scanner_burst.py's script
# guards against), and a set at its cap must stop growing rather than keep
# accepting members an attacker chooses.
#
# KEYS -- one per SIGNAL_ORDER entry, in that order.
# ARGV -- window, then quads of (index, kind, cap, token) for each add.
_RECORD_SCRIPT = """
local window = tonumber(ARGV[1])
local i = 2
while i + 3 <= #ARGV do
    local idx = tonumber(ARGV[i])
    local kind = ARGV[i + 1]
    local cap = tonumber(ARGV[i + 2])
    local token = ARGV[i + 3]
    local key = KEYS[idx]
    if kind == 'd' then
        if cap <= 0 or redis.call('SCARD', key) < cap then
            redis.call('SADD', key, token)
        end
    else
        redis.call('INCR', key)
    end
    if redis.call('TTL', key) < 0 then
        redis.call('EXPIRE', key, window)
    end
    i = i + 4
end
local out = {}
for n = 1, #KEYS do
    local kind = redis.call('TYPE', KEYS[n])['ok']
    if kind == 'set' then
        out[n] = redis.call('SCARD', KEYS[n])
    elseif kind == 'string' then
        out[n] = tonumber(redis.call('GET', KEYS[n])) or 0
    else
        out[n] = 0
    end
end
return out
"""


class ActorFallback:
    """Per-process backup for the shared counters, bounded and LRU.

    Same role as ``ScannerBurstFallback``: Redis being briefly unreachable must
    not silently switch detection off, and must not be able to grow without
    limit either. Counts from both sources are combined with max(), so a flap
    loses precision rather than losing the actor.
    """

    def __init__(self, window=WINDOW_SECONDS, max_actors=2048):
        self.window = window
        self.max_actors = max_actors
        self.entries = OrderedDict()
        self.lock = Lock()

    def record(self, actor, adds):
        with self.lock:
            now = monotonic()
            expires, counters = self.entries.pop(actor, (now + self.window, {}))
            if expires <= now:
                expires, counters = now + self.window, {}
            for name, token in adds:
                signal = SIGNALS.get(name)
                if signal is None:
                    continue
                if signal.kind == "distinct":
                    seen = counters.setdefault(name, set())
                    if signal.cap is None or len(seen) < signal.cap:
                        seen.add(token)
                else:
                    counters[name] = counters.get(name, 0) + 1
            self.entries[actor] = (expires, counters)
            while len(self.entries) > self.max_actors:
                self.entries.popitem(last=False)
            return {
                name: len(value) if isinstance(value, set) else int(value)
                for name, value in counters.items()
            }

    def alert_allowed(self, actor, cooldown):
        """True at most once per cooldown, so the fallback path is not chatty."""
        with self.lock:
            marker = ("__alert__", actor)
            last = self.entries.get(marker, (0, 0))[0]
            now = monotonic()
            if last > now:
                return False
            self.entries[marker] = (now + cooldown, 0)
            return True


class ActorScorer:
    """Accumulate signals per actor and report when one crosses the threshold.

    ``redis_factory`` is a callable returning a client, called lazily so a
    missing or unreachable Redis costs nothing until there is something to
    record. Every Redis failure degrades to the in-process fallback and is
    reported once per interval rather than per request.
    """

    def __init__(
        self, redis_factory=None, *, window_seconds=WINDOW_SECONDS,
        threshold=ESCALATION_THRESHOLD, cooldown=ALERT_COOLDOWN_SECONDS,
        on_error=None,
    ):
        self.redis_factory = redis_factory
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.cooldown = cooldown
        self.on_error = on_error
        self.fallback = ActorFallback(window_seconds)

    def _keys(self, actor):
        return [f"{KEY_PREFIX}:{actor}:{name}" for name in SIGNAL_ORDER]

    def _redis_record(self, actor, adds):
        if self.redis_factory is None:
            return None
        client = self.redis_factory()
        argv = [self.window_seconds]
        for name, token in adds:
            signal = SIGNALS[name]
            argv.extend([
                SIGNAL_ORDER.index(name) + 1,
                "d" if signal.kind == "distinct" else "c",
                signal.cap or 0,
                token,
            ])
        keys = self._keys(actor)
        raw = client.eval(_RECORD_SCRIPT, len(keys), *keys, *argv)
        return {
            name: int(value or 0)
            for name, value in zip(SIGNAL_ORDER, raw)
            if value
        }

    def record(self, actor, signals):
        """Record signals for one actor; return an Escalation or None.

        ``signals`` is an iterable of ``(signal_name, token)``. Unknown names
        are ignored rather than raising: this runs in an after_request hook and
        must never be the reason a response fails.
        """
        adds = []
        for name, token in signals or ():
            if name in SIGNALS:
                adds.append((name, sanitize_token(token)))
        if not adds:
            return None

        local = self.fallback.record(actor, adds)
        shared = None
        try:
            shared = self._redis_record(actor, adds)
        except Exception as exc:  # noqa: BLE001 - any client/socket failure
            if self.on_error is not None:
                try:
                    self.on_error(exc)
                except Exception:
                    pass

        counts = dict(local)
        for name, value in (shared or {}).items():
            counts[name] = max(counts.get(name, 0), value)

        score = score_signals(counts)
        if score < self.threshold:
            return None
        if not self._claim_alert(actor):
            return None
        return Escalation(actor=actor, score=score, counts=counts)

    def _claim_alert(self, actor):
        """One escalation line per actor per cooldown, across all workers."""
        if self.redis_factory is not None:
            try:
                claimed = self.redis_factory().set(
                    f"{KEY_PREFIX}:alert:{actor}", int(time()),
                    nx=True, ex=self.cooldown,
                )
                return bool(claimed)
            except Exception as exc:  # noqa: BLE001
                if self.on_error is not None:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
        return self.fallback.alert_allowed(actor, self.cooldown)


class BrowserWitness:
    """Has this actor ever fetched a static asset in this window?

    The cheapest discriminator there is. A browser rendering the tree viewer
    pulls stylesheets, phylotree.js and a font; a script walking the URL map
    pulls none of them, because nothing tells it to. So ``route_breadth`` and
    ``health_scrape`` -- both of which a real user would otherwise trip, the
    monitoring page especially -- only count for clients that have never
    fetched one.

    Both directions are cached in-process for ``cache_seconds``, because
    otherwise this would add a Redis round trip to every single request rather
    than to the handful that are actually being scored. A stale negative costs
    at most a minute of over-counting; a stale positive costs a minute of
    under-counting. Neither matters against a 6-hour window.
    """

    def __init__(self, redis_factory=None, *, ttl=WINDOW_SECONDS,
                 cache_seconds=60, max_actors=4096, on_error=None):
        self.redis_factory = redis_factory
        self.ttl = ttl
        self.cache_seconds = cache_seconds
        self.max_actors = max_actors
        self.on_error = on_error
        self.cache = OrderedDict()
        self.lock = Lock()

    def _cached(self, actor):
        with self.lock:
            entry = self.cache.get(actor)
            if entry is None or entry[0] <= monotonic():
                return None
            return entry[1]

    def _remember(self, actor, value):
        with self.lock:
            self.cache[actor] = (monotonic() + self.cache_seconds, value)
            self.cache.move_to_end(actor)
            while len(self.cache) > self.max_actors:
                self.cache.popitem(last=False)

    def _report(self, exc):
        if self.on_error is not None:
            try:
                self.on_error(exc)
            except Exception:
                pass

    def mark(self, actor):
        """Record that this actor fetched a static asset."""
        if self._cached(actor) is True:
            return
        self._remember(actor, True)
        if self.redis_factory is None:
            return
        try:
            self.redis_factory().set(f"{KEY_PREFIX}:browser:{actor}", 1, ex=self.ttl)
        except Exception as exc:  # noqa: BLE001
            self._report(exc)

    def seen(self, actor):
        cached = self._cached(actor)
        if cached is not None:
            return cached
        if self.redis_factory is None:
            return False
        try:
            value = bool(self.redis_factory().get(f"{KEY_PREFIX}:browser:{actor}"))
        except Exception as exc:  # noqa: BLE001
            self._report(exc)
            # Unknown, and guessing "script" here would score real users during
            # a Redis outage. Assume browser: quiet is the safe direction.
            return True
        self._remember(actor, value)
        return value
