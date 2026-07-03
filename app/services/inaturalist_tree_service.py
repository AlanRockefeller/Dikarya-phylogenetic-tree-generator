"""iNaturalist → Dikarya tree integration.

Takes a single iNaturalist observation, reads its "Mycomap BLAST Results"
field, imports those sequences via the existing MycoMap pipeline, queues a
one-click Dikarya tree job, and (later, in the worker) writes the public
tree URL back to the observation's "Phylogenetic Tree" field.

All iNaturalist writes use the site-wide authorized account configured via
the OAuth flow in app/services/inaturalist_oauth_service.py — they do NOT
use the logged-in Dikarya user's iNaturalist account.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from flask import current_app

logger = logging.getLogger(__name__)

INAT_API_BASE = "https://api.inaturalist.org/v1"
USER_AGENT = "Dikarya Phylogenetic Tree Builder 1.0"
REQUEST_TIMEOUT = 30
MAX_RAW_INPUT_LEN = 300
MAX_PER_PAGE = 200
RATE_LIMIT_DELAY = 1.0

MYCOMAP_BLAST_FIELD_NAME = "Mycomap BLAST Results"
PHYLOGENETIC_TREE_FIELD_NAME = "Phylogenetic Tree"
PHYLOGENETIC_TREE_FIELD_DESC = (
    "Link to a Dikarya phylogenetic tree built from this observation's "
    "Mycomap BLAST Results."
)

OBS_URL_RE = re.compile(
    r"^https?://(?:www\.)?inaturalist\.org/observations/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
PLAIN_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,198}[A-Za-z0-9]$")


class InatTreeError(Exception):
    """User-facing validation error. Safe to surface as a 400/422."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_single_observation_input(raw: str) -> int:
    """Parse a bare numeric observation ID or a single-observation URL.

    Rejects search URLs, multiple IDs, non-iNaturalist hosts, and any other
    malformed input. Returns the numeric observation ID on success.
    """
    parsed = parse_inaturalist_tree_input(raw)
    if parsed.get("type") != "single_observation":
        raise InatTreeError(
            "Provide a single iNaturalist observation as either a numeric "
            "ID (e.g. 360934883) or a single-observation URL "
            "(https://www.inaturalist.org/observations/<id>). Search URLs "
            "are not accepted."
        )
    return int(parsed["observation_id"])


def parse_inaturalist_tree_input(raw_input: str) -> Dict[str, Any]:
    """Classify Tree Builder iNaturalist one-click input without API calls."""
    if raw_input is None:
        raise InatTreeError("No iNaturalist input provided.")
    raw = str(raw_input).strip()
    if not raw:
        raise InatTreeError("No iNaturalist input provided.")
    if len(raw) > MAX_RAW_INPUT_LEN:
        raise InatTreeError("Input is too long.")
    if raw.isdigit():
        if len(raw) > 12:
            raise InatTreeError("iNaturalist observation ID is implausibly long.")
        return {
            "type": "single_observation",
            "observation_id": int(raw),
            "raw": raw,
            "normalized": raw,
        }

    urlish = raw
    if re.match(r"^(?:www\.)?inaturalist\.org(?:/|$)", raw, re.IGNORECASE):
        urlish = f"https://{raw}"
    if re.match(r"^https?://", urlish, re.IGNORECASE):
        parsed = urllib.parse.urlparse(urlish)
        host = (parsed.hostname or "").lower()
        if host not in {"inaturalist.org", "www.inaturalist.org"}:
            raise InatTreeError("Only inaturalist.org URLs are accepted.")
        path_parts = [
            urllib.parse.unquote(part)
            for part in (parsed.path or "").split("/")
            if part
        ]
        query_params = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)
        if len(path_parts) >= 2 and path_parts[0].lower() == "observations":
            token = path_parts[1].strip()
            if token.isdigit():
                return {
                    "type": "single_observation",
                    "observation_id": int(token),
                    "raw": raw,
                    "normalized": f"https://www.inaturalist.org/observations/{int(token)}",
                }
            return {
                "type": "user_candidate",
                "value": token,
                "raw": raw,
                "normalized": token,
                "source": "observations_path",
            }
        if len(path_parts) == 1 and path_parts[0].lower() == "observations":
            project_values = query_params.get("project_id") or []
            project_value = _clean_candidate(project_values[0]) if project_values else ""
            if project_value:
                return {
                    "type": "project_candidate",
                    "value": project_value,
                    "raw": raw,
                    "normalized": project_value,
                    "source": "observations_project_id",
                }
        if len(path_parts) >= 2 and path_parts[0].lower() == "people":
            return {
                "type": "user_candidate",
                "value": path_parts[1].strip(),
                "raw": raw,
                "normalized": path_parts[1].strip(),
                "source": "people_path",
                "value_kind": "id" if path_parts[1].strip().isdigit() else "login",
            }
        if len(path_parts) >= 2 and path_parts[0].lower() == "projects":
            return {
                "type": "project_candidate",
                "value": path_parts[1].strip(),
                "raw": raw,
                "normalized": path_parts[1].strip(),
                "source": "projects_path",
            }
        raise InatTreeError("That iNaturalist URL is not supported for one-click trees.")

    if not PLAIN_TOKEN_RE.match(raw):
        raise InatTreeError("Enter an observation ID, iNaturalist username, project name, or iNaturalist URL.")
    return {
        "type": "plain_candidate",
        "value": re.sub(r"\s+", " ", raw).strip(),
        "raw": raw,
        "normalized": re.sub(r"\s+", " ", raw).strip(),
    }


# ---------------------------------------------------------------------------
# iNaturalist HTTP helpers
# ---------------------------------------------------------------------------

