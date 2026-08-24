"""
iNaturalist API service for DNA sequence extraction.

Fetches DNA Barcode ITS sequences from iNaturalist observations.

API best practices followed per https://www.inaturalist.org/pages/api+recommended+practices:
- Rate limit: 1 request per second
- Custom User-Agent header
- Batch requests using per_page (max 200)
- Use api.inaturalist.org for new API
"""

import logging
import re
import time
import urllib.request
import urllib.parse
import urllib.error
import json
from typing import Optional, List, Dict, Tuple, Any
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# API Configuration
INATURALIST_API_BASE = "https://api.inaturalist.org/v1"
USER_AGENT = "Dikarya Phylogenetic Tree Builder 1.0 - For questions contact Alan Rockefeller"
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 1.0  # seconds between requests
MAX_PER_PAGE = 200  # iNaturalist max per_page value
MAX_OBSERVATIONS = 10000  # API pagination limit

# Wall-clock budget for a paginated search made from inside a web request.
#
# A full walk is up to 50 pages per field query, each page costing an upstream
# round trip plus the 1s courtesy delay, and an `analyze` runs two such queries
# -- several minutes of one Gunicorn slot (of eight) for a single click, with
# nginx having given up at 300s long before. Passing a budget makes the fetch
# return what it has, flagged as incomplete, instead of holding the slot.
#
# Background/worker callers pass `time_budget=None` and still walk the whole
# result set: there is no request to keep alive there, and a truncated tree is
# a worse outcome than a slow one.
INTERACTIVE_FETCH_BUDGET_SECONDS = 45

# Valid IUPAC nucleotide characters (DNA + ambiguity codes + gap)
VALID_DNA_CHARS = set('ATGCYRWSKMNatgcyrwskmn-')


