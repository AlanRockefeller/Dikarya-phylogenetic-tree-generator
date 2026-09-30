"""
Mycomap FASTA downloader service.

Fetches BLAST result sequences from Mycomap URLs for use in the tree builder.
Based on standalone script by Alan Rockefeller - June 30, 2025.
"""

import html
import html.parser
import base64
import hashlib
import json
import logging
import os
import random
import re
import shlex
import time
import urllib.request
from datetime import datetime, timezone
from app.services.api_diagnostics import diagnostic_urlopen, record_api_failure
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Timeout for network requests in seconds
REQUEST_TIMEOUT = 10

# FASTA fetches are retried on transient upstream faults (5xx, timeouts, connection
# errors). Three attempts with 2s then 4s of backoff stays well inside the job's
# budget while riding out the brief MycoMap 500s seen in production.
FASTA_FETCH_ATTEMPTS = 3
FASTA_FETCH_RETRY_BASE_SECONDS = 2

# Alan 8/15/26 - That full retry budget is only affordable in a worker. A request
# handler fetches both endpoints, so the unbounded worst case is
# 2 x (3 x REQUEST_TIMEOUT + 6s backoff) = 72s spent holding one of only 8
# Gunicorn slots -- long enough to be killed by the worker timeout, so the user
# pays the whole wait and still gets nothing. Interactive callers pass this as a
# deadline covering all of their fetches; retries stop once it is spent.
INTERACTIVE_FETCH_BUDGET_SECONDS = 25

# Mycomap API base URL
MYCOMAP_BASE_URL = "https://mycomap.com/index.php"
MYCOMAP_API_BASE_URL = "https://mycomap.com/api/mycomap"
MYCOMAP_COM_API_KEY_ENV = "MYCOMAP_COM_API_KEY"
MYCOMAP_COM_USER_ID_ENV = "MYCOMAP_COM_USER_ID"
MYCOMAP_DEFAULT_USER_ID = 1
MYCOMAP_DEFAULT_LOCAL_RERUN_LIMIT = 50
MYCOMAP_DEFAULT_NCBI_RERUN_LIMIT = 100
MYCOMAP_RERUN_LIMIT_MIN = 1
MYCOMAP_RERUN_LIMIT_MAX = 500
MYCOMAP_RERUN_REQUEST_TIMEOUT = 60
# Alan 9/23/26 - The BLAST history lookup used to borrow the 60s rerun timeout.
# It runs on every discovery poll of every waiting bulk job, and over 5,400
# logged polls a successful answer took 0.9s median and 3.3s at p90; only ~12
# answered between 30 and 60s. The 26 that timed out held the only bulk worker
# for a full minute each, mostly during one MycoMap outage. A missed lookup is
# harmless -- the job simply looks again on its next poll -- so fail fast.
MYCOMAP_HISTORY_REQUEST_TIMEOUT = 15
# The member history is the same for every job that Dikarya created under the
# configured member. Share one first page for a minute so concurrent workers
# do not each scan it while checking their own pending BLAST.
MYCOMAP_HISTORY_SHARED_CACHE_SECONDS = 60
# A waiter must outlast the fetch it is waiting for, or concurrent lookups
# never actually share one; it stops early once that fetch gives up.
MYCOMAP_HISTORY_SHARED_CACHE_WAIT_SECONDS = MYCOMAP_HISTORY_REQUEST_TIMEOUT + 2
_MYCOMAP_HISTORY_CACHE_KEY = "dikarya:mycomap:blast_history:member:{}:limit:100"
# After one history lookup times out, every other waiting job would time out
# too; a batch of 300 jobs then costs 300 x timeout per pass. Skip the lookup
# for everyone for this long instead. Redis-backed; without Redis it fails open.
MYCOMAP_HISTORY_BACKOFF_SECONDS = 300
_HISTORY_BACKOFF_KEY = "dikarya:mycomap:history_backoff"
MYCOMAP_NCBI_RERUN_WAIT_SECONDS = 600
MYCOMAP_NCBI_POLL_INTERVAL_SECONDS = 60
MYCOMAP_NCBI_POLL_MAX_ATTEMPTS = 120
MYCOMAP_NCBI_LOCAL_FALLBACK_SECONDS = 600
MYCOMAP_NCBI_BACKLOG_FALLBACK_POSITION = 100
MYCOMAP_NCBI_RECHECK_MAX_HOURS = 48
# How long to keep looking for a newly created BLAST's result page before
# giving up. MycoMap answers the create POST with "Job added to queue" and no
# ID, so the record only becomes discoverable once its queue reaches the job.
# MycoMap can leave an accepted search in its queue for well over ten minutes.
# RQ retries do not occupy a worker slot. Check frequently for the first hour,
# then back off while keeping the accepted search alive for four days so a large
# MycoMap backlog does not turn into a failed tree that must be rebuilt by hand.
MYCOMAP_CREATION_DISCOVERY_MAX_SECONDS = 4 * 24 * 60 * 60
# A create POST that times out may or may not have reached MycoMap. Such a
# search is discovered like any other, and re-sent only when the history API
# answers cleanly without it -- at most this many POSTs in total.
MYCOMAP_UNCONFIRMED_CREATE_MAX_ATTEMPTS = 3
MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES = 4

_CONCRETE_DNA_BASES = frozenset("ACGT")
_IUPAC_COMPLEMENT = str.maketrans(
    "ACGTRYSWKMBDHVN",
    "TGCAYRSWMKVHDBN",
)


class MycoMapRerunError(Exception):
    """Raised when the MycoMap BLAST rerun API cannot start or complete."""


class MycoMapCreateError(Exception):
    """Raised when a new MycoMap BLAST search cannot be created."""


class MycoMapRefreshError(Exception):
    """Raised when MycoMap observation records cannot be refreshed or resolved."""


class MycoMapRefreshTimeout(MycoMapRefreshError):
    """A MycoMap refresh-API request ran out of time (MycoMap slow or down)."""


MYCOMAP_OBSERVATION_REF_RE = re.compile(r"^(?:inat|mo):\d{1,12}$")


_INAT_PATTERNS = (
    r"\binat\s*:\s*(\d{1,12})\b",
    r"\b(?:https?://)?(?:www\.)?(?:[a-z0-9-]+\.)*inaturalist(?:\.[a-z0-9-]+)+/observations/(\d{1,12})\b",
    r"\bi\s*naturalist(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{5,12})(?:[_-]\d+)?\b",
    r"\binaturalist(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{5,12})(?:[_-]\d+)?\b",
    r"\binat(?:uralist)?(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{5,12})(?:[_-]\d+)?\b",
)

# Alan 9/14/26 - "MO" is a real two-letter INSDC accession prefix, so the
# compact token MO123456 is BOTH the Mushroom Observer label Dikarya prints on
# tips and a syntactically valid GenBank nucleotide accession (2 letters + 6
# digits -- the single most common accession shape there is). Reading a real
# accession as an observation number is not a cosmetic mistake: observation
# dedup collapses records that share a reference, so a GenBank record and an
# unrelated Mushroom Observer observation whose numbers happen to match would
# have one of them deleted from the tree.
#
# Every OTHER way of writing a Mushroom Observer reference is unambiguous,
# because a GenBank accession has no separator between its letters and its
# digits and never spells the site's name. Those stay in _MO_EXPLICIT_PATTERNS
# and are honoured wherever they appear, including inside a GenBank record's own
# qualifiers -- a submitter who wrote "Mushroom Observer 123456" in an /isolate
# meant it. Only the compact form is gated, by the caller's knowledge of where
# the sequence came from; see allow_compact_mo below.
_MO_EXPLICIT_PATTERNS = (
    r"\bmo\s*:\s*(\d{1,12})\b",
    r"\b(?:https?://)?(?:www\.)?mushroomobserver\.org/(?:obs/)?(\d{1,12})\b",
    r"\bmushroom\s*observer(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{1,12})(?:[_-]\d+)?\b",
    # "MO #123456", "MO-123456", "MO 123456": a separator between the prefix and
    # the digits is exactly what no accession has. The five-digit floor is the
    # one the compact form has always used, and it is what keeps a voucher such
    # as "TENN-MO-45" from being read as observation 45.
    r"\bmo\s*[-_#:/]\s*(\d{5,12})(?:[_-]\d+)?\b",
    r"\bmo\s+(\d{5,12})(?:[_-]\d+)?\b",
)

# The ambiguous one: letters immediately followed by digits, no separator.
_MO_COMPACT_PATTERN = r"\bmo(\d{5,12})(?:[_-]\d+)?\b"


def extract_mycomap_observation_references(
    value: str, *, allow_compact_mo: bool = True
) -> List[str]:
    """Every distinct observation reference in one string, best first.

    The singular form below stops at the first match, which is what a caller
    that only wants to label or fetch something needs. A caller about to
    *delete* a record needs the whole list: a single ``/note`` reading
    "sequenced from iNat 280384724; compare iNat 999999999" names two different
    observations, and taking the first would group the record with one of them
    on no evidence at all.

    Ordering matches the singular form exactly -- every iNaturalist pattern
    before every Mushroom Observer one, patterns in declaration order -- so
    ``extract_mycomap_observation_references(x)[0]`` is always
    ``extract_mycomap_observation_reference(x)``. Asserted in
    tests/test_observation_provenance.py.
    """
    text = html.unescape(str(value or ""))
    if not text:
        return []

    found: List[str] = []

    def _collect(patterns, prefix):
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                reference = f"{prefix}:{match.group(1)}"
                # Several patterns match the same occurrence (an id written
                # "iNat # 280384724" is found by three of them); the same
                # observation named twice is not a conflict either way.
                if reference not in found:
                    found.append(reference)

    _collect(_INAT_PATTERNS, "inat")
    mo_patterns = _MO_EXPLICIT_PATTERNS
    if allow_compact_mo:
        mo_patterns = mo_patterns + (_MO_COMPACT_PATTERN,)
    _collect(mo_patterns, "mo")
    return found


def extract_mycomap_observation_reference(
    value: str, *, allow_compact_mo: bool = True
) -> Optional[str]:
    """Return an ``inat:<id>`` or ``mo:<id>`` reference found in a tip label.

    ``allow_compact_mo=False`` suppresses the bare ``MO123456`` form, which is
    shape-identical to a GenBank accession. Pass it when the text is known to
    have come from GenBank/NCBI, or when the provenance is unknown and the
    answer is about to be used destructively. Explicit Mushroom Observer
    references (``mo:123456``, ``MO #123456``, a mushroomobserver.org URL, the
    site's name spelled out) are unaffected either way.

    The default stays True so display and lookup callers -- which only ever use
    the answer to label or fetch something -- keep behaving as they did.
    """
    text = html.unescape(str(value or ""))
    if not text:
        return None

    for pattern in _INAT_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"inat:{match.group(1)}"

    mo_patterns = _MO_EXPLICIT_PATTERNS
    if allow_compact_mo:
        mo_patterns = mo_patterns + (_MO_COMPACT_PATTERN,)
    for pattern in mo_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"mo:{match.group(1)}"
    return None


def _normalize_dna_for_near_duplicate_comparison(value: str) -> str:
    """Return ungapped uppercase IUPAC DNA for observation-level comparison."""
    text = str(value or "").upper().replace("U", "T").replace("?", "N")
    return re.sub(r"[^ACGTRYSWKMBDHVN]", "", text)


_EDIT_DISTANCE_UNREACHABLE = 1 << 30


def _ambiguity_aware_edit_distance(
    first: str,
    second: str,
    max_distance: Optional[int] = None,
) -> int:
    """Return global edit distance while treating ambiguous bases as unknown.

    When ``max_distance`` is supplied the dynamic programming table is confined
    to the diagonal band that any alignment costing at most ``max_distance``
    must stay inside. Distances up to ``max_distance`` are still exact; anything
    beyond it is reported as ``max_distance + 1``. This keeps a pair of ~1.5 kb
    ITS sequences to a few thousand cell evaluations instead of a few million,
    which matters because the caller runs pairwise over an observation group
    inside a synchronous request.
    """
    first_length = len(first)
    second_length = len(second)

    band = None
    if max_distance is not None:
        # Indels are free on ambiguous bases, so an alignment within budget can
        # still drift one position off the diagonal per ambiguous base.
        free_indels = sum(
            1 for base in first if base not in _CONCRETE_DNA_BASES
        ) + sum(
            1 for base in second if base not in _CONCRETE_DNA_BASES
        )
        band = max_distance + free_indels
        if abs(first_length - second_length) > band:
            return max_distance + 1
        if band >= max(first_length, second_length):
            band = None

    previous = [0]
    for base in second:
        previous.append(previous[-1] + (1 if base in _CONCRETE_DNA_BASES else 0))

    for row, first_base in enumerate(first, start=1):
        delete_cost = 1 if first_base in _CONCRETE_DNA_BASES else 0
        if band is None:
            low, high = 1, second_length
        else:
            low = max(1, row - band)
            high = min(second_length, row + band)
        current = [_EDIT_DISTANCE_UNREACHABLE] * (second_length + 1)
        if low == 1:
            current[0] = previous[0] + delete_cost
        for column in range(low, high + 1):
            second_base = second[column - 1]
            insert_cost = 1 if second_base in _CONCRETE_DNA_BASES else 0
            substitution_cost = (
                0
                if (
                    first_base == second_base
                    or first_base not in _CONCRETE_DNA_BASES
                    or second_base not in _CONCRETE_DNA_BASES
                )
                else 1
            )
            current[column] = min(
                previous[column] + delete_cost,
                current[column - 1] + insert_cost,
                previous[column - 1] + substitution_cost,
            )
        previous = current

    distance = previous[second_length]
    if max_distance is not None and distance > max_distance:
        return max_distance + 1
    return distance


def mycomap_sequence_difference_count(
    first: str,
    second: str,
    max_distance: Optional[int] = None,
) -> Optional[int]:
    """Return the ambiguity-aware distance in the closer sequence orientation.

    ``max_distance`` caps the search: distances at or below it are exact, and a
    more distant pair reports ``max_distance + 1`` rather than its true value.
    """
    normalized_first = _normalize_dna_for_near_duplicate_comparison(first)
    normalized_second = _normalize_dna_for_near_duplicate_comparison(second)
    if not normalized_first or not normalized_second:
        return None

    forward_distance = _ambiguity_aware_edit_distance(
        normalized_first,
        normalized_second,
        max_distance,
    )
    if max_distance is not None and forward_distance == 0:
        return 0
    reverse_complement = normalized_second.translate(_IUPAC_COMPLEMENT)[::-1]
    reverse_distance = _ambiguity_aware_edit_distance(
        normalized_first,
        reverse_complement,
        max_distance,
    )
    return min(forward_distance, reverse_distance)


# Overlap comparison tuning. The anchor is a seed exact-match used to find how
# the two reads line up; several starts are tried so a mismatch inside one seed
# does not sink the comparison.
_OVERLAP_ANCHOR_LENGTH = 25
_OVERLAP_ANCHOR_ATTEMPTS = 8
# A shared region shorter than this proves nothing, and one that covers only
# part of the shorter read means these are not the same stretch of DNA.
_OVERLAP_MIN_BASES = 100
_OVERLAP_MIN_FRACTION = 0.8


