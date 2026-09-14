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


def record_accession(header: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Return the GenBank accession for a record, uppercase, or ''.

    Prefers the metadata field the import paths fill in, and falls back to the
    first token of the FASTA header, which is where both MycoMap's DB39 export
    and NCBI's own header put it.
    """
    from app.services.fasta_utils import is_genbank_accession

    metadata = metadata or {}
    for value in (metadata.get("accession"), metadata.get("internal_id")):
        text = str(value or "").strip()
        if text and is_genbank_accession(text):
            return text.upper()

    first = str(header or "").lstrip(">").strip().split(None, 1)
    if first and is_genbank_accession(first[0]):
        return first[0].upper()
    return ""


def _resolve_genbank_references(
    records: List[Dict[str, str]],
    references: List[str],
    accessions: List[str],
) -> int:
    """Fill in references for accession records using GenBank's own annotation.

    A GenBank record's FASTA header never carries the observation number -- it
    lives in the record's DEFINITION line or in an ``isolate``/``note``
    qualifier -- so without this step an accession and the iNaturalist record of
    the same collection have nothing in common to group on. Best effort by
    design: a failed or slow NCBI lookup leaves the duplicates in the tree,
    which is the same outcome as before, and never fails the submission.
    """
    from app.services.genbank_observation_service import lookup_observation_references

    wanted = sorted({
        accession for index, accession in enumerate(accessions)
        if accession and not references[index]
    })
    if not wanted:
        return 0

    resolved = lookup_observation_references(wanted)
    if not resolved:
        return 0

    filled = 0
    for index, accession in enumerate(accessions):
        if references[index] or not accession:
            continue
        reference = resolved.get(accession) or resolved.get(accession.split(".")[0])
        if reference:
            references[index] = reference
            filled += 1
    if filled:
        logger.info(
            "event=dedup.genbank_references_resolved accessions=%d resolved=%d "
            "Observation numbers were read from GenBank annotation.",
            len(wanted), filled,
        )
    return filled


def _identifier_label(reference: str) -> str:
    """Render 'inat:280384724' as the 'iNat280384724' form tip labels use."""
    source, _, number = str(reference or "").partition(":")
    if not number:
        return ""
    return f"iNat{number}" if source == "inat" else f"MO{number}"


def _contains_identifier(name: str, identifier: str) -> bool:
    key = _header_key(identifier)
    return bool(key) and key in _header_key(name)


def _merge_identifier_into_name(kept_name: str, identifier: str) -> str:
    """Add a collapsed record's identifier to the surviving tip's label.

    A GenBank record and an iNaturalist record of one collection each carry the
    number the other lacks, and collapsing them used to throw one of the two
    away -- the tip said PX860295 and nothing on screen connected it to
    iNat280384724. The identifier goes next to the surviving one, so a label
    reads "PX860295 iNat280384724 Panaeolus cinctulus ...", rather than at the
    end where the location text already lives.
    """
    from app.services.fasta_utils import is_genbank_accession

    identifier = str(identifier or "").strip()
    kept_name = str(kept_name or "").strip()
    if not identifier or not kept_name or _contains_identifier(kept_name, identifier):
        return kept_name

    parts = kept_name.split(None, 1)
    leading = parts[0]
    if is_genbank_accession(leading) or observation_reference(leading):
        rest = parts[1] if len(parts) > 1 else ""
        return " ".join(part for part in (leading, identifier, rest) if part)
    return f"{kept_name} {identifier}"


def dedupe_by_observation(
    sequence_text: str,
    sequence_metadata: Optional[List[Dict[str, Any]]] = None,
    max_differences: int = OBSERVATION_NEAR_DUPLICATE_MAX_DIFFERENCES,
    resolve_genbank_references: bool = True,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse near-identical records that share an observation number.

    Returns (sequence_text, sequence_metadata, removed_records). Removed records
    keep their sequence so the viewer can offer a rebuild that puts them back.
    On any unexpected failure the input is returned untouched -- dropping tips is
    far worse than leaving a duplicate in.

    ``resolve_genbank_references`` allows one NCBI annotation lookup for the
    accessions whose observation number is not in the FASTA at all; pass False
    to keep the whole thing offline.
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

        references: List[str] = []
        accessions: List[str] = []
        for index, record in enumerate(records):
            metadata = metadata_for(index, record)
            name = str(record.get("name") or "")
            references.append(observation_reference(name, metadata))
            accessions.append(record_accession(name, metadata))

        if resolve_genbank_references:
            try:
                _resolve_genbank_references(records, references, accessions)
            except Exception:
                logger.exception(
                    "GenBank observation lookup failed; grouping on the "
                    "observation numbers already in the input."
                )

        groups: Dict[str, List[int]] = {}
        for index, reference in enumerate(references):
            if reference:
                groups.setdefault(reference, []).append(index)

        # index of a removed record -> {kept_index, difference, reference}
        dropped: Dict[int, Dict[str, Any]] = {}
        # index of a surviving record -> its label after absorbing the
        # identifiers of the records collapsed into it.
        merged_names: Dict[int, str] = {}

        for reference, indexes in groups.items():
            if len(indexes) < 2:
                continue
            if len(indexes) > MAX_GROUP_SIZE:
                # Keeping all records is the safe outcome, but it is not the
                # requested one: the user gets a tree with duplicate tips for
                # this observation and nothing on screen says why. Say so here
                # so the cap can be reviewed against real data instead of
                # guessed at.
                logger.warning(
                    "event=dedup.group_skipped observation=%s records=%d max=%d "
                    "Observation group too large to compare pairwise; all of its "
                    "records were kept, so duplicate tips may remain.",
                    reference, len(indexes), MAX_GROUP_SIZE,
                )
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
                dropped[index] = {
                    "kept_index": kept_index,
                    "difference": difference,
                    "reference": reference,
                }
                # Carry the identifier the surviving record does not have --
                # the accession when a GenBank record is being collapsed, the
                # observation number when it is an import -- onto the tip that
                # stays, so one tip can be traced to both databases.
                identifier = (
                    accessions[index]
                    or _identifier_label(reference)
                )
                merged_names[kept_index] = _merge_identifier_into_name(
                    merged_names.get(kept_index, records[kept_index].get("name", "")),
                    identifier,
                )

        if not dropped:
            return sequence_text, sequence_metadata, []

        # Apply the merged labels before describing the removals, so every
        # "same observation as ..." message names the tip as it now reads.
        renamed: Dict[str, str] = {}
        for kept_index, merged_name in merged_names.items():
            original_name = records[kept_index].get("name", "")
            if not merged_name or merged_name == original_name:
                continue
            # Look the metadata up before renaming: without positional
            # alignment metadata_for() matches on the header, which the rename
            # is about to change.
            kept_metadata = metadata_for(kept_index, records[kept_index])
            records[kept_index]["name"] = merged_name
            renamed[merged_name] = original_name
            if kept_metadata:
                kept_metadata.setdefault("display_label",
                                         kept_metadata.get("name") or original_name)
                kept_metadata.setdefault("raw_fasta_header", original_name)
                kept_metadata["name"] = merged_name
                kept_metadata["fasta_header"] = merged_name
                kept_metadata["merged_observation_label"] = True

        removed: List[Dict[str, Any]] = []
        for index in sorted(dropped):
            entry = dropped[index]
            kept_name = records[entry["kept_index"]].get("name", "")
            difference = entry["difference"]
            removed_record = {
                "name": records[index].get("name", ""),
                "sequence": records[index].get("sequence", ""),
                "duplicate_of": kept_name,
                "observation_reference": entry["reference"],
                "difference_count": difference,
                "reason": "duplicate_observation_record",
                "reason_label": (
                    f"Same observation as \'{kept_name}\'; identical where the "
                    f"reads overlap"
                    if difference == 0
                    else (
                        f"Same observation as \'{kept_name}\' ({difference} base "
                        f"difference{'' if difference == 1 else 's'} where the "
                        f"reads overlap)"
                    )
                ),
            }
            original_kept_name = renamed.get(kept_name)
            if original_kept_name:
                # The surviving tip was relabelled to carry this record's
                # identifier. Both names travel with the removal so the
                # "rebuild including duplicates" action can put the original
                # label back instead of showing a merged tip beside the record
                # it was merged from.
                removed_record["merged_into_label"] = kept_name
                removed_record["kept_original_name"] = original_kept_name
            record_metadata = metadata_for(index, records[index])
            if record_metadata:
                # Keep observation/display/BLAST provenance with the removed
                # record so a later duplicate-restoration job can recreate
                # a positional metadata row for this exact occurrence.
                removed_record["metadata"] = dict(record_metadata)
            removed.append(removed_record)

        kept_records = [r for i, r in enumerate(records) if i not in dropped]
        if positional:
            kept_metadata_list = [
                m for i, m in enumerate(sequence_metadata) if i not in dropped
            ]
        else:
            dropped_headers = {_header_key(records[i].get("name")) for i in dropped}
            kept_headers = {_header_key(r.get("name")) for r in kept_records}
            kept_metadata_list = [
                item for item in sequence_metadata
                if _header_key(item.get("fasta_header") or item.get("name"))
                not in (dropped_headers - kept_headers)
            ]

        logger.info(
            "Observation dedup removed %d of %d records across %d observation group(s).",
            len(removed), len(records), len(groups),
        )
        return _format_fasta(kept_records), kept_metadata_list, removed

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
    merged_labels = {
        str(record.get("merged_into_label")): str(record.get("kept_original_name"))
        for record in removed
        if record.get("merged_into_label") and record.get("kept_original_name")
    }
    details["duplicates"] = {
        "label": "Duplicate observation records",
        "max_differences": OBSERVATION_NEAR_DUPLICATE_MAX_DIFFERENCES,
        "removed_count": len(removed),
        "removed_records": removed,
        # merged tip label -> the label it had before it absorbed a collapsed
        # record's identifier, so a rebuild that restores the duplicates can put
        # the original label back.
        "merged_labels": merged_labels,
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
        resolve_genbank_references=not job_params.get(
            "skip_genbank_observation_lookup"
        ),
    )
    if not removed:
        return 0
    job_params["sequence"] = text
    job_params["sequence_metadata"] = metadata
    record_dedup_details(job_params, removed)
    return len(removed)
