import hashlib
import json
import logging
import re
import time
import requests
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from app.config import Config

logger = logging.getLogger(__name__)

NCBI_BLAST_URL = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
NCBI_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Global rate limiter state
_ncbi_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_GAP = 1.0 / 3.0  # At least 0.34s between requests (~3 req/s)

def _ncbi_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Helper to perform NCBI requests with:
    - Global rate limiting (min gap between requests)
    - User-Agent header
    - Retries with exponential backoff for suitable errors
    - Respect for Retry-After headers
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
    max_retries = 5
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


def blast_from_sequence(seq: str, config: Config, min_identity: float = 90.0, max_sequences: int = 50, logger=logger) -> Dict:
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
        
        blast_result = _fetch_blast_results(rid)
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

def blast_from_accessions(accessions: List[str], config: Config, min_identity: float = 90.0, max_sequences: int = 50, logger=logger) -> Dict:
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
        
        blast_result = _fetch_blast_results(rid)
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
    
    # Log sequence info
    seq_preview = seq[:100] + "..." if len(seq) > 100 else seq
    logger.info(f"Submitting BLAST request (ident={min_identity}%, max={max_sequences}), sequence preview: {seq_preview}")
    
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
    
    logger.debug(f"BLAST submission response: {response.text[:500]}")
    
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
            logger.debug(f"Poll response for RID {rid}: {content[:500]}")
            
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

def _fetch_blast_results(rid: str) -> Dict[str, List]:
    """Fetch BLAST results from NCBI. Handle both raw JSON and ZIP-compressed responses.
    
    Returns:
        Dict with 'accessions' (list of str) and 'hit_details' (list of dicts with accession/organism)
    """
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
        logger.warning(f"BLAST response doesn't look like JSON, first 500 chars: {content[:500]}")
        # Try to extract accessions from text-based format as fallback
        import re
        accession_pattern = r'\b([A-Z]{1,2}_?\d{6,}(?:\.\d+)?)\b'
        matches = re.findall(accession_pattern, content)
        if matches:
            unique_accessions = list(dict.fromkeys(matches))[:50]
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
            logger.warning(f"No BlastOutput2 in response. Full response keys: {list(data.keys())}")
            debug_path = Path("/tmp/blast_debug_response.json")
            debug_path.write_text(json.dumps(data, indent=2)[:10000])
            logger.info(f"Saved debug response to {debug_path}")
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
        return {"accessions": accessions[:50], "hit_details": hit_details[:50]}
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}, response preview: {content[:500]}")
        raise ValueError(f"Failed to parse BLAST JSON response: {e}")

def fetch_fasta_for_accessions(accessions: List[str]) -> str:
    """Fetch FASTA sequences from NCBI for the given accessions."""
    if not accessions:
        return ""
    
    # Validate accession format
    import re
    # CHANGE TO (SECURE): Enforces GenBank format (1-6 letters, optional _, 5-9 digits, optional version)
    valid_pattern = re.compile(r'^[A-Z]{1,6}_?\d{5,9}(?:\.\d+)?$', re.IGNORECASE)
    validated = [a for a in accessions if valid_pattern.match(a)]
    
    if not validated:
        logger.warning(f"No valid accessions found in list of {len(accessions)}")
        return ""

    logger.info(f"Fetching FASTA for {len(validated)} accessions: {validated[:5]}...")
        
    ids = ",".join(validated)
    params = {
        "db": "nuccore",
        "id": ids,
        "rettype": "fasta",
        "retmode": "text"
    }
    
    response = _ncbi_request("POST", NCBI_EFETCH_URL, data=params, timeout=(10, 60))
    response.raise_for_status()
    
    fasta_text = response.text
    logger.info(f"Fetched FASTA response: {len(fasta_text)} chars, preview: {fasta_text[:200]}")
    return fasta_text
