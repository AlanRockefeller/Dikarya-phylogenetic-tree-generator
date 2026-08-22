from flask import jsonify, request, send_file, url_for
from flask_login import current_user
from app.api import bp
from app.workers.queue import (
    active_recompute_snapshot_mtime,
    enqueue_job,
    enqueue_recompute_job,
    get_job_status,
)
from app.config import Config
from app.extensions import db
from app.models import Job
import logging
import re
import hashlib
import time
from urllib.parse import urlsplit
from difflib import SequenceMatcher
from datetime import datetime

from app.services.security_utils import validate_job_id, validate_safe_file_path, coerce_bool
from app.services.artifact_storage import (
    artifact_exists,
    open_artifact,
    read_artifact_bytes,
    resolve_artifact,
)
from app.services.its_extraction_service import (
    normalize_region as normalize_its_region,
    resolve_min_length as resolve_its_min_length,
)
from app.services.access_control import check_job_access
from app.extensions import csrf, limiter

logger = logging.getLogger(__name__)


CLIENT_LOG_MAX_STR = 120


def _server_error(exc, *, where=""):
    """Return a generic 500 JSON response without leaking internals.

    The full exception (including traceback) is logged server-side under a
    short request_id, which is echoed back to the client so support requests
    can be correlated to the log without exposing file paths, library
    versions, or message text from the underlying error.
    """
    from flask import g
    request_id = g.request_id
    logger.exception(
        "event=http.unhandled_error where=%s exception=%s",
        where or "unknown", type(exc).__name__,
    )
    return jsonify({
        "status": "error",
        "error": "Internal server error",
        "request_id": request_id,
    }), 500


def _client_log_value(value, max_length=CLIENT_LOG_MAX_STR):
    """Return a short, printable, credential-free string for client diagnostics.

    Alan 8/15/26 - This used to only strip newlines and truncate. Every caller
    passes browser-supplied text, so it now defers to the shared telemetry
    sanitizer (query strings, credentials and nucleotide runs removed).
    """
    from app.services.log_context import sanitize_telemetry_text

    return sanitize_telemetry_text(value, max_length)


@bp.route('/client-log', methods=['POST'])
@csrf.exempt
@limiter.limit("30 per minute; 300 per hour")
def client_log():
    """Record sanitized client-side diagnostics that never include raw form data."""
    data = request.get_json(silent=True) or {}
    event = _client_log_value(data.get("event"), 60)
    if event != "mycomap_url_rejected":
        return jsonify({"status": "ignored"})

    url_length = data.get("url_length")
    try:
        url_length = int(url_length)
    except (TypeError, ValueError):
        url_length = None
    if url_length is not None:
        url_length = max(0, min(url_length, 10000))

    logger.warning(
        "client_event=%s reason=%s url_length=%s hostname=%s path_prefix=%s "
        "has_r_id=%s starts_with_required_prefix=%s user_agent=%s referrer_path=%s",
        event,
        _client_log_value(data.get("reason"), 80),
        url_length,
        _client_log_value(data.get("hostname"), 120),
        _client_log_value(data.get("path_prefix"), 120),
        bool(data.get("has_r_id")),
        bool(data.get("starts_with_required_prefix")),
        _client_log_value(request.headers.get("User-Agent"), 200),
        urlsplit(request.headers.get("Referer") or "").path[:200],
    )
    return jsonify({"status": "ok"})


# =============================================================================
# BLAST API Endpoint
# =============================================================================

# INSDC nucleotide accessions come in a small number of fixed shapes, and the
# previous catch-all (1-6 letters + 5-9 digits) was loose enough to accept
# things that are not accessions at all: an iNaturalist observation id pasted
# into the accession box ("INAT125467754", 4 letters + 9 digits) matched, was
# sent to NCBI, and came back as an opaque 400 that took the rest of its batch
# down with it. Matching the real shapes rejects it here, by name, instead.
#
#   1 letter  + 5 digits            e.g. U49845
#   2 letters + 6 digits            e.g. OR807397, AF123456
#   2 letters + 8 digits            e.g. KY12345678
#   RefSeq: 2 letters + '_' + 6, 8 or 9 digits   e.g. NC_012345, NM_001234567
#   WGS: 4 letters + 2-digit assembly version + 6 or 8 contig digits, so
#        exactly 4+8 or 4+10 e.g. AAAA01000001. Notably never 4+9, which is
#        what keeps the observed iNaturalist id from matching this arm.
#   WGS (6-letter prefix): 6 letters + 2-digit assembly version + 7 or 9
#        contig digits e.g. AAAAAA010000001. No iNaturalist id has ever had
#        six leading letters, so this arm costs nothing to allow -- and
#        without it a perfectly ordinary INSDC accession was rejected as "not
#        an accession" before any NCBI call.
#
# A 4+8 id is genuinely ambiguous -- "INAT12546775" is shape-identical to a
# real WGS accession, and no pattern can separate them. That case still reaches
# NCBI, which is why _fetch_genbank_xml_batch also isolates a failing accession
# rather than letting it void its whole batch.
_GENBANK_ACCESSION_RE = re.compile(
    r'^(?:'
    r'[A-Z]\d{5}'
    r'|[A-Z]{2}\d{6}'
    r'|[A-Z]{2}\d{8}'
    r'|[A-Z]{2}_\d{6}'
    r'|[A-Z]{2}_\d{8,9}'
    r'|[A-Z]{4}\d{8}'
    r'|[A-Z]{4}\d{10}'
    r'|[A-Z]{6}\d{9}'
    r'|[A-Z]{6}\d{11}'
    r')(?:\.\d+)?$',
    re.IGNORECASE,
)


def _is_genbank_accession(text):
    """Check if text looks like a GenBank accession number."""
    return bool(_GENBANK_ACCESSION_RE.match((text or "").strip()))


MAX_CUSTOM_GENBANK_ACCESSIONS = 200
MAX_CUSTOM_GENBANK_SEQUENCE_BP = 5000
MAX_SEQUENCE_METADATA_ITEMS = 5000

VALID_TREE_METHODS = {"nj", "raxml", "iqtree", "mrbayes", "fasttree"}

# Settings the tree viewer's Advanced panel may change on a recompute. Anything
# outside this set (sequences, import provenance, trimming report, job
# ownership) is read from the stored job and is not request-controlled.
RECOMPUTE_OVERRIDABLE_FIELDS = frozenset({
    "alignment_method", "trimming_method", "trim_terminal_overhangs",
    "tree_method", "tree_model",
    "bootstrap", "alrt_replicates",
    "mcmc_generations", "mcmc_nruns", "mcmc_nchains", "mcmc_burnin_fraction",
    "run_preset", "bootstrap_preset", "bootstrap_cap", "enable_bootstrap",
    "start_tree_override", "moose_enabled", "early_stopping", "seed",
    "outgroup", "notes",
})

# Settings the viewer's Advanced panel reads back so it can open pre-filled with
# what the job actually ran. Same list, minus free-text notes.
RECOMPUTE_READABLE_FIELDS = RECOMPUTE_OVERRIDABLE_FIELDS - {"notes"}

US_STATE_TO_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}
US_STATE_ABBRS = set(US_STATE_TO_ABBR.values())


def _optional_float(value):
    """Return a finite float for JSON numeric fields, or None for empty/invalid values."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _normalize_sequence_metadata(raw_items):
    """Keep only bounded, frontend-facing per-sequence metric metadata."""
    if not isinstance(raw_items, list):
        return []

    normalized = []
    for raw in raw_items[:MAX_SEQUENCE_METADATA_ITEMS]:
        if not isinstance(raw, dict):
            continue

        name = str(raw.get("name") or "")[:500]
        fasta_header = str(raw.get("fasta_header") or name)[:500]
        if not name and not fasta_header:
            continue

        row = {
            "name": name,
            "fasta_header": fasta_header,
            "organism": str(raw.get("organism") or "")[:300],
            "source": str(raw.get("source") or "")[:50],
            "hit_source": str(raw.get("hit_source") or "")[:50],
            "location": str(raw.get("location") or raw.get("mycomap_location") or "")[:200],
            "raw_fasta_header": str(raw.get("raw_fasta_header") or "")[:2000],
            "raw_ncbi_description": str(raw.get("raw_ncbi_description") or "")[:2000],
            "mycomap_header_format": str(raw.get("mycomap_header_format") or "")[:50],
            "internal_id": str(raw.get("internal_id") or "")[:150],
            "display_label": str(raw.get("display_label") or "")[:500],
            "accession": str(raw.get("accession") or "")[:100],
            "taxon": str(raw.get("taxon") or "")[:300],
            "raw_mycomap_taxon": str(raw.get("raw_mycomap_taxon") or "")[:2000],
            "voucher": str(raw.get("voucher") or "")[:300],
            "observation_id": str(raw.get("observation_id") or "")[:50],
            "locus": str(raw.get("locus") or "")[:100],
            "identity": _optional_float(raw.get("identity")),
            "query_cover": _optional_float(raw.get("query_cover")),
            "subject_cover": _optional_float(raw.get("subject_cover")),
        }
        try:
            occurrence = int(raw.get("occurrence") or 0)
        except (TypeError, ValueError):
            occurrence = 0
        if occurrence > 0:
            row["occurrence"] = occurrence
        try:
            mo_sequence_id = int(raw.get("mushroom_observer_sequence_id") or 0)
        except (TypeError, ValueError):
            mo_sequence_id = 0
        if mo_sequence_id > 0:
            row["mushroom_observer_sequence_id"] = mo_sequence_id
        row["blast_metrics_available"] = bool(raw.get("blast_metrics_available")) or any(
            row[field] is not None
            for field in ("identity", "query_cover", "subject_cover")
        )
        normalized.append(row)

    return normalized


def _normalize_dedup_location(value, preserve_locality=False):
    """Normalize known geographic labels enough to compare same-location sequences."""
    text = re.sub(r'\s+', ' ', str(value or '').strip())
    if not text:
        return ""

    tokens = [token.strip(" ,.;:()[]{}") for token in text.split()]
    tokens = [token for token in tokens if token]
    if not tokens:
        return ""

    lower = [token.lower() for token in tokens]
    country = ""
    if lower[-1] in {"us", "usa", "u.s.", "u.s.a.", "unitedstates", "united", "states"}:
        country = "us"

    if country == "us":
        location_tokens = lower[:-1]
        zip_code = ""
        if location_tokens and re.fullmatch(r'\d{5}(?:-\d{4})?', location_tokens[-1]):
            zip_code = location_tokens.pop()
        for size in (2, 1):
            if len(location_tokens) >= size:
                candidate = " ".join(location_tokens[-size:])
                state = US_STATE_TO_ABBR.get(candidate)
                if not state and size == 1 and candidate in US_STATE_ABBRS:
                    state = candidate
                if state:
                    locality = ""
                    if preserve_locality:
                        locality = re.sub(r'[^a-z0-9]+', ' ', " ".join(location_tokens[:-size])).strip()
                    return "|".join(part for part in ("us", state, locality, zip_code) if part)

    normalized = re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()
    return normalized


def _location_from_sequence_label(label):
    """Extract trailing location fragments from the labels Dikarya renders in tree tips."""
    text = re.sub(r'\s+', ' ', str(label or '').strip())
    if not text:
        return ""

    us_match = re.search(
        r'\b((?:[A-Za-z]+(?:\s+[A-Za-z]+)?|[A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s+(?:US|USA))\b\s*$',
        text,
        flags=re.IGNORECASE,
    )
    if us_match:
        return us_match.group(1)

    country_match = re.search(r'\b([A-Za-z][A-Za-z\s.-]{1,80}\s+[A-Z]{2,3})\b\s*$', text)
    return country_match.group(1) if country_match else ""


def _sequence_location_dedup_key(seq, metadata=None):
    """Return a dedup key for same sequence + same known location, else exact record key."""
    sequence = ''.join(str(seq.get('sequence') or '').split()).upper()
    if not sequence:
        return None

    header = str(seq.get('name') or '').strip()
    metadata = metadata or {}
    location = metadata.get("location") or _location_from_sequence_label(header)
    normalized_location = _normalize_dedup_location(location, preserve_locality=bool(metadata.get("location")))
    if normalized_location:
        return ("sequence_location", sequence, normalized_location)
    return ("exact_record", header, sequence)


def _dedupe_sequence_payload(sequence_text, sequence_metadata):
    """Remove duplicate FASTA records when both cleaned sequence and known location match."""
    sequences = _parse_fasta_sequences(sequence_text)
    if not sequences:
        return sequence_text, sequence_metadata

    metadata_by_header = {}
    for item in sequence_metadata:
        for key in (item.get("fasta_header"), item.get("name")):
            if key:
                metadata_by_header.setdefault(str(key).strip(), item)

    positional_metadata = (
        len(sequence_metadata) == len(sequences)
        and all(
            str(
                sequence_metadata[index].get("fasta_header")
                or sequence_metadata[index].get("name")
                or ""
            ).strip() == str(seq.get("name") or "").strip()
            for index, seq in enumerate(sequences)
        )
    )
    seen = set()
    deduped = []
    deduped_metadata = []
    deduped_headers = set()
    for index, seq in enumerate(sequences):
        header = str(seq.get('name') or '').strip()
        metadata = (
            sequence_metadata[index]
            if positional_metadata else metadata_by_header.get(header, {})
        )
        key = _sequence_location_dedup_key(seq, metadata)
        preserve_mycomap_ncbi_hit = (
            str(metadata.get("source") or "").casefold() == "mycomap"
            and str(metadata.get("hit_source") or "").casefold() == "ncbi"
        )
        if preserve_mycomap_ncbi_hit:
            if key:
                seen.add(key)
            deduped.append(seq)
            if positional_metadata:
                deduped_metadata.append(metadata)
            deduped_headers.add(header)
            continue
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(seq)
        if positional_metadata:
            deduped_metadata.append(metadata)
        deduped_headers.add(str(seq.get("name") or "").strip())

    metadata = deduped_metadata if positional_metadata else [
        item for item in sequence_metadata
        if str(item.get("fasta_header") or item.get("name") or "").strip() in deduped_headers
    ]

    # Retained records can deliberately share a full FASTA header and sequence
    # when their positional metadata identifies different locations. Give only
    # those later records a unique internal ID before the worker's exact-record
    # cleanup, while leaving their display metadata unchanged.
    reserved_ids = {
        str(seq.get("name") or "").strip().split(None, 1)[0]
        for seq in deduped if str(seq.get("name") or "").strip()
    }
    generated_ids = set()
    retained_exact_counts = {}
    headers_changed = False
    for index, seq in enumerate(deduped):
        header = str(seq.get("name") or "").strip()
        sequence = ''.join(str(seq.get("sequence") or "").split()).upper()
        exact_key = (header, sequence)
        occurrence = retained_exact_counts.get(exact_key, 0) + 1
        retained_exact_counts[exact_key] = occurrence
        if occurrence == 1:
            continue

        parts = header.split(None, 1)
        base_id = parts[0] if parts else "seq"
        description = parts[1] if len(parts) > 1 else ""
        suffix = occurrence
        internal_id = f"{base_id}_{suffix}"
        while internal_id in reserved_ids or internal_id in generated_ids:
            suffix += 1
            internal_id = f"{base_id}_{suffix}"
        generated_ids.add(internal_id)
        new_header = f"{internal_id} {description}".rstrip()
        updated_seq = dict(seq)
        updated_seq["name"] = new_header
        deduped[index] = updated_seq
        if positional_metadata and index < len(metadata):
            updated_metadata = dict(metadata[index])
            updated_metadata["fasta_header"] = new_header
            metadata[index] = updated_metadata
        elif not positional_metadata:
            # Header-keyed metadata: the original header still belongs to the
            # first occurrence, so add a copy under the new internal ID rather
            # than rewriting it. Without this the renamed record drops out of
            # the tree viewer's header -> metric map and silently loses its
            # identity / query_cover / subject_cover.
            source_metadata = next(
                (
                    item for item in metadata
                    if str(item.get("fasta_header") or item.get("name") or "").strip() == header
                ),
                None,
            )
            if source_metadata is not None:
                copied = dict(source_metadata)
                if not str(source_metadata.get("fasta_header") or "").strip():
                    copied["name"] = new_header
                copied["fasta_header"] = new_header
                metadata.append(copied)
        headers_changed = True

    if len(deduped) == len(sequences) and not headers_changed:
        return sequence_text, sequence_metadata

    fasta = ''.join(
        f">{seq.get('name', '').strip()}\n{''.join(str(seq.get('sequence') or '').split())}\n"
        for seq in deduped
    ).strip()
    return fasta, metadata


MYCOMAP_LOCAL_FASTA_QUERY_SIMILARITY_MIN = 99.5
MYCOMAP_LOCAL_FASTA_REPORTED_IDENTITY_CONFLICT_MAX = 98.5
MAX_IMPORT_FILTER_DETAIL_RECORDS = 100


def _sequence_similarity_percent(a, b):
    """Return approximate percent similarity for short barcode sequences."""
    seq_a = ''.join(str(a or '').split()).upper()
    seq_b = ''.join(str(b or '').split()).upper()
    if not seq_a or not seq_b:
        return 0.0

    shorter = seq_a if len(seq_a) <= len(seq_b) else seq_b
    longer = seq_b if len(seq_a) <= len(seq_b) else seq_a
    if len(shorter) >= 100 and shorter in longer:
        return 100.0

    return SequenceMatcher(None, seq_a, seq_b, autojunk=False).ratio() * 100


def _extract_mycomap_query_tokens(url):
    """Return likely query identifiers embedded in a MycoMap BLAST URL."""
    tokens = set()
    text = str(url or '')
    for match in re.finditer(r'\binat(?:uralist)?[\s_-]*(\d{5,12})\b', text, flags=re.IGNORECASE):
        digits = match.group(1)
        tokens.add(f"inat{digits}")
    return tokens


def _mycomap_metric_for_sequence(seq, metrics_by_key):
    """Return the MycoMap BLAST table metric associated with one FASTA record."""
    from app.services.mycomap_service import build_blast_metric_keys

    for lookup_name in (seq.get('name', ''), seq.get('_mycomap_original_name', '')):
        for key in build_blast_metric_keys(lookup_name):
            metric = metrics_by_key.get(key)
            if metric:
                return metric
    return None


def _mycomap_sequence_location(seq, metric=None):
    """Return the best available location for a MycoMap FASTA record."""
    return str(
        (metric or {}).get('mycomap_location')
        or seq.get('location')
        or _location_from_sequence_label(
            seq.get('name') or seq.get('_mycomap_original_name') or ''
        )
        or ''
    )


def _mycomap_location_region_key(value):
    """Return a conservative region key suitable for a location exception."""
    text = re.sub(r'\s+', ' ', str(value or '').strip())
    if not text:
        return ''

    us_match = re.search(
        r'(?:US|USA|U\.S\.?A?\.?|United States(?: of America)?)\s*$',
        text,
        flags=re.IGNORECASE,
    )
    if us_match:
        normalized = _normalize_dedup_location(
            f"{text[:us_match.start()].strip()} US".strip(),
            preserve_locality=False,
        )
        return normalized if normalized.startswith('us|') else 'us'

    country_match = re.search(r'(?:^|\s)([A-Z]{2,3})[.,;]?\s*$', text)
    return country_match.group(1).lower() if country_match else ''


def _mycomap_locations_are_distinct(first, second):
    """Return true only when both locations resolve to different regions."""
    normalized_first = _mycomap_location_region_key(first)
    normalized_second = _mycomap_location_region_key(second)
    return bool(normalized_first and normalized_second and normalized_first != normalized_second)


def _mycomap_label_has_query_token(label, query_tokens):
    """Match canonical iNaturalist query IDs without bare-number substring matches."""
    from app.services.mycomap_service import build_blast_metric_keys

    tokens = {str(token or '').casefold() for token in query_tokens}
    label_keys = {
        str(key or '').casefold()
        for key in build_blast_metric_keys(label)
    }
    return bool(tokens.intersection(label_keys))


def _mycomap_query_sequences(sequences, metrics_by_key, query_tokens):
    """Find query records included in MycoMap's local FASTA export."""
    if not query_tokens:
        return []

    candidates = []
    for seq in sequences:
        if seq.get('hit_source') != 'local':
            continue

        metric = _mycomap_metric_for_sequence(seq, metrics_by_key)
        if metric:
            continue

        name = seq.get('name') or seq.get('_mycomap_original_name') or ''
        if not _mycomap_label_has_query_token(name, query_tokens):
            continue

        sequence = ''.join(str(seq.get('sequence') or '').split()).upper()
        if len(sequence) >= 100:
            candidates.append({
                'sequence': sequence,
                'location': _mycomap_sequence_location(seq, metric),
            })

    return candidates