def _overlap_anchor_offset(shorter: str, longer: str) -> Optional[int]:
    """Return the offset lining ``shorter`` up against ``longer``, or None.

    The offset is defined so ``shorter[i]`` corresponds to ``longer[i + offset]``.
    Seeds that occur more than once in ``longer`` are skipped: a repeat gives no
    evidence about which copy the read came from.
    """
    span = len(shorter) - _OVERLAP_ANCHOR_LENGTH
    if span < 0:
        return None
    step = max(1, span // _OVERLAP_ANCHOR_ATTEMPTS)
    for start in range(0, span + 1, step):
        seed = shorter[start:start + _OVERLAP_ANCHOR_LENGTH]
        position = longer.find(seed)
        if position < 0 or longer.find(seed, position + 1) >= 0:
            continue
        return position - start
    return None


def _oriented_overlap_distance(
    first: str,
    second: str,
    max_distance: Optional[int],
) -> Optional[int]:
    """Distance over the shared region of two already-normalized sequences."""
    shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
    offset = _overlap_anchor_offset(shorter, longer)
    if offset is None:
        return None

    start = max(0, -offset)
    end = min(len(shorter), len(longer) - offset)
    overlap_length = end - start
    if (
        overlap_length < _OVERLAP_MIN_BASES
        or overlap_length < _OVERLAP_MIN_FRACTION * len(shorter)
    ):
        return None

    return _ambiguity_aware_edit_distance(
        shorter[start:end],
        longer[start + offset:end + offset],
        max_distance,
    )


def mycomap_sequence_overlap_difference_count(
    first: str,
    second: str,
    max_distance: Optional[int] = None,
) -> Optional[int]:
    """Return the distance over the region two reads share, or None if unrelated.

    Unlike :func:`mycomap_sequence_difference_count` this ignores how far each
    read extends past the other: two records of the same DNA trimmed to 584 and
    596 bases score 0 here, where the global distance charges a base per
    overhang and scores 12. Substitutions and indels inside the shared region
    still count normally.

    Returns None when the reads cannot be lined up confidently -- no unique seed
    match, or too little overlap to judge -- so callers can fall back to the
    global comparison rather than treat "unknown" as "identical".
    """
    normalized_first = _normalize_dna_for_near_duplicate_comparison(first)
    normalized_second = _normalize_dna_for_near_duplicate_comparison(second)
    if not normalized_first or not normalized_second:
        return None

    forward = _oriented_overlap_distance(
        normalized_first, normalized_second, max_distance
    )
    if forward == 0:
        return 0
    reverse_complement = normalized_second.translate(_IUPAC_COMPLEMENT)[::-1]
    reverse = _oriented_overlap_distance(
        normalized_first, reverse_complement, max_distance
    )
    candidates = [d for d in (forward, reverse) if d is not None]
    return min(candidates) if candidates else None


def _env_int(name: str, default: int, *, min_value: Optional[int] = None,
             max_value: Optional[int] = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def get_mycomap_rerun_limit(result_type: str = "local") -> int:
    """Return the configured MycoMap BLAST rerun hit limit."""
    result_type = str(result_type or "").strip().lower()
    if result_type == "ncbi":
        return _env_int(
            "MYCOMAP_NCBI_BLAST_RERUN_LIMIT",
            MYCOMAP_DEFAULT_NCBI_RERUN_LIMIT,
            min_value=MYCOMAP_RERUN_LIMIT_MIN,
            max_value=MYCOMAP_RERUN_LIMIT_MAX,
        )
    return _env_int(
        "MYCOMAP_LOCAL_BLAST_RERUN_LIMIT",
        _env_int("MYCOMAP_BLAST_RERUN_LIMIT", MYCOMAP_DEFAULT_LOCAL_RERUN_LIMIT),
        min_value=MYCOMAP_RERUN_LIMIT_MIN,
        max_value=MYCOMAP_RERUN_LIMIT_MAX,
    )


def validate_mycomap_rerun_limit(value, result_type: str = "local") -> Tuple[int, Optional[str]]:
    """Coerce a user-supplied MycoMap rerun limit or return a validation error."""
    default = get_mycomap_rerun_limit(result_type)
    if value in (None, ""):
        return default, None
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default, "MycoMap BLAST hit limit must be a whole number."
    if limit < MYCOMAP_RERUN_LIMIT_MIN or limit > MYCOMAP_RERUN_LIMIT_MAX:
        return default, (
            f"MycoMap BLAST hit limit must be between "
            f"{MYCOMAP_RERUN_LIMIT_MIN} and {MYCOMAP_RERUN_LIMIT_MAX}."
        )
    return limit, None


def get_mycomap_ncbi_rerun_wait_seconds() -> int:
    """Return how long to wait after queueing an async MycoMap NCBI rerun."""
    return _env_int(
        "MYCOMAP_NCBI_RERUN_WAIT_SECONDS",
        MYCOMAP_NCBI_RERUN_WAIT_SECONDS,
        min_value=0,
        max_value=1800,
    )


def get_mycomap_ncbi_poll_interval_seconds() -> int:
    """Return the interval used while waiting for a new NCBI result set."""
    return _env_int(
        "MYCOMAP_NCBI_POLL_INTERVAL_SECONDS",
        MYCOMAP_NCBI_POLL_INTERVAL_SECONDS,
        min_value=60,
        max_value=300,
    )


def get_mycomap_ncbi_poll_max_attempts() -> int:
    """Return the maximum one-minute checks for a newly created BLAST."""
    return _env_int(
        "MYCOMAP_NCBI_POLL_MAX_ATTEMPTS",
        MYCOMAP_NCBI_POLL_MAX_ATTEMPTS,
        min_value=1,
        max_value=720,
    )


def get_mycomap_ncbi_local_fallback_seconds() -> int:
    """
    Return how long to wait for an auto-created BLAST's NCBI results before
    giving up on NCBI and building the tree from local results only.
    """
    return _env_int(
        "MYCOMAP_NCBI_LOCAL_FALLBACK_SECONDS",
        MYCOMAP_NCBI_LOCAL_FALLBACK_SECONDS,
        min_value=60,
        max_value=7200,
    )


def get_mycomap_ncbi_backlog_fallback_position() -> int:
    """Queue depth at which waiting ten minutes before using local hits is futile."""
    return _env_int(
        "MYCOMAP_NCBI_BACKLOG_FALLBACK_POSITION",
        MYCOMAP_NCBI_BACKLOG_FALLBACK_POSITION,
        min_value=1,
        max_value=10000,
    )


def get_mycomap_creation_discovery_max_seconds() -> int:
    """
    Return how long to keep looking for a just-created BLAST's result page
    before giving up. Nothing can be built until the record is discovered:
    local and NCBI results both hang off the same MycoMap BLAST ID.
    """
    return _env_int(
        "MYCOMAP_CREATION_DISCOVERY_MAX_SECONDS",
        MYCOMAP_CREATION_DISCOVERY_MAX_SECONDS,
        min_value=60,
        max_value=7 * 24 * 60 * 60,
    )


def get_mycomap_creation_discovery_poll_interval_seconds(
        elapsed_seconds: int) -> int:
    """Return the next non-blocking discovery interval for an accepted search."""
    elapsed = max(0, int(elapsed_seconds or 0))
    if elapsed < 60 * 60:
        return get_mycomap_ncbi_poll_interval_seconds()
    if elapsed < 6 * 60 * 60:
        return 5 * 60
    if elapsed < 24 * 60 * 60:
        return 15 * 60
    return 60 * 60


def advance_mycomap_creation_discovery(
        details: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Record one completed discovery wait and whether its total budget expired."""
    details = dict(details or {})
    attempt = int(details.get("creation_discovery_attempt") or 0)
    base_interval = get_mycomap_ncbi_poll_interval_seconds()
    elapsed = details.get("creation_discovery_elapsed_seconds")
    if elapsed is None:
        # Compatibility with jobs queued before elapsed time was persisted.
        elapsed = attempt * base_interval
    elapsed = max(0, int(elapsed))
    waited = int(details.get("creation_discovery_next_interval_seconds") or 0)
    if waited <= 0:
        waited = get_mycomap_creation_discovery_poll_interval_seconds(elapsed)
    elapsed += waited
    details["creation_discovery_attempt"] = attempt + 1
    details["creation_discovery_elapsed_seconds"] = elapsed
    details["creation_discovery_next_interval_seconds"] = (
        get_mycomap_creation_discovery_poll_interval_seconds(elapsed)
    )
    return details, elapsed >= get_mycomap_creation_discovery_max_seconds()


def get_mycomap_creation_discovery_max_attempts() -> int:
    """Return the number of RQ retries required by the rolling backoff."""
    max_seconds = get_mycomap_creation_discovery_max_seconds()
    elapsed = 0
    attempts = 0
    while elapsed < max_seconds:
        elapsed += get_mycomap_creation_discovery_poll_interval_seconds(elapsed)
        attempts += 1
    return max(1, attempts)


def get_mycomap_ncbi_recheck_max_hours() -> int:
    """
    Return how many hourly rechecks to attempt (after the initial local-only
    fallback) before giving up on ever appending late-arriving NCBI results.
    """
    return _env_int(
        "MYCOMAP_NCBI_RECHECK_MAX_HOURS",
        MYCOMAP_NCBI_RECHECK_MAX_HOURS,
        min_value=1,
        max_value=336,
    )


def get_mycomap_user_id() -> int:
    """Return the MycoMap member that owns API-created BLAST searches."""
    return _env_int(
        MYCOMAP_COM_USER_ID_ENV,
        MYCOMAP_DEFAULT_USER_ID,
        min_value=1,
    )


def _normalize_mycomap_api_key(raw: str, env_name: str) -> str:
    """Normalize common secret-file formats without exposing the key."""
    key = str(raw or "").strip().strip("\"'")
    if not key:
        return ""
    if "\n" in key:
        for line in key.splitlines():
            normalized = _normalize_mycomap_api_key(line, env_name)
            if normalized:
                return normalized
        return ""
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if "=" in key:
        candidate_name, candidate_value = key.split("=", 1)
        candidate_name = candidate_name.strip()
        if candidate_name == MYCOMAP_COM_API_KEY_ENV:
            key = candidate_value.strip().strip("\"'")
    if key.startswith("-u "):
        try:
            parts = shlex.split(key)
        except ValueError:
            parts = []
        if "-u" in parts:
            index = parts.index("-u")
            if len(parts) > index + 1:
                key = parts[index + 1].strip().strip("\"'")
    if key.endswith(":"):
        key = key[:-1]
    return key.strip()


def _mycomap_api_key_info() -> Tuple[str, dict]:
    """Return the configured MycoMap.com API key and safe diagnostics."""
    raw = os.environ.get(MYCOMAP_COM_API_KEY_ENV)
    key = _normalize_mycomap_api_key(raw, MYCOMAP_COM_API_KEY_ENV)
    if key:
        return key, {
            "source": MYCOMAP_COM_API_KEY_ENV,
            "length": len(key),
            "sha256": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
        }
    return "", {"source": "missing", "length": 0, "sha256": ""}


def _mycomap_api_key() -> str:
    """Return the configured MycoMap.com API key, if present."""
    key, _info = _mycomap_api_key_info()
    return key


def _summarize_api_response(parsed_body, raw_body: str) -> str:
    if isinstance(parsed_body, dict):
        for key in ("message", "error", "errorMessage", "status", "errorCode"):
            value = parsed_body.get(key)
            if value:
                return str(value)
    raw_body = " ".join(str(raw_body or "").split())
    return raw_body[:200]


def _iter_create_response_values(value, keys: set):
    """Yield selected values from a possibly nested MycoMap API response."""
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in keys and child not in (None, ""):
                yield child
            yield from _iter_create_response_values(child, keys)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_create_response_values(child, keys)


def _parse_created_mycomap_blast(parsed_body, raw_body: str,
                                  response_url: str = "",
                                  location: str = "") -> dict:
    """Extract a new MycoMap BLAST ID and URL from compatible response shapes."""
    url_keys = {"url", "result_url", "resulturl", "blast_url", "blasturl", "link"}
    id_keys = {"blast_id", "blastid", "result_id", "resultid", "record_id", "recordid", "id"}
    url_candidates = [location, response_url]
    url_candidates.extend(
        str(value) for value in _iter_create_response_values(parsed_body, url_keys)
    )
    for candidate in url_candidates:
        candidate = str(candidate or "").strip()
        if candidate.startswith("/"):
            candidate = urllib.parse.urljoin("https://mycomap.com", candidate)
        # Speculative: url_candidates includes the POST target and the Location
        # header, which are usually not result URLs at all. A miss just falls
        # through to the id_keys scan below, so do not log it as a failure.
        blast_id = validate_mycomap_url(candidate, quiet=True)
        if blast_id:
            return {"blast_id": blast_id, "url": candidate}

    for value in _iter_create_response_values(parsed_body, id_keys):
        match = re.fullmatch(r"(?:r)?(\d+)", str(value).strip(), re.IGNORECASE)
        if match:
            blast_id = match.group(1)
            return {
                "blast_id": blast_id,
                "url": f"https://mycomap.com/genetics/blast-search/r{blast_id}/",
            }

    match = re.search(
        r"https?://(?:[A-Za-z0-9-]+\.)*mycomap\.com/[^\s\"'<>]*\br(\d+)\b[^\s\"'<>]*",
        str(raw_body or ""),
        re.IGNORECASE,
    )
    if match:
        return {"blast_id": match.group(1), "url": match.group(0)}
    raise MycoMapCreateError(
        "MycoMap accepted the BLAST request but did not return a result ID or URL."
    )


def _title_matches_blast_label(label: str, wanted: str) -> bool:
    """Match a MycoMap job label against a title, allowing its " - id" suffix."""
    label = " ".join(str(label or "").split()).strip()
    if not label:
        return False
    return label == wanted or label.startswith(f"{wanted} - ")


# Alan 9/22/26 - The listing page is the only place a new BLAST's result-page
# URL can be read (the history API's rows carry an id but almost never a url,
# and the bare r<id>/ form 404s), and it shows only the newest 25 searches. So
# the cache-busted fetch below has to stay -- MycoMap's 15-minute guest cache
# would let a new record scroll off before we ever saw it. What it must not do
# is run once per waiting job per minute: each uncached hit makes MycoMap
# re-count ~634k rows, and during the 2026-09-22 batch that was a large share
# of the listing-page timeouts. One fresh copy is shared across every job and
# worker for this long.
MYCOMAP_LISTING_SHARED_CACHE_SECONDS = 60
_MYCOMAP_LISTING_CACHE_KEY = "dikarya:mycomap:blast_listing"


def _shared_redis():
    """Redis for cross-process coordination, or None (callers then fail open)."""
    try:
        from app.workers.queue import get_redis_connection
        return get_redis_connection()
    except Exception as exc:
        logger.info("MycoMap coordination skipped; Redis unavailable: %s", exc)
        return None


def _fetch_mycomap_blast_listing(warnings: Optional[list] = None) -> str:
    """Fetch the public BLAST listing page, or "" if it cannot be read."""
    conn = _shared_redis()
    if conn is not None:
        try:
            cached = conn.get(_MYCOMAP_LISTING_CACHE_KEY)
            if cached:
                return cached.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.info("MycoMap listing cache read failed: %s", exc)
    page = _fetch_mycomap_blast_listing_uncached(warnings)
    if page and conn is not None:
        try:
            conn.set(
                _MYCOMAP_LISTING_CACHE_KEY, page.encode("utf-8"),
                ex=MYCOMAP_LISTING_SHARED_CACHE_SECONDS,
            )
        except Exception as exc:
            logger.info("MycoMap listing cache write failed: %s", exc)
    return page


def _fetch_mycomap_blast_listing_uncached(warnings: Optional[list] = None) -> str:
    listing_query = urllib.parse.urlencode({"d": "38", "_": str(time.time_ns())})
    request = urllib.request.Request(
        f"https://mycomap.com/genetics/blast-search/?{listing_query}",
        headers={
            "User-Agent": "Dikarya-TreeBuilder/1.0",
            "Accept": "text/html,*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    try:
        with diagnostic_urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not read the MycoMap BLAST listing page: %s", exc)
        if warnings is not None:
            warnings.append(f"MycoMap BLAST listing page could not be read: {exc}")
        return ""


def find_mycomap_record_url_by_id(blast_id: str,
                                  warnings: Optional[list] = None
                                  ) -> Optional[str]:
    """
    Resolve a BLAST ID to its real result page URL.

    MycoMap result pages live at ``<title-slug>-r<id>/``; the bare
    ``blast-search/r<id>/`` form that can be built from an ID alone always
    404s, so an ID has to be matched back to a full URL on the listing page.
    """
    blast_id = str(blast_id or "").strip()
    if not blast_id.isdigit():
        return None
    page = _fetch_mycomap_blast_listing(warnings)
    if not page:
        return None
    pattern = re.compile(
        r"https?://(?:[A-Za-z0-9-]+\.)*mycomap\.com/genetics/blast-search/"
        r"[A-Za-z0-9._-]*?-?r" + re.escape(blast_id) + r"/",
        re.IGNORECASE,
    )
    match = pattern.search(html.unescape(page))
    if not match:
        return None
    url = match.group(0)
    return url if validate_mycomap_url(url) == blast_id else None


def _history_backoff_active() -> bool:
    conn = _shared_redis()
    if conn is None:
        return False
    try:
        return bool(conn.exists(_HISTORY_BACKOFF_KEY))
    except Exception as exc:
        logger.info("MycoMap history backoff check failed; not skipping: %s", exc)
        return False


def _start_history_backoff() -> None:
    conn = _shared_redis()
    if conn is None:
        return
    try:
        # nx: a lookup that times out during an existing window must not
        # extend it, or a steady trickle of stragglers would keep it shut.
        if conn.set(_HISTORY_BACKOFF_KEY, "1", nx=True,
                    ex=MYCOMAP_HISTORY_BACKOFF_SECONDS):
            logger.warning(
                "event=mycomap.history_backoff MycoMap BLAST history timed out; "
                "skipping history lookups for %ss", MYCOMAP_HISTORY_BACKOFF_SECONDS,
            )
    except Exception as exc:
        logger.info("MycoMap history backoff could not be set: %s", exc)


def _read_cached_mycomap_history(conn, cache_key: str):
    """Read a decoded history payload from Redis, or return None on a miss."""
    try:
        cached = conn.get(cache_key)
    except Exception as exc:
        logger.info("MycoMap history cache read failed: %s", exc)
        return None
    if not cached:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8", errors="replace")
    try:
        payload = json.loads(cached)
    except (TypeError, json.JSONDecodeError):
        logger.info("MycoMap history cache contained invalid JSON")
        return None
    return payload if isinstance(payload, (list, dict)) else None


def _fetch_mycomap_history(member_id: int, fresh: bool = False):
    """Fetch one shared member-history page, coalescing concurrent workers.

    ``fresh`` skips the shared cache and asks MycoMap directly (still refilling
    the cache). Use it when an absent row is taken as evidence of absence: a
    page cached before a timed-out create POST landed would otherwise read as
    "never created" and send a duplicate.
    """
    conn = _shared_redis()
    if conn is None:
        return _mycomap_refresh_request(
            "blast/history",
            data={"member": str(member_id), "limit": "100"},
            timeout=MYCOMAP_HISTORY_REQUEST_TIMEOUT,
        )

    cache_key = _MYCOMAP_HISTORY_CACHE_KEY.format(member_id)
    if fresh:
        payload = _mycomap_refresh_request(
            "blast/history",
            data={"member": str(member_id), "limit": "100"},
            timeout=MYCOMAP_HISTORY_REQUEST_TIMEOUT,
        )
        if isinstance(payload, (list, dict)):
            try:
                conn.set(
                    cache_key,
                    json.dumps(payload, separators=(",", ":")),
                    ex=MYCOMAP_HISTORY_SHARED_CACHE_SECONDS,
                )
            except Exception as exc:
                logger.info("MycoMap history cache write failed: %s", exc)
        return payload

    cached = _read_cached_mycomap_history(conn, cache_key)
    if cached is not None:
        return cached

    lock_key = f"{cache_key}:fetching"
    owner_token = os.urandom(16).hex()
    try:
        owns_lock = bool(conn.set(
            lock_key,
            owner_token,
            nx=True,
            ex=MYCOMAP_HISTORY_REQUEST_TIMEOUT + 5,
        ))
    except Exception as exc:
        logger.info("MycoMap history request lock failed: %s", exc)
        owns_lock = None

    if owns_lock is True:
        try:
            # A concurrent fetch may have filled the cache between our first
            # read and lock acquisition.
            cached = _read_cached_mycomap_history(conn, cache_key)
            if cached is not None:
                return cached
            payload = _mycomap_refresh_request(
                "blast/history",
                data={"member": str(member_id), "limit": "100"},
                timeout=MYCOMAP_HISTORY_REQUEST_TIMEOUT,
            )
            if isinstance(payload, (list, dict)):
                try:
                    conn.set(
                        cache_key,
                        json.dumps(payload, separators=(",", ":")),
                        ex=MYCOMAP_HISTORY_SHARED_CACHE_SECONDS,
                    )
                except Exception as exc:
                    logger.info("MycoMap history cache write failed: %s", exc)
            return payload
        finally:
            # Compare-and-delete so an expired lock acquired by another worker
            # is never removed by this request's cleanup.
            try:
                conn.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    lock_key,
                    owner_token,
                )
            except Exception as exc:
                logger.info("MycoMap history request lock release failed: %s", exc)

    if owns_lock is False:
        deadline = time.monotonic() + MYCOMAP_HISTORY_SHARED_CACHE_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.2)
            cached = _read_cached_mycomap_history(conn, cache_key)
            if cached is not None:
                return cached
            try:
                fetch_running = bool(conn.exists(lock_key))
            except Exception:
                fetch_running = True
            if not fetch_running:
                # The fetch ended without caching anything, so it failed;
                # its owner has already reported (and backed off) why.
                cached = _read_cached_mycomap_history(conn, cache_key)
                if cached is not None:
                    return cached
                raise MycoMapRefreshError(
                    "MycoMap BLAST history lookup by another worker failed; "
                    "retrying on the next scheduled check."
                )
        raise MycoMapRefreshError(
            "MycoMap BLAST history is being checked by another worker; "
            "retrying on the next scheduled check."
        )

    # Redis is unavailable. Preserve the old fail-open behavior and make the
    # request directly; the cache is only a coordination optimization.
    return _mycomap_refresh_request(
        "blast/history",
        data={"member": str(member_id), "limit": "100"},
        timeout=MYCOMAP_HISTORY_REQUEST_TIMEOUT,
    )


# Alan 9/23/26 - Jobs created before creation_pending_blast_id was persisted
# only ever had their search ID in the logs, and the title lookup cannot find
# them again once a batch outgrows history's 100 rows. Every ID the history
# lookup sees is remembered here by title, so the known-ID path can recover it.
# Oldest wins, matching MycoMap's rule for duplicate searches of one sequence.
MYCOMAP_TITLE_ID_TTL_SECONDS = 14 * 24 * 60 * 60
_TITLE_ID_KEY = "dikarya:mycomap:blast_id_by_title:{}"


def _title_id_key(title: str) -> str:
    return _TITLE_ID_KEY.format(" ".join(str(title or "").split()).strip().lower())


def remember_blast_id_for_title(title: str, blast_id) -> None:
    blast_id = str(blast_id or "").strip()
    if not blast_id.isdigit() or not str(title or "").strip():
        return
    conn = _shared_redis()
    if conn is None:
        return
    try:
        key = _title_id_key(title)
        existing = conn.get(key)
        existing = existing.decode() if isinstance(existing, bytes) else existing
        if existing and existing.isdigit() and int(existing) <= int(blast_id):
            conn.expire(key, MYCOMAP_TITLE_ID_TTL_SECONDS)
            return
        conn.set(key, blast_id, ex=MYCOMAP_TITLE_ID_TTL_SECONDS)
    except Exception as exc:
        logger.info("Could not remember MycoMap BLAST %s for %s: %s", blast_id, title, exc)


def recalled_blast_id_for_title(title: str) -> Optional[str]:
    conn = _shared_redis()
    if conn is None or not str(title or "").strip():
        return None
    try:
        value = conn.get(_title_id_key(title))
    except Exception as exc:
        logger.info("Could not recall a MycoMap BLAST for %s: %s", title, exc)
        return None
    value = value.decode() if isinstance(value, bytes) else value
    return value if value and value.isdigit() else None


def _mycomap_title_slug(title: str) -> str:
    """MycoMap's result-page slug for a search title.

    "iNat61372441 DNA Barcode ITS" -> "inat61372441-dna-barcode-its", as in
    blast-search/inat61372441-dna-barcode-its-r660784/.
    """
    return re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")


def find_mycomap_blast_by_known_id(blast_id: str, title: str,
                                   warnings: Optional[list] = None
                                   ) -> Optional[dict]:
    """
    Find a created BLAST whose ID we already hold, without the title lookup.

    Alan 9/23/26 - The history API returns only our newest 100 searches and the
    listing only the site's newest 50, so a bulk batch outgrows both: its
    searches finished on MycoMap but could never be found again, and 324 jobs
    polled for hours (r660784, created 2026-09-22 16:27, was published and
    complete while its job kept waiting). A job that saw its ID once keeps it
    as creation_pending_blast_id; this resolves it directly.

    Asks the status record first so an unfinished search costs no page fetch,
    then tries the listing, then builds <title-slug>-r<id>/ and accepts it only
    when that page answers 200 with our own title -- the slug rule is inferred
    from MycoMap's URLs, so it is verified rather than trusted. An unreadable
    status record only skips that shortcut: the listing and page checks still
    decide, so a status outage cannot hide a finished search for the whole
    polling budget.
    """
    blast_id = str(blast_id or "").strip()
    wanted = " ".join(str(title or "").split()).strip()
    if not blast_id.isdigit() or not wanted:
        return None
    record = fetch_mycomap_blast_record(blast_id)
    if record is not None and _looks_unfinished(_api_text_value(record.get("status"))):
        return None

    url = find_mycomap_record_url_by_id(blast_id, warnings)
    if not url:
        candidate = (
            "https://mycomap.com/genetics/blast-search/"
            f"{_mycomap_title_slug(wanted)}-r{blast_id}/"
        )
        request = urllib.request.Request(
            candidate,
            headers={"User-Agent": "Dikarya-TreeBuilder/1.0",
                     "Accept": "text/html,*/*"},
            method="GET",
        )
        try:
            with diagnostic_urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                # The <title> is in the first few KB of a ~500 KB page.
                head = resp.read(65536).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.info("MycoMap result page %s could not be confirmed: %s",
                        candidate, exc)
            # Only a status record that said so makes the search "finished".
            # Without one, a 404 is the ordinary not-published-yet answer.
            if warnings is not None and record is not None:
                warnings.append(
                    f"MycoMap BLAST {blast_id} is finished but its results page "
                    f"could not be confirmed: {exc}"
                )
            elif warnings is not None and getattr(exc, "code", None) != 404:
                warnings.append(
                    f"MycoMap BLAST {blast_id}'s status could not be read and "
                    f"its results page could not be checked: {exc}"
                )
            return None
        match = re.search(r"<title>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
        page_title = html.unescape(match.group(1)) if match else ""
        page_label = page_title.split(" - BLAST Search", 1)[0]
        if (not _title_matches_blast_label(page_label, wanted)
                or validate_mycomap_url(candidate, quiet=True) != blast_id):
            logger.warning(
                "event=mycomap.known_id_page_mismatch blast_id=%s MycoMap page "
                "for our search did not carry the expected title", blast_id,
            )
            return None
        url = candidate
    logger.info(
        "event=mycomap.found_by_known_id blast_id=%s Found MycoMap BLAST for "
        "title %s by its recorded ID", blast_id, wanted,
    )
    return {"blast_id": blast_id, "url": url, "title": wanted}


def find_mycomap_blast_via_history(title: str,
                                   warnings: Optional[list] = None,
                                   pending_out: Optional[dict] = None,
                                   fresh: bool = False,
                                   ) -> Optional[dict]:
    """
    Find a BLAST record by title through the authenticated history API.

    The public listing page only shows a short window of already-published
    searches, so a queued job can be invisible there for as long as MycoMap's
    BLAST queue is backed up -- and can scroll off it entirely. The history
    endpoint is the authoritative view of jobs we created.

    A lookup that fails outright (bad credentials, MycoMap down) is
    indistinguishable from "not published yet" in the return value, so the
    reason is appended to ``warnings`` when one is supplied. Callers that give
    up after a timeout can then report why they never found the record instead
    of blaming MycoMap's queue.

    MycoMap knows the record's ID before it publishes its result page, and that
    ID is enough to read the search's queue position. When ``pending_out`` is
    supplied it receives that ID under ``blast_id`` so a caller still waiting
    for the page can tell the user where the search sits in the queue, and
    ``id_kind``: ``"blast"`` when it is the result's r<id>, ``"job"`` when it
    is only MycoMap's job id, which is not necessarily the same number and so
    must not be resolved as a result page.

    ``fresh`` bypasses the shared history cache (see _fetch_mycomap_history).
    """
    wanted = " ".join(str(title or "").split()).strip()
    if not wanted:
        return None
    if _history_backoff_active():
        logger.info("MycoMap BLAST history lookup skipped for %s: backing off "
                    "after a recent timeout", wanted)
        if warnings is not None:
            warnings.append("MycoMap BLAST history lookup skipped: MycoMap "
                            "timed out recently")
        return None
    try:
        payload = _fetch_mycomap_history(get_mycomap_user_id(), fresh=fresh)
    except MycoMapRefreshTimeout as exc:
        _start_history_backoff()
        logger.warning("MycoMap BLAST history lookup failed for %s: %s", wanted, exc)
        if warnings is not None:
            warnings.append(f"MycoMap BLAST history lookup failed: {exc}")
        return None
    except MycoMapRefreshError as exc:
        logger.warning("MycoMap BLAST history lookup failed for %s: %s", wanted, exc)
        if warnings is not None:
            warnings.append(f"MycoMap BLAST history lookup failed: {exc}")
        return None
    except Exception as exc:
        logger.warning(
            "Unexpected MycoMap BLAST history error for %s: %s", wanted, exc
        )
        if warnings is not None:
            warnings.append(f"MycoMap BLAST history lookup error: {exc}")
        return None

    for row in _mycomap_result_rows(payload):
        label = _find_api_field(
            row,
            ("title", "jobtitle", "job_title", "blasttitle", "blast_title",
             "name", "label"),
        )
        if not _title_matches_blast_label(label, wanted):
            continue

        # Alan 9/23/26 - The history endpoint exposes job_id/status directly.
        # Once we have a pending job ID, carry it to the next discovery pass;
        # that pass checks /blast/<job_id> instead of rescanning all history.
        row_status = _find_api_field(row, ("status",))
        row_job_id = _find_api_field(row, ("job_id", "jobid")).strip()
        if _looks_unfinished(row_status) and row_job_id.isdigit():
            row_blast_id = re.fullmatch(
                r"(?:r)?(\d+)",
                _find_api_field(
                    row, ("blastid", "blast_id", "resultid", "result_id")
                ).strip(),
                re.IGNORECASE,
            )
            if pending_out is not None:
                if row_blast_id:
                    pending_out["blast_id"] = row_blast_id.group(1)
                    pending_out["id_kind"] = "blast"
                else:
                    pending_out["blast_id"] = row_job_id
                    pending_out["id_kind"] = "job"
            logger.info(
                "MycoMap history found pending BLAST job %s for title %s",
                row_job_id, wanted,
            )
            continue

        url_text = _find_api_field(
            row,
            ("url", "resulturl", "result_url", "blasturl", "blast_url",
             "link", "permalink"),
        )
        if url_text.startswith("/"):
            url_text = urllib.parse.urljoin("https://mycomap.com", url_text)
        # The history API sometimes puts its own collection endpoint in a
        # generic ``url`` field. It is metadata, not a malformed user URL, so
        # do not feed it through the warning-producing result URL validator on
        # every discovery poll.
        history_api_url = f"{MYCOMAP_API_BASE_URL}/blast"
        blast_id = (
            validate_mycomap_url(url_text)
            if url_text.rstrip("/") != history_api_url.rstrip("/")
            else None
        )
        if blast_id:
            remember_blast_id_for_title(wanted, blast_id)
            logger.info(
                "Found MycoMap BLAST %s for title %s via history API",
                blast_id, wanted,
            )
            return {"blast_id": blast_id, "url": url_text, "title": wanted}

        # No usable URL on the row: fall back to its numeric ID and resolve the
        # real result page. The row already matched our exact title under our
        # configured member, so the ID is trustworthy -- what is missing is its URL.
        for id_names in (
            ("blastid", "blast_id", "resultid", "result_id",
             "recordid", "record_id"),
            ("jobid", "job_id"),
            ("id",),
        ):
            match = re.fullmatch(
                r"(?:r)?(\d+)", _find_api_field(row, id_names).strip(), re.IGNORECASE
            )
            if not match:
                continue
            candidate_id = match.group(1)
            if id_names != ("jobid", "job_id"):
                # A job id is not necessarily the BLAST id; "oldest wins"
                # would pin a wrong one, so only BLAST-id fields are kept.
                remember_blast_id_for_title(wanted, candidate_id)
            # Alan 8/5/26 - This used to synthesize blast-search/r<id>/ and check
            # that page for the title. That URL form does not exist on MycoMap
            # (records are <slug>-r<id>/), so the check 404'd every time and the
            # record was discarded no matter how long we waited. Resolve the
            # real slug URL from the listing instead.
            candidate_url = find_mycomap_record_url_by_id(candidate_id, warnings)
            if candidate_url:
                logger.info(
                    "Found MycoMap BLAST %s for title %s via history API ID",
                    candidate_id, wanted,
                )
                return {
                    "blast_id": candidate_id,
                    "url": candidate_url,
                    "title": wanted,
                }
            # This is the normal intermediate state after MycoMap accepts a
            # search but before its public result page is generated.
            logger.info(
                "MycoMap history API returned BLAST %s for title %s, but its "
                "result page URL is not on the listing yet",
                candidate_id, wanted,
            )
            if pending_out is not None:
                pending_out["blast_id"] = candidate_id
                pending_out["id_kind"] = (
                    "job" if id_names == ("jobid", "job_id") else "blast"
                )
            if warnings is not None:
                warnings.append(
                    f"MycoMap reported BLAST record {candidate_id} for this "
                    "search, but its results page has not been published yet"
                )
    return None


def find_mycomap_blast_by_title(title: str,
                                warnings: Optional[list] = None,
                                pending_out: Optional[dict] = None,
                                fresh: bool = False,
                                ) -> Optional[dict]:
    """
    Find the newest MycoMap BLAST record matching an exact job title.

    When ``warnings`` is supplied, any reason the lookup could not complete is
    appended to it -- see ``find_mycomap_blast_via_history``, which also
    documents ``pending_out`` and ``fresh``.
    """
    wanted = " ".join(str(title or "").split()).strip()
    if not wanted:
        return None
    found = find_mycomap_blast_via_history(
        wanted, warnings=warnings, pending_out=pending_out, fresh=fresh
    )
    if found:
        return found
    # The history row already gave us the job ID. It is both cheaper and more
    # reliable to check that one record on the next pass than to fetch another
    # site-wide listing while its result page is still being published.
    if (pending_out or {}).get("blast_id"):
        return None
    page = _fetch_mycomap_blast_listing(warnings)
    if not page:
        return None

    link_pattern = re.compile(
        r"<a\b[^>]*href=['\"](?P<url>[^'\"]*?/genetics/blast-search/[^'\"]*?r\d+/?)[^>]*>"
        r"(?P<label>.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in link_pattern.finditer(page):
        label = html.unescape(re.sub(r"<[^>]+>", " ", match.group("label")))
        if not _title_matches_blast_label(label, wanted):
            continue
        url = urllib.parse.urljoin("https://mycomap.com", html.unescape(match.group("url")))
        blast_id = validate_mycomap_url(url)
        if blast_id:
            return {"blast_id": blast_id, "url": url, "title": wanted}
    return None


# ---------------------------------------------------------------------------
# Finding an existing BLAST instead of creating one
#
# Alan 9/22/26 - From MycoMap's own investigation of the 2026-09-22 batch:
#   * A legacy link, index.php?app=genbank&module=genbank&controller=blast
#     &do=results&db=D&id=N, is NOT a BLAST id. db=42 is the Sequences database,
#     db=39 is GenBank, and N is a record there. The page lists every BLAST whose
#     input sequence hashes to that record's sequence. BLAST ids are nearly
#     contiguous, so treating N as a BLAST id finds a real -- wrong -- search
#     and imports another specimen's hits with no error. Never do that.
#   * In 6 of 12 sampled observations a BLAST of the identical sequence already
#     existed and Dikarya created a duplicate anyway.
# The rule MycoMap gave: read the r<id> links off the results page and prefer
# the oldest one that has results; create a new search only when there are none.
# ---------------------------------------------------------------------------
_LEGACY_RESULTS_DATABASES = frozenset({"39", "42"})
MYCOMAP_SEQUENCES_DATABASE = "42"
# Status reads per lookup. The page lists oldest-first once sorted, and the
# first complete one wins, so a handful covers every real case seen.
_MAX_EXISTING_BLAST_STATUS_CHECKS = 5


def parse_legacy_mycomap_results_url(url: str) -> Optional[Tuple[str, str]]:
    """Return (db, record_id) for a legacy do=results link, else None."""
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except Exception:
        return None
    if (parsed.hostname or "").lower() not in ("mycomap.com", "www.mycomap.com"):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path not in ("", "/", "/index.php"):
        return None
    query = urllib.parse.parse_qs(parsed.query or "")

    def one(name):
        values = query.get(name) or []
        return values[0].strip() if len(values) == 1 else ""

    if (one("app").lower(), one("controller").lower(), one("do").lower()) != (
        "genbank", "blast", "results"
    ):
        return None
    db, record_id = one("db"), one("id")
    if db not in _LEGACY_RESULTS_DATABASES or not record_id.isdigit():
        return None
    return db, record_id


def find_mycomap_blasts_for_sequence_record(db: str, record_id: str,
                                            warnings: Optional[list] = None
                                            ) -> List[Dict[str, str]]:
    """List the BLASTs MycoMap has run on one Sequences/GenBank record's
    sequence, oldest first, as [{"blast_id", "url"}]."""
    db, record_id = str(db or ""), str(record_id or "")
    if db not in _LEGACY_RESULTS_DATABASES or not record_id.isdigit():
        return []
    query = urllib.parse.urlencode({
        "app": "genbank", "module": "genbank", "controller": "blast",
        "do": "results", "db": db, "id": record_id,
    })
    request = urllib.request.Request(
        f"{MYCOMAP_BASE_URL}?{query}",
        headers={"User-Agent": "Dikarya-TreeBuilder/1.0", "Accept": "text/html,*/*"},
        method="GET",
    )
    try:
        with diagnostic_urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning(
            "Could not read MycoMap BLAST results for db=%s record=%s: %s",
            db, record_id, exc,
        )
        if warnings is not None:
            warnings.append(f"MycoMap BLAST list for record {record_id} could not be read: {exc}")
        return []

    found: Dict[str, str] = {}
    for match in re.finditer(
        r"""href=['"]([^'"]*/genetics/blast-search/[A-Za-z0-9._-]*?-?r\d+/?)['"]""",
        page, re.IGNORECASE,
    ):
        url = urllib.parse.urljoin("https://mycomap.com", html.unescape(match.group(1)))
        blast_id = validate_mycomap_url(url, quiet=True)
        if blast_id and blast_id not in found:
            found[blast_id] = url
    return [
        {"blast_id": blast_id, "url": found[blast_id]}
        for blast_id in sorted(found, key=int)
    ]


def _choose_existing_blast(
        candidates: List[Dict[str, str]],
        max_status_checks: Optional[int] = None,
        status_check_deadline: Optional[float] = None,
        ) -> Optional[Dict[str, str]]:
    """Choose the oldest finished candidate, or failing that an unknown one.

    Status-unavailable candidates count as unknown. Only the first
    ``max_status_checks`` (default _MAX_EXISTING_BLAST_STATUS_CHECKS) are asked,
    and no new check starts once ``status_check_deadline`` (time.monotonic())
    has passed -- an interactive caller bounds time, not count, so a normal
    fast MycoMap still gets every candidate asked. If every one asked is still
    unfinished, the first candidate not asked is returned as unknown rather
    than reporting that no search exists.
    """
    candidates = list(candidates or [])
    limit = (_MAX_EXISTING_BLAST_STATUS_CHECKS if max_status_checks is None
             else max(0, int(max_status_checks)))
    checked = []
    fallback = None
    for candidate in candidates[:limit]:
        if (checked and status_check_deadline is not None
                and time.monotonic() >= status_check_deadline):
            break
        checked.append(candidate)
        record = fetch_mycomap_blast_record(candidate["blast_id"])
        if isinstance(record, dict):
            ncbi = record.get("ncbi") if isinstance(record.get("ncbi"), dict) else {}
            status = (_api_text_value(ncbi.get("status"))
                      or _api_text_value(record.get("status"))).lower()
            if status == "complete":
                return dict(candidate, status="complete")
            if _looks_unfinished(status):
                continue
        if fallback is None:
            fallback = dict(candidate, status="unknown")
    if fallback is None and len(candidates) > len(checked):
        fallback = dict(candidates[len(checked)], status="unknown")
    return fallback


def resolve_legacy_mycomap_results_url(
        url: str,
        warnings: Optional[list] = None,
        max_status_checks: Optional[int] = None,
        status_check_deadline: Optional[float] = None,
        ) -> Optional[Dict[str, str]]:
    """Map a legacy do=results link to a current BLAST {"blast_id", "url"}.

    ``warnings`` receives a message when MycoMap's BLAST list could not be read,
    which callers use to tell "unreachable" apart from "no search yet".
    """
    parsed = parse_legacy_mycomap_results_url(url)
    if not parsed:
        return None
    candidates = find_mycomap_blasts_for_sequence_record(*parsed, warnings=warnings)
    chosen = _choose_existing_blast(
        candidates, max_status_checks=max_status_checks,
        status_check_deadline=status_check_deadline,
    )
    logger.info(
        "event=mycomap.legacy_url_resolved db=%s record=%s candidates=%s "
        "chosen=%s status=%s",
        parsed[0], parsed[1], len(candidates),
        (chosen or {}).get("blast_id", "-"), (chosen or {}).get("status", "-"),
    )
    return chosen


def _clean_blast_sequence(value: str) -> str:
    return re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(value or "").upper())


def find_existing_mycomap_blast_for_sequence(reference: str, sequence: str,
                                             warnings: Optional[list] = None
                                             ) -> Optional[Dict[str, str]]:
    """Find a BLAST MycoMap has already run on this observation's sequence.

    MycoMap has no lookup-by-sequence endpoint yet, so this goes through the
    observation's own Sequences record: sequences/batch returns it, and the
    legacy results page for that record lists the BLASTs of its sequence. Reuse
    is only offered when the record's sequence is identical to ``sequence`` --
    otherwise the BLASTs found are of a different sequence. Any failure returns
    None and the caller creates a search exactly as before.
    """
    wanted = _clean_blast_sequence(sequence)
    if not wanted or not MYCOMAP_OBSERVATION_REF_RE.fullmatch(str(reference or "")):
        return None
    try:
        payload = _mycomap_refresh_request(
            "sequences/batch", data={"observations": reference}
        )
    except Exception as exc:
        logger.info("MycoMap sequence lookup for %s failed: %s", reference, exc)
        return None

    rows = _mycomap_result_rows(payload)
    matched_rows = [row for row in rows if _reference_from_api_record(row) == reference]
    outcome = "no_record"
    for row in matched_rows:
        record_id = _find_api_field(
            row, ("record_id", "recordid", "primary_id_field", "sequence_id",
                  "sequenceid", "id"),
        )
        record_sequence = _clean_blast_sequence(_find_api_field(
            row, ("sequence", "dna", "dna_sequence", "its", "its_sequence", "seq"),
        ))
        if not record_id.isdigit():
            outcome = "record_without_id"
            continue
        if record_sequence != wanted:
            outcome = "sequence_differs" if record_sequence else "record_without_sequence"
            continue
        chosen = _choose_existing_blast(find_mycomap_blasts_for_sequence_record(
            MYCOMAP_SEQUENCES_DATABASE, record_id, warnings=warnings,
        ))
        if chosen:
            logger.info(
                "event=mycomap.blast_reused reference=%s record=%s blast_id=%s status=%s",
                reference, record_id, chosen["blast_id"], chosen["status"],
            )
            return chosen
        outcome = "no_existing_blast"
    # Field names are logged so a shape mismatch is diagnosable from the log
    # rather than silently never matching.
    logger.info(
        "event=mycomap.blast_reuse_miss reference=%s outcome=%s rows=%s keys=%s",
        reference, outcome, len(rows),
        ",".join(sorted({str(k) for row in rows[:3] for k in row})) or "-",
    )
    return None


# ---------------------------------------------------------------------------
# Bulk creation throttle
#
# Alan 9/22/26 - One batch created ~354 MycoMap BLASTs in four hours (a normal
# day is 1-10), 160 of them in a single hour, and left 81 waiting on NCBI for up
# to 3h43m. MycoMap asked for at most ~20 outstanding and one new search a
# minute. Applied to the bulk lane only: a single one-click tree never waits
# behind a batch. Redis-backed so it holds across worker processes, and it fails
# open -- a Redis fault must never stop a tree from being built.
# ---------------------------------------------------------------------------
MYCOMAP_BULK_MAX_OUTSTANDING = 20
MYCOMAP_BULK_MIN_CREATE_INTERVAL_SECONDS = 60
# A search that never reports back (job killed, failed elsewhere) stops counting
# against the cap after this long.
MYCOMAP_BULK_OUTSTANDING_MAX_AGE_SECONDS = 4 * 60 * 60
_BULK_OUTSTANDING_KEY = "dikarya:mycomap:bulk_outstanding"
_BULK_CREATE_SLOT_KEY = "dikarya:mycomap:bulk_create_slot"


def get_mycomap_bulk_max_outstanding() -> int:
    return _env_int("MYCOMAP_BULK_MAX_OUTSTANDING", MYCOMAP_BULK_MAX_OUTSTANDING,
                    min_value=1, max_value=500)


# One day of one-minute checks: how long a bulk job may wait for a slot, on
# top of its discovery and NCBI polling budgets.
MYCOMAP_BULK_THROTTLE_MAX_WAIT_ATTEMPTS = 24 * 60


def get_mycomap_bulk_throttle_max_wait_attempts() -> int:
    return _env_int("MYCOMAP_BULK_THROTTLE_MAX_WAIT_ATTEMPTS",
                    MYCOMAP_BULK_THROTTLE_MAX_WAIT_ATTEMPTS,
                    min_value=0, max_value=7 * 24 * 60)


def reserve_bulk_mycomap_creation(member: str) -> Tuple[bool, str]:
    """Claim permission to create one bulk BLAST. Returns (allowed, reason)."""
    member = str(member or "").strip()
    conn = _shared_redis()
    if not member or conn is None:
        return True, "unthrottled"
    try:
        now = time.time()
        conn.zremrangebyscore(
            _BULK_OUTSTANDING_KEY, "-inf", now - MYCOMAP_BULK_OUTSTANDING_MAX_AGE_SECONDS
        )
        if conn.zscore(_BULK_OUTSTANDING_KEY, member) is not None:
            return True, "already_reserved"
        if conn.zcard(_BULK_OUTSTANDING_KEY) >= get_mycomap_bulk_max_outstanding():
            return False, "outstanding_limit"
        if not conn.set(_BULK_CREATE_SLOT_KEY, member, nx=True,
                        ex=MYCOMAP_BULK_MIN_CREATE_INTERVAL_SECONDS):
            return False, "rate_limit"
        conn.zadd(_BULK_OUTSTANDING_KEY, {member: now})
        return True, "reserved"
    except Exception as exc:
        logger.info("MycoMap bulk throttle unavailable; not throttling: %s", exc)
        return True, "unthrottled"


def release_bulk_mycomap_creation(member: str) -> None:
    """Stop counting a bulk BLAST against the outstanding cap."""
    member = str(member or "").strip()
    conn = _shared_redis() if member else None
    if conn is None:
        return
    try:
        conn.zrem(_BULK_OUTSTANDING_KEY, member)
    except Exception as exc:
        logger.info("MycoMap bulk throttle release failed: %s", exc)


def create_mycomap_blast(sequence: str, *, title: str = "",
                          local_limit: Optional[int] = None,
                          ncbi_limit: Optional[int] = None) -> dict:
    """Create a new local + NCBI MycoMap BLAST search from a DNA sequence."""
    cleaned_sequence = re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(sequence or "").upper())
    if not cleaned_sequence:
        raise MycoMapCreateError("DNA Barcode ITS does not contain a usable DNA sequence.")

    local_limit, local_error = validate_mycomap_rerun_limit(local_limit, "local")
    ncbi_limit, ncbi_error = validate_mycomap_rerun_limit(ncbi_limit, "ncbi")
    if local_error or ncbi_error:
        raise MycoMapCreateError(local_error or ncbi_error)

    api_key, key_info = _mycomap_api_key_info()
    if not api_key:
        raise MycoMapCreateError(
            f"MycoMap.com API key is not configured in {MYCOMAP_COM_API_KEY_ENV}."
        )

    job_title = str(title or "Dikarya iNaturalist ITS").strip()[:200]
    user_id = get_mycomap_user_id()
    data = {
        "type": "sequence",
        "input": cleaned_sequence,
        "userID": str(user_id),
        "sequence": cleaned_sequence,
        "title": job_title,
        "limit": str(ncbi_limit),
        "local_limit": str(local_limit),
        "ncbi_limit": str(ncbi_limit),
        "blast_search_by": "sequence",
        "blast_search_type": "blastn",
        "blast_search_job_title": job_title,
        "blast_search_local_only": "0",
        "blast_search_limit": str(ncbi_limit),
        "blast_search_sequence": cleaned_sequence,
    }
    url = f"{MYCOMAP_API_BASE_URL}/blast"
    post_data = urllib.parse.urlencode(data).encode("utf-8")
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    logger.info(
        "Creating MycoMap BLAST: title=%s user_id=%s local_limit=%s ncbi_limit=%s "
        "api_key_source=%s api_key_len=%s api_key_sha256=%s",
        job_title,
        user_id,
        local_limit,
        ncbi_limit,
        key_info.get("source"),
        key_info.get("length"),
        key_info.get("sha256"),
    )
    request = urllib.request.Request(
        url,
        data=post_data,
        headers={
            "User-Agent": "Dikarya-TreeBuilder/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {token}",
            "X-API-Key": api_key,
        },
        method="POST",
    )

    try:
        with diagnostic_urlopen(request, timeout=MYCOMAP_RERUN_REQUEST_TIMEOUT) as resp:
            status_code = getattr(resp, "status", resp.getcode())
            raw_body = resp.read().decode("utf-8", errors="replace")
            response_url = resp.geturl() or ""
            location = resp.headers.get("Location") or ""
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed_body = json.loads(raw_body) if raw_body else None
        except json.JSONDecodeError:
            parsed_body = None
        message = _summarize_api_response(parsed_body, raw_body) or f"HTTP {exc.code}"
        logger.error("MycoMap BLAST creation failed: HTTP %s %s", exc.code, message)
        raise MycoMapCreateError(f"MycoMap BLAST creation failed: {message}")
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, TimeoutError):
            logger.error("MycoMap BLAST creation network error: %s", exc)
            raise MycoMapCreateError("MycoMap BLAST creation network error.")
        return _unconfirmed_mycomap_creation(job_title, local_limit, ncbi_limit)
    except TimeoutError:
        return _unconfirmed_mycomap_creation(job_title, local_limit, ncbi_limit)
    except Exception as exc:
        logger.error("Unexpected MycoMap BLAST creation error: %s", exc, exc_info=True)
        raise MycoMapCreateError("MycoMap BLAST creation failed unexpectedly.")

    try:
        parsed_body = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        parsed_body = None
    try:
        created = _parse_created_mycomap_blast(
            parsed_body, raw_body, response_url=response_url, location=location
        )
    except MycoMapCreateError:
        created = find_mycomap_blast_by_title(job_title)
        if not created:
            response_summary = _summarize_api_response(parsed_body, raw_body)
            logger.info(
                "MycoMap accepted BLAST creation but its result page is not "
                "published yet: %s",
                response_summary or "empty response",
            )
            created = {
                "record_pending": True,
                "title": job_title,
            }
    created.update({
        "status_code": status_code,
        "message": _summarize_api_response(parsed_body, raw_body),
        "local_limit": local_limit,
        "ncbi_limit": ncbi_limit,
    })
    if created.get("record_pending"):
        logger.info("Waiting for MycoMap to publish BLAST title %s", job_title)
    else:
        logger.info("Created MycoMap BLAST %s", created["blast_id"])
    return created


def _unconfirmed_mycomap_creation(job_title: str, local_limit: int,
                                  ncbi_limit: int) -> dict:
    """Treat a timed-out create POST as a search that may exist.

    Alan 9/23/26 - A timeout used to fail the job outright, although MycoMap
    had often accepted the request and only answered late. Returning a pending
    record hands it to the ordinary title discovery, and
    ``unconfirmed_mycomap_creation_verdict`` re-sends it if it never arrived.
    """
    logger.warning(
        "MycoMap BLAST creation timed out for %s; checking whether MycoMap "
        "created it before trying again", job_title,
    )
    return {
        "record_pending": True,
        "creation_unconfirmed": True,
        "title": job_title,
        "local_limit": local_limit,
        "ncbi_limit": ncbi_limit,
    }


def unconfirmed_mycomap_creation_verdict(details: Optional[dict], *,
                                         lookup_warnings: List[str],
                                         pending_creation: Optional[dict]) -> str:
    """Decide what to do with a pending search whose create POST timed out.

    Call only after a title lookup that did not find the search. Returns
    ``"wait"`` (keep discovering as normal), ``"retry"`` (MycoMap's history
    answered and has no such search, so the POST never landed) or
    ``"give_up"`` (it never landed, and the attempts are spent).

    A failed or partial lookup is never evidence of absence, and a history
    row with an ID means the search exists but is unpublished. Bulk creation
    is throttled to one a minute, so a search created on the previous pass is
    always inside the 100 newest history rows the lookup reads.
    """
    details = details or {}
    if not details.get("creation_unconfirmed"):
        return "wait"
    if (lookup_warnings or (pending_creation or {}).get("blast_id")
            or details.get("creation_pending_blast_id")
            or details.get("creation_pending_job_id")):
        return "wait"
    attempts = int(details.get("creation_attempts") or 1)
    if attempts >= MYCOMAP_UNCONFIRMED_CREATE_MAX_ATTEMPTS:
        return "give_up"
    return "retry"


def get_mycomap_ncbi_result_count(blast_id: str) -> Tuple[int, list]:
    """Return the currently exported NCBI hit count and any fetch warnings.

    This is a poll, not an interactive import. The outer RQ schedule is already
    the retry policy, so one quiet upstream attempt is enough; three nested
    attempts every minute turned an ordinary MycoMap backlog into dozens of
    ERROR records for one search.
    """
    ncbi_bytes, error = _fetch_fasta(
        str(blast_id), "fasta", max_attempts=1, log_final_failure=False
    )
    return _count_fasta_sequences(ncbi_bytes), [error] if error else []


def get_mycomap_local_result_count(blast_id: str) -> Tuple[int, list]:
    """Return the currently exported local (MycoBLAST) hit count and warnings."""
    local_bytes, error = _fetch_fasta(
        str(blast_id), "localFasta", max_attempts=1, log_final_failure=False
    )
    return _count_fasta_sequences(local_bytes), [error] if error else []


def rerun_mycomap_blast(blast_id: str, result_type: str = "local",
                        limit: Optional[int] = None) -> dict:
    """
    Re-run an existing MycoMap BLAST job through the authenticated API.

    ``result_type`` must be one of ``local``, ``ncbi``, or ``both``. Local
    reruns complete synchronously in MycoMap. NCBI reruns are queued there;
    callers currently wait a fixed grace period before fetching those results.
    """
    if not str(blast_id or "").isdigit():
        raise MycoMapRerunError("Invalid MycoMap BLAST result ID.")

    result_type = str(result_type or "").strip().lower()
    if result_type not in {"local", "ncbi", "both"}:
        raise MycoMapRerunError("Invalid MycoMap BLAST rerun type.")

    api_key, key_info = _mycomap_api_key_info()
    if not api_key:
        raise MycoMapRerunError(
            f"MycoMap.com API key is not configured in {MYCOMAP_COM_API_KEY_ENV}."
        )

    data = {"type": result_type}
    if limit is not None:
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):
            limit_value = None
        if limit_value and limit_value > 0:
            data["limit"] = str(limit_value)

    url = f"{MYCOMAP_API_BASE_URL}/blast/{blast_id}/rerun"
    post_data = urllib.parse.urlencode(data).encode("utf-8")
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    logger.info(
        "MycoMap BLAST %s rerun request for %s: limit=%s api_key_source=%s "
        "api_key_len=%s api_key_sha256=%s",
        result_type,
        blast_id,
        data.get("limit"),
        key_info.get("source"),
        key_info.get("length"),
        key_info.get("sha256"),
    )
    request = urllib.request.Request(
        url,
        data=post_data,
        headers={
            "User-Agent": "Dikarya-TreeBuilder/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {token}",
            "X-API-Key": api_key,
        },
        method="POST",
    )

    try:
        with diagnostic_urlopen(request, timeout=MYCOMAP_RERUN_REQUEST_TIMEOUT) as resp:
            status_code = getattr(resp, "status", resp.getcode())
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw_body = e.read().decode("utf-8", errors="replace")
        try:
            parsed_body = json.loads(raw_body) if raw_body else None
        except json.JSONDecodeError:
            parsed_body = None
        message = _summarize_api_response(parsed_body, raw_body) or f"HTTP {e.code}"
        logger.error(
            "MycoMap BLAST %s rerun failed for %s: HTTP %s %s "
            "api_key_source=%s api_key_len=%s api_key_sha256=%s",
            result_type,
            blast_id,
            e.code,
            message,
            key_info.get("source"),
            key_info.get("length"),
            key_info.get("sha256"),
        )
        raise MycoMapRerunError(f"MycoMap BLAST {result_type} rerun failed: {message}")
    except urllib.error.URLError as e:
        logger.error("MycoMap BLAST %s rerun network error for %s: %s", result_type, blast_id, e)
        raise MycoMapRerunError(f"MycoMap BLAST {result_type} rerun network error.")
    except TimeoutError:
        logger.error("MycoMap BLAST %s rerun timed out for %s", result_type, blast_id)
        raise MycoMapRerunError(f"MycoMap BLAST {result_type} rerun timed out.")
    except Exception as e:
        logger.error("Unexpected MycoMap BLAST %s rerun error for %s: %s", result_type, blast_id, e, exc_info=True)
        raise MycoMapRerunError(f"MycoMap BLAST {result_type} rerun failed unexpectedly.")

    try:
        parsed_body = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        parsed_body = None
    message = _summarize_api_response(parsed_body, raw_body)
    logger.info("MycoMap BLAST %s rerun accepted for %s: %s", result_type, blast_id, message or status_code)
    return {
        "type": result_type,
        "status_code": status_code,
        "limit": data.get("limit"),
        "message": message,
    }


def _mycomap_refresh_request(path: str, *, method: str = "GET",
                             data: Optional[dict] = None,
                             timeout: float = MYCOMAP_RERUN_REQUEST_TIMEOUT):
    """Call one authenticated MycoMap refresh-related endpoint and parse JSON."""
    api_key, key_info = _mycomap_api_key_info()
    if not api_key:
        raise MycoMapRefreshError(
            f"MycoMap.com API key is not configured in {MYCOMAP_COM_API_KEY_ENV}."
        )

    url = f"{MYCOMAP_API_BASE_URL}/{path.lstrip('/')}"
    request_data = None
    if method == "GET" and data:
        url = f"{url}?{urllib.parse.urlencode(data)}"
    elif data is not None:
        request_data = urllib.parse.urlencode(data, doseq=True).encode("utf-8")

    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url,
        data=request_data,
        headers={
            "User-Agent": "Dikarya-TreeViewer/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {token}",
            "X-API-Key": api_key,
        },
        method=method,
    )
    try:
        with diagnostic_urlopen(request, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed_body = json.loads(raw_body) if raw_body else None
        except json.JSONDecodeError:
            parsed_body = None
        message = _summarize_api_response(parsed_body, raw_body) or f"HTTP {exc.code}"
        logger.error(
            "MycoMap refresh API failed for %s: HTTP %s %s "
            "api_key_source=%s api_key_len=%s api_key_sha256=%s",
            path,
            exc.code,
            message,
            key_info.get("source"),
            key_info.get("length"),
            key_info.get("sha256"),
        )
        raise MycoMapRefreshError(f"MycoMap refresh failed: {message}")
    except urllib.error.URLError as exc:
        # A connect-phase timeout arrives wrapped in URLError; report it as the
        # timeout it is so callers can back off.
        if isinstance(exc.reason, TimeoutError):
            logger.error("MycoMap refresh timed out for %s", path)
            raise MycoMapRefreshTimeout("MycoMap refresh timed out.")
        logger.error("MycoMap refresh network error for %s: %s", path, exc)
        raise MycoMapRefreshError("MycoMap refresh network error.")
    except TimeoutError:
        logger.error("MycoMap refresh timed out for %s", path)
        raise MycoMapRefreshTimeout("MycoMap refresh timed out.")
    except Exception as exc:
        logger.error("Unexpected MycoMap refresh error for %s: %s", path, exc, exc_info=True)
        raise MycoMapRefreshError("MycoMap refresh failed unexpectedly.")

    try:
        return json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.error("MycoMap refresh API returned non-JSON for %s", path)
        record_api_failure(request.full_url, reason="invalid_json", status=200,
                           body=raw_body, method=method, req=request)
        raise MycoMapRefreshError("MycoMap refresh returned an invalid response.")


def _mycomap_result_rows(payload) -> list:
    """Return the record rows from compatible MycoMap response envelopes."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "observations", "records", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    for key in ("result", "record", "data"):
        row = payload.get(key)
        if isinstance(row, dict):
            return [row]
    return [payload]


def _normalized_api_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _api_text_value(value) -> str:
    """Collapse a scalar or common nested API value to display text."""
    if isinstance(value, str):
        return _clean_label_fragment(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "label", "title", "formatted", "value"):
            text = _api_text_value(value.get(key))
            if text:
                return text
    return ""


def _find_api_field(record: dict, field_names: tuple) -> str:
    """Find a named scalar field in a possibly nested MycoMap record."""
    wanted = {_normalized_api_key(name) for name in field_names}
    queue = [record]
    seen = set()
    while queue:
        current = queue.pop(0)
        if not isinstance(current, dict) or id(current) in seen:
            continue
        seen.add(id(current))
        for key, value in current.items():
            if _normalized_api_key(key) in wanted:
                text = _api_text_value(value)
                if text:
                    return text
        for nested_key in ("data", "record", "result", "sequence", "observation", "species", "taxon"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                queue.append(nested)
    return ""


def _reference_from_api_record(record: dict) -> Optional[str]:
    """Resolve the external observation reference represented by an API row."""
    queue = [record]
    seen = set()
    reference_keys = {
        "observation", "observationref", "observationreference", "externalreference",
        "sourcereference", "reference", "ref", "observationurl", "sourceurl", "url",
    }
    while queue:
        current = queue.pop(0)
        if not isinstance(current, dict) or id(current) in seen:
            continue
        seen.add(id(current))
        normalized = {_normalized_api_key(key): value for key, value in current.items()}
        for key in reference_keys:
            value = normalized.get(key)
            if isinstance(value, (str, int)):
                reference = extract_mycomap_observation_reference(str(value))
                if reference:
                    return reference

        platform = _api_text_value(
            normalized.get("platform")
            or normalized.get("source")
            or normalized.get("observationsource")
        ).lower()
        observation_id = _api_text_value(
            normalized.get("observationnumber")
            or normalized.get("observationid")
            or normalized.get("sourceid")
            or normalized.get("externalid")
        )
        if not observation_id and isinstance(normalized.get("observation"), dict):
            observation = normalized["observation"]
            observation_id = _api_text_value(
                observation.get("id")
                or observation.get("observation_id")
                or observation.get("observationNumber")
            )
        id_match = re.search(r"\d{1,12}", observation_id)
        if id_match:
            if platform in {"inat", "inaturalist"}:
                return f"inat:{id_match.group(0)}"
            if platform in {"mo", "mushroomobserver", "mushroom observer"}:
                return f"mo:{id_match.group(0)}"

        for value in current.values():
            if isinstance(value, dict):
                queue.append(value)
    return None


def _summarize_mycomap_records(payload, references: list) -> dict:
    """Map MycoMap sequence detail rows to the requested external references."""
    rows = _mycomap_result_rows(payload)
    mapped = {}
    unresolved = []
    wanted = set(references)
    for row in rows:
        reference = _reference_from_api_record(row)
        if reference in wanted:
            mapped.setdefault(reference, row)
        else:
            unresolved.append(row)

    missing = [reference for reference in references if reference not in mapped]
    # sequences/batch is queried with an explicit observation list, so a row we
    # could not parse a reference out of is only safe to attribute when a single
    # observation was requested and exactly one candidate row came back. Never
    # guess by position across a multi-observation batch, and never override a
    # reference that already matched: either would write another observation's
    # taxon onto the tip label.
    if len(references) == 1 and len(missing) == 1 and len(unresolved) == 1:
        mapped[missing[0]] = unresolved[0]

    summaries = {}
    for reference in references:
        record = mapped.get(reference)
        if not record:
            summaries[reference] = {
                "found": False,
                "scientific_name": "",
                "location": "",
            }
            continue
        summaries[reference] = {
            "found": True,
            "scientific_name": _find_api_field(
                record,
                (
                    "scientific_name", "scientificName", "species_name", "speciesName",
                    "taxon_name", "species", "taxon",
                ),
            ),
            "location": _find_api_field(
                record,
                ("location", "location_name", "locationName", "locality", "place_name", "formatted_location"),
            ),
        }
    return summaries


def _fetch_mycomap_observation_records(references: list) -> dict:
    payload = _mycomap_refresh_request(
        "sequences/batch",
        data={"observations": ",".join(references)},
    )
    return _summarize_mycomap_records(payload, references)


def refresh_mycomap_observation_records(references: list) -> dict:
    """Refresh observation records and return their before/after name and location."""
    normalized = []
    seen = set()
    for value in references:
        reference = extract_mycomap_observation_reference(str(value or ""))
        if not reference or not MYCOMAP_OBSERVATION_REF_RE.fullmatch(reference):
            raise MycoMapRefreshError("Invalid MycoMap observation reference.")
        if reference not in seen:
            normalized.append(reference)
            seen.add(reference)
    if not normalized:
        raise MycoMapRefreshError("No MycoMap observation references were supplied.")
    if len(normalized) > 100:
        raise MycoMapRefreshError("No more than 100 MycoMap records can be refreshed at once.")

    before = _fetch_mycomap_observation_records(normalized)
    if len(normalized) == 1:
        refresh_payload = _mycomap_refresh_request(
            "refresh",
            method="POST",
            data={"observation": normalized[0]},
        )
    else:
        refresh_payload = _mycomap_refresh_request(
            "refresh/batch",
            method="POST",
            data={"observations": ",".join(normalized)},
        )
    after = _fetch_mycomap_observation_records(normalized)
    return {
        "references": normalized,
        "before": before,
        "after": after,
        "message": _summarize_api_response(refresh_payload, ""),
    }


# The legacy validator below remains specific to .com. The provider-aware
# parser accepts these .org hosts on the three supported result paths.
MYCOMAP_ORG_HOSTNAMES = ('mycomap.org', 'www.mycomap.org')


def parse_mycomap_result_reference(url: str) -> Optional[dict]:
    """Identify a supported result URL without making a network request.

    A reference keeps the supplied URL for provenance. An observation link's
    sequence ID is resolved to its BLAST job through MycoMap.org metadata later.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or '').lower()
        if parsed.username or parsed.password or parsed.port:
            return None
    except (ValueError, TypeError):
        return None
    if host in MYCOMAP_ORG_HOSTNAMES:
        if parsed.scheme != 'https' or parsed.query or parsed.fragment:
            return None
        path = parsed.path.rstrip('/')
        direct = re.fullmatch(r'/mycoblast/(\d+)', path)
        linked = re.fullmatch(
            r'/(?:admin/)?blast-results/(\d{1,12})/(\d+)', path
        )
        if direct:
            return {"provider": "org", "kind": "mycoblast",
                    "result_id": direct.group(1), "url": url}
        if linked:
            return {"provider": "org", "kind": "sequence",
                    "observation_id": linked.group(1),
                    "sequence_id": linked.group(2), "url": url}
        return None
    if host in ('mycomap.com', 'www.mycomap.com'):
        blast_id = validate_mycomap_url(url, quiet=True)
        if blast_id:
            return {"provider": "com", "kind": "mycoblast",
                    "result_id": blast_id, "url": url}
    return None


def resolve_mycomap_result_reference(url: str) -> Optional[dict]:
    """Return the provider and BLAST ID, resolving org sequence links if needed."""
    reference = parse_mycomap_result_reference(url)
    if reference and reference["provider"] == "org" and not reference.get("result_id"):
        from app.services.mycomap_org_service import resolve_result_id
        reference["result_id"] = resolve_result_id(reference)
    return reference


def rerun_mycomap_result(reference: dict, result_type: str, limit: int) -> dict:
    if reference["provider"] == "org":
        from app.services.mycomap_org_service import rerun
        return rerun(reference["result_id"], result_type, limit)
    return rerun_mycomap_blast(reference["result_id"], result_type=result_type, limit=limit)


def is_mycomap_org_url(url: str) -> bool:
    """True when ``url`` points at the mycomap.org host (any path)."""
    if not url:
        return False
    try:
        hostname = (urllib.parse.urlparse(url).hostname or '').lower()
    except Exception:
        return False
    return hostname in MYCOMAP_ORG_HOSTNAMES


def validate_mycomap_url(url: str, *, quiet: bool = False) -> Optional[str]:
    """
    Validate a Mycomap URL and extract the blast_id.
    
    Uses strict hostname checking to prevent bypass attacks like:
    - https://evil.com/?q=mycomap.com/r12345
    - https://mycomap.com.evil.com/r12345
    
    Args:
        url: The URL to validate (e.g., "https://mycomap.com/...r12345...")
        quiet: Log rejections at DEBUG instead of WARNING. Set this ONLY on
            speculative callers that scan a list of candidates and expect most
            of them to miss -- a rejection there is normal control flow, not a
            failure. Alan 9/10/26 - the BLAST-creation response scan was feeding
            its own POST target (https://mycomap.com/api/mycomap/blast) through
            here, so every auto-created search logged a WARNING into errors.log
            for something that had not gone wrong. Leave quiet=False wherever
            the URL came from a user or from stored job state: those rejections
            are the ones worth seeing. Note this only changes the log level --
            the hostname and scheme checks still reject exactly as before.
        
    Returns:
        The blast_id (digits only) if valid, None otherwise.
    """
    if not url:
        return None

    reject = logger.debug if quiet else logger.warning
    
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        reject(f"URL validation failed: could not parse URL: {url}")
        return None
    
    # Strict hostname check - must be exactly mycomap.com or www.mycomap.com
    hostname = (parsed.hostname or '').lower()
    valid_hostnames = ['mycomap.com', 'www.mycomap.com']
    if hostname not in valid_hostnames:
        reject(f"URL validation failed: invalid hostname '{hostname}' (expected mycomap.com): {url}")
        return None
    
    # Enforce http or https protocol
    if parsed.scheme not in ('http', 'https'):
        reject(f"URL validation failed: invalid scheme '{parsed.scheme}' (expected http/https): {url}")
        return None
    
    # Extract the r<digits> pattern from path or query
    # Use word boundary to avoid matching middle of other tokens
    search_text = parsed.path + '?' + (parsed.query or '')

    # Alan 8/15/26 - Prefer r<digits> standing alone as a whole path segment or
    # query value, which is what a real BLAST URL looks like
    # (https://mycomap.com/genetics/blast-search/r590124/), and take the last such
    # match rather than the first.
    #
    # The old pattern was the loose scan below with no trailing boundary, so the
    # first r-followed-by-digits anywhere in the URL won -- including inside a
    # longer slug. On 2026-08-14 that turned a pasted URL into blast_id "025" and
    # a user retried four times, each attempt fetching a nonexistent record and
    # getting a 502; the real IDs that day were all six digits. ID length is not a
    # usable check (tests and older records use short IDs like r42), so anchor on
    # the delimiter instead.
    segment_matches = re.findall(r'(?:^|[/?&=])r(\d+)(?=$|[/?&#])', search_text)
    if segment_matches:
        blast_id = segment_matches[-1]
    else:
        # Fall back to the loose scan for hand-edited or unusual URLs, but require
        # the digits to end the token so "r025abc" is not read as 025.
        loose_matches = re.findall(
            r'(?:^|[^a-zA-Z0-9])r(\d+)(?![a-zA-Z0-9])', search_text
        )
        if not loose_matches:
            reject(f"URL validation failed: no r<digits> pattern found in: {url}")
            return None
        # Slug-style result URLs may contain earlier r-prefixed specimen or
        # plate tokens before the final BLAST result ID. The result ID is the
        # last complete r<digits> token in the slug.
        blast_id = loose_matches[-1]
        logger.info(
            "Mycomap URL had no standalone r<digits> segment; using loose match "
            "blast_id=%s from: %s", blast_id, url,
        )

    # Alan 8/14/26 - DEBUG, not INFO. This fires several times per user action and
    # was 37% of every line in error.log (97 of 267), burying real errors. The
    # blast_id is still recoverable from the surrounding "Mycomap helper"/fetch
    # lines, which log once per operation rather than once per validation call.
    logger.debug("Validated Mycomap URL, extracted blast_id: %s", blast_id)
    return blast_id


# Alan 8/14/26 - MycoMap sequence record pages, e.g.
# https://mycomap.com/genetics/sequences/ont_sequences/f05-bc26-...-ric100-r763916/
# These are a different thing from BLAST result pages: they identify one sequence
# record rather than a search. They used to be rejected outright by the Tree Builder
# ("bad_prefix"), which is confusing because the URL is a perfectly good handle for a
# sequence a user wants in their tree.
_MYCOMAP_SEQUENCE_PATH_RE = re.compile(r'/genetics/sequences(?:/|$)', re.IGNORECASE)
_MYCOMAP_RECORD_ID_RE = re.compile(r'(?:^|[^a-zA-Z0-9])r(\d+)(?:[^0-9]|$)')

# A DNA payload found in an arbitrary JSON field: mostly IUPAC nucleotide codes and
# long enough not to be an accession, primer name, or status string.
_DNA_LIKE_RE = re.compile(r'^[ACGTURYKMSWBDHVN\-\.\s]+$', re.IGNORECASE)
MYCOMAP_MIN_SEQUENCE_LENGTH = 50


def validate_mycomap_sequence_url(url: str) -> Optional[str]:
    """Validate a MycoMap sequence record URL and extract its numeric record ID.

    Applies the same strict hostname/scheme checks as validate_mycomap_url -- the
    hostname must be exactly mycomap.com, so lookalikes such as
    https://mycomap.com.evil.com/... and https://evil.com/?q=mycomap.com/r1 are
    rejected. Returns None for anything that is not a sequence record URL,
    including BLAST result URLs, so callers can tell the two apart.
    """
    if not url:
        return None

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        logger.warning("Sequence URL validation failed: could not parse URL")
        return None

    hostname = (parsed.hostname or '').lower()
    if hostname not in ('mycomap.com', 'www.mycomap.com'):
        logger.warning(
            "Sequence URL validation failed: invalid hostname %r (expected mycomap.com)",
            hostname,
        )
        return None

    if parsed.scheme not in ('http', 'https'):
        logger.warning(
            "Sequence URL validation failed: invalid scheme %r (expected http/https)",
            parsed.scheme,
        )
        return None

    if not _MYCOMAP_SEQUENCE_PATH_RE.search(parsed.path or ''):
        return None

    # The record ID is the r<digits> token, conventionally last in the slug. Take the
    # last match so a slug that happens to contain an earlier r<digits> (a RiC number,
    # a run label) cannot win over the actual record ID.
    matches = _MYCOMAP_RECORD_ID_RE.findall(parsed.path + '?' + (parsed.query or ''))
    if not matches:
        logger.warning("Sequence URL validation failed: no r<digits> record ID in path")
        return None

    sequence_id = matches[-1]
    logger.debug("Validated MycoMap sequence URL, extracted sequence_id: %s", sequence_id)
    return sequence_id


def _looks_like_dna(value) -> bool:
    """True when a JSON value looks like a usable nucleotide sequence."""
    if not isinstance(value, str):
        return False
    compact = ''.join(value.split())
    if len(compact) < MYCOMAP_MIN_SEQUENCE_LENGTH:
        return False
    if not _DNA_LIKE_RE.match(compact):
        return False
    # Guard against long runs of a single ambiguity code (e.g. a padded field).
    bases = sum(compact.upper().count(base) for base in 'ACGTU')
    return bases >= len(compact) * 0.5


def _find_dna_in_payload(payload, _depth: int = 0):
    """Recursively locate the nucleotide sequence in a MycoMap record payload.

    The sequences API response shape is not pinned down in our copy of the MycoMap
    docs, and the field has been named differently across MycoMap versions, so match
    on the value rather than on a guessed key. Preferred key names are tried first so
    a record carrying several DNA-ish fields resolves predictably.
    """
    if _depth > 6:
        return None

    if isinstance(payload, dict):
        for key in ('sequence', 'sequence_data', 'nucleotides', 'dna', 'seq', 'fasta'):
            value = payload.get(key)
            if _looks_like_dna(value):
                return value
        for value in payload.values():
            found = _find_dna_in_payload(value, _depth + 1)
            if found:
                return found
        return None

    if isinstance(payload, list):
        for item in payload:
            found = _find_dna_in_payload(item, _depth + 1)
            if found:
                return found
        return None

    return payload if _looks_like_dna(payload) else None


def _first_str(record: dict, keys) -> str:
    """Return the first non-empty string value among keys."""
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def fetch_mycomap_sequence(sequence_id: str) -> dict:
    """Fetch a single MycoMap sequence record by its numeric record ID.

    Returns a dict with 'sequence' (nucleotides), 'name' (a display label),
    'species', 'location', 'record_url', and 'errors'.
    """
    result = {
        'sequence_id': sequence_id,
        'sequence': '',
        'name': '',
        'species': '',
        'location': '',
        'record_url': '',
        'errors': [],
    }

    if not str(sequence_id).isdigit():
        result['errors'].append("Invalid MycoMap sequence ID.")
        return result

    try:
        payload = _mycomap_refresh_request(f"sequences/{sequence_id}")
    except MycoMapRefreshError as exc:
        result['errors'].append(str(exc))
        return result

    rows = _mycomap_result_rows(payload)
    record = rows[0] if rows else {}
    if not isinstance(record, dict):
        record = {}

    sequence = _find_dna_in_payload(payload)
    if not sequence:
        result['errors'].append(
            f"MycoMap sequence {sequence_id} does not have a DNA sequence attached."
        )
        return result

    result['sequence'] = ''.join(str(sequence).split()).upper()
    result['species'] = _first_str(record, ('scientificName', 'species', 'species_name', 'taxon'))
    result['location'] = _first_str(record, ('location', 'locality', 'place'))
    result['record_url'] = _first_str(record, ('url', 'record_url', 'permalink', 'link'))

    title = _first_str(record, ('title', 'name', 'label'))
    # Build a tip label in the same spirit as the BLAST importer: identity first,
    # then whatever context is available, so it stays readable on a tree.
    parts = [part for part in (title or f"MycoMap{sequence_id}", result['species'], result['location']) if part]
    seen = set()
    deduped = []
    for part in parts:
        if part.lower() in seen:
            continue
        seen.add(part.lower())
        deduped.append(part)
    result['name'] = ' '.join(deduped)

    logger.info(
        "Fetched MycoMap sequence %s: %s bp, name=%r",
        sequence_id, len(result['sequence']), result['name'],
    )
    return result


_NCBI_QUEUE_POSITION_RE = re.compile(
    r"position of this BLAST search in the queue is:\s*(\d+)", re.IGNORECASE
)

# The authenticated status endpoint is cheap and is the only place MycoMap
# publishes the queue position as data rather than as a sentence inside a page,
# so it is tried first and the HTML scrape is kept only as a fallback.
MYCOMAP_BLAST_STATUS_TIMEOUT = 15


def _coerce_queue_position(value) -> Optional[int]:
    """Return a non-negative int queue position, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        position = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return position if position >= 0 else None


def fetch_mycomap_blast_record(blast_id: str) -> Optional[dict]:
    """
    Best-effort read of MycoMap's authenticated BLAST status record
    (``GET /api/mycomap/blast/<id>``), which reports the search status and,
    while it is waiting, ``ncbi.queue_position``.

    Returns the parsed record, or None if it is unavailable for any reason
    (no API key, network fault, non-JSON body). Callers treat this as context
    only, so a failure here must never turn into an error for the user.
    """
    blast_id = str(blast_id or "").strip()
    if not blast_id.isdigit():
        return None

    api_key = _mycomap_api_key()
    if not api_key:
        logger.info(
            "MycoMap BLAST status skipped: no %s configured", MYCOMAP_COM_API_KEY_ENV
        )
        return None

    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{MYCOMAP_API_BASE_URL}/blast/{blast_id}",
        headers={
            "User-Agent": "Dikarya-TreeBuilder/1.0",
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
            "X-API-Key": api_key,
        },
        method="GET",
    )
    try:
        with diagnostic_urlopen(request, timeout=MYCOMAP_BLAST_STATUS_TIMEOUT) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.info("Could not read MycoMap BLAST status for %s: %s", blast_id, e)
        return None

    try:
        payload = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        logger.info("MycoMap BLAST status for %s was not JSON", blast_id)
        return None

    if isinstance(payload, list):
        payload = next((row for row in payload if isinstance(row, dict)), None)
    if not isinstance(payload, dict):
        return None
    # Some MycoMap endpoints wrap the record in an envelope.
    for key in ("record", "result", "data", "blast"):
        nested = payload.get(key)
        if isinstance(nested, dict) and ("status" in nested or "ncbi" in nested):
            payload = nested
            break
    ncbi = payload.get("ncbi")
    ncbi = ncbi if isinstance(ncbi, dict) else {}
    # One line per status read, so "did MycoMap report a queue position?" is a
    # grep rather than a guess when a wait goes by without one being shown.
    logger.info(
        "event=mycomap.blast_status blast_id=%s status=%s ncbi_status=%s "
        "queue_position=%s keys=%s",
        blast_id,
        _api_text_value(payload.get("status")) or "-",
        _api_text_value(ncbi.get("status")) or "-",
        ncbi.get("queue_position", payload.get("queue_position")),
        ",".join(sorted(str(k) for k in payload)) or "-",
    )
    return payload


_UNFINISHED_BLAST_STATUSES = frozenset(
    {"queued", "queue", "pending", "waiting", "processing", "running", "in_progress"}
)


def _looks_unfinished(status: Optional[str]) -> bool:
    """True when MycoMap's reported status means the search has not finished."""
    return str(status or "").strip().lower().replace("-", "_") in _UNFINISHED_BLAST_STATUSES


def get_mycomap_ncbi_queue_status(mycomap_url: Optional[str] = None, *,
                                  blast_id: Optional[str] = None) -> dict:
    """
    Return what MycoMap currently says about an NCBI BLAST search:

        {"queue_position": int|None, "status": str|None,
         "rid": str|None, "source": "api"|"html"|None}

    ``queue_position`` is None both when the search is not queued and when the
    position could not be determined, which is why ``source`` is reported: only
    an answer from the API or the page is evidence about the queue at all.
    """
    reference = parse_mycomap_result_reference(mycomap_url) if mycomap_url else None
    if reference and reference["provider"] == "org":
        from app.services.mycomap_org_service import OrgResultError, status
        try:
            resolved = blast_id or resolve_mycomap_result_reference(mycomap_url)["result_id"]
            record = status(resolved)
            ncbi = record.get("ncbi") or {}
            return {
                "queue_position": _coerce_queue_position(ncbi.get("queue_position")),
                "status": ncbi.get("status"), "rid": ncbi.get("rid"),
                "source": "api",
            }
        except OrgResultError:
            logger.warning("MycoMap.org NCBI status unavailable", exc_info=True)
            return {"queue_position": None, "status": None, "rid": None, "source": None}

    resolved_id = str(blast_id or "").strip()
    if not resolved_id.isdigit() and mycomap_url:
        resolved_id = validate_mycomap_url(mycomap_url) or ""

    if resolved_id.isdigit():
        record = fetch_mycomap_blast_record(resolved_id)
        if isinstance(record, dict):
            ncbi = record.get("ncbi")
            ncbi = ncbi if isinstance(ncbi, dict) else {}
            position = _coerce_queue_position(
                ncbi.get("queue_position", record.get("queue_position"))
            )
            status = _api_text_value(ncbi.get("status") or record.get("status")) or None
            rid = _api_text_value(ncbi.get("rid")) or None
            if position is None and mycomap_url and _looks_unfinished(status):
                # MycoMap drops queue_position from the record as soon as the
                # queue row is removed, which happens before the results exist.
                # While the search is still unfinished the page may still name a
                # position, so keep the old scrape as a second opinion.
                position = _scrape_mycomap_ncbi_queue_position(mycomap_url)
                if position is not None:
                    return {
                        "queue_position": position,
                        "status": status or "queued",
                        "rid": rid,
                        "source": "html",
                    }
            return {
                "queue_position": position,
                "status": status,
                "rid": rid,
                "source": "api",
            }

    position = _scrape_mycomap_ncbi_queue_position(mycomap_url) if mycomap_url else None
    return {
        "queue_position": position,
        "status": "queued" if position is not None else None,
        "rid": None,
        "source": "html" if position is not None else None,
    }


def get_mycomap_ncbi_queue_position(mycomap_url: Optional[str] = None, *,
                                    blast_id: Optional[str] = None) -> Optional[int]:
    """
    Return this search's position in MycoMap's NCBI BLAST queue, or None when
    it is not queued (or cannot be determined).

    Prefers the authenticated JSON status endpoint and falls back to scraping
    the result page, which is all MycoMap offered before the API existed.
    """
    return get_mycomap_ncbi_queue_status(
        mycomap_url, blast_id=blast_id
    )["queue_position"]


def _scrape_mycomap_ncbi_queue_position(mycomap_url: str) -> Optional[int]:
    """
    Best-effort scrape of the NCBI BLAST queue position from a Mycomap
    result page (e.g. "The position of this BLAST search in the queue
    is: 1."). Fallback for when the authenticated status endpoint is
    unavailable.
    """
    if not validate_mycomap_url(mycomap_url):
        return None

    request = urllib.request.Request(
        mycomap_url,
        headers={
            'User-Agent': 'Dikarya-TreeBuilder/1.0',
            'Accept': 'text/html',
        },
    )
    try:
        with diagnostic_urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            content = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        # Queue position is best-effort context. The actual result fetch still
        # decides whether work can proceed, so a status-page hiccup is not an
        # operator warning by itself.
        logger.info("Could not fetch MycoMap page to check queue position: %s", e)
        return None

    match = _NCBI_QUEUE_POSITION_RE.search(content)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def record_mycomap_queue_position(details: dict, position: Optional[int]) -> dict:
    """
    Store the current queue position on a rerun-details dict, keeping the first
    position seen and when it was seen so the wait can be described as movement
    rather than as a bare number.
    """
    details = dict(details or {})
    if position is None:
        details.pop("ncbi_queue_position", None)
        return details

    details["ncbi_queue_position"] = position
    details["ncbi_queue_position_seen_at"] = datetime.now(timezone.utc).isoformat()
    first = _coerce_queue_position(details.get("ncbi_queue_first_position"))
    if first is None or position > first:
        # A higher number than anything seen before means a new wait (a rerun
        # re-queues the search), so restart the movement baseline.
        details["ncbi_queue_first_position"] = position
        details["ncbi_queue_first_seen_at"] = details["ncbi_queue_position_seen_at"]
    return details


def _parse_iso_timestamp(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def describe_mycomap_queue_position(details: dict, *, first: bool = True) -> str:
    """
    Return a sentence naming where this search sits in MycoMap's NCBI BLAST
    queue, including how fast the queue has been moving when that is known.

    This is published on its own, separately from whatever the job is waiting
    on, so it reads as an event when the position changes rather than as a tail
    on a message the user has already read. ``first`` selects the opening for
    the first report; later ones lead with "Now at". Returns "" when there is
    no position to report.
    """
    details = details or {}
    position = _coerce_queue_position(details.get("ncbi_queue_position"))
    if position is None:
        return ""
    if position == 0:
        return (
            "MycoMap reports this search has reached the front of its NCBI "
            "BLAST queue." if first
            else "Now at the front of MycoMap's NCBI BLAST queue."
        )

    sentence = (
        f"MycoMap reports this search is at position {position} in its NCBI "
        "BLAST queue" if first
        else f"Now at position {position} in MycoMap's NCBI BLAST queue"
    )

    started = _coerce_queue_position(details.get("ncbi_queue_first_position"))
    first_seen = _parse_iso_timestamp(details.get("ncbi_queue_first_seen_at"))
    seen_at = _parse_iso_timestamp(details.get("ncbi_queue_position_seen_at"))
    if started is not None and started > position and first_seen and seen_at:
        elapsed_minutes = (seen_at - first_seen).total_seconds() / 60.0
        if elapsed_minutes >= 1:
            minutes_each = elapsed_minutes / (started - position)
            eta_minutes = max(1, round(minutes_each * position))
            sentence += (
                f", down from {started}. At that rate it should start in "
                f"roughly {eta_minutes} minute{'s' if eta_minutes != 1 else ''}"
            )
        else:
            sentence += f", down from {started}"
    return sentence + "."


def _count_fasta_sequences(fasta_bytes: bytes) -> int:
    """Return the number of sequences in a FASTA file (bytes)."""
    return sum(1 for line in fasta_bytes.splitlines() if line.startswith(b'>'))


def _fetch_fasta(
    blast_id: str, endpoint: str, deadline: Optional[float] = None, *,
    max_attempts: Optional[int] = None, log_final_failure: bool = True,
) -> Tuple[bytes, Optional[str]]:
    """
    Fetch FASTA content from Mycomap.

    Args:
        blast_id: The blast ID to fetch
        endpoint: Either 'fasta' (NCBI) or 'localFasta' (local/MycoBLAST)
        deadline: Optional time.monotonic() value past which no further attempt
            is started and per-attempt timeouts are shortened to fit. None means
            the full retry budget, which is only appropriate off the request path.
        max_attempts: Override the nested fetch retry count. Polling callers use
            one because their outer RQ schedule is already the retry policy.
        log_final_failure: Emit ERROR after exhausting attempts. Polling callers
            set this false because a not-yet-ready export is an expected state.

    Returns:
        Tuple of (fasta_bytes, error_message). If error, fasta_bytes will be empty.
    """
    # Double-check blast_id is only digits
    if not blast_id.isdigit():
        return b'', "Invalid blast_id format"

    params = urllib.parse.urlencode({
        'app': 'genbank',
        'module': 'genbank',
        'controller': 'blast',
        'do': endpoint,
        'id': blast_id
    })
    url = f"{MYCOMAP_BASE_URL}?{params}"
    
    # The NCBI export now has either two or four logical header fields. Request
    # the documented hyphen separator so blank voucher/location fields remain
    # distinguishable; localFasta keeps its established space-delimited labels.
    delimiter = 'h' if endpoint == 'fasta' else 's'
    post_data = urllib.parse.urlencode({'delimiter': delimiter}).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=post_data,
        headers={
            'User-Agent': 'Dikarya-TreeBuilder/1.0',
            'Accept': '*/*',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    
    # Alan 8/14/26 - Retry transient upstream failures. A single MycoMap 500 used to
    # drop an entire result set: the caller kept the sequences it did get and built a
    # tree missing all of the NCBI references, with no warning to the user. Only
    # server-side/network faults are retried -- a 4xx is a real answer, not a blip.
    attempts = FASTA_FETCH_ATTEMPTS if max_attempts is None else max(1, int(max_attempts))
    last_error = None
    for attempt in range(attempts):
        timeout = REQUEST_TIMEOUT
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Never fall through with last_error still None: the caller
                # treats (b'', None) as a successful empty fetch and would
                # silently import zero sequences from this endpoint.
                last_error = last_error or (
                    f"Ran out of time fetching {endpoint} before any attempt completed"
                )
                break
            timeout = min(REQUEST_TIMEOUT, remaining)
        try:
            with diagnostic_urlopen(request, timeout=timeout) as resp:
                content = resp.read()
            if attempt:
                logger.info(
                    "Fetched %s for blast_id %s on attempt %s/%s.",
                    endpoint, blast_id, attempt + 1, attempts,
                )
            return content, None
        except urllib.error.HTTPError as e:
            last_error = f"Network error fetching {endpoint}: {e}"
            if e.code < 500:
                logger.error(last_error)
                return b'', last_error
        except urllib.error.URLError as e:
            last_error = f"Network error fetching {endpoint}: {e}"
        except TimeoutError:
            last_error = f"Request timed out after {timeout:.0f}s for {endpoint}"
        except Exception as e:
            last_error = f"Unexpected error fetching {endpoint}: {e}"
            logger.error(last_error, exc_info=True)
            return b'', last_error

        if attempt + 1 < attempts:
            # Alan 9/22/26 - Jittered, per MycoMap's own recommendation: when a
            # batch hits a MycoMap slowdown, un-jittered retries from every job
            # land on the same seconds and keep the pile-up going.
            delay = round(
                FASTA_FETCH_RETRY_BASE_SECONDS * (2 ** attempt) * random.uniform(0.8, 1.6), 1
            )
            if deadline is not None and time.monotonic() + delay >= deadline:
                logger.warning(
                    "%s (attempt %s/%s); no time budget left to retry.",
                    last_error, attempt + 1, attempts,
                )
                break
            logger.warning(
                "%s (attempt %s/%s); retrying in %ss.",
                last_error, attempt + 1, attempts, delay,
            )
            time.sleep(delay)

    if log_final_failure:
        logger.error("%s (gave up on %s for blast_id %s)", last_error, endpoint, blast_id)
    else:
        logger.info(
            "MycoMap %s export is not ready for blast_id %s: %s",
            endpoint, blast_id, last_error,
        )
    return b'', last_error


def fetch_mycomap_fasta(
    blast_id: str,
    include_ncbi: bool = True,
    include_local: bool = True,
    time_budget: Optional[float] = None
) -> dict:
    """
    Fetch FASTA sequences from Mycomap BLAST results.

    Args:
        blast_id: The Mycomap blast ID (digits only)
        include_ncbi: Whether to include NCBI BLAST results
        include_local: Whether to include local MycoBLAST results
        time_budget: Optional wall-clock ceiling in seconds covering *both*
            endpoint fetches together. Request handlers should pass
            INTERACTIVE_FETCH_BUDGET_SECONDS; workers leave it None for the
            full retry budget.

    Returns:
        Dict with keys:
        - fasta_content: str - Combined FASTA content
        - ncbi_count: int - Number of NCBI sequences fetched
        - local_count: int - Number of local sequences fetched
        - errors: list[str] - Any error messages
    """
    result = {
        'fasta_content': '',
        'ncbi_count': 0,
        'local_count': 0,
        'errors': [],
        # Alan 8/14/26 - Name which requested sources actually failed. Callers used to
        # see only a flat error list and treated "some FASTA came back" as success, so
        # a MycoMap 500 on the NCBI half silently produced a tree with no NCBI
        # references and no warning anywhere the user could see it.
        'failed_sources': [],
    }

    if not include_ncbi and not include_local:
        result['errors'].append("At least one result type must be selected")
        return result

    # One deadline shared by both fetches, so a slow NCBI half cannot spend the
    # local half's time as well.
    deadline = None if time_budget is None else time.monotonic() + time_budget

    fasta_parts = []

    # Fetch NCBI results
    if include_ncbi:
        logger.info(f"Fetching NCBI FASTA for blast_id: {blast_id}")
        ncbi_bytes, ncbi_error = _fetch_fasta(blast_id, 'fasta', deadline)
        if ncbi_error:
            result['errors'].append(ncbi_error)
            result['failed_sources'].append('ncbi')
        else:
            result['ncbi_count'] = _count_fasta_sequences(ncbi_bytes)
            if ncbi_bytes:
                fasta_parts.append(ncbi_bytes.decode('utf-8', errors='replace'))
            logger.info(f"NCBI: fetched {len(ncbi_bytes)} bytes, {result['ncbi_count']} sequences")
    
    # Fetch local/MycoBLAST results
    if include_local:
        logger.info(f"Fetching local FASTA for blast_id: {blast_id}")
        local_bytes, local_error = _fetch_fasta(blast_id, 'localFasta', deadline)
        if local_error:
            result['errors'].append(local_error)
            result['failed_sources'].append('local')
        else:
            result['local_count'] = _count_fasta_sequences(local_bytes)
            if local_bytes:
                fasta_parts.append(local_bytes.decode('utf-8', errors='replace'))
            logger.info(f"Local: fetched {len(local_bytes)} bytes, {result['local_count']} sequences")
    
    # Combine FASTA content
    result['fasta_content'] = '\n'.join(fasta_parts)

    # Alan 8/14/26 - Say out loud when a requested source came back empty but we are
    # returning the other one anyway. Previously this produced a tree missing its
    # entire NCBI reference set with nothing in the logs marking it as degraded --
    # the fetch ERROR was followed by ordinary INFO lines and a completed job.
    if result['failed_sources'] and result['fasta_content']:
        from app.services.log_context import log_degradation
        log_degradation(
            logger,
            "mycomap_partial_fetch",
            f"continuing with {'/'.join(s for s in ('ncbi', 'local') if s not in result['failed_sources'])} "
            f"results only; sequences from {'/'.join(result['failed_sources'])} are missing from this import",
            blast_id=blast_id,
            failed=','.join(result['failed_sources']),
            ncbi_count=result['ncbi_count'],
            local_count=result['local_count'],
        )

    return result


# =============================================================================
# BLAST Metrics Fetcher
# =============================================================================

class _BlastTableParser(html.parser.HTMLParser):
    """Stdlib HTML table extractor. Collects all tables as lists of rows of cell strings."""

    def __init__(self):
        super().__init__()
        self.tables = []       # list of tables
        self._cur_table = None  # list of rows while inside <table>
        self._cur_row = None    # list of cells while inside <tr>
        self._cur_cell = None   # str accumulator while inside <td>/<th>
        self._depth = 0         # table nesting depth

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == 'table':
            self._depth += 1
            if self._depth == 1:
                self._cur_table = []
        elif tag == 'tr' and self._cur_table is not None:
            self._cur_row = []
        elif tag in ('td', 'th') and self._cur_row is not None:
            self._cur_cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == 'table':
            if self._depth == 1 and self._cur_table is not None:
                self.tables.append(self._cur_table)
                self._cur_table = None
            self._depth = max(0, self._depth - 1)
        elif tag == 'tr' and self._cur_table is not None and self._cur_row is not None:
            self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif tag in ('td', 'th') and self._cur_row is not None and self._cur_cell is not None:
            self._cur_row.append(' '.join(''.join(self._cur_cell).split()))
            self._cur_cell = None

    def handle_data(self, data):
        if self._cur_cell is not None:
            self._cur_cell.append(data)


def _parse_blast_metrics_table(rows: list) -> dict:
    """
    Given raw table rows, find the header row and parse per-accession metrics.

    Returns dict[bare_accession -> {identity, query_cover, subject_cover}].
    Returns {} if no usable header found or identity column missing.
    """
    if not rows:
        return {}

    contaminant_re = re.compile(r'contamin(?:a|e)nt', flags=re.IGNORECASE)
    header_idx = None
    header = []
    for i, row in enumerate(rows[:10]):
        lower = [_normalize_header_cell(c) for c in row]
        has_identity = any(
            re.search(r'\bident(?:ity|ities)?\b', c) or 'percent identity' in c
            for c in lower
        )
        has_coverage = any(('query' in c and 'cover' in c) or ('subject' in c and 'cover' in c)
                           or ('q' in c and 'cov' in c) or ('s' in c and 'cov' in c)
                           or c in ('qcov', 'qcovs', 'scov', 'scovs') for c in lower)
        if has_identity and has_coverage:
            header_idx = i
            header = lower
            break

    if header_idx is None or not header:
        return {}

    def find_col(*keyword_groups):
        for group in keyword_groups:
            if isinstance(group, str):
                group = (group,)
            for j, h in enumerate(header):
                if all(kw in h for kw in group):
                    return j
        return None

    direct_hit_cols = []
    for group in (
        'accession',
        ('subject', 'id'),
        ('subject', 'acc'),
        ('hit', 'id'),
        ('sequence', 'id'),
        ('record', 'id'),
        ('result', 'id'),
    ):
        col = find_col(group)
        if col is not None and col not in direct_hit_cols:
            direct_hit_cols.append(col)

    desc_col = find_col('description', ('hit', 'name'), ('sequence', 'name'), 'name', 'title')
    source_col = find_col('source')
    ident_col = find_col('identity', 'ident')
    qcov_col = find_col(('query', 'cover'), ('q', 'cov'), 'qcov')
    scov_col = find_col(('subject', 'cover'), ('subj', 'cover'), ('s', 'cov'), 'scov')

    if ident_col is None:
        return {}

    metric_cols = {col for col in (ident_col, qcov_col, scov_col) if col is not None}
    if direct_hit_cols:
        hit_cols = direct_hit_cols
    else:
        hit_cols = []
        for group in (
            ('hit', 'name'),
            ('sequence', 'name'),
            'observation',
            'voucher',
            'specimen',
            'source',
            'record',
            'name',
            'title',
            'description',
            'taxon',
        ):
            col = find_col(group)
            if col is not None and col not in metric_cols and col not in hit_cols:
                hit_cols.append(col)
        if not hit_cols:
            hit_cols = [0]

    def _to_float(val):
        if val is None:
            return None
        match = re.search(r'-?\d+(?:\.\d+)?', str(val))
        if not match:
            return None
        return float(match.group(0))

    def _cover_values(row):
        query_cover = _to_float(row[qcov_col] if qcov_col is not None and len(row) > qcov_col else None)
        subject_cover = _to_float(row[scov_col] if scov_col is not None and len(row) > scov_col else None)
        if qcov_col is not None and qcov_col == scov_col and len(row) > qcov_col:
            matches = re.findall(r'-?\d+(?:\.\d+)?', str(row[qcov_col]))
            if len(matches) >= 2:
                query_cover = float(matches[0])
                subject_cover = float(matches[1])
        # MycoMap prefixes reverse-strand subject coverage with a minus sign;
        # coverage filtering needs its magnitude because orientation is handled separately.
        if subject_cover is not None:
            subject_cover = abs(subject_cover)
        return query_cover, subject_cover

    result = {}
    for row in rows[header_idx + 1:]:
        if len(row) <= ident_col:
            continue
        metric_keys = []
        primary_identifier = ''
        for hit_col in hit_cols:
            if len(row) <= hit_col:
                continue
            raw_hit = row[hit_col] if row[hit_col] else ''
            if not direct_hit_cols and not _looks_like_hit_identifier(raw_hit):
                continue
            if not primary_identifier:
                primary_identifier = raw_hit
            metric_keys.extend(build_blast_metric_keys(raw_hit))
        if source_col is not None and source_col not in hit_cols and len(row) > source_col:
            raw_source = str(row[source_col] or '').strip()
            if (
                _looks_like_hit_identifier(raw_source)
                or re.match(r'^\d{5,12}(?:\s|$)', raw_source)
            ):
                metric_keys.extend(build_blast_metric_keys(raw_source))
        if not direct_hit_cols:
            for idx, cell in enumerate(row):
                if idx in metric_cols or idx in hit_cols or not _looks_like_hit_identifier(cell):
                    continue
                metric_keys.extend(build_blast_metric_keys(cell))
        metric_keys = _unique_metric_keys(metric_keys)
        if not metric_keys:
            continue
        identity = _to_float(row[ident_col] if len(row) > ident_col else None)
        query_cover, subject_cover = _cover_values(row)
        display_info = _build_result_display_info(
            row[desc_col] if desc_col is not None and len(row) > desc_col else '',
            primary_identifier,
            source=row[source_col] if source_col is not None and len(row) > source_col else '',
        )
        metric = {
            'identity': identity,
            'query_cover': query_cover,
            'subject_cover': subject_cover,
            'is_contaminant': any(
                contaminant_re.search(str(cell or ''))
                for cell in row
            ),
        }
        metric.update(display_info)
        for key in metric_keys:
            result.setdefault(key, metric)

    return result


def _normalize_header_cell(value: str) -> str:
    """Normalize a table header cell for flexible BLAST column matching."""
    value = html.unescape(str(value or '')).lower()
    value = re.sub(r'[%_.-]+', ' ', value)
    return ' '.join(value.split())


def _unique_metric_keys(candidates: list) -> list:
    """Return unique non-empty metric lookup keys while preserving order."""
    keys = []
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        keys.append(candidate)
        seen.add(candidate)
    return keys


def _append_metric_candidate(candidates: list, value: str):
    value = str(value or '').strip()
    if value:
        candidates.append(value)


def _clean_label_fragment(value: str) -> str:
    """Collapse whitespace and trim punctuation from a display-name fragment."""
    value = html.unescape(str(value or ''))
    value = ' '.join(value.split())
    return value.strip(' ,;')


def _first_identifier_token(value: str) -> str:
    """Return a normalized first token for accession/display-name comparison."""
    token = str(value or '').lstrip('>').strip().split()
    if not token:
        return ''
    return token[0].strip('>,;()[]{}').split('.')[0].lower()


def _species_tokens(value: str) -> list:
    """Return alphanumeric tokens used to decide whether a species name is present."""
    return re.findall(r'[a-z0-9]+', html.unescape(str(value or '')).lower())


def _contains_species_name(label: str, species_name: str) -> bool:
    """Return True when all species-name tokens already appear in label order."""
    label_tokens = _species_tokens(label)
    species_tokens = _species_tokens(species_name)
    if not species_tokens:
        return False

    pos = 0
    for species_token in species_tokens:
        try:
            pos = label_tokens.index(species_token, pos) + 1
        except ValueError:
            return False
    return True


def _compact_ncbi_description(description: str) -> str:
    """Return the organism/voucher part of an NCBI BLAST description."""
    text = _clean_label_fragment(description)
    if not text:
        return ''

    type_match = re.search(
        r'\b(?:from\s+)?(?:holo|iso|para|epi|neo|syn|lecto)?type(?:\s+material)?\b',
        text,
        flags=re.IGNORECASE,
    )
    type_marker = _clean_label_fragment(type_match.group(0)) if type_match else ''
    if type_marker:
        type_marker = re.sub(r'(?i)\btype\b', 'TYPE', type_marker)

    marker_pattern = (
        r'\s+(?:small subunit|internal transcribed spacer|large subunit|'
        r'5\.8S|18S|28S|ribosomal RNA|rRNA|ITS\b)'
    )
    match = re.search(marker_pattern, text, flags=re.IGNORECASE)
    if match:
        text = text[:match.start()]
    text = _clean_label_fragment(text)
    if type_marker and type_marker.casefold() not in text.casefold():
        text = _clean_label_fragment(f"{text} {type_marker}")
    return text


def _infer_species_name(description: str) -> str:
    """Infer a binomial-style species name from a BLAST description."""
    text = _compact_ncbi_description(description)
    match = re.match(r'^([A-Z][a-zA-Z-]+)\s+([a-z][a-zA-Z-]+|["\'][^"\']+["\'])\b', text)
    if not match:
        return ''
    return _clean_label_fragment(' '.join(match.groups()))


def parse_mycomap_ncbi_fasta_header(header: str) -> dict:
    """Parse old/new MycoMap NCBI FASTA headers without losing raw metadata.

    Explicit exports use either four DB39-backed fields or a two-field NCBI
    description fallback. Legacy space-delimited exports cannot expose blank
    field boundaries, so they retain the existing label unless their second
    portion clearly looks like a raw NCBI feature description.
    """
    raw_header = str(header or '').lstrip('>').strip()
    result = {
        'accession': '',
        'taxon': '',
        'raw_mycomap_taxon': '',
        'voucher': '',
        'location': '',
        'raw_fasta_header': raw_header,
        'raw_ncbi_description': '',
        'mycomap_header_format': 'legacy_flat',
        'display_name': raw_header,
    }
    if not raw_header:
        return result

    fields = None
    if ' - ' in raw_header or raw_header.endswith(' -'):
        # str.strip() removes the final space from an empty fourth field but
        # leaves the hyphen. Add it back before splitting to preserve position.
        split_header = f"{raw_header} " if raw_header.endswith(' -') else raw_header
        candidate = split_header.split(' - ', 3)
        if len(candidate) in (2, 4):
            fields = candidate
    elif '|' in raw_header:
        candidate = raw_header.split('|')
        if len(candidate) in (2, 4):
            fields = candidate

    if fields:
        fields = [_clean_label_fragment(field) for field in fields]
        accession = fields[0]
        result['accession'] = accession
        if len(fields) == 4:
            taxon, voucher, location = fields[1:]
            display_taxon = _compact_ncbi_description(taxon)
            result.update({
                'taxon': display_taxon,
                'raw_mycomap_taxon': taxon,
                'voucher': voucher,
                'location': location,
                'mycomap_header_format': 'db39',
                'display_name': _clean_label_fragment(
                    ' '.join(
                        part for part in (accession, display_taxon, voucher, location)
                        if part
                    )
                ),
            })
            return result

        raw_description = fields[1]
        compact_description = _compact_ncbi_description(raw_description)
        result.update({
            'taxon': _infer_species_name(raw_description),
            'raw_ncbi_description': raw_description,
            'mycomap_header_format': 'ncbi_description',
            'display_name': _clean_label_fragment(
                ' '.join(part for part in (accession, compact_description) if part)
            ),
        })
        return result

    parts = raw_header.split(None, 1)
    result['accession'] = parts[0]
    description = parts[1] if len(parts) > 1 else ''
    if re.search(
        r'\b(?:small subunit|internal transcribed spacer|large subunit|5\.8S|'
        r'18S|28S|ribosomal RNA|rRNA|ITS(?:\s+region)?\b)',
        description,
        flags=re.IGNORECASE,
    ):
        result.update({
            'taxon': _infer_species_name(description),
            'raw_ncbi_description': description,
            'mycomap_header_format': 'ncbi_description',
            'display_name': _clean_label_fragment(
                ' '.join((parts[0], _compact_ncbi_description(description)))
            ),
        })
    return result


def compact_mycomap_ncbi_header(header: str) -> str:
    """Return the compact display name for a MycoMap/NCBI FASTA header.

    Thin wrapper over parse_mycomap_ncbi_fasta_header() for callers that only
    write FASTA and have no place to keep the parsed fields. A header that is
    not in one of the recognized NCBI export formats comes back unchanged, so
    this is safe to apply to arbitrary pasted FASTA.
    """
    details = parse_mycomap_ncbi_fasta_header(header)
    return details.get('display_name') or str(header or '')


def uniquify_mycomap_sequence_names(sequences: list) -> list:
    """Give repeated MycoMap hit identifiers stable occurrence suffixes."""
    used_ids = set()
    next_occurrence = {}

    for index, sequence in enumerate(sequences, start=1):
        name = _clean_label_fragment(sequence.get('name', ''))
        parts = name.split(None, 1)
        base_id = str(sequence.get('accession') or (parts[0] if parts else '')).strip()
        if not base_id:
            base_id = f"MycoMap_hit_{index}"
        description = parts[1] if len(parts) > 1 else ''

        base_key = base_id.casefold()
        occurrence = next_occurrence.get(base_key, 0) + 1
        candidate = base_id if occurrence == 1 else f"{base_id}_{occurrence}"
        while candidate.casefold() in used_ids:
            occurrence += 1
            candidate = f"{base_id}_{occurrence}"

        next_occurrence[base_key] = occurrence
        used_ids.add(candidate.casefold())
        sequence['display_label'] = name
        sequence['internal_id'] = candidate
        sequence['occurrence'] = occurrence
        sequence['name'] = _clean_label_fragment(
            ' '.join(part for part in (candidate, description) if part)
        )

    return sequences


def _source_column_species_name(source: str, description: str = '') -> str:
    """
    Recover a taxon from the BLAST table's Source column.

    A MycoMap local hit whose sequence record carries no record-level species
    has a bare "iNaturalist #<id>" Description with no "Species Name:" badge,
    and the taxon appears only in the Source column as "<id> - <taxon>" (the
    linked observation). Rows sourced from an annotation file put a file title
    there instead, so the "<id> - " prefix is required, and it must agree with
    any observation id the Description already names.
    """
    text = _clean_label_fragment(source)
    if not text:
        return ''

    match = re.match(r'^(\d{1,12})\s*[-‐-―]\s*(.+)$', text)
    if not match:
        return ''

    source_id, species_name = match.group(1), _clean_label_fragment(match.group(2))
    if not species_name:
        return ''

    description_ids = extract_inaturalist_observation_ids(description)
    if description_ids and source_id not in description_ids:
        return ''
    return species_name


def _build_result_display_info(description: str, identifier: str = '',
                               *, source: str = '') -> dict:
    """Extract compact display-name metadata from a MycoMap BLAST result row."""
    text = _clean_label_fragment(description)
    identifier = str(identifier or '').strip()
    if not text:
        return {}

    species_name = ''
    location = ''

    species_match = re.search(
        r'\bSpecies Name:\s*(.*?)(?=\s+Location:|$)',
        text,
        flags=re.IGNORECASE,
    )
    if species_match:
        species_name = _clean_label_fragment(species_match.group(1))

    location_match = re.search(r'\bLocation:\s*(.*)$', text, flags=re.IGNORECASE)
    if location_match:
        location = _clean_label_fragment(location_match.group(1))

    if not species_name:
        # The Source column carries the observation's taxon when the sequence
        # record itself has none; without this the hit keeps a name-less label.
        species_name = _source_column_species_name(source, text)
        if species_name:
            display_name = _clean_label_fragment(
                ' '.join(part for part in (identifier, species_name, location) if part)
            )
            result = {
                'species_name': species_name,
                'mycomap_location': location,
            }
            if display_name:
                result['display_name'] = display_name
            return result

    if not species_name:
        species_name = _infer_species_name(text)
        compact_description = _compact_ncbi_description(text)
        if species_name and compact_description:
            display_name = _clean_label_fragment(' '.join(part for part in (identifier, compact_description) if part))
            return {
                'species_name': species_name,
                'display_name': display_name,
            }
        return {}

    display_parts = [identifier, species_name, location]
    display_name = _clean_label_fragment(' '.join(part for part in display_parts if part))
    result = {
        'species_name': species_name,
        'mycomap_location': location,
    }
    if display_name:
        result['display_name'] = display_name
    return result


def improve_mycomap_sequence_name(current_name: str, metric: Optional[dict],
                                   hit_source: str = '', *, accession: str = '',
                                   voucher: str = '', location: str = '') -> str:
    """
    Use MycoMap table metadata to repair stale or sparse FASTA headers.

    MycoMap's FASTA export can emit headers such as "MH855376 England GB" even
    when the BLAST results table has "Species Name: Ascobolus equinus". Local
    FASTA headers can also retain an outdated source taxon after the current
    MycoMap species name has changed.
    """
    if not metric:
        return current_name

    display_name = metric.get('display_name') or ''
    species_name = metric.get('species_name') or ''
    if not species_name:
        return current_name
    if hit_source == 'local':
        identifier = str(current_name or '').lstrip('>').strip().split()
        if not identifier:
            return current_name
        observation_ids = extract_inaturalist_observation_ids(current_name)
        label_identifier = identifier[0]
        if observation_ids:
            inat_token = f"iNat{observation_ids[0]}"
            species_token = species_name.split()[0] if species_name.split() else ''
            if label_identifier.casefold() == species_token.casefold():
                label_identifier = inat_token
            elif label_identifier.casefold() != inat_token.casefold():
                label_identifier = f"{label_identifier} {inat_token}"
        location = metric.get('mycomap_location') or ''
        return _clean_label_fragment(
            ' '.join(part for part in (label_identifier, species_name, location) if part)
        )
    if hit_source and hit_source != 'ncbi':
        return current_name
    if not display_name:
        return current_name
    if _first_identifier_token(current_name) != _first_identifier_token(display_name):
        return current_name
    if _contains_species_name(current_name, species_name):
        return current_name
    if accession:
        # The result table reflects MycoMap's current, locally curated taxon,
        # while the NCBI FASTA export can retain an older DB39 taxon. Keep the
        # structured NCBI metadata, but give the current MycoMap taxon priority.
        current_location = metric.get('mycomap_location') or location
        return _clean_label_fragment(
            ' '.join(
                part for part in (accession, species_name, voucher, current_location)
                if part
            )
        )
    return display_name


def prefer_local_mycomap_taxa(sequences: list) -> list:
    """Give exact-sequence local MycoMap taxa priority over NCBI taxa.

    Local records are refreshed more frequently than NCBI/DB39 metadata. Only
    unambiguous exact sequence matches are used: if local records with the same
    sequence disagree on the taxon, the NCBI label is left unchanged.
    """
    local_taxa = {}
    conflicting_keys = set()

    def sequence_key(sequence: dict) -> str:
        normalized = _normalize_dna_for_near_duplicate_comparison(
            sequence.get('sequence', '')
        )
        if not normalized:
            return ''
        reverse_complement = normalized.translate(_IUPAC_COMPLEMENT)[::-1]
        return min(normalized, reverse_complement)

    for sequence in sequences:
        if sequence.get('hit_source') != 'local':
            continue
        key = sequence_key(sequence)
        taxon = _clean_label_fragment(sequence.get('taxon', ''))
        if not key or not taxon:
            continue
        previous = local_taxa.get(key)
        if previous and previous.casefold() != taxon.casefold():
            conflicting_keys.add(key)
        else:
            local_taxa[key] = taxon

    for sequence in sequences:
        if sequence.get('hit_source') != 'ncbi':
            continue
        key = sequence_key(sequence)
        local_taxon = local_taxa.get(key)
        if not local_taxon or key in conflicting_keys:
            continue
        if str(sequence.get('taxon') or '').casefold() == local_taxon.casefold():
            continue

        identifier = str(
            sequence.get('accession')
            or sequence.get('internal_id')
            or str(sequence.get('name') or '').split()[0]
        ).strip()
        sequence['name'] = _clean_label_fragment(
            ' '.join(
                part for part in (
                    identifier,
                    local_taxon,
                    sequence.get('voucher', ''),
                    sequence.get('location', ''),
                )
                if part
            )
        )
        sequence['taxon'] = local_taxon

    return sequences


def _local_observation_candidates(text: str) -> list:
    """Return normalized MycoMap-local observation keys from a hit label."""
    candidates = []
    local_patterns = [
        (
            r'\bi\s*nat(?:uralist)?(?:\.org)?'
            r'(?:\s*/\s*observations?)?[\s#:/-]*(\d{5,12})\b',
            'iNat'
        ),
        (
            r'\binaturalist(?:\.org)?'
            r'(?:\s*/\s*observations?)?[\s#:/-]*(\d{5,12})\b',
            'iNat'
        ),
        (
            r'\bmushroom\s*observer(?:\.org)?[\s#:/-]*(\d{3,12})\b',
            'MO'
        ),
        (
            r'\bmushroomobserver\.org[\s#/:-]*(\d{3,12})\b',
            'MO'
        ),
        (
            r'\bMO[\s#:/-]*(\d{3,12})\b',
            'MO'
        ),
    ]
    for pattern, prefix in local_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            digits = match.group(1)
            candidates.append(f"{prefix}{digits}")
            candidates.append(digits)
    return candidates


def extract_inaturalist_observation_ids(text: str) -> list:
    """Return unique iNaturalist observation IDs found anywhere in a hit label."""
    observation_ids = []
    seen = set()
    for candidate in _local_observation_candidates(text):
        match = re.fullmatch(r'iNat(\d{5,12})', candidate, flags=re.IGNORECASE)
        if not match or match.group(1) in seen:
            continue
        observation_ids.append(match.group(1))
        seen.add(match.group(1))
    return observation_ids


def _looks_like_hit_identifier(value: str) -> bool:
    """Return True when a table cell is likely to contain a hit identifier."""
    text = html.unescape(str(value or '')).strip()
    if not text:
        return False
    if _local_observation_candidates(text):
        return True
    if re.search(r'\b[A-Z]{1,6}_?\d{3,12}(?:\.\d+)?\b', text, flags=re.IGNORECASE):
        return True
    first_token = text.split()[0] if text.split() else text
    return bool(re.search(r'[A-Za-z]', first_token) and re.search(r'\d', first_token))


def build_blast_metric_keys(label: str) -> list:
    """Return stable lookup keys for a BLAST table hit label or FASTA header."""
    text = html.unescape(str(label or '')).strip()
    if not text:
        return []

    text = text.lstrip('>').strip()
    first_token = text.split()[0] if text.split() else text
    candidates = []
    _append_metric_candidate(candidates, text)
    _append_metric_candidate(candidates, first_token)

    # Many BLAST identifiers use pipe-delimited prefixes such as gb|ACCESSION.1|.
    for part in re.split(r'[|;,]', first_token):
        part = part.strip()
        if part and part.lower() not in {'gb', 'emb', 'dbj', 'ref', 'gi', 'lcl'}:
            _append_metric_candidate(candidates, part)

    # Capture accession-like tokens anywhere in the label without indexing every
    # species-name word, which would create noisy matches.
    candidates.extend(re.findall(r'\b[A-Z]{1,6}_?\d{3,12}(?:\.\d+)?\b', text, flags=re.IGNORECASE))
    candidates.extend(_local_observation_candidates(text))

    keys = []
    seen = set()
    for candidate in candidates:
        cleaned = candidate.strip().strip('>,;()[]{}')
        if not cleaned:
            continue
        variants = [cleaned]
        if '.' in cleaned:
            variants.append(cleaned.split('.')[0])
        for variant in variants:
            if variant and variant not in seen:
                keys.append(variant)
                seen.add(variant)

    return keys


def _metrics_page_urls(blast_id: str, source_url: Optional[str] = None) -> list:
    urls = []
    if source_url:
        source_blast_id = validate_mycomap_url(source_url)
        if source_blast_id == blast_id:
            urls.append(source_url)
        else:
            logger.warning("fetch_mycomap_blast_metrics: ignored source_url with mismatched or invalid blast_id")
    urls.append(f"https://mycomap.com/genetics/blast-search/r{blast_id}/")
    return list(dict.fromkeys(urls))


def fetch_mycomap_blast_metrics(blast_id: str, source_url: Optional[str] = None) -> dict:
    """
    Fetch BLAST metrics from the MycoMap user-facing BLAST results page.

    Args:
        blast_id: Digits-only blast ID string.

    Returns:
        dict[bare_accession -> {identity, query_cover, subject_cover}]
        Returns {} on any error (non-fatal).
    """
    if not blast_id.isdigit():
        logger.warning(f"fetch_mycomap_blast_metrics: invalid blast_id '{blast_id}'")
        return {}

    opener = urllib.request.build_opener()
    opener.addheaders = [
        ('User-Agent', 'Dikarya-TreeBuilder/1.0'),
        ('Accept', 'text/html,*/*')
    ]

    content = ''
    for url in _metrics_page_urls(blast_id, source_url):
        try:
            with diagnostic_urlopen(url, timeout=REQUEST_TIMEOUT, opener=opener.open) as resp:
                content = resp.read().decode('utf-8', errors='replace')
            break
        except Exception as e:
            logger.warning(f"fetch_mycomap_blast_metrics: could not fetch page {url}: {e}")

    if not content:
        return {}

    try:
        parser = _BlastTableParser()
        parser.feed(content)
        combined = {}
        for table_rows in parser.tables:
            metrics = _parse_blast_metrics_table(table_rows)
            # Later tables don't overwrite earlier ones (prefer first match)
            for acc, m in metrics.items():
                combined.setdefault(acc, m)
        logger.info(f"fetch_mycomap_blast_metrics: parsed {len(combined)} accession(s) from {len(parser.tables)} table(s)")
        return combined
    except Exception as e:
        logger.warning(f"fetch_mycomap_blast_metrics: parse error: {e}", exc_info=True)
        record_api_failure(url, reason="invalid_blast_metrics_html", status=200, body=content)
        return {}