def _http_request(url: str, *, method: str = "GET", body: Optional[Dict[str, Any]] = None,
                   bearer: Optional[str] = None) -> Dict[str, Any]:
    data = None
    headers = {
        'Accept': 'application/json',
        'User-Agent': USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if bearer:
        headers['Authorization'] = f'Bearer {bearer}'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8') or '{}'
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode('utf-8', errors='replace')[:200]
        except Exception:
            detail = ''
        logger.warning("iNat %s %s failed: HTTP %s", method, url, e.code)
        raise InatTreeError(
            f"iNaturalist API returned HTTP {e.code}.",
            status=502 if e.code >= 500 else 400,
        )
    except urllib.error.URLError as e:
        raise InatTreeError(f"iNaturalist network error: {e.reason}", status=502)


def fetch_observation(observation_id: int) -> Dict[str, Any]:
    url = f"{INAT_API_BASE}/observations/{int(observation_id)}"
    payload = _http_request(url)
    results = (payload or {}).get('results') or []
    if not results:
        raise InatTreeError(f"iNaturalist observation {observation_id} was not found.", status=404)
    return results[0]


def _clean_candidate(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _has_nonempty_field(observation: Dict[str, Any], field_name: str) -> bool:
    value = extract_observation_field_value(observation, field_name)
    return bool(str(value or "").strip())


def _lookup_inaturalist_user_exact(value: str) -> Optional[Dict[str, Any]]:
    candidate = _clean_candidate(value)
    if not candidate:
        return None
    q = urllib.parse.urlencode({"q": candidate})
    payload = _http_request(f"{INAT_API_BASE}/users/autocomplete?{q}")
    for item in payload.get("results") or []:
        login = _clean_candidate(item.get("login"))
        if login.casefold() == candidate.casefold():
            return {
                "id": item.get("id"),
                "login": login,
                "display_name": login,
                "raw": item,
            }
    return None


def _lookup_inaturalist_user_by_id(value: str) -> Optional[Dict[str, Any]]:
    candidate = _clean_candidate(value)
    if not candidate.isdigit():
        return None
    payload = _http_request(f"{INAT_API_BASE}/users/{int(candidate)}")
    results = payload.get("results") or []
    if not results:
        return None
    item = results[0]
    login = _clean_candidate(item.get("login"))
    if not login:
        return None
    return {
        "id": item.get("id"),
        "login": login,
        "display_name": login,
        "raw": item,
    }


def _lookup_inaturalist_project_exact(value: str) -> Optional[Dict[str, Any]]:
    candidate = _clean_candidate(value)
    if not candidate:
        return None
    q = urllib.parse.urlencode({"q": candidate})
    payload = _http_request(f"{INAT_API_BASE}/projects?{q}")
    for item in payload.get("results") or []:
        slug = _clean_candidate(item.get("slug"))
        title = _clean_candidate(item.get("title") or item.get("name"))
        if candidate.casefold() in {slug.casefold(), title.casefold()}:
            return {
                "id": item.get("id"),
                "slug": slug or candidate,
                "title": title or slug or candidate,
                "display_name": title or slug or candidate,
                "raw": item,
            }
    return None


def _scope_from_user(user_info: Dict[str, Any]) -> Dict[str, Any]:
    login = _clean_candidate(user_info.get("login"))
    return {
        "type": "user",
        "value": login,
        "display_name": login,
        "query_params": {"user_login": login},
        "user": user_info,
    }


def _scope_from_project(project_info: Dict[str, Any]) -> Dict[str, Any]:
    project_id = project_info.get("id")
    if not project_id:
        raise InatTreeError("The matching iNaturalist project did not include an ID.", status=502)
    return {
        "type": "project",
        "value": _clean_candidate(project_info.get("slug")) or str(project_id),
        "display_name": _clean_candidate(project_info.get("display_name")) or str(project_id),
        "query_params": {"project_id": int(project_id)},
        "project": project_info,
    }


def resolve_inaturalist_user_or_project(parsed: Dict[str, Any],
                                        preferred_type: Optional[str] = None
                                        ) -> Dict[str, Any]:
    """Resolve a parsed username/project candidate through iNaturalist."""
    preferred_type = (preferred_type or "").strip().lower() or None
    if preferred_type not in {None, "user", "project"}:
        raise InatTreeError("resolved_type must be 'user' or 'project'.")

    kind = parsed.get("type")
    value = _clean_candidate(parsed.get("value"))
    if kind == "single_observation":
        return {"type": "single_observation", "observation_id": parsed["observation_id"]}
    if not value:
        return {
            "type": "not_found",
            "message": f"No iNaturalist user or project was found for '{parsed.get('raw') or ''}'.",
        }

    user_match = None
    project_match = None
    if kind in {"user_candidate", "plain_candidate"} and preferred_type in {None, "user"}:
        if parsed.get("source") == "people_path" and parsed.get("value_kind") == "id":
            user_match = _lookup_inaturalist_user_by_id(value)
        else:
            user_match = _lookup_inaturalist_user_exact(value)
    if kind in {"project_candidate", "plain_candidate"} and preferred_type in {None, "project"}:
        project_match = _lookup_inaturalist_project_exact(value)

    if preferred_type == "user":
        if user_match:
            return {"type": "user", "scope": _scope_from_user(user_match)}
        return {
            "type": "not_found",
            "message": f"No iNaturalist user or project was found for '{parsed.get('raw') or value}'.",
        }
    if preferred_type == "project":
        if project_match:
            return {"type": "project", "scope": _scope_from_project(project_match)}
        return {
            "type": "not_found",
            "message": f"No iNaturalist user or project was found for '{parsed.get('raw') or value}'.",
        }
    if user_match and project_match:
        return {
            "type": "ambiguous",
            "message": "This matches both a user and a project. Choose which one to use.",
            "choices": {
                "user": {
                    "login": user_match.get("login"),
                    "display_name": user_match.get("display_name"),
                },
                "project": {
                    "slug": project_match.get("slug"),
                    "display_name": project_match.get("display_name"),
                },
            },
        }
    if user_match:
        return {"type": "user", "scope": _scope_from_user(user_match)}
    if project_match:
        return {"type": "project", "scope": _scope_from_project(project_match)}
    return {
        "type": "not_found",
        "message": f"No iNaturalist user or project was found for '{parsed.get('raw') or value}'.",
    }


def _fetch_scope_observation_page(scope: Dict[str, Any], page: int,
                                  *, require_mycomap: bool = False,
                                  per_page: int = MAX_PER_PAGE) -> Dict[str, Any]:
    params = dict(scope.get("query_params") or {})
    params.update({
        "page": int(page),
        "per_page": int(per_page),
        "order_by": "id",
        "order": "asc",
    })
    if require_mycomap:
        params[f"field:{MYCOMAP_BLAST_FIELD_NAME}"] = ""
    query = urllib.parse.urlencode(params, doseq=True)
    return _http_request(f"{INAT_API_BASE}/observations?{query}")


def _scope_total_observations(scope: Dict[str, Any]) -> int:
    payload = _fetch_scope_observation_page(scope, 1, per_page=1)
    return int(payload.get("total_results") or 0)


def _collect_tree_eligible_observations(scope: Dict[str, Any]) -> Dict[str, Any]:
    total_matching = _scope_total_observations(scope)
    observations = []
    page = 1
    total_with_mycomap = 0
    skipped_existing_tree = 0

    while True:
        payload = _fetch_scope_observation_page(scope, page, require_mycomap=True)
        results = payload.get("results") or []
        if page == 1:
            total_with_mycomap = int(payload.get("total_results") or 0)
        if not results:
            break
        for obs in results:
            if _has_nonempty_field(obs, PHYLOGENETIC_TREE_FIELD_NAME):
                skipped_existing_tree += 1
                continue
            if _has_nonempty_field(obs, MYCOMAP_BLAST_FIELD_NAME):
                observations.append(obs)
        if len(results) < MAX_PER_PAGE or (page * MAX_PER_PAGE) >= total_with_mycomap:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    skipped_missing_mycomap = max(0, total_matching - total_with_mycomap)
    return {
        "scope": scope,
        "total_matching_observations": total_matching,
        "mycomap_matching_observations": total_with_mycomap,
        "eligible_tree_count": len(observations),
        "skipped_existing_tree_count": skipped_existing_tree,
        "skipped_missing_mycomap_count": skipped_missing_mycomap,
        "observations": observations,
    }


def count_tree_eligible_observations(scope: Dict[str, Any]) -> Dict[str, Any]:
    counts = _collect_tree_eligible_observations(scope)
    counts.pop("observations", None)
    return counts


def iter_tree_eligible_observations(scope: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for obs in _collect_tree_eligible_observations(scope).get("observations") or []:
        yield obs


def _scope_found_label(scope: Dict[str, Any]) -> str:
    return f"{scope.get('type')} {scope.get('display_name') or scope.get('value')}"


def _message_for_scope_counts(scope: Dict[str, Any], counts: Dict[str, Any]) -> str:
    label = _scope_found_label(scope)
    ready = int(counts.get("eligible_tree_count") or 0)
    skipped_tree = int(counts.get("skipped_existing_tree_count") or 0)
    skipped_myco = int(counts.get("skipped_missing_mycomap_count") or 0)
    if ready:
        noun = "observation is" if ready == 1 else "observations are"
        priority = " high-priority" if ready == 1 else " bulk"
        message = (
            f"Found {label}. {ready} {noun} ready for tree building. "
            f"Clicking One-Click Tree will create {ready}{priority} tree job"
            f"{'' if ready == 1 else 's'}."
        )
        if skipped_tree or skipped_myco:
            message += (
                f" Skipped {skipped_tree} observations that already had trees "
                f"and {skipped_myco} without Mycomap BLAST Results."
            )
        return message
    if skipped_tree and not skipped_myco:
        return f"Found {label}, but all matching observations already have Phylogenetic Tree fields."
    if skipped_myco and not skipped_tree:
        return f"Found {label}, but none of the matching observations have a Mycomap BLAST Results field."
    return f"Found {label}, but no matching observations are ready for tree building."


def preview_inaturalist_tree_input(raw_input: str,
                                   resolved_type: Optional[str] = None
                                   ) -> Dict[str, Any]:
    parsed = parse_inaturalist_tree_input(raw_input)
    if parsed.get("type") == "single_observation":
        observation_id = int(parsed["observation_id"])
        observation = fetch_observation(observation_id)
        has_mycomap = _has_nonempty_field(observation, MYCOMAP_BLAST_FIELD_NAME)
        has_tree = _has_nonempty_field(observation, PHYLOGENETIC_TREE_FIELD_NAME)
        if has_mycomap and has_tree:
            message = (
                f"Found observation {observation_id}. It already has a "
                "Phylogenetic Tree field."
            )
        elif has_mycomap:
            message = (
                f"Found observation {observation_id}. It has Mycomap BLAST "
                "Results and is ready for one high-priority tree job."
            )
        else:
            message = (
                f"Found observation {observation_id}, but it does not have a "
                "Mycomap BLAST Results field."
            )
        return {
            "status": "success",
            "type": "single_observation",
            "observation_id": observation_id,
            "has_mycomap_blast_results": has_mycomap,
            "has_phylogenetic_tree": has_tree,
            "eligible_tree_count": 1 if has_mycomap and not has_tree else 0,
            "total_matching_observations": 1,
            "skipped_existing_tree_count": 1 if has_tree else 0,
            "skipped_missing_mycomap_count": 0 if has_mycomap else 1,
            "message": message,
        }

    resolved = resolve_inaturalist_user_or_project(parsed, preferred_type=resolved_type)
    if resolved.get("type") in {"ambiguous", "not_found"}:
        return {"status": "success", **resolved}
    scope = resolved["scope"]
    counts = count_tree_eligible_observations(scope)
    return {
        "status": "success",
        "type": scope["type"],
        "scope_value": scope.get("value"),
        "display_name": scope.get("display_name"),
        **counts,
        "message": _message_for_scope_counts(scope, counts),
    }


def find_observation_field_value_record(observation: Dict[str, Any], field_name: str) -> Optional[Dict[str, Any]]:
    """Return the OFV record for the named field, or None."""
    target = (field_name or '').strip().lower()
    for ofv in observation.get('ofvs') or []:
        name = (ofv.get('name') or '').strip().lower()
        if not name:
            of = ofv.get('observation_field') or {}
            name = (of.get('name') or '').strip().lower()
        if name == target:
            return ofv
    return None


def extract_observation_field_value(observation: Dict[str, Any], field_name: str) -> Optional[str]:
    rec = find_observation_field_value_record(observation, field_name)
    if not rec:
        return None
    val = rec.get('value')
    return None if val is None else str(val)


def get_observation_field_id_by_name(field_name: str) -> int:
    """Look up an iNaturalist observation field's numeric ID by name.

    Uses the v1 autocomplete endpoint (the bare /v1/observation_fields
    path returns 404). Prefers an exact case-insensitive match; if none,
    falls back to the first autocomplete result for that exact query.
    """
    q = urllib.parse.urlencode({'q': field_name})
    payload = _http_request(f"{INAT_API_BASE}/observation_fields/autocomplete?{q}")
    results = payload.get('results') or []
    target = field_name.strip().lower()
    for r in results:
        if (r.get('name') or '').strip().lower() == target:
            return int(r['id'])
    if results:
        return int(results[0]['id'])
    raise InatTreeError(
        f"iNaturalist observation field '{field_name}' was not found. "
        "An admin may need to create it first.",
        status=502,
    )


# ---------------------------------------------------------------------------
# Writing back to iNaturalist (uses the site-wide OAuth/JWT token)
# ---------------------------------------------------------------------------

def set_observation_field_value(observation_id: int, field_name: str, value: str) -> Dict[str, Any]:
    """Create or update an observation_field_value for the given observation.

    Uses the site-wide JWT (NOT the requesting user's account). If a value
    for ``field_name`` already exists on the observation, that OFV is
    updated in place rather than creating a duplicate.
    """
    from app.services.inaturalist_oauth_service import get_api_jwt
    jwt = get_api_jwt()
    obs = fetch_observation(observation_id)
    existing = find_observation_field_value_record(obs, field_name)
    field_id = (
        int(existing.get('observation_field', {}).get('id'))
        if existing and existing.get('observation_field', {}).get('id')
        else get_observation_field_id_by_name(field_name)
    )
    body = {
        "observation_field_value": {
            "observation_id": int(observation_id),
            "observation_field_id": int(field_id),
            "value": str(value),
        }
    }
    if existing and existing.get('uuid'):
        url = f"{INAT_API_BASE}/observation_field_values/{existing['uuid']}"
        return _http_request(url, method='PUT', body=body, bearer=jwt) or {}
    elif existing and existing.get('id'):
        url = f"{INAT_API_BASE}/observation_field_values/{existing['id']}"
        return _http_request(url, method='PUT', body=body, bearer=jwt) or {}
    else:
        url = f"{INAT_API_BASE}/observation_field_values"
        return _http_request(url, method='POST', body=body, bearer=jwt) or {}


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------

# Default one-click tree parameters for the iNat → tree flow. Match the
# MycoMap "One-Click Tree" card defaults so behavior is predictable.
DEFAULT_TREE_PARAMS = {
    "input_type": "fasta",
    "alignment_method": "mafft",
    "trimming_method": "none",
    "trim_terminal_overhangs": True,
    "tree_method": "fasttree",
    "tree_model": "GTR+G",
    "bootstrap": 1000,
    "mcmc_generations": 50000,
}


DNA_BARCODE_ITS_FIELD_NAME = "DNA Barcode ITS"
GENBANK_ACCESSION_RE = re.compile(r"^[A-Z]{1,3}_?[0-9]{5,9}(?:\.[0-9]+)?$")


def _normalize_dna_for_match(text: str) -> str:
    """Uppercase + strip everything that isn't a DNA / IUPAC nucleotide letter.

    Used only for the exact-match comparison; the cleaned sequence we
    actually splice into the tree input goes through clean_dna_sequence so
    it follows the same rules as the rest of the pipeline.
    """
    if not text:
        return ""
    return re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(text).upper())


def _observation_id_pattern(observation_id: int) -> re.Pattern:
    obs_id = str(int(observation_id))
    return re.compile(rf"(?<!\d){re.escape(obs_id)}(?!\d)", re.IGNORECASE)


def _name_has_observation_id(name: str, observation_id: int) -> bool:
    if not name or not observation_id:
        return False
    return bool(_observation_id_pattern(observation_id).search(str(name)))


def _name_has_inat_token(name: str, observation_id: int) -> bool:
    if not name or not observation_id:
        return False
    token = f"iNat{int(observation_id)}"
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![0-9])",
                          str(name), re.IGNORECASE))


