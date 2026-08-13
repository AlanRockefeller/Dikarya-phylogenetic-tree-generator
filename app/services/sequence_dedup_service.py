"""
Observation-level near-duplicate dedup.

The same physical collection often reaches a job twice: once from an
iNaturalist/Mushroom Observer import and once from a MycoMap local-BLAST hit.
Those two records carry different headers and different location strings, so
neither the worker's exact-record cleanup (identical header AND sequence) nor
`_dedupe_sequence_payload` (sequence + normalized location) collapses them, and
the tree ends up with two tips for one observation.

This module collapses records that share an observation number *and* whose
sequences differ by at most a few bases. Records without a resolvable
observation number (GenBank accessions, UNITE/BOLD entries, pasted FASTA) are
never touched, and two different observations are never merged even when their
sequences are identical -- an identical ITS from a separate collection is a real
observation and stays in the tree.

Used by every job-creation path so behaviour does not depend on which entry
point built the job.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum non-ambiguous base differences still treated as the same record within
# a single observation. Deliberately separate from
# MYCOMAP_NEAR_DUPLICATE_MAX_DIFFERENCES (which governs the MycoMap import
# stage): this rule runs over every input source, so it is set tighter.
#
# Counted over the region the two reads share, not end to end. The same read
# reaching a job from two sources is routinely trimmed to different lengths --
# 584 vs 596 bases is typical -- and an end-to-end comparison charges a base per
# overhang, so every real duplicate scored 5-15 and nothing was ever collapsed.
OBSERVATION_NEAR_DUPLICATE_MAX_DIFFERENCES = 3

# Cap the pairwise comparisons done inside one observation group. Groups are
# normally 2-3 records; anything pathological degrades to "keep them all"
# rather than burning CPU on an O(n^2) scan at submit time.
MAX_GROUP_SIZE = 25


def _parse_fasta(text: str) -> List[Dict[str, str]]:
    """Parse FASTA text into [{name, sequence}], preserving full headers."""
    records: List[Dict[str, str]] = []
    name: Optional[str] = None
    chunks: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if name is not None:
                records.append({"name": name, "sequence": "".join(chunks)})
            name = line[1:].strip()
            chunks = []
        elif name is not None:
            chunks.append(line)
    if name is not None:
        records.append({"name": name, "sequence": "".join(chunks)})
    return records


def _format_fasta(records: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    for record in records:
        lines.append(f">{record.get('name', '')}")
        sequence = "".join(str(record.get("sequence") or "").split())
        for index in range(0, len(sequence), 80):
            lines.append(sequence[index:index + 80])
    return "\n".join(lines) + "\n" if lines else ""


def observation_reference(header: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Return an 'inat:123'/'mo:123' reference for a record, or '' if unknown.

    Checks the FASTA header first, then falls back to the observation_id /
    internal_id fields that local MycoMap hits carry instead of a header token.
    """
    from app.services.mycomap_service import extract_mycomap_observation_reference

    reference = extract_mycomap_observation_reference(header or "")
    if reference:
        return reference

    metadata = metadata or {}
    for key in ("observation_id", "internal_id", "display_label", "fasta_header"):
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        # A bare numeric observation_id carries no source prefix; iNaturalist is
        # the only source that stores one, so qualify it before extraction.
        candidate = f"iNat{value}" if value.isdigit() else value
        reference = extract_mycomap_observation_reference(candidate)
        if reference:
            return reference
    return ""


def _difference_count(
    first: str,
    second: str,
    max_differences: int,
) -> Optional[int]:
    """Differences between two reads, ignoring how far each extends at the ends.

    Falls back to the end-to-end comparison when the reads cannot be lined up
    confidently, so an unanchored pair is judged conservatively rather than
    waved through as a duplicate.
    """
    from app.services.mycomap_service import (
        mycomap_sequence_difference_count,
        mycomap_sequence_overlap_difference_count,
    )

    difference = mycomap_sequence_overlap_difference_count(
        first, second, max_distance=max_differences
    )
    if difference is not None:
        return difference
    return mycomap_sequence_difference_count(
        first, second, max_distance=max_differences
    )