def _mycomap_local_fasta_metric_conflict_detail(seq, metric, query_sequences, query_tokens,
                                                allow_identical_sequences_different_locations):
    """Detect a local FASTA record that contradicts the MycoMap BLAST table."""
    if seq.get('hit_source') != 'local' or not query_sequences:
        return None

    name = seq.get('name') or seq.get('_mycomap_original_name') or ''
    if query_tokens and _mycomap_label_has_query_token(name, query_tokens):
        return None

    sequence = ''.join(str(seq.get('sequence') or '').split()).upper()
    if len(sequence) < 100:
        return None

    best_similarity = max(
        _sequence_similarity_percent(sequence, candidate['sequence'])
        for candidate in query_sequences
    )
    if best_similarity < MYCOMAP_LOCAL_FASTA_QUERY_SIMILARITY_MIN:
        return None

    exact_query_sequences = [
        candidate for candidate in query_sequences
        if sequence == candidate['sequence']
    ]
    if (
        allow_identical_sequences_different_locations
        and exact_query_sequences
        and all(
            _mycomap_locations_are_distinct(
                _mycomap_sequence_location(seq, metric), candidate.get('location')
            )
            for candidate in exact_query_sequences
        )
    ):
        return None

    if not metric:
        return {
            'reason': 'local_fasta_matches_query',
            'reason_label': 'Local FASTA sequence matches query, but the label is not the query',
            'query_similarity': round(best_similarity, 2),
        } if query_tokens else None

    reported_identity = metric.get('identity')
    if reported_identity is None:
        return None
    try:
        reported_identity = float(reported_identity)
    except (TypeError, ValueError):
        return None
    if reported_identity > MYCOMAP_LOCAL_FASTA_REPORTED_IDENTITY_CONFLICT_MAX:
        return None

    return {
        'reason': 'local_fasta_identity_conflict',
        'reason_label': 'Local FASTA sequence matches query, but BLAST table reports lower identity',
        'query_similarity': round(best_similarity, 2),
        'reported_identity': reported_identity,
    }


def _mycomap_local_fasta_group_conflict_detail(seq, conflict_sequences, query_tokens,
                                               allow_identical_sequences_different_locations):
    """Detect a local record that shares a sequence with a conflicting local record."""
    if seq.get('hit_source') != 'local' or not conflict_sequences:
        return None

    name = seq.get('name') or seq.get('_mycomap_original_name') or ''
    if query_tokens and _mycomap_label_has_query_token(name, query_tokens):
        return None

    sequence = ''.join(str(seq.get('sequence') or '').split()).upper()
    if len(sequence) < 100:
        return None

    best_similarity = max(
        _sequence_similarity_percent(sequence, candidate['sequence'])
        for candidate in conflict_sequences
    )
    if best_similarity < MYCOMAP_LOCAL_FASTA_QUERY_SIMILARITY_MIN:
        return None

    exact_conflicting_sequences = [
        candidate for candidate in conflict_sequences
        if sequence == candidate['sequence']
    ]
    if (
        allow_identical_sequences_different_locations
        and exact_conflicting_sequences
        and all(
            _mycomap_locations_are_distinct(
                _mycomap_sequence_location(seq), candidate.get('location')
            )
            for candidate in exact_conflicting_sequences
        )
    ):
        return None

    return {
        'reason': 'local_fasta_matches_conflicting_record',
        'reason_label': 'Local FASTA sequence matches another local record with a query/identity conflict',
        'query_similarity': round(best_similarity, 2),
    }


def _append_import_filter_detail(details, *, name, source, reason, reason_label,
                                 hit_source="", reported_identity=None,
                                 query_similarity=None):
    """Append a bounded, sequence-free import filter detail row."""
    if len(details) >= MAX_IMPORT_FILTER_DETAIL_RECORDS:
        return
    row = {
        "name": str(name or "")[:500],
        "source": str(source or "")[:50],
        "hit_source": str(hit_source or "")[:50],
        "reason": str(reason or "")[:80],
        "reason_label": str(reason_label or "")[:200],
    }
    if reported_identity is not None:
        row["reported_identity"] = reported_identity
    if query_similarity is not None:
        row["query_similarity"] = query_similarity
    details.append(row)


def _mycomap_observation_reference(seq):
    """Return one unambiguous iNaturalist or Mushroom Observer reference."""
    from app.services.mycomap_service import extract_mycomap_observation_reference

    observation_references = set()
    hit_source = str(seq.get('hit_source') or '').casefold()
    for label in (seq.get('name', ''), seq.get('_mycomap_original_name', '')):
        reference = extract_mycomap_observation_reference(label)
        if (
            reference
            and reference.startswith('mo:')
            and hit_source != 'local'
            and not re.search(
                r'mushroom\s*observer|mushroomobserver\.org|\bmo\s*(?:#|:)',
                str(label or ''),
                flags=re.IGNORECASE,
            )
        ):
            # A compact MO123456 token can also be a GenBank accession. Only
            # treat that shape as Mushroom Observer provenance for local hits.
            reference = None
        if reference:
            observation_references.add(reference)
    return (
        next(iter(observation_references))
        if len(observation_references) == 1 else ''
    )


def _mycomap_ric_score(seq):
    """Return the highest RiC score encoded in a MycoMap record label."""
    scores = []
    for label in (seq.get('name', ''), seq.get('_mycomap_original_name', '')):
        for match in re.finditer(
            r'\bRiC\b\s*(?:[:=]\s*)?(\d+(?:\.\d+)?)',
            str(label or ''),
            flags=re.IGNORECASE,
        ):
            scores.append(float(match.group(1)))
    return max(scores) if scores else None


def _mycomap_observation_record_score(item, observation_reference):
    """Prefer longer, less ambiguous, higher-quality records within one observation."""
    index, seq = item
    name = str(seq.get('name') or '').strip()
    first_token = name.split()[0] if name.split() else ''
    sequence = ''.join(str(seq.get('sequence') or '').split()).upper().replace('U', 'T')
    ambiguous_bases = sum(1 for base in sequence if base not in {'A', 'C', 'G', 'T'})
    ric_score = _mycomap_ric_score(seq)
    source, observation_id = observation_reference.split(':', 1)
    canonical_identifier = f"iNat{observation_id}" if source == 'inat' else f"MO{observation_id}"
    canonical_observation = first_token.casefold() == canonical_identifier.casefold()
    genbank_accession = _is_genbank_accession(first_token.split('.')[0])
    has_metrics = bool(seq.get('blast_metrics_available'))
    return (
        -len(sequence),
        ambiguous_bases,
        0 if ric_score is not None else 1,
        -(ric_score or 0),
        0 if canonical_observation else 1,
        0 if genbank_accession else 1,
        0 if has_metrics else 1,
        index,
    )


def _merge_mycomap_duplicate_metadata(keep, duplicate):
    """Retain useful optional metadata when collapsing a duplicate source record."""
    for field in ('organism', 'location', 'identity', 'query_cover', 'subject_cover'):
        if keep.get(field) in (None, '') and duplicate.get(field) not in (None, ''):
            keep[field] = duplicate[field]
    keep['blast_metrics_available'] = bool(keep.get('blast_metrics_available')) or bool(
        duplicate.get('blast_metrics_available')
    )


def _dedupe_mycomap_observation_records(sequences, filtered_records):
    """Collapse near-identical records within one source observation."""
    from app.services.mycomap_service import (
        MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES,
        mycomap_sequence_difference_count,
    )

    groups = {}
    for index, seq in enumerate(sequences):
        observation_reference = _mycomap_observation_reference(seq)
        if observation_reference:
            groups.setdefault(observation_reference, []).append((index, seq))

    keep_indexes = set(range(len(sequences)))
    dropped_count = 0
    for observation_reference, records in groups.items():
        if len(records) < 2:
            continue
        ranked_records = sorted(
            records,
            key=lambda item: _mycomap_observation_record_score(
                item, observation_reference
            ),
        )
        retained_records = []
        source, observation_id = observation_reference.split(':', 1)
        source_label = 'iNaturalist' if source == 'inat' else 'Mushroom Observer'
        for record_index, record in ranked_records:
            matching_retained = []
            for retained_index, retained in retained_records:
                difference_count = mycomap_sequence_difference_count(
                    record.get('sequence', ''),
                    retained.get('sequence', ''),
                    max_distance=MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES,
                )
                if (
                    difference_count is not None
                    and difference_count <= MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES
                ):
                    matching_retained.append((difference_count, retained_index, retained))
            if not matching_retained:
                retained_records.append((record_index, record))
                continue

            difference_count, _retained_index, keep = min(
                matching_retained,
                key=lambda item: item[0],
            )
            keep_indexes.discard(record_index)
            dropped_count += 1
            _merge_mycomap_duplicate_metadata(keep, record)
            _append_import_filter_detail(
                filtered_records,
                name=record.get('_mycomap_original_name') or record.get('name', ''),
                source='mycomap',
                hit_source=record.get('hit_source', ''),
                reason='near_duplicate_observation',
                reason_label=(
                    f'Near-duplicate record for {source_label} observation '
                    f'{observation_id}; {difference_count} non-ambiguous base '
                    f'difference{"s" if difference_count != 1 else ""}'
                ),
            )

    return [seq for index, seq in enumerate(sequences) if index in keep_indexes], dropped_count


def _normalize_import_filter_details(raw):
    """Keep compact, frontend-facing import filter diagnostics."""
    if not isinstance(raw, dict):
        return {}

    def _count(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    normalized = {}
    mycomap = raw.get("mycomap")
    if isinstance(mycomap, dict):
        records = []
        for item in (mycomap.get("filtered_records") or [])[:MAX_IMPORT_FILTER_DETAIL_RECORDS]:
            if not isinstance(item, dict):
                continue
            records.append({
                "name": str(item.get("name") or "")[:500],
                "source": str(item.get("source") or "")[:50],
                "hit_source": str(item.get("hit_source") or "")[:50],
                "reason": str(item.get("reason") or "")[:80],
                "reason_label": str(item.get("reason_label") or "")[:200],
                "reported_identity": _optional_float(item.get("reported_identity")),
                "query_similarity": _optional_float(item.get("query_similarity")),
            })
        counts = mycomap.get("counts") if isinstance(mycomap.get("counts"), dict) else {}
        normalized["mycomap"] = {
            "label": str(mycomap.get("label") or "MycoMap import filters")[:100],
            "filtered_records": records,
            "counts": {
                "invalid_sequence": _count(counts.get("invalid_sequence")),
                "contaminant": _count(counts.get("contaminant")),
                "conflicting_local": _count(counts.get("conflicting_local")),
                "duplicate_observation": _count(counts.get("duplicate_observation")),
            },
        }

    return normalized


def _parse_genbank_accession_tokens(value):
    """Parse comma/whitespace-separated GenBank accessions."""
    if isinstance(value, list):
        raw_text = " ".join(str(item) for item in value)
    else:
        raw_text = str(value or "")

    tokens = [token.strip() for token in re.split(r'[\s,;]+', raw_text) if token.strip()]
    accessions = []
    invalid = []
    seen = set()

    for token in tokens:
        normalized = token.upper()
        if not _is_genbank_accession(normalized):
            invalid.append(token)
            continue
        if normalized not in seen:
            accessions.append(normalized)
            seen.add(normalized)

    return accessions, invalid


def _split_fasta_header(header):
    """Split FASTA header into ID (first token) and description."""
    header = (header or "").strip()
    if not header:
        return "", ""
    parts = header.split(None, 1)
    seq_id = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return seq_id, rest

def _make_unique_id(base_id, used_ids):
    """Make ID unique by appending suffix if needed."""
    if base_id not in used_ids:
        used_ids.add(base_id)
        return base_id
    # Start with simple _added suffix first if not present?
    # User logic: _added{i} starting at 2?
    # Or just _added, then _added2?
    # Let's follow user example: {base_id}_added{i}
    
    # Check simple _added first?
    candidate = f"{base_id}_added"
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
        
    i = 2
    MAX_SUFFIX = 10000  # Prevent infinite loops
    while f"{base_id}_added{i}" in used_ids:
        if i > MAX_SUFFIX:
            # Fallback: use timestamp or random suffix
            import time
            new_id = f"{base_id}_added_{int(time.time()*1000) % 1000000}"
            used_ids.add(new_id)
            return new_id
        i += 1
    new_id = f"{base_id}_added{i}"
    used_ids.add(new_id)
    return new_id

def _parse_fasta_sequences(text):
    """Parse FASTA text into list of {name, sequence} dicts.
    
    Preserves the full header (after >) for display in tree tips.
    """
    sequences = []
    current_name = None
    current_seq = []
    
    for line in text.strip().split('\n'):
        line = line.strip()
        if line.startswith('>'):
            if current_name is not None:
                sequences.append({
                    'name': current_name,
                    'sequence': ''.join(current_seq)
                })
            # Keep the full header (everything after >) for tree tip labels
            current_name = line[1:].strip()
            current_seq = []
        elif line and current_name is not None:
            current_seq.append(line)
    
    # Don't forget the last sequence
    if current_name is not None:
        sequences.append({
            'name': current_name,
            'sequence': ''.join(current_seq)
        })
    
    return sequences


def _sequence_exact_key(seq):
    header = (seq.get('name') or '').strip()
    sequence = ''.join(str(seq.get('sequence') or '').split())
    return header, sequence


def _format_fasta_record_for_job(seq, used_ids, fallback_index):
    # Sanitize header: remove control chars including \0
    raw_header = seq.get('name', '')
    sanitized_header = "".join(ch for ch in raw_header if ord(ch) >= 32 or ch == '\t')

    # Split header to dedupe by ID properly
    seq_id, rest = _split_fasta_header(sanitized_header)

    # Cap header lengths to prevent abuse (e.g., 200KB pasted headers)
    MAX_SEQ_ID_LEN = 100
    MAX_DESC_LEN = 300
    seq_id = seq_id[:MAX_SEQ_ID_LEN]
    rest = rest[:MAX_DESC_LEN]

    if not seq_id:
        seq_id = f"Sequence_{fallback_index}"

    new_id = _make_unique_id(seq_id, used_ids)
    new_header = f"{new_id} {rest}".strip()
    sequence = ''.join(str(seq.get('sequence') or '').split())
    return f">{new_header}\n{sequence}\n"


def _fetch_genbank_sequences_for_queue(accessions, max_sequence_bp=MAX_CUSTOM_GENBANK_SEQUENCE_BP):
    """Fetch GenBank accessions and shape them for frontend queue entries."""
    from app.services.blast_service import fetch_fasta_for_accessions
    from app.services.fasta_utils import clean_dna_sequence

    fasta_content = fetch_fasta_for_accessions(accessions)
    parsed_sequences = _parse_fasta_sequences(fasta_content)

    sequences = []
    skipped = []
    found_versions = set()
    found_bases = set()

    for seq in parsed_sequences:
        raw_header = (seq.get("name") or "").strip()
        seq_id, description = _split_fasta_header(raw_header)

        if seq_id:
            found_versions.add(seq_id.upper())
            found_bases.add(seq_id.split(".")[0].upper())

        sequence = clean_dna_sequence(seq.get("sequence", ""), min_length=1)
        if not sequence:
            skipped.append({
                "accession": seq_id or raw_header or "unknown",
                "reason": "empty_sequence"
            })
            continue

        if len(sequence) > max_sequence_bp:
            skipped.append({
                "accession": seq_id or raw_header or "unknown",
                "reason": "too_long",
                "length": len(sequence),
                "max_length": max_sequence_bp
            })
            continue

        sequences.append({
            "name": seq_id or raw_header,
            "organism": description,
            "sequence": sequence,
            "source": "genbank"
        })

    for accession in accessions:
        requested = accession.upper()
        if "." in requested:
            found = requested in found_versions
        else:
            found = requested in found_bases

        if not found:
            skipped.append({
                "accession": accession,
                "reason": "not_found"
            })

    return sequences, skipped



@bp.route('/blast', methods=['POST'])
@limiter.limit("10 per minute; 200 per hour")
def run_blast():
    """
    Run BLAST on a single sequence or accession.
    
    Request: { "query": "<sequence or accession>" }
    Response: { "status": "success", "sequences": [...], "message": "..." }
    """
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    
    if not query:
        return jsonify({"status": "error", "error": "No query provided"}), 400

    try:
        from app.services.blast_service import blast_from_sequence, blast_from_accessions
        from pathlib import Path

        # Extract + clamp parameters. Bad/missing values fall back to the
        # defaults; out-of-range values are pulled into the safe band.
        try:
            min_identity = float(data.get('min_identity', 90.0))
        except (TypeError, ValueError):
            min_identity = 90.0
        min_identity = max(50.0, min(100.0, min_identity))

        try:
            max_sequences = int(data.get('max_sequences', 50))
        except (TypeError, ValueError):
            max_sequences = 50
        max_sequences = max(1, min(500, max_sequences))
        
        # Determine if query is an accession or a sequence
        if _is_genbank_accession(query):
            logger.info(f"BLAST API: Detected accession: {query}")
            result = blast_from_accessions([query], Config, min_identity=min_identity, max_sequences=max_sequences)
        else:
            # Assume it's a raw sequence
            logger.info(f"BLAST API: Using sequence query ({len(query)} chars)")
            result = blast_from_sequence(query, Config, min_identity=min_identity, max_sequences=max_sequences)
        
        # Read FASTA content from the file path returned by blast service
        fasta_path = result.get('fasta_path', '')
        fasta_content = ''
        if fasta_path:
            path = Path(fasta_path)
            if path.exists():
                fasta_content = path.read_text()
        
        sequences = _parse_fasta_sequences(fasta_content)
        
        # Merge organism info from hit_details into sequences
        hit_details = result.get('hit_details', [])
        organism_map = {h['accession']: h.get('organism', '') for h in hit_details}
        
        for seq in sequences:
            # Extract accession from the full name (first word, without version)
            # Full name could be "AY702745.1 Mycena amicta..." - we need "AY702745" for lookup
            first_word = seq['name'].split()[0] if seq['name'] else ''
            acc_no_version = first_word.split('.')[0]
            seq['organism'] = organism_map.get(acc_no_version, organism_map.get(first_word, ''))
        
        return jsonify({
            "status": "success",
            "sequences": sequences,
            "accessions": result.get('hit_accessions', []),
            "message": f"Found {len(sequences)} related sequences"
        })
        
    except ValueError as e:
        # Invalid/oversized BLAST input is a user-correctable request, not an
        # application crash. Keep it out of the 500 log and return the useful
        # explanation to the Tree Builder.
        logger.warning("BLAST request rejected: %s", e)
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e, where="blast")


