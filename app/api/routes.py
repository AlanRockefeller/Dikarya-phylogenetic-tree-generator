from flask import jsonify, request, send_file, url_for
from flask_login import current_user
from app.api import bp
from app.workers.queue import enqueue_job, enqueue_recompute_job, get_job_status
from app.config import Config
from app.extensions import db
from app.models import Job
import logging
import re
from datetime import datetime

from app.services.security_utils import validate_job_id, validate_safe_file_path
from app.services.access_control import check_job_access
from app.extensions import limiter

logger = logging.getLogger(__name__)


def _server_error(exc, *, where=""):
    """Return a generic 500 JSON response without leaking internals.

    The full exception (including traceback) is logged server-side under a
    short request_id, which is echoed back to the client so support requests
    can be correlated to the log without exposing file paths, library
    versions, or message text from the underlying error.
    """
    import uuid as _uuid
    import traceback as _tb
    request_id = _uuid.uuid4().hex[:12]
    logger.error(
        "[%s] %s%s: %s\n%s",
        request_id,
        where + " " if where else "",
        type(exc).__name__,
        exc,
        _tb.format_exc(),
    )
    return jsonify({
        "status": "error",
        "error": "Internal server error",
        "request_id": request_id,
    }), 500


# =============================================================================
# BLAST API Endpoint
# =============================================================================

def _is_genbank_accession(text):
    """Check if text looks like a GenBank accession number."""
    # Common nucleotide patterns: NC_012345, NM_001234567, OR807397.1, etc.
    pattern = r'^[A-Z]{1,6}_?\d{5,9}(?:\.\d+)?$'
    return bool(re.match(pattern, text.strip(), re.IGNORECASE))


MAX_CUSTOM_GENBANK_ACCESSIONS = 200
MAX_CUSTOM_GENBANK_SEQUENCE_BP = 5000
MAX_SEQUENCE_METADATA_ITEMS = 5000

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
            "identity": _optional_float(raw.get("identity")),
            "query_cover": _optional_float(raw.get("query_cover")),
            "subject_cover": _optional_float(raw.get("subject_cover")),
        }
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


def _sequence_location_dedup_key(seq, metadata_by_header):
    """Return a dedup key for same sequence + same known location, else exact record key."""
    sequence = ''.join(str(seq.get('sequence') or '').split()).upper()
    if not sequence:
        return None

    header = str(seq.get('name') or '').strip()
    metadata = metadata_by_header.get(header, {})
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

    seen = set()
    deduped = []
    deduped_headers = set()
    for seq in sequences:
        key = _sequence_location_dedup_key(seq, metadata_by_header)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(seq)
        deduped_headers.add(str(seq.get("name") or "").strip())

    if len(deduped) == len(sequences):
        return sequence_text, sequence_metadata

    fasta = ''.join(
        f">{seq.get('name', '').strip()}\n{''.join(str(seq.get('sequence') or '').split())}\n"
        for seq in deduped
    ).strip()
    metadata = [
        item for item in sequence_metadata
        if str(item.get("fasta_header") or item.get("name") or "").strip() in deduped_headers
    ]
    return fasta, metadata


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
        
    except Exception as e:
        logger.error(f"BLAST API error: {e}", exc_info=True)
        return _server_error(e)


@bp.route('/genbank/accessions', methods=['POST'])
@limiter.limit("10 per minute; 200 per hour")
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
        logger.error(f"GenBank accession API error: {e}", exc_info=True)
        return _server_error(e)