def _extract_genbank_accession(name: str) -> str:
    first = str(name or "").split()[0] if str(name or "").split() else ""
    if GENBANK_ACCESSION_RE.match(first):
        return first
    return ""


def _source_tip_label_with_inat(name: str, observation_id: int) -> str:
    original = str(name or "").strip()
    inat_token = f"iNat{int(observation_id)}"
    if not original:
        return inat_token
    parts = original.split(None, 1)
    if _name_has_inat_token(original, observation_id):
        return original
    if _extract_genbank_accession(original):
        rest = parts[1] if len(parts) > 1 else ""
        return f"{parts[0]} {inat_token} {rest}".strip()
    return f"{inat_token} {original}".strip()


def _source_display_label_for_tip(tip_name: str, source_label: str,
                                  observation_id: int) -> str:
    """Keep an accession in the visible iNat source label when one exists."""
    source_label = _clean_display_text(source_label)
    if not source_label:
        return source_label
    accession = _extract_genbank_accession(tip_name)
    if accession and source_label.split()[0:1] != [accession]:
        return f"{accession} {source_label}"
    if observation_id and not _name_has_inat_token(source_label, observation_id):
        return _source_tip_label_with_inat(source_label, observation_id)
    return source_label


def _find_observation_source_tip_name(sequences: List[Dict[str, Any]],
                                      observation_id: int) -> Optional[str]:
    """Return the best existing tip name that appears to belong to the observation.

    Prefer a raw MycoMap tip that embeds the numeric observation ID but is not
    already an `iNat<id>` label. This avoids creating a second synthetic tip
    when the source observation is already represented in the imported BLAST
    results.
    """
    if not observation_id:
        return None
    obs_id = str(int(observation_id))
    inat_prefix = f"iNat{obs_id}"
    for s in sequences:
        name = str(s.get("name") or "").strip()
        if not name or name.startswith(inat_prefix):
            continue
        if _CONTAMINANT_LABEL_RE.search(name):
            continue
        if _name_has_observation_id(name, observation_id):
            return name
    return None