@bp.route('/genbank/accessions', methods=['POST'])
@limiter.limit("40 per minute; 600 per hour")
def fetch_genbank_accessions():
    """
    Fetch one or more GenBank accessions for direct queue insertion.

    Request: { "accessions": ["OR807397", "OR807397.1"] }
    Response: { "status": "success", "sequences": [...], "skipped": [...] }
    """
    data = request.get_json(silent=True) or {}
    raw_accessions = data.get("accessions", data.get("input", ""))

    accessions, invalid = _parse_genbank_accession_tokens(raw_accessions)
    if invalid:
        return jsonify({
            "status": "error",
            "error": f"Invalid GenBank accession(s): {', '.join(invalid[:10])}"
        }), 400

    if not accessions:
        return jsonify({"status": "error", "error": "No GenBank accessions provided"}), 400

    if len(accessions) > MAX_CUSTOM_GENBANK_ACCESSIONS:
        return jsonify({
            "status": "error",
            "error": f"Too many accessions (max {MAX_CUSTOM_GENBANK_ACCESSIONS})"
        }), 400

    try:
        sequences, skipped = _fetch_genbank_sequences_for_queue(
            accessions,
            max_sequence_bp=MAX_CUSTOM_GENBANK_SEQUENCE_BP
        )

        if not sequences:
            too_long = [item for item in skipped if item.get("reason") == "too_long"]
            if too_long:
                return jsonify({
                    "status": "error",
                    "error": "No sequences were added because all fetched GenBank records exceeded the size limit.",
                    "skipped": skipped,
                    "max_sequence_bp": MAX_CUSTOM_GENBANK_SEQUENCE_BP
                }), 400

            return jsonify({
                "status": "error",
                "error": "No GenBank sequences found for the provided accession(s).",
                "skipped": skipped,
                "max_sequence_bp": MAX_CUSTOM_GENBANK_SEQUENCE_BP
            }), 404

        message = f"Fetched {len(sequences)} GenBank sequence{'s' if len(sequences) != 1 else ''}"
        if skipped:
            message += f" ({len(skipped)} skipped)"

        return jsonify({
            "status": "success",
            "sequences": sequences,
            "skipped": skipped,
            "max_sequence_bp": MAX_CUSTOM_GENBANK_SEQUENCE_BP,
            "message": message
        })
    except Exception as e:
        return _server_error(e, where="genbank_accessions")


@bp.route('/genbank/locations', methods=['POST'])
@limiter.limit("60 per minute; 900 per hour")
def fetch_genbank_locations():
    """
    Look up collection locations for GenBank accessions.

    Used by the Tree Builder's "add collection locations" option, which appends
    the location to FASTA headers for pasted accessions and for accessions found
    inside pasted FASTA headers.

    Request: { "accessions": ["OR807397", "MJ505555.1"] }
    Response: { "status": "success", "locations": {"OR807397": "USA: Arizona, Greenlee County"}, "missing": [...] }
    """
    data = request.get_json(silent=True) or {}
    raw_accessions = data.get("accessions", data.get("input", ""))

    # Tokens scraped out of FASTA headers are best-effort guesses, so silently
    # drop anything that is not accession-shaped instead of failing the request.
    accessions, _invalid = _parse_genbank_accession_tokens(raw_accessions)

    if not accessions:
        return jsonify({"status": "error", "error": "No GenBank accessions provided"}), 400

    if len(accessions) > MAX_CUSTOM_GENBANK_ACCESSIONS:
        return jsonify({
            "status": "error",
            "error": f"Too many accessions (max {MAX_CUSTOM_GENBANK_ACCESSIONS})"
        }), 400

    try:
        from app.services.genbank_location_service import lookup_locations

        locations, missing = lookup_locations(accessions)
        return jsonify({
            "status": "success",
            "locations": locations,
            "missing": missing
        })
    except Exception as e:
        return _server_error(e, where="genbank_locations")


def gather_mycomap_sequences_for_queue(url, include_ncbi=True, include_local=True,
                                       filter_conflicting_local_fasta=False,
                                       allow_identical_sequences_different_locations=True,
                                       fetch_time_budget=None):
    """Reusable helper for fetching MycoMap BLAST result sequences.

    Returns a tuple ``(payload, error_response)`` where ``payload`` is the
    dict normally returned by /api/mycomap on success (sequences,
    ncbi_count, local_count, blast_metrics_count, message) and
    ``error_response`` is a ``(json_dict, http_status)`` tuple on failure.
    Exactly one of the two will be non-None. The local FASTA conflict filter
    is deliberately opt-in because identical barcode sequences can be valid
    records from separate collection locations.

    ``fetch_time_budget`` bounds the total upstream FASTA fetch time. Callers
    running inside a request must pass one (see
    ``INTERACTIVE_FETCH_BUDGET_SECONDS``) so a MycoMap outage cannot hold a
    Gunicorn slot for the full retry budget; worker callers leave it None.
    """
    if not url:
        return None, ({"status": "error", "error": "No URL provided"}, 400)
    if not include_ncbi and not include_local:
        return None, ({
            "status": "error",
            "error": "Select at least one result type (NCBI or Local)",
        }, 400)

    from app.services.mycomap_service import (
        fetch_mycomap_blast_metrics,
        fetch_mycomap_fasta,
        improve_mycomap_sequence_name,
        parse_mycomap_ncbi_fasta_header,
        prefer_local_mycomap_taxa,
        uniquify_mycomap_sequence_names,
        validate_mycomap_sequence_url,
        validate_mycomap_url,
    )
    from app.services.fasta_utils import clean_dna_sequence

    filter_conflicting_local_fasta = coerce_bool(
        filter_conflicting_local_fasta, default=False
    )[0]
    allow_identical_sequences_different_locations = coerce_bool(
        allow_identical_sequences_different_locations, default=True
    )[0]

    contaminant_re = re.compile(r'contamin(?:a|e)nt', flags=re.IGNORECASE)
    lowquality_re = re.compile(r'low\s*quality|lowquality', flags=re.IGNORECASE)

    def is_contaminant_sequence(seq, metric=None):
        if metric and metric.get('is_contaminant'):
            return True
        labels = (
            seq.get('name', ''),
            seq.get('_mycomap_original_name', ''),
        )
        return any(contaminant_re.search(str(label or '')) for label in labels)

    def remove_lowquality_label(name):
        cleaned = lowquality_re.sub('', str(name or ''))
        return re.sub(r'\s+', ' ', cleaned).strip(' ,;:-_')

    # Alan 8/14/26 - A sequence record URL also carries an r<digits> token, so
    # validate_mycomap_url() happily reads it as a BLAST ID and would fetch an
    # unrelated BLAST record. Route it to the endpoint that actually understands it.
    if validate_mycomap_sequence_url(url):
        return None, ({
            "status": "error",
            "error": (
                "That is a MycoMap sequence record page, not a BLAST results page. "
                "Use the sequence import for it, or paste the URL from the BLAST "
                "Search page to import BLAST hits."
            ),
            "sequence_url": True,
        }, 400)

    blast_id = validate_mycomap_url(url)
    if not blast_id:
        return None, ({
            "status": "error",
            "error": "Invalid Mycomap URL. URL must be from mycomap.com and contain a result ID (e.g., r12345)",
        }, 400)

    # Alan 8/15/26 - Log the URL alongside the extracted ID. The raw URL was logged
    # only when validation *failed*, so a URL that parsed to the wrong ID and then
    # 404'd upstream left no record of what the user actually pasted -- which is
    # exactly the case that needed diagnosing on 2026-08-14. This line fires once
    # per operation, not once per validation call, so it is not the volume problem
    # the old per-validation INFO line was.
    logger.info(
        f"Mycomap helper: blast_id={blast_id} (ncbi={include_ncbi}, local={include_local}) url={url}"
    )
    result = fetch_mycomap_fasta(
        blast_id, include_ncbi, include_local, time_budget=fetch_time_budget
    )

    if result['errors'] and not result['fasta_content']:
        # An upstream 404 means the record does not exist -- a real answer, not a
        # gateway fault. Say so in words the user can act on instead of returning
        # a 502 carrying raw urllib text ("Network error fetching fasta: HTTP
        # Error 404: Not Found"), which reads as a site outage and prompts retries.
        if all('404' in err for err in result['errors']):
            return None, ({
                "status": "error",
                "error": (
                    f"MycoMap has no BLAST result r{blast_id}. Check the URL — the "
                    "result may have expired, or the link may point somewhere other "
                    "than a BLAST Search page."
                ),
            }, 404)
        return None, ({"status": "error", "error": "; ".join(result['errors'])}, 502)

    sequences = _parse_fasta_sequences(result['fasta_content'])
    for seq in sequences:
        seq['_mycomap_original_name'] = seq.get('name', '')

    ncbi_count = result['ncbi_count']
    for i, seq in enumerate(sequences):
        seq['source'] = 'mycomap'
        if include_ncbi and include_local:
            seq['hit_source'] = 'ncbi' if i < ncbi_count else 'local'
        elif include_ncbi:
            seq['hit_source'] = 'ncbi'
        else:
            seq['hit_source'] = 'local'

        if seq['hit_source'] == 'ncbi':
            header_details = parse_mycomap_ncbi_fasta_header(
                seq.get('_mycomap_original_name', '')
            )
            for field in (
                'accession', 'taxon', 'raw_mycomap_taxon', 'voucher', 'location',
                'raw_fasta_header', 'raw_ncbi_description',
                'mycomap_header_format',
            ):
                seq[field] = header_details.get(field, '')
            seq['name'] = header_details.get('display_name') or seq.get('name', '')

    from app.services.blast_service import fetch_fasta_for_accessions
    accessions_to_enrich = []
    for seq in sequences:
        parts = seq['name'].split()
        if (
            len(parts) == 1
            and seq.get('mycomap_header_format') == 'legacy_flat'
            and _is_genbank_accession(parts[0].split('.')[0])
        ):
            accessions_to_enrich.append(parts[0].split('.')[0])

    if accessions_to_enrich:
        enriched_fasta = fetch_fasta_for_accessions(accessions_to_enrich)
        if enriched_fasta:
            enriched_seqs = _parse_fasta_sequences(enriched_fasta)
            enriched_map = {s['name'].split()[0].split('.')[0]: s for s in enriched_seqs if s['name'].strip()}
            for seq in sequences:
                parts = seq['name'].split()
                if len(parts) == 1:
                    base_acc = parts[0].split('.')[0]
                    if base_acc in enriched_map:
                        seq['name'] = enriched_map[base_acc]['name']
                        seq['sequence'] = enriched_map[base_acc]['sequence']

    filtered_records = []
    original_count = len(sequences)
    cleaned_sequences = []
    for seq in sequences:
        seq['sequence'] = clean_dna_sequence(seq['sequence'], min_length=1)
        if seq['sequence']:
            cleaned_sequences.append(seq)
        else:
            _append_import_filter_detail(
                filtered_records,
                name=seq.get('name', ''),
                source='mycomap',
                hit_source=seq.get('hit_source', ''),
                reason='invalid_sequence',
                reason_label='Invalid or short sequence after DNA cleanup',
            )
    sequences = cleaned_sequences
    dropped_count = original_count - len(sequences)

    metrics_by_key = fetch_mycomap_blast_metrics(blast_id, source_url=url)
    query_tokens = set()
    query_sequences = []
    if filter_conflicting_local_fasta:
        query_tokens = _extract_mycomap_query_tokens(url)
        query_sequences = _mycomap_query_sequences(
            sequences, metrics_by_key, query_tokens
        )
    sequence_metrics = [
        _mycomap_metric_for_sequence(seq, metrics_by_key)
        for seq in sequences
    ]
    metric_conflicts = {}
    conflict_sequences = []
    if filter_conflicting_local_fasta:
        for idx, seq in enumerate(sequences):
            metric = sequence_metrics[idx]
            conflict_detail = _mycomap_local_fasta_metric_conflict_detail(
                seq,
                metric,
                query_sequences,
                query_tokens,
                allow_identical_sequences_different_locations,
            )
            if conflict_detail:
                metric_conflicts[idx] = conflict_detail
                conflict_sequences.append({
                    'sequence': ''.join(str(seq.get('sequence') or '').split()).upper(),
                    'location': _mycomap_sequence_location(seq, metric),
                })

    metrics_attached_count = 0
    contaminant_dropped_count = 0
    conflicting_local_dropped_count = 0
    filtered_sequences = []
    for idx, seq in enumerate(sequences):
        metric = sequence_metrics[idx]
        conflict_detail = metric_conflicts.get(idx)
        if filter_conflicting_local_fasta and not conflict_detail and not metric:
            conflict_detail = _mycomap_local_fasta_group_conflict_detail(
                seq,
                conflict_sequences,
                query_tokens,
                allow_identical_sequences_different_locations,
            )
        if conflict_detail:
            conflicting_local_dropped_count += 1
            _append_import_filter_detail(
                filtered_records,
                name=seq.get('name', ''),
                source='mycomap',
                hit_source=seq.get('hit_source', ''),
                reason=conflict_detail.get('reason'),
                reason_label=conflict_detail.get('reason_label'),
                reported_identity=conflict_detail.get('reported_identity'),
                query_similarity=conflict_detail.get('query_similarity'),
            )
            logger.warning(
                "Dropped conflicting MycoMap localFasta record: blast_id=%s name=%r reported_identity=%s",
                blast_id,
                seq.get('name', ''),
                metric.get('identity') if metric else None,
            )
            continue
        if is_contaminant_sequence(seq, metric):
            contaminant_dropped_count += 1
            _append_import_filter_detail(
                filtered_records,
                name=seq.get('name', ''),
                source='mycomap',
                hit_source=seq.get('hit_source', ''),
                reason='contaminant',
                reason_label='Marked as contaminant by MycoMap label or BLAST table',
                reported_identity=metric.get('identity') if metric else None,
            )
            continue
        if metric:
            seq['name'] = improve_mycomap_sequence_name(
                seq.get('name', ''),
                metric,
                seq.get('hit_source', ''),
                accession=seq.get('accession', ''),
                voucher=seq.get('voucher', ''),
                location=seq.get('location', ''),
            )
            if metric.get('species_name'):
                seq['taxon'] = metric['species_name']
            seq['identity'] = metric.get('identity')
            seq['query_cover'] = metric.get('query_cover')
            seq['subject_cover'] = metric.get('subject_cover')
            seq['location'] = metric.get('mycomap_location') or seq.get('location') or ''
            seq['blast_metrics_available'] = any(
                seq[field] is not None for field in ('identity', 'query_cover', 'subject_cover')
            )
            if seq['blast_metrics_available']:
                metrics_attached_count += 1
        else:
            seq['identity'] = None
            seq['query_cover'] = None
            seq['subject_cover'] = None
            seq['location'] = seq.get('location') or ''
            seq['blast_metrics_available'] = False
        seq['name'] = remove_lowquality_label(seq.get('name', ''))
        filtered_sequences.append(seq)
    sequences = prefer_local_mycomap_taxa(filtered_sequences)
    sequences, duplicate_observation_count = _dedupe_mycomap_observation_records(
        sequences,
        filtered_records,
    )
    sequences = uniquify_mycomap_sequence_names(sequences)
    for seq in sequences:
        seq.pop('_mycomap_original_name', None)
    metrics_attached_count = sum(
        1 for seq in sequences if seq.get('blast_metrics_available')
    )

    parts = []
    if include_ncbi:
        parts.append(f"{result['ncbi_count']} NCBI")
    if include_local:
        parts.append(f"{result['local_count']} local")
    msg = f"Fetched {' + '.join(parts)} sequences from Mycomap"
    if dropped_count > 0:
        msg += f" ({dropped_count} dropped due to invalid/short sequences)"
    if contaminant_dropped_count > 0:
        msg += f" ({contaminant_dropped_count} contaminant sequence{'s' if contaminant_dropped_count != 1 else ''} filtered)"
    if conflicting_local_dropped_count > 0:
        msg += f" ({conflicting_local_dropped_count} conflicting local FASTA sequence{'s' if conflicting_local_dropped_count != 1 else ''} filtered)"
    if duplicate_observation_count > 0:
        msg += f" ({duplicate_observation_count} duplicate observation record{'s' if duplicate_observation_count != 1 else ''} collapsed)"
    # Alan 8/14/26 - Call out a source that failed outright. A MycoMap 500 on one half
    # of the fetch still returns the other half, and burying that in a trailing
    # "(warnings: ...)" meant users imported a partial result set without noticing.
    failed_sources = result.get('failed_sources') or []
    if failed_sources:
        labels = {'ncbi': 'NCBI', 'local': 'local MycoBLAST'}
        named = ' and '.join(labels.get(s, s) for s in failed_sources)
        msg += (
            f" -- WARNING: {named} results could not be retrieved from MycoMap, "
            f"so those sequences are missing. Try again in a moment."
        )
    if result['errors']:
        msg += f" (warnings: {'; '.join(result['errors'])})"

    return {
        "status": "success",
        "sequences": sequences,
        "ncbi_count": result['ncbi_count'],
        "local_count": result['local_count'],
        "failed_sources": failed_sources,
        "blast_metrics_count": metrics_attached_count,
        "conflicting_local_count": conflicting_local_dropped_count,
        "duplicate_observation_count": duplicate_observation_count,
        "conflicting_local_filter_enabled": filter_conflicting_local_fasta,
        "allow_identical_sequences_different_locations": (
            allow_identical_sequences_different_locations
        ),
        "import_filter_details": {
            "mycomap": {
                "label": "MycoMap import filters",
                "counts": {
                    "invalid_sequence": dropped_count,
                    "contaminant": contaminant_dropped_count,
                    "conflicting_local": conflicting_local_dropped_count,
                    "duplicate_observation": duplicate_observation_count,
                },
                "filtered_records": filtered_records,
            }
        },
        "message": msg,
    }, None


# Alan 8/14/26 - Accept MycoMap sequence record pages
# (https://mycomap.com/genetics/sequences/...-r<id>/), which the Tree Builder used to
# reject as an invalid URL. These identify a single sequence rather than a BLAST
# search, so they get their own endpoint and return one queue-ready entry.
@bp.route('/mycomap/sequence', methods=['POST'])
@limiter.limit("40 per minute; 600 per hour")
def fetch_mycomap_sequence_record():
    """
    Fetch one sequence from a Mycomap sequence record URL.

    Request:  { "url": "https://mycomap.com/genetics/sequences/.../...-r763916/" }
    Response: { "status": "success", "sequences": [ {...} ], "message": "..." }
    """
    from app.services.mycomap_service import (
        fetch_mycomap_sequence,
        validate_mycomap_sequence_url,
    )
    from app.services.fasta_utils import clean_dna_sequence

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({"status": "error", "error": "Missing MycoMap sequence URL."}), 400

    sequence_id = validate_mycomap_sequence_url(url)
    if not sequence_id:
        return jsonify({
            "status": "error",
            "error": (
                "Invalid MycoMap sequence URL. It must be a mycomap.com sequence "
                "record page containing a record ID (e.g. .../genetics/sequences/"
                "ont_sequences/<slug>-r12345/)."
            ),
        }), 400

    try:
        record = fetch_mycomap_sequence(sequence_id)
    except Exception as e:
        return _server_error(e, where="mycomap_sequence")

    if record.get('errors') or not record.get('sequence'):
        error = '; '.join(record.get('errors') or []) or (
            f"MycoMap sequence {sequence_id} has no DNA sequence attached."
        )
        # 502 for an upstream fault, 404 when the record simply has nothing to import.
        status = 404 if 'does not have a DNA sequence' in error else 502
        return jsonify({"status": "error", "error": error}), status

    cleaned = clean_dna_sequence(record['sequence'])
    if not cleaned:
        return jsonify({
            "status": "error",
            "error": f"MycoMap sequence {sequence_id} contains no usable DNA characters.",
        }), 422

    sequence_entry = {
        "name": record.get('name') or f"MycoMap{sequence_id}",
        "sequence": cleaned,
        "organism": record.get('species') or '',
        "source": "mycomap",
        "hit_source": "sequence_record",
        "mycomap_sequence_id": sequence_id,
        "mycomap_record_url": record.get('record_url') or url,
    }

    return jsonify({
        "status": "success",
        "sequences": [sequence_entry],
        "message": (
            f"Fetched 1 sequence ({len(cleaned)} bp) from MycoMap record {sequence_id}"
        ),
    })