def gather_mycomap_sequences_for_queue(url, include_ncbi=True, include_local=True):
    """Reusable helper for fetching MycoMap BLAST result sequences.

    Returns a tuple ``(payload, error_response)`` where ``payload`` is the
    dict normally returned by /api/mycomap on success (sequences,
    ncbi_count, local_count, blast_metrics_count, message) and
    ``error_response`` is a ``(json_dict, http_status)`` tuple on failure.
    Exactly one of the two will be non-None.
    """
    if not url:
        return None, ({"status": "error", "error": "No URL provided"}, 400)
    if not include_ncbi and not include_local:
        return None, ({
            "status": "error",
            "error": "Select at least one result type (NCBI or Local)",
        }, 400)

    from app.services.mycomap_service import (
        build_blast_metric_keys,
        fetch_mycomap_blast_metrics,
        fetch_mycomap_fasta,
        improve_mycomap_sequence_name,
        validate_mycomap_url,
    )
    from app.services.fasta_utils import clean_dna_sequence

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

    blast_id = validate_mycomap_url(url)
    if not blast_id:
        return None, ({
            "status": "error",
            "error": "Invalid Mycomap URL. URL must be from mycomap.com and contain a result ID (e.g., r12345)",
        }, 400)

    logger.info(f"Mycomap helper: blast_id={blast_id} (ncbi={include_ncbi}, local={include_local})")
    result = fetch_mycomap_fasta(blast_id, include_ncbi, include_local)

    if result['errors'] and not result['fasta_content']:
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

    from app.services.blast_service import fetch_fasta_for_accessions
    accessions_to_enrich = []
    for seq in sequences:
        parts = seq['name'].split()
        if len(parts) == 1 and _is_genbank_accession(parts[0].split('.')[0]):
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

    original_count = len(sequences)
    for seq in sequences:
        seq['sequence'] = clean_dna_sequence(seq['sequence'])
    sequences = [s for s in sequences if s['sequence']]
    dropped_count = original_count - len(sequences)

    metrics_by_key = fetch_mycomap_blast_metrics(blast_id, source_url=url)
    metrics_attached_count = 0
    contaminant_dropped_count = 0
    filtered_sequences = []
    for seq in sequences:
        metric = None
        lookup_names = [seq.get('name', ''), seq.get('_mycomap_original_name', '')]
        for lookup_name in lookup_names:
            for key in build_blast_metric_keys(lookup_name):
                metric = metrics_by_key.get(key)
                if metric:
                    break
            if metric:
                break
        if is_contaminant_sequence(seq, metric):
            contaminant_dropped_count += 1
            continue
        if metric:
            seq['name'] = improve_mycomap_sequence_name(
                seq.get('name', ''), metric, seq.get('hit_source', ''),
            )
            seq['identity'] = metric.get('identity')
            seq['query_cover'] = metric.get('query_cover')
            seq['subject_cover'] = metric.get('subject_cover')
            seq['location'] = metric.get('mycomap_location') or ''
            seq['blast_metrics_available'] = any(
                seq[field] is not None for field in ('identity', 'query_cover', 'subject_cover')
            )
            if seq['blast_metrics_available']:
                metrics_attached_count += 1
        else:
            seq['identity'] = None
            seq['query_cover'] = None
            seq['subject_cover'] = None
            seq['location'] = ''
            seq['blast_metrics_available'] = False
        seq['name'] = remove_lowquality_label(seq.get('name', ''))
        seq.pop('_mycomap_original_name', None)
        filtered_sequences.append(seq)
    sequences = filtered_sequences

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
    if result['errors']:
        msg += f" (warnings: {'; '.join(result['errors'])})"

    return {
        "status": "success",
        "sequences": sequences,
        "ncbi_count": result['ncbi_count'],
        "local_count": result['local_count'],
        "blast_metrics_count": metrics_attached_count,
        "message": msg,
    }, None


@bp.route('/mycomap', methods=['POST'])
@limiter.limit("10 per minute; 200 per hour")
def fetch_mycomap():
    """
    Fetch sequences from a Mycomap BLAST results URL.

    Request: {
        "url": "<mycomap URL>",
        "include_ncbi": true,
        "include_local": true
    }
    Response: { "status": "success", "sequences": [...], "message": "..." }
    """
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    include_ncbi = data.get('include_ncbi', True)
    include_local = data.get('include_local', True)

    try:
        payload, err = gather_mycomap_sequences_for_queue(url, include_ncbi, include_local)
        if err is not None:
            body, status = err
            return jsonify(body), status
        return jsonify(payload)
    except Exception as e:
        logger.error(f"Mycomap API error: {e}", exc_info=True)
        return _server_error(e)


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
        return "10 per 5 minutes"
    return "20 per hour"


