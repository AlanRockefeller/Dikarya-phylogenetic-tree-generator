"""Collection-location lookup for GenBank accessions.

Backs the Tree Builder's "add collection locations to FASTA headers" option.
GenBank keeps the collection site as a source-feature qualifier named
``geo_loc_name`` -- what INSDC renamed ``country`` to in 2023, so older records
still carry the old name -- and its value is already display ready
(``USA: Colorado``). When a record has no textual location we fall back to
reverse-geocoding its ``lat_lon`` qualifier. That fallback is rare, so a
per-record geocoder call is affordable.
"""

import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from app.config import Config
from app.services.blast_service import (
    NCBI_EFETCH_URL,
    _ncbi_request,
    _parse_genbank_xml,
)

logger = logging.getLogger(__name__)

# Source qualifiers holding a human-readable collection site, best first.
TEXT_LOCATION_QUALIFIERS = ("geo_loc_name", "country")

# NCBI's own guidance is a few hundred ids per efetch; 100 keeps each response
# small enough to parse quickly even when every record is annotation-heavy.
EFETCH_BATCH_SIZE = 100

# Nominatim's usage policy allows one request per second from a single client.
_GEOCODE_MIN_GAP_SECONDS = 1.1
_geocode_lock = threading.Lock()
_geocode_last_request = 0.0

# A published record's collection site never changes, so an in-process cache is
# enough -- it just has to outlive a burst of Add clicks, not a deploy.
_CACHE_MAX_ENTRIES = 20000
_cache_lock = threading.Lock()
_location_cache: Dict[str, str] = {}
_geocode_cache: Dict[Tuple[float, float], str] = {}

_LAT_LON_RE = re.compile(
    r'^\s*(\d+(?:\.\d+)?)\s*([NS])[\s,]+(\d+(?:\.\d+)?)\s*([EW])\s*$',
    re.IGNORECASE
)


def _cache_get(key: str) -> Optional[str]:
    with _cache_lock:
        return _location_cache.get(key)


def _cache_put(keys: List[str], location: str) -> None:
    with _cache_lock:
        if len(_location_cache) > _CACHE_MAX_ENTRIES:
            _location_cache.clear()
        for key in keys:
            if key:
                _location_cache[key] = location


def _clean_location_text(value: str) -> str:
    """Collapse whitespace and drop trailing punctuation from a location."""
    text = " ".join(str(value or "").split())
    return text.strip(" ,;:")


def parse_lat_lon(value: str) -> Optional[Tuple[float, float]]:
    """Parse a GenBank ``lat_lon`` value (``39.53 N 105.28 W``) to decimals."""
    match = _LAT_LON_RE.match(str(value or ""))
    if not match:
        return None

    lat = float(match.group(1))
    lon = float(match.group(3))
    if match.group(2).upper() == "S":
        lat = -lat
    if match.group(4).upper() == "W":
        lon = -lon

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _format_geocoded_address(address: Dict) -> str:
    """Render a reverse-geocoder address in GenBank's ``Country: Region`` style."""
    country = _clean_location_text(address.get("country"))
    if str(address.get("country_code") or "").lower() == "us":
        # GenBank writes the United States as "USA"; match it so locations from
        # coordinates and locations from geo_loc_name look the same in a tree.
        country = "USA"
    region = _clean_location_text(
        address.get("state")
        or address.get("province")
        or address.get("region")
        or address.get("county")
    )

    if country and region:
        return f"{country}: {region}"
    return country or region