@bp.route('/mycomap', methods=['POST'])
@limiter.limit("40 per minute; 600 per hour")
def fetch_mycomap():
    """
    Fetch sequences from a Mycomap BLAST results URL.

    Request: {
        "url": "<mycomap URL>",
        "include_ncbi": true,
        "include_local": true,
        "filter_conflicting_local_fasta": false,
        "allow_identical_sequences_different_locations": true
    }
    Response: { "status": "success", "sequences": [...], "message": "..." }
    """
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    include_ncbi = data.get('include_ncbi', True)
    include_local = data.get('include_local', True)
    filter_conflicting_local_fasta, filter_conflicts_valid = coerce_bool(
        data.get('filter_conflicting_local_fasta'), default=False
    )
    if not filter_conflicts_valid:
        return jsonify({
            "status": "error",
            "error": "filter_conflicting_local_fasta must be a boolean.",
        }), 422
    allow_identical_sequences_different_locations, allow_locations_valid = coerce_bool(
        data.get('allow_identical_sequences_different_locations'), default=True
    )
    if not allow_locations_valid:
        return jsonify({
            "status": "error",
            "error": "allow_identical_sequences_different_locations must be a boolean.",
        }), 422

    try:
        from app.services.mycomap_service import INTERACTIVE_FETCH_BUDGET_SECONDS

        payload, err = gather_mycomap_sequences_for_queue(
            url,
            include_ncbi,
            include_local,
            filter_conflicting_local_fasta=filter_conflicting_local_fasta,
            allow_identical_sequences_different_locations=(
                allow_identical_sequences_different_locations
            ),
            # This runs in a request handler, so cap the upstream retry budget.
            fetch_time_budget=INTERACTIVE_FETCH_BUDGET_SECONDS,
        )
        if err is not None:
            body, status = err
            return jsonify(body), status
        return jsonify(payload)
    except Exception as e:
        return _server_error(e, where="mycomap")


@bp.route('/mycomap/refresh', methods=['POST'])
@limiter.limit("10 per minute; 200 per hour")
def start_mycomap_blast_refresh():
    """
    Refresh an existing Mycomap BLAST result (local always, NCBI optionally)
    and gather its sequences for the queue. Runs as a background job because
    an NCBI rebuild can take about 10 minutes to become available.

    Request: {
        "url": "<mycomap URL>",
        "rebuild_ncbi": false,
        "local_limit": 50,
        "ncbi_limit": 100,
        "include_ncbi": true,
        "include_local": true
    }
    Response: { "status": "success", "job_id": "<uuid>" }
    """
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"status": "error", "error": "No URL provided"}), 400

    from app.services.mycomap_service import validate_mycomap_url
    if not validate_mycomap_url(url):
        return jsonify({
            "status": "error",
            "error": "Invalid Mycomap URL. URL must be from mycomap.com and contain a result ID (e.g., r12345)",
        }), 400

    rebuild_ncbi, rebuild_ncbi_valid = coerce_bool(data.get('rebuild_ncbi'), default=False)
    if not rebuild_ncbi_valid:
        return jsonify({"status": "error", "error": "rebuild_ncbi must be a boolean."}), 422
    include_ncbi, include_ncbi_valid = coerce_bool(data.get('include_ncbi'), default=True)
    include_local, include_local_valid = coerce_bool(data.get('include_local'), default=True)
    if not include_ncbi_valid or not include_local_valid:
        return jsonify({"status": "error", "error": "include_ncbi/include_local must be booleans."}), 422

    try:
        from app.workers.queue import enqueue_mycomap_blast_refresh_job
        job_id = enqueue_mycomap_blast_refresh_job({
            "url": url,
            "rebuild_ncbi": rebuild_ncbi,
            "local_limit": data.get('local_limit'),
            "ncbi_limit": data.get('ncbi_limit'),
            "include_ncbi": include_ncbi,
            "include_local": include_local,
        })
        return jsonify({"status": "success", "job_id": job_id})
    except Exception as e:
        return _server_error(e, where="mycomap_refresh")


@bp.route('/mycomap/refresh/<job_id>', methods=['GET'])
def get_mycomap_blast_refresh_status(job_id):
    """Poll a MycoMap BLAST refresh job started via /api/mycomap/refresh."""
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    from app.workers.queue import get_job_status
    return jsonify(get_job_status(job_id))


def _inat_tree_rate_key():
    """Rate-limit key for /api/inaturalist/tree.

    Logged-in admin emails are exempt (the limit string handles that via a
    short-circuit). Logged-in users get bucketed by user id; anonymous
    callers fall back to remote IP.
    """
    from flask_limiter.util import get_remote_address
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return f"ip:{get_remote_address()}"


def _inat_tree_rate_limit():
    """Per-call rate string. Admin emails get an effectively unlimited bucket."""
    if current_user.is_authenticated:
        email = (current_user.email or "").strip().lower()
        admins = Config.INAT_OAUTH_ADMIN_EMAILS
        if email in admins:
            return "10000 per minute"
        return "30 per 5 minutes"
    return "60 per hour"


def _inat_tree_preview_rate_limit():
    """Rate string for the read-only preview lookup.

    Alan 8/14/26 - Preview used to share the job-creation limit, so pasting a few
    observations to look at them burned the same budget as queueing trees and real
    users were served 429s (the only 429s in the whole access-log history). Preview
    creates nothing: it is a lookup that fires while the user pastes, so it gets a
    budget sized for interactive use. Still bounded, because each call fans out to
    the iNaturalist API, which rate-limits us in turn.
    """
    if current_user.is_authenticated:
        email = (current_user.email or "").strip().lower()
        if email in Config.INAT_OAUTH_ADMIN_EMAILS:
            return "10000 per minute"
        return "60 per minute; 1000 per hour"
    return "30 per minute; 400 per hour"


def _mycomap_rerun_limit_from_request(data, result_type):
    """Return a validated MycoMap rerun limit from compatible request keys."""
    from app.services.mycomap_service import validate_mycomap_rerun_limit

    aliases = {
        "local": ("mycomap_local_limit", "mycomap_local_blast_limit", "local_limit"),
        "ncbi": ("mycomap_ncbi_limit", "mycomap_ncbi_blast_limit", "ncbi_limit"),
    }
    value = None
    for key in aliases.get(result_type, ()):
        if key in data:
            value = data.get(key)
            break
    return validate_mycomap_rerun_limit(value, result_type)


@bp.route('/inaturalist/tree', methods=['POST'])
@limiter.limit(_inat_tree_rate_limit, key_func=_inat_tree_rate_key)
def inaturalist_tree():
    """Create one-click Dikarya tree jobs from iNaturalist input.

    Request: { "observation": "<id-or-single-observation-url>" }
    Scope inputs may include { "resolved_type": "user"|"project" }.
    """
    from app.services.inaturalist_tree_service import (
        InatTreeError, create_job_from_inat_observation,
        create_jobs_from_inat_scope, parse_inaturalist_tree_input,
    )
    data = request.get_json(silent=True) or {}
    raw = data.get('observation') or data.get('url') or data.get('input') or ''
    resolved_type = (data.get('resolved_type') or '').strip().lower()
    rebuild_ncbi_blast, rebuild_ncbi_ok = coerce_bool(
        data.get('rebuild_ncbi_blast', data.get('rebuild_ncbi')),
        default=False,
    )
    if not rebuild_ncbi_ok:
        return jsonify({
            "status": "error",
            "error": "rebuild_ncbi_blast must be a boolean.",
        }), 422
    recreate_existing_tree, recreate_existing_ok = coerce_bool(
        data.get('recreate_existing_tree'),
        default=False,
    )
    if not recreate_existing_ok:
        return jsonify({
            "status": "error",
            "error": "recreate_existing_tree must be a boolean.",
        }), 422
    local_limit, local_limit_error = _mycomap_rerun_limit_from_request(data, "local")
    if local_limit_error:
        return jsonify({"status": "error", "error": local_limit_error}), 422
    # Alan 8/4/26 - Allow an extra tree that leaves the observation's existing
    # Phylogenetic Tree field URL in place.
    keep_existing_tree_url, keep_existing_ok = coerce_bool(
        data.get('keep_existing_tree_url'),
        default=False,
    )
    if not keep_existing_ok:
        return jsonify({
            "status": "error",
            "error": "keep_existing_tree_url must be a boolean.",
        }), 422
    ncbi_limit, ncbi_limit_error = _mycomap_rerun_limit_from_request(data, "ncbi")
    if ncbi_limit_error:
        return jsonify({"status": "error", "error": ncbi_limit_error}), 422
    try:
        parsed = parse_inaturalist_tree_input(raw)
        if parsed.get("type") == "single_observation":
            result = create_job_from_inat_observation(
                raw,
                user=current_user,
                rebuild_ncbi_blast=rebuild_ncbi_blast,
                recreate_existing_tree=recreate_existing_tree,
                keep_existing_tree_url=keep_existing_tree_url,
                mycomap_local_limit=local_limit,
                mycomap_ncbi_limit=ncbi_limit,
                public_base_url=request.url_root,
            )
            return jsonify(result), 202
        if rebuild_ncbi_blast or recreate_existing_tree or keep_existing_tree_url:
            return jsonify({
                "status": "error",
                "error": (
                    "NCBI BLAST rebuild and existing-tree options are only "
                    "supported for a single iNaturalist observation."
                ),
            }), 422
        if not resolved_type:
            return jsonify({
                "status": "error",
                "error": "Preview this username or project and provide resolved_type before queueing.",
            }), 409
        result = create_jobs_from_inat_scope(
            raw,
            resolved_type=resolved_type,
            user=current_user,
            mycomap_local_limit=local_limit,
            mycomap_ncbi_limit=ncbi_limit,
            public_base_url=request.url_root,
        )
        return jsonify(result), 202
    except InatTreeError as e:
        error_payload = {"status": "error", "error": str(e)}
        if e.details:
            error_payload.update(e.details)
            error_payload["message"] = str(e)
        return jsonify(error_payload), e.status
    except Exception as e:
        return _server_error(e, where="inaturalist_tree")


@bp.route('/inaturalist/tree/preview', methods=['POST'])
@limiter.limit(_inat_tree_preview_rate_limit, key_func=_inat_tree_rate_key)
def inaturalist_tree_preview():
    """Preview iNaturalist one-click tree scope and eligibility."""
    from app.services.inaturalist_tree_service import (
        InatTreeError, preview_inaturalist_tree_input,
    )
    data = request.get_json(silent=True) or {}
    raw = data.get('input') or data.get('observation') or data.get('url') or ''
    resolved_type = data.get('resolved_type')
    try:
        return jsonify(preview_inaturalist_tree_input(raw, resolved_type=resolved_type))
    except InatTreeError as e:
        return jsonify({"status": "error", "error": str(e)}), e.status
    except Exception as e:
        return _server_error(e, where="inaturalist_tree_preview")


@bp.route('/inaturalist/tree/batch', methods=['POST'])
@limiter.limit(_inat_tree_rate_limit, key_func=_inat_tree_rate_key)
def inaturalist_tree_batch():
    """Queue one-click tree jobs for an iNaturalist user or project."""
    from app.services.inaturalist_tree_service import (
        InatTreeError, create_jobs_from_inat_scope,
    )
    data = request.get_json(silent=True) or {}
    raw = data.get('input') or data.get('observation') or data.get('url') or ''
    resolved_type = data.get('resolved_type') or ''
    rebuild_ncbi_blast, rebuild_ncbi_ok = coerce_bool(
        data.get('rebuild_ncbi_blast', data.get('rebuild_ncbi')),
        default=False,
    )
    if not rebuild_ncbi_ok:
        return jsonify({
            "status": "error",
            "error": "rebuild_ncbi_blast must be a boolean.",
        }), 422
    recreate_existing_tree, recreate_existing_ok = coerce_bool(
        data.get('recreate_existing_tree'),
        default=False,
    )
    if not recreate_existing_ok:
        return jsonify({
            "status": "error",
            "error": "recreate_existing_tree must be a boolean.",
        }), 422
    keep_existing_tree_url, keep_existing_ok = coerce_bool(
        data.get('keep_existing_tree_url'),
        default=False,
    )
    if not keep_existing_ok:
        return jsonify({
            "status": "error",
            "error": "keep_existing_tree_url must be a boolean.",
        }), 422
    local_limit, local_limit_error = _mycomap_rerun_limit_from_request(data, "local")
    if local_limit_error:
        return jsonify({"status": "error", "error": local_limit_error}), 422
    ncbi_limit, ncbi_limit_error = _mycomap_rerun_limit_from_request(data, "ncbi")
    if ncbi_limit_error:
        return jsonify({"status": "error", "error": ncbi_limit_error}), 422
    if rebuild_ncbi_blast or recreate_existing_tree or keep_existing_tree_url:
        return jsonify({
            "status": "error",
            "error": (
                "NCBI BLAST rebuild and existing-tree options are only "
                "supported for a single iNaturalist observation."
            ),
        }), 422
    try:
        result = create_jobs_from_inat_scope(
            raw,
            resolved_type=resolved_type,
            user=current_user,
            mycomap_local_limit=local_limit,
            mycomap_ncbi_limit=ncbi_limit,
            public_base_url=request.url_root,
        )
        return jsonify(result), 202
    except InatTreeError as e:
        error_payload = {"status": "error", "error": str(e)}
        if e.details:
            error_payload.update(e.details)
            error_payload["message"] = str(e)
        return jsonify(error_payload), e.status
    except Exception as e:
        return _server_error(e, where="inaturalist_tree_batch")