def validate_inaturalist_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Validate an iNaturalist URL and extract query parameters.
    
    Args:
        url: The URL to validate
        
    Returns:
        Dict with 'type' ('observation', 'observations_search') and relevant IDs/params,
        or None if invalid.
    """
    if not url:
        return None
    
    url = url.strip()
    if re.fullmatch(r'\d{7,9}', url):
        logger.debug(f"Validated bare iNaturalist observation ID: {url}")
        return {
            'type': 'single_observation',
            'observation_id': url
        }
    
    # Must be from inaturalist.org domain (strict check)
    # Matches: inaturalist.org, www.inaturalist.org
    # Rejects: notinaturalist.org, inaturalist.org.fake.com
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        
        # Must be inaturalist.org or subdomain
        if not (hostname == "inaturalist.org" or 
                hostname.endswith(".inaturalist.org")):
            logger.warning(f"URL validation failed: not an inaturalist.org URL: {url}")
            return None
    except Exception as e:
        logger.warning(f"URL parsing failed: {e}")
        return None
    
    # Check for single observation URL pattern: /observations/12345
    single_obs_match = re.search(r'/observations/(\d+)(?:\?|$|#)', url)
    if single_obs_match:
        obs_id = single_obs_match.group(1)
        logger.debug(f"Validated single observation URL, ID: {obs_id}")
        return {
            'type': 'single_observation',
            'observation_id': obs_id
        }
    
    # Check for observations list/search URL: /observations or /observations?...
    if '/observations' in parsed.path:
        # Parse query parameters, keeping blank values (e.g. field:DNA Barcode ITS=)
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        
        # Security: Require at least one meaningful filter param to prevent massive/broad queries
        # We don't want users scraping *all* observations or causing backend abuse.
        meaningful_filters = {
            'taxon_id', 'place_id', 'user_id', 'project_id', 'list_id', 'id', 
            'identified_by', 'q'
        }
        has_meaningful_filter = any(k in meaningful_filters for k in query_params.keys())
        has_field_filter = any(k.startswith('field:') for k in query_params.keys())
        
        if not (has_meaningful_filter or has_field_filter):
            logger.warning(f"URL validation failed: Search URL missing required filter params (taxon_id, user_id, etc.): {url}")
            return None
            
        logger.info(f"Validated observations search URL with params: {list(query_params.keys())}")
        return {
            'type': 'observations_search',
            'query_params': query_params,
            'raw_query': parsed.query
        }
    
    logger.warning(f"URL validation failed: unrecognized iNaturalist URL format: {url}")
    return None


from app.services.fasta_utils import clean_dna_sequence


def get_field_value(observation_data: Dict, field_name: str) -> Optional[str]:
    """
    Extract value from observation field values (ofvs) by field name.
    
    Args:
        observation_data: iNaturalist observation data dict
        field_name: Name of the field to extract (case-insensitive)
        
    Returns:
        Field value if found, None otherwise
    """
    field_name_lower = field_name.lower()
    for field in observation_data.get('ofvs', []):
        # Handle both 'name' (from API) and check for nested 'observation_field'
        name = field.get('name', '')
        if not name and 'observation_field' in field:
            name = field['observation_field'].get('name', '')
        
        if name.lower() == field_name_lower:
            return field.get('value')
    
    return None


def _clean_display_text(value: Any) -> str:
    """Normalize free-form iNaturalist text for FASTA/tree labels."""
    if value is None:
        return ""
    text = re.sub(r'[<>"]', "", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ,;:-_")


def _normalize_location_piece(piece: str) -> str:
    """Collapse common country variants so location labels stay short."""
    cleaned = _clean_display_text(piece)
    if not cleaned:
        return ""
    lowered = cleaned.casefold()
    if lowered in {
        "united states",
        "united states of america",
        "usa",
        "u.s.a.",
        "u.s.",
    }:
        return "US"
    return cleaned


def _extract_location_label(observation_data: Dict) -> str:
    """Return a compact iNaturalist location label like `New Mexico US`."""
    for key in (
        "private_place_guess",
        "place_guess",
        "private_locality",
        "locality",
    ):
        raw = observation_data.get(key)
        if not raw:
            continue
        parts = [_normalize_location_piece(part) for part in str(raw).split(",")]
        parts = [part for part in parts if part]
        if not parts:
            continue
        if len(parts) >= 3 and len(parts[-2]) <= 3:
            return f"{parts[0]} {parts[-1]}".strip()
        if len(parts) >= 2:
            return " ".join(parts[-2:])
        return parts[0]
    return ""


def _make_api_request(url: str, max_retries: int = 3) -> Dict:
    """
    Make a request to the iNaturalist API with proper headers and 429 retry logic.
    
    Args:
        url: Full API URL to request
        max_retries: Number of retries for 429 errors
        
    Returns:
        Parsed JSON response as dict
        
    Raises:
        Exception on network or API errors after retries exhausted
    """
    import random
    
    opener = urllib.request.build_opener()
    opener.addheaders = [
        ('User-Agent', USER_AGENT),
        ('Accept', 'application/json')
    ]
    
    attempts = 0
    while attempts <= max_retries:
        try:
            with opener.open(url, timeout=REQUEST_TIMEOUT) as resp:
                content = resp.read()
                return json.loads(content.decode('utf-8'))
                
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempts < max_retries:
                # Calculate backoff: 2s, 4s, 8s... + jitter
                sleep_time = (2 ** (attempts + 1)) + random.uniform(0, 1)
                logger.warning(f"iNaturalist 429 Rate Limit hit. Retrying in {sleep_time:.2f}s... (Attempt {attempts + 1}/{max_retries})")
                time.sleep(sleep_time)
                attempts += 1
                continue
                
            error_msg = f"HTTP error {e.code}: {e.reason}"
            logger.error(f"iNaturalist API error: {error_msg}")
            raise Exception(error_msg)
            
        except urllib.error.URLError as e:
            error_msg = f"Network error: {e.reason}"
            logger.error(f"iNaturalist API error: {error_msg}")
            raise Exception(error_msg)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse API response: {e}")
            raise Exception("Invalid response from iNaturalist API")
        except TimeoutError:
            error_msg = f"Request timed out after {REQUEST_TIMEOUT}s"
            logger.error(error_msg)
            raise Exception(error_msg)
            
    # Should be unreachable given the logic above, but fail-safe
    raise Exception("Max retries exceeded")


def fetch_single_observation(observation_id: str) -> Dict:
    """
    Fetch a single observation by ID.
    
    Args:
        observation_id: iNaturalist observation ID (numeric string)
        
    Returns:
        Observation data dict
    """
    # Sanitize observation_id - must be numeric only
    if not observation_id.isdigit():
        raise ValueError("Invalid observation ID - must be numeric")
    
    url = f"{INATURALIST_API_BASE}/observations/{observation_id}"
    logger.info(f"Fetching single observation: {observation_id}")
    
    response = _make_api_request(url)
    
    results = response.get('results', [])
    if not results:
        raise Exception(f"Observation {observation_id} not found")
    
    return results[0]


def build_base_params(query_params: Dict[str, List[str]]) -> Dict[str, Any]:
    """
    Build sanitized base parameters from URL query parameters.
    
    Args:
        query_params: Dict of query parameters (from parse_qs)
        
    Returns:
        Dict of sanitized parameters for API calls
    """
    base_params = {}
    
    # Copy relevant params from the URL, sanitizing as we go
    # Comprehensive whitelist based on https://www.inaturalist.org/pages/search+urls
    allowed_params = {
        # Taxon filters
        'taxon_id', 'taxon_ids', 'taxon_name', 'exact_taxon_id',
        'without_taxon_id', 'without_direct_taxon_id',
        'ident_taxon_id', 'ident_taxon_id_exclusive',
        'iconic_taxa', 'list_id',
        
        # Taxon status
        'native', 'introduced', 'threatened',
        
        # Place/geographic filters
        'place_id', 'not_in_place',
        'nelat', 'nelng', 'swlat', 'swlng',  # Bounding box
        'lat', 'lng', 'radius',  # Circle
        'geo', 'mappable',
        
        # Accuracy and geoprivacy
        'acc', 'acc_above', 'acc_below',
        'geoprivacy', 'taxon_geoprivacy',
        
        # Date and time filters
        'd1', 'd2', 'month', 'day', 'year', 'hour',
        
        # Observer filters
        'user_id', 'not_user_id',
        'user_after', 'user_before',
        
        # Identification filters
        'identified', 'ident_user_id', 'without_ident_user_id',
        'disagreements',
        
        # Project filters
        'project_id', 'not_in_project',
        'apply_project_rules_for', 'not_matching_project_rules_for',
        'members_of_project',
        
        # Quality and verification
        'quality_grade', 'captive', 'verifiable',
        
        # Media and license filters
        'photos', 'sounds',
        'photo_license', 'photo_licensed',
        'sound_license', 'license', 'licensed',
        
        # Observation ID filters
        'id', 'not_id',
        
        # Description/tag search
        'q', 'search_on',
        
        # Annotation filters
        'term_id', 'term_value_id',
        'without_term_id', 'without_term_value_id',
        
        # Observation field filters
        'without_field',
        
        # App source (read-only info)
        'oauth_application_id',
        
        # NOTE: We intentionally EXCLUDE pagination/ordering params (per_page, page, 
        # order, order_by) to prevent abuse - we control these ourselves.
        # We also exclude 'reviewed' as it's user-session-specific.
    }
    
    # Also allow any field: prefixed params (preserve user intent if they added them)
    for key, values in query_params.items():
        if key in allowed_params or key.startswith('field:'):
            # Sanitize values - no script injection
            safe_values = [re.sub(r'[<>]', '', str(v)) for v in values]
            # A blank value (e.g. field:Name=) is passed through as "" rather
            # than dropped: to iNaturalist that means "the field must be
            # present", which is not the same query as omitting it.
            val = safe_values[0] if len(safe_values) == 1 else safe_values
            base_params[key] = val
            
    # Default required params
    base_params['per_page'] = MAX_PER_PAGE
    base_params['order_by'] = 'id'
    base_params['order'] = 'asc'
    
    return base_params


def fetch_observations_with_field_filter(base_params: Dict, field_name: str,
                                         deadline: Optional[float] = None) -> Dict:
    """
    Fetch observations enforcing a specific observation field filter.
    
    Args:
        base_params: Base query parameters
        field_name: Name of the field to enforce (e.g. 'DNA Barcode ITS')
        deadline: Optional time.monotonic() value past which pagination stops
            and returns what it has. None means walk the whole result set.
        
    Returns:
        Dict with 'observations', 'fetched_count', 'truncated', 'timed_out',
        'total_available'. 'timed_out' is True only when the deadline cut the
        walk short, so a caller can say "partial results" rather than presenting
        them as the complete answer.
    """
    # Strip any existing field: parameters to avoid intersection/conflict
    # We want these queries to be independent based on the base filters (taxon, place, etc.)
    params = {k: v for k, v in base_params.items() if not k.startswith("field:")}
    
    # Enforce field presence (empty value means "any value" in iNat API)
    params[f"field:{field_name}"] = ""
    
    all_observations = []
    page = 1
    timed_out = False
    total_results = 0
    
    logger.info(f"Fetching observations with field '{field_name}' (params: {list(params.keys())})")
    
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            logger.warning(
                "event=inaturalist.fetch_truncated Time budget spent after "
                "%s page(s) for '%s'; returning %s of %s observations",
                page - 1, field_name, len(all_observations), total_results,
            )
            break
        params['page'] = page
        query_string = urllib.parse.urlencode(params, doseq=True)
        url = f"{INATURALIST_API_BASE}/observations?{query_string}"
        
        # Log less frequently to avoid spam
        if page % 5 == 1:
            logger.info(f"Fetching page {page} for '{field_name}' query")
            
        response = _make_api_request(url)
        
        results = response.get('results', [])
        total_results = response.get('total_results', 0)
        
        if not results:
            break
            
        all_observations.extend(results)
        
        if len(all_observations) >= total_results or len(all_observations) >= MAX_OBSERVATIONS:
            break
            
        page += 1
        time.sleep(RATE_LIMIT_DELAY)
        
    truncated = len(all_observations) < total_results and (
        timed_out or len(all_observations) >= MAX_OBSERVATIONS
    )
    
    logger.info(f"Fetched {len(all_observations)} observations for '{field_name}' (total_available={total_results})")
    
    return {
        'observations': all_observations,
        'fetched_count': len(all_observations),
        'truncated': truncated,
        'timed_out': timed_out,
        'total_available': total_results
    }


def analyze_observations(observations: List[Dict]) -> Dict:
    """
    Analyze observations for DNA Barcode ITS and Provisional Species Name fields.
    
    Args:
        observations: List of observation dicts
        
    Returns:
        Analysis dict with counts and species breakdown
    """
    dna_observations = []
    provisional_species_counts = {}
    
    for obs in observations:
        dna_value = get_field_value(obs, 'DNA Barcode ITS')
        
        if dna_value:
            # Clean the DNA and check if it's valid
            cleaned = clean_dna_sequence(dna_value)
            if cleaned:  # clean_dna_sequence already enforces min_length
                dna_observations.append({
                    'observation': obs,
                    'raw_dna': dna_value,
                    'cleaned_dna': cleaned
                })
                
                # Track provisional species name
                prov_species = get_field_value(obs, 'Provisional Species Name')
                if prov_species:
                    # Sanitize the species name for display
                    prov_species = re.sub(r'[<>]', '', str(prov_species)).strip()
                    if prov_species:
                        provisional_species_counts[prov_species] = \
                            provisional_species_counts.get(prov_species, 0) + 1
    
    # Sort provisional species by count (descending) like "sort | uniq -c | sort -n"
    sorted_species = sorted(
        provisional_species_counts.items(),
        key=lambda x: (-x[1], x[0])  # Sort by count desc, then name asc
    )
    
    return {
        'total_observations': len(observations),
        'dna_observations': dna_observations,
        'dna_count': len(dna_observations),
        'provisional_species': [
            {'name': name, 'count': count}
            for name, count in sorted_species
        ]
    }


def analyze_provisional_species(observations: List[Dict]) -> List[Dict]:
    """
    Analyze observations specifically for Provisional Species Names statistics.
    
    Args:
        observations: List of observations from PSN query
        
    Returns:
        List of dicts [{'name': '...', 'count': 123}, ...]
    """
    provisional_species_counts = {}
    
    for obs in observations:
        prov_species = get_field_value(obs, 'Provisional Species Name')
        if prov_species:
            # Sanitize the species name for display
            prov_species = re.sub(r'[<>]', '', str(prov_species)).strip()
            if prov_species:
                provisional_species_counts[prov_species] = \
                    provisional_species_counts.get(prov_species, 0) + 1
                    
    # Sort by count desc, then name asc
    sorted_species = sorted(
        provisional_species_counts.items(),
        key=lambda x: (-x[1], x[0])
    )
    
    return [
        {'name': name, 'count': count}
        for name, count in sorted_species
    ]


def extract_sequences_from_observations(dna_observations: List[Dict]) -> List[Dict]:
    """
    Convert analyzed observations to sequence dicts for the queue.
    
    Args:
        dna_observations: List of observation analysis dicts
        
    Returns:
        List of sequence dicts compatible with the sequence queue
    """
    sequences = []
    
    for obs_data in dna_observations:
        obs = obs_data['observation']
        cleaned_dna = obs_data['cleaned_dna']
        
        # Build a meaningful name from observation data
        obs_id = obs.get('id', 'unknown')
        
        # Priority: 
        # 1. Species Name Override (manual override)
        # 2. Provisional Species Name (more specific for DNA-barcoded specimens)
        # 3. Taxon name from iNaturalist identification
        
        species_name = get_field_value(obs, 'Species Name Override')
        if not species_name:
            species_name = get_field_value(obs, 'Provisional Species Name')
        
        if not species_name:
            taxon = obs.get('taxon', {})
            if taxon:
                species_name = taxon.get('name', '')
        
        # Sanitize species name
        species_name = re.sub(r'[<>"\']', '', str(species_name or '')).strip()
        location_label = _extract_location_label(obs)
        
        # Build sequence name with the observation ID and location visible in tree labels.
        seq_name_parts = [f"iNat{obs_id}"]
        if location_label:
            seq_name_parts.append(location_label)
        seq_name = " ".join(seq_name_parts)
        
        sequences.append({
            'name': seq_name,
            'organism': species_name,
            'sequence': cleaned_dna,
            'source': 'inaturalist',
            'hit_source': 'inat_observation',
            'location': location_label,
            'observation_id': str(obs_id)
        })
    
    return sequences


def fetch_inaturalist_data(url: str, mode: str = 'all',
                           time_budget: Optional[float] = None) -> Dict:
    """
    Main entry point: validate URL and fetch/analyze observations.
    
    Args:
        url: iNaturalist URL (observation or search)
        mode: 'all' (default) to fetch both ITS and PSN data (for analysis),
              or 'its_only' to fetch only ITS sequences (for adding to queue).
        time_budget: Seconds of wall clock the paginated search may spend in
              total, shared across the ITS and PSN queries. Callers running
              inside a web request must pass one (see
              INTERACTIVE_FETCH_BUDGET_SECONDS); background callers leave it
              None to fetch everything. When the budget runs out the result is
              returned with 'truncated' and 'timed_out' set.
        
    Returns:
        Dict with analysis results and extracted sequences
    """
    # Validate and parse URL
    url_info = validate_inaturalist_url(url)
    if not url_info:
        raise ValueError(
            "Invalid iNaturalist input. Please enter a 7 to 9 digit observation ID, "
            "a valid observation URL (e.g., https://www.inaturalist.org/observations/1234567), or "
            "observations search URL."
        )
    
    # Fetch observations based on URL type
    truncated = False
    
    # Initialize result containers
    final_result = {
        'total_observations': 0,
        'total_its_observations': 0,
        'total_psn_observations': 0,
        'dna_count': 0,
        'dna_observations': [],
        'provisional_species': [],
        'sequences': [],
        'is_single': url_info['type'] == 'single_observation',
        'truncated': False,
        'timed_out': False,
        'total_available': 0
    }

    deadline = None if time_budget is None else time.monotonic() + time_budget
    
    if url_info['type'] == 'single_observation':
        # Single observation: fetch once, analyze for both
        observation = fetch_single_observation(url_info['observation_id'])
        
        # Analyze ITS
        analysis = analyze_observations([observation])
        sequences = extract_sequences_from_observations(analysis['dna_observations'])
        
        # Analyze PSN independently (regardless of whether ITS exists)
        psn_histogram = analyze_provisional_species([observation])

        from app.services.inaturalist_tree_service import MYCOMAP_BLAST_FIELD_NAME
        mycomap_blast_url = get_field_value(observation, MYCOMAP_BLAST_FIELD_NAME)

        final_result.update({
            'total_observations': 1,
            'total_its_observations': 1 if analysis['dna_count'] > 0 else 0,
            'total_psn_observations': 1 if psn_histogram else 0,
            'fetched_its_count': 1,
            'fetched_psn_count': 1,
            'dna_count': analysis['dna_count'],
            'dna_observations': analysis['dna_observations'],
            'provisional_species': psn_histogram,
            'sequences': sequences,
            'mycomap_blast_url': mycomap_blast_url,
        })
        
    else:
        # Search URL: perform queries based on mode
        base_params = build_base_params(url_info.get('query_params', {}))
        
        # 1. Fetch ITS observations (needed in all modes)
        its_result = fetch_observations_with_field_filter(
            base_params, "DNA Barcode ITS", deadline=deadline)
        its_obs = its_result['observations']
        
        # Analyze ITS results
        its_analysis = analyze_observations(its_obs)
        sequences = extract_sequences_from_observations(its_analysis['dna_observations'])
        
        final_result.update({
             # Use ITS available count as primary "match" metric for sequences
            'total_observations': its_result['total_available'],
            'total_its_observations': its_result['total_available'],
            'fetched_its_count': its_result['fetched_count'],
            'dna_count': its_analysis['dna_count'],
            'dna_observations': its_analysis['dna_observations'],
            'sequences': sequences,
            'truncated': its_result['truncated'],
            'timed_out': its_result.get('timed_out', False),
            'total_available': its_result['total_available']
        })

        # 2. Fetch PSN observations (only if mode is 'all')
        if mode == 'all':
            psn_result = fetch_observations_with_field_filter(
                base_params, "Provisional Species Name", deadline=deadline)
            psn_obs = psn_result['observations']
            psn_histogram = analyze_provisional_species(psn_obs)
            
            final_result.update({
                'total_psn_observations': psn_result['total_available'],
                'fetched_psn_count': psn_result['fetched_count'],
                'provisional_species': psn_histogram,
                # Update truncated status to reflect if ANY query was truncated
                'truncated': its_result['truncated'] or psn_result['truncated'],
                'timed_out': (its_result.get('timed_out', False)
                              or psn_result.get('timed_out', False)),
                 # Update total available to be the max of either query
                'total_available': max(its_result['total_available'], psn_result['total_available'])
            })
        
    return final_result
