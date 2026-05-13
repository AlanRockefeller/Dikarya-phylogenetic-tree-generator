"""
Mycomap FASTA downloader service.

Fetches BLAST result sequences from Mycomap URLs for use in the tree builder.
Based on standalone script by Alan Rockefeller - June 30, 2025.
"""

import html
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
    
    post_data = urllib.parse.urlencode({'delimiter': 's'}).encode('utf-8')
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
                re.search(r'\bcontaminant\b', str(cell or ''), flags=re.IGNORECASE)
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

    marker_pattern = (
        r'\s+(?:small subunit|internal transcribed spacer|large subunit|'
        r'5\.8S|18S|28S|ribosomal RNA|rRNA|ITS\b|isolate\b.*?\bITS\b)'
    )
    match = re.search(marker_pattern, text, flags=re.IGNORECASE)
    if match:
        text = text[:match.start()]
    return _clean_label_fragment(text)


def _infer_species_name(description: str) -> str:
    """Infer a binomial-style species name from a BLAST description."""
    text = _compact_ncbi_description(description)
    match = re.match(r'^([A-Z][a-zA-Z-]+)\s+([a-z][a-zA-Z-]+|["\'][^"\']+["\'])\b', text)
    if not match:
        return ''
    return _clean_label_fragment(' '.join(match.groups()))


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


def improve_mycomap_sequence_name(current_name: str, metric: Optional[dict], hit_source: str = '') -> str:
    """
    Use MycoMap table metadata to repair sparse NCBI FASTA headers.

    MycoMap's FASTA export can emit headers such as "MH855376 England GB" even
    when the BLAST results table has "Species Name: Ascobolus equinus".
    """
    if hit_source and hit_source != 'ncbi':
        return current_name
    if not metric:
        return current_name

    display_name = metric.get('display_name') or ''
    species_name = metric.get('species_name') or ''
    if not display_name or not species_name:
        return current_name
    if _first_identifier_token(current_name) != _first_identifier_token(display_name):
        return current_name
    if _contains_species_name(current_name, species_name):
        return current_name
    return display_name


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