@bp.route('/inaturalist', methods=['POST'])
@limiter.limit("40 per minute; 600 per hour")
def fetch_inaturalist():
    """
    Fetch DNA sequences from iNaturalist observations.
    
    Request: { 
        "url": "<iNaturalist URL>",
        "action": "analyze" | "fetch_sequences"
    }
    
    Response (analyze): {
        "status": "success",
        "total_observations": int,
        "dna_count": int,
        "provisional_species": [{"name": "...", "count": int}, ...],
        "is_single": bool,
        "can_blast": bool
    }
    
    Response (fetch_sequences): {
        "status": "success", 
        "sequences": [...],
        "message": "..."
    }
    """
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    action = data.get('action', 'analyze').strip()
    
    # Input sanitization
    if not url:
        return jsonify({"status": "error", "error": "No URL provided"}), 400
    
    # Limit URL length to prevent abuse
    if len(url) > 2000:
        return jsonify({"status": "error", "error": "URL too long"}), 400
    
    # Validate action
    if action not in ('analyze', 'fetch_sequences'):
        return jsonify({"status": "error", "error": "Invalid action"}), 400
    
    try:
        from app.services.inaturalist_service import (
            validate_inaturalist_url,
            fetch_inaturalist_data
        )
        
        # Validate URL format first
        url_info = validate_inaturalist_url(url)
        if not url_info:
            return jsonify({
                "status": "error",
                "error": "Invalid iNaturalist input. Please enter a 7 to 9 digit observation ID, "
                         "a valid observation URL (e.g., https://www.inaturalist.org/observations/1234567), or "
                         "observations search URL."
            }), 400
        
        logger.info(f"iNaturalist API: Processing URL type={url_info['type']}, action={action}")
        
        if action == 'analyze':
            # Fetch and analyze observations (default mode='all' gets both ITS and PSN stats)
            result = fetch_inaturalist_data(url, mode='all')

            # Return analysis without full sequences
            # Defensive: check sequences list exists and has items before accessing
            sequences = result.get('sequences', [])
            seq = sequences[0]['sequence'] if sequences else None
            
            # Determine if this is a single result based on actual ITS data
            # Use total_its_observations if available, falling back to total_observations
            total_its = result.get('total_its_observations', result['total_observations'])
            
            # is_single reflects the URL type (single observation URL)
            is_single_url = result.get('is_single', False)
            
            # can_blast requires exactly one usable ITS sequence found
            can_blast = bool(total_its == 1 and result['dna_count'] == 1 and len(sequences) == 1 and seq)
            
            return jsonify({
                "status": "success",
                "total_observations": result['total_observations'],
                "total_its_observations": result.get('total_its_observations', 0),
                "total_psn_observations": result.get('total_psn_observations', 0),
                "fetched_its_count": result.get('fetched_its_count', 0),
                "fetched_psn_count": result.get('fetched_psn_count', 0),
                "dna_count": result['dna_count'],
                "provisional_species": result['provisional_species'],
                "is_single": is_single_url,
                "can_blast": can_blast,
                # For single obs with DNA, include the sequence for BLAST
                "sequence": seq if can_blast else None,
                "mycomap_blast_url": result.get('mycomap_blast_url') if is_single_url else None,
                "truncated": result.get('truncated', False),
                "total_available": result.get('total_available', 0),
                "message": f"Found {result['dna_count']} observation(s) with DNA Barcode ITS"
            })
        else:
            # Optimize: Only fetch ITS sequences for queue adding (skip PSN stats)
            result = fetch_inaturalist_data(url, mode='its_only')
            
            # Return full sequences for queue
            return jsonify({
                "status": "success",
                "sequences": result['sequences'],
                "truncated": result.get('truncated', False),
                "total_available": result.get('total_available', 0),
                "message": f"Fetched {len(result['sequences'])} DNA sequences from iNaturalist"
            })
        
    except ValueError as e:
        logger.warning(f"iNaturalist API validation error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e, where="inaturalist")



@bp.route('/mushroom-observer', methods=['POST'])
@limiter.limit("40 per minute; 600 per hour")
def fetch_mushroom_observer():
    """Analyze one Mushroom Observer observation or return its selected ITS."""
    from app.services.mushroom_observer_service import (
        MushroomObserverError,
        analyze_observation,
        build_queue_sequence,
    )

    data = request.get_json(silent=True) or {}
    raw = data.get('observation') or data.get('url') or data.get('input') or ''
    action = str(data.get('action') or 'analyze').strip().lower()
    if action not in {'analyze', 'fetch_sequence'}:
        return jsonify({"status": "error", "error": "Invalid action."}), 400
    try:
        if action == 'analyze':
            return jsonify(analyze_observation(raw))
        sequence = build_queue_sequence(raw, data.get('sequence_id'))
        return jsonify({
            "status": "success",
            "sequences": [sequence],
            "message": "Fetched the selected ITS sequence from Mushroom Observer.",
        })
    except MushroomObserverError as exc:
        return jsonify({"status": "error", "error": str(exc)}), exc.status
    except Exception as exc:
        return _server_error(exc, where="mushroom_observer")


@bp.route('/mushroom-observer/tree', methods=['POST'])
@limiter.limit("20 per 5 minutes; 60 per hour")
def mushroom_observer_tree():
    """Queue a one-click tree for one selected Mushroom Observer ITS sequence."""
    from app.services.mushroom_observer_service import (
        MushroomObserverError,
        create_tree_job,
    )

    data = request.get_json(silent=True) or {}
    raw = data.get('observation') or data.get('url') or data.get('input') or ''
    rebuild_ncbi, rebuild_ok = coerce_bool(data.get('rebuild_ncbi_blast'), default=False)
    if not rebuild_ok:
        return jsonify({
            "status": "error",
            "error": "rebuild_ncbi_blast must be a boolean.",
        }), 422
    local_limit, local_error = _mycomap_rerun_limit_from_request(data, "local")
    if local_error:
        return jsonify({"status": "error", "error": local_error}), 422
    ncbi_limit, ncbi_error = _mycomap_rerun_limit_from_request(data, "ncbi")
    if ncbi_error:
        return jsonify({"status": "error", "error": ncbi_error}), 422
    try:
        result = create_tree_job(
            raw,
            data.get('sequence_id'),
            user=current_user,
            rebuild_ncbi_blast=rebuild_ncbi,
            mycomap_local_limit=local_limit,
            mycomap_ncbi_limit=ncbi_limit,
            public_base_url=request.url_root,
        )
        return jsonify(result), 202
    except MushroomObserverError as exc:
        return jsonify({"status": "error", "error": str(exc)}), exc.status
    except Exception as exc:
        return _server_error(exc, where="mushroom_observer_tree")




@bp.route('/job', methods=['POST'])
# Alan 8/14/26 - Raised so a user working through a batch of observations in one
# sitting is not cut off partway.
@limiter.limit("60 per hour; 300 per day")
def create_job():
    data = request.get_json() or {}
    
    # Extract Tree Params for Validation
    tree_method = data.get("tree_method", "nj")
    
    # Basic params
    job_params = {
        "input_type": data.get("input_type", "unknown"),
        "notes": data.get("notes", ""),
        "sequence": data.get("sequence", ""),
        "sequence_metadata": _normalize_sequence_metadata(data.get("sequence_metadata", [])),
        "import_filter_details": _normalize_import_filter_details(data.get("import_filter_details", {})),
        "mycomap_blast_url": data.get("mycomap_blast_url") or "",
        "accessions": data.get("accessions", []),
        "alignment_method": data.get("alignment_method", "default"),
        "trimming_method": data.get("trimming_method", Config.DEFAULT_TRIMMING_METHOD),
        "trim_terminal_overhangs": coerce_bool(data.get("trim_terminal_overhangs"), True)[0],
        "its_region": normalize_its_region(data.get("its_region")),
        "its_min_length": resolve_its_min_length(
            normalize_its_region(data.get("its_region")), data.get("its_min_length")
        ),
        "alignment_options": data.get("alignment_options", {}),
        "tree_method": tree_method,
        # Left unset when the caller omits it: the worker resolves the default
        # per method (ModelFinder for IQ-TREE, Config.DEFAULT_ML_MODEL otherwise).
        "tree_model": data.get("tree_model") or None,
        "bootstrap": data.get("bootstrap", 1000), # Legacy field
        "mcmc_generations": data.get("mcmc_generations", 50000),
        "mcmc_nruns": data.get("mcmc_nruns", 2),
        "mcmc_nchains": data.get("mcmc_nchains", 4),
        "mcmc_burnin_fraction": data.get("mcmc_burnin_fraction", 0.25),
        
        # New RAxML-NG params - Extract raw first
        "run_preset": data.get("run_preset", "fast_good"),
        "bootstrap_preset": data.get("bootstrap_preset", "standard"),
        "bootstrap_cap": data.get("bootstrap_cap"),
        "enable_bootstrap": data.get("enable_bootstrap", True),
        "start_tree_override": data.get("start_tree_override"),
        "moose_enabled": data.get("moose_enabled", False),
        "early_stopping": data.get("early_stopping", False),
        "seed": data.get("seed"),
        "outgroup": data.get("outgroup")
    }

    # job_params is a whitelist, so IQ-TREE's SH-aLRT setting has to be copied
    # across explicitly. Only forward it when the caller actually sent one --
    # leaving the key absent lets the worker apply DEFAULT_IQTREE_ALRT, while an
    # explicit 0 still disables the test.
    if "alrt_replicates" in data:
        job_params["alrt_replicates"] = data.get("alrt_replicates")

    # Validate/Clamp RAxML params if method is raxml
    if tree_method == 'raxml':
        try:
            from app.services.raxml_validator import validate_and_resolve_raxml_params
            
            # Use data_type assumption (can't fully detect here without MSA, assume DNA for initial clamp or check sequence?)
            # For now default to DNA validation rules, the worker does final check.
            # But the validator mainly checks Integers which are agnostic.
            resolved = validate_and_resolve_raxml_params(job_params, data_type="DNA")
            
            # overwrite with safe values to ensure what is stored/shown matches execution
            job_params.update({
                "bootstrap_cap": resolved.bootstrap_cap,
                "enable_bootstrap": resolved.enable_bootstrap,
                "seed": resolved.seed,
                "outgroup": resolved.outgroup,
                "moose_enabled": resolved.enable_moose, 
                "early_stopping": resolved.enable_early_stopping
            })
            
            # Store validation warnings separately
            if resolved.warnings:
                job_params['validation_warnings'] = resolved.warnings
                
        except Exception as e:
            logger.error(f"Validation error: {e}")
            # Continue but verify in worker

    # Clamp generic numeric tree params for all methods (iqtree/mrbayes/etc.)
    # so a malicious or careless caller can't pin a worker forever with e.g.
    # mcmc_generations=999_999_999_999. Limits are intentionally generous --
    # larger than any legitimate run we expect.
    def _clamp_int(value, default, lo, hi):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def _clamp_float(value, default, lo, hi):
        import math
        try:
            n = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(n):
            return default
        return max(lo, min(hi, n))

    job_params["bootstrap"] = _clamp_int(job_params.get("bootstrap"), 1000, 0, 10_000)
    job_params["mcmc_generations"] = _clamp_int(
        job_params.get("mcmc_generations"), 50_000, 1_000, 100_000_000
    )
    job_params["mcmc_nruns"] = _clamp_int(job_params.get("mcmc_nruns"), 2, 1, 8)
    job_params["mcmc_nchains"] = _clamp_int(job_params.get("mcmc_nchains"), 4, 1, 16)
    job_params["mcmc_burnin_fraction"] = _clamp_float(
        job_params.get("mcmc_burnin_fraction"), 0.25, 0.0, 0.99
    )
    job_params["sequence"], job_params["sequence_metadata"] = _dedupe_sequence_payload(
        job_params.get("sequence", ""),
        job_params.get("sequence_metadata", []),
    )

    # Reject a malformed FASTA here rather than in the worker. This validation
    # used to live only in run_phylo_job, so a stray character on a sequence
    # line was accepted with 202, queued, and only reported as a failed job
    # once a worker picked it up -- the user waited for a run that could never
    # have started. validate_dna_fasta is pure, so it is safe in-request.
    #
    # Only the pasted path is checked: a fasta_upload lands on disk separately
    # and an accession/MycoMap import has no sequence text yet, both of which
    # the worker still validates once it has the records in hand.
    if job_params.get("sequence"):
        from app.services.fasta_utils import validate_dna_fasta
        try:
            validate_dna_fasta(job_params["sequence"])
        except ValueError as e:
            return jsonify({"status": "error", "error": str(e)}), 400

    try:
        job_id = enqueue_job(job_params)
        
        # Create DB record
        job_record = Job(
            id=job_id,
            status="queued",
            job_dir=str(Config.JOB_DIR / job_id),
            input_type=job_params["input_type"],
            metrics={
                "tree_method": job_params["tree_method"],
                "notes": job_params["notes"],
                "alignment_method": job_params["alignment_method"],
                "trimming_method": job_params["trimming_method"],
                "trim_terminal_overhangs": job_params["trim_terminal_overhangs"],
                "its_region": job_params.get("its_region"),
                "run_preset": job_params.get("run_preset"),
                "bootstrap_cap": job_params.get("bootstrap_cap"),
                # Set by enqueue_job when the submitted set cannot yield an
                # informative tree. Kept on the record so the warning is still
                # there when the user opens the finished job.
                "input_warnings": job_params.get("input_warnings") or [],
            }
        )

        if current_user.is_authenticated:
            job_record.user_id = current_user.id

        db.session.add(job_record)
        db.session.commit()

        response = {"status": "queued", "job_id": job_id}
        if job_params.get("input_warnings"):
            response["warnings"] = job_params["input_warnings"]
        return jsonify(response), 202
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/status', methods=['GET'])
def get_job_status_route(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400
        
    status_info = get_job_status(job_id)
    return jsonify(status_info)

@bp.route('/job/<job_id>/tree/state', methods=['GET'])
def get_tree_state(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400
        
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    try:
        from app.services.tree_edit_service import load_tree_state
        state = load_tree_state(job_dir)
        return jsonify(state)
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/prune', methods=['POST'])
def prune_tree(job_id):
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    data = request.get_json(silent=True) or {}
    
    # Support both single and multiple
    tip_names = data.get("tip_names")
    if not tip_names and data.get("tip_name"):
        tip_names = [data.get("tip_name")]
        
    if not tip_names:
        return jsonify({"status": "error", "error": "No tips specified"}), 400
    
    try:
        from app.services.tree_edit_service import load_tree_state, prune_taxa, save_tree_state, tree_state_lock
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state = prune_taxa(job_dir, state, tip_names)
            save_tree_state(job_dir, state)
        return jsonify(state)
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/rename', methods=['POST'])
def rename_tree_tip(job_id):
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({
            "status": "error",
            "error": "Request body must be a JSON object.",
        }), 400

    try:
        from app.services.tree_edit_service import (
            load_tree_state,
            rename_tip,
            save_tree_state,
            tree_state_lock,
            validate_tip_rename,
        )
    except Exception as e:
        return _server_error(e)

    try:
        old_name, new_name = validate_tip_rename(
            data.get("old_name"), data.get("new_name")
        )
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400

    try:
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state = rename_tip(state, old_name, new_name)
            save_tree_state(job_dir, state)
        return jsonify(state)
    except Exception as e:
        return _server_error(e)


@bp.route('/job/<job_id>/tree/refresh-mycomap-records', methods=['POST'])
@limiter.limit("5 per minute; 50 per hour")
def refresh_tree_mycomap_records(job_id):
    """Refresh selected observation records and persist changed MycoMap tip labels."""
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    data = request.get_json(silent=True) or {}
    tip_names = data.get("tip_names")
    if not isinstance(tip_names, list) or not tip_names:
        return jsonify({
            "status": "error",
            "error": "Select at least one observation-backed tree record.",
        }), 422
    if len(tip_names) > 100:
        return jsonify({
            "status": "error",
            "error": "No more than 100 MycoMap records can be refreshed at once.",
        }), 422

    normalized_tip_names = []
    for value in tip_names:
        if not isinstance(value, str):
            return jsonify({"status": "error", "error": "Invalid tree tip name."}), 422
        name = value.strip()
        if not name or len(name) > 1000 or any(ord(char) < 32 for char in name):
            return jsonify({"status": "error", "error": "Invalid tree tip name."}), 422
        if name not in normalized_tip_names:
            normalized_tip_names.append(name)

    job_dir = Config.JOB_DIR / job_id
    from app.services.mycomap_service import MycoMapRefreshError
    try:
        from app.services.mycomap_service import (
            extract_mycomap_observation_reference,
            refresh_mycomap_observation_records,
        )
        from app.services.tree_edit_service import (
            _tree_tip_set,
            load_tree_state,
            refresh_mycomap_tip_labels,
            save_tree_state,
            tree_state_lock,
        )

        state = load_tree_state(job_dir)
        available_tips = _tree_tip_set(state)
        if any(name not in available_tips for name in normalized_tip_names):
            return jsonify({
                "status": "error",
                "error": "One or more selected tree records are no longer available.",
            }), 409

        renames = state.get("renames") or {}
        references = []
        for name in normalized_tip_names:
            reference = (
                extract_mycomap_observation_reference(name)
                or extract_mycomap_observation_reference(renames.get(name))
            )
            if reference and reference not in references:
                references.append(reference)
        if not references:
            return jsonify({
                "status": "error",
                "error": "The selected records do not contain iNaturalist or Mushroom Observer numbers.",
            }), 422

        refresh_result = refresh_mycomap_observation_records(references)
        with tree_state_lock(job_dir):
            # The remote lookup can take several seconds, so reload only at
            # commit time under the shared edit lock. This avoids either
            # holding the lock across network I/O or saving the stale snapshot
            # that was used to resolve observation references above.
            state = load_tree_state(job_dir)
            available_tips = _tree_tip_set(state)
            if any(name not in available_tips for name in normalized_tip_names):
                return jsonify({
                    "status": "error",
                    "error": "One or more selected tree records are no longer available.",
                }), 409
            label_result = refresh_mycomap_tip_labels(
                state,
                normalized_tip_names,
                refresh_result,
            )
            changes = label_result["changes"]
            if changes:
                save_tree_state(job_dir, label_result["tree_state"])
        return jsonify({
            "status": "success",
            "refreshed_count": len(references),
            "updated_tip_count": len(changes),
            "changes": changes,
            "warnings": label_result["warnings"],
            "message": refresh_result.get("message") or "",
        })
    except MycoMapRefreshError as exc:
        logger.warning("MycoMap tree-record refresh failed for job %s: %s", job_id, exc)
        return jsonify({"status": "error", "error": str(exc)}), 502
    except Exception as exc:
        return _server_error(exc, where="refresh_tree_mycomap_records")

@bp.route('/job/<job_id>/tree/rotate', methods=['POST'])
def rotate_tree_node(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")
    if not isinstance(node_id, str) or not node_id.strip():
        return jsonify({"status": "error", "error": "Missing node_id"}), 400
    node_id = node_id.strip()
    if len(node_id) > 256 or any(ord(char) < 32 for char in node_id):
        return jsonify({"status": "error", "error": "Invalid node_id"}), 400

    try:
        from app.services.tree_edit_service import load_tree_state, rotate_node, save_tree_state, tree_state_lock
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state = rotate_node(job_dir, state, node_id)
            save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/reroot', methods=['POST'])
def reroot_tree_endpoint(job_id):
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    data = request.get_json(silent=True) or {}
    target = data.get("root_target") or data.get("target") or data.get("node_name")

    if not target:
        return jsonify({"status": "error", "error": "Missing root_target"}), 400
    
    try:
        from app.services.tree_edit_service import load_tree_state, reroot_tree, save_tree_state, tree_state_lock
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state = reroot_tree(job_dir, state, target)
            save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/midpoint_root', methods=['POST'])
def midpoint_root_endpoint(job_id):
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    try:
        from app.services.tree_edit_service import load_tree_state, midpoint_root, save_tree_state, tree_state_lock
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state = midpoint_root(job_dir, state)
            save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/midpoint_root_toggle', methods=['POST'])
def midpoint_root_toggle_endpoint(job_id):
    """Toggle midpoint rooting on/off."""
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    try:
        from app.services.tree_edit_service import (
            load_tree_state, midpoint_root, undo_midpoint_root, save_tree_state,
            tree_state_lock,
        )
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)

            # Check current state and toggle
            if state.get("is_midpoint_rooted", False):
                # Currently midpoint rooted - undo it
                state = undo_midpoint_root(job_dir, state)
            else:
                # Not midpoint rooted - apply it
                state = midpoint_root(job_dir, state)

            save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/rooting_mode', methods=['POST'])
def set_rooting_mode_endpoint(job_id):
    """Apply a rooting mode: auto | midpoint | most_divergent_hit | unrooted | manual."""
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    # Alan 6/2/26 - Treat missing/null/empty mode as "auto"; reject a non-string mode with a
    # clean 400 instead of letting (mode or "auto").lower() raise AttributeError -> 500.
    raw_mode = data.get("mode")
    if raw_mode is None:
        mode = "auto"
    elif isinstance(raw_mode, str):
        mode = raw_mode.strip().lower() or "auto"
    else:
        return jsonify({"status": "error", "error": "Invalid rooting mode"}), 400
    target = data.get("target")
    soi = data.get("sequence_of_interest")

    if mode not in ("auto", "midpoint", "most_divergent_hit", "unrooted", "manual"):
        return jsonify({"status": "error", "error": f"Unknown rooting mode: {mode}"}), 400

    try:
        from app.services.tree_edit_service import (
            load_tree_state, save_tree_state, apply_rooting_mode, set_sequence_of_interest,
            tree_state_lock,
        )
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            if soi:
                state = set_sequence_of_interest(state, soi, source="user_selected")
            state = apply_rooting_mode(job_dir, state, mode, target=target,
                                       sequence_of_interest=soi)
            save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e)


@bp.route('/job/<job_id>/tree/sequence_of_interest', methods=['POST'])
def set_sequence_of_interest_endpoint(job_id):
    """Set or clear the persisted focal/sequence-of-interest tip."""
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    data = request.get_json(silent=True) or {}
    tip_name = data.get("tip_name")
    source = data.get("source", "user_selected")

    try:
        from app.services.tree_edit_service import (
            load_tree_state, save_tree_state, set_sequence_of_interest,
            tree_state_lock,
        )
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state = set_sequence_of_interest(state, tip_name, source=source)
            save_tree_state(job_dir, state)
        return jsonify({
            "status": "ok",
            "sequence_of_interest": state.get("sequence_of_interest"),
            "sequence_of_interest_source": state.get("sequence_of_interest_source"),
        })
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return _server_error(e)


@bp.route('/job/<job_id>/tree/selection_sets', methods=['POST'])
def save_selection_sets(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404

    data = request.get_json(silent=True) or {}
    sets = data.get("sets")
    active = data.get("active", "Default")
    colors = data.get("colors", {})
    if sets is None or not isinstance(sets, dict):
        return jsonify({"status": "error", "error": "Missing or invalid 'sets' field"}), 400
    if colors is not None and not isinstance(colors, dict):
        return jsonify({"status": "error", "error": "Invalid 'colors' field"}), 400

    try:
        from app.services.tree_edit_service import load_tree_state, save_tree_state, tree_state_lock
        with tree_state_lock(job_dir):
            state = load_tree_state(job_dir)
            state["selection_sets"] = sets
            state["active_selection_set"] = active
            state["selection_set_colors"] = colors or {}
            save_tree_state(job_dir, state)
        return jsonify({"status": "ok"})
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/annotations', methods=['POST'])
def save_clade_annotations(job_id):
    """Replace this job's clade-annotation configuration in one atomic save.

    The whole configuration is submitted together: partial updates would let a
    rejected layer leave the annotations referencing it stranded. Everything
    else in tree_state.json is preserved, and last-write-wins across tabs.
    """
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({
            "status": "error",
            "error": "Request body must be a valid JSON object",
        }), 400

    try:
        from app.services.tree_edit_service import load_tree_state, save_tree_state, tree_state_lock
        from app.services.tree_annotation_service import (
            ANNOTATION_LAYERS_KEY,
            AnnotationValidationError,
            CLADE_ANNOTATIONS_KEY,
            apply_annotation_config,
            normalize_annotation_config,
        )

        with tree_state_lock(job_dir):
            # Load and commit under the same per-job lock. This preserves a
            # prune/reroot/rename that finishes while an annotation request is
            # in flight instead of writing the request's stale whole snapshot.
            state = load_tree_state(job_dir)
            try:
                config = normalize_annotation_config(state, data)
            except AnnotationValidationError as exc:
                # Nothing has been mutated at this point, so the previously saved
                # configuration is still intact on disk.
                return jsonify({"status": "error", "error": str(exc)}), 400

            apply_annotation_config(state, config)
            save_tree_state(job_dir, state)
        return jsonify({
            "status": "ok",
            "layers": config[ANNOTATION_LAYERS_KEY],
            "annotations": config[CLADE_ANNOTATIONS_KEY],
        })
    except Exception as e:
        return _server_error(e, where="save_clade_annotations")


@bp.route('/job/<job_id>/rebuild-with-duplicates', methods=['POST'])
@limiter.limit("6 per minute")
def rebuild_with_duplicates(job_id):
    """Start a NEW job from this job's sequences with the removed duplicates added back.

    Deliberately creates a separate job rather than recomputing in place, so the
    original tree keeps its URL and stays citable.
    """
    db_job, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    try:
        import json as _json
        input_info_path = Config.JOB_DIR / job_id / "input_info.json"
        if not input_info_path.exists():
            return jsonify({"status": "error", "error": "Original job inputs are no longer on disk"}), 404

        with open(input_info_path, "r") as f:
            source_params = _json.load(f)

        duplicates = (source_params.get("import_filter_details") or {}).get("duplicates") or {}
        removed = duplicates.get("removed_records") or []
        if not removed:
            return jsonify({"status": "error", "error": "This job has no removed duplicates to restore"}), 400

        restored = [r for r in removed if str(r.get("sequence") or "").strip()]
        if not restored:
            return jsonify({
                "status": "error",
                "error": "Removed duplicates were recorded without their sequences, so they cannot be restored"
            }), 400

        job_params = dict(source_params)
        # Append the removed records back onto the FASTA payload.
        sequence_text = str(job_params.get("sequence") or "").rstrip("\n")
        blocks = [sequence_text] if sequence_text else []
        sequence_metadata = list(job_params.get("sequence_metadata") or [])
        existing_records = _parse_fasta_sequences(sequence_text)
        used_ids = {
            str(record.get("name") or "").strip().split(None, 1)[0]
            for record in existing_records
            if str(record.get("name") or "").strip()
        }

        for record in restored:
            sequence = "".join(str(record.get("sequence") or "").split())
            wrapped = "\n".join(sequence[i:i + 80] for i in range(0, len(sequence), 80))
            original_header = str(record.get("name") or "").strip()
            parts = original_header.split(None, 1) if original_header else ["seq"]
            base_id = parts[0] or "seq"
            description = parts[1] if len(parts) > 1 else ""
            internal_id = base_id
            suffix = 2
            while internal_id in used_ids:
                internal_id = f"{base_id}_{suffix}"
                suffix += 1
            used_ids.add(internal_id)
            internal_header = f"{internal_id} {description}".rstrip()
            blocks.append(f">{internal_header}\n{wrapped}")

            restored_metadata = dict(record.get("metadata") or {})
            restored_metadata.setdefault(
                "display_label",
                restored_metadata.get("name") or original_header,
            )
            restored_metadata.setdefault("raw_fasta_header", original_header)
            # FASTA/header keyed consumers need the unique tool-facing name;
            # display_label/raw_fasta_header retain the original occurrence.
            restored_metadata["name"] = internal_header
            restored_metadata["fasta_header"] = internal_header
            sequence_metadata.append(restored_metadata)
        combined_fasta = "\n".join(blocks) + "\n"
        combined_records = _parse_fasta_sequences(combined_fasta)
        from app.workers.tasks import uniquify_fasta_identifiers

        unique_fasta, _unique_stats = uniquify_fasta_identifiers(combined_fasta)
        unique_records = _parse_fasta_sequences(unique_fasta)

        positional_metadata = (
            len(sequence_metadata) == len(combined_records)
            and all(
                str(sequence_metadata[index].get("fasta_header")
                    or sequence_metadata[index].get("name") or "").strip()
                == str(record.get("name") or "").strip()
                for index, record in enumerate(combined_records)
            )
        )
        metadata_by_header = {}
        if not positional_metadata:
            for item in sequence_metadata:
                key = str(item.get("fasta_header") or item.get("name") or "").strip()
                metadata_by_header.setdefault(key, []).append(item)

        updated_metadata = []
        for index, (before, after) in enumerate(zip(combined_records, unique_records)):
            original_header = str(before.get("name") or "").strip()
            internal_header = str(after.get("name") or "").strip()
            if positional_metadata:
                item = dict(sequence_metadata[index])
            else:
                candidates = metadata_by_header.get(original_header) or []
                item = dict(candidates.pop(0)) if candidates else {}
            item.setdefault("display_label", item.get("name") or original_header)
            item.setdefault("raw_fasta_header", original_header)
            item["name"] = internal_header
            item["fasta_header"] = internal_header
            updated_metadata.append(item)

        job_params["sequence"] = unique_fasta
        job_params["sequence_metadata"] = updated_metadata

        # Without this the dedup in enqueue_job would immediately strip them again.
        job_params["skip_observation_dedup"] = True
        # Internal-only pipeline flag: the worker must uniquify identifiers but
        # must not collapse the exact records this action explicitly restores.
        job_params["preserve_exact_duplicate_records"] = True
        job_params["import_filter_details"] = {
            k: v for k, v in (job_params.get("import_filter_details") or {}).items()
            if k != "duplicates"
        }
        job_params["notes"] = (
            f"Rebuild of {job_id} including {len(restored)} duplicate "
            f"observation record{'' if len(restored) == 1 else 's'}"
        )
        job_params["rebuilt_from_job_id"] = job_id

        new_job_id = enqueue_job(job_params)
        new_record = Job(
            id=new_job_id,
            status="queued",
            job_dir=str(Config.JOB_DIR / new_job_id),
            input_type=job_params.get("input_type", "sequence"),
            metrics={
                "tree_method": job_params.get("tree_method"),
                "notes": job_params.get("notes"),
                "alignment_method": job_params.get("alignment_method"),
                "trimming_method": job_params.get("trimming_method"),
                "rebuilt_from_job_id": job_id,
            },
        )
        if db_job is not None and db_job.user_id:
            new_record.user_id = db_job.user_id
        elif current_user.is_authenticated:
            new_record.user_id = current_user.id
        db.session.add(new_record)
        db.session.commit()

        return jsonify({
            "status": "queued",
            "job_id": new_job_id,
            "restored_count": len(restored),
            "view_url": f"/job/{new_job_id}/view",
            "status_url": f"/job/{new_job_id}",
        }), 202
    except Exception as e:
        return _server_error(e)


@bp.route('/job/<job_id>/params', methods=['GET'])
def get_job_pipeline_params(job_id):
    """Return the pipeline settings a job ran with.

    Used by the Add Sequences page to open its Advanced panel pre-filled with
    the job's real settings instead of the form defaults, so "Add & Recompute"
    doesn't quietly change the analysis.
    """
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    import json as _json
    input_info_path = Config.JOB_DIR / job_id / "input_info.json"
    if not input_info_path.exists():
        return jsonify({"status": "error", "error": "Job inputs are no longer on disk"}), 404

    try:
        with open(input_info_path, "r") as f:
            stored = _json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read params for job {job_id}: {e}")
        return jsonify({"status": "error", "error": "Job parameters could not be read"}), 500

    params = {key: stored.get(key) for key in RECOMPUTE_READABLE_FIELDS if key in stored}

    # The requested model is often not the one that was fit -- IQ-TREE's "MFP"
    # defers to ModelFinder, and RAxML with MOOSE enabled substitutes its own
    # pick. Report the fitted model, and who chose it, rather than leaving the
    # user guessing.
    selected_model = None
    model_selector = None
    metadata_path = Config.JOB_DIR / job_id / "tree" / "tree_metadata.json"
    if metadata_path.exists():
        try:
            with open(metadata_path, "r") as f:
                metadata = _json.load(f) or {}
            selected_model = metadata.get("model_selected")
            model_selector = metadata.get("model_selector")
        except (OSError, ValueError):
            pass

    return jsonify({
        "status": "success",
        "params": params,
        "model_selected": selected_model,
        "model_selector": model_selector,
    })


@bp.route('/job/<job_id>/tree/recompute', methods=['POST'])
# Alan 8/14/26 - 1/min blocked a legitimate second attempt (adjust settings, re-run).
# Still tight because recompute runs MAFFT/FastTree inside the web request.
@limiter.limit("6 per minute; 60 per hour")
def recompute_tree_job(job_id):
    db_job, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    try:
        import json
        # Load original params
        input_info_path = job_dir / "input_info.json"
        params_dict = {}
        if input_info_path.exists():
            with open(input_info_path, "r") as f:
                params_dict = json.load(f)
        
        # Merge with request data. Only pipeline settings may be overridden --
        # the request must not be able to rewrite the stored sequences, the
        # import provenance, or the trimming report by posting those keys.
        req_data = request.get_json(silent=True) or {}
        overrides = {
            key: value for key, value in req_data.items()
            if key in RECOMPUTE_OVERRIDABLE_FIELDS
        }
        if overrides:
            method = str(overrides.get("tree_method", params_dict.get("tree_method", "")) or "").lower()
            if "tree_method" in overrides and method not in VALID_TREE_METHODS:
                return jsonify({
                    "status": "error",
                    "error": f"Unsupported tree method. Choose one of: {', '.join(sorted(VALID_TREE_METHODS))}"
                }), 400
            params_dict.update(overrides)
        persisted_params = dict(params_dict)

        # Per-request control flags, not pipeline settings. Applied after the
        # write above so they are not persisted into the job's stored params,
        # and passed through params_dict because that is where both the sync
        # call below and run_recompute_job read them from.
        if "use_current_input" in req_data:
            params_dict["use_current_input"] = req_data["use_current_input"]

        # Recompute can take hours. It must never run inside one of Gunicorn's
        # eight request slots: a routine web restart would SIGTERM the tool and
        # two concurrent requests could write the same output paths. Queue every
        # request and make unchanged duplicate clicks idempotent.
        recompute_job_id, created = enqueue_recompute_job(
            job_id, params_dict, return_created=True,
        )
        mutation_requested = bool(overrides) or "use_current_input" in req_data
        if not created and not mutation_requested:
            # The viewer's Recompute button posts {"async": true} and nothing
            # else: the pruning it means to apply lives in tree_state.json,
            # which the running task snapshotted at its step 1. Prune three more
            # taxa and click again and the body still looks like a duplicate,
            # so the request was swallowed and the finished tree still carried
            # the three taxa with nothing on screen saying why.
            snapshot = active_recompute_snapshot_mtime(job_id)
            state_path = job_dir / "tree_state.json"
            if snapshot is not None:
                try:
                    mutation_requested = state_path.stat().st_mtime > snapshot
                except OSError:
                    mutation_requested = False
        if not created and mutation_requested:
            # Add & Recompute saves input_raw.fasta before it reaches this
            # endpoint. An already-running task may already have copied its
            # input into staging, so accepting this as an idempotent duplicate
            # would make the saved queue/settings disagree with the resulting
            # tree. Leave the new input in place and make the caller retry once
            # the active generation finishes.
            return jsonify({
                "status": "conflict",
                "error": (
                    "Another recompute is already in progress and will not include "
                    "your newest changes. Wait for it to finish, then recompute "
                    "again."
                ),
                "job_id": job_id,
                "rq_job_id": recompute_job_id,
                "redirect_url": url_for('main.job_status', job_id=job_id),
            }), 409
        if created:
            # Only the request that actually created this run may update its
            # reported settings. A duplicate request cannot alter the params
            # already captured by the active RQ task.
            if overrides:
                try:
                    with open(input_info_path, "w") as f:
                        json.dump(persisted_params, f, separators=(",", ":"))
                except OSError as write_err:
                    logger.warning(
                        f"Could not persist recompute overrides for job {job_id}: {write_err}"
                    )
            if db_job:
                metrics = dict(db_job.metrics or {})
                metrics["recompute_requested_at"] = datetime.utcnow().isoformat()
                db_job.metrics = metrics
                db_job.status = "queued"
                db.session.commit()
        return jsonify({
            "status": "queued" if created else "already_queued",
            "job_id": job_id,
            "rq_job_id": recompute_job_id,
            "redirect_url": url_for('main.job_status', job_id=job_id),
            "message": (
                "Tree recompute queued."
                if created else "A recompute for this tree is already in progress."
            ),
        }), 202
        
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/download/tree/newick', methods=['GET'])
def download_newick(job_id):
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    # Prefer pruned tree if available, else initialize from original
    pruned_path = job_dir / "tree" / "tree_pruned.newick"
    
    if not pruned_path.exists():
        try:
            from app.services.tree_edit_service import initialize_tree
            pruned_path = initialize_tree(job_dir)
        except Exception as e:
            logger.error(f"Failed to auto-init tree: {e}")
            # Fallback to original if initialization fails
            pruned_path = job_dir / "tree" / "tree_original.newick"
    
    path = pruned_path
    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "Tree file not found or invalid"}), 404
        
    response = send_file(path, as_attachment=True, download_name="tree.newick")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@bp.route('/job/<job_id>/download/tree/newick/original', methods=['GET'])
def download_newick_original(job_id):
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    path = job_dir / "tree" / "tree_original.newick"
    logger.info(f"Serving original newick from: {path}, Exists: {path.exists()}")
    if not validate_safe_file_path(path, job_dir):
        logger.error(f"File not found or unsafe: {path}")
        return jsonify({"status": "error", "error": "Tree file not found or invalid"}), 404
        
    return send_file(path, as_attachment=True, download_name="tree_original.newick")

@bp.route('/job/<job_id>/download/tree/newick/pruned', methods=['GET'])
def download_newick_pruned(job_id):
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    path = job_dir / "tree" / "tree_pruned.newick"
    
    if not validate_safe_file_path(path, job_dir):
         return jsonify({"status": "error", "error": "Tree file not found or invalid"}), 404

    response = send_file(path, as_attachment=True, download_name="tree_pruned.newick")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@bp.route('/job/<job_id>/download/tree/nexus', methods=['GET'])
def download_nexus(job_id):
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    pruned_path = job_dir / "tree" / "tree_pruned.nexus"
    original_path = job_dir / "tree" / "tree_original.nexus"
    
    path = pruned_path if pruned_path.exists() else original_path
    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "Tree file not found or invalid"}), 404
        
    response = send_file(path, as_attachment=True, download_name="tree.nexus")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@bp.route('/job/<job_id>/download/mrbayes', methods=['GET'])
