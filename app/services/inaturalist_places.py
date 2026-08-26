"""Standardized location labels for iNaturalist observations.

`place_guess` is free text the observer typed, so parsing it produced labels
that told the reader nothing: "Pike 93 N, Summit, MS, US" became
"Pike 93 N US", and a header that reads "N Gray Center Rd, Pickens, MS 39146,
USA" became "MS 39146 US". A zip code or a road name is not a location anyone
can place on a map.

Every observation also carries `place_ids` -- iNaturalist's *standardized*
places, derived from the coordinates rather than from what the observer typed.
Those include the administrative hierarchy (country / state / county / town),
which is what this module turns into a tip label like "Pike Co. MS US". It is
consistent across observations, so tips from the same county sort together, and
it still resolves when `place_guess` is missing entirely.

Place records are resolved through the **v2** endpoint, which accepts a `fields`
selector: the v1 equivalent returns each place's full geometry, which for one
tree's worth of places is well over a megabyte of GeoJSON we would immediately
throw away. Results are cached process-wide because admin places do not change
and the same states and counties recur in every tree.
"""

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

USER_AGENT = ("Dikarya Phylogenetic Tree Builder 1.0 - "
              "For questions contact Alan Rockefeller")
PLACES_API_BASE = "https://api.inaturalist.org/v2/places/"
# Asking for four scalar fields instead of the default record keeps a 100-place
# batch at a couple of kilobytes.
PLACE_FIELDS = "(id:!t,name:!t,admin_level:!t,display_name:!t)"
PLACE_REQUEST_TIMEOUT = 20
PLACE_BATCH_SIZE = 100
# Courtesy delay between batches, matching the rest of the iNaturalist client.
PLACE_BATCH_DELAY = 1.0
# A ceiling on how much of a very large tree we are willing to resolve inside
# one request. Anything beyond it falls back to the place_guess label.
MAX_PLACE_BATCHES = 12
# Tip labels have to stay readable; a pathological place name is truncated
# rather than allowed to dominate the tree.
MAX_LABEL_LENGTH = 60

ADMIN_COUNTRY = 0
ADMIN_STATE = 10
ADMIN_COUNTY = 20
ADMIN_TOWN = 30
ADMIN_LEVELS = (ADMIN_TOWN, ADMIN_COUNTY, ADMIN_STATE, ADMIN_COUNTRY)

# place id -> place record, or None for "fetched, not an admin place". The
# negative entries matter: an observation carries ~20 place ids and only three
# of them are administrative, so caching the misses is what keeps the second
# tree from re-fetching the same ecoregions.
_PLACE_CACHE: Dict[int, Optional[Dict[str, Any]]] = {}
_PLACE_CACHE_LOCK = threading.Lock()
_PLACE_CACHE_MAX = 20000


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


def location_label_from_place_guess(observation: Dict[str, Any]) -> str:
    """Compact label parsed out of the observer's free-text place.

    The fallback for when the standardized places cannot be resolved. Kept
    because a rough label beats no label at all.
    """
    for key in (
        "private_place_guess",
        "place_guess",
        "private_locality",
        "locality",
    ):
        raw = observation.get(key)
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


def _observation_place_ids(observation: Dict[str, Any]) -> List[int]:
    ids: List[int] = []
    for key in ("private_place_ids", "place_ids"):
        for value in observation.get(key) or []:
            try:
                place_id = int(value)
            except (TypeError, ValueError):
                continue
            if place_id > 0 and place_id not in ids:
                ids.append(place_id)
    return ids


def _cache_get(place_id: int, missing: object) -> Any:
    with _PLACE_CACHE_LOCK:
        return _PLACE_CACHE.get(place_id, missing)


def _cache_store(records: Dict[int, Optional[Dict[str, Any]]]) -> None:
    with _PLACE_CACHE_LOCK:
        if len(_PLACE_CACHE) + len(records) > _PLACE_CACHE_MAX:
            _PLACE_CACHE.clear()
        _PLACE_CACHE.update(records)


def _fetch_place_batch(place_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    """Resolve one batch of place ids. Returns {} on any upstream problem."""
    url = (f"{PLACES_API_BASE}{','.join(str(i) for i in place_ids)}"
           f"?fields={PLACE_FIELDS}")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=PLACE_REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, ValueError) as exc:
        logger.warning("iNaturalist place lookup failed for %d ids: %s",
                       len(place_ids), exc)
        return {}

    resolved: Dict[int, Dict[str, Any]] = {}
    for record in payload.get("results") or []:
        try:
            place_id = int(record.get("id"))
        except (TypeError, ValueError):
            continue
        resolved[place_id] = record
    return resolved


