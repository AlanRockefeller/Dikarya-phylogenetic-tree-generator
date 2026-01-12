"""
Mycomap FASTA downloader service.

Fetches BLAST result sequences from Mycomap URLs for use in the tree builder.
Based on standalone script by Alan Rockefeller - June 30, 2025.
"""

import logging
import re
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Timeout for network requests in seconds
REQUEST_TIMEOUT = 10

# Mycomap API base URL
MYCOMAP_BASE_URL = "https://mycomap.com/index.php"


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
    
    opener = urllib.request.build_opener()
    opener.addheaders = [
        ('User-Agent', 'Dikarya-TreeBuilder/1.0'),
        ('Accept', '*/*')
    ]
    
    try:
        with opener.open(url, timeout=REQUEST_TIMEOUT) as resp:
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
