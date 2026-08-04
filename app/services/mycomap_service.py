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
import re
import shlex
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Timeout for network requests in seconds
REQUEST_TIMEOUT = 10

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
MYCOMAP_NCBI_RERUN_WAIT_SECONDS = 600
MYCOMAP_NCBI_POLL_INTERVAL_SECONDS = 60
MYCOMAP_NCBI_POLL_MAX_ATTEMPTS = 120
MYCOMAP_NCBI_LOCAL_FALLBACK_SECONDS = 900
MYCOMAP_NCBI_RECHECK_MAX_HOURS = 48
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


MYCOMAP_OBSERVATION_REF_RE = re.compile(r"^(?:inat|mo):\d{1,12}$")


def extract_mycomap_observation_reference(value: str) -> Optional[str]:
    """Return an ``inat:<id>`` or ``mo:<id>`` reference found in a tip label."""
    text = html.unescape(str(value or ""))
    if not text:
        return None

    inat_patterns = (
        r"\binat\s*:\s*(\d{1,12})\b",
        r"\b(?:https?://)?(?:www\.)?(?:[a-z0-9-]+\.)*inaturalist(?:\.[a-z0-9-]+)+/observations/(\d{1,12})\b",
        r"\bi\s*naturalist(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{5,12})(?:[_-]\d+)?\b",
        r"\binaturalist(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{5,12})(?:[_-]\d+)?\b",
        r"\binat(?:uralist)?(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{5,12})(?:[_-]\d+)?\b",
    )
    for pattern in inat_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"inat:{match.group(1)}"

    mo_patterns = (
        r"\bmo\s*:\s*(\d{1,12})\b",
        r"\b(?:https?://)?(?:www\.)?mushroomobserver\.org/(?:obs/)?(\d{1,12})\b",
        r"\bmushroom\s*observer(?:\s*(?:observation|obs))?\s*[-_#:\s]*(\d{1,12})(?:[_-]\d+)?\b",
        r"\bmo\s*#?\s*(\d{5,12})(?:[_-]\d+)?\b",
    )
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
        blast_id = validate_mycomap_url(candidate)
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


def find_mycomap_blast_by_title(title: str) -> Optional[dict]:
    """Find the newest public MycoMap BLAST record matching an exact job title."""
    wanted = " ".join(str(title or "").split()).strip()
    if not wanted:
        return None
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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not check MycoMap for existing BLAST title %s: %s", wanted, exc)
        return None

    link_pattern = re.compile(
        r"<a\b[^>]*href=['\"](?P<url>[^'\"]*?/genetics/blast-search/[^'\"]*?r\d+/?)[^>]*>"
        r"(?P<label>.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in link_pattern.finditer(page):
        label = html.unescape(re.sub(r"<[^>]+>", " ", match.group("label")))
        label = " ".join(label.split()).strip()
        if label != wanted and not label.startswith(f"{wanted} - "):
            continue
        url = urllib.parse.urljoin("https://mycomap.com", html.unescape(match.group("url")))
        blast_id = validate_mycomap_url(url)
        if blast_id:
            return {"blast_id": blast_id, "url": url, "title": wanted}
    return None


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
        with urllib.request.urlopen(request, timeout=MYCOMAP_RERUN_REQUEST_TIMEOUT) as resp:
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
        logger.error("MycoMap BLAST creation network error: %s", exc)
        raise MycoMapCreateError("MycoMap BLAST creation network error.")
    except TimeoutError:
        logger.error("MycoMap BLAST creation timed out")
        raise MycoMapCreateError("MycoMap BLAST creation timed out.")
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


def get_mycomap_ncbi_result_count(blast_id: str) -> Tuple[int, list]:
    """Return the currently exported NCBI hit count and any fetch warnings."""
    result = fetch_mycomap_fasta(
        str(blast_id), include_ncbi=True, include_local=False
    )
    return int(result.get("ncbi_count") or 0), list(result.get("errors") or [])


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
        with urllib.request.urlopen(request, timeout=MYCOMAP_RERUN_REQUEST_TIMEOUT) as resp:
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
                             data: Optional[dict] = None):
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
        with urllib.request.urlopen(request, timeout=MYCOMAP_RERUN_REQUEST_TIMEOUT) as resp:
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
        logger.error("MycoMap refresh network error for %s: %s", path, exc)
        raise MycoMapRefreshError("MycoMap refresh network error.")
    except TimeoutError:
        logger.error("MycoMap refresh timed out for %s", path)
        raise MycoMapRefreshError("MycoMap refresh timed out.")
    except Exception as exc:
        logger.error("Unexpected MycoMap refresh error for %s: %s", path, exc, exc_info=True)
        raise MycoMapRefreshError("MycoMap refresh failed unexpectedly.")

    try:
        return json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.error("MycoMap refresh API returned non-JSON for %s", path)
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