@bp.route('/inaturalist/tree', methods=['POST'])
@limiter.limit(_inat_tree_rate_limit, key_func=_inat_tree_rate_key)
def inaturalist_tree():
    """Create a one-click Dikarya tree from a single iNaturalist observation.

    Request: { "observation": "<id-or-single-observation-url>" }
    """
    from app.services.inaturalist_tree_service import (
        InatTreeError, create_job_from_inat_observation,
    )
    data = request.get_json(silent=True) or {}
    raw = data.get('observation') or data.get('url') or ''
    try:
        result = create_job_from_inat_observation(
            raw,
            user=current_user,
            public_base_url=request.url_root,
        )
        return jsonify(result), 202
    except InatTreeError as e:
        return jsonify({"status": "error", "error": str(e)}), e.status
    except Exception as e:
        logger.error("iNaturalist tree endpoint error: %s", e, exc_info=True)
        return _server_error(e)


@bp.route('/inaturalist', methods=['POST'])
@limiter.limit("10 per minute; 200 per hour")
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
        logger.error(f"iNaturalist API error: {e}", exc_info=True)
        return _server_error(e)




@bp.route('/job', methods=['POST'])
@limiter.limit("20 per hour; 100 per day")
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
        "mycomap_blast_url": data.get("mycomap_blast_url") or "",
        "accessions": data.get("accessions", []),
        "alignment_method": data.get("alignment_method", "default"),
        "trimming_method": data.get("trimming_method", "none"),
        "alignment_options": data.get("alignment_options", {}),
        "tree_method": tree_method,
        "tree_model": data.get("tree_model", "GTR+G"),
        "bootstrap": data.get("bootstrap", 1000), # Legacy field
        "mcmc_generations": data.get("mcmc_generations", 50000),
        "mcmc_nruns": data.get("mcmc_nruns", 2),
        "mcmc_nchains": data.get("mcmc_nchains", 4),
        
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

    job_params["bootstrap"] = _clamp_int(job_params.get("bootstrap"), 1000, 0, 10_000)
    job_params["mcmc_generations"] = _clamp_int(
        job_params.get("mcmc_generations"), 50_000, 1_000, 100_000_000
    )
    job_params["mcmc_nruns"] = _clamp_int(job_params.get("mcmc_nruns"), 2, 1, 8)
    job_params["mcmc_nchains"] = _clamp_int(job_params.get("mcmc_nchains"), 4, 1, 16)
    job_params["sequence"], job_params["sequence_metadata"] = _dedupe_sequence_payload(
        job_params.get("sequence", ""),
        job_params.get("sequence_metadata", []),
    )

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
                "run_preset": job_params.get("run_preset"),
                "bootstrap_cap": job_params.get("bootstrap_cap")
            }
        )
        
        if current_user.is_authenticated:
            job_record.user_id = current_user.id
            
        db.session.add(job_record)
        db.session.commit()
        
        return jsonify({"status": "queued", "job_id": job_id}), 202
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
        from app.services.tree_edit_service import load_tree_state, prune_taxa, save_tree_state
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
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    
    try:
        from app.services.tree_edit_service import load_tree_state, rename_tip, save_tree_state
        state = load_tree_state(job_dir)
        state = rename_tip(state, old_name, new_name)
        save_tree_state(job_dir, state)
        return jsonify(state)
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
        from app.services.tree_edit_service import load_tree_state, reroot_tree, save_tree_state
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
        from app.services.tree_edit_service import load_tree_state, midpoint_root, save_tree_state
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
            load_tree_state, midpoint_root, undo_midpoint_root, save_tree_state
        )
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
        from app.services.tree_edit_service import load_tree_state, save_tree_state
        state = load_tree_state(job_dir)
        state["selection_sets"] = sets
        state["active_selection_set"] = active
        state["selection_set_colors"] = colors or {}
        save_tree_state(job_dir, state)
        return jsonify({"status": "ok"})
    except Exception as e:
        return _server_error(e)