def _display_components(place: Dict[str, Any]) -> List[str]:
    return [part.strip()
            for part in str(place.get("display_name") or "").split(",")
            if part.strip()]


def _first_component(value: Any) -> str:
    """First comma-separated piece of a place name.

    A few iNaturalist places are named with their whole hierarchy inline
    (`"Oakland Township, Oakland County, MI, US"`), which would otherwise be
    pasted into the tip label verbatim.
    """
    return _clean_display_text(str(value or "").split(",")[0])


def _looks_like_region_code(token: str) -> bool:
    return 2 <= len(token) <= 3 and token.isalpha() and token.isupper()


def _label_from_places(places: Iterable[Dict[str, Any]]) -> str:
    """Build `<town or county> <state> <country>` from admin places."""
    by_level: Dict[int, Dict[str, Any]] = {}
    for place in places:
        level = place.get("admin_level")
        if level in ADMIN_LEVELS and level not in by_level:
            by_level[level] = place

    country = by_level.get(ADMIN_COUNTRY)
    state = by_level.get(ADMIN_STATE)
    county = by_level.get(ADMIN_COUNTY)
    town = by_level.get(ADMIN_TOWN)

    # iNaturalist spells the country code into the display_name of every place
    # below it ("Mississippi, US"), but a country's own display_name is just
    # its name, so fall back to normalizing that.
    country_code = ""
    if state:
        components = _display_components(state)
        if len(components) >= 2 and _looks_like_region_code(components[-1]):
            country_code = components[-1]
    if not country_code and country:
        country_code = _normalize_location_piece(_first_component(country.get("name")))

    # The state code sits in the sub-state display_name, but its position
    # differs by country ("Pike County, US, MS" vs "Francheville, QC, CA"), so
    # take whichever component looks like a code and is not the country.
    state_code = ""
    sub_state = town or county
    if sub_state:
        for component in _display_components(sub_state)[1:]:
            if component != country_code and _looks_like_region_code(component):
                state_code = component
                break
    if not state_code and state:
        state_code = _first_component(state.get("name"))

    locality = ""
    if town:
        locality = _first_component(town.get("name"))
    elif county:
        locality = _first_component(county.get("name"))
        # Level 20 is a county in the US and a regional county municipality in
        # Québec; only the ones iNaturalist itself calls counties get "Co.".
        if (locality
                and re.search(r"\bcounty\b", str(county.get("display_name") or ""), re.I)
                and not re.search(r"\bcounty\b", locality, re.I)):
            locality = f"{locality} Co."

    label = " ".join(part for part in (locality, state_code, country_code) if part)
    return label[:MAX_LABEL_LENGTH].strip()


def resolve_place_labels(observations: Sequence[Dict[str, Any]],
                         deadline: Optional[float] = None) -> Dict[int, str]:
    """Map observation id -> standardized location label.

    Resolves every observation's places in shared batches, so a 58-tip tree
    costs about three upstream calls rather than one per observation.
    Observations whose places cannot be resolved are simply absent from the
    result; callers fall back to `location_label_from_place_guess`.
    """
    wanted: Dict[int, List[int]] = {}
    for observation in observations or []:
        try:
            observation_id = int(observation.get("id"))
        except (TypeError, ValueError):
            continue
        place_ids = _observation_place_ids(observation)
        if place_ids:
            wanted[observation_id] = place_ids
    if not wanted:
        return {}

    sentinel = object()
    pending: List[int] = []
    seen = set()
    for place_ids in wanted.values():
        for place_id in place_ids:
            if place_id in seen:
                continue
            seen.add(place_id)
            if _cache_get(place_id, sentinel) is sentinel:
                pending.append(place_id)

    batches = 0
    fetched_all = True
    for start in range(0, len(pending), PLACE_BATCH_SIZE):
        if batches >= MAX_PLACE_BATCHES or (
                deadline is not None and time.monotonic() >= deadline):
            fetched_all = False
            break
        if batches:
            time.sleep(PLACE_BATCH_DELAY)
        batch = pending[start:start + PLACE_BATCH_SIZE]
        batches += 1
        resolved = _fetch_place_batch(batch)
        if not resolved:
            fetched_all = False
            continue
        _cache_store({
            place_id: (record if record.get("admin_level") in ADMIN_LEVELS else None)
            for place_id, record in ((pid, resolved.get(pid, {})) for pid in batch)
        })

    labels: Dict[int, str] = {}
    for observation_id, place_ids in wanted.items():
        places = []
        for place_id in place_ids:
            record = _cache_get(place_id, None)
            if record:
                places.append(record)
        label = _label_from_places(places)
        if label:
            labels[observation_id] = label

    if not fetched_all:
        try:
            from app.services.log_context import log_degradation_rate_limited
            log_degradation_rate_limited(
                logger, "inat_place_labels",
                "standardized iNaturalist places could not be fully resolved; "
                "some tip locations fall back to the observer's place_guess",
                observations=len(wanted), place_ids=len(pending), batches=batches,
            )
        except Exception:  # logging must never break an import
            logger.warning("iNaturalist place labels incomplete for %d observations",
                           len(wanted))
    return labels


