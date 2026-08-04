"""iNaturalist → Dikarya tree integration.

Takes a single iNaturalist observation, reads its "Mycomap BLAST Results"
field, imports those sequences via the existing MycoMap pipeline, queues a
one-click Dikarya tree job, and (later, in the worker) writes the public
tree URL back to the observation's "Phylogenetic Tree" field.

All iNaturalist writes use the site-wide authorized account configured via
the OAuth flow in app/services/inaturalist_oauth_service.py. They do NOT use
the logged-in Dikarya user's iNaturalist account.
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
    """User-facing application error with an HTTP status and safe details."""
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


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
                                  *, required_field: Optional[str] = None,
                                  per_page: int = MAX_PER_PAGE) -> Dict[str, Any]:
    params = dict(scope.get("query_params") or {})
    params.update({
        "page": int(page),
        "per_page": int(per_page),
        "order_by": "id",
        "order": "asc",
    })
    if required_field:
        params[f"field:{required_field}"] = ""
    query = urllib.parse.urlencode(params, doseq=True)
    return _http_request(f"{INAT_API_BASE}/observations?{query}")


def _scope_total_observations(scope: Dict[str, Any]) -> int:
    payload = _fetch_scope_observation_page(scope, 1, per_page=1)
    return int(payload.get("total_results") or 0)


def _collect_scope_observations_with_field(scope: Dict[str, Any],
                                           field_name: str) -> Dict[int, Dict[str, Any]]:
    """Return scope observations with one non-empty tree-input field."""
    observations: Dict[int, Dict[str, Any]] = {}
    page = 1
    total_results = 0
    while True:
        payload = _fetch_scope_observation_page(
            scope, page, required_field=field_name
        )
        results = payload.get("results") or []
        if page == 1:
            total_results = int(payload.get("total_results") or 0)
        if not results:
            break
        for observation in results:
            if not _has_nonempty_field(observation, field_name):
                continue
            try:
                observation_id = int(observation.get("id"))
            except (TypeError, ValueError):
                continue
            observations[observation_id] = observation
        if len(results) < MAX_PER_PAGE or (page * MAX_PER_PAGE) >= total_results:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)
    return observations


def _collect_tree_eligible_observations(scope: Dict[str, Any]) -> Dict[str, Any]:
    total_matching = _scope_total_observations(scope)
    with_mycomap = _collect_scope_observations_with_field(
        scope, MYCOMAP_BLAST_FIELD_NAME
    )
    with_its = _collect_scope_observations_with_field(
        scope, DNA_BARCODE_ITS_FIELD_NAME
    )
    observations_by_id = dict(with_mycomap)
    for observation_id, observation in with_its.items():
        observations_by_id.setdefault(observation_id, observation)

    observations = []
    skipped_existing_tree = 0
    auto_create_mycomap = 0
    for observation_id in sorted(observations_by_id):
        observation = observations_by_id[observation_id]
        if _has_nonempty_field(observation, PHYLOGENETIC_TREE_FIELD_NAME):
            skipped_existing_tree += 1
            continue
        observations.append(observation)
        if observation_id not in with_mycomap:
            auto_create_mycomap += 1

    skipped_missing_mycomap = max(0, total_matching - len(observations_by_id))
    return {
        "scope": scope,
        "total_matching_observations": total_matching,
        "mycomap_matching_observations": len(with_mycomap),
        "dna_barcode_its_matching_observations": len(with_its),
        "auto_create_mycomap_blast_count": auto_create_mycomap,
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
    auto_create_mycomap = int(counts.get("auto_create_mycomap_blast_count") or 0)
    if ready:
        noun = "observation is" if ready == 1 else "observations are"
        priority = " high-priority" if ready == 1 else " bulk"
        message = (
            f"Found {label}. {ready} {noun} ready for tree building. "
            f"Clicking One-Click Tree will create {ready}{priority} tree job"
            f"{'' if ready == 1 else 's'}."
        )
        if auto_create_mycomap:
            blast_noun = "observation" if auto_create_mycomap == 1 else "observations"
            message += (
                f" {auto_create_mycomap} {blast_noun} will first get a new "
                "Mycomap BLAST from DNA Barcode ITS."
            )
        if skipped_tree or skipped_myco:
            message += (
                f" Skipped {skipped_tree} observations that already had trees "
                f"and {skipped_myco} without Mycomap BLAST Results or DNA Barcode ITS."
            )
        return message
    if skipped_tree and not skipped_myco:
        return f"Found {label}, but all matching observations already have Phylogenetic Tree fields."
    if skipped_myco and not skipped_tree:
        return (
            f"Found {label}, but none of the matching observations have a "
            "Mycomap BLAST Results or DNA Barcode ITS field."
        )
    return f"Found {label}, but no matching observations are ready for tree building."


def preview_inaturalist_tree_input(raw_input: str,
                                   resolved_type: Optional[str] = None
                                   ) -> Dict[str, Any]:
    parsed = parse_inaturalist_tree_input(raw_input)
    if parsed.get("type") == "single_observation":
        observation_id = int(parsed["observation_id"])
        observation = fetch_observation(observation_id)
        has_mycomap = _has_nonempty_field(observation, MYCOMAP_BLAST_FIELD_NAME)
        has_its = _has_nonempty_field(observation, DNA_BARCODE_ITS_FIELD_NAME)
        has_tree = _has_nonempty_field(observation, PHYLOGENETIC_TREE_FIELD_NAME)
        can_recreate_tree = bool(has_tree and (has_mycomap or has_its))
        if can_recreate_tree:
            message = (
                f"Found observation {observation_id}. It already has a "
                "Phylogenetic Tree field. Select Re-create phylogenetic tree "
                "to build a new tree and replace the field's current URL."
            )
        elif has_tree:
            message = (
                f"Found observation {observation_id}. It already has a "
                "Phylogenetic Tree field, but it cannot be re-created because "
                "the observation has neither Mycomap BLAST Results nor a DNA "
                "Barcode ITS field."
            )
        elif has_mycomap:
            message = (
                f"Found observation iNat # {observation_id}. Mycomap BLAST "
                "Results loaded."
            )
        elif has_its and not has_tree:
            message = (
                f"Found observation iNat # {observation_id}. Dikarya will start "
                "a Mycomap BLAST from its DNA Barcode ITS, add the results URL "
                "to iNaturalist, and build the tree when NCBI results are ready."
            )
        else:
            message = (
                f"Found observation {observation_id}, but it has neither a "
                "Mycomap BLAST Results field nor a DNA Barcode ITS field."
            )
        eligible = bool(not has_tree and (has_mycomap or has_its))
        return {
            "status": "success",
            "type": "single_observation",
            "observation_id": observation_id,
            "has_mycomap_blast_results": has_mycomap,
            "has_dna_barcode_its": has_its,
            "will_create_mycomap_blast": bool(
                has_its and not has_mycomap
            ),
            "has_phylogenetic_tree": has_tree,
            "can_recreate_phylogenetic_tree": can_recreate_tree,
            "eligible_tree_count": 1 if eligible else 0,
            "total_matching_observations": 1,
            "skipped_existing_tree_count": 1 if has_tree else 0,
            "skipped_missing_mycomap_count": 0 if has_mycomap or has_its else 1,
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


def _refresh_mycomap_blast_results(blast_id: str, *, rebuild_ncbi_blast: bool = False,
                                   rebuild_local_blast: bool = True,
                                   mycomap_local_limit=None,
                                   mycomap_ncbi_limit=None) -> Dict[str, Any]:
    """Refresh MycoMap BLAST results before importing FASTA for a tree job.

    The automatic local refresh is best-effort so a missing API key or a
    transient MycoMap failure cannot break trees that can still use the saved
    result set. An explicitly requested NCBI refresh remains strict.
    """
    from app.services.mycomap_service import (
        MycoMapRerunError,
        rerun_mycomap_blast,
        validate_mycomap_rerun_limit,
    )

    local_limit, local_error = validate_mycomap_rerun_limit(mycomap_local_limit, "local")
    if local_error:
        raise MycoMapRerunError(local_error)
    ncbi_limit, ncbi_error = validate_mycomap_rerun_limit(mycomap_ncbi_limit, "ncbi")
    if ncbi_error:
        raise MycoMapRerunError(ncbi_error)

    result: Dict[str, Any] = {
        "local_limit": local_limit,
        "ncbi_limit": ncbi_limit,
        "local": None,
        "local_status": "skipped" if not rebuild_local_blast else "pending",
        "warnings": [],
    }
    if rebuild_local_blast:
        try:
            result["local"] = rerun_mycomap_blast(
                blast_id, result_type="local", limit=local_limit
            )
            result["local_status"] = "completed"
        except MycoMapRerunError as exc:
            warning = (
                "MycoMap local BLAST could not be refreshed; Dikarya will use "
                f"the saved MycoMap results instead. {exc}"
            )
            logger.warning("%s blast_id=%s", warning, blast_id)
            result["local_status"] = "failed"
            result["local_error"] = str(exc)
            result["warnings"].append(warning)
    if rebuild_ncbi_blast:
        result["ncbi"] = rerun_mycomap_blast(blast_id, result_type="ncbi", limit=ncbi_limit)
        result["ncbi_status"] = "queued"
    return result


DNA_BARCODE_ITS_FIELD_NAME = "DNA Barcode ITS"
GENBANK_ACCESSION_RE = re.compile(r"^[A-Z]{1,3}_?[0-9]{5,9}(?:\.[0-9]+)?$")


def _normalize_dna_for_match(text: str) -> str:
    """Uppercase + strip everything that isn't a DNA / IUPAC nucleotide letter.

    Used only for source-sequence comparison; the cleaned sequence we
    actually splice into the tree input goes through clean_dna_sequence so
    it follows the same rules as the rest of the pipeline.
    """
    if not text:
        return ""
    return re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(text).upper())


def _dna_matches_with_terminal_overhangs(first: str, second: str) -> bool:
    """Return true when normalized sequences differ only by terminal overhangs."""
    first_norm = _normalize_dna_for_match(first)
    second_norm = _normalize_dna_for_match(second)
    if not first_norm or not second_norm:
        return False
    return first_norm in second_norm or second_norm in first_norm


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
        sequence matches the ITS field, allowing terminal overhangs, or None.
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

    source_sequence_matches = []
    for idx, s in enumerate(sequences):
        name = str(s.get("name") or "").strip()
        if not name or _CONTAMINANT_LABEL_RE.search(name):
            continue
        if not _name_has_observation_id(name, observation_id):
            continue
        if _dna_matches_with_terminal_overhangs(s.get("sequence") or "", its_norm):
            source_sequence_matches.append((idx, s))

    if source_sequence_matches:
        def source_match_score(item: Tuple[int, Dict[str, Any]]) -> Tuple[int, int, int]:
            idx, seq = item
            name = str(seq.get("name") or "")
            accession_penalty = 0 if _extract_genbank_accession(name) else 1
            synthetic_penalty = 1 if name.startswith(f"iNat{int(observation_id)}") else 0
            return accession_penalty, synthetic_penalty, idx

        keep_idx, keep_seq = sorted(source_sequence_matches, key=source_match_score)[0]
        keep_name = _source_tip_label_with_inat(keep_seq.get("name") or "", observation_id)
        keep_seq["name"] = keep_name
        remove_indexes = {idx for idx, _seq in source_sequence_matches if idx != keep_idx}
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


INAT_SPECIES_NAME_FIELD_NAMES = (
    "Species Name Override",
    "Provisional Species Name",
    "Tagged NZ Fungal Species",
    "Barcode Inferred Species or Name",
)


def _extract_inat_species_field_label(observation: Dict[str, Any]) -> str:
    """Return the most specific saved species-of-interest field value."""
    for field_name in INAT_SPECIES_NAME_FIELD_NAMES:
        value = extract_observation_field_value(observation, field_name)
        if value:
            cleaned = _clean_display_text(value)
            if cleaned:
                return cleaned
    return ""


def _extract_inat_species_label(observation: Dict[str, Any]) -> str:
    """Return the best scientific-name-like label for an iNat observation."""
    field_label = _extract_inat_species_field_label(observation)
    if field_label:
        return field_label

    taxon = observation.get("taxon") or {}
    if isinstance(taxon, dict):
        for key in ("name", "preferred_common_name"):
            cleaned = _clean_display_text(taxon.get(key))
            if cleaned:
                return cleaned
    return ""


def _extract_genus_from_scientific_label(value: Any) -> str:
    """Extract a leading fungal genus from a scientific-name-like label."""
    cleaned = _clean_display_text(value)
    if not cleaned:
        return ""
    parts = cleaned.split()
    if parts and parts[0] == "×":
        parts = parts[1:]
    if not parts:
        return ""
    candidate = parts[0].strip("()[]{}.,;:")
    if re.fullmatch(r"[A-Z][A-Za-z-]+", candidate):
        return candidate
    return ""


def _extract_inat_genus(observation: Dict[str, Any]) -> str:
    """Return the observation's genus without mistaking a higher rank for one."""
    field_label = _extract_inat_species_field_label(observation)
    if field_label:
        genus = _extract_genus_from_scientific_label(field_label)
        if genus:
            return genus

    taxon = observation.get("taxon") or {}
    if not isinstance(taxon, dict):
        return ""

    taxon_name = _clean_display_text(taxon.get("name"))
    taxon_rank = _clean_display_text(taxon.get("rank")).casefold()
    if taxon_rank in {"genus", "subgenus"}:
        genus = _extract_genus_from_scientific_label(taxon_name)
        if genus:
            return genus
    if " " in taxon_name:
        genus = _extract_genus_from_scientific_label(taxon_name)
        if genus:
            return genus

    for ancestor in reversed(taxon.get("ancestors") or []):
        if not isinstance(ancestor, dict):
            continue
        if _clean_display_text(ancestor.get("rank")).casefold() != "genus":
            continue
        genus = _extract_genus_from_scientific_label(ancestor.get("name"))
        if genus:
            return genus
    return ""


