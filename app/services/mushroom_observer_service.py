"""Mushroom Observer observation import and one-click tree integration."""
from __future__ import annotations

import logging
import hashlib
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

logger = logging.getLogger(__name__)

MO_API_BASE = "https://mushroomobserver.org/api2"
MO_OBSERVATION_BASE = "https://mushroomobserver.org/obs"
USER_AGENT = "Dikarya Phylogenetic Tree Builder 1.0"
REQUEST_TIMEOUT = 30
MAX_RAW_INPUT_LEN = 500
MO_COMMENT_SUMMARY = "Phylogenetic tree"


class MushroomObserverError(Exception):
    """User-facing Mushroom Observer error with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_mushroom_observer_input(raw_input: str) -> int:
    """Return an observation ID from a bare ID or an official MO URL shape."""
    raw = str(raw_input or "").strip()
    if not raw:
        raise MushroomObserverError("Enter a Mushroom Observer observation URL or number.")
    if len(raw) > MAX_RAW_INPUT_LEN:
        raise MushroomObserverError("Mushroom Observer input is too long.")
    if raw.isdigit():
        if len(raw) > 12 or int(raw) <= 0:
            raise MushroomObserverError("Mushroom Observer observation number is invalid.")
        return int(raw)

    candidate = raw if re.match(r"^https?://", raw, re.IGNORECASE) else f"https://{raw}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        raise MushroomObserverError("That Mushroom Observer URL is invalid.")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise MushroomObserverError("Mushroom Observer URLs must use HTTP or HTTPS.")
    if parsed.username or parsed.password or port is not None:
        raise MushroomObserverError("That Mushroom Observer URL is invalid.")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname not in {"mushroomobserver.org", "www.mushroomobserver.org"}:
        raise MushroomObserverError("That URL is not from mushroomobserver.org.")

    path = urllib.parse.unquote(parsed.path or "")
    patterns = (
        r"^/(\d+)/?$",
        r"^/obs/(\d+)/?$",
        r"^/observations/(\d+)/?$",
        r"^/observer/show_observation/(\d+)/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, path, re.IGNORECASE)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    raise MushroomObserverError(
        "Enter a Mushroom Observer observation URL, such as "
        "mushroomobserver.org/obs/575883, or its observation number."
    )


def _map_upstream_status(upstream_code: int) -> Tuple[int, str]:
    """Translate a Mushroom Observer HTTP status into ours, plus a message.

    Forwarding the upstream code verbatim for everything under 500 was wrong in
    both directions:

    * an upstream 401/403 became a Dikarya 401/403, telling a signed-in user
      that *they* were not authorized when the failure is entirely between this
      server and Mushroom Observer;
    * every other 4xx (400 from a malformed request of ours, 422, and so on)
      became the user's problem too.

    So the only upstream status that keeps its meaning is 404, which really does
    mean "no such observation", and 429, which really does mean "come back
    later" (503, because the throttling is on our shared client, not on this
    user). Everything else is an upstream/integration fault and is reported as
    such: 502.
    """
    if upstream_code == 404:
        return 404, "That observation was not found on Mushroom Observer."
    if upstream_code == 429:
        return 503, ("Mushroom Observer is rate-limiting requests right now. "
                     "Please try again in a minute.")
    if upstream_code >= 500:
        return 502, f"Mushroom Observer API returned HTTP {upstream_code}."
    return 502, (f"Mushroom Observer rejected the request (HTTP "
                 f"{upstream_code}). This is a problem with the Mushroom "
                 f"Observer integration, not with your account.")


def _api_request(table: str, *, params: Optional[Dict[str, Any]] = None,
                 method: str = "GET", body: Optional[Dict[str, Any]] = None
                 ) -> Dict[str, Any]:
    query = urllib.parse.urlencode(params or {})
    url = f"{MO_API_BASE}/{table}"
    if query:
        url = f"{url}?{query}"
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            import json

            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        status, message = _map_upstream_status(exc.code)
        logger.warning(
            "Mushroom Observer API HTTP error table=%s method=%s status=%s",
            table, method, exc.code,
        )
        raise MushroomObserverError(message, status=status)
    except urllib.error.URLError as exc:
        logger.warning(
            "Mushroom Observer API network error table=%s method=%s reason=%s",
            table, method, exc.reason,
        )
        raise MushroomObserverError("Mushroom Observer could not be reached.", status=502)
    except TimeoutError:
        logger.warning(
            "Mushroom Observer API timed out table=%s method=%s", table, method
        )
        raise MushroomObserverError(
            f"Mushroom Observer did not respond within {REQUEST_TIMEOUT} seconds. "
            "Please try again.",
            status=504,
        )
    except ValueError:
        logger.warning(
            "Mushroom Observer API returned invalid JSON table=%s method=%s",
            table, method,
        )
        raise MushroomObserverError("Mushroom Observer returned an invalid response.", status=502)

    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        detail = str((errors[0] or {}).get("details") or "Mushroom Observer API error.")
        safe_detail = _clean_text(detail, 300)
        logger.warning(
            "Mushroom Observer API error payload table=%s method=%s detail=%s",
            table, method, safe_detail,
        )
        raise MushroomObserverError(safe_detail, status=502)
    if not isinstance(payload, dict):
        logger.warning(
            "Mushroom Observer API returned non-object JSON table=%s method=%s type=%s",
            table, method, type(payload).__name__,
        )
        raise MushroomObserverError("Mushroom Observer returned an invalid response.", status=502)
    return payload


def fetch_observation(observation_id: int) -> Dict[str, Any]:
    payload = _api_request("observations", params={
        "id": int(observation_id), "detail": "high", "format": "json"
    })
    results = payload.get("results") or []
    if not results:
        raise MushroomObserverError(
            f"Mushroom Observer observation {int(observation_id)} was not found.", status=404
        )
    return results[0]


def _coerce_sequence_id(value: Any) -> Optional[int]:
    """Return a positive sequence id, or None for anything unusable.

    Accepts the integer or numeric string Mushroom Observer normally sends and
    rejects everything else -- nulls, names, nested objects -- without raising.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _fetch_sequence_details(sequence_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not sequence_ids:
        return {}
    payload = _api_request("sequences", params={
        "id": ",".join(str(int(value)) for value in sequence_ids),
        "detail": "high",
        "format": "json",
    })
    details = {}
    for record in (payload.get("results") or []):
        if not isinstance(record, dict):
            continue
        record_id = _coerce_sequence_id(record.get("id"))
        if record_id is not None:
            details[record_id] = record
    return details


def _clean_text(value: Any, max_length: int = 500) -> str:
    text = re.sub(r'[<>"`]', "", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:max_length]


def _mycomap_blast_url_from_notes(notes: Any) -> str:
    """Return the first validated MycoMap result URL in sequence notes."""
    text = str(notes or "")
    from app.services.mycomap_service import validate_mycomap_url

    for match in re.finditer(
        r"https?://(?:www\.)?mycomap\.com/[^\s<>\"']+",
        text,
        re.IGNORECASE,
    ):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        if validate_mycomap_url(candidate):
            return candidate
    return ""


def _clean_sequence_notes(notes: Any) -> str:
    """Convert simple Mushroom Observer HTML notes into readable plain text."""
    without_tags = re.sub(r"<[^>]*>", " ", str(notes or ""))
    return _clean_text(html.unescape(without_tags), 1000)


def _consensus_name(observation: Dict[str, Any]) -> str:
    return _clean_text((observation.get("consensus") or {}).get("name"), 300)


def _location_name(observation: Dict[str, Any]) -> str:
    return _clean_text((observation.get("location") or {}).get("name"), 300)


def _compact_location(observation: Dict[str, Any]) -> str:
    raw = _location_name(observation)
    if not raw:
        return ""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) >= 2:
        return _clean_text(" ".join(parts[-2:]), 120)
    return _clean_text(parts[0], 120)