def download_mrbayes_files(job_id):
    """Download the MrBayes command file and raw convergence artifacts."""
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    job_dir = Config.JOB_DIR / job_id
    tree_dir = job_dir / "tree"
    files = []
    if tree_dir.exists() and tree_dir.is_dir():
        files = [
            path for path in sorted(tree_dir.glob("mrbayes_input.nex*"))
            if not path.name.endswith("~") and validate_safe_file_path(path, job_dir)
        ]
    if not files:
        return jsonify({
            "status": "error",
            "error": "MrBayes analysis files not found"
        }), 404

    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        for path in files:
            zip_file.write(path, arcname=path.name)
    archive.seek(0)

    response = send_file(
        archive,
        as_attachment=True,
        download_name="mrbayes_analysis_files.zip",
        mimetype="application/zip",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@bp.route('/job/<job_id>/download/fasta/original', methods=['GET'])
def download_fasta_original(job_id):
    # Check authorization
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code
    
    job_dir = Config.JOB_DIR / job_id
    path = job_dir / "input" / "input_raw.fasta"
    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "FASTA file not found or invalid"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_original.fasta")

@bp.route('/job/<job_id>/download/fasta/edited', methods=['GET'])
def download_fasta_edited(job_id):
    """Download the current unaligned tree input with the tree viewer's pruning
    and renaming applied. Built fresh from the persisted tree
    state on every request so it can never serve a stale export."""
    from io import BytesIO

    # Check authorization
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    input_path = job_dir / "input" / "input_raw.fasta"
    if not validate_safe_file_path(input_path, job_dir):
        return jsonify({"status": "error", "error": "FASTA file not found or invalid"}), 404

    try:
        from app.services.tree_edit_service import build_edited_fasta_text, load_tree_state
        state = load_tree_state(job_dir)
        content = build_edited_fasta_text(input_path, state)
    except Exception as e:
        logger.warning(f"Failed to build edited FASTA for job {job_id}: {e}")
        return jsonify({"status": "error", "error": "Could not build edited FASTA"}), 500

    if not content.strip():
        return jsonify({
            "status": "error",
            "error": "No sequences remain after the current tree edits"
        }), 404

    response = send_file(
        BytesIO(content.encode("utf-8")),
        as_attachment=True,
        download_name="sequences_edited.fasta",
        mimetype="text/plain",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@bp.route('/job/<job_id>/download/fasta/pruned', methods=['GET'])
def download_fasta_pruned(job_id):
    """Legacy prune-only export, kept for old links and for the add-sequences flow.

    Deliberately NOT an alias for the edited export: /tree?edit=<job> loads this
    FASTA into the queue and can hand it straight back to /sequences/add, which
    rewrites input_raw.fasta. Renamed headers here would replace the original
    names that tree_state's renames/pruned_taxa are keyed on, so recomputation
    keeps getting original identifiers.
    """
    # Check authorization
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    path = job_dir / "alignment" / "alignment_pruned.fasta"
    try:
        from app.services.tree_edit_service import load_tree_state, extract_pruned_fasta
        state = load_tree_state(job_dir)
        input_path = job_dir / "input" / "input_raw.fasta"
        if state.get("pruned_taxa") and validate_safe_file_path(input_path, job_dir):
            if path.parent.exists() and (path.parent.is_symlink() or not path.parent.is_dir()):
                raise RuntimeError("Pruned FASTA directory is unsafe")
            if path.exists() and (path.is_symlink() or not path.is_file()):
                raise RuntimeError("Pruned FASTA path is unsafe")
            path.parent.mkdir(parents=True, exist_ok=True)
            extract_pruned_fasta(input_path, state, path)
    except Exception as e:
        logger.warning(f"Failed to refresh pruned FASTA for job {job_id}: {e}")

    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "Pruned FASTA not found or invalid"}), 404

    return send_file(path, as_attachment=True, download_name="sequences_pruned.fasta")

@bp.route('/job/<job_id>/download/fasta/aligned', methods=['GET'])
def download_fasta_aligned(job_id):
    """Download the aligned (but not trimmed) FASTA file."""
    # Check authorization
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code
    
    job_dir = Config.JOB_DIR / job_id

    # Return the raw alignment (before trimming)
    path = job_dir / "alignment" / "alignment_raw.fasta"
    if not artifact_exists(path):
        path = job_dir / "alignment" / "aligned.fasta"

    stored = resolve_artifact(path)
    if stored is None or not validate_safe_file_path(stored, job_dir):
        return jsonify({"status": "error", "error": "Aligned FASTA not found or invalid"}), 404

    if stored != path:
        # Stored gzipped: hand the client the plain FASTA it asked for rather
        # than a .gz they did not, since this is an attachment download.
        from io import BytesIO

        return send_file(
            BytesIO(read_artifact_bytes(path)),
            as_attachment=True,
            download_name="sequences_aligned.fasta",
            mimetype="text/plain",
        )

    return send_file(path, as_attachment=True, download_name="sequences_aligned.fasta")