def _extract_genus_from_inat_tip(value: Any, observation_id: int) -> str:
    """Extract the first scientific token following an exact iNat ID token."""
    cleaned = _clean_display_text(value)
    if not cleaned:
        return ""
    match = re.search(
        rf"(?<![A-Za-z0-9])iNat{int(observation_id)}(?![0-9])\s+([^\s]+)",
        cleaned,
        re.IGNORECASE,
    )
    return _extract_genus_from_scientific_label(match.group(1)) if match else ""


def _resolve_inat_genus(observation: Dict[str, Any], observation_id: int,
                        source_tip_name: Optional[str] = None) -> str:
    """Resolve a genus from fields, current taxonomy, ancestry, or source tip."""
    genus = _extract_inat_genus(observation)
    taxon = observation.get("taxon") or {}
    taxon_id = taxon.get("id") if isinstance(taxon, dict) else None
    if not genus and taxon_id:
        try:
            payload = _http_request(f"{INAT_API_BASE}/taxa/{int(taxon_id)}")
            results = (payload or {}).get("results") or []
            if results:
                genus = _extract_inat_genus({"taxon": results[0]})
        except Exception as exc:
            logger.warning(
                "Could not resolve genus ancestry for iNaturalist observation %s: %s",
                observation_id,
                exc,
            )
    if not genus and source_tip_name:
        genus = _extract_genus_from_inat_tip(source_tip_name, observation_id)
    return genus