def validate_mycomap_url(url: str) -> Optional[str]:
    """
    Validate a Mycomap URL and extract the blast_id.
    
    Uses strict hostname checking to prevent bypass attacks like:
    - https://evil.com/?q=mycomap.com/r12345
    - https://mycomap.com.evil.com/r12345
    
    Args:
        url: The URL to validate (e.g., "https://mycomap.com/...r12345...")
        
    Returns:
        The blast_id (digits only) if valid, None otherwise.
    """
    if not url:
        return None
    
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        logger.warning(f"URL validation failed: could not parse URL: {url}")
        return None
    
    # Strict hostname check - must be exactly mycomap.com or www.mycomap.com
    hostname = (parsed.hostname or '').lower()
    valid_hostnames = ['mycomap.com', 'www.mycomap.com']
    if hostname not in valid_hostnames:
        logger.warning(f"URL validation failed: invalid hostname '{hostname}' (expected mycomap.com): {url}")
        return None
    
    # Enforce http or https protocol
    if parsed.scheme not in ('http', 'https'):
        logger.warning(f"URL validation failed: invalid scheme '{parsed.scheme}' (expected http/https): {url}")
        return None
    
    # Extract the r<digits> pattern from path or query
    # Use word boundary to avoid matching middle of other tokens
    search_text = parsed.path + '?' + (parsed.query or '')
    match = re.search(r'(?:^|[^a-zA-Z0-9])r(\d+)', search_text)
    if not match:
        logger.warning(f"URL validation failed: no r<digits> pattern found in: {url}")
        return None
    
    blast_id = match.group(1)
    logger.info(f"Validated Mycomap URL, extracted blast_id: {blast_id}")
    return blast_id


_NCBI_QUEUE_POSITION_RE = re.compile(
    r"position of this BLAST search in the queue is:\s*(\d+)", re.IGNORECASE
)


def get_mycomap_ncbi_queue_position(mycomap_url: str) -> Optional[int]:
    """
    Best-effort scrape of the NCBI BLAST queue position from a Mycomap
    result page (e.g. "The position of this BLAST search in the queue
    is: 1."). Mycomap does not expose this via a JSON API, so this reads
    the same page a user would see in their browser.

    Returns None if the page doesn't show a queue position (e.g. results
    are already ready) or on any fetch error.
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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            content = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        logger.warning(f"Could not fetch Mycomap page to check queue position: {e}")
        return None

    match = _NCBI_QUEUE_POSITION_RE.search(content)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _count_fasta_sequences(fasta_bytes: bytes) -> int:
    """Return the number of sequences in a FASTA file (bytes)."""
    return sum(1 for line in fasta_bytes.splitlines() if line.startswith(b'>'))


def _fetch_fasta(blast_id: str, endpoint: str) -> Tuple[bytes, Optional[str]]:
    """
    Fetch FASTA content from Mycomap.
    
    Args:
        blast_id: The blast ID to fetch
        endpoint: Either 'fasta' (NCBI) or 'localFasta' (local/MycoBLAST)
        
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
    
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            content = resp.read()
        return content, None
    except urllib.error.URLError as e:
        error_msg = f"Network error fetching {endpoint}: {e}"
        logger.error(error_msg)
        return b'', error_msg
    except TimeoutError:
        error_msg = f"Request timed out after {REQUEST_TIMEOUT}s for {endpoint}"
        logger.error(error_msg)
        return b'', error_msg
    except Exception as e:
        error_msg = f"Unexpected error fetching {endpoint}: {e}"
        logger.error(error_msg, exc_info=True)
        return b'', error_msg


def fetch_mycomap_fasta(
    blast_id: str,
    include_ncbi: bool = True,
    include_local: bool = True
) -> dict:
    """
    Fetch FASTA sequences from Mycomap BLAST results.
    
    Args:
        blast_id: The Mycomap blast ID (digits only)
        include_ncbi: Whether to include NCBI BLAST results
        include_local: Whether to include local MycoBLAST results
        
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
        'errors': []
    }
    
    if not include_ncbi and not include_local:
        result['errors'].append("At least one result type must be selected")
        return result
    
    fasta_parts = []
    
    # Fetch NCBI results
    if include_ncbi:
        logger.info(f"Fetching NCBI FASTA for blast_id: {blast_id}")
        ncbi_bytes, ncbi_error = _fetch_fasta(blast_id, 'fasta')
        if ncbi_error:
            result['errors'].append(ncbi_error)
        else:
            result['ncbi_count'] = _count_fasta_sequences(ncbi_bytes)
            if ncbi_bytes:
                fasta_parts.append(ncbi_bytes.decode('utf-8', errors='replace'))
            logger.info(f"NCBI: fetched {len(ncbi_bytes)} bytes, {result['ncbi_count']} sequences")
    
    # Fetch local/MycoBLAST results
    if include_local:
        logger.info(f"Fetching local FASTA for blast_id: {blast_id}")
        local_bytes, local_error = _fetch_fasta(blast_id, 'localFasta')
        if local_error:
            result['errors'].append(local_error)
        else:
            result['local_count'] = _count_fasta_sequences(local_bytes)
            if local_bytes:
                fasta_parts.append(local_bytes.decode('utf-8', errors='replace'))
            logger.info(f"Local: fetched {len(local_bytes)} bytes, {result['local_count']} sequences")
    
    # Combine FASTA content
    result['fasta_content'] = '\n'.join(fasta_parts)

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


def _build_result_display_info(description: str, identifier: str = '') -> dict:
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
            with opener.open(url, timeout=REQUEST_TIMEOUT) as resp:
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
        return {}