@bp.route('/job/<job_id>/alignment/view', methods=['POST'])
def alignment_view(job_id):
    """Return parsed aligned FASTA contents for the in-page Alignment Viewer.

    Inputs (JSON): tip_names, tree_order, include_pruned, pruned_tip_names.
    Returns sequence rows constrained to selected/visible tree tips, with the
    option to include pruned sequences from the underlying alignment file.
    """
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job id"}), 400

    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    try:
        from Bio import SeqIO  # local import keeps cold-path cheap
    except ImportError:
        return jsonify({"status": "error", "error": "BioPython unavailable"}), 500

    data = request.get_json(silent=True) or {}
    tip_names = data.get("tip_names") or []
    tree_order = data.get("tree_order") or []
    include_pruned = bool(data.get("include_pruned"))
    if not isinstance(tip_names, list) or not isinstance(tree_order, list):
        return jsonify({"status": "error", "error": "Malformed request"}), 400

    # Soft caps so a runaway payload can't blow up the server.
    MAX_NAMES = 5000
    MAX_RETURN = 2000
    MAX_TOTAL_CHARS = 5_000_000
    tip_names = [str(n) for n in tip_names[:MAX_NAMES] if n]
    tree_order = [str(n) for n in tree_order[:MAX_NAMES] if n]

    job_dir = Config.JOB_DIR / job_id
    alignment_dir = job_dir / "alignment"
    alignment_candidates = (
        alignment_dir / "alignment_pruned_aligned.fasta",
        alignment_dir / "alignment_raw.fasta",
        alignment_dir / "aligned.fasta",
    )
    path = next((candidate for candidate in alignment_candidates if artifact_exists(candidate)), None)
    stored = resolve_artifact(path) if path is not None else None
    if stored is None:
        return jsonify({"status": "error", "error": "Aligned FASTA not found"}), 404
    if not validate_safe_file_path(stored, job_dir):
        return jsonify({"status": "error", "error": "Aligned FASTA not found"}), 404

    try:
        with open_artifact(path, "rt") as handle:
            records = list(SeqIO.parse(handle, "fasta"))
    except Exception as exc:  # pragma: no cover - defensive
        return _server_error(exc, where="alignment_view parse")

    if not records:
        return jsonify({"status": "error", "error": "Alignment is empty"}), 404

    # Build lookup tables for robust name matching.
    by_full = {}
    by_trimmed = {}
    by_token = {}
    fasta_rows = []
    for rec in records:
        header = rec.description or rec.id
        sequence = str(rec.seq)
        row = {"name": header, "sequence": sequence}
        fasta_rows.append(row)
        by_full.setdefault(header, row)
        trimmed = header.strip()
        by_trimmed.setdefault(trimmed, row)
        first_token = trimmed.split(None, 1)[0] if trimmed else ""
        if first_token:
            by_token.setdefault(first_token, []).append(row)

    rename_aliases = {}
    try:
        from app.services.tree_edit_service import load_tree_state
        renames = load_tree_state(job_dir).get("renames") or {}
        if isinstance(renames, dict):
            for original_name, display_name in renames.items():
                if not isinstance(original_name, str) or not isinstance(display_name, str):
                    continue
                display_name = display_name.strip()
                if not display_name:
                    continue
                existing = rename_aliases.get(display_name)
                rename_aliases[display_name] = (
                    original_name if existing in (None, original_name) else False
                )
    except Exception as exc:
        logger.warning("Could not load rename aliases for alignment view %s: %s", job_id, exc)

    def match_exact_alignment_name(name):
        if not name:
            return None
        if name in by_full:
            return by_full[name]
        t = name.strip()
        if t in by_trimmed:
            return by_trimmed[t]
        return None

    def match_alignment_token(name):
        if not name:
            return None
        t = name.strip()
        tok = t.split(None, 1)[0] if t else ""
        hits = by_token.get(tok, [])
        if len(hits) == 1:
            return hits[0]
        return None

    def match_alignment_name(name):
        return match_exact_alignment_name(name) or match_alignment_token(name)

    def match(name):
        row = match_exact_alignment_name(name)
        if row is not None:
            return row
        original_name = rename_aliases.get(str(name).strip())
        if original_name:
            row = match_alignment_name(original_name)
            if row is not None:
                return row
        return match_alignment_token(name)

    warnings = []
    alignment_length = max((len(r["sequence"]) for r in fasta_rows), default=0)
    total_alignment_count = len(fasta_rows)

    # Determine which alignment rows correspond to current tree tips.
    tree_set_rows = []
    tree_seen = set()
    for name in tree_order:
        row = match(name)
        if row is None or id(row) in tree_seen:
            continue
        tree_seen.add(id(row))
        tree_set_rows.append(row)

    pruned_rows = [r for r in fasta_rows if id(r) not in tree_seen]
    available_pruned_count = len(pruned_rows)

    # Build the ordered selection of rows to return.
    selected_rows = []
    seen_ids = set()

    def push(row):
        if row is None or id(row) in seen_ids:
            return
        seen_ids.add(id(row))
        selected_rows.append(row)

    if include_pruned:
        # Start with tree order (or selected subset if provided), then append remaining.
        if tip_names:
            for name in tip_names:
                row = match(name)
                if row is None:
                    warnings.append(f"Not found in alignment: {name}")
                    continue
                push(row)
            for r in tree_set_rows:
                push(r)
        else:
            for r in tree_set_rows:
                push(r)
        for r in pruned_rows:
            push(r)
        # Also include any record not yet covered (e.g., tree_order empty).
        for r in fasta_rows:
            push(r)
        included_pruned_count = sum(1 for r in selected_rows if id(r) not in tree_seen)
    else:
        if tip_names:
            for name in tip_names:
                row = match(name)
                if row is None:
                    warnings.append(f"Not found in alignment: {name}")
                    continue
                # exclude pruned by default
                if id(row) not in tree_seen and tree_seen:
                    continue
                push(row)
        else:
            for r in tree_set_rows:
                push(r)
            if not selected_rows and not tree_order:
                # No tree order provided: fall back to whole alignment.
                for r in fasta_rows:
                    push(r)
        included_pruned_count = 0

    # Apply defensive caps.
    capped_warning = None
    if len(selected_rows) > MAX_RETURN:
        capped_warning = f"Truncated to {MAX_RETURN} sequences for display."
        selected_rows = selected_rows[:MAX_RETURN]

    total_chars = sum(len(r["sequence"]) for r in selected_rows)
    if total_chars > MAX_TOTAL_CHARS:
        # Trim rows until under the cap.
        running = 0
        trimmed = []
        for r in selected_rows:
            running += len(r["sequence"])
            if running > MAX_TOTAL_CHARS:
                break
            trimmed.append(r)
        capped_warning = f"Truncated to {len(trimmed)} sequences to stay within size limits."
        selected_rows = trimmed

    if capped_warning:
        warnings.append(capped_warning)

    if not selected_rows:
        return jsonify({
            "status": "error",
            "error": "No sequences matched the requested tips.",
            "warnings": warnings,
        }), 404

    return jsonify({
        "status": "success",
        "source": "aligned",
        "total_alignment_count": total_alignment_count,
        "returned_count": len(selected_rows),
        "alignment_length": alignment_length,
        "included_pruned_count": included_pruned_count,
        "available_pruned_count": available_pruned_count,
        "sequences": selected_rows,
        "warnings": warnings,
    })


@bp.route('/job/<job_id>/download/fasta/trimmed', methods=['GET'])
def download_fasta_trimmed(job_id):
    """Download the trimmed FASTA file (only available if trimming was performed)."""
    # Check authorization
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code
    
    job_dir = Config.JOB_DIR / job_id
    path = job_dir / "alignment" / "alignment_trimmed.fasta"

    stored = resolve_artifact(path)
    if stored is None or not validate_safe_file_path(stored, job_dir):
        return jsonify({"status": "error", "error": "Trimmed FASTA not found or invalid"}), 404

    if stored != path:
        from io import BytesIO

        return send_file(
            BytesIO(read_artifact_bytes(path)),
            as_attachment=True,
            download_name="sequences_trimmed.fasta",
            mimetype="text/plain",
        )

    return send_file(path, as_attachment=True, download_name="sequences_trimmed.fasta")


@bp.route('/job/<job_id>/download/alignment/inspection', methods=['GET'])
def download_alignment_inspection(job_id):
    """Download before/after alignments plus the trimmer's marked HTML report."""
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    import json
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    job_dir = Config.JOB_DIR / job_id
    raw_path = job_dir / "alignment" / "alignment_raw.fasta"
    if not artifact_exists(raw_path):
        raw_path = job_dir / "alignment" / "aligned.fasta"
    trimmed_path = job_dir / "alignment" / "alignment_trimmed.fasta"
    report_path = job_dir / "alignment" / "alignment_trimmed_report.html"

    # Any of these may be stored gzipped (see app/services/artifact_storage.py).
    # Resolve to whichever form is on disk first, then run the usual symlink /
    # traversal check against that real path.
    resolved = [resolve_artifact(path) for path in (raw_path, trimmed_path, report_path)]
    if not all(
        path is not None and validate_safe_file_path(path, job_dir)
        for path in resolved
    ):
        return jsonify({
            "status": "error",
            "error": "A marked trimming report is not available for this job",
        }), 404

    job_details = {}
    input_info_path = job_dir / "input_info.json"
    if validate_safe_file_path(input_info_path, job_dir):
        try:
            with open(input_info_path, "r") as handle:
                job_details = json.load(handle)
        except (OSError, ValueError, TypeError):
            logger.warning("Could not read trimming details for job %s", job_id)

    trimming_details = job_details.get("trimming_details") or {}
    terminal_details = trimming_details.get("terminal_overhang_trim") or {}
    method = str(trimming_details.get("method") or job_details.get("trimming_method") or "unknown")
    readme_lines = [
        "Dikarya alignment trimming inspection bundle",
        "",
        f"Trimming algorithm: {method}",
        "",
        "alignment_before_trimming.fasta",
        "  The complete aligned FASTA before any trimming.",
        "",
        "alignment_after_trimming.fasta",
        "  The final alignment supplied to the tree builder.",
        "",
        "trimming_report.html",
        "  The trimming algorithm's colored view of retained and removed columns.",
    ]
    if terminal_details.get("enabled"):
        ranges = terminal_details.get("removed_ranges") or []
        range_text = ", ".join(
            f"{item.get('start')}-{item.get('end')}"
            for item in ranges
            if item.get("start") is not None and item.get("end") is not None
        ) or "none"
        readme_lines.extend([
            "",
            "Terminal-overhang stage:",
            f"  Removed original alignment columns (1-based): {range_text}",
            "  The HTML report uses the alignment after this terminal-overhang stage as its input.",
        ])

    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        # writestr rather than write: these artifacts may be gzipped at rest,
        # and the bundle must always contain the plain text the names promise.
        zip_file.writestr("alignment_before_trimming.fasta", read_artifact_bytes(raw_path))
        zip_file.writestr("alignment_after_trimming.fasta", read_artifact_bytes(trimmed_path))
        zip_file.writestr("trimming_report.html", read_artifact_bytes(report_path))
        zip_file.writestr("README.txt", "\n".join(readme_lines) + "\n")
    archive.seek(0)

    response = send_file(
        archive,
        as_attachment=True,
        download_name="alignment_trimming_inspection.zip",
        mimetype="application/zip",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@bp.route('/job/<job_id>/analysis/review', methods=['POST'])
@limiter.limit("4 per minute; 40 per hour")
def claude_review(job_id):
    """Have Claude assess this job's alignment and tree and report back.

    The numbers are computed here (see tree_analysis_service) and only the
    summary is sent to the API, so the response time depends on the model rather
    than on how large the alignment is. A review is cached against a fingerprint
    of those numbers: re-opening the viewer replays the stored review for free,
    and only an actual change to the tree or alignment triggers a new call.

    Read-only, so view-mode visitors get it too.
    """
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404

    from app.services.tree_analysis_service import (
        TreeAnalysisDailyLimit,
        TreeAnalysisError,
        TreeAnalysisInProgress,
        TreeAnalysisNoTree,
        TreeAnalysisUnavailable,
        TreeAnalysisUpstreamError,
        is_configured,
        review_job,
    )

    if not is_configured():
        return jsonify({
            "status": "error",
            "error": "Claude review is not enabled on this server.",
        }), 503

    data = request.get_json(silent=True) or {}
    force_refresh, _ = coerce_bool(data.get("refresh"), False)

    try:
        payload = review_job(job_dir, force_refresh=force_refresh)
    except TreeAnalysisDailyLimit as exc:
        response = jsonify({"status": "error", "error": str(exc)})
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response, 429
    except TreeAnalysisInProgress as exc:
        # A duplicate of a review already running: a conflicting request for the
        # same resource, not an outage. Must be caught before the Unavailable
        # handler below, which it subclasses.
        response = jsonify({"status": "error", "error": str(exc)})
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return response, 409
    except TreeAnalysisUnavailable as exc:
        # Out of capacity or misconfigured: a retry may well succeed, so this is
        # deliberately not the same status as a dataset we cannot review at all.
        return jsonify({"status": "error", "error": str(exc)}), 503
    except TreeAnalysisNoTree as exc:
        # A job that never produced a tree: no such resource, rather than a bad
        # request. Must precede the TreeAnalysisError handler it subclasses.
        return jsonify({"status": "error", "error": str(exc)}), 404
    except TreeAnalysisUpstreamError as exc:
        # 502, not 400. The browser's request was fine; what failed was the
        # model's reply -- an empty review, a malformed one, or one that named
        # sequences this tree does not contain. Reporting that as a client error
        # blamed the user for an upstream failure and told every retry-on-5xx
        # client not to bother. Must precede the TreeAnalysisError handler it
        # subclasses.
        logger.warning("event=claude_review.upstream_failed reason=%s", type(exc).__name__)
        return jsonify({"status": "error", "error": str(exc)}), 502
    except TreeAnalysisError as exc:
        # The job itself has nothing reviewable (no tree, no aligned FASTA, an
        # empty alignment). That really is about the request.
        logger.warning("event=claude_review.failed reason=%s", type(exc).__name__)
        return jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as e:
        return _server_error(e, where="claude_review")

    logger.info(
        "event=claude_review.served cached=%s model=%s elapsed=%s",
        payload.get("cached"), payload.get("model"), payload.get("elapsed_seconds"),
    )
    return jsonify({
        "status": "ok",
        "cached": payload.get("cached", False),
        "model": payload.get("model"),
        "generated_at": payload.get("generated_at"),
        "review": payload.get("review"),
        "metrics": payload.get("metrics"),
    })


# =============================================================================
# SSE Real-Time Events Endpoint
# =============================================================================

def _read_log_tail(log_path, max_bytes=65536, max_lines=200):
    """
    Read the last N lines from a log file efficiently.
    
    Reads last max_bytes of file, splits into lines, returns last max_lines.
    """
    try:
        if not log_path.exists():
            return []
        
        file_size = log_path.stat().st_size
        
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
                # Skip partial first line
                f.readline()
            
            lines = f.readlines()
        
        # Strip and return last max_lines
        return [line.rstrip() for line in lines[-max_lines:]]
    
    except Exception as e:
        logger.warning(f"Failed to read log tail from {log_path}: {e}")
        return []


def _build_snapshot(job_id: str) -> dict:
    """
    Build initial snapshot for SSE connection.
    
    Includes job status, meta, and log tails.
    """
    import time
    from datetime import datetime
    
    job_dir = Config.JOB_DIR / job_id
    
    # Get RQ job status
    from app.workers.queue import get_job_status
    rq_status = get_job_status(job_id)
    
    # Get DB job for additional info
    db_job = Job.query.get(job_id)
    
    # Determine status
    status = rq_status.get('status', 'unknown')
    if db_job and db_job.status:
        # DB status is authoritative for completed/failed
        if db_job.status in ('completed', 'failed'):
            status = db_job.status
    
    # Normalize RQ vocabulary to the frontend's status vocabulary.
    if status == 'started':
        status = 'running'
    elif status == 'finished':
        status = 'completed'
    
    # Build job info
    job_info = {
        "id": job_id,
        "status": status,
        "enqueued_at": rq_status.get('enqueued_at'),
        "started_at": rq_status.get('started_at'),
        "ended_at": rq_status.get('ended_at'),
        "elapsed_seconds": None,
        "error_summary": None,
        "failed_step": None,
        "failed_step_label": None,
        "tool": None,
        "exit_code": None,
        "result_files": None,
        "meta": {},
    }
    
    def _parse_rq_timestamp(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except Exception:
            return None

    enqueued = _parse_rq_timestamp(rq_status.get('enqueued_at'))
    started = _parse_rq_timestamp(rq_status.get('started_at'))
    ended = _parse_rq_timestamp(rq_status.get('ended_at'))

    if enqueued and started:
        job_info["queue_wait_seconds"] = (started - enqueued).total_seconds()
    else:
        job_info["queue_wait_seconds"] = None

    if ended:
        # A finished job reports how long it actually ran. Deferred MycoMap/NCBI
        # steps re-enqueue with RQ Retry, which preserves the original
        # enqueued_at, so measuring from there would bill hours of queue wait as
        # pipeline runtime.
        run_start = started or enqueued
        if run_start:
            job_info["elapsed_seconds"] = (ended - run_start).total_seconds()
    else:
        # A running job counts from when it actually started; a job still
        # waiting has no start yet, so it counts from enqueue and the queued
        # page keeps its live counter.
        live_start = started or enqueued
        if live_start:
            from datetime import timezone
            now = datetime.now(timezone.utc)
            job_info["elapsed_seconds"] = (now - live_start).total_seconds()
    
    # Get RQ job meta
    try:
        from app.workers.queue import get_queue
        q = get_queue()
        rq_job = q.fetch_job(job_id)
        if rq_job and rq_job.meta:
            job_info["meta"] = rq_job.meta
    except Exception:
        pass
    
    # If job failed, extract failure info
    # Surface an automatic restart at any status: a requeued job goes back to
    # 'queued', so gating this on 'failed' would hide it exactly when the user is
    # watching the job run again and wondering why it started over.
    _metrics_any = (db_job.metrics if db_job else None) or {}
    job_info["requeued_after_interrupt"] = bool(_metrics_any.get('requeued_at'))
    job_info["restart_requeue_count"] = _metrics_any.get('restart_requeue_count') or 0
    if job_info["requeued_after_interrupt"] and status != 'failed':
        job_info["interrupted_notice"] = _metrics_any.get('interrupted_reason')

    if status == 'failed':
        metrics = (db_job.metrics if db_job else None) or {}
        if metrics:
            job_info["error_summary"] = metrics.get('error')
            job_info["failed_step"] = metrics.get('failed_step')
            # Diagnostics persisted by the worker's failure handler. Without these
            # a reloaded page lost everything the live SSE stream had shown.
            job_info["exit_code"] = metrics.get('exit_code')
            job_info["stderr_tail"] = metrics.get('stderr_tail') or []
            job_info["traceback_tail"] = metrics.get('traceback_tail') or []
            job_info["failed_step_label"] = metrics.get('failed_step_label')
            job_info["tool"] = metrics.get('failed_tool')
            job_info["failed_at"] = metrics.get('failed_at')

            # A job killed mid-run (service restart, OOM) never reaches the
            # failure handler, so it has no 'error' at all and the panel would
            # just say "An error occurred". The reconciler records what happened;
            # surface that instead, plus whether it was retried.
            interrupted = metrics.get('interrupted_reason')
            if interrupted and not job_info["error_summary"]:
                job_info["error_summary"] = interrupted
            job_info["interrupted"] = bool(interrupted)
            job_info["requeued_after_interrupt"] = bool(metrics.get('requeued_at'))
            job_info["restart_requeue_count"] = metrics.get('restart_requeue_count') or 0

        # Try to get more from RQ result
        result = rq_status.get('result', {})
        if isinstance(result, dict):
            job_info["error_summary"] = job_info["error_summary"] or result.get('error')

        # Fall back to step meta for the label/tool when metrics lacks them
        # (jobs that failed before this richer capture existed).
        failed_step = job_info.get("failed_step")
        if failed_step and "steps" in job_info["meta"]:
            step_info = job_info["meta"]["steps"].get(failed_step, {})
            job_info["failed_step_label"] = (
                job_info.get("failed_step_label") or step_info.get("label", failed_step)
            )
            job_info["tool"] = job_info.get("tool") or step_info.get("tool")
    
    # If job completed, include result files
    if status == 'completed':
        job_info["result_files"] = {
            "tree_newick": f"/api/job/{job_id}/download/tree/newick",
            "tree_nexus": f"/api/job/{job_id}/download/tree/nexus",
            "fasta_original": f"/api/job/{job_id}/download/fasta/original",
        }
        if (job_dir / "tree" / "mrbayes_input.nex").is_file():
            job_info["result_files"]["mrbayes"] = f"/api/job/{job_id}/download/mrbayes"
        if artifact_exists(job_dir / "alignment" / "alignment_trimmed_report.html"):
            job_info["result_files"]["alignment_inspection"] = (
                f"/api/job/{job_id}/download/alignment/inspection"
            )
    
    # Read log tails (generous limits to capture most output for completed jobs)
    logs_dir = job_dir / "logs"
    log_tails = {
        "pipeline": _read_log_tail(logs_dir / "pipeline.log", max_lines=500),
        "alignment": _read_log_tail(logs_dir / "alignment.log", max_lines=500),
        "tree_builder": _read_log_tail(logs_dir / "tree_builder.log", max_lines=1000),
    }
    
    return {
        "job": job_info,
        "log_tails": log_tails,
    }


@bp.route('/job/<job_id>/events', methods=['GET'])
def job_events_stream(job_id):
    """
    SSE endpoint for real-time job status updates.
    
    Protocol:
    - Emits `event: snapshot` with initial job state and log tails
    - Emits plain `data:` lines for all subsequent live events
    - Emits `event: ping` with `data: {}` every 15s as keepalive
    
    On disconnect/reconnect, server sends fresh snapshot.
    """
    import json
    import time
    import redis
    from flask import Response, stream_with_context
    
    # Check authorization
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    db_job, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code
    
    job_dir = Config.JOB_DIR / job_id
    
    def generate():
        from app.services.log_context import log_degradation_rate_limited
        from app.services import sse_registry
        stream_started = time.monotonic()
        close_reason = "unexpected_exception"
        pubsub = None
        stream_token = None
        registry_conn = None
        try:
            # Connect inside the guarded region so connection/subscription
            # failures are observable and cleanup stays safe.
            r = redis.from_url(Config.REDIS_URL)
            pubsub = r.pubsub()
            pubsub.subscribe(f"job:{job_id}:events")
            registry_conn = r
            stream_token, _ = sse_registry.open_stream(r, job_id)
            # Send initial snapshot
            snapshot = _build_snapshot(job_id)
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            
            # Check if job is already terminal
            job_status = snapshot["job"]["status"]
            # A job that finished before the client connected will never produce a
            # terminal pubsub event, and the DB-poll block below is skipped for
            # terminal jobs -- so without a deadline this loop had no reachable exit
            # and every viewer of a completed job pinned a request slot until their
            # socket happened to fail. Linger only long enough to catch stragglers.
            already_terminal = job_status in ('completed', 'failed')
            terminal_deadline = (
                time.monotonic() + Config.SSE_TERMINAL_LINGER_SECONDS
                if already_terminal else None
            )

            # Throttle timers (use monotonic clock for reliable intervals)
            last_ping = stream_started
            last_db_poll = 0.0  # Start at 0 to trigger immediate first poll

            # Tunable interval for DB polling (seconds)
            DB_POLL_INTERVAL = 1.0

            # Hard lifetime cap. This loop only exits on a terminal job state, and a
            # generator whose client has gone away keeps looping while sleeping --
            # burning almost no CPU but permanently holding one of the
            # (workers x threads) request slots. Jobs that never reach a terminal
            # state therefore leak a slot per viewer until the pool is exhausted and
            # the whole site stops responding. Cutting the stream lets EventSource
            # reconnect (it retries automatically and we re-send a fresh snapshot),
            # so a live viewer sees nothing but an orphan cannot pin a thread.
            MAX_STREAM_SECONDS = Config.SSE_MAX_STREAM_SECONDS
            # Alan 8/14/26 - Age alone is a bad proxy for "this stream is abandoned":
            # it cut genuinely long jobs (a RAxML publication run) every 30 minutes.
            # Idleness is the signal that actually distinguishes a stuck or orphaned
            # stream from a slow one, so track when this stream last saw real activity.
            MAX_IDLE_SECONDS = Config.SSE_MAX_IDLE_SECONDS
            last_activity = stream_started

            while True:
                if time.monotonic() - stream_started >= MAX_STREAM_SECONDS:
                    # Ask the client to come straight back, then let go of the slot.
                    yield "event: reconnect\ndata: {\"reason\": \"max_stream_age\"}\n\n"
                    logger.info(
                        "SSE stream for job %s hit the %ss lifetime cap; closing so the "
                        "client can reconnect.", job_id, MAX_STREAM_SECONDS
                    )
                    close_reason = "lifetime_cap"
                    break

                if (
                    MAX_IDLE_SECONDS > 0
                    and job_status not in ('completed', 'failed')
                    and time.monotonic() - last_activity >= MAX_IDLE_SECONDS
                ):
                    # Nothing has happened on this job for a long time. Either the
                    # viewer is gone or the job is stuck; both pin a request slot for
                    # no benefit. A live viewer's EventSource reconnects immediately.
                    yield "event: reconnect\ndata: {\"reason\": \"idle\"}\n\n"
                    logger.info(
                        "SSE stream for job %s idle for %ss with status %r; closing so "
                        "the client can reconnect.", job_id, MAX_IDLE_SECONDS, job_status
                    )
                    close_reason = "idle_reconnect"
                    break

                if terminal_deadline is not None and time.monotonic() >= terminal_deadline:
                    # Job was already finished when this stream opened; nothing more
                    # is coming, so release the slot instead of pinging forever.
                    close_reason = "terminal_completion"
                    break

                # Check for PubSub messages (non-blocking with short timeout)
                # Use shorter timeout to allow responsive loop with brief sleep
                message = pubsub.get_message(timeout=0.1)
                
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                    # Real job output: this stream is following live work, not idling.
                    last_activity = time.monotonic()

                    # Check if this is a terminal event
                    try:
                        event = json.loads(data)
                        if event.get('type') == 'job_state' and event.get('status') in ('completed', 'failed'):
                            # Send final event and close after brief delay
                            time.sleep(0.5)
                            close_reason = "terminal_completion"
                            break
                    except json.JSONDecodeError:
                        pass
                
                now = time.monotonic()
                
                # Send keepalive ping every 15 seconds
                if now - last_ping >= 15:
                    yield "event: ping\ndata: {}\n\n"
                    last_ping = now
                
                # Poll DB for job status at most once per DB_POLL_INTERVAL
                if job_status not in ('completed', 'failed'):
                    if now - last_db_poll >= DB_POLL_INTERVAL:
                        last_db_poll = now
                        try:
                            db.session.expire_all()
                            db_job_check = Job.query.get(job_id)
                        except Exception as db_exc:
                            # An SSE request can outlive the PostgreSQL SSL
                            # connection it checked out. Discard that scoped
                            # session and retry once with a fresh pooled
                            # connection instead of ending the stream.
                            from sqlalchemy.exc import OperationalError
                            if not isinstance(db_exc, OperationalError):
                                raise
                            db.session.remove()
                            log_degradation_rate_limited(
                                logger,
                                "sse_db_connection_recovered",
                                "SSE database connection dropped; retrying with a fresh session",
                                job_id=job_id,
                            )
                            db_job_check = Job.query.get(job_id)
                        logger.debug(f"SSE DB poll for job {job_id}: status={db_job_check.status if db_job_check else 'None'}")
                        if db_job_check and db_job_check.status in ('completed', 'failed'):
                            job_status = db_job_check.status
                            # Give a moment for final metadata to settle, then send a
                            # terminal snapshot in case the Redis completion event was
                            # missed. Closing without this snapshot can leave an open
                            # status page displaying its last non-terminal step.
                            time.sleep(1)
                            terminal_snapshot = _build_snapshot(job_id)
                            yield (
                                "event: snapshot\n"
                                f"data: {json.dumps(terminal_snapshot)}\n\n"
                            )
                            close_reason = "terminal_completion"
                            break
                
                # Brief sleep to prevent CPU spin (50-100ms effective with pubsub timeout)
                time.sleep(0.05)
        
        except GeneratorExit:
            close_reason = "client_disconnect"
            raise
        except redis.RedisError as exc:
            close_reason = "redis_failure"
            log_degradation_rate_limited(
                logger, "sse_redis_failure",
                "SSE stream closed after Redis failure",
                exception=type(exc).__name__,
            )
        except Exception:
            close_reason = "unexpected_exception"
            logger.exception("event=sse.generator_failed SSE generator failed")
        finally:
            if pubsub is not None:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception as exc:
                    log_degradation_rate_limited(
                        logger, "sse_cleanup_failed",
                        "SSE PubSub cleanup failed",
                        exception=type(exc).__name__,
                    )
            remaining = (
                sse_registry.close_stream(registry_conn, stream_token)
                if stream_token is not None else 0
            )
            logger.info(
                "event=sse.closed SSE stream closed reason=%s duration_seconds=%.3f "
                "open_streams=%s",
                close_reason, time.monotonic() - stream_started, remaining,
            )
    
    response = Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # Disable nginx buffering
            'Connection': 'keep-alive',
        }
    )
    return response