def _header_key(value: Any) -> str:
    """Comparison key for a FASTA header.

    Punctuation is dropped because headers are sanitized on the way into the
    FASTA (quotes in names like Lepiota "clypeolaria-IN02" are stripped) while
    sequence_metadata keeps the original text. Comparing raw strings makes the
    metadata look mismatched on most real jobs.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _metadata_lookup(
    records: List[Dict[str, str]],
    sequence_metadata: List[Dict[str, Any]],
) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
    """Mirror _dedupe_sequence_payload: prefer positional metadata when it lines up."""
    by_header: Dict[str, Dict[str, Any]] = {}
    for item in sequence_metadata or []:
        for key in (item.get("fasta_header"), item.get("name")):
            if key:
                by_header.setdefault(_header_key(key), item)

    positional = (
        len(sequence_metadata or []) == len(records)
        and all(
            _header_key(
                sequence_metadata[index].get("fasta_header")
                or sequence_metadata[index].get("name")
            ) == _header_key(record.get("name"))
            for index, record in enumerate(records)
        )
    )
    return positional, by_header


def dedupe_by_observation(
    sequence_text: str,
    sequence_metadata: Optional[List[Dict[str, Any]]] = None,
    max_differences: int = OBSERVATION_NEAR_DUPLICATE_MAX_DIFFERENCES,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse near-identical records that share an observation number.

    Returns (sequence_text, sequence_metadata, removed_records). Removed records
    keep their sequence so the viewer can offer a rebuild that puts them back.
    On any unexpected failure the input is returned untouched -- dropping tips is
    far worse than leaving a duplicate in.
    """
    sequence_metadata = list(sequence_metadata or [])
    try:
        records = _parse_fasta(sequence_text)
        if len(records) < 2:
            return sequence_text, sequence_metadata, []

        positional, by_header = _metadata_lookup(records, sequence_metadata)

        def metadata_for(index: int, record: Dict[str, str]) -> Dict[str, Any]:
            if positional:
                return sequence_metadata[index]
            return by_header.get(_header_key(record.get("name")), {})

        groups: Dict[str, List[int]] = {}
        for index, record in enumerate(records):
            reference = observation_reference(
                str(record.get("name") or ""), metadata_for(index, record)
            )
            if reference:
                groups.setdefault(reference, []).append(index)

        dropped: Dict[int, Dict[str, Any]] = {}
        for reference, indexes in groups.items():
            if len(indexes) < 2 or len(indexes) > MAX_GROUP_SIZE:
                continue
            # Longest first, so the representative left in the tree is the most
            # complete read of the observation and the shorter trimmings are the
            # ones removed -- never the other way round.
            ordered = sorted(
                indexes,
                key=lambda i: (-len(records[i].get("sequence") or ""), i),
            )
            kept: List[int] = []
            for index in ordered:
                sequence = records[index].get("sequence") or ""
                duplicate_of = None
                for kept_index in kept:
                    difference = _difference_count(
                        sequence,
                        records[kept_index].get("sequence") or "",
                        max_differences,
                    )
                    if difference is not None and difference <= max_differences:
                        duplicate_of = (kept_index, difference)
                        break
                if duplicate_of is None:
                    kept.append(index)
                    continue
                kept_index, difference = duplicate_of
                kept_name = records[kept_index].get("name", "")
                dropped[index] = {
                    "name": records[index].get("name", ""),
                    "sequence": records[index].get("sequence", ""),
                    "duplicate_of": kept_name,
                    "observation_reference": reference,
                    "difference_count": difference,
                    "reason": "duplicate_observation_record",
                    "reason_label": (
                        f"Same observation as '{kept_name}'; identical where the "
                        f"reads overlap"
                        if difference == 0
                        else (
                            f"Same observation as '{kept_name}' ({difference} base "
                            f"difference{'' if difference == 1 else 's'} where the "
                            f"reads overlap)"
                        )
                    ),
                }

        if not dropped:
            return sequence_text, sequence_metadata, []

        kept_records = [r for i, r in enumerate(records) if i not in dropped]
        if positional:
            kept_metadata = [
                m for i, m in enumerate(sequence_metadata) if i not in dropped
            ]
        else:
            dropped_headers = {_header_key(records[i].get("name")) for i in dropped}
            kept_headers = {_header_key(r.get("name")) for r in kept_records}
            kept_metadata = [
                item for item in sequence_metadata
                if _header_key(item.get("fasta_header") or item.get("name"))
                not in (dropped_headers - kept_headers)
            ]

        removed = [dropped[i] for i in sorted(dropped)]
        logger.info(
            "Observation dedup removed %d of %d records across %d observation group(s).",
            len(removed), len(records), len(groups),
        )
        return _format_fasta(kept_records), kept_metadata, removed

    except Exception:
        logger.exception("Observation dedup failed; leaving sequences untouched.")
        return sequence_text, sequence_metadata, []


def record_dedup_details(job_params: Dict[str, Any], removed: List[Dict[str, Any]]) -> None:
    """Store removed duplicates on job_params so the viewer can list them."""
    if not removed:
        return
    details = job_params.setdefault("import_filter_details", {})
    if not isinstance(details, dict):
        details = {}
        job_params["import_filter_details"] = details
    details["duplicates"] = {
        "label": "Duplicate observation records",
        "max_differences": OBSERVATION_NEAR_DUPLICATE_MAX_DIFFERENCES,
        "removed_count": len(removed),
        "removed_records": removed,
    }


def apply_observation_dedup(job_params: Dict[str, Any]) -> int:
    """Run dedup over a job_params dict in place. Returns how many were removed.

    Honours job_params['skip_observation_dedup'], which the "rebuild including
    duplicates" action sets so restored records are not immediately re-removed.
    """
    if job_params.get("skip_observation_dedup"):
        return 0
    text, metadata, removed = dedupe_by_observation(
        job_params.get("sequence", ""),
        job_params.get("sequence_metadata", []),
    )
    if not removed:
        return 0
    job_params["sequence"] = text
    job_params["sequence_metadata"] = metadata
    record_dedup_details(job_params, removed)
    return len(removed)
