import hashlib
import json
import logging
import re
import time
import requests
import threading
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple, Any
from app.config import Config

logger = logging.getLogger(__name__)

NCBI_BLAST_URL = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
NCBI_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Global rate limiter state
_ncbi_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_GAP = 1.0 / 3.0  # At least 0.34s between requests (~3 req/s)
# Wall-clock ceiling for re-fetching a rejected batch one accession at a time.
# 50 ids at the rate limit cost ~17s when NCBI is healthy; this only bites when
# it is not, and it is what keeps the pass inside nginx's 300s read timeout.
_ISOLATION_BUDGET_SECONDS = 60.0

# How many hits a BLAST search returns when the caller names no limit. Both
# public entry points already defaulted to this; it is named here so the
# submission (HITLIST_SIZE) and the parse cannot drift apart again.
DEFAULT_MAX_SEQUENCES = 50


def _normalize_max_sequences(value) -> int:
    """Coerce a requested hit limit to a usable positive integer."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SEQUENCES
    return limit if limit > 0 else DEFAULT_MAX_SEQUENCES

def _ncbi_request(method: str, url: str, max_retries: int = 5, **kwargs) -> requests.Response:
    """
    Helper to perform NCBI requests with:
    - Global rate limiting (min gap between requests)
    - User-Agent header
    - Retries with exponential backoff for suitable errors
    - Respect for Retry-After headers

    ``max_retries`` is settable so a caller making many requests in a row can
    stop each one from multiplying an NCBI outage by its own backoff schedule.
    """
    global _last_request_time
    
    # 1. Apply Rate Limit
    with _ncbi_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_GAP:
            sleep_time = _MIN_REQUEST_GAP - elapsed
            time.sleep(sleep_time)
        _last_request_time = time.monotonic()

    # 2. Prepare headers
    headers = kwargs.get("headers", {})
    # Use a descriptive User-Agent
    if "User-Agent" not in headers:
        headers["User-Agent"] = f"Dikarya/1.0 ({Config.BLAST_EMAIL})"
    kwargs["headers"] = headers
    
    # 3. Execute with Retries
    # Retry on: Connection errors, Timeouts, HTTP 429, HTTP 5xx
    # Do NOT retry on: HTTP 4xx (except 429)
    max_retries = max(1, int(max_retries))
    backoff = 1.0  # Start with 1s
    
    for attempt in range(max_retries):
        try:
            response = requests.request(method, url, **kwargs)
            
            # Check for 429 Too Many Requests
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = int(retry_after)
                    except ValueError:
                        wait_time = backoff
                else:
                    wait_time = backoff
                
                logger.warning(f"NCBI 429 (Too Many Requests). Waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                time.sleep(wait_time)
                backoff *= 2 # Exponential backoff for next time if needed
                continue
                
            # Check for 5xx Server Errors
            if 500 <= response.status_code < 600:
                logger.warning(f"NCBI {response.status_code} Error. Waiting {backoff}s before retry {attempt + 1}/{max_retries}")
                time.sleep(backoff)
                backoff *= 2
                continue
            
            # For other statuses, letting raise_for_status handle or return response
            # Note: We return response here and let caller decide to raise for 4xx
            return response
            
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.warning(f"NCBI Connection/Timeout error: {e}. Waiting {backoff}s before retry {attempt + 1}/{max_retries}")
            time.sleep(backoff)
            backoff *= 2
            
    # Final attempt (or if we ran out of retries)
    # If we exited loop, try one last time or re-raise
    raise requests.exceptions.RetryError(f"Max retries ({max_retries}) exceeded for NCBI request to {url}")


def blast_from_sequence(seq: str, config: Config, min_identity: float = 90.0,
                        max_sequences: int = DEFAULT_MAX_SEQUENCES, logger=logger) -> Dict:
    """
    Perform remote BLAST using the given nucleotide sequence.
    Use caching if available.
    """
    query_hash = _hash_query(seq, min_identity, max_sequences)
    cached_result = _check_cache(query_hash, config)
    if cached_result:
        logger.info(f"BLAST cache hit for hash {query_hash}")
        return cached_result

    logger.info(f"BLAST cache miss for hash {query_hash}. Submitting to NCBI.")
    
    try:
        rid, rtoe = _submit_blast_request(seq, config, min_identity, max_sequences)
        logger.info(f"BLAST submitted. RID: {rid}, RTOE: {rtoe}")
        
        _poll_blast(rid, rtoe, config, logger)
        
        blast_result = _fetch_blast_results(rid, max_sequences)
        hit_accessions = blast_result.get("accessions", [])
        hit_details = blast_result.get("hit_details", [])
        logger.info(f"BLAST finished. Found {len(hit_accessions)} hits.")
        
        fasta_content = fetch_fasta_for_accessions(hit_accessions)
        logger.info(f"Downloaded FASTA for {len(hit_accessions)} accessions.")
        
        return _save_cache(query_hash, hit_accessions, hit_details, fasta_content, config)
        
    except Exception as e:
        logger.error(f"BLAST failed: {e}")
        # Re-raise to let caller handle failure (e.g. show user error)
        raise

def blast_from_accessions(accessions: List[str], config: Config, min_identity: float = 90.0,
                          max_sequences: int = DEFAULT_MAX_SEQUENCES, logger=logger) -> Dict:
    """
    Retrieve sequences for the provided accessions.
    Then run remote BLAST using the combined sequence.
    Use caching.
    """
    # Sort to ensure stable hash
    sorted_accessions = sorted(accessions)
    query_str = ",".join(sorted_accessions)
    query_hash = _hash_query(query_str, min_identity, max_sequences)
    
    cached_result = _check_cache(query_hash, config)
    if cached_result:
        logger.info(f"BLAST cache hit for accessions hash {query_hash}")
        return cached_result

    logger.info(f"BLAST cache miss for accessions {query_hash}. Fetching sequences first.")
    
    try:
        # 1. Fetch initial sequences to use as query
        query_fasta = fetch_fasta_for_accessions(sorted_accessions)
        
        # 2. Run BLAST with these sequences
        rid, rtoe = _submit_blast_request(query_fasta, config, min_identity, max_sequences)
        logger.info(f"BLAST submitted. RID: {rid}, RTOE: {rtoe}")
        
        _poll_blast(rid, rtoe, config, logger)
        
        blast_result = _fetch_blast_results(rid, max_sequences)
        hit_accessions = blast_result.get("accessions", [])
        hit_details = blast_result.get("hit_details", [])
        logger.info(f"BLAST finished. Found {len(hit_accessions)} hits.")
        
        # Combine original accessions with hits to ensure we have everything
        all_accessions = list(set(sorted_accessions + hit_accessions))
        
        # Also add original accessions to hit_details if not present
        existing_accs = {h["accession"] for h in hit_details}
        for acc in sorted_accessions:
            if acc not in existing_accs:
                hit_details.insert(0, {"accession": acc, "organism": "(query)"})
        
        fasta_content = fetch_fasta_for_accessions(all_accessions)
        logger.info(f"Downloaded FASTA for {len(all_accessions)} accessions.")
        
        return _save_cache(query_hash, all_accessions, hit_details, fasta_content, config)

    except Exception as e:
        logger.error(f"BLAST from accessions failed: {e}")
        raise

def _hash_query(query_str: str, min_identity: float = 90.0, max_sequences: int = 50) -> str:
    raw = f"{query_str}|{min_identity}|{max_sequences}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _check_cache(query_hash: str, config: Config) -> Optional[Dict]:
    # Validate hash format (SHA-256 = 64 hex chars)
    if not re.match(r'^[0-9a-f]{64}$', query_hash):
        return None
        
    cache_dir = config.BLAST_CACHE_DIR
    json_path = cache_dir / f"{query_hash}.json"
    fasta_path = cache_dir / f"{query_hash}.fasta"
    
    if json_path.exists() and fasta_path.exists():
        try:
            with open(json_path, 'r') as f:
                metadata = json.load(f)
            return {
                "cached": True,
                "sequence_count": metadata["sequence_count"],
                "hit_accessions": metadata["hit_accessions"],
                "hit_details": metadata.get("hit_details", []),  # Include organism info
                "fasta_path": str(fasta_path),
                "metadata_path": str(json_path)
            }
        except Exception:
            return None
    return None

def _save_cache(query_hash: str, hit_accessions: List[str], hit_details: List[Dict], fasta_content: str, config: Config) -> Dict:
    cache_dir = config.BLAST_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    json_path = cache_dir / f"{query_hash}.json"
    fasta_path = cache_dir / f"{query_hash}.fasta"
    
    metadata = {
        "query_hash": query_hash,
        "sequence_count": len(hit_accessions),
        "hit_accessions": hit_accessions,
        "hit_details": hit_details,  # Include organism info
        "timestamp": time.time()
    }
    
    with open(json_path, 'w') as f:
        json.dump(metadata, f, indent=2)
        
    with open(fasta_path, 'w') as f:
        f.write(fasta_content)
        
    return {
        "cached": False,
        "sequence_count": len(hit_accessions),
        "hit_accessions": hit_accessions,
        "hit_details": hit_details,
        "fasta_path": str(fasta_path),
        "metadata_path": str(json_path)
    }

def _submit_blast_request(seq: str, config: Config = None, min_identity: float = 90.0, max_sequences: int = 50) -> Tuple[str, int]:
    """Submit a BLAST request to NCBI and return RID and estimated wait time."""
    from app.config import Config as DefaultConfig
    if config is None:
        config = DefaultConfig
    
    # Validate query length
    if len(seq) > config.BLAST_MAX_QUERY_LENGTH:
        raise ValueError(f"Query too long: {len(seq)} chars (max: {config.BLAST_MAX_QUERY_LENGTH})")
    
    # Sequence content is sensitive and high-volume; length + a stable digest
    # provide enough correlation without writing bases to logs.
    from app.services.log_context import stable_fingerprint
    logger.info(
        "event=blast.submitted Submitting BLAST request identity=%s max_hits=%s "
        "query_bases=%s query_fingerprint=%s",
        min_identity, max_sequences, len(seq), stable_fingerprint(seq),
    )
    
    params = {
        "CMD": "Put",
        "PROGRAM": "blastn",
        "DATABASE": "nt",
        "QUERY": seq,
        "HITLIST_SIZE": max_sequences,
        "ALIGNMENTS": max_sequences,
        "PERC_IDENT": min_identity,
        "FORMAT_TYPE": "JSON2",
        "EMAIL": config.BLAST_EMAIL
    }
    
    # Use _ncbi_request helper
    response = _ncbi_request("POST", NCBI_BLAST_URL, data=params, timeout=(10, 60))
    # Raise if 4xx/5xx (though 5xx handled by retry, final attempt might fail)
    response.raise_for_status()
    
    logger.debug("BLAST submission response_chars=%s", len(response.text))
    
    # Parse RID and RTOE from response text
    # Response usually contains: "    RID = ...\n    RTOE = ..."
    rid = None
    rtoe = 30
    
    for line in response.text.splitlines():
        if "RID =" in line:
            rid = line.split("=")[1].strip()
        if "RTOE =" in line:
            try:
                rtoe = int(line.split("=")[1].strip())
            except ValueError:
                pass
                
    if not rid:
        raise ValueError(f"Could not retrieve RID from NCBI BLAST submission. Response: {response.text[:500]}")
    
    logger.info(f"BLAST submission successful. RID={rid}, RTOE={rtoe}")
    return rid, rtoe

def _poll_blast(rid: str, rtoe: int, config: Config, logger) -> None:
    """Poll NCBI for BLAST job completion."""
    # 1. Wait initial RTOE
    logger.info(f"Waiting {rtoe} seconds for RTOE...")
    time.sleep(rtoe)
    
    start_time = time.time()
    max_wait = 600  # 10 minutes max
    
    # Use configured poll interval, but MINIMUM 60 seconds
    configured_interval = getattr(config, 'BLAST_POLL_INTERVAL_SECONDS', 60)
    poll_interval = max(60, configured_interval)
    
    # 2. Poll Loop
    while (time.time() - start_time) < max_wait:
        params = {
            "CMD": "Get",
            "FORMAT_OBJECT": "SearchInfo",
            "RID": rid
        }
        
        # Use helper
        try:
            response = _ncbi_request("GET", NCBI_BLAST_URL, params=params, timeout=(5, 30))
            # Don't strictly raise on 404/etc inside poll loop? Actually SearchInfo should exist.
            # If 404, maybe RID expired.
            if response.status_code != 200:
                logger.warning(f"Poll got status {response.status_code}. Retrying next cycle.")
                time.sleep(poll_interval)
                continue
                
            content = response.text
             # Log full poll response for debugging
            logger.debug("BLAST poll response RID=%s response_chars=%s", rid, len(content))
            
            if "Status=WAITING" in content:
                logger.info(f"BLAST Status: WAITING. Sleeping {poll_interval}s...")
                time.sleep(poll_interval)
                continue
            
            if "Status=FAILED" in content:
                raise RuntimeError(f"BLAST failed for RID {rid}")
                
            if "Status=UNKNOWN" in content:
                raise RuntimeError(f"BLAST RID {rid} expired or unknown")
                
            if "Status=READY" in content:
                if "ThereAreHits=yes" in content:
                    logger.info("BLAST Status: READY with hits.")
                else:
                    # Still try to fetch - sometimes this flag is wrong
                    logger.warning("BLAST Status: READY but ThereAreHits=no (will still attempt to fetch results).")
                return
                
        except Exception as e:
             logger.warning(f"Poll exception: {e}. Retrying next cycle.")
             time.sleep(poll_interval)
             continue

    raise TimeoutError("BLAST timed out waiting for results")

def _fetch_blast_results(rid: str, max_sequences: int = DEFAULT_MAX_SEQUENCES) -> Dict[str, List]:
    """Fetch BLAST results from NCBI. Handle both raw JSON and ZIP-compressed responses.

    `max_sequences` is the same limit that was sent to NCBI as HITLIST_SIZE. It
    has to be repeated here because the parse used to slice its output to a
    hard-coded 50, so a caller asking for 200 reference sequences quietly got
    50 -- a change in taxon sampling, not just a truncated list.

    Returns:
        Dict with 'accessions' (list of str) and 'hit_details' (list of dicts with accession/organism)
    """
    limit = _normalize_max_sequences(max_sequences)
    import io
    import zipfile
    
    params = {
        "CMD": "Get",
        "FORMAT_TYPE": "JSON2",
        "RID": rid
    }
    
    response = _ncbi_request("GET", NCBI_BLAST_URL, params=params, timeout=(10, 120))
    response.raise_for_status()
    
    content_type = response.headers.get('content-type', '')
    logger.info(f"BLAST result content-type: {content_type}, length: {len(response.content)} bytes")
    
    # Handle ZIP-compressed response (NCBI sometimes returns JSON in a ZIP)
    if 'application/zip' in content_type or response.content[:2] == b'PK':
        logger.info("Detected ZIP-compressed BLAST response, extracting...")
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                # Find the JSON file in the archive
                json_files = [f for f in zf.namelist() if f.endswith('.json')]
                logger.info(f"ZIP contains files: {zf.namelist()}")
                
                if not json_files:
                    logger.error("No JSON files found in ZIP archive")
                    return {"accessions": [], "hit_details": []}
                
                # Read the main JSON file (or first JSON ending with _1.json)
                main_json = None
                for jf in json_files:
                    if '_1.json' in jf:
                        main_json = jf
                        break
                if not main_json:
                    main_json = json_files[0]
                
                logger.info(f"Reading JSON from ZIP: {main_json}")
                content = zf.read(main_json).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to extract ZIP: {e}")
            return {"accessions": [], "hit_details": []}
    else:
        content = response.text
    
    logger.debug(f"BLAST JSON content length: {len(content)} chars")
    
    # Check if response looks like JSON
    if not content.strip().startswith('{') and not content.strip().startswith('['):
        from app.services.log_context import stable_fingerprint
        logger.warning(
            "event=blast.response_unexpected BLAST response was not JSON "
            "response_chars=%s response_fingerprint=%s",
            len(content), stable_fingerprint(content),
        )
        # Try to extract accessions from text-based format as fallback
        import re
        accession_pattern = r'\b([A-Z]{1,2}_?\d{6,}(?:\.\d+)?)\b'
        matches = re.findall(accession_pattern, content)
        if matches:
            unique_accessions = list(dict.fromkeys(matches))[:limit]
            logger.info(f"Extracted {len(unique_accessions)} accessions from text response")
            return {"accessions": unique_accessions, "hit_details": []}
        logger.warning("Could not extract accessions from non-JSON response")
        return {"accessions": [], "hit_details": []}
    
    try:
        data = json.loads(content)
        
        # Log top-level keys for debugging
        logger.info(f"BLAST JSON top-level keys: {list(data.keys())}")
        
        # Navigate JSON structure to find hits
        # BlastOutput2 can be either a dict (with 'report' key) or a list of dicts
        blast_output = data.get("BlastOutput2", {})
        
        if not blast_output:
            # Deliberately no debug dump. This used to write the decoded NCBI
            # response to a fixed /tmp/blast_debug_response.json, which every
            # concurrent job overwrote and which put upstream response data
            # outside the normal logging controls. The bounded fingerprint plus
            # the key list is enough to recognise a recurring malformed shape.
            from app.services.log_context import stable_fingerprint
            logger.warning(
                "event=blast.no_blastoutput2 BLAST response carried no "
                "BlastOutput2 top_level_keys=%s response_chars=%s "
                "response_fingerprint=%s",
                sorted(data.keys())[:20], len(content), stable_fingerprint(content),
            )
            return {"accessions": [], "hit_details": []}
        
        # Handle both dict and list formats
        if isinstance(blast_output, dict):
            # Direct structure: BlastOutput2 -> report -> results -> search -> hits
            report = blast_output.get("report", {})
        elif isinstance(blast_output, list) and len(blast_output) > 0:
            # List structure: BlastOutput2[0] -> report -> results -> search -> hits
            report = blast_output[0].get("report", {})
        else:
            logger.warning(f"Unexpected BlastOutput2 type: {type(blast_output)}")
            return {"accessions": [], "hit_details": []}
        
        logger.debug(f"Report keys: {list(report.keys())}")
        
        results = report.get("results", {})
        logger.debug(f"Results keys: {list(results.keys())}")
        
        search_results = results.get("search", {})
        logger.debug(f"Search results keys: {list(search_results.keys())}")
        
        hit_list = search_results.get("hits", [])
        
        logger.info(f"Found {len(hit_list)} hits in JSON response")
        
        if len(hit_list) == 0:
            message = search_results.get("message", "No message")
            logger.warning(f"No hits found. Search message: {message}")
        
        accessions = []
        hit_details = []  # List of {accession, organism} dicts
        for hit in hit_list:
            # description -> [ { "accession": "...", "sciname": "..." } ]
            descs = hit.get("description", [])
            if descs:
                desc = descs[0]
                acc = desc.get("accession")
                organism = desc.get("sciname", "")
                if acc:
                    accessions.append(acc)
                    hit_details.append({"accession": acc, "organism": organism})
                    
        logger.info(f"Extracted {len(accessions)} accessions from hits")
        return {"accessions": accessions[:limit], "hit_details": hit_details[:limit]}
        
    except json.JSONDecodeError as e:
        from app.services.log_context import stable_fingerprint
        logger.error(
            "event=blast.response_parse_failed JSON decode failed exception=%s "
            "response_bytes=%s response_fingerprint=%s",
            type(e).__name__, len(content), stable_fingerprint(content),
        )
        raise ValueError(f"Failed to parse BLAST JSON response: {e}")

def _fetch_genbank_xml_batch(accessions: List[str]) -> List[str]:
    """
    Fetch GenBank XML for a single batch of accessions.

    Returns a list of XML documents -- normally one for the whole batch, but
    one per accession when the batch had to be retried individually. Empty when
    nothing could be fetched. Each document is parsed separately by the caller,
    since efetch replies cannot simply be concatenated into one XML tree.
    """
    if not accessions:
        return []
        
    ids = ",".join(accessions)
    params = {
        "db": "nuccore",
        "id": ids,
        "rettype": "gb",
        "retmode": "xml"
    }
    
    try:
        # Use existing helper with retries
        response = _ncbi_request("POST", NCBI_EFETCH_URL, data=params, timeout=(15, 90))
        response.raise_for_status()
        return [response.text]
    except Exception as e:
        # Log the whole batch, not accessions[:3]. The truncation hid how many
        # records a single failure was taking down, and the "..." read as if
        # the batch were always large when it was often one bad id.
        logger.error(
            "event=ncbi.batch_failed Failed to fetch GenBank XML batch_size=%s "
            "accessions=%s error=%s",
            len(accessions), ",".join(accessions[:50]), e,
        )
        # NCBI rejects the entire efetch when any one id is unresolvable, so a
        # single bad accession used to void every good one beside it. Retry the
        # survivors individually to find out which id is actually at fault --
        # but ONLY when the batch was rejected rather than merely unreachable.
        # This runs synchronously inside a Gunicorn request slot: on a timeout
        # or a 5xx, splitting a 50-id batch into 50 requests that each retry
        # with their own exponential backoff turns one slow call into an hour
        # of them, well past nginx's proxy_read_timeout.
        if len(accessions) > 1 and _is_rejected_batch(e):
            return _fetch_genbank_xml_individually(accessions)
        _report_unresolved_accessions(accessions, 0)
        return []


def _is_rejected_batch(error: Exception) -> bool:
    """True when NCBI answered and refused, rather than failing to answer.

    A 4xx (other than 429, which _ncbi_request already retries) is NCBI saying
    it will not resolve these ids -- the case individual isolation can actually
    fix. Timeouts, connection errors and RetryError mean the service is down or
    slow, where isolation only multiplies the damage.
    """
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


def _fetch_genbank_xml_individually(accessions: List[str]) -> List[str]:
    """Re-fetch a rejected batch one accession at a time, keeping what works.

    Only reached after NCBI refused a whole batch, so the extra requests are
    bounded by that rare case rather than being the normal path. Everything
    here is deliberately cheap per accession: this can run inside a Gunicorn
    request slot, so each id gets one attempt with a short read timeout, and
    the pass abandons the rest once _ISOLATION_BUDGET_SECONDS is spent. The
    accessions it gives up on are reported as unresolved like any other, so a
    truncated pass still shows up as a degradation rather than a silent gap.
    """
    documents: List[str] = []
    failed: List[str] = []
    deadline = time.monotonic() + _ISOLATION_BUDGET_SECONDS

    for index, accession in enumerate(accessions):
        if time.monotonic() >= deadline:
            logger.warning(
                "event=ncbi.isolation_budget_exhausted Stopped isolating a "
                "rejected batch after %ss; %s of %s accessions unchecked",
                _ISOLATION_BUDGET_SECONDS, len(accessions) - index, len(accessions),
            )
            failed.extend(accessions[index:])
            break
        params = {
            "db": "nuccore",
            "id": accession,
            "rettype": "gb",
            "retmode": "xml",
        }
        try:
            response = _ncbi_request(
                "POST", NCBI_EFETCH_URL, max_retries=1, data=params, timeout=(10, 30),
            )
            response.raise_for_status()
            documents.append(response.text)
        except Exception:
            failed.append(accession)

    if failed:
        _report_unresolved_accessions(failed, len(documents))

    return documents


def _report_unresolved_accessions(failed: List[str], recovered: int) -> None:
    """Mark accessions NCBI would not return as a degradation, not a silent gap.

    A tree quietly missing its reference sequences looks like a successful run,
    which is exactly the failure mode log_degradation() exists to prevent.
    """
    from app.services.log_context import log_degradation
    log_degradation(
        logger,
        "ncbi_accessions_unresolved",
        "NCBI could not resolve every requested accession; sequences for "
        "these accessions are missing",
        failed=",".join(failed[:50]),
        failed_count=len(failed),
        recovered_count=recovered,
    )

def _parse_genbank_xml(xml_text: str) -> Dict[str, Dict]:
    """
    Parse GenBank XML string.
    Returns a dict with 'by_acc' and 'by_ver' lookups mapping to parsed records.
    
    ParsedRecord schema:
    {
        "accession": str,
        "version": str,  # "Accession.Version"
        "organism": str,
        "sequence": str, # Uppercase, clean
        "raw_record": str, # Blob for keyword scanning
        "source_features": Dict[str, str], # qualifier_name -> value
        "type_material": Optional[str] # If found in source qualifiers
    }
    """
    result = {"by_acc": {}, "by_ver": {}}
    
    try:
        # Remove namespace prefixes if present to simplify parsing
        xml_text = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
        root = ET.fromstring(xml_text)
        
        for gb_seq in root.findall(".//GBSeq"):
            # 1. Basic Info
            acc = gb_seq.findtext("GBSeq_primary-accession", "")
            ver = gb_seq.findtext("GBSeq_accession-version", "")
            organism = gb_seq.findtext("GBSeq_organism", "").strip()
            definition = gb_seq.findtext("GBSeq_definition", "").strip()
            
            # 2. Sequence
            raw_seq = gb_seq.findtext("GBSeq_sequence", "")
            # Clean sequence: strip whitespace, remove - and ., keep IUPAC, uppercase
            if raw_seq:
                # Remove whitespace (newlines, spaces)
                clean_seq = "".join(raw_seq.split())
                # Remove gaps/align chars
                clean_seq = re.sub(r'[-.]', '', clean_seq)
                clean_seq = clean_seq.upper()
            else:
                clean_seq = None
                
            # 3. Source Features & Qualifiers
            source_quals = {}
            type_material = None
            
            features = gb_seq.find("GBSeq_feature-table")
            all_quals_text = [] # For blob scanning
            
            if features is not None:
                for feature in features.findall("GBFeature"):
                    key = feature.findtext("GBFeature_key", "")
                    quals = feature.find("GBFeature_quals")
                    
                    if quals is not None:
                        for qual in quals.findall("GBQualifier"):
                            q_name = qual.findtext("GBQualifier_name", "")
                            q_val = qual.findtext("GBQualifier_value", "")
                            
                            all_quals_text.append(f"{q_name}={q_val}")
                            
                            if key == "source":
                                # Keep all source qualifiers
                                source_quals[q_name] = q_val
                                if q_name == "type_material":
                                    type_material = q_val

            # 4. Blob for keyword scanning (Definition + Organism + All Qualifiers + Comments + Refs)
            # Add comments and refs if needed, for now def + org + quals is usually sufficient for types
            blob_parts = [definition, organism] + all_quals_text
            
            # Add comments
            comment = gb_seq.findtext("GBSeq_comment", "")
            if comment:
                blob_parts.append(comment)
                
            record = {
                "accession": acc,
                "version": ver,
                "organism": organism,
                "sequence": clean_seq,
                "source_features": source_quals,
                "type_material": type_material,
                "blob": " ".join(blob_parts)
            }
            
            if acc:
                result["by_acc"][acc] = record
            if ver:
                result["by_ver"][ver] = record
                
    except ET.ParseError as e:
        logger.error(f"XML Parse Error: {e}")
    except Exception as e:
        logger.error(f"Error parsing GenBank XML: {e}")
        
    return result

def _build_header(record: Dict) -> str:
    """
    Construct FASTA header from parsed record.
    Format: >{acc_ver} {organism} {sample_id} {extras} {type_status}
    """
    # 1. Base
    # Use version if available, else accession
    acc_ver = record.get("version") or record.get("accession")
    organism = record.get("organism") or "(unknown organism)"
    
    parts = [acc_ver, organism]
    
    # Helper to clean redundant organism name from values
    def _strip_organism(val: str, org_name: str) -> str:
        if not val:
            return val

        val = " ".join(str(val).split())

        org_name = " ".join(str(org_name or "").split())
        org_tokens = org_name.split()
        org_binomial = " ".join(org_tokens[:2]) if len(org_tokens) >= 2 else ""

        # Try stripping both the full organism string and the binomial.
        variants = [v for v in [org_name, org_binomial] if v]

        matched = False
        for v in variants:
            # Remove occurrences anywhere (case-insensitive)
            new_val = re.sub(re.escape(v), "", val, flags=re.IGNORECASE)
            if new_val != val:
                matched = True
            val = new_val

        if not matched:
            # No organism substring was actually present in this value, so
            # leave punctuation (e.g. "USA: Maryland", "Ecuador: X, Y, Z")
            # untouched because it isn't leftover residue from a stripped match.
            return val.strip()

        # Clean up punctuation/separators left behind by the removal above.
        # Only touch residue at the edges (stray leading/trailing colons,
        # commas, empty bracket pairs). Do NOT blanket-strip punctuation
        # throughout the value, since well-formed content like geo_loc_name
        # ("Ecuador: Reserva Los Cedros, Imbabura, ...") may still be present
        # even when the organism name was found and removed elsewhere in it.
        val = re.sub(r"[\(\[\{]\s*[\)\]\}]", "", val)
        val = re.sub(r"^[\s:;,]+|[\s:;,]+$", "", val)
        val = re.sub(r"\bof\b", "", val, flags=re.IGNORECASE)
        val = " ".join(val.split()).strip()

        # Also remove trailing organism variants (repeated)
        for v in variants:
            val = re.sub(rf"(?:\s+{re.escape(v)})+\s*$", "", val, flags=re.IGNORECASE).strip()

        return val




    # If the assembled header accidentally ends with the organism repeated,
    # drop that suffix (token-aware).
    def _drop_trailing_organism_tokens(tokens: List[str], org_name: str) -> List[str]:
        if not tokens or not org_name:
            return tokens
        org_tokens = org_name.split()
        n = len(org_tokens)
        
        # Check if it ends with organism
        if len(tokens) >= n and [t.lower() for t in tokens[-n:]] == [t.lower() for t in org_tokens]:
            # Ensure we don't strip the PRIMARY organism (at index 1)
            # We assume index 0 is Accession, index 1 is Organism.
            # If the stripped range overlaps with index 1, don't strip (or stripped part is the primary one).
            # Indices of stripped part: len(tokens)-n to len(tokens)
            # Primary organism indices: 1 to 1+n (roughly, if split?) 
            # Actually, 'parts' list has strict slots. parts[1] is the Organism string.
            # If org_name is "Psilocybe cyanescens", parts[1] is "Psilocybe cyanescens".
            # If parts = [Acc, Org], then parts[-1] is Org.
            # logical check: Is the detected suffix the SAME element as parts[1]?
            
            # Simple heuristic: Don't strip if the result would have fewer than 2 elements (Acc + Org)?
            # Or if the match starts at index 1.
            start_index_of_match = len(tokens) - n
            if start_index_of_match <= 1:
                return tokens
                
            return tokens[:-n]
            
        return tokens

    # Extract type status so we can append it at the end without the "type_material" label.
    # Prefer structured /type_material, but reduce it to just known statuses when possible.
    TYPE_KEYWORDS = ["holotype", "isotype", "epitype", "lectotype", "neotype", "paratype", "syntype", "topotype"]

    def _extract_type_status(rec: Dict) -> List[str]:
        # 1) Structured
        t_val = rec.get("type_material")
        if t_val:
            t_val_norm = " ".join(str(t_val).split())
            t_val_norm = _strip_organism(t_val_norm, organism)
            low = t_val_norm.lower()
            found = [kw for kw in TYPE_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", low)]
            # If we found recognizable statuses, return those only (in stable order).
            if found:
                return found
            # Otherwise return the cleaned structured value as-is (still appended at end)
            return [t_val_norm] if t_val_norm else []

        # 2) Fallback keyword scan
        blob = (rec.get("blob") or "").lower()
        found = [kw for kw in TYPE_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", blob)]
        return found

    # 2. Sample ID Priority
    # specimen_voucher > culture_collection > bio_material > isolate > strain
    quals = record.get("source_features", {})
    sample_id = None
    for key in ["specimen_voucher", "culture_collection", "bio_material", "isolate", "strain"]:
        if val := quals.get(key):
            # Clean redundant organism from sample ID value
            val = _strip_organism(val, organism)
            if val:
                sample_id = f"{key} {val}"
                break
            
    if sample_id:
        parts.append(sample_id)
        
    # 3. Extras (Location first; we want type status at the very end)
    # geo_loc_name, country
    extras = []
    current_len = sum(len(p) for p in parts) + len(parts) # approx
    MAX_LEN = 120 # Cap for extras
    
    for key in ["geo_loc_name", "country"]:
        if val := quals.get(key):
            val = " ".join(val.split())
            
            # Clean redundant organism from value
            val = _strip_organism(val, organism)
            if not val:
                 continue
            
            # Formating: just value for location fields, else key="val"
            if key in ["geo_loc_name", "country"]:
                token = f"{val}"
            else:
                token = f"{key}=\"{val}\""
            
            if len(extras) < 3 and (current_len + len(token)) < (current_len + 120): # Soft cap
                extras.append(token)
                current_len += len(token) + 1
                
    if extras:
        parts.extend(extras)
        
    # 4. Type status LAST (no "type_material" label, and not in the middle)
    type_status = _extract_type_status(record)
    if type_status:
        parts.extend(type_status)
        
    # 5. Final cleanup: remove trailing organism if it still sneaks in
    parts = _drop_trailing_organism_tokens(parts, organism)
        
    return ">" + " ".join(parts)

def fetch_fasta_for_accessions(accessions: List[str]) -> str:
    """
    Fetch FASTA sequences from NCBI for the given accessions using GenBank XML
    fetching to rebuild detailed headers with type material info.
    """
    if not accessions:
        return ""
    
    # Validate accession format
    import re
    # Loose safety net only -- the entry points validate against the exact
    # INSDC shapes (_GENBANK_ACCESSION_RE in app/api/routes.py). The digit
    # range has to cover the longest of those (a 6-letter WGS prefix with a
    # 2-digit assembly version and 9 contig digits), or this filter silently
    # drops accessions the caller was already told were valid.
    valid_pattern = re.compile(r'^[A-Z]{1,6}_?\d{5,11}(?:\.\d+)?$', re.IGNORECASE)
    validated = [a for a in accessions if valid_pattern.match(a)]
    
    if not validated:
        logger.warning(f"No valid accessions found in list of {len(accessions)}")
        return ""

    logger.info(f"Fetching sequences for {len(validated)} accessions via GenBank XML...")
    
    # 1. Fetch and Parse
    BATCH_SIZE = 50
    records_by_acc = {}
    records_by_ver = {}
    
    counters = {
        "xml_ok": 0,
        "type_structured": 0,
        "type_keyword": 0,
        "xml_missing_seq": 0,
        "batch_fail": 0
    }
    
    for i in range(0, len(validated), BATCH_SIZE):
        batch = validated[i : i + BATCH_SIZE]
        xml_documents = _fetch_genbank_xml_batch(batch)

        if not xml_documents:
            counters["batch_fail"] += 1
            continue

        for xml_text in xml_documents:
            parsed = _parse_genbank_xml(xml_text)
            records_by_acc.update(parsed["by_acc"])
            records_by_ver.update(parsed["by_ver"])

            # counting stats
            for r in parsed["by_acc"].values():
                if r.get("type_material"): counters["type_structured"] += 1
                elif "holotype" in r.get("blob", "").lower(): counters["type_keyword"] += 1 # approx check


    # 2. Reassemble deterministically
    final_lines = []
    missing_for_fallback = []
    
    for acc in validated:
        # Try finding record
        record = records_by_ver.get(acc) or records_by_acc.get(acc)
        
        if record and record.get("sequence"):
            counters["xml_ok"] += 1
            
            header = _build_header(record)
            seq = record["sequence"]
            
            # Wrap sequence at 60 chars
            wrapped_seq = "\n".join(seq[i:i+60] for i in range(0, len(seq), 60))
            final_lines.append(f"{header}\n{wrapped_seq}")
        else:
            if record:
                counters["xml_missing_seq"] += 1
            missing_for_fallback.append(acc)

    logger.info(f"XML Fetch Stats: OK={counters['xml_ok']}, TypeStruct={counters['type_structured']}, "
                f"TypeKey={counters['type_keyword']}, MissingSeq={counters['xml_missing_seq']}, "
                f"BatchFail={counters['batch_fail']}")

    # 3. Fallback for missing
    if missing_for_fallback:
        logger.warning(f"Falling back to legacy FASTA fetch for {len(missing_for_fallback)} accessions")
        
        # We can implement a simple fallback here using the OLD method (efetch fasta)
        # Construct parameters for fallback
        try:
            ids = ",".join(missing_for_fallback)
            params = {
                "db": "nuccore",
                "id": ids,
                "rettype": "fasta",
                "retmode": "text"
            }
            # Use same batching logic? Or just dump all if small?
            # Safe to assume fallback list might be small, but if batch fail, it could be large.
            # Let's just do one call if small, or reuse verify logic? 
            # Simple approach: Just request. If it's huge, requests might fail, but existing code didn't batch FASTA fetch anyway.
            # Wait, existing code did NOT batch FASTA fetch (it sent all IDs).
            
            response = _ncbi_request("POST", NCBI_EFETCH_URL, data=params, timeout=(10, 60))
            if response.status_code == 200:
                final_lines.append(response.text.strip())
            else:
                logger.error(
                    "event=ncbi.fallback_failed Fallback FASTA fetch failed "
                    "status=%s count=%s",
                    response.status_code, len(missing_for_fallback),
                )
                # These accessions are now definitively absent from the result.
                # Silently returning without them produced a tree missing its
                # references that looked like a completely successful run.
                _report_unresolved_accessions(missing_for_fallback, len(final_lines))
        except Exception as e:
            logger.error(f"Fallback FASTA fetch exception: {e}")
            _report_unresolved_accessions(missing_for_fallback, len(final_lines))

    return "\n".join(final_lines)
