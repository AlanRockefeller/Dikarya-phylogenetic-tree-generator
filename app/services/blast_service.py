import hashlib
import json
import logging
import time
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from app.config import Config

logger = logging.getLogger(__name__)

NCBI_BLAST_URL = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
NCBI_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

def blast_from_sequence(seq: str, config: Config, logger=logger) -> Dict:
    """
    Perform remote BLAST using the given nucleotide sequence.
    Use caching if available.
    """
    query_hash = _hash_query(seq)
    cached_result = _check_cache(query_hash, config)
    if cached_result:
        logger.info(f"BLAST cache hit for hash {query_hash}")
        return cached_result

    logger.info(f"BLAST cache miss for hash {query_hash}. Submitting to NCBI.")
    
    try:
        rid, rtoe = _submit_blast_request(seq)
        logger.info(f"BLAST submitted. RID: {rid}, RTOE: {rtoe}")
        
        _poll_blast(rid, rtoe, logger)
        
        hit_accessions = _fetch_blast_results(rid)
        logger.info(f"BLAST finished. Found {len(hit_accessions)} hits.")
        
        fasta_content = _fetch_fasta_for_accessions(hit_accessions)
        logger.info(f"Downloaded FASTA for {len(hit_accessions)} accessions.")
        
        return _save_cache(query_hash, hit_accessions, fasta_content, config)
        
    except Exception as e:
        logger.error(f"BLAST failed: {e}")
        raise

def blast_from_accessions(accessions: List[str], config: Config, logger=logger) -> Dict:
    """
    Retrieve sequences for the provided accessions.
    Then run remote BLAST using the combined sequence.
    Use caching.
    """
    # Sort to ensure stable hash
    sorted_accessions = sorted(accessions)
    query_str = ",".join(sorted_accessions)
    query_hash = _hash_query(query_str)
    
    cached_result = _check_cache(query_hash, config)
    if cached_result:
        logger.info(f"BLAST cache hit for accessions hash {query_hash}")
        return cached_result

    logger.info(f"BLAST cache miss for accessions {query_hash}. Fetching sequences first.")
    
    try:
        # 1. Fetch initial sequences to use as query
        query_fasta = _fetch_fasta_for_accessions(sorted_accessions)
        
        # 2. Run BLAST with these sequences
        # Note: We use the same flow as blast_from_sequence, but we might want to 
        # include the original sequences in the final result too. 
        # For now, let's just BLAST them.
        
        rid, rtoe = _submit_blast_request(query_fasta)
        logger.info(f"BLAST submitted. RID: {rid}, RTOE: {rtoe}")
        
        _poll_blast(rid, rtoe, logger)
        
        hit_accessions = _fetch_blast_results(rid)
        logger.info(f"BLAST finished. Found {len(hit_accessions)} hits.")
        
        # Combine original accessions with hits to ensure we have everything
        all_accessions = list(set(sorted_accessions + hit_accessions))
        
        fasta_content = _fetch_fasta_for_accessions(all_accessions)
        logger.info(f"Downloaded FASTA for {len(all_accessions)} accessions.")
        
        return _save_cache(query_hash, all_accessions, fasta_content, config)

    except Exception as e:
        logger.error(f"BLAST from accessions failed: {e}")
        raise

def _hash_query(query_str: str) -> str:
    return hashlib.sha256(query_str.encode('utf-8')).hexdigest()

def _check_cache(query_hash: str, config: Config) -> Optional[Dict]:
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
                "fasta_path": str(fasta_path),
                "metadata_path": str(json_path)
            }
        except Exception:
            return None
    return None

def _save_cache(query_hash: str, hit_accessions: List[str], fasta_content: str, config: Config) -> Dict:
    cache_dir = config.BLAST_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    json_path = cache_dir / f"{query_hash}.json"
    fasta_path = cache_dir / f"{query_hash}.fasta"
    
    metadata = {
        "query_hash": query_hash,
        "sequence_count": len(hit_accessions),
        "hit_accessions": hit_accessions,
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
        "fasta_path": str(fasta_path),
        "metadata_path": str(json_path)
    }

def _submit_blast_request(seq: str) -> Tuple[str, int]:
    params = {
        "CMD": "Put",
        "PROGRAM": "blastn",
        "DATABASE": "nt",
        "QUERY": seq,
        "HITLIST_SIZE": 100,
        "FORMAT_TYPE": "JSON2", # Request JSON for easier parsing if possible, though standard is often XML/Text
        # We'll stick to standard and poll for JSON/XML later
        "EMAIL": "blast_user@example.com" # Placeholder
    }
    response = requests.post(NCBI_BLAST_URL, data=params)
    response.raise_for_status()
    
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
        raise ValueError("Could not retrieve RID from NCBI BLAST submission")
        
    return rid, rtoe

def _poll_blast(rid: str, rtoe: int, logger) -> None:
    # Wait initial RTOE
    logger.info(f"Waiting {rtoe} seconds for RTOE...")
    time.sleep(rtoe)
    
    start_time = time.time()
    max_wait = 600 # 10 minutes max
    
    while (time.time() - start_time) < max_wait:
        params = {
            "CMD": "Get",
            "FORMAT_OBJECT": "SearchInfo",
            "RID": rid
        }
        response = requests.get(NCBI_BLAST_URL, params=params)
        content = response.text
        
        if "Status=WAITING" in content:
            logger.info("BLAST Status: WAITING. Sleeping 10s...")
            time.sleep(10)
            continue
        
        if "Status=FAILED" in content:
            raise RuntimeError(f"BLAST failed for RID {rid}")
            
        if "Status=UNKNOWN" in content:
            raise RuntimeError(f"BLAST RID {rid} expired or unknown")
            
        if "Status=READY" in content:
            if "ThereAreHits=yes" in content:
                logger.info("BLAST Status: READY with hits.")
                return
            else:
                logger.warning("BLAST Status: READY but NO hits found.")
                return

    raise TimeoutError("BLAST timed out waiting for results")

def _fetch_blast_results(rid: str) -> List[str]:
    # Fetch results in JSON format
    params = {
        "CMD": "Get",
        "FORMAT_TYPE": "JSON2",
        "RID": rid
    }
    response = requests.get(NCBI_BLAST_URL, params=params)
    response.raise_for_status()
    
    try:
        data = response.json()
        # Navigate JSON structure to find hits
        # Structure: BlastOutput2 -> report -> results -> search -> hits -> [ ... ]
        hits = data.get("BlastOutput2", [])
        if not hits:
            return []
            
        search_results = hits[0].get("report", {}).get("results", {}).get("search", {})
        hit_list = search_results.get("hits", [])
        
        accessions = []
        for hit in hit_list:
            # description -> [ { "accession": "..." } ]
            descs = hit.get("description", [])
            if descs:
                acc = descs[0].get("accession")
                if acc:
                    accessions.append(acc)
                    
        return accessions[:100] # Ensure max 100
        
    except json.JSONDecodeError:
        # Fallback or error if not JSON
        raise ValueError("Failed to parse BLAST JSON response")

def _fetch_fasta_for_accessions(accessions: List[str]) -> str:
    if not accessions:
        return ""
        
    ids = ",".join(accessions)
    params = {
        "db": "nuccore",
        "id": ids,
        "rettype": "fasta",
        "retmode": "text"
    }
    response = requests.post(NCBI_EFETCH_URL, data=params)
    response.raise_for_status()
    return response.text