def _sequence_candidate(record: Dict[str, Any], observation_id: int) -> Optional[Dict[str, Any]]:
    locus = _clean_text(record.get("locus"), 100)
    if "its" not in locus.casefold():
        return None
    from app.services.fasta_utils import clean_dna_sequence

    raw_bases = str(record.get("bases") or "")
    cleaned = clean_dna_sequence(raw_bases)
    if not cleaned:
        return None
    owner = record.get("user") or {}
    ambiguous = sum(1 for base in cleaned if base not in "ACGT")
    candidate_id = _coerce_sequence_id(record.get("id"))
    if candidate_id is None:
        return None
    return {
        "id": candidate_id,
        "observation_id": int(observation_id),
        "locus": locus,
        "sequence": cleaned,
        "length": len(cleaned),
        "source_length": len(re.sub(r"\s+", "", raw_bases)),
        "ambiguous_bases": ambiguous,
        "archive": _clean_text(record.get("archive"), 100),
        "accession": _clean_text(record.get("accession"), 255),
        "notes": _clean_sequence_notes(record.get("notes")),
        "mycomap_blast_url": _mycomap_blast_url_from_notes(record.get("notes")),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "contributor": {
            "id": owner.get("id"),
            "login_name": _clean_text(owner.get("login_name"), 150),
            "legal_name": _clean_text(owner.get("legal_name"), 150),
        },
    }