def _build_inat_job_title(observation_id: int, genus: Optional[str] = None) -> str:
    """Build the user-facing job title for one iNaturalist tree."""
    genus_label = _extract_genus_from_scientific_label(genus) or "Genus pending"
    return f"iNat # {int(observation_id)} - {genus_label} → Phylogenetic Tree"


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


def _build_sequence_metadata(sequences: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Preserve MycoMap header provenance separately from rendered tip labels."""
    metadata = []
    for sequence in sequences:
        name = _clean_display_text(sequence.get('name'))
        if not name:
            continue
        metadata.append({
            'name': name,
            'fasta_header': name,
            'organism': _clean_display_text(sequence.get('organism')),
            'source': _clean_display_text(sequence.get('source')),
            'hit_source': _clean_display_text(sequence.get('hit_source')),
            'location': _clean_display_text(sequence.get('location')),
            'raw_fasta_header': str(sequence.get('raw_fasta_header') or ''),
            'raw_ncbi_description': str(sequence.get('raw_ncbi_description') or ''),
            'mycomap_header_format': _clean_display_text(
                sequence.get('mycomap_header_format')
            ),
            'internal_id': _clean_display_text(sequence.get('internal_id')),
            'display_label': _clean_display_text(sequence.get('display_label')),
            'accession': _clean_display_text(sequence.get('accession')),
            'taxon': _clean_display_text(sequence.get('taxon')),
            'raw_mycomap_taxon': str(sequence.get('raw_mycomap_taxon') or ''),
            'voucher': _clean_display_text(sequence.get('voucher')),
            'occurrence': sequence.get('occurrence'),
            'identity': sequence.get('identity'),
            'query_cover': sequence.get('query_cover'),
            'subject_cover': sequence.get('subject_cover'),
            'blast_metrics_available': bool(sequence.get('blast_metrics_available')),
        })
    return metadata


def _build_inat_tree_job_params(observation_id: int, mycomap_url: str,
                                payload: Dict[str, Any],
                                sequences: List[Dict[str, Any]],
                                genus: Optional[str] = None) -> Dict[str, Any]:
    """Build the normal phylogeny-worker payload from imported MycoMap data."""
    return {
        "input_type": "pasted_sequence",
        "notes": _build_inat_job_title(observation_id, genus),
        "sequence": _build_fasta_text(sequences),
        "sequence_metadata": _build_sequence_metadata(sequences),
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


def _build_inat_tree_metrics(observation_id: int, *, queue_class: str,
                             source: str, public_base_url: Optional[str],
                             rebuild_ncbi_blast: bool,
                             recreate_existing_tree: bool = False,
                             genus: Optional[str] = None,
                             extra_metrics: Optional[Dict[str, Any]] = None
                             ) -> Dict[str, Any]:
    """Build the metrics stored before the iNaturalist input is imported."""
    metrics = {
        "via": "inat_phylogenetic_tree",
        "tree_method": DEFAULT_TREE_PARAMS["tree_method"],
        "alignment_method": DEFAULT_TREE_PARAMS["alignment_method"],
        "trimming_method": DEFAULT_TREE_PARAMS["trimming_method"],
        "trim_terminal_overhangs": DEFAULT_TREE_PARAMS["trim_terminal_overhangs"],
        "notes": _build_inat_job_title(observation_id, genus),
        "inat_observation_id": observation_id,
        "inat_source_url": f"https://www.inaturalist.org/observations/{observation_id}",
        "inat_update_status": "pending",
        "inat_observation_field": PHYLOGENETIC_TREE_FIELD_NAME,
        "inat_replace_existing_tree": bool(recreate_existing_tree),
        "queue_class": queue_class,
        "source": source,
    }
    if rebuild_ncbi_blast:
        metrics["mycomap_ncbi_blast_rebuild_requested"] = True
        metrics["mycomap_preparation_status"] = "queued"
    if extra_metrics:
        metrics.update(extra_metrics)
    public_base_url = _clean_display_text(public_base_url).rstrip("/")
    if public_base_url:
        metrics["inat_public_base_url"] = public_base_url
    return metrics


def _create_mycomap_blast_from_observation(observation: Dict[str, Any],
                                           observation_id: int, *,
                                           mycomap_local_limit=None,
                                           mycomap_ncbi_limit=None,
                                           pending_creation_details=None
                                           ) -> Dict[str, Any]:
    """Create a MycoMap search from an observation's ITS and write its URL back."""
    from app.services.fasta_utils import clean_dna_sequence
    from app.services.mycomap_service import (
        MycoMapCreateError,
        create_mycomap_blast,
        find_mycomap_blast_by_title,
        get_mycomap_ncbi_poll_max_attempts,
        validate_mycomap_rerun_limit,
    )

    raw_its = extract_observation_field_value(observation, DNA_BARCODE_ITS_FIELD_NAME)
    cleaned_its = clean_dna_sequence(raw_its or "") or ""
    if not cleaned_its:
        raise InatTreeError(
            "This observation has no usable DNA Barcode ITS sequence and no "
            "Mycomap BLAST Results URL.",
            status=422,
        )
    job_title = f"iNat{int(observation_id)} DNA Barcode ITS"
    created = find_mycomap_blast_by_title(job_title)
    if created:
        local_limit, local_error = validate_mycomap_rerun_limit(
            mycomap_local_limit, "local"
        )
        ncbi_limit, ncbi_error = validate_mycomap_rerun_limit(
            mycomap_ncbi_limit, "ncbi"
        )
        if local_error or ncbi_error:
            raise InatTreeError(local_error or ncbi_error, status=422)
        created.update({
            "local_limit": local_limit,
            "ncbi_limit": ncbi_limit,
            "reused_existing": True,
        })
    elif (pending_creation_details or {}).get("creation_pending"):
        details = dict(pending_creation_details)
        attempt = int(details.get("creation_discovery_attempt") or 0) + 1
        max_attempts = get_mycomap_ncbi_poll_max_attempts()
        if attempt >= max_attempts:
            raise InatTreeError(
                "MycoMap accepted the BLAST request, but its result could not "
                f"be discovered within {max_attempts} permitted attempts.",
                status=504,
            )
        details["creation_discovery_attempt"] = attempt
        return details
    else:
        try:
            created = create_mycomap_blast(
                cleaned_its,
                title=job_title,
                local_limit=mycomap_local_limit,
                ncbi_limit=mycomap_ncbi_limit,
            )
        except MycoMapCreateError as exc:
            raise InatTreeError(str(exc), status=502)

    if created.get("record_pending"):
        return {
            "local_limit": created.get("local_limit"),
            "ncbi_limit": created.get("ncbi_limit"),
            "local": created,
            "local_status": "queued",
            "ncbi": created,
            "ncbi_status": "queued",
            "warnings": [],
            "auto_created": True,
            "creation_pending": True,
            "creation_discovery_attempt": 0,
            "created_title": job_title,
            "created_blast_id": None,
            "created_mycomap_url": "",
            "inat_mycomap_field_status": "pending",
            "inat_mycomap_field_value_id": None,
            "ncbi_poll_attempt": 0,
        }

    mycomap_url = str(created.get("url") or "").strip()
    blast_id = str(created.get("blast_id") or "").strip()
    if not mycomap_url or not blast_id:
        raise InatTreeError(
            "MycoMap did not return a usable BLAST Results URL.", status=502
        )
    set_result = set_observation_field_value(
        observation_id, MYCOMAP_BLAST_FIELD_NAME, mycomap_url
    )
    field_value_id = None
    if isinstance(set_result, dict):
        field_value_id = set_result.get("id") or set_result.get("uuid")
    return {
        "local_limit": created.get("local_limit"),
        "ncbi_limit": created.get("ncbi_limit"),
        "local": created,
        "local_status": "queued",
        "ncbi": created,
        "ncbi_status": "queued",
        "warnings": [],
        "auto_created": True,
        "created_blast_id": blast_id,
        "created_mycomap_url": mycomap_url,
        "inat_mycomap_field_status": "success",
        "inat_mycomap_field_value_id": field_value_id,
        "ncbi_poll_attempt": 0,
    }


def _check_auto_created_mycomap_ncbi_results(
        blast_id: str, details: Dict[str, Any],
        mycomap_url: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    """Check one polling interval for NCBI hits on an auto-created BLAST."""
    from app.services.mycomap_service import (
        get_mycomap_ncbi_local_fallback_seconds,
        get_mycomap_ncbi_poll_interval_seconds,
        get_mycomap_ncbi_poll_max_attempts,
        get_mycomap_ncbi_queue_position,
        get_mycomap_ncbi_result_count,
    )

    details = dict(details or {})
    attempt = int(details.get("ncbi_poll_attempt") or 0) + 1
    count, warnings = get_mycomap_ncbi_result_count(blast_id)
    details["ncbi_poll_attempt"] = attempt
    details["ncbi_result_count"] = count
    details["ncbi_status"] = "available" if count > 0 else "waiting"
    if warnings:
        details["ncbi_poll_warnings"] = warnings
    if count > 0:
        details.pop("ncbi_queue_position", None)
        return True, details
    queue_position = (
        get_mycomap_ncbi_queue_position(mycomap_url) if mycomap_url else None
    )
    if queue_position is not None:
        details["ncbi_queue_position"] = queue_position
    else:
        details.pop("ncbi_queue_position", None)

    elapsed_seconds = attempt * get_mycomap_ncbi_poll_interval_seconds()
    fallback_seconds = get_mycomap_ncbi_local_fallback_seconds()
    if elapsed_seconds >= fallback_seconds:
        details["ncbi_status"] = "timed_out_local_fallback"
        details["ncbi_fallback_local_only"] = True
        fallback_minutes = max(1, round(fallback_seconds / 60))
        details["warnings"] = list(details.get("warnings") or []) + [
            "MycoMap NCBI BLAST results were not ready within "
            f"{fallback_minutes} minute{'s' if fallback_minutes != 1 else ''}; "
            "building the tree from local (MycoBLAST) results only."
        ]
        return True, details

    max_attempts = get_mycomap_ncbi_poll_max_attempts()
    if attempt >= max_attempts:
        raise InatTreeError(
            "MycoMap NCBI BLAST results were not available after "
            f"{max_attempts} one-minute checks.",
            status=504,
        )
    return False, details


def _append_fasta_to_job_input(job_dir, fasta_text: str) -> int:
    """
    Append newly-available sequences to a job's original input FASTA,
    de-duplicating against what's already there. Reuses the same
    parsing/formatting helpers as the "add sequences to an existing job"
    API endpoint. Returns the number of sequences actually appended.
    """
    from app.api.routes import (
        _format_fasta_record_for_job,
        _parse_fasta_sequences,
        _sequence_exact_key,
        _split_fasta_header,
    )

    sequences_to_add = [
        s for s in _parse_fasta_sequences(fasta_text) if s.get("sequence", "").strip()
    ]
    if not sequences_to_add:
        return 0

    input_path = job_dir / "input" / "input_raw.fasta"
    input_path.parent.mkdir(parents=True, exist_ok=True)

    existing_ids = set()
    existing_records = set()
    if input_path.exists():
        for s in _parse_fasta_sequences(input_path.read_text()):
            seq_id, _ = _split_fasta_header(s["name"])
            if seq_id:
                existing_ids.add(seq_id)
            existing_records.add(_sequence_exact_key(s))

    added_count = 0
    with open(input_path, "a") as f:
        if input_path.stat().st_size > 0:
            f.write("\n")
        for seq in sequences_to_add:
            exact_key = _sequence_exact_key(seq)
            if exact_key in existing_records:
                continue
            f.write(_format_fasta_record_for_job(seq, existing_ids, added_count + 1))
            existing_records.add(exact_key)
            added_count += 1
    return added_count


def _schedule_ncbi_recheck(job_id: str, *, hours: int = 1) -> None:
    """Schedule one delayed re-check of MycoMap NCBI results for a job."""
    from datetime import timedelta
    from app.workers.queue import get_queue

    q = get_queue("phylo_bulk")
    q.enqueue_in(
        timedelta(hours=hours),
        reconcile_delayed_ncbi_results,
        job_id,
        job_timeout="10m",
    )


def schedule_initial_ncbi_recheck(job_id: str) -> None:
    """
    Called once, right after a job completes having built its tree from
    local-only results (NCBI results weren't ready in time). Kicks off the
    hourly background re-check that appends NCBI hits and rebuilds the tree
    once MycoMap's NCBI BLAST search finally finishes.
    """
    _schedule_ncbi_recheck(job_id, hours=1)


def reconcile_delayed_ncbi_results(job_id: str) -> Dict[str, Any]:
    """
    Background (RQ-scheduled) re-check for a job that fell back to local-only
    MycoMap results. If NCBI results have since become available, appends
    them to the job's input and triggers a full recompute (which preserves
    prunes/renames/rooting). Otherwise reschedules itself hourly, up to
    ``get_mycomap_ncbi_recheck_max_hours()`` attempts.

    This is intentionally generic over how the job was created (iNat or
    Mushroom Observer both write the same ``mycomap_blast_rerun`` /
    ``mycomap_blast_url`` metrics shape), so it can be reused for any future
    "rebuild this job once more data exists" need.
    """
    from app import create_app
    from app.extensions import db
    from app.models import Job
    from app.config import Config
    from app.services.mycomap_service import (
        fetch_mycomap_fasta,
        get_mycomap_ncbi_recheck_max_hours,
        get_mycomap_ncbi_result_count,
        validate_mycomap_url,
    )

    _app = create_app()
    with _app.app_context():
        db_job = Job.query.get(job_id)
        if not db_job:
            return {"status": "job_not_found"}

        metrics = dict(db_job.metrics or {})
        rerun_details = dict(metrics.get("mycomap_blast_rerun") or {})
        if not rerun_details.get("ncbi_fallback_local_only"):
            # Already reconciled (or never fell back) -- nothing to do.
            return {"status": "noop"}

        mycomap_url = str(metrics.get("mycomap_blast_url") or "").strip()
        blast_id = validate_mycomap_url(mycomap_url)
        if not blast_id:
            return {"status": "invalid_url"}

        recheck_count = int(rerun_details.get("ncbi_recheck_count") or 0) + 1
        rerun_details["ncbi_recheck_count"] = recheck_count
        rerun_details["ncbi_last_rechecked_at"] = datetime.now(timezone.utc).isoformat()

        count, _warnings = get_mycomap_ncbi_result_count(blast_id)
        if count <= 0:
            max_hours = get_mycomap_ncbi_recheck_max_hours()
            if recheck_count >= max_hours:
                rerun_details["ncbi_status"] = "gave_up"
                rerun_details["ncbi_fallback_local_only"] = False
                metrics["mycomap_blast_rerun"] = rerun_details
                metrics["mycomap_refresh_warnings"] = list(
                    metrics.get("mycomap_refresh_warnings") or []
                ) + [
                    "Gave up waiting for MycoMap NCBI BLAST results after "
                    f"{max_hours} hourly re-checks; tree remains local-only."
                ]
                db_job.metrics = metrics
                db.session.commit()
                return {"status": "gave_up"}

            metrics["mycomap_blast_rerun"] = rerun_details
            db_job.metrics = metrics
            db.session.commit()
            _schedule_ncbi_recheck(job_id, hours=1)
            return {"status": "still_waiting", "recheck_count": recheck_count}

        # NCBI results are in -- fetch and append them, then rebuild.
        fetch_result = fetch_mycomap_fasta(blast_id, include_ncbi=True, include_local=False)
        fasta_text = fetch_result.get("fasta_content") or ""
        added_count = 0
        if fasta_text.strip():
            added_count = _append_fasta_to_job_input(Config.JOB_DIR / job_id, fasta_text)

        rerun_details["ncbi_status"] = "available"
        rerun_details["ncbi_fallback_local_only"] = False
        rerun_details["ncbi_appended_at"] = datetime.now(timezone.utc).isoformat()
        rerun_details["ncbi_appended_count"] = added_count
        metrics["mycomap_blast_rerun"] = rerun_details
        db_job.metrics = metrics
        db.session.commit()

        if added_count <= 0:
            return {"status": "no_new_sequences"}

        input_info_path = (Config.JOB_DIR / job_id) / "input_info.json"
        params_dict = {}
        if input_info_path.exists():
            with open(input_info_path, "r") as f:
                params_dict = json.load(f)
        params_dict["use_current_input"] = True

        from app.workers.queue import enqueue_recompute_job

        enqueue_recompute_job(job_id, params_dict)
        return {"status": "rebuilt", "added_count": added_count}


def prepare_inat_tree_job(observation_id: int, *, include_ncbi: bool = True,
                          include_local: bool = True,
                          rebuild_ncbi_blast: bool = False,
                          recreate_existing_tree: bool = False,
                          mycomap_local_limit=None,
                          mycomap_ncbi_limit=None,
                          defer_after_ncbi_rerun: bool = False,
                          skip_mycomap_refresh: bool = False,
                          mycomap_rerun_details: Optional[Dict[str, Any]] = None
                          ) -> Dict[str, Any]:
    """Fetch, refresh, and import an iNaturalist observation's MycoMap input.

    This runs in an RQ worker. NCBI refresh callers may request a staged return
    immediately after MycoMap accepts the rerun, then call again after RQ's
    delayed retry with ``skip_mycomap_refresh`` enabled.
    """
    from app.api.routes import gather_mycomap_sequences_for_queue
    from app.services.mycomap_service import (
        MycoMapRerunError,
        validate_mycomap_url,
    )

    observation = fetch_observation(observation_id)
    genus = _resolve_inat_genus(observation, observation_id)
    existing_tree_record = find_observation_field_value_record(
        observation, PHYLOGENETIC_TREE_FIELD_NAME
    )
    if (
        existing_tree_record
        and str(existing_tree_record.get("value") or "").strip()
        and not recreate_existing_tree
    ):
        raise InatTreeError(
            "This observation already has a Phylogenetic Tree field. Select "
            "Re-create phylogenetic tree to replace its URL with a new tree.",
            status=409,
        )
    mycomap_url = extract_observation_field_value(observation, MYCOMAP_BLAST_FIELD_NAME)
    if not mycomap_url:
        saved_created_url = str(
            (mycomap_rerun_details or {}).get("created_mycomap_url") or ""
        ).strip()
        if skip_mycomap_refresh and saved_created_url:
            mycomap_url = saved_created_url
        else:
            mycomap_rerun_details = _create_mycomap_blast_from_observation(
                observation,
                observation_id,
                mycomap_local_limit=mycomap_local_limit,
                mycomap_ncbi_limit=mycomap_ncbi_limit,
                pending_creation_details=mycomap_rerun_details,
            )
            return {
                "status": "waiting_for_ncbi",
                "notes": _build_inat_job_title(observation_id, genus),
                "inat_genus": genus,
                "mycomap_blast_url": mycomap_rerun_details["created_mycomap_url"],
                "mycomap_rerun_details": mycomap_rerun_details,
            }

    mycomap_url = mycomap_url.strip()
    blast_id = validate_mycomap_url(mycomap_url)
    if not blast_id:
        raise InatTreeError(
            "The observation's Mycomap BLAST Results field does not "
            "contain a valid MycoMap BLAST URL. Edit that iNaturalist field "
            "to contain the complete MycoMap result-page URL ending in an "
            "r-number, then retry the tree job.",
            status=422,
        )

    if skip_mycomap_refresh:
        mycomap_rerun_details = dict(mycomap_rerun_details or {})
    else:
        try:
            mycomap_rerun_details = _refresh_mycomap_blast_results(
                blast_id,
                rebuild_local_blast=bool(include_local),
                rebuild_ncbi_blast=bool(rebuild_ncbi_blast),
                mycomap_local_limit=mycomap_local_limit,
                mycomap_ncbi_limit=mycomap_ncbi_limit,
            )
        except MycoMapRerunError as e:
            raise InatTreeError(str(e), status=502)

    if rebuild_ncbi_blast and defer_after_ncbi_rerun and not skip_mycomap_refresh:
        return {
            "status": "waiting_for_ncbi",
            "notes": _build_inat_job_title(observation_id, genus),
            "inat_genus": genus,
            "mycomap_blast_url": mycomap_url,
            "mycomap_rerun_details": mycomap_rerun_details,
        }

    if mycomap_rerun_details.get("auto_created"):
        ncbi_ready, mycomap_rerun_details = _check_auto_created_mycomap_ncbi_results(
            blast_id, mycomap_rerun_details, mycomap_url=mycomap_url
        )
        if not ncbi_ready:
            return {
                "status": "waiting_for_ncbi",
                "notes": _build_inat_job_title(observation_id, genus),
                "inat_genus": genus,
                "mycomap_blast_url": mycomap_url,
                "mycomap_rerun_details": mycomap_rerun_details,
            }
        if mycomap_rerun_details.get("ncbi_fallback_local_only"):
            include_ncbi = False

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

    # Ensure an observation's own ITS barcode is represented in the tree.
    added_inat_its, matched_inat_its_tip = _maybe_add_inat_its_sequence(
        observation, observation_id, sequences
    )
    if not genus:
        genus = _resolve_inat_genus(
            observation,
            observation_id,
            source_tip_name=matched_inat_its_tip,
        )
    job_params = _build_inat_tree_job_params(
        observation_id, mycomap_url, payload or {}, sequences, genus=genus
    )
    metrics = {
        "notes": _build_inat_job_title(observation_id, genus),
        "inat_genus": genus,
        "inat_source_display_name": _build_inat_source_display_name(observation, observation_id),
        "mycomap_blast_url": mycomap_url,
        "inat_added_its_sequence": bool(added_inat_its),
        "mycomap_blast_rerun": mycomap_rerun_details,
        "mycomap_local_blast_rebuilt": (
            mycomap_rerun_details.get("local_status") == "completed"
        ),
        "mycomap_ncbi_blast_rebuilt": bool(
            (rebuild_ncbi_blast and mycomap_rerun_details.get("ncbi"))
            or (
                mycomap_rerun_details.get("auto_created")
                and mycomap_rerun_details.get("ncbi_status") == "available"
            )
        ),
        "mycomap_local_blast_limit": mycomap_rerun_details.get("local_limit"),
        "mycomap_ncbi_blast_limit": mycomap_rerun_details.get("ncbi_limit"),
        "mycomap_preparation_status": "completed",
        "mycomap_blast_auto_created": bool(
            mycomap_rerun_details.get("auto_created")
        ),
    }
    refresh_warnings = list(mycomap_rerun_details.get("warnings") or [])
    if refresh_warnings:
        metrics["mycomap_refresh_warnings"] = refresh_warnings
    if added_inat_its:
        metrics["inat_added_its_name"] = added_inat_its
    if matched_inat_its_tip:
        metrics["inat_matched_its_tip"] = matched_inat_its_tip
    return {
        "job_params": job_params,
        "metrics": metrics,
        "mycomap_blast_url": mycomap_url,
        "inat_added_its_sequence": bool(added_inat_its),
    }


def create_job_from_inat_observation(raw_input: str, user=None,
                                      include_ncbi: bool = True,
                                      include_local: bool = True,
                                      rebuild_ncbi_blast: bool = False,
                                      recreate_existing_tree: bool = False,
                                      mycomap_local_limit=None,
                                      mycomap_ncbi_limit=None,
                                      public_base_url: Optional[str] = None,
                                      queue_name: str = "phylo_high",
                                      queue_class: str = "high",
                                      source: str = "inaturalist_single_tree",
                                      extra_metrics: Optional[Dict[str, Any]] = None
                                      ) -> Dict[str, Any]:
    """Validate the iNat input and queue preparation for a one-click tree job.

    Returns a dict with: status, job_id, observation_id, mycomap_blast_url,
    tree_view_url, tree_status_url, message.
    """
    from app.config import Config
    from app.extensions import db
    from app.models import Job
    from app.workers.queue import enqueue_job

    observation_id = parse_single_observation_input(raw_input)
    initial_genus = _clean_display_text((extra_metrics or {}).get("inat_genus"))
    rq_meta = {
        "queue_class": queue_class,
        "source": source,
    }
    if extra_metrics:
        rq_meta.update(extra_metrics)

    job_id = str(uuid.uuid4())
    job_params = {
        "input_type": "inat_tree_preparation",
        "notes": _build_inat_job_title(observation_id, initial_genus),
        "trim_terminal_overhangs": DEFAULT_TREE_PARAMS["trim_terminal_overhangs"],
        "_inat_tree_preparation": {
            "observation_id": observation_id,
            "include_ncbi": bool(include_ncbi),
            "include_local": bool(include_local),
            "rebuild_ncbi_blast": bool(rebuild_ncbi_blast),
            "recreate_existing_tree": bool(recreate_existing_tree),
            "mycomap_local_limit": mycomap_local_limit,
            "mycomap_ncbi_limit": mycomap_ncbi_limit,
        },
    }
    metrics = _build_inat_tree_metrics(
        observation_id,
        queue_class=queue_class,
        source=source,
        public_base_url=public_base_url,
        rebuild_ncbi_blast=bool(rebuild_ncbi_blast),
        recreate_existing_tree=bool(recreate_existing_tree),
        genus=initial_genus,
        extra_metrics=extra_metrics,
    )
    metrics["mycomap_preparation_status"] = "queued"
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
    try:
        rq_meta["inat_tree_preparation"] = "queued"
        enqueue_job(
            job_params,
            queue_name=queue_name,
            meta=rq_meta,
            job_id=job_id,
            job_timeout=7200,
        )
    except Exception:
        metrics = dict(job_record.metrics or {})
        metrics["mycomap_preparation_status"] = "failed"
        metrics["error"] = "Unable to queue iNaturalist tree preparation."
        job_record.metrics = metrics
        job_record.status = "failed"
        db.session.commit()
        raise

    if recreate_existing_tree:
        message = (
            "Tree re-creation queued. When the new tree finishes, Dikarya "
            "will replace the existing “Phylogenetic Tree” field URL on the "
            "iNaturalist observation."
        )
    elif rebuild_ncbi_blast:
        message = (
            "Tree job queued. Dikarya will refresh MycoMap local and NCBI "
            "BLAST results in the background, wait for the NCBI results "
            "without blocking other tree jobs, then build the tree. When it "
            "finishes, Dikarya will add a “Phylogenetic Tree” field to the "
            "iNaturalist observation."
        )
    else:
        message = (
            "Tree job queued. If the observation has saved Mycomap BLAST "
            "Results, Dikarya will refresh and use them. If it only has a DNA "
            "Barcode ITS, Dikarya will create the Mycomap BLAST, add its URL "
            "to iNaturalist, check once a minute for NCBI results, and then "
            "build the tree. When it finishes, Dikarya will add a “Phylogenetic "
            "Tree” field to the observation."
        )
    return {
        "status": "queued",
        "job_id": job_id,
        "observation_id": observation_id,
        "mycomap_blast_url": None,
        "tree_status_url": f"/job/{job_id}",
        "tree_view_url": f"/job/{job_id}/view",
        "inat_added_its_sequence": None,
        "mycomap_local_blast_rebuilt": False,
        "mycomap_ncbi_blast_rebuilt": False,
        "mycomap_ncbi_blast_rebuild_requested": bool(rebuild_ncbi_blast),
        "recreate_existing_tree": bool(recreate_existing_tree),
        "queue_class": queue_class,
        "message": message,
    }


def create_jobs_from_inat_scope(raw_input: str, resolved_type: str, user=None,
                                public_base_url: Optional[str] = None,
                                mycomap_local_limit=None,
                                mycomap_ncbi_limit=None) -> Dict[str, Any]:
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
    eligible_count = len(observations)
    queue_class = "high" if eligible_count == 1 else "bulk"
    queue_name = "phylo_high" if queue_class == "high" else "phylo_bulk"
    batch_id = uuid.uuid4().hex
    job_ids: List[str] = []
    failed_observations: List[Dict[str, Any]] = []

    for observation in observations:
        obs_id = int(observation.get("id"))
        try:
            result = create_job_from_inat_observation(
                str(obs_id),
                user=user,
                public_base_url=public_base_url,
                queue_name=queue_name,
                queue_class=queue_class,
                source="inaturalist_batch_tree",
                mycomap_local_limit=mycomap_local_limit,
                mycomap_ncbi_limit=mycomap_ncbi_limit,
                extra_metrics={
                    "batch_id": batch_id,
                    "batch_scope_type": scope["type"],
                    "batch_scope_value": scope.get("value"),
                    "inat_genus": _extract_inat_genus(observation),
                },
            )
            job_ids.append(result["job_id"])
        except InatTreeError as exc:
            logger.warning(
                "Could not queue iNaturalist batch observation %s: %s", obs_id, exc
            )
            failed_observations.append({
                "observation_id": obs_id,
                "error": str(exc),
            })
        except Exception:
            from app.extensions import db

            db.session.rollback()
            logger.exception(
                "Unexpected failure queueing iNaturalist batch observation %s", obs_id
            )
            failed_observations.append({
                "observation_id": obs_id,
                "error": "Unable to queue this observation's tree job.",
            })

    queued_count = len(job_ids)
    failed_count = len(failed_observations)

    skipped_tree = int(collected.get("skipped_existing_tree_count") or 0)
    skipped_myco = int(collected.get("skipped_missing_mycomap_count") or 0)
    auto_create_mycomap = int(
        collected.get("auto_create_mycomap_blast_count") or 0
    )
    if eligible_count and queued_count == 0 and failed_count == eligible_count:
        raise InatTreeError(
            "No tree jobs could be queued. Please try again.",
            status=503,
            details={
                "partial": False,
                "eligible_count": eligible_count,
                "queued_count": 0,
                "failed_count": failed_count,
                "failed_observations": failed_observations,
                "skipped_existing_tree_count": skipped_tree,
                "skipped_missing_mycomap_count": skipped_myco,
                "batch_id": batch_id,
                "job_ids": [],
                "queue_class": queue_class,
                "scope_type": scope["type"],
                "scope_value": scope.get("value"),
            },
        )
    if queued_count > 1:
        message = (
            f"Queued {queued_count} bulk tree jobs. These will run in the "
            "background without blocking one-at-a-time tree jobs."
        )
    elif queued_count == 1:
        priority_label = "high-priority" if queue_class == "high" else "bulk"
        message = f"Queued 1 {priority_label} tree job."
    elif failed_count:
        message = "No eligible iNaturalist tree jobs could be queued."
    else:
        message = _message_for_scope_counts(scope, collected)
    if queued_count and (skipped_tree or skipped_myco):
        message += (
            f" Skipped {skipped_tree} observations that already had trees "
            f"and {skipped_myco} without Mycomap BLAST Results or DNA Barcode ITS."
        )
    if queued_count and auto_create_mycomap:
        blast_noun = "job" if auto_create_mycomap == 1 else "jobs"
        message += (
            f" {auto_create_mycomap} {blast_noun} will create Mycomap BLAST "
            "Results from DNA Barcode ITS and add the URL to iNaturalist "
            "before building the tree."
        )
    if failed_count:
        message += (
            f" {failed_count} eligible observation"
            f"{'s' if failed_count != 1 else ''} could not be queued."
        )
        if queued_count:
            message += " The other queued jobs are unaffected."

    return {
        "status": "success",
        "partial": bool(failed_count),
        "eligible_count": eligible_count,
        "queued_count": queued_count,
        "failed_count": failed_count,
        "failed_observations": failed_observations,
        "skipped_existing_tree_count": skipped_tree,
        "skipped_missing_mycomap_count": skipped_myco,
        "auto_create_mycomap_blast_count": auto_create_mycomap,
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

    Never raises. The caller must not fail the tree job if the iNat write
    fails and is responsible for merging the result into Job.metrics.
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