@bp.route('/job/<job_id>/tree/recompute', methods=['POST'])
@limiter.limit("1 per minute")
def recompute_tree_job(job_id):
    db_job, error_msg, status_code = check_job_access(job_id, mode="edit")
    if error_msg:
        return jsonify({"status": "error", "error": error_msg}), status_code

    job_dir = Config.JOB_DIR / job_id
        
    try:
        import json
        from app.services.tree_edit_service import build_recompute_job_params, recompute_tree
        
        # Load original params
        input_info_path = job_dir / "input_info.json"
        params_dict = {}
        if input_info_path.exists():
            with open(input_info_path, "r") as f:
                params_dict = json.load(f)
        
        # Merge with request data
        req_data = request.get_json(silent=True) or {}
        params_dict.update(req_data)

        if req_data.get("async"):
            recompute_job_id = enqueue_recompute_job(job_id, params_dict)
            if db_job:
                metrics = db_job.metrics or {}
                metrics["recompute_requested_at"] = datetime.utcnow().isoformat()
                db_job.metrics = metrics
                db_job.status = "queued"
                db.session.commit()
            return jsonify({
                "status": "queued",
                "job_id": job_id,
                "rq_job_id": recompute_job_id,
                "redirect_url": url_for('main.job_status', job_id=job_id)
            }), 202

        job_params = build_recompute_job_params(params_dict)
        result = recompute_tree(
            job_dir,
            job_params,
            Config,
            logger,
            use_current_input=bool(params_dict.get("use_current_input"))
        )
        return jsonify(result)
        
    except Exception as e:
        import traceback
        logger.error(f"Recompute error: {e}\n{traceback.format_exc()}")
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