def analyze_observation(raw_input: str) -> Dict[str, Any]:
    observation_id = parse_mushroom_observer_input(raw_input)
    observation = fetch_observation(observation_id)
    embedded = observation.get("sequences") or []
    if not isinstance(embedded, list):
        logger.warning(
            "Mushroom Observer observation %s returned a non-list `sequences` "
            "field (%s); treating it as empty.",
            observation_id, type(embedded).__name__,
        )
        embedded = []

    # Everything below comes from an external API, so a single unusable row is
    # skipped rather than allowed to raise. `int(item["id"])` on a null, a
    # string, or a nested object used to abort the whole import and leave the
    # user with a 500 for an observation whose other sequences were perfectly
    # good.
    usable = []
    skipped = 0
    for item in embedded:
        if not isinstance(item, dict):
            skipped += 1
            continue
        sequence_id = _coerce_sequence_id(item.get("id"))
        if sequence_id is None:
            skipped += 1
            continue
        usable.append((sequence_id, item))
    if skipped:
        logger.warning(
            "Mushroom Observer observation %s: skipped %s sequence record(s) "
            "with a missing or non-numeric id.", observation_id, skipped,
        )

    detailed = _fetch_sequence_details([sequence_id for sequence_id, _ in usable])
    candidates = []
    for sequence_id, embedded_record in usable:
        record = dict(embedded_record)
        record.update(detailed.get(sequence_id) or {})
        candidate = _sequence_candidate(record, observation_id)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item["id"])

    owner = observation.get("owner") or {}
    result = {
        "status": "success",
        "observation": {
            "id": observation_id,
            "url": f"{MO_OBSERVATION_BASE}/{observation_id}",
            "date": str(observation.get("date") or ""),
            "consensus_name": _consensus_name(observation),
            "location": _location_name(observation),
            "owner": {
                "id": owner.get("id"),
                "login_name": _clean_text(owner.get("login_name"), 150),
                "legal_name": _clean_text(owner.get("legal_name"), 150),
            },
        },
        "its_sequences": candidates,
        "dna_count": len(candidates),
        "message": (
            f"Found {len(candidates)} usable ITS sequence"
            f"{'s' if len(candidates) != 1 else ''} on Mushroom Observer observation {observation_id}."
        ),
    }
    return result


