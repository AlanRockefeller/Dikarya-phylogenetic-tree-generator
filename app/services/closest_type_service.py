"""The type specimen in a job's tree that is closest to its sequence of interest.

Powers the tree viewer's "your sequence is 98.7% identical to the holotype of
X" callout. Only type tips already in the tree are compared -- no search is
run -- and identity is computed here, on the job's own alignment:

* Columns where both sequences hold an unambiguous base (A, C, G, T/U) are
  compared. Gaps, N and the other IUPAC ambiguity codes are skipped, so a
  shorter read is not penalised for the part of the locus it does not cover.
* The UNTRIMMED alignment is used, because trimming throws away exactly the
  variable regions that separate close species; the trimmed file is only a
  fallback.
* A pair needs at least ``MIN_COMPARED_BP`` comparable positions, or the
  percentage would be meaningless.

This is not the BLAST ``identity`` stored in ``sequence_metadata`` (which
belongs to the search that retrieved a reference and was measured against a
different query). Nothing here is written to the job: tree state is read, never
created or saved.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from app.services.artifact_storage import artifact_exists, open_artifact

logger = logging.getLogger(__name__)

MIN_COMPARED_BP = 100
_BASES = frozenset("ACGT")


def _strip_reversed(name: str) -> str:
    return name[3:] if name.startswith("_R_") else name


def _alignment_path(job_dir: Path, recomputed: bool) -> Optional[Path]:
    alignment = job_dir / "alignment"
    if recomputed:
        names = ("alignment_pruned_aligned.fasta", "alignment_pruned_trimmed.fasta")
    else:
        names = ()
    names += ("alignment_raw.fasta", "alignment_trimmed.fasta")
    return next((alignment / n for n in names if artifact_exists(alignment / n)), None)


def _read_sequences(path: Path, wanted: Iterable[str]) -> Dict[str, str]:
    """Aligned sequences for ``wanted`` names only, keyed by the wanted name.

    A record matches on its full header (what tips are labelled with) or on its
    first word, with MAFFT's ``_R_`` reversal prefix removed from both.
    """
    wanted = {_strip_reversed(n): n for n in wanted if n}
    found: Dict[str, list] = {}
    current = None
    with open_artifact(path, "rt") as handle:
        for line in handle:
            line = line.rstrip("\n\r")
            if line.startswith(">"):
                header = _strip_reversed(line[1:].strip())
                key = header if header in wanted else (header.split() or [""])[0]
                name = wanted.get(key)
                current = None
                if name and name not in found:
                    current = found[name] = []
            elif current is not None:
                current.append(line.strip())
    return {name: "".join(parts).upper().replace("U", "T") for name, parts in found.items()}


def pairwise_identity(a: str, b: str) -> Dict[str, int]:
    """Matches and comparable positions between two rows of one alignment."""
    compared = matches = 0
    for x, y in zip(a, b):
        if x in _BASES and y in _BASES:
            compared += 1
            if x == y:
                matches += 1
    return {"compared": compared, "matches": matches}


def _type_phrase(status: str, organism: str) -> str:
    """'the holotype of X' / 'an ex-type culture of X' / 'a type of X'."""
    status = (status or "").strip().lower() or "type"
    if status == "type":
        noun = "a type specimen"
    elif status.startswith("ex-"):
        noun = f"an {status} culture"
    else:
        noun = f"the {status}"
    return f"{noun} of {organism}" if organism else noun


def closest_type_for_job(job_dir: Path) -> Dict[str, Any]:
    """``{"closest": {...} | None, "reason": str}`` for the viewer callout."""
    from app.services.tree_rooting_service import _tree_tip_set, resolve_sequence_of_interest
    from app.services.type_specimen_service import resolve_tip, type_specimens_for_job

    job_dir = Path(job_dir)
    try:
        with open_artifact(job_dir / "tree_state.json", "rt") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {"closest": None, "reason": "no_tree_state"}
    if not isinstance(state, dict):
        return {"closest": None, "reason": "no_tree_state"}

    focal, _source, _warnings = resolve_sequence_of_interest(state)
    if not focal:
        return {"closest": None, "reason": "no_sequence_of_interest"}

    try:
        with open_artifact(job_dir / "input_info.json", "rt") as handle:
            metadata = (json.load(handle) or {}).get("sequence_metadata")
    except (OSError, ValueError):
        metadata = None
    job_types = type_specimens_for_job(job_dir, metadata)
    tips = _tree_tip_set(state) - set(state.get("pruned_taxa") or [])
    types = {}
    for tip in tips:
        if tip == focal:
            continue
        info = resolve_tip(tip, job_types)
        if info:
            types[tip] = info
    if not types:
        return {"closest": None, "reason": "no_type_tips"}
    if resolve_tip(focal, job_types):
        # The sequence of interest is itself a type; "closest type" would
        # compare a type with its neighbours, which is not this callout's job.
        return {"closest": None, "reason": "focal_is_type"}

    recomputed = artifact_exists(job_dir / "tree" / "tree_pruned_metadata.json")
    path = _alignment_path(job_dir, recomputed)
    if path is None:
        return {"closest": None, "reason": "no_alignment"}
    sequences = _read_sequences(path, [focal, *types])
    query = sequences.get(focal)
    if not query:
        return {"closest": None, "reason": "focal_not_in_alignment"}

    best = None
    for tip, info in types.items():
        row = sequences.get(tip)
        if not row or len(row) != len(query):
            continue
        score = pairwise_identity(query, row)
        if score["compared"] < MIN_COMPARED_BP:
            continue
        identity = score["matches"] / score["compared"]
        key = (identity, score["compared"])
        if best is None or key > best[0]:
            best = (key, tip, info, score)
    if best is None:
        return {"closest": None, "reason": "insufficient_overlap"}

    (identity, _), tip, info, score = best
    organism = str(info.get("organism") or "").strip()
    status = str(info.get("status") or "type").strip()
    percent = round(identity * 100, 1)
    return {
        "closest": {
            "sequence_of_interest": focal,
            "type_tip": tip,
            "accession": info.get("accession"),
            "status": status,
            "organism": organism,
            "identity_percent": percent,
            "compared_bp": score["compared"],
            "differences": score["compared"] - score["matches"],
            "alignment": path.name.replace(".gz", ""),
            "type_tips_compared": len(types),
            "summary": (
                f"Your sequence is {percent:g}% identical to "
                f"{_type_phrase(status, organism)}"
            ),
        },
        "reason": "ok",
    }