def reverse_geocode(lat: float, lon: float) -> str:
    """Turn coordinates into a place name, or return '' if unavailable."""
    global _geocode_last_request

    if not Config.REVERSE_GEOCODE_ENABLED:
        return ""

    key = (round(lat, 3), round(lon, 3))
    with _cache_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]

    with _geocode_lock:
        elapsed = time.monotonic() - _geocode_last_request
        if elapsed < _GEOCODE_MIN_GAP_SECONDS:
            time.sleep(_GEOCODE_MIN_GAP_SECONDS - elapsed)

        try:
            response = requests.get(
                Config.REVERSE_GEOCODE_URL,
                params={
                    "format": "jsonv2",
                    "lat": f"{lat:.6f}",
                    "lon": f"{lon:.6f}",
                    # Zoom 8 resolves to county/state rather than a street address,
                    # which is the granularity GenBank records use.
                    "zoom": "8",
                    "accept-language": "en",
                },
                headers={"User-Agent": f"Dikarya/1.0 ({Config.BLAST_EMAIL})"},
                timeout=(10, 20),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            logger.warning(f"Reverse geocode failed for {lat},{lon}: {e}")
            return ""
        finally:
            _geocode_last_request = time.monotonic()

    location = _format_geocoded_address(payload.get("address") or {})
    with _cache_lock:
        if len(_geocode_cache) > _CACHE_MAX_ENTRIES:
            _geocode_cache.clear()
        _geocode_cache[key] = location
    return location


def _location_from_record(record: Dict) -> str:
    """Pick the best available location for one parsed GenBank record."""
    source = record.get("source_features") or {}

    for qualifier in TEXT_LOCATION_QUALIFIERS:
        text = _clean_location_text(source.get(qualifier))
        if text:
            return text

    coordinates = parse_lat_lon(source.get("lat_lon"))
    if coordinates:
        return reverse_geocode(*coordinates)

    return ""


def _fetch_annotation_xml(accessions: List[str]) -> Optional[str]:
    """Fetch GenBank XML for a batch, without the sequence data."""
    params = {
        "db": "nuccore",
        "id": ",".join(accessions),
        "rettype": "gb",
        "retmode": "xml",
        # Only the annotation matters here, and slicing to a single base keeps a
        # mistyped genome accession from pulling megabytes of sequence.
        "seq_start": "1",
        "seq_stop": "1",
    }

    try:
        response = _ncbi_request("POST", NCBI_EFETCH_URL, data=params, timeout=(15, 90))
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"GenBank location fetch failed for {accessions[:3]}...: {e}")
        return None


def lookup_locations(accessions: List[str]) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Look up collection locations for GenBank accessions.

    Returns ``(locations, missing, unavailable)``.

    ``locations`` is keyed by both the bare accession and the versioned
    accession (both uppercase) so a caller can match whichever form appeared in
    the user's FASTA header.

    ``missing`` lists accessions NCBI answered about and had no location for --
    a fact about the record.

    ``unavailable`` lists accessions we could not ask about, because their whole
    efetch batch failed. These used to be folded into ``missing``, which turned
    an NCBI outage into the claim that a hundred records have no collection
    site: wrong, and wrong in the direction that makes a user stop asking. A
    caller should say "could not be checked" and offer a retry.
    """
    requested = []
    seen = set()
    for accession in accessions:
        normalized = str(accession or "").strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            requested.append(normalized)

    locations: Dict[str, str] = {}
    to_fetch = []
    for accession in requested:
        cached = _cache_get(accession)
        if cached is None:
            to_fetch.append(accession)
        elif cached:
            locations[accession] = cached

    unavailable_set = set()
    for start in range(0, len(to_fetch), EFETCH_BATCH_SIZE):
        batch = to_fetch[start:start + EFETCH_BATCH_SIZE]
        xml_text = _fetch_annotation_xml(batch)
        if not xml_text:
            # The request failed; nothing was learned about any id in it.
            unavailable_set.update(batch)
            continue

        parsed = _parse_genbank_xml(xml_text)
        for record in parsed.get("by_acc", {}).values():
            location = _location_from_record(record)
            keys = [
                str(record.get("accession") or "").upper(),
                str(record.get("version") or "").upper(),
            ]
            _cache_put(keys, location)
            if location:
                for key in keys:
                    if key:
                        locations[key] = location

    # A header may cite "MJ505555" while NCBI answers with "MJ505555.1" (or cite
    # a version NCBI has since superseded), so resolve each requested accession
    # against its bare form too before deciding it went unanswered.
    missing = []
    unavailable = []
    for accession in requested:
        base = accession.split(".")[0]
        if accession not in locations and base in locations:
            locations[accession] = locations[base]
        if accession not in locations and _cache_get(accession) is None and _cache_get(base) is None:
            if accession in unavailable_set:
                unavailable.append(accession)
            else:
                missing.append(accession)

    if unavailable:
        from app.services.log_context import log_degradation
        log_degradation(
            logger, "genbank_locations_unavailable",
            "GenBank could not be queried for some accessions, so their "
            "collection locations are unknown rather than absent",
            count=len(unavailable),
        )

    return locations, missing, unavailable