def _selected_candidate(raw_input: str, sequence_id: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        wanted_id = int(sequence_id)
    except (TypeError, ValueError):
        raise MushroomObserverError("Select an ITS sequence first.", status=422)
    if wanted_id <= 0:
        raise MushroomObserverError("Select an ITS sequence first.", status=422)
    analysis = analyze_observation(raw_input)
    for candidate in analysis["its_sequences"]:
        if candidate["id"] == wanted_id:
            return analysis, candidate
    raise MushroomObserverError(
        "The selected ITS sequence is not attached to this observation.", status=422
    )


def build_queue_sequence(raw_input: str, sequence_id: Any) -> Dict[str, Any]:
    analysis, candidate = _selected_candidate(raw_input, sequence_id)
    observation = analysis["observation"]
    location = _compact_location({"location": {"name": observation.get("location")}})
    name = f"MO{observation['id']}"
    if location:
        name = f"{name} {location}"
    return {
        "name": name,
        "organism": observation.get("consensus_name") or "",
        "sequence": candidate["sequence"],
        "source": "mushroom_observer",
        "hit_source": "mo_observation",
        "location": location,
        "observation_id": str(observation["id"]),
        "mushroom_observer_sequence_id": candidate["id"],
        "locus": candidate["locus"],
        "accession": candidate.get("accession") or "",
    }


def _job_title(observation_id: int, consensus_name: str = "") -> str:
    label = _clean_text(consensus_name, 200) or "Identification pending"
    return f"MO # {int(observation_id)} - {label} → Phylogenetic Tree"


def _mycomap_title(observation_id: int, sequence_id: int, sequence: str) -> str:
    digest = hashlib.sha256(str(sequence or "").encode("ascii", errors="ignore")).hexdigest()[:10]
    return f"MO{int(observation_id)} ITS sequence {int(sequence_id)} {digest}"


def create_tree_job(raw_input: str, sequence_id: Any, *, user=None,
                    rebuild_ncbi_blast: bool = False,
                    mycomap_local_limit=None, mycomap_ncbi_limit=None,
                    public_base_url: Optional[str] = None) -> Dict[str, Any]:
    from app.config import Config
    from app.extensions import db
    from app.models import Job
    from app.services.inaturalist_tree_service import DEFAULT_TREE_PARAMS
    from app.services.mycomap_service import validate_mycomap_rerun_limit
    from app.workers.queue import enqueue_job

    analysis, candidate = _selected_candidate(raw_input, sequence_id)
    observation = analysis["observation"]
    local_limit, local_error = validate_mycomap_rerun_limit(mycomap_local_limit, "local")
    ncbi_limit, ncbi_error = validate_mycomap_rerun_limit(mycomap_ncbi_limit, "ncbi")
    if local_error or ncbi_error:
        raise MushroomObserverError(local_error or ncbi_error, status=422)

    observation_id = int(observation["id"])
    job_id = str(uuid.uuid4())
    title = _job_title(observation_id, observation.get("consensus_name"))
    preparation = {
        "observation_id": observation_id,
        "sequence_id": candidate["id"],
        "sequence": candidate["sequence"],
        "locus": candidate["locus"],
        "consensus_name": observation.get("consensus_name") or "",
        "location": observation.get("location") or "",
        "include_ncbi": True,
        "include_local": True,
        "rebuild_ncbi_blast": bool(rebuild_ncbi_blast),
        "mycomap_blast_url": candidate.get("mycomap_blast_url") or "",
        "mycomap_local_limit": local_limit,
        "mycomap_ncbi_limit": ncbi_limit,
    }
    job_params = {
        "input_type": "mo_tree_preparation",
        "notes": title,
        "trim_terminal_overhangs": DEFAULT_TREE_PARAMS["trim_terminal_overhangs"],
        "_mo_tree_preparation": preparation,
    }
    metrics = {
        "via": "mo_phylogenetic_tree",
        "notes": title,
        "tree_method": DEFAULT_TREE_PARAMS["tree_method"],
        "alignment_method": DEFAULT_TREE_PARAMS["alignment_method"],
        "trimming_method": DEFAULT_TREE_PARAMS["trimming_method"],
        "trim_terminal_overhangs": DEFAULT_TREE_PARAMS["trim_terminal_overhangs"],
        "mo_observation_id": observation_id,
        "mo_sequence_id": candidate["id"],
        "mo_locus": candidate["locus"],
        "mo_source_url": observation["url"],
        "mo_comment_status": "pending",
        "mo_consensus_name": observation.get("consensus_name") or "",
        "mo_location": observation.get("location") or "",
        "mycomap_preparation_status": "queued",
        "mycomap_ncbi_blast_rebuild_requested": bool(rebuild_ncbi_blast),
        "mycomap_blast_reused_from_sequence_notes": bool(
            candidate.get("mycomap_blast_url")
        ),
        "queue_class": "high",
        "source": "mushroom_observer_single_tree",
    }
    base = _clean_text(public_base_url, 500).rstrip("/")
    if base:
        metrics["mo_public_base_url"] = base

    record = Job(
        id=job_id,
        status="queued",
        job_dir=str(Config.JOB_DIR / job_id),
        input_type=job_params["input_type"],
        metrics=metrics,
    )
    if user is not None and getattr(user, "is_authenticated", False):
        record.user_id = user.id
    db.session.add(record)
    db.session.commit()
    try:
        enqueue_job(
            job_params,
            queue_name="phylo_high",
            meta={
                "queue_class": "high",
                "source": "mushroom_observer_single_tree",
                "mo_tree_preparation": "queued",
            },
            job_id=job_id,
        )
    except Exception:
        failed_metrics = dict(record.metrics or {})
        failed_metrics["mycomap_preparation_status"] = "failed"
        failed_metrics["error"] = "Unable to queue Mushroom Observer tree preparation."
        record.metrics = failed_metrics
        record.status = "failed"
        db.session.commit()
        raise
    return {
        "status": "queued",
        "job_id": job_id,
        "observation_id": observation_id,
        "sequence_id": candidate["id"],
        "tree_status_url": f"/job/{job_id}",
        "tree_view_url": f"/job/{job_id}/view",
        "message": (
            "Tree job queued. Dikarya will create or refresh MycoMap BLAST results, "
            "build the tree, and post the finished tree link as a Mushroom Observer comment."
        ),
    }


def _creation_wait_details(created: Dict[str, Any], *, title: str,
                           local_limit: int, ncbi_limit: int) -> Dict[str, Any]:
    pending = bool(created.get("record_pending"))
    return {
        "local_limit": local_limit,
        "ncbi_limit": ncbi_limit,
        "local": created,
        "local_status": "queued",
        "ncbi": created,
        "ncbi_status": "queued",
        "warnings": [],
        "auto_created": True,
        "creation_pending": pending,
        "creation_discovery_attempt": 0,
        "created_title": title,
        "created_blast_id": None if pending else str(created.get("blast_id") or ""),
        "created_mycomap_url": "" if pending else str(created.get("url") or ""),
        "ncbi_poll_attempt": 0,
    }


def _source_name(name: str, observation_id: int) -> str:
    original = _clean_text(name, 500)
    token = f"MO{int(observation_id)}"
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?!\d)", original, re.IGNORECASE):
        return original
    parts = original.split(None, 1)
    if parts and re.match(r"^[A-Z]{1,3}_?\d{5,9}(?:\.\d+)?$", parts[0]):
        return f"{parts[0]} {token} {parts[1] if len(parts) > 1 else ''}".strip()
    return f"{token} {original}".strip()