def _maybe_add_inat_its_sequence(observation: Dict[str, Any], observation_id: int,
                                  sequences: List[Dict[str, Any]]
                                  ) -> Tuple[Optional[str], Optional[str]]:
    """Ensure the observation's `DNA Barcode ITS` is represented in the tree.

    Returns ``(added_name, matched_name)``:
      - ``added_name``: header of a new record appended to ``sequences``,
        or None if nothing was added.
      - ``matched_name``: name of an existing MycoMap result whose
        sequence is an exact match for the ITS field, or None if no match.
    When ``matched_name`` is set the existing record is left in place
    (no duplicate); the highlighter colors it blue using this metadata.
    """
    raw_its = extract_observation_field_value(observation, DNA_BARCODE_ITS_FIELD_NAME)
    if not raw_its:
        return None, None
    from app.services.fasta_utils import clean_dna_sequence
    cleaned = clean_dna_sequence(raw_its) or ""
    if not cleaned:
        return None, None
    its_norm = _normalize_dna_for_match(cleaned)
    if not its_norm:
        return None, None

    source_exact_matches = []
    for idx, s in enumerate(sequences):
        name = str(s.get("name") or "").strip()
        if not name or _CONTAMINANT_LABEL_RE.search(name):
            continue
        if not _name_has_observation_id(name, observation_id):
            continue
        if _normalize_dna_for_match(s.get("sequence") or "") == its_norm:
            source_exact_matches.append((idx, s))

    if source_exact_matches:
        def source_match_score(item: Tuple[int, Dict[str, Any]]) -> Tuple[int, int, int]:
            idx, seq = item
            name = str(seq.get("name") or "")
            accession_penalty = 0 if _extract_genbank_accession(name) else 1
            synthetic_penalty = 1 if name.startswith(f"iNat{int(observation_id)}") else 0
            return accession_penalty, synthetic_penalty, idx

        keep_idx, keep_seq = sorted(source_exact_matches, key=source_match_score)[0]
        keep_name = _source_tip_label_with_inat(keep_seq.get("name") or "", observation_id)
        keep_seq["name"] = keep_name
        remove_indexes = {idx for idx, _seq in source_exact_matches if idx != keep_idx}
        if remove_indexes:
            sequences[:] = [
                seq for idx, seq in enumerate(sequences)
                if idx not in remove_indexes
            ]
        return None, keep_name

    source_tip_name = _find_observation_source_tip_name(sequences, observation_id)
    if source_tip_name:
        return None, source_tip_name

    for s in sequences:
        if _CONTAMINANT_LABEL_RE.search(str(s.get("name") or "")):
            continue
        if _normalize_dna_for_match(s.get("sequence") or "") == its_norm:
            # Rename the matching tip so the iNaturalist observation # is
            # visible directly in the tree label. The first token (usually
            # a GenBank accession) is preserved; iNat<id> is inserted
            # right after it. Example:
            #   "OQ256154 Beauveria sp. DAVFP-29733 British Columbia CA"
            # becomes
            #   "OQ256154 iNat360934883 Beauveria sp. DAVFP-29733 ..."
            original = str(s.get("name") or "")
            new_name = _source_tip_label_with_inat(original, observation_id)
            s["name"] = new_name
            return None, new_name
    # Name with the iNat<id> token so the highlighter colors it blue.
    name = f"iNat{int(observation_id)} (observation DNA Barcode ITS)"
    sequences.append({
        "name": name,
        "sequence": cleaned,
        "source": "inaturalist",
        "hit_source": "inat_observation",
        "identity": None,
        "query_cover": None,
        "subject_cover": None,
        "blast_metrics_available": False,
    })
    return name, None


