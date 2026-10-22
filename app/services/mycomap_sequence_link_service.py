"""Resolve a tree tip to its exact public MycoMap sequence page."""

import json
import re
from pathlib import Path

from app.services.artifact_storage import artifact_exists, open_artifact
from app.services.fasta_utils import parse_fasta_records


class MycoMapSequenceLinkError(Exception):
    """A tree tip cannot be resolved safely to one MycoMap sequence."""

    def __init__(self, message, status=404):
        super().__init__(message)
        self.status = status


_DNA_COMPLEMENT = str.maketrans(
    "ACGTRYSWKMBDHVN",
    "TGCAYRSWMKVHDBN",
)


def _normalize_dna(value):
    return re.sub(r"[^ACGTRYSWKMBDHVN]", "", str(value or "").upper().replace("U", "T"))


def _sequence_orientations(value):
    sequence = _normalize_dna(value)
    if not sequence:
        return set()
    return {sequence, sequence.translate(_DNA_COMPLEMENT)[::-1]}


def _ric(value):
    match = re.search(r"\bric\s*(?:[=:]\s*)?(\d+)\b", str(value or ""), re.I)
    return match.group(1) if match else ""


def _iter_leaves(node):
    if not isinstance(node, dict):
        return
    children = node.get("children") or []
    if children:
        for child in children:
            yield from _iter_leaves(child)
        return
    yield node


def _original_tip_name(state, requested):
    renames = state.get("renames") if isinstance(state, dict) else {}
    renames = renames if isinstance(renames, dict) else {}
    matches = []
    for leaf in _iter_leaves((state or {}).get("tree_structure") or {}):
        original = str(leaf.get("original_name") or leaf.get("name") or "").strip()
        displayed = str(renames.get(original) or leaf.get("display_name")
                        or leaf.get("name") or original).strip()
        if requested in {original, displayed} and original not in matches:
            matches.append(original)
    if len(matches) != 1:
        raise MycoMapSequenceLinkError(
            "That tree sequence is no longer available." if not matches
            else "That tree label matches more than one sequence.",
            409,
        )
    return matches[0]


def _metadata_for_tip(job_info, original_name):
    exact = []
    token_matches = []
    wanted_token = original_name.split(None, 1)[0] if original_name else ""
    for row in job_info.get("sequence_metadata") or []:
        if not isinstance(row, dict):
            continue
        names = {
            str(row.get("fasta_header") or "").strip(),
            str(row.get("name") or "").strip(),
        }
        if original_name in names:
            exact.append(row)
        elif wanted_token and any(
            name.split(None, 1)[0] == wanted_token for name in names if name
        ):
            token_matches.append(row)
    matches = exact or token_matches
    if len(matches) != 1:
        raise MycoMapSequenceLinkError("No unique MycoMap source record was saved for this tip.")
    row = matches[0]
    if (str(row.get("source") or "").lower() != "mycomap"
            or str(row.get("hit_source") or "").lower() not in {"local", "sequence_record"}):
        raise MycoMapSequenceLinkError("This tip is not a MycoMap sequence record.", 422)
    return row


def _matching_fasta_sequences(fasta_text, original_name):
    records = parse_fasta_records(str(fasta_text or ""))
    exact = [sequence for header, sequence in records if header == original_name]
    if exact:
        return exact
    token = original_name.split(None, 1)[0] if original_name else ""
    token_matches = [
        sequence for header, sequence in records
        if token and header.split(None, 1)[0] == token
    ]
    return token_matches if len(token_matches) == 1 else []


def _tip_sequences(job_dir, job_info, original_name):
    sequences = list(_matching_fasta_sequences(job_info.get("sequence"), original_name))
    processed_path = Path(job_dir) / "input" / "input_raw.fasta"
    if artifact_exists(processed_path):
        with open_artifact(processed_path, "rt") as handle:
            sequences.extend(_matching_fasta_sequences(handle.read(), original_name))
    normalized = set()
    for sequence in sequences:
        normalized.update(_sequence_orientations(sequence))
    return normalized


def _resolve_remote_record(reference, sequences, label):
    from app.services.mycomap_org_service import OrgResultError, observation_sequences

    try:
        rows = observation_sequences(reference)
    except OrgResultError as exc:
        raise MycoMapSequenceLinkError(str(exc), exc.status) from exc

    matches = []
    for row in rows:
        record_id = str(row.get("id") or "").strip()
        record_sequence = _normalize_dna(row.get("sequence"))
        if record_id.isdigit() and record_sequence and record_sequence in sequences:
            matches.append(row)

    if len(matches) > 1:
        wanted_ric = _ric(label)
        if wanted_ric:
            ric_matches = [row for row in matches if _ric(row.get("title")) == wanted_ric]
            if ric_matches:
                matches = ric_matches
    if len(matches) != 1:
        message = (
            "MycoMap has more than one record with this observation and sequence."
            if matches else
            "MycoMap no longer has a sequence matching this tree tip."
        )
        raise MycoMapSequenceLinkError(message, 409 if matches else 404)
    return f"https://mycomap.org/sequences/{matches[0]['id']}"


def resolve_job_tip_url(job_dir, tip_name):
    """Return the canonical MycoMap URL for one original or displayed tip."""
    job_dir = Path(job_dir)
    from app.services.mycomap_service import extract_mycomap_observation_reference
    from app.services.tree_edit_service import load_tree_state

    state = load_tree_state(job_dir)
    original_name = _original_tip_name(state, str(tip_name or "").strip())
    input_info_path = job_dir / "input_info.json"
    try:
        with open_artifact(input_info_path, "rt") as handle:
            job_info = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        raise MycoMapSequenceLinkError("The job's sequence metadata could not be read.", 500) from exc

    metadata = _metadata_for_tip(job_info, original_name)
    saved_id = str(metadata.get("mycomap_sequence_id") or "").strip()
    if saved_id.isdigit():
        return f"https://mycomap.org/sequences/{saved_id}"

    displayed_name = str((state.get("renames") or {}).get(original_name) or original_name)
    reference = (
        extract_mycomap_observation_reference(original_name)
        or extract_mycomap_observation_reference(displayed_name)
        or extract_mycomap_observation_reference(metadata.get("display_label"))
    )
    if not reference:
        raise MycoMapSequenceLinkError(
            "This MycoMap sequence has no iNaturalist or Mushroom Observer reference.",
            422,
        )
    sequences = _tip_sequences(job_dir, job_info, original_name)
    if not sequences:
        raise MycoMapSequenceLinkError("The saved DNA sequence for this tip could not be found.")
    return _resolve_remote_record(reference, sequences, original_name)