def _ensure_source_sequence(sequences: List[Dict[str, Any]], preparation: Dict[str, Any]
                            ) -> Tuple[Optional[str], Optional[str]]:
    from app.services.mycomap_service import (
        MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES,
        extract_mycomap_observation_reference,
        mycomap_sequence_difference_count,
    )

    wanted = re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(preparation.get("sequence") or "").upper())
    observation_id = int(preparation["observation_id"])
    exact = []
    for sequence in sequences:
        current = re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(sequence.get("sequence") or "").upper())
        if current == wanted:
            exact.append(sequence)
    if exact:
        source_pattern = re.compile(
            rf"(?:\bMO\s*#?\s*{observation_id}\b|mushroomobserver\.org/(?:obs/)?{observation_id}\b)",
            re.IGNORECASE,
        )
        selected = next(
            (item for item in exact if source_pattern.search(str(item.get("name") or ""))),
            exact[0],
        )
        selected["name"] = _source_name(selected.get("name") or "", observation_id)
        selected["observation_id"] = str(observation_id)
        selected["mushroom_observer_sequence_id"] = int(preparation["sequence_id"])
        selected["locus"] = preparation.get("locus") or "ITS"
        return None, selected["name"]

    near_matches = []
    for index, sequence in enumerate(sequences):
        name = str(sequence.get("name") or "")
        if extract_mycomap_observation_reference(name) != f"mo:{observation_id}":
            continue
        difference_count = mycomap_sequence_difference_count(
            sequence.get("sequence") or "",
            wanted,
            max_distance=MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES,
        )
        if (
            difference_count is not None
            and difference_count <= MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES
        ):
            near_matches.append((
                difference_count,
                -len(str(sequence.get("sequence") or "")),
                index,
                sequence,
            ))
    if near_matches:
        _difference_count, _negative_length, _index, selected = min(near_matches)
        selected["name"] = _source_name(selected.get("name") or "", observation_id)
        selected["observation_id"] = str(observation_id)
        selected["mushroom_observer_sequence_id"] = int(preparation["sequence_id"])
        selected["locus"] = preparation.get("locus") or "ITS"
        return None, selected["name"]

    name = f"MO{observation_id} (Mushroom Observer ITS sequence {preparation['sequence_id']})"
    sequences.append({
        "name": name,
        "organism": preparation.get("consensus_name") or "",
        "sequence": wanted,
        "source": "mushroom_observer",
        "hit_source": "mo_observation",
        "location": preparation.get("location") or "",
        "observation_id": str(observation_id),
        "mushroom_observer_sequence_id": int(preparation["sequence_id"]),
        "locus": preparation.get("locus") or "ITS",
        "identity": None,
        "query_cover": None,
        "subject_cover": None,
        "blast_metrics_available": False,
    })
    return name, None