def _clean_display_text(value: Any) -> str:
    """Normalize a free-form string for use in a tree tip label."""
    if value is None:
        return ""
    # Strip HTML/attribute-hazard characters (< > ") but KEEP single quotes:
    # provisional fungal names use them, e.g. Pluteus sp. 'flammasilvae'.
    text = re.sub(r'[<>"]', "", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ,;:-_")


def _extract_inat_species_label(observation: Dict[str, Any]) -> str:
    """Return the best scientific-name-like label for an iNat observation."""
    for field_name in ("Species Name Override", "Provisional Species Name"):
        value = extract_observation_field_value(observation, field_name)
        if value:
            cleaned = _clean_display_text(value)
            if cleaned:
                return cleaned

    taxon = observation.get("taxon") or {}
    if isinstance(taxon, dict):
        for key in ("name", "preferred_common_name"):
            cleaned = _clean_display_text(taxon.get(key))
            if cleaned:
                return cleaned
    return ""


def _normalize_location_piece(piece: str) -> str:
    """Collapse common country variants so labels stay short."""
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


def _extract_inat_location_label(observation: Dict[str, Any]) -> str:
    """Return a compact place label like `New Mexico US`."""
    for key in (
        "private_place_guess",
        "place_guess",
        "private_locality",
        "locality",
    ):
        raw = observation.get(key)
        if not raw:
            continue
        parts = [
            _normalize_location_piece(part)
            for part in str(raw).split(",")
        ]
        parts = [part for part in parts if part]
        if not parts:
            continue
        # Prefer the human-readable region name when iNat returns nested
        # place text like "New Mexico, NM, United States". In that case the
        # middle component is just an abbreviation and the first + last
        # components are the label people expect to see.
        if len(parts) >= 3 and len(parts[-2]) <= 3:
            return f"{parts[0]} {parts[-1]}".strip()
        if len(parts) >= 2:
            return " ".join(parts[-2:])
        return parts[0]
    return ""


def _build_inat_source_display_name(observation: Dict[str, Any], observation_id: int) -> str:
    """Build the concise source-tip label used in the tree viewer."""
    parts = [f"iNat{int(observation_id)}"]
    species = _extract_inat_species_label(observation)
    if species:
        parts.append(species)
    location = _extract_inat_location_label(observation)
    if location:
        parts.append(location)
    return " ".join(parts)


def _build_fasta_text(sequences: List[Dict[str, Any]]) -> str:
    parts = []
    for s in sequences:
        name = (s.get('name') or '').strip()
        seq = (s.get('sequence') or '').strip()
        if not name or not seq:
            continue
        parts.append(f">{name}\n{seq}\n")
    return ''.join(parts)


def create_job_from_inat_observation(raw_input: str, user=None,
                                      include_ncbi: bool = True,
                                      include_local: bool = True,
                                      public_base_url: Optional[str] = None,
                                      queue_name: str = "phylo_high",
                                      queue_class: str = "high",
                                      source: str = "inaturalist_single_tree",
                                      extra_metrics: Optional[Dict[str, Any]] = None
                                      ) -> Dict[str, Any]:
    """Validate the iNat input, pull sequences, enqueue a one-click tree job.

    Returns a dict with: status, job_id, observation_id, mycomap_blast_url,
    tree_view_url, tree_status_url, message.
    """
    from app.api.routes import gather_mycomap_sequences_for_queue
    from app.config import Config
    from app.extensions import db
    from app.models import Job
    from app.services.mycomap_service import validate_mycomap_url
    from app.workers.queue import enqueue_job

    observation_id = parse_single_observation_input(raw_input)
    observation = fetch_observation(observation_id)
    mycomap_url = extract_observation_field_value(observation, MYCOMAP_BLAST_FIELD_NAME)

    if not mycomap_url:
        raise InatTreeError(
            "This iNaturalist observation needs to have a “Mycomap "
            "BLAST Results” observation field containing a MycoMap "
            "BLAST result URL.",
            status=422,
        )

    mycomap_url = mycomap_url.strip()
    if not validate_mycomap_url(mycomap_url):
        raise InatTreeError(
            "The observation's Mycomap BLAST Results field does not "
            "contain a valid MycoMap BLAST URL.",
            status=422,
        )

    payload, err = gather_mycomap_sequences_for_queue(
        mycomap_url, include_ncbi=include_ncbi, include_local=include_local,
    )
    if err is not None:
        body, status = err
        raise InatTreeError(
            body.get('error', 'Failed to fetch MycoMap sequences.'),
            status=status if status in (400, 422, 502) else 502,
        )
    sequences = (payload or {}).get('sequences') or []
    if len(sequences) < 2:
        raise InatTreeError(
            "MycoMap returned fewer than 2 usable sequences; cannot build a tree.",
            status=422,
        )

    # If the observation has a `DNA Barcode ITS` field, ensure it's
    # represented in the tree. Either an exact match already exists in
    # the MycoMap results (we'll color that tip blue) or we append a
    # synthetic record named `iNat<id> ...` so the highlighter picks it
    # up automatically.
    added_inat_its, matched_inat_its_tip = _maybe_add_inat_its_sequence(
        observation, observation_id, sequences
    )

    fasta_text = _build_fasta_text(sequences)
    # The worker aliases "fasta" -> "fasta_upload" (file on disk). Inline
    # FASTA text must use "pasted_sequence" so the worker writes it to
    # input_raw.fasta itself.
    job_params = {
        "input_type": "pasted_sequence",
        "notes": f"iNaturalist obs {observation_id} → Phylogenetic Tree",
        "sequence": fasta_text,
        "accessions": [],
        "alignment_method": DEFAULT_TREE_PARAMS["alignment_method"],
        "trimming_method": DEFAULT_TREE_PARAMS["trimming_method"],
        "trim_terminal_overhangs": DEFAULT_TREE_PARAMS["trim_terminal_overhangs"],
        "alignment_options": {},
        "tree_method": DEFAULT_TREE_PARAMS["tree_method"],
        "tree_model": DEFAULT_TREE_PARAMS["tree_model"],
        "bootstrap": DEFAULT_TREE_PARAMS["bootstrap"],
        "mcmc_generations": DEFAULT_TREE_PARAMS["mcmc_generations"],
        "mcmc_nruns": 2,
        "mcmc_nchains": 4,
        "mycomap_blast_url": mycomap_url,
        "import_filter_details": (payload or {}).get("import_filter_details") or {},
    }

    rq_meta = {
        "queue_class": queue_class,
        "source": source,
    }
    if extra_metrics:
        rq_meta.update(extra_metrics)
    job_id = enqueue_job(job_params, queue_name=queue_name, meta=rq_meta)
    obs_source_url = f"https://www.inaturalist.org/observations/{observation_id}"
    public_base_url = _clean_display_text(public_base_url).rstrip("/")
    metrics = {
        "via": "inat_phylogenetic_tree",
        "tree_method": job_params["tree_method"],
        "alignment_method": job_params["alignment_method"],
        "trimming_method": job_params["trimming_method"],
        "trim_terminal_overhangs": job_params["trim_terminal_overhangs"],
        "notes": job_params["notes"],
        "inat_observation_id": observation_id,
        "inat_source_url": obs_source_url,
        "inat_source_display_name": _build_inat_source_display_name(observation, observation_id),
        "mycomap_blast_url": mycomap_url,
        "inat_update_status": "pending",
        "inat_observation_field": PHYLOGENETIC_TREE_FIELD_NAME,
        "inat_added_its_sequence": bool(added_inat_its),
        "queue_class": queue_class,
        "source": source,
    }
    if extra_metrics:
        metrics.update(extra_metrics)
    if added_inat_its:
        metrics["inat_added_its_name"] = added_inat_its
    if matched_inat_its_tip:
        # An existing MycoMap result already carried this exact sequence
        # (typically under its GenBank accession). The viewer will color
        # that tip blue when the job finishes.
        metrics["inat_matched_its_tip"] = matched_inat_its_tip
    if public_base_url:
        metrics["inat_public_base_url"] = public_base_url
    job_record = Job(
        id=job_id,
        status="queued",
        job_dir=str(Config.JOB_DIR / job_id),
        input_type=job_params["input_type"],
        metrics=metrics,
    )
    if user is not None and getattr(user, 'is_authenticated', False):
        job_record.user_id = user.id
    db.session.add(job_record)
    db.session.commit()

    message = (
        "Tree job queued. When it finishes, Dikarya will add a "
        "“Phylogenetic Tree” field on the iNaturalist "
        "observation linking to the tree."
    )
    if added_inat_its:
        message = (
            "Tree job queued. The observation's DNA Barcode ITS sequence "
            "was added to the input because it did not exactly match any "
            "MycoMap result. " + message
        )
    return {
        "status": "queued",
        "job_id": job_id,
        "observation_id": observation_id,
        "mycomap_blast_url": mycomap_url,
        "tree_status_url": f"/job/{job_id}",
        "tree_view_url": f"/job/{job_id}/view",
        "inat_added_its_sequence": bool(added_inat_its),
        "queue_class": queue_class,
        "message": message,
    }


def create_jobs_from_inat_scope(raw_input: str, resolved_type: str, user=None,
                                public_base_url: Optional[str] = None) -> Dict[str, Any]:
    """Queue one one-click tree job for each eligible observation in a scope."""
    parsed = parse_inaturalist_tree_input(raw_input)
    resolved = resolve_inaturalist_user_or_project(parsed, preferred_type=resolved_type)
    if resolved.get("type") == "ambiguous":
        raise InatTreeError("Choose whether to use the matching user or project before queueing.")
    if resolved.get("type") == "not_found":
        raise InatTreeError(resolved.get("message") or "No iNaturalist user or project was found.", status=404)
    if resolved.get("type") not in {"user", "project"}:
        raise InatTreeError("Batch tree queueing requires an iNaturalist username or project.")

    scope = resolved["scope"]
    collected = _collect_tree_eligible_observations(scope)
    observations = collected.get("observations") or []
    queued_count = len(observations)
    queue_class = "high" if queued_count == 1 else "bulk"
    queue_name = "phylo_high" if queue_class == "high" else "phylo_bulk"
    batch_id = uuid.uuid4().hex
    job_ids: List[str] = []

    for observation in observations:
        obs_id = int(observation.get("id"))
        result = create_job_from_inat_observation(
            str(obs_id),
            user=user,
            public_base_url=public_base_url,
            queue_name=queue_name,
            queue_class=queue_class,
            source="inaturalist_batch_tree",
            extra_metrics={
                "batch_id": batch_id,
                "batch_scope_type": scope["type"],
                "batch_scope_value": scope.get("value"),
            },
        )
        job_ids.append(result["job_id"])

    skipped_tree = int(collected.get("skipped_existing_tree_count") or 0)
    skipped_myco = int(collected.get("skipped_missing_mycomap_count") or 0)
    if queued_count > 1:
        message = (
            f"Queued {queued_count} bulk tree jobs. These will run in the "
            "background without blocking one-at-a-time tree jobs."
        )
    elif queued_count == 1:
        message = "Queued 1 high-priority tree job."
    else:
        message = _message_for_scope_counts(scope, collected)
    if queued_count and (skipped_tree or skipped_myco):
        message += (
            f" Skipped {skipped_tree} observations that already had trees "
            f"and {skipped_myco} without Mycomap BLAST Results."
        )

    return {
        "status": "success",
        "queued_count": queued_count,
        "skipped_existing_tree_count": skipped_tree,
        "skipped_missing_mycomap_count": skipped_myco,
        "batch_id": batch_id,
        "job_ids": job_ids,
        "queue_class": queue_class,
        "scope_type": scope["type"],
        "scope_value": scope.get("value"),
        "message": message,
    }


# ---------------------------------------------------------------------------
# Tree-viewer integration: highlight the source observation in blue
# ---------------------------------------------------------------------------

_INAT_TIP_TOKEN_RE_TMPL = r"(?<![A-Za-z0-9]){tok}(?![0-9])"
_CONTAMINANT_LABEL_RE = re.compile(r"contamin(?:a|e)nt", re.IGNORECASE)


def _iter_tree_tip_names(node):
    """Yield tip (leaf) names from a tree_state.json `tree_structure` subtree."""
    if not isinstance(node, dict):
        return
    children = node.get("children") or []
    if not children:
        name = node.get("name")
        if isinstance(name, str) and name:
            yield name
        return
    for c in children:
        yield from _iter_tree_tip_names(c)


def _find_source_observation_tip(tree_structure: Dict[str, Any], observation_id: int) -> Optional[str]:
    """Return the tip label corresponding to the source iNaturalist observation.

    MycoMap-derived FASTA labels usually encode the observation as the
    token ``iNat<id>``. We match that exact token (case-insensitive),
    with a digit-boundary check so ``iNat254525876`` doesn't match
    ``iNat2545258761``.
    """
    if not observation_id:
        return None
    tok = f"iNat{int(observation_id)}"
    pat = re.compile(_INAT_TIP_TOKEN_RE_TMPL.format(tok=re.escape(tok)), re.IGNORECASE)
    matches = [name for name in _iter_tree_tip_names(tree_structure) if pat.search(name)]
    if not matches:
        return None
    exact_token = tok.casefold()

    def score(name: str) -> Tuple[int, int]:
        first = name.split()[0].casefold() if name.split() else ""
        contaminant_penalty = 1 if _CONTAMINANT_LABEL_RE.search(name) else 0
        exact_first_token_penalty = 0 if first == exact_token else 1
        return contaminant_penalty, exact_first_token_penalty

    return sorted(matches, key=score)[0]


def highlight_source_observation_tip(job_id: str, observation_id: int,
                                       extra_tip_names: Optional[List[str]] = None,
                                       display_name: Optional[str] = None
                                       ) -> List[str]:
    """Materialize tree_state.json for ``job_id`` and add the source-observation
    tip(s) to the Default selection set (blue, ``#1f77b4``).

    Tips are picked up in two ways:
      1. By name pattern: any tip whose label contains the token
         ``iNat<observation_id>``.
      2. By exact name match against ``extra_tip_names`` (e.g. a MycoMap
         result whose sequence was an exact match for the observation's
         DNA Barcode ITS, typically labelled by its GenBank accession).

    Returns the list of tip names that were colored. Never raises.
    """
    try:
        from app.config import Config
        from app.services.tree_edit_service import load_tree_state, rename_tip, save_tree_state
        job_dir = Config.JOB_DIR / job_id
        state = load_tree_state(job_dir)
        if not state or not isinstance(state.get("tree_structure"), dict):
            return []
        all_tip_names = list(_iter_tree_tip_names(state["tree_structure"]))
        targets: List[str] = []
        source_label = _clean_display_text(display_name)

        if not source_label and observation_id:
            try:
                observation = fetch_observation(observation_id)
                source_label = _build_inat_source_display_name(observation, observation_id)
            except Exception:
                source_label = ""

        for raw in (extra_tip_names or []):
            if not raw:
                continue
            wanted = str(raw)
            if wanted in all_tip_names and wanted not in targets:
                targets.append(wanted)
                continue
            # Fall back to matching by first whitespace token (FASTA IDs
            # sometimes get normalized to the leading accession only).
            first = wanted.split()[0] if wanted.split() else ""
            if first:
                for tn in all_tip_names:
                    if tn == first or tn.split()[0] == first:
                        if tn not in targets:
                            targets.append(tn)
                        break

        pattern_tip = _find_source_observation_tip(state["tree_structure"], observation_id)
        if pattern_tip and pattern_tip not in targets:
            targets.append(pattern_tip)

        if not targets:
            return []

        if source_label:
            for tip in targets:
                display_label = _source_display_label_for_tip(tip, source_label, observation_id)
                rename_tip(state, tip, display_label)

        sel_sets = state.get("selection_sets") or {}
        if not isinstance(sel_sets, dict):
            sel_sets = {}
        default_members = list(sel_sets.get("Default") or [])
        for tip in targets:
            if tip not in default_members:
                default_members.append(tip)
        sel_sets["Default"] = default_members
        state["selection_sets"] = sel_sets
        colors = state.get("selection_set_colors") or {}
        if not isinstance(colors, dict):
            colors = {}
        colors.setdefault("Default", "#1f77b4")
        state["selection_set_colors"] = colors
        state.setdefault("active_selection_set", "Default")

        # Alan 6/2/26 - When the iNat source observation is the single focal tip, make Auto
        # root the default (anchored on that sequence of interest) rather than the generic
        # midpoint default. The shared helper only overrides the build-time default ("" or
        # "MIDPOINT") and won't persist a partial state if auto-rooting fails.
        if len(targets) == 1:
            from app.services.tree_edit_service import apply_auto_root_default
            state = apply_auto_root_default(job_dir, state, targets[0],
                                            source="inat_highlight")

        save_tree_state(job_dir, state)
        return targets
    except Exception as e:
        logger.warning("highlight_source_observation_tip failed for job %s: %s",
                       job_id, type(e).__name__)
        return []


# ---------------------------------------------------------------------------
# Worker post-completion hook
# ---------------------------------------------------------------------------

def post_completed_tree_to_inaturalist(job_id: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Write the public Dikarya tree URL into the iNaturalist observation.

    Returns a dict describing the outcome:
      {status: "success"|"failed"|"skipped",
       inat_tree_url: str | None,
       inat_observation_field_value_id: int | None,
       error: str | None}

    Never raises — the caller must not fail the tree job if the iNat write
    fails. The caller is responsible for merging the result into Job.metrics.
    """
    out: Dict[str, Any] = {
        "status": "skipped",
        "inat_tree_url": None,
        "inat_observation_field_value_id": None,
        "error": None,
    }
    try:
        observation_id = int(metrics.get("inat_observation_id") or 0)
        if not observation_id:
            out["error"] = "missing observation id"
            out["status"] = "failed"
            return out

        # Build the external tree URL. Prefer url_for(_external=True) when
        # SERVER_NAME is configured; otherwise fall back to the configured
        # INAT_PUBLIC_BASE_URL.
        tree_url: Optional[str] = None
        try:
            from flask import url_for
            tree_url = url_for("main.job_viewer", job_id=job_id, _external=True)
        except Exception:
            tree_url = None
        if not tree_url or tree_url.startswith("/"):
            base = (
                (current_app.config.get("INAT_PUBLIC_BASE_URL") or "")
                or (metrics.get("inat_public_base_url") or "")
            ).strip().rstrip("/")
            if base:
                tree_url = f"{base}/job/{job_id}/view"
            else:
                out["status"] = "skipped"
                out["error"] = "missing public base URL"
                return out
        out["inat_tree_url"] = tree_url

        result = set_observation_field_value(
            observation_id, PHYLOGENETIC_TREE_FIELD_NAME, tree_url
        )
        if isinstance(result, dict):
            ofv_id = result.get("id") or result.get("uuid")
            out["inat_observation_field_value_id"] = ofv_id
        out["status"] = "success"
        return out
    except InatTreeError as e:
        out["status"] = "failed"
        out["error"] = str(e)[:300]
        logger.warning("iNat write failed for job %s: %s", job_id, out["error"])
        return out
    except Exception as e:  # pragma: no cover - defensive
        out["status"] = "failed"
        out["error"] = f"{type(e).__name__}"[:300]
        logger.warning("iNat write unexpected error for job %s: %s", job_id, type(e).__name__)
        return out
