"""Submit MycoMap.org BLAST searches and read results through fixed API paths."""

import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from app.services.api_diagnostics import diagnostic_urlopen
from app.services.fasta_utils import is_genbank_accession, parse_fasta_records

logger = logging.getLogger(__name__)
ORG_API = "https://mycomap.org/api/mycoblast"
ORG_API_ROOT = "https://mycomap.org/api"
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
NCBI_BATCH_SIZE = 50
_JOB_LINK = re.compile(r"(?:/|%2F)(\d+)(?:/?$|[?&#])", re.I)
_LOCAL_XML_ID = re.compile(r"/local_(\d+)_xml\.", re.I)
# The rerun endpoint accepts only these hit counts (MycoMap.org PR #9).
RERUN_MAX_HITS = (50, 100, 250, 500)
# MycoMap.org's public MycoBLAST form accepts one 50-20,000 bp query.
SEARCH_MIN_QUERY_LENGTH = 50
SEARCH_MAX_QUERY_LENGTH = 20_000
SEARCH_POLL_INTERVAL_SECONDS = 2
SEARCH_REQUEST_BUDGET_SECONDS = 200
SEARCH_AUTH_KEY_PREFIX = "dikarya:mycomap-org:submit-auth:"


def _search_auth_key():
    key = (os.environ.get("MYCOMAP_ORG_API_KEY") or "").strip()
    return SEARCH_AUTH_KEY_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def search_availability():
    """Report a known refusal without probing upstream or exposing credentials."""
    message = "MycoMap BLAST is temporarily unavailable. Use NCBI BLAST while access is restored."
    if not (os.environ.get("MYCOMAP_ORG_API_KEY") or "").strip():
        return {"available": False, "message": message, "retry_after": 600}
    try:
        from app.services.ncbi_pacing import connection
        remaining = connection().ttl(_search_auth_key())
        if remaining > 0:
            return {"available": False, "message": message, "retry_after": remaining}
    except Exception:
        pass
    return {"available": True, "message": "", "retry_after": 0}


def _remember_search_refusal():
    try:
        from app.services.ncbi_pacing import connection
        connection().set(_search_auth_key(), "refused", ex=600)
    except Exception:
        logger.warning("event=mycomap_org.availability_store_failed")