def prepare_tree_job(preparation: Dict[str, Any], *, defer_after_ncbi_rerun: bool = False,
                     skip_mycomap_refresh: bool = False,
                     mycomap_rerun_details: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    from app.api.routes import gather_mycomap_sequences_for_queue
    from app.services.inaturalist_tree_service import (
        DEFAULT_TREE_PARAMS,
        _build_fasta_text,
        _build_sequence_metadata,
        _check_auto_created_mycomap_ncbi_results,
        _mycomap_creation_discovery_message,
        _refresh_mycomap_blast_results,
    )
    from app.services.mycomap_service import (
        MycoMapCreateError,
        MycoMapRerunError,
        create_mycomap_blast,
        find_mycomap_blast_by_title,
        get_mycomap_creation_discovery_max_attempts,
        get_mycomap_creation_discovery_max_seconds,
        validate_mycomap_url,
        validate_mycomap_rerun_limit,
    )

    observation_id = int(preparation.get("observation_id") or 0)
    sequence_id = int(preparation.get("sequence_id") or 0)
    sequence = re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(preparation.get("sequence") or "").upper())
    if not observation_id or not sequence_id or len(sequence) < 100:
        raise MushroomObserverError("The selected Mushroom Observer ITS sequence is invalid.", status=422)
    local_limit, local_error = validate_mycomap_rerun_limit(
        preparation.get("mycomap_local_limit"), "local"
    )
    ncbi_limit, ncbi_error = validate_mycomap_rerun_limit(
        preparation.get("mycomap_ncbi_limit"), "ncbi"
    )
    if local_error or ncbi_error:
        raise MushroomObserverError(local_error or ncbi_error, status=422)

    title = _mycomap_title(observation_id, sequence_id, sequence)
    details = dict(mycomap_rerun_details or {})
    notes_mycomap_url = str(preparation.get("mycomap_blast_url") or "").strip()
    notes_blast_id = validate_mycomap_url(notes_mycomap_url)
    if not notes_blast_id:
        notes_mycomap_url = ""
    mycomap_url = str(
        details.get("created_mycomap_url") or notes_mycomap_url or ""
    ).strip()

    if skip_mycomap_refresh and details.get("creation_pending"):
        discovery_warnings = list(details.get("creation_discovery_warnings") or [])
        lookup_warnings = []
        found = find_mycomap_blast_by_title(title, warnings=lookup_warnings)
        discovery_warnings.extend(lookup_warnings)
        discovery_warnings = list(dict.fromkeys(discovery_warnings))
        if not found:
            attempt = int(details.get("creation_discovery_attempt") or 0) + 1
            if attempt >= get_mycomap_creation_discovery_max_attempts():
                raise MushroomObserverError(
                    _mycomap_creation_discovery_message(
                        get_mycomap_creation_discovery_max_seconds(),
                        discovery_warnings,
                    ),
                    status=504,
                )
            details["creation_discovery_attempt"] = attempt
            if discovery_warnings:
                details["creation_discovery_warnings"] = discovery_warnings
            return {
                "status": "waiting_for_ncbi",
                "notes": _job_title(observation_id, preparation.get("consensus_name")),
                "mycomap_blast_url": "",
                "mycomap_rerun_details": details,
            }
        details["creation_pending"] = False
        details["created_blast_id"] = found["blast_id"]
        details["created_mycomap_url"] = found["url"]
        mycomap_url = found["url"]

    if not skip_mycomap_refresh:
        discovery_warnings = []
        found = (
            {"blast_id": notes_blast_id, "url": notes_mycomap_url}
            if notes_blast_id
            else find_mycomap_blast_by_title(title, warnings=discovery_warnings)
        )
        if found:
            mycomap_url = found["url"]
            try:
                details = _refresh_mycomap_blast_results(
                    found["blast_id"],
                    rebuild_local_blast=True,
                    rebuild_ncbi_blast=bool(preparation.get("rebuild_ncbi_blast")),
                    mycomap_local_limit=local_limit,
                    mycomap_ncbi_limit=ncbi_limit,
                )
            except MycoMapRerunError as exc:
                raise MushroomObserverError(str(exc), status=502)
            details["auto_created"] = False
            details["reused_from_sequence_notes"] = bool(notes_blast_id)
            details["created_blast_id"] = found["blast_id"]
            details["created_mycomap_url"] = found["url"]
            if preparation.get("rebuild_ncbi_blast") and defer_after_ncbi_rerun:
                return {
                    "status": "waiting_for_ncbi",
                    "notes": _job_title(observation_id, preparation.get("consensus_name")),
                    "mycomap_blast_url": mycomap_url,
                    "mycomap_rerun_details": details,
                }
        else:
            try:
                created = create_mycomap_blast(
                    sequence,
                    title=title,
                    local_limit=local_limit,
                    ncbi_limit=ncbi_limit,
                )
            except MycoMapCreateError as exc:
                raise MushroomObserverError(str(exc), status=502)
            details = _creation_wait_details(
                created, title=title, local_limit=local_limit, ncbi_limit=ncbi_limit
            )
            mycomap_url = details.get("created_mycomap_url") or ""
            return {
                "status": "waiting_for_ncbi",
                "notes": _job_title(observation_id, preparation.get("consensus_name")),
                "mycomap_blast_url": mycomap_url,
                "mycomap_rerun_details": details,
            }

    if details.get("auto_created"):
        blast_id = str(details.get("created_blast_id") or "")
        if not blast_id:
            raise MushroomObserverError("MycoMap BLAST result ID is missing.", status=502)
        ready, details = _check_auto_created_mycomap_ncbi_results(
            blast_id, details, mycomap_url=mycomap_url
        )
        if not ready:
            return {
                "status": "waiting_for_ncbi",
                "notes": _job_title(observation_id, preparation.get("consensus_name")),
                "mycomap_blast_url": mycomap_url,
                "mycomap_rerun_details": details,
            }

    if not mycomap_url:
        mycomap_url = str(details.get("created_mycomap_url") or "")
    include_ncbi = bool(preparation.get("include_ncbi", True))
    if details.get("ncbi_fallback_local_only"):
        include_ncbi = False
    payload, error = gather_mycomap_sequences_for_queue(
        mycomap_url,
        include_ncbi=include_ncbi,
        include_local=bool(preparation.get("include_local", True)),
    )
    if error is not None:
        body, status = error
        raise MushroomObserverError(
            body.get("error", "Failed to fetch MycoMap sequences."),
            # 404 = MycoMap has no such BLAST result. Passing it through keeps the
            # status honest; collapsing it to 502 reads as "our gateway is broken".
            status=status if status in {400, 404, 422, 502} else 502,
        )
    sequences = (payload or {}).get("sequences") or []
    if len(sequences) < 2:
        raise MushroomObserverError(
            "MycoMap returned fewer than 2 usable sequences; cannot build a tree.", status=422
        )
    added, matched = _ensure_source_sequence(sequences, preparation)
    title = _job_title(observation_id, preparation.get("consensus_name"))
    sequence_metadata = _build_sequence_metadata(sequences)
    sequences_by_name = {
        str(sequence_item.get("name") or ""): sequence_item
        for sequence_item in sequences
    }
    for metadata_item in sequence_metadata:
        source_item = sequences_by_name.get(str(metadata_item.get("name") or "")) or {}
        if source_item.get("observation_id"):
            metadata_item["observation_id"] = str(source_item["observation_id"])
        if source_item.get("mushroom_observer_sequence_id"):
            metadata_item["mushroom_observer_sequence_id"] = int(
                source_item["mushroom_observer_sequence_id"]
            )
        if source_item.get("locus"):
            metadata_item["locus"] = _clean_text(source_item["locus"], 100)
    job_params = {
        "input_type": "pasted_sequence",
        "notes": title,
        "sequence": _build_fasta_text(sequences),
        "sequence_metadata": sequence_metadata,
        "accessions": [],
        "alignment_method": DEFAULT_TREE_PARAMS["alignment_method"],
        "trimming_method": DEFAULT_TREE_PARAMS["trimming_method"],
        "trim_terminal_overhangs": DEFAULT_TREE_PARAMS["trim_terminal_overhangs"],
        "alignment_options": {},
        "tree_method": DEFAULT_TREE_PARAMS["tree_method"],
        "tree_model": DEFAULT_TREE_PARAMS["tree_model"],
        "bootstrap": DEFAULT_TREE_PARAMS["bootstrap"],
        "mcmc_generations": DEFAULT_TREE_PARAMS["mcmc_generations"],
        "mcmc_nruns": 2,
        "mcmc_nchains": 4,
        "mcmc_stop_early": DEFAULT_TREE_PARAMS["mcmc_stop_early"],
        "mycomap_blast_url": mycomap_url,
        "import_filter_details": (payload or {}).get("import_filter_details") or {},
    }
    source_display = " ".join(filter(None, [
        f"MO{observation_id}",
        _clean_text(preparation.get("consensus_name"), 200),
        _clean_text(preparation.get("location"), 200),
    ]))
    metrics = {
        "notes": title,
        "mo_source_display_name": source_display,
        "mycomap_blast_url": mycomap_url,
        "mycomap_blast_rerun": details,
        "mycomap_local_blast_rebuilt": details.get("local_status") == "completed",
        "mycomap_ncbi_blast_rebuilt": details.get("ncbi_status") in {"available", "queued"},
        "mycomap_local_blast_limit": details.get("local_limit"),
        "mycomap_ncbi_blast_limit": details.get("ncbi_limit"),
        "mycomap_preparation_status": "completed",
        "mycomap_blast_auto_created": bool(details.get("auto_created")),
        "mycomap_blast_reused_from_sequence_notes": bool(
            details.get("reused_from_sequence_notes")
        ),
    }
    if added:
        metrics["mo_added_its_name"] = added
    if matched:
        metrics["mo_matched_its_tip"] = matched
    warnings = list(details.get("warnings") or [])
    if warnings:
        metrics["mycomap_refresh_warnings"] = warnings
    return {"job_params": job_params, "metrics": metrics, "mycomap_blast_url": mycomap_url}


