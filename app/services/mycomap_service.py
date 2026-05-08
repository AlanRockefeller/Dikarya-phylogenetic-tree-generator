"""
Mycomap FASTA downloader service.

Fetches BLAST result sequences from Mycomap URLs for use in the tree builder.
Based on standalone script by Alan Rockefeller - June 30, 2025.
"""

import html.parser
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
        if tag == 'table':
            self._depth += 1
            if self._depth == 1:
                self._cur_table = []
        elif tag == 'tr' and self._cur_table is not None:
            self._cur_row = []
        elif tag in ('td', 'th') and self._cur_row is not None:
            self._cur_cell = []

    def handle_endtag(self, tag):
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

    header_idx = None
    header = []
    for i, row in enumerate(rows[:5]):
        lower = [c.lower() for c in row]
        if any('identity' in c or 'ident' in c or 'coverage' in c or 'cover' in c for c in lower):
            header_idx = i
            header = lower
            break

    if header_idx is None or not header:
        return {}

    # Detect column indices
    def find_col(*keywords):
        for j, h in enumerate(header):
            if any(kw in h for kw in keywords):
                return j
        return None

    acc_col = find_col('accession', 'subject id', 'subject', 'id')
    if acc_col is None:
        acc_col = 0
    ident_col = find_col('identity', 'ident')
    qcov_col = find_col('query cov', 'query cover', 'q. cov', 'qcov')
    scov_col = find_col('subject cov', 'subj cov', 's. cov', 'scov')

    if ident_col is None:
        return {}

    def _to_float(val):
        if val is None:
            return None
        try:
            return float(val.strip().rstrip('%').strip())
        except (ValueError, AttributeError):
            return None

    result = {}
    for row in rows[header_idx + 1:]:
        if len(row) <= max(acc_col, ident_col):
            continue
        raw_acc = row[acc_col].split()[0] if row[acc_col] else ''
        if not raw_acc:
            continue
        bare_acc = raw_acc.split('.')[0]
        identity = _to_float(row[ident_col] if len(row) > ident_col else None)
        query_cover = _to_float(row[qcov_col] if qcov_col is not None and len(row) > qcov_col else None)
        subject_cover = _to_float(row[scov_col] if scov_col is not None and len(row) > scov_col else None)
        result[bare_acc] = {
            'identity': identity,
            'query_cover': query_cover,
            'subject_cover': subject_cover,
        }
        # Also store with version suffix as a fallback key
        if raw_acc != bare_acc:
            result.setdefault(raw_acc, result[bare_acc])

    return result


def fetch_mycomap_blast_metrics(blast_id: str) -> dict:
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

    url = f"https://mycomap.com/genetics/blast-search/r{blast_id}"

    opener = urllib.request.build_opener()
    opener.addheaders = [
        ('User-Agent', 'Dikarya-TreeBuilder/1.0'),
        ('Accept', 'text/html,*/*')
    ]

    try:
        with opener.open(url, timeout=REQUEST_TIMEOUT) as resp:
            content = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        logger.warning(f"fetch_mycomap_blast_metrics: could not fetch page: {e}")
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