OBSERVATION_BATCH_SIZE = 100
OBSERVATION_FETCH_ATTEMPTS = 4
OBSERVATIONS_API_BASE = "https://api.inaturalist.org/v2/observations"


def fetch_observation_places(observation_ids: Iterable[int],
                            deadline: Optional[float] = None
                            ) -> List[Dict[str, Any]]:
    """Fetch just enough of each observation to resolve its places.

    The v1 record carries the photos, identifications and comments too, which
    for 30 observations is three megabytes of JSON we would throw away -- and a
    long enough read to have been cut short in practice. v2's `fields` selector
    asks for the two keys `resolve_place_labels` needs.
    """
    ordered = sorted({int(value) for value in observation_ids})
    observations: List[Dict[str, Any]] = []
    for start in range(0, len(ordered), OBSERVATION_BATCH_SIZE):
        if deadline is not None and time.monotonic() >= deadline:
            break
        batch = ordered[start:start + OBSERVATION_BATCH_SIZE]
        url = (f"{OBSERVATIONS_API_BASE}?id={','.join(str(i) for i in batch)}"
               f"&per_page={OBSERVATION_BATCH_SIZE}&fields=(id:!t,place_ids:!t)")
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        payload = None
        for attempt in range(1, OBSERVATION_FETCH_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=PLACE_REQUEST_TIMEOUT) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception as exc:  # includes IncompleteRead, which is not a URLError
                if attempt == OBSERVATION_FETCH_ATTEMPTS:
                    logger.warning("iNaturalist observation lookup failed for %d ids: %s",
                                   len(batch), exc)
                    break
                time.sleep(2 ** attempt)
        if payload:
            observations.extend(payload.get("results") or [])
        if start + OBSERVATION_BATCH_SIZE < len(ordered):
            time.sleep(PLACE_BATCH_DELAY)
    return observations


def fill_missing_inat_locations(sequences: Sequence[Dict[str, Any]],
                                deadline: Optional[float] = None) -> int:
    """Give iNat-referenced queue records with no location one from the places.

    MycoMap supplies its own location for most hits and that one is left alone
    -- it is what the "Refresh MycoMap records" button syncs to. This only
    fills the blanks, and only for records whose label carries an iNaturalist
    observation number to resolve. Returns how many were filled.
    """
    from app.services.mycomap_service import extract_inaturalist_observation_ids

    targets: List[tuple] = []
    for sequence in sequences or []:
        if str(sequence.get("location") or "").strip():
            continue
        observation_ids = extract_inaturalist_observation_ids(
            str(sequence.get("name") or ""))
        if observation_ids:
            targets.append((sequence, int(observation_ids[0])))
    if not targets:
        return 0

    observations = fetch_observation_places(
        (observation_id for _sequence, observation_id in targets), deadline=deadline)
    if not observations:
        return 0
    labels = resolve_place_labels(observations, deadline=deadline)

    filled = 0
    for sequence, observation_id in targets:
        location = labels.get(observation_id, "")
        if not location:
            continue
        sequence["location"] = location
        name = str(sequence.get("name") or "").strip()
        if name and location.casefold() not in name.casefold():
            sequence["name"] = f"{name} {location}"
        filled += 1
    return filled


def location_label_for_observation(observation: Dict[str, Any],
                                   deadline: Optional[float] = None) -> str:
    """Standardized label for one observation, falling back to place_guess."""
    labels = resolve_place_labels([observation], deadline=deadline)
    try:
        observation_id = int(observation.get("id"))
    except (TypeError, ValueError):
        observation_id = 0
    return labels.get(observation_id) or location_label_from_place_guess(observation)