def highlight_source_observation_tip(job_id: str, observation_id: int,
                                     extra_tip_names: Optional[List[str]] = None,
                                     display_name: Optional[str] = None) -> List[str]:
    """Highlight and label the Mushroom Observer source tip in tree state."""
    try:
        from app.config import Config
        from app.services.inaturalist_tree_service import _iter_tree_tip_names
        from app.services.tree_edit_service import (
            apply_auto_root_default,
            load_tree_state,
            rename_tip,
            save_tree_state,
            tree_state_lock,
        )

        job_dir = Config.JOB_DIR / job_id
        # Everything below is fast local work on the state itself -- no network
        # and no subprocess -- so the whole load/modify/save runs inside the
        # per-job lock. Splitting it would let a viewer edit that lands mid-way
        # be overwritten by this function's stale snapshot.
        source_label = _clean_text(display_name, 500)
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            if not state or not isinstance(state.get("tree_structure"), dict):
                return []
            tips = list(_iter_tree_tip_names(state["tree_structure"]))
            targets = []
            for raw_name in extra_tip_names or []:
                wanted = str(raw_name or "")
                for tip in tips:
                    if tip == wanted or (wanted.split() and tip.split() and tip.split()[0] == wanted.split()[0]):
                        if tip not in targets:
                            targets.append(tip)
                        break
            pattern = re.compile(rf"(?<![A-Za-z0-9])MO\s*#?\s*{int(observation_id)}(?!\d)", re.IGNORECASE)
            for tip in tips:
                if pattern.search(tip) and tip not in targets:
                    targets.append(tip)
            if not targets:
                return []
            if source_label:
                for tip in list(targets):
                    rename_tip(state, tip, _source_name(source_label, observation_id))
            selection_sets = state.get("selection_sets") or {}
            default_members = list(selection_sets.get("Default") or [])
            for target in targets:
                if target not in default_members:
                    default_members.append(target)
            selection_sets["Default"] = default_members
            state["selection_sets"] = selection_sets
            colors = state.get("selection_set_colors") or {}
            colors.setdefault("Default", "#1f77b4")
            state["selection_set_colors"] = colors
            state.setdefault("active_selection_set", "Default")
            if len(targets) == 1:
                state = apply_auto_root_default(job_dir, state, targets[0], source="mo_highlight")
            save_tree_state(job_dir, state)
            return targets
    except Exception as exc:
        logger.warning("Mushroom Observer source highlighting failed for %s: %s", job_id, type(exc).__name__)
        return []


