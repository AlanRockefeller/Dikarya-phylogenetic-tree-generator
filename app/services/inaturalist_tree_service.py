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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

logger = logging.getLogger(__name__)

INAT_API_BASE = "https://api.inaturalist.org/v1"
USER_AGENT = "Dikarya Phylogenetic Tree Builder 1.0"
REQUEST_TIMEOUT = 30
MAX_RAW_INPUT_LEN = 300

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
    if raw is None:
        raise InatTreeError("No iNaturalist observation provided.")
    s = str(raw).strip()
    if not s:
        raise InatTreeError("No iNaturalist observation provided.")
    if len(s) > MAX_RAW_INPUT_LEN:
        raise InatTreeError("Input is too long.")
    if s.isdigit():
        if len(s) > 12:
            raise InatTreeError("iNaturalist observation ID is implausibly long.")
        return int(s)
    m = OBS_URL_RE.match(s)
    if not m:
        raise InatTreeError(
            "Provide a single iNaturalist observation as either a numeric "
            "ID (e.g. 360934883) or a single-observation URL "
            "(https://www.inaturalist.org/observations/<id>). Search URLs "
            "are not accepted."
        )
    return int(m.group(1))


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
    "tree_method": "fasttree",
    "tree_model": "GTR+G",
    "bootstrap": 1000,
    "mcmc_generations": 50000,
}


DNA_BARCODE_ITS_FIELD_NAME = "DNA Barcode ITS"


def _normalize_dna_for_match(text: str) -> str:
    """Uppercase + strip everything that isn't a DNA / IUPAC nucleotide letter.

    Used only for the exact-match comparison; the cleaned sequence we
    actually splice into the tree input goes through clean_dna_sequence so
    it follows the same rules as the rest of the pipeline.
    """
    if not text:
        return ""
    return re.sub(r"[^ACGTNRYSWKMBDHV]", "", str(text).upper())


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
            inat_token = f"iNat{int(observation_id)}"
            parts = original.split(None, 1)
            if not parts:
                new_name = inat_token
            elif inat_token in original.split():
                new_name = original  # already labelled with this iNat id
            else:
                head = parts[0]
                rest = parts[1] if len(parts) > 1 else ""
                new_name = f"{head} {inat_token} {rest}".strip()
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
                                      include_local: bool = True) -> Dict[str, Any]:
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
        "alignment_options": {},
        "tree_method": DEFAULT_TREE_PARAMS["tree_method"],
        "tree_model": DEFAULT_TREE_PARAMS["tree_model"],
        "bootstrap": DEFAULT_TREE_PARAMS["bootstrap"],
        "mcmc_generations": DEFAULT_TREE_PARAMS["mcmc_generations"],
        "mcmc_nruns": 2,
        "mcmc_nchains": 4,
        "mycomap_blast_url": mycomap_url,
    }

    job_id = enqueue_job(job_params)
    obs_source_url = f"https://www.inaturalist.org/observations/{observation_id}"
    metrics = {
        "via": "inat_phylogenetic_tree",
        "tree_method": job_params["tree_method"],
        "alignment_method": job_params["alignment_method"],
        "trimming_method": job_params["trimming_method"],
        "notes": job_params["notes"],
        "inat_observation_id": observation_id,
        "inat_source_url": obs_source_url,
        "mycomap_blast_url": mycomap_url,
        "inat_update_status": "pending",
        "inat_observation_field": PHYLOGENETIC_TREE_FIELD_NAME,
        "inat_added_its_sequence": bool(added_inat_its),
    }
    if added_inat_its:
        metrics["inat_added_its_name"] = added_inat_its
    if matched_inat_its_tip:
        # An existing MycoMap result already carried this exact sequence
        # (typically under its GenBank accession). The viewer will color
        # that tip blue when the job finishes.
        metrics["inat_matched_its_tip"] = matched_inat_its_tip
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
                                       extra_tip_names: Optional[List[str]] = None
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
        from app.services.tree_edit_service import load_tree_state, save_tree_state
        job_dir = Config.JOB_DIR / job_id
        state = load_tree_state(job_dir)
        if not state or not isinstance(state.get("tree_structure"), dict):
            return []
        all_tip_names = list(_iter_tree_tip_names(state["tree_structure"]))
        targets: List[str] = []

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
            base = (current_app.config.get("INAT_PUBLIC_BASE_URL")
                    or "https://dikarya.us").rstrip("/")
            tree_url = f"{base}/job/{job_id}/view"
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