@bp.route('/job/<job_id>/download/fasta/pruned', methods=['GET'])
def download_fasta_pruned(job_id):
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
    if not path.exists():
        path = job_dir / "alignment" / "aligned.fasta"
    
    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "Aligned FASTA not found or invalid"}), 404
        
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
    path = job_dir / "alignment" / "alignment_raw.fasta"
    if not path.exists():
        path = job_dir / "alignment" / "aligned.fasta"
    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "Aligned FASTA not found"}), 404

    try:
        records = list(SeqIO.parse(str(path), "fasta"))
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

    def match(name):
        if not name:
            return None
        if name in by_full:
            return by_full[name]
        t = name.strip()
        if t in by_trimmed:
            return by_trimmed[t]
        tok = t.split(None, 1)[0] if t else ""
        hits = by_token.get(tok, [])
        if len(hits) == 1:
            return hits[0]
        return None

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
    
    if not validate_safe_file_path(path, job_dir):
        return jsonify({"status": "error", "error": "Trimmed FASTA not found or invalid"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_trimmed.fasta")


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
    
    # Normalize: RQ uses 'finished', we use 'completed'
    if status == 'finished':
        status = 'completed'
    
    # Build job info
    job_info = {
        "id": job_id,
        "status": status,
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
    
    # Calculate elapsed time
    if rq_status.get('started_at'):
        try:
            started = datetime.fromisoformat(rq_status['started_at'].replace('Z', '+00:00'))
            if rq_status.get('ended_at'):
                ended = datetime.fromisoformat(rq_status['ended_at'].replace('Z', '+00:00'))
                job_info["elapsed_seconds"] = (ended - started).total_seconds()
            else:
                from datetime import timezone
                now = datetime.now(timezone.utc)
                job_info["elapsed_seconds"] = (now - started).total_seconds()
        except Exception:
            pass
    
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
    if status == 'failed':
        if db_job and db_job.metrics:
            job_info["error_summary"] = db_job.metrics.get('error')
            job_info["failed_step"] = db_job.metrics.get('failed_step')
        
        # Try to get more from RQ result
        result = rq_status.get('result', {})
        if isinstance(result, dict):
            job_info["error_summary"] = job_info["error_summary"] or result.get('error')
            
        # Get failed step label from meta
        failed_step = job_info.get("failed_step")
        if failed_step and "steps" in job_info["meta"]:
            step_info = job_info["meta"]["steps"].get(failed_step, {})
            job_info["failed_step_label"] = step_info.get("label", failed_step)
            job_info["tool"] = step_info.get("tool")
    
    # If job completed, include result files
    if status == 'completed':
        job_info["result_files"] = {
            "tree_newick": f"/api/job/{job_id}/download/tree/newick",
            "tree_nexus": f"/api/job/{job_id}/download/tree/nexus",
            "fasta_original": f"/api/job/{job_id}/download/fasta/original",
        }
    
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
        # Connect to Redis for PubSub
        redis_url = Config.REDIS_URL
        r = redis.from_url(redis_url)
        pubsub = r.pubsub()
        
        channel = f"job:{job_id}:events"
        pubsub.subscribe(channel)
        
        try:
            # Send initial snapshot
            snapshot = _build_snapshot(job_id)
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            
            # Check if job is already terminal
            job_status = snapshot["job"]["status"]
            if job_status in ('completed', 'failed'):
                # Still keep connection open briefly for any final events
                pass
            
            # Throttle timers (use monotonic clock for reliable intervals)
            last_ping = time.monotonic()
            last_db_poll = 0.0  # Start at 0 to trigger immediate first poll
            
            # Tunable interval for DB polling (seconds)
            DB_POLL_INTERVAL = 1.0
            
            while True:
                # Check for PubSub messages (non-blocking with short timeout)
                # Use shorter timeout to allow responsive loop with brief sleep
                message = pubsub.get_message(timeout=0.1)
                
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                    
                    # Check if this is a terminal event
                    try:
                        event = json.loads(data)
                        if event.get('type') == 'job_state' and event.get('status') in ('completed', 'failed'):
                            # Send final event and close after brief delay
                            time.sleep(0.5)
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
                        db.session.expire_all()
                        db_job_check = Job.query.get(job_id)
                        logger.debug(f"SSE DB poll for job {job_id}: status={db_job_check.status if db_job_check else 'None'}")
                        if db_job_check and db_job_check.status in ('completed', 'failed'):
                            job_status = db_job_check.status
                            # Give a moment for final events from Redis
                            time.sleep(1)
                            break
                
                # Brief sleep to prevent CPU spin (50-100ms effective with pubsub timeout)
                time.sleep(0.05)
        
        finally:
            pubsub.unsubscribe()
            pubsub.close()
    
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
    Expected JSON: { "message": "...", "stack": "...", "url": "...", "context": "..." }
    """
    from flask import current_app
    
    data = request.get_json(silent=True) or {}
    # Apply size limits to prevent log spam
    msg = (data.get("message") or "Unknown client error")[:2000]
    stack = (data.get("stack") or "")[:20000]
    context = (data.get("context") or "")[:1000]
    # Sanitize inputs to prevent log injection
    msg = msg.replace('\n', ' ').replace('\r', ' ')
    stack = stack.replace('\n', ' ').replace('\r', ' ')
    context = context.replace('\n', ' ').replace('\r', ' ')
    url = (data.get("url") or "")[:500].replace('\n', '').replace('\r', '')
    
    # Request metadata for debugging
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    user_agent = (request.headers.get("User-Agent") or "")[:200]
    
    # Format log message with metadata
    log_msg = f"Client Error: {msg}"
    log_msg += f" [IP: {client_ip}]"
    if url:
        log_msg += f" [URL: {url}]"
    if context:
        log_msg += f" [Context: {context}]"
    if user_agent:
        log_msg += f" [UA: {user_agent}]"
    if stack:
        log_msg += f"\nStack: {stack}"
        
    current_app.logger.error(log_msg)
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

    # 1. Raw Input Limit (e.g. 200KB characters)
    MAX_INPUT_CHARS = 200_000
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
                logger.info(f"Adding sequences from accessions: {accessions}")
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
        logger.error(f"Failed to add sequences: {e}", exc_info=True)
        return _server_error(e)