def _public_tree_url(job_id: str, metrics: Dict[str, Any]) -> Optional[str]:
    try:
        from flask import url_for

        value = url_for("main.job_viewer", job_id=job_id, _external=True)
    except Exception:
        value = ""
    if value and not value.startswith("/"):
        return value
    base = str(metrics.get("mo_public_base_url") or "").strip().rstrip("/")
    return f"{base}/job/{job_id}/view" if base else None


def post_completed_tree_comment(job_id: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Post the completed tree URL to Mushroom Observer; never raise."""
    output = {"status": "skipped", "mo_tree_url": None, "mo_comment_id": None, "error": None}
    try:
        observation_id = int(metrics.get("mo_observation_id") or 0)
        if not observation_id:
            raise MushroomObserverError("Missing Mushroom Observer observation ID.")
        tree_url = _public_tree_url(job_id, metrics)
        if not tree_url:
            output["error"] = "missing public base URL"
            return output
        output["mo_tree_url"] = tree_url
        existing = _api_request("comments", params={
            "target": f"observation #{observation_id}",
            "content_has": tree_url,
            "detail": "high",
            "format": "json",
        })
        matches = existing.get("results") or []
        if matches:
            output["status"] = "success"
            output["mo_comment_id"] = matches[0].get("id")
            return output
        api_key = str(current_app.config.get("MUSHROOM_OBSERVER_API_KEY") or "").strip()
        if not api_key:
            raise MushroomObserverError("MUSHROOM_OBSERVER_API_KEY is not configured.", status=503)
        content = f"Dikarya phylogenetic tree for this observation: {tree_url}"
        mycomap_blast_url = str(metrics.get("mycomap_blast_url") or "").strip()
        if mycomap_blast_url:
            content += f"\n\nMycoMap BLAST results: {mycomap_blast_url}"
        posted = _api_request("comments", method="POST", body={
            "api_key": api_key,
            "target": f"observation #{observation_id}",
            "summary": MO_COMMENT_SUMMARY,
            "content": content,
        })
        result = (posted.get("results") or [{}])[0]
        output["status"] = "success"
        output["mo_comment_id"] = result.get("id") or posted.get("id")
        return output
    except MushroomObserverError as exc:
        output["status"] = "failed"
        output["error"] = str(exc)[:300]
        logger.warning("Mushroom Observer comment failed for %s: %s", job_id, str(exc))
        return output
    except Exception as exc:
        output["status"] = "failed"
        output["error"] = type(exc).__name__
        logger.warning("Unexpected Mushroom Observer comment failure for %s: %s", job_id, type(exc).__name__)
        return output