# =============================================================================
# Log Download Endpoints
# =============================================================================

@bp.route('/job/<job_id>/logs/<log_name>', methods=['GET'])
def download_log(job_id, log_name):
    """
    Download job log files.
    
    Valid log_name values:
    - pipeline: pipeline.log
    - alignment: alignment.log  
    - tree_builder: tree_builder.log
    """
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    # Check authorization
    _, error_msg, status_code = check_job_access(job_id)
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code
    
    job_dir = Config.JOB_DIR / job_id
    
    # Map log names to files
    log_files = {
        "pipeline": "pipeline.log",
        "alignment": "alignment.log",
        "tree_builder": "tree_builder.log",
    }
    
    if log_name not in log_files:
        return jsonify({
            "status": "error", 
            "error": f"Invalid log name. Valid options: {', '.join(log_files.keys())}"
        }), 400
    
    log_path = job_dir / "logs" / log_files[log_name]
    
    if not validate_safe_file_path(log_path, job_dir / "logs"):
        return jsonify({"status": "error", "error": "Log file not found or invalid"}), 404
    
    return send_file(
        log_path,
        as_attachment=True,
        download_name=f"{job_id}_{log_files[log_name]}"
    )

@bp.route('/log/client', methods=['POST'])
@limiter.limit("30 per minute; 500 per day")
def log_client_error():
    """
    Log client-side errors to the server log.
    Accept only bounded, query-free telemetry fields from the shared browser layer.
    """
    from flask import current_app
    if request.content_length and request.content_length > 16 * 1024:
        return jsonify({"status": "ignored", "error": "payload too large"}), 413

    data = request.get_json(silent=True) or {}
    allowed_events = {
        "window_error", "unhandled_rejection", "resource_load_failed",
        "api_non_2xx", "ui_action_failed",
    }
    event = _client_log_value(data.get("event"), 50)
    if event not in allowed_events:
        return jsonify({"status": "ignored"}), 200
    # Defense in depth: every untrusted field goes through the one shared
    # sanitizer. The browser helper cleans the same fields, but this endpoint is
    # a plain POST that anything can call, and a stack frame or an action label
    # is exactly where a pasted sequence or an OAuth callback URL ends up.
    from app.services.log_context import sanitize_telemetry_text

    message = sanitize_telemetry_text(data.get("message") or "browser failure", 500)
    action = sanitize_telemetry_text(data.get("action"), 100)
    pathname = sanitize_telemetry_text(
        urlsplit(str(data.get("pathname") or "")).path, 500
    )
    if not pathname.startswith("/"):
        pathname = "/"
    job_id = _client_log_value(data.get("job_id"), 40)
    if job_id and not validate_job_id(job_id):
        job_id = ""
    stack = sanitize_telemetry_text(data.get("stack"), 2000)
    supplied_fingerprint = _client_log_value(data.get("fingerprint"), 80)
    fingerprint = supplied_fingerprint or hashlib.sha256(
        f"{event}|{pathname}|{action}|{message}|{stack[:300]}".encode()
    ).hexdigest()[:16]

    # Cross-process short-window dedup. Telemetry remains fail-open if Redis is
    # unavailable; the endpoint's existing rate limit and size bounds still apply.
    try:
        from app.workers.queue import get_redis_connection
        if not get_redis_connection().set(
            f"client-telemetry:{fingerprint}", "1", nx=True, ex=120
        ):
            return jsonify({"status": "duplicate"}), 200
    except Exception:
        pass

    current_app.logger.error(
        "event=client.%s Browser failure pathname=%s job_id=%s action=%s "
        "message=%s fingerprint=%s release=%s browser=%s stack=%s",
        event, pathname, job_id or "-", action or "-", message, fingerprint,
        current_app.config.get("RELEASE_VERSION", "unknown"),
        sanitize_telemetry_text(request.headers.get("User-Agent"), 200), stack or "-",
    )
    return jsonify({"status": "logged"}), 200

@bp.route('/job/<job_id>/sequences/add', methods=['POST'])
def add_sequences_to_job(job_id):
    """
    Add sequences to an existing job's input file.
    
    Request: { "input": "<fasta or accession list>", "replace": false }
    Response: { "status": "success", "count": int, "message": "..." }
    """
    # Check authorization
    _, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code
    
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    data = request.get_json(silent=True) or {}
    input_text = data.get("input", "").strip()
    replace_existing = bool(data.get("replace"))
    
    if not input_text:
        return jsonify({"status": "error", "error": "No input provided"}), 400

    # 1. Raw input limit. Leave room for FASTA headers and line wrapping around
    # the separate 2,000,000-base semantic limit enforced below.
    MAX_INPUT_CHARS = 2_500_000
    if len(input_text) > MAX_INPUT_CHARS:
        return jsonify({"status": "error", "error": f"Input too large (max {MAX_INPUT_CHARS} chars)"}), 400
        
    try:
        sequences_to_add = []
        is_accession_input = False
        skipped = []
        
        # Heuristic: Check if input looks like a list of accessions (no > at start, short lines)
        first_line = input_text.splitlines()[0].strip()
        if not first_line.startswith(">"):
            # Assume accessions
            is_accession_input = True
            
            accessions, invalid = _parse_genbank_accession_tokens(input_text)
            if invalid:
                return jsonify({
                    "status": "error",
                    "error": f"Invalid GenBank accession(s): {', '.join(invalid[:10])}"
                }), 400
            
            # Security: Limit number of accessions to prevent abuse
            if len(accessions) > MAX_CUSTOM_GENBANK_ACCESSIONS:
                 return jsonify({"status": "error", "error": f"Too many accessions (max {MAX_CUSTOM_GENBANK_ACCESSIONS})"}), 400
            
            if accessions:
                logger.info("event=job.accessions_added Adding sequences accession_count=%s", len(accessions))
                sequences_to_add, skipped = _fetch_genbank_sequences_for_queue(
                    accessions,
                    max_sequence_bp=MAX_CUSTOM_GENBANK_SEQUENCE_BP
                )
        else:
            # Assume FASTA
            sequences_to_add = _parse_fasta_sequences(input_text)
            
        # Filter out sequences with empty sequence data
        sequences_to_add = [s for s in sequences_to_add if s.get('sequence', '').strip()]
        
        if not sequences_to_add:
            if is_accession_input and any(item.get("reason") == "too_long" for item in skipped):
                return jsonify({
                    "status": "error",
                    "error": "No sequences were added because all fetched GenBank records exceeded the size limit.",
                    "skipped": skipped
                }), 400
            return jsonify({"status": "error", "error": "No valid sequences found in input"}), 400

        # 2. Sequence Count Limit
        MAX_SEQUENCES_TO_ADD = 500
        if len(sequences_to_add) > MAX_SEQUENCES_TO_ADD:
            return jsonify({"status": "error", "error": f"Too many sequences (max {MAX_SEQUENCES_TO_ADD})"}), 400
            
        # 3. Total Base Pair Limit
        MAX_TOTAL_BP_TO_ADD = 2_000_000
        total_bp = sum(len(s['sequence']) for s in sequences_to_add)
        if total_bp > MAX_TOTAL_BP_TO_ADD:
             return jsonify({"status": "error", "error": f"Total sequence length too large (max {MAX_TOTAL_BP_TO_ADD} bp)"}), 400
             
        # Append to input_raw.fasta, or replace it when the queue is the desired final set.
        input_path = job_dir / "input" / "input_raw.fasta"
        input_path.parent.mkdir(parents=True, exist_ok=True)

        if replace_existing:
            used_ids = set()
            seen_records = set()
            added_count = 0
            output_records = []

            for seq in sequences_to_add:
                exact_key = _sequence_exact_key(seq)
                if exact_key in seen_records:
                    continue
                seen_records.add(exact_key)
                output_records.append(_format_fasta_record_for_job(seq, used_ids, added_count + 1))
                added_count += 1

            input_path.write_text("\n".join(record.rstrip() for record in output_records) + "\n")

            message = f"Saved {added_count} queued sequence{'s' if added_count != 1 else ''}."
            if skipped:
                message += f" {len(skipped)} accession{'s' if len(skipped) != 1 else ''} skipped."

            return jsonify({
                "status": "success",
                "count": added_count,
                "skipped": skipped,
                "mode": "replace",
                "message": message
            })
        
        # Read existing IDs to check for duplicates
        existing_ids = set()
        existing_records = set()
        if input_path.exists():
            try:
                existing_seqs = _parse_fasta_sequences(input_path.read_text())
                for s in existing_seqs:
                    sid, _ = _split_fasta_header(s['name'])
                    if sid:
                        existing_ids.add(sid)
                    existing_records.add(_sequence_exact_key(s))
            except Exception as e:
                logger.warning(f"Failed to parse existing FASTA for deduplication: {e}")
                pass # Continue anyway - will allow duplicates but won't crash
            
        added_count = 0
        with open(input_path, "a") as f:
            # Ensure newline at end of file before appending
            if input_path.stat().st_size > 0:
                f.write("\n")
                 
            for seq in sequences_to_add:
                exact_key = _sequence_exact_key(seq)
                if exact_key in existing_records:
                    continue
                f.write(_format_fasta_record_for_job(seq, existing_ids, added_count + 1))
                existing_records.add(exact_key)
                added_count += 1
                    
        message = f"Added {added_count} sequences."
        if skipped:
            message += f" {len(skipped)} accession{'s' if len(skipped) != 1 else ''} skipped."

        return jsonify({
            "status": "success", 
            "count": added_count,
            "skipped": skipped,
            "message": message
        })
        
    except Exception as e:
        return _server_error(e, where="add_sequences")