class OrgResultError(Exception):
    def __init__(self, message, status=502, retryable=True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def deferral_details(exc):
    """The worker's wait marker for a MycoMap.org failure, or None to fail.

    A retryable 502 means MycoMap.org did not answer; a 409 means it answered
    that the BLAST results are not published yet. The worker waits on both
    (see _defer_for_inat_rate_limit in app/workers/tasks.py).
    """
    if not getattr(exc, "retryable", False):
        return None
    if exc.status == 502:
        return {"mycomap_unavailable": True}
    if exc.status == 409:
        return {"mycomap_results_pending": True}
    return None


def _read(path, *, limit=MAX_JSON_BYTES, method="GET", body=None,
          authenticated=False, deadline=None, timeout=15):
    """Only fixed API paths are accepted; no URL from a result is fetched."""
    # ":" is for observation references (inat:123) in a query string.
    if not re.fullmatch(r"[a-z0-9/?=&,:_-]+", path, re.I) or ".." in path:
        raise OrgResultError("Invalid MycoMap API path.", 400)
    headers = {
        "Accept": "application/json" if limit == MAX_JSON_BYTES else "application/xml",
        "User-Agent": "Dikarya-TreeBuilder/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if authenticated:
        key = (os.environ.get("MYCOMAP_ORG_API_KEY") or "").strip()
        if not key:
            raise OrgResultError("MycoMap.org access key is not configured.", 502,
                                 retryable=False)
        # Alan 10/7/26 - MycoMap.org reads an API key only from X-API-Key, raw,
        # with no "Bearer " prefix. Authorization: Bearer there carries mobile-app
        # tokens, so a key sent that way is treated as anonymous -- which is why
        # every rerun since the integration was written answered 401. (The
        # rerun route itself also took session cookies only until MycoMap.org
        # PR #9 moved it to requireAuthOrApiKey.)
        headers["X-API-Key"] = key
        if path == "submit":
            availability = search_availability()
            if not availability["available"]:
                raise OrgResultError(availability["message"], 502, retryable=False)
    base = ORG_API_ROOT if path.startswith(("mycomap/", "admin/")) else ORG_API
    request = urllib.request.Request(
        f"{base}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0:
        raise OrgResultError("MycoMap.org request timed out.")
    try:
        with diagnostic_urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        # Alan 10/7/26 - A 401/403 is a refusal, not an outage. It used to fall
        # into the generic retryable 502 below, so the user was told MycoMap
        # "could not complete the BLAST request" and an NCBI rebuild would sit
        # in the mycomap_unavailable wait loop on a credential that will never
        # start working. Name it, and do not retry it.
        if exc.code == 429:
            # 10 reruns per 10 minutes per key owner, and every Dikarya user shares
            # this one key. A wait clears it, so make it the retryable 502 the
            # worker already waits out (deferral_details) rather than a refusal.
            retry_after = (exc.headers.get("Retry-After") if exc.headers else None) or ""
            logger.warning("event=mycomap_org.rate_limited path=%s retry_after=%s",
                           path.split("?", 1)[0], retry_after.strip()[:10] or "-")
            raise OrgResultError(
                ("MycoMap.org's search limit was reached; please try again shortly."
                 if path == "submit" else
                 "MycoMap.org's rerun limit was reached; Dikarya will try again shortly."),
                502,
            ) from exc
        if exc.code in (401, 403):
            if authenticated:
                if path == "submit":
                    _remember_search_refusal()
                logger.error(
                    "event=mycomap_org.auth_refused status=%s path=%s "
                    "key_env=MYCOMAP_ORG_API_KEY key_len=%s key_sha256=%s",
                    exc.code, path.split("?", 1)[0], len(key),
                    hashlib.sha256(key.encode()).hexdigest()[:12],
                )
                message = (
                    "MycoMap.org is not accepting new BLAST searches from Dikarya. "
                    "You can use NCBI BLAST meanwhile."
                    if path == "submit" else
                    f"MycoMap.org did not accept Dikarya's access key (HTTP {exc.code})."
                )
            else:
                message = (f"MycoMap.org refused access to this BLAST result "
                           f"(HTTP {exc.code}).")
            raise OrgResultError(message, 502, retryable=False) from exc
        raise OrgResultError(
            "MycoMap.org has no matching BLAST result." if exc.code == 404
            else "MycoMap.org could not complete the BLAST request.",
            404 if exc.code == 404 else 502,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OrgResultError("MycoMap.org is temporarily unavailable.") from exc
    if len(data) > limit:
        raise OrgResultError("MycoMap.org returned an oversized result.")
    return data


def _json(path, *, deadline=None):
    try:
        value = json.loads(_read(path, deadline=deadline))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrgResultError("MycoMap.org returned invalid status data.") from exc
    if not isinstance(value, dict):
        raise OrgResultError("MycoMap.org returned invalid status data.")
    return value


def resolve_result_id(reference):
    """Resolve an org observation/sequence URL to a BLAST job ID."""
    if reference["kind"] == "mycoblast":
        return reference["result_id"]
    sequence_id = reference["sequence_id"]
    metadata = _json(f"sequence-metadata/{sequence_id}")
    observation_id = reference["observation_id"]
    linked_ids = {
        str(metadata.get("inatObservationId") or ""),
        str(metadata.get("moObservationId") or ""),
    }
    if observation_id not in linked_ids:
        raise OrgResultError("That MycoMap sequence does not belong to the linked observation.", 404)
    link = str((metadata.get("blastMetadata") or {}).get("MM_Blast_Link") or "")
    match = _JOB_LINK.search(link)
    if not match:
        match = _LOCAL_XML_ID.search(str(metadata.get("localBlastXml") or ""))
    if not match:
        raise OrgResultError("MycoMap has not published BLAST results for this sequence yet.", 409)
    return match.group(1)


def status(result_id):
    return _json(result_id)


def _search_query(query):
    """Normalize one raw/FASTA query or an individual GenBank accession.

    Match the public MycoBLAST form's bounds before creating an upstream job.
    Headers and comments are discarded locally rather than sent to MycoMap.
    """
    from app.services.fasta_utils import is_insdc_master_accession

    if not isinstance(query, str) or not query.strip():
        raise ValueError("Enter a DNA sequence or GenBank accession to search.")
    query = query.strip()
    if is_genbank_accession(query):
        if is_insdc_master_accession(query):
            raise ValueError(
                "That accession is a project record with no sequence; enter an "
                "individual sequence accession instead."
            )
        return "genbank", query.upper()
    lines = [line.strip() for line in query.splitlines() if line.strip()]
    if sum(line.startswith(">") for line in lines) > 1:
        raise ValueError("MycoMap BLAST searches one sequence at a time; paste a single FASTA record.")
    sequence = re.sub(r"[\s\d]", "", "".join(
        line for line in lines if not line.startswith((">", ";"))
    )).upper()
    if not sequence or not re.fullmatch(r"[ACGTURYKMSWBDHVN]+", sequence):
        raise ValueError("Enter a DNA sequence containing only nucleotide codes, or a GenBank accession.")
    sequence = sequence.replace("U", "T")
    if not any(base in sequence for base in "ACGT"):
        raise ValueError("The sequence needs at least one A, C, G or T base.")
    if len(sequence) < SEARCH_MIN_QUERY_LENGTH:
        raise ValueError(f"MycoMap BLAST needs at least {SEARCH_MIN_QUERY_LENGTH} sequence bases.")
    if len(sequence) > SEARCH_MAX_QUERY_LENGTH:
        raise ValueError(f"MycoMap BLAST accepts up to {SEARCH_MAX_QUERY_LENGTH:,} sequence bases.")
    return "sequence", sequence


def search_from_query(query, *, min_identity=90.0, max_sequences=50,
                      time_budget=SEARCH_REQUEST_BUDGET_SECONDS):
    """Search MycoMap.org's local database and return import-ready full hits.

    ``submit`` creates a BLAST search, not a persistent sequence record. The
    contract is the one used by MycoMap.org's public MycoBLAST page: type/input,
    limit and localOnly. The installed server key stays on this server. A
    single deadline covers submission, polling, XML and full-sequence fetches.
    """
    query_type, normalized_query = _search_query(query)
    try:
        identity = float(min_identity)
    except (TypeError, ValueError):
        identity = 90.0
    if not math.isfinite(identity):
        identity = 90.0
    identity = max(50.0, min(100.0, identity))
    try:
        limit = int(max_sequences)
    except (TypeError, ValueError, OverflowError):
        limit = 50
    limit = max(1, min(500, limit))
    try:
        budget = float(time_budget)
    except (TypeError, ValueError):
        budget = 0
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("MycoMap BLAST needs a positive request time budget.")
    deadline = time.monotonic() + budget

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("MycoMap.org BLAST did not finish in time.")
        return seconds

    try:
        submitted = json.loads(_read(
            "submit", method="POST", authenticated=True, deadline=deadline,
            body={"type": query_type, "input": normalized_query,
                  "limit": rerun_max_hits(limit), "localOnly": True},
        ))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrgResultError("MycoMap.org returned an invalid search response.") from exc
    result_id = str(submitted.get("job_id") or "") if isinstance(submitted, dict) else ""
    if not re.fullmatch(r"[1-9]\d{0,11}", result_id):
        raise OrgResultError("MycoMap.org did not return a valid BLAST search ID.")
    logger.info("event=mycomap_org.search_submitted result_id=%s query_type=%s "
                "max_hits=%s min_identity=%s", result_id, query_type, limit, identity)

    while True:
        remaining()
        record = _json(result_id, deadline=deadline)
        local = record.get("local") or {}
        if not isinstance(local, dict):
            raise OrgResultError("MycoMap.org returned invalid local BLAST status.")
        local_state = str(local.get("status") or "").lower()
        overall_state = str(record.get("status") or "").lower()
        if local_state in {"failed", "failure", "error"} or overall_state in {
            "failed", "failure", "error"
        }:
            raise OrgResultError("MycoMap.org could not complete the local BLAST search.",
                                 retryable=False)
        if local_state not in {"queued", "pending", "running", "processing"}:
            if local.get("has_results"):
                break
            if local_state in {"done", "complete", "completed"} or overall_state in {
                "done", "complete", "completed"
            }:
                raise OrgResultError("MycoMap.org finished the search without publishing BLAST results.")
        time.sleep(min(SEARCH_POLL_INTERVAL_SECONDS, remaining()))

    result = fetch_results(result_id, include_ncbi=False, include_local=True,
                           time_budget=remaining())
    remaining()
    if result.get("failed_sources") or result.get("pending_sources") or result.get("errors"):
        raise OrgResultError("MycoMap.org could not load the completed local BLAST results.")
    result["sequences"] = [
        hit for hit in result.get("sequences", [])
        if hit.get("identity") is not None and hit["identity"] >= identity
    ][:limit]
    result["local_count"] = len(result["sequences"])
    result["result_id"] = result_id
    result["result_url"] = f"https://mycomap.org/mycoblast/{result_id}"
    return result


def rerun_pending(details):
    """Check requested rerun sources against the saved pre-rerun XML dates.

    Every source is checked on each call, and its state is recorded in
    ``details["org_source_states"]`` ("running", "done" or "failed") so a
    caller can report a local rerun that finished while NCBI is still running.
    """
    sources = list(details.get("org_wait_sources") or [])
    if not sources:
        return False
    record = status(str(details["result_id"]))
    before = details.get("org_before_dates") or {}
    states = details.setdefault("org_source_states", {})
    pending = False
    for source in sources:
        current = record.get(source) or {}
        state = current.get("status")
        if state in {"queued", "pending", "running", "processing"}:
            states[source] = "running"
            pending = True
            continue
        if state in {"failed", "error"} or not current.get("has_results"):
            if source == "ncbi":
                states[source] = "failed"
                raise OrgResultError("MycoMap.org NCBI rerun finished without results.",
                                     retryable=False)
            if details.get("local_status") != "failed":
                details.setdefault("warnings", []).append(
                    "MycoMap.org local rerun did not finish; using any saved local results."
                )
                details["local_status"] = "failed"
            states[source] = "failed"
            continue
        old_date = before.get(source)
        if old_date and current.get("xml_date") == old_date:
            states[source] = "running"
            pending = True
            continue
        details[f"{source}_status"] = "available" if source == "ncbi" else "completed"
        states[source] = "done"
    if pending:
        return True
    details["org_wait_sources"] = []
    return False


# How long a local rerun is polled in place before the caller falls back to a
# deferred wait. Every logged local rerun has published at the first check,
# 3.3-4.2 s after the request (event=mycomap_org.local_rerun_wait).
LOCAL_RERUN_INLINE_WAIT_SECONDS = 30


def local_rerun_poll_delay(elapsed):
    """Seconds until the next check of a MycoMap.org local rerun.

    Most local reruns publish in 3-4 s, so the first check waits 3 s, then
    every second until 10 s, then every 3 s. Tune against the elapsed_seconds
    that event=mycomap_org.local_rerun_wait records.
    """
    if elapsed < 3:
        return 3 - elapsed
    return 1 if elapsed < 10 else 3


def wait_for_local_rerun(details, on_check=None):
    """Poll a requested local rerun for up to LOCAL_RERUN_INLINE_WAIT_SECONDS.

    Updates ``details`` exactly as rerun_pending() does and returns
    ``(outcome, elapsed_seconds, checks)``; outcome is "ready", "failed",
    "error" (a check raised; the deferred path reports it) or "deferred".
    An NCBI rerun alongside is left pending: it takes minutes, not seconds.
    """
    started = time.monotonic()
    checks = 0
    outcome = "deferred"
    while True:
        elapsed = time.monotonic() - started
        delay = local_rerun_poll_delay(elapsed)
        if elapsed + delay > LOCAL_RERUN_INLINE_WAIT_SECONDS:
            break
        time.sleep(delay)
        checks += 1
        try:
            still_pending = rerun_pending(details)
        except OrgResultError:
            outcome = "error"
            break
        if on_check:
            on_check(details)
        if details.get("org_source_states", {}).get("local") != "running":
            outcome = "ready" if details.get("local_status") != "failed" else "failed"
            break
        if not still_pending:
            outcome = "ready"
            break
    return outcome, time.monotonic() - started, checks


def rerun_max_hits(limit):
    """The smallest hit count MycoMap.org accepts that covers ``limit``.

    Dikarya lets a user ask for 1-500 hits; the rerun endpoint takes only
    RERUN_MAX_HITS, so round up rather than return fewer than were asked for.
    """
    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = RERUN_MAX_HITS[0]
    return next((n for n in RERUN_MAX_HITS if n >= wanted), RERUN_MAX_HITS[-1])


def rerun(result_id, result_type, limit):
    if not str(result_id).isdigit() or result_type not in {"local", "ncbi"}:
        raise OrgResultError("Invalid MycoMap rerun request.", 400)
    try:
        result = json.loads(_read(
            "rerun", method="POST",
            body={"sequenceId": int(result_id), "rerunType": result_type,
                  "maxHits": rerun_max_hits(limit)},
            authenticated=True,
        ))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrgResultError("MycoMap.org returned an invalid rerun response.") from exc
    if not isinstance(result, dict):
        raise OrgResultError("MycoMap.org returned an invalid rerun response.")
    return result


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _percent(numerator, denominator):
    return round(100 * numerator / denominator, 2) if denominator else None


def _location(metadata):
    value = metadata.get("location") or ""
    if isinstance(value, str) and value.startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if isinstance(value, dict):
        return " ".join(str(value.get(key) or "").strip() for key in ("city", "region", "country")).strip()
    return str(value).strip()


def _parse_xml(data, source):
    # BLAST XML declares an external DTD. ElementTree never loads it; reject
    # entities as an additional guard before parsing upstream content.
    if b"<!ENTITY" in data.upper():
        raise OrgResultError("MycoMap.org returned unsafe BLAST XML.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OrgResultError("MycoMap.org returned invalid BLAST XML.") from exc
    query_length = _number(root.findtext("BlastOutput_query-len"))
    hits = []
    for hit in root.findall(".//Hit"):
        hsps = hit.findall("./Hit_hsps/Hsp")
        if not hsps:
            continue
        hsp = max(hsps, key=lambda item: _number(item.findtext("Hsp_score")))
        subject_length = _number(hit.findtext("Hit_len"))
        aligned = _number(hsp.findtext("Hsp_align-len"))
        hit_from = _number(hsp.findtext("Hsp_hit-from"))
        hit_to = _number(hsp.findtext("Hsp_hit-to"))
        sequence = re.sub(r"[^ACGTNRYSWKMBDHV]", "", (hsp.findtext("Hsp_hseq") or "").upper())
        if not sequence:
            continue
        hits.append({
            "id": (hit.findtext("Hit_id") or "").strip(),
            "accession": (hit.findtext("Hit_accession") or "").strip(),
            "description": (hit.findtext("Hit_def") or "").strip(),
            "subject_length": subject_length,
            "sequence": sequence,
            "identity": _percent(_number(hsp.findtext("Hsp_identity")), aligned),
            "query_cover": _percent(abs(_number(hsp.findtext("Hsp_query-to")) - _number(hsp.findtext("Hsp_query-from"))) + 1, query_length),
            "subject_cover": _percent(abs(hit_to - hit_from) + 1, subject_length),
            "source": source,
        })
    return hits


def _local_metadata(ids, *, deadline=None):
    """Fetch full local sequences and names in batches; XML remains fallback."""
    output = {}
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        query = ",".join(batch)
        try:
            payload = _json(f"mycomap/sequences/batch?ids={query}", deadline=deadline)
        except OrgResultError:
            logger.warning("MycoMap.org local metadata was unavailable", exc_info=True)
            continue
        for item in payload.get("sequences") or []:
            if isinstance(item, dict) and str(item.get("id") or "") in batch:
                output[str(item["id"])] = item
    return output


def _is_transient_ncbi_failure(exc):
    """True when NCBI did not answer, so the same request may work later.

    A 4xx refusal, an oversized batch or undecodable text fails the same way on
    every retry; counting those as transient would keep the delayed recheck
    retrying until it gave up and discarded every sequence that did arrive.
    """
    import requests

    if isinstance(exc, (requests.ConnectionError, requests.Timeout,
                        requests.exceptions.RetryError,
                        requests.exceptions.ChunkedEncodingError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _ncbi_sequences(accessions, *, deadline=None):
    """Fetch complete NCBI records in batches, keyed by the requested accession.

    Returns ``(sequences, unchecked)``. ``unchecked`` holds the accessions never
    answered because their batch failed transiently or the deadline passed.
    Those are worth retrying, unlike an accession NCBI answered without a
    usable record or a batch it refused outright.
    """
    from app.services.blast_service import NCBI_EFETCH_URL, _ncbi_request

    requested = list(dict.fromkeys(
        accession.upper() for accession in accessions if is_genbank_accession(accession)
    ))
    sequences = {}
    unchecked = set()
    for offset in range(0, len(requested), NCBI_BATCH_SIZE):
        batch = requested[offset:offset + NCBI_BATCH_SIZE]
        remaining = deadline - time.monotonic() if deadline is not None else 30
        if remaining <= 0:
            unchecked.update(requested[offset:])
            break
        try:
            response = _ncbi_request(
                "POST", NCBI_EFETCH_URL, max_retries=2, deadline=deadline,
                data={"db": "nuccore", "id": ",".join(batch),
                      "rettype": "fasta", "retmode": "text"},
                timeout=(min(5, remaining), min(20, remaining)), stream=True,
            )
            try:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_XML_BYTES:
                        raise ValueError("NCBI FASTA batch is too large")
                    chunks.append(chunk)
                fasta = b"".join(chunks).decode("utf-8")
            finally:
                response.close()
        except Exception as exc:
            logger.warning("NCBI full sequence fetch failed for %s accessions",
                           len(batch), exc_info=True)
            if _is_transient_ncbi_failure(exc):
                unchecked.update(batch)
            continue

        returned = {}
        for header, raw_sequence in parse_fasta_records(fasta):
            token = header.split(None, 1)[0].upper() if header else ""
            for part in token.split("|"):
                if is_genbank_accession(part):
                    sequence = re.sub(r"[^ACGTNRYSWKMBDHV]", "", raw_sequence.upper())
                    if sequence:
                        returned[part] = sequence
        for accession in batch:
            # A versioned BLAST hit must match that version. Bare accessions may
            # match the version NCBI puts in the FASTA header.
            sequence = returned.get(accession)
            if sequence is None and "." not in accession:
                sequence = next((value for key, value in returned.items()
                                 if key.split(".")[0] == accession), None)
            if sequence:
                sequences[accession] = sequence
    logger.info("event=import.source_summary operation=mycomap_full_sequences source=ncbi expected=%s fetched=%s omitted=%s unchecked=%s",
                len(requested), len(sequences), len(requested) - len(sequences), len(unchecked))
    return sequences, unchecked


CURATION_BATCH_SIZE = 100
# The GenBank qualifiers that end the organism at the start of a defline, as in
# "Geoglossum sp. SA-2022a voucher AMI-SPL642 small subunit ...".
_DEFLINE_QUALIFIER = re.compile(
    r"\s(?:voucher|isolate|strain|culture|clone|specimen|haplotype|"
    r"culture-collection|cultivar|genotype|from)\s", re.I)


def _curated_genbank_names(accessions, *, deadline=None):
    """MycoMap's curated name for each GenBank accession, keyed by accession.

    POST mycoblast/genbank-curation is public and is what MycoMap.org's own
    BLAST page labels NCBI hits with: curators rename GenBank records there
    (OM691497 "Geoglossum sp. SA-2022a" -> "Geoglossum laurisilvae") without
    GenBank changing. Best effort -- on failure the GenBank names stand.
    """
    names = {}
    unique = list(dict.fromkeys(a for a in accessions if a))
    for offset in range(0, len(unique), CURATION_BATCH_SIZE):
        batch = unique[offset:offset + CURATION_BATCH_SIZE]
        try:
            payload = json.loads(_read("genbank-curation", method="POST",
                                       body={"accessions": batch}, deadline=deadline))
        except (OrgResultError, ValueError, UnicodeDecodeError):
            logger.warning("event=mycomap_org.genbank_curation_unavailable count=%s",
                           len(batch), exc_info=True)
            break
        results = payload.get("results") if isinstance(payload, dict) else None
        for key, row in (results or {}).items():
            name = str((row or {}).get("scientificName") or "").strip() if isinstance(row, dict) else ""
            if name:
                names[str(key).upper().split(".")[0]] = name
    return names


# The organism at the start of a GenBank label: a genus (or "uncultured X"),
# optionally sp./cf./aff./nr. and one epithet or quoted provisional name, and an
# optional infraspecific rank. Deliberately stops there, so a voucher written
# straight after the name ("Geoglossum squamulosum SAV F-11677") is not taken
# for part of it and dropped.
_LABEL_ORGANISM = re.compile(r"""
    ^(?:[Uu]ncultured\s+)?[A-Z][A-Za-z]+(?:-[a-z]+)?
    (?:
        \s+(?:sp|cf|aff|nr)\.(?:\s+(?:'[^']*'|"[^"]*"|\S+))?
      | \s+(?:'[^']*'|"[^"]*"|[a-z][a-z-]*)
    )?
    (?:\s+(?:var|subsp|f)\.\s+[a-z][a-z-]*)?
""", re.X)


def apply_curated_genbank_name(display_name, accession, curated_name):
    """Swap the GenBank organism in an NCBI hit label for MycoMap's curated name.

    A curated name is trusted even when it moves the record to another genus:
    someone who went to the trouble of curating it is probably right. Returns
    the new label, or None when the label already carries that name or does not
    visibly begin with an organism (the GenBank label is then kept).
    """
    curated = " ".join(str(curated_name or "").split())
    prefix = f"{accession} "
    if not curated or not display_name.startswith(prefix):
        return None
    body = display_name[len(prefix):]
    qualifier = _DEFLINE_QUALIFIER.search(f" {body} ")
    head = body[:max(qualifier.start() - 1, 0)] if qualifier else body
    match = _LABEL_ORGANISM.match(head)
    if not match:
        return None
    organism = match.group(0)
    if organism.casefold() == curated.casefold():
        return None
    return f"{accession} {curated}{body[len(organism):]}".strip()


REFRESH_REQUEST_TIMEOUT = 45
REFRESH_BUDGET_SECONDS = 150
REFRESH_WORKERS = 3
OBSERVATION_BATCH_SIZE = 50


def observation_records(references, *, deadline=None):
    """The name and location MycoMap.org holds for each inat:/mo: reference.

    sequences/batch?observations= is public and returns one row per sequence,
    so an observation with several sequences comes back several times; the
    lowest sequence id wins, so a before/after pair always compares one row.
    """
    rows = []
    for offset in range(0, len(references), OBSERVATION_BATCH_SIZE):
        query = ",".join(references[offset:offset + OBSERVATION_BATCH_SIZE])
        payload = _json(f"mycomap/sequences/batch?observations={query}", deadline=deadline)
        rows.extend(row for row in payload.get("sequences") or [] if isinstance(row, dict))
    rows.sort(key=lambda row: _number(row.get("id")))
    by_reference = {}
    for row in rows:
        for prefix, key in (("inat", "inatObservationId"), ("mo", "moObservationId")):
            value = str(row.get(key) or "").strip()
            if value.isdigit():
                by_reference.setdefault(f"{prefix}:{value}", row)
    summaries = {}
    for reference in references:
        row = by_reference.get(reference)
        summaries[reference] = {
            "found": row is not None,
            "scientific_name": str((row or {}).get("scientificName") or "").strip(),
            "location": _location(row) if row else "",
        }
    return summaries


def observation_sequences(reference, *, deadline=None):
    """Return the public MycoMap sequence rows attached to one observation.

    The public batch endpoint is also used by :func:`observation_records`, but
    callers resolving a tree tip need the sequence ID and bases so they can
    distinguish several reads attached to the same observation.  Keep the
    fixed-path validation in ``_read`` and accept only the two observation
    reference forms Dikarya itself emits.
    """
    reference = str(reference or "").strip().lower()
    if not re.fullmatch(r"(?:inat|mo):\d{1,12}", reference):
        raise OrgResultError("Invalid MycoMap observation reference.", 400,
                             retryable=False)
    payload = _json(f"mycomap/sequences/batch?observations={reference}",
                    deadline=deadline)
    return [
        row for row in (payload.get("sequences") or [])
        if isinstance(row, dict)
    ]


def _refresh_observation(reference, deadline):
    return _read("admin/mycomap/refresh", method="POST", body={"observation": reference},
                 authenticated=True, deadline=deadline, timeout=REFRESH_REQUEST_TIMEOUT)


def refresh_observations(references):
    """Refresh observations from their source, then sync them onto their sequences.

    Mirrors MycoMap.org's own Refresh button: admin/mycomap/refresh pulls each
    observation from iNaturalist or Mushroom Observer, and sync-batch copies the
    refreshed name onto the observation's sequences -- which is where
    sequences/batch, and so every Dikarya label, reads it from. Returns a list
    of warnings; raises OrgResultError only when nothing could be refreshed.
    """
    from concurrent.futures import ThreadPoolExecutor

    warnings = []
    deadline = time.monotonic() + REFRESH_BUDGET_SECONDS
    refused = None
    failed = []
    # The first call alone, so a refused key is reported once rather than by
    # every worker thread at the same moment.
    try:
        _refresh_observation(references[0], deadline)
    except OrgResultError as exc:
        if exc.retryable:
            failed.append(references[0])
        else:
            refused = exc
    if refused is None and len(references) > 1:
        def attempt(reference):
            try:
                _refresh_observation(reference, deadline)
            except OrgResultError:
                logger.warning("event=mycomap_org.refresh_failed reference=%s",
                               reference, exc_info=True)
                return reference
            return None
        with ThreadPoolExecutor(max_workers=REFRESH_WORKERS) as pool:
            failed.extend(ref for ref in pool.map(attempt, references[1:]) if ref)
    if refused is not None:
        warnings.append(f"{refused} Names were synced from MycoMap's stored "
                        "observations without re-reading iNaturalist or Mushroom Observer.")
    elif failed:
        warnings.append("MycoMap.org could not refresh " + ", ".join(failed[:10])
                        + (" and others" if len(failed) > 10 else "")
                        + " from the source site.")

    try:
        _read("sync-batch", method="POST",
              body={"action": "sync", "observations": list(references)},
              authenticated=True, deadline=time.monotonic() + REFRESH_REQUEST_TIMEOUT,
              timeout=REFRESH_REQUEST_TIMEOUT)
    except OrgResultError:
        if refused is not None or len(failed) == len(references):
            raise
        logger.warning("event=mycomap_org.sync_failed count=%s", len(references), exc_info=True)
        warnings.append("MycoMap.org refreshed the observations but could not update "
                        "their sequence records, so some names may still be the old ones.")
    return warnings


def fetch_results(result_id, *, include_ncbi=True, include_local=True,
                  time_budget=None):
    """Return normalized hits and source state for the shared import pipeline."""
    # Imported here: mycomap_service imports this module lazily as well.
    from app.services.mycomap_service import describe_ncbi_hit

    deadline = time.monotonic() + time_budget if time_budget is not None else None
    record = _json(result_id, deadline=deadline)
    result = {"sequences": [], "ncbi_count": 0, "local_count": 0,
              "errors": [], "failed_sources": [], "pending_sources": [],
              "ncbi_queue_position": None, "metrics_by_key": {}}
    for source in ("ncbi", "local"):
        if not (include_ncbi if source == "ncbi" else include_local):
            continue
        source_status = record.get(source) or {}
        if source == "ncbi":
            result["ncbi_queue_position"] = source_status.get("queue_position")
        if source_status.get("status") in {"queued", "pending", "running", "processing"}:
            result["pending_sources"].append(source)
            continue
        if not source_status.get("has_results"):
            if source_status.get("status") in {"failed", "error"}:
                result["failed_sources"].append(source)
                result["errors"].append(f"{source} BLAST failed on MycoMap.org")
            continue
        try:
            data = _read(f"{result_id}/{source}", limit=MAX_XML_BYTES,
                         deadline=deadline)
            hits = _parse_xml(data, source)
        except OrgResultError as exc:
            result["failed_sources"].append(source)
            result["errors"].append(str(exc))
            continue
        if source == "local":
            metadata = _local_metadata([h["id"] for h in hits if h["id"].isdigit()],
                                       deadline=deadline)
            curated_names = {}
        else:
            metadata = {}
            ncbi_sequences, _unchecked = _ncbi_sequences(
                [h["accession"] for h in hits], deadline=deadline)
            # Hit_len identifies the sequence whose BLAST metrics we received.
            # A changed or incomplete NCBI record must not inherit those metrics.
            for hit in hits:
                sequence = ncbi_sequences.get(hit["accession"].upper())
                if sequence and (not hit["subject_length"] or
                                 len(sequence) == hit["subject_length"]):
                    hit["full_sequence"] = sequence
            missing = sum("full_sequence" not in hit for hit in hits)
            if missing:
                result["errors"].append(
                    f"Could not fetch full NCBI sequences for {missing} BLAST hits"
                )
                # Even one missing full record leaves an incomplete result.
                # The delayed reconciler retains available hits after its
                # bounded retries rather than declaring NCBI complete.
                result["failed_sources"].append(source)
            curated_names = _curated_genbank_names(
                [hit["accession"] for hit in hits if "full_sequence" in hit],
                deadline=deadline)
        for hit in hits:
            item = metadata.get(hit["id"], {})
            accession = hit["accession"] if source == "ncbi" else ""
            if source == "ncbi" and "full_sequence" not in hit:
                continue
            taxon = str(item.get("scientificName") or "").strip()
            observation_token = (
                f"iNat{item['inatObservationId']}" if item.get("inatObservationId")
                else f"MO #{item['moObservationId']}" if item.get("moObservationId")
                else ""
            )
            raw_description = ""
            if accession:
                # The label the mycomap.com path gives the same accession, or
                # the tip label is the whole GenBank definition line. BLAST XML
                # joins the deflines of identical subjects with " >"; only the
                # first names this hit.
                raw_description = hit["description"].split(" >", 1)[0].strip()
                described = describe_ncbi_hit(accession, raw_description)
                taxon = taxon or described["taxon"]
                name = described["display_name"]
                curated = curated_names.get(accession.upper().split(".")[0])
                curated_label = apply_curated_genbank_name(name, accession, curated)
                if curated_label:
                    name, taxon = curated_label, curated
            else:
                # MycoMap.org identifies local hits with its own sequence ID.
                # A bare numeric ID such as ``833182`` looks exactly like a
                # GenBank accession with its letter prefix stripped, which led
                # users to conclude both that local hits were absent and that
                # GenBank labels had been damaged. Keep the upstream ID, but
                # make its namespace explicit in every exported tree label.
                name = (
                    f"MycoMap#{hit['id']} {observation_token} "
                    f"{taxon or hit['description']}"
                )
            name = name.strip()
            if source == "ncbi":
                sequence = hit["full_sequence"]
            else:
                sequence = (re.sub(r"[^ACGTNRYSWKMBDHV]", "",
                                   str(item.get("sequence") or "").upper())
                            or hit["sequence"])
            result["sequences"].append({
                "name": name, "sequence": sequence, "source": "mycomap",
                "hit_source": source, "accession": accession, "taxon": taxon,
                "mycomap_sequence_id": hit["id"] if source == "local" else "",
                "raw_ncbi_description": raw_description,
                "location": _location(item), "identity": hit["identity"],
                "query_cover": hit["query_cover"], "subject_cover": hit["subject_cover"],
                "is_contaminant": any("contaminant" in str(flag).lower()
                                      for flag in (item.get("flags") or [])),
                "blast_metrics_available": True,
            })
        result[f"{source}_count"] = sum(
            sequence["hit_source"] == source for sequence in result["sequences"]
        )
        logger.info("event=import.source_summary operation=mycomap_org source=%s expected=%s fetched=%s omitted=%s",
                    source, len(hits), result[f"{source}_count"],
                    max(0, len(hits) - result[f"{source}_count"]))
    return result
