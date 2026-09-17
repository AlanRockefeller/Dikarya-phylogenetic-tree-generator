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


# Metadata values that say a record came from NCBI rather than from an
# observation site. Both keys are set by the import paths: `source` names the
# panel the record arrived through, `hit_source` distinguishes a MycoMap
# local-database hit from a MycoMap NCBI hit, and it wins when the two disagree
# because it is the more specific of the two.
_GENBANK_SOURCES = frozenset({"genbank", "ncbi", "blast", "nuccore"})
_OBSERVATION_SOURCES = frozenset({
    "inat", "inaturalist", "mo", "mushroom_observer", "mushroomobserver",
    "mycomap", "mo_observation", "inat_observation", "local",
})

# "MO123456.1" -- a version suffix is a GenBank thing. Mushroom Observer labels
# have never carried one, so this shape settles the ambiguity on its own even
# when the record has no provenance metadata at all.
_VERSIONED_COMPACT_MO_RE = re.compile(r"^MO\d{5,12}\.\d+$", re.IGNORECASE)


def record_provenance(header: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Where a record came from: ``'genbank'``, ``'observation'`` or ``'unknown'``.

    Used to settle the one genuinely ambiguous identifier Dikarya handles: the
    compact token ``MO123456``, which is simultaneously a Mushroom Observer tip
    label and a valid two-letter GenBank accession. See
    ``extract_mycomap_observation_reference`` for why that matters here in
    particular -- this module *deletes* records that share a reference.
    """
    metadata = metadata or {}
    hit_source = str(metadata.get("hit_source") or "").strip().lower()
    source = str(metadata.get("source") or "").strip().lower()
    for value in (hit_source, source):
        if value in _GENBANK_SOURCES:
            return "genbank"
        if value in _OBSERVATION_SOURCES:
            return "observation"

    first = str(header or "").lstrip(">").strip().split(None, 1)
    if first and _VERSIONED_COMPACT_MO_RE.match(first[0]):
        return "genbank"

    if str(metadata.get("observation_id") or "").strip():
        return "observation"
    return "unknown"


def observation_reference(header: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Return an 'inat:123'/'mo:123' reference for a record, or '' if unknown.

    Checks the FASTA header first, then falls back to the observation_id /
    internal_id fields that local MycoMap hits carry instead of a header token.

    Only a record known to have come from an observation source yields a
    reference from the bare ``MO123456`` token. There, the token is ambiguous
    with a real GenBank accession, and this function feeds a destructive dedup:
    unknown provenance therefore takes the safe false negative. Explicit
    Mushroom Observer references still count from any source, so a GenBank
    record whose ``/isolate`` says "Mushroom Observer 123456" is still linked
    to that observation -- which is the point of the GenBank annotation lookup.
    """
    from app.services.mycomap_service import extract_mycomap_observation_reference

    provenance = record_provenance(header, metadata)
    allow_compact_mo = provenance == "observation"

    reference = extract_mycomap_observation_reference(
        header or "", allow_compact_mo=allow_compact_mo
    )
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
        reference = extract_mycomap_observation_reference(
            candidate, allow_compact_mo=allow_compact_mo
        )
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


_COMPACT_MO_RE = re.compile(r"^MO\d{5,12}(?:\.\d+)?$", re.IGNORECASE)


def record_accession(header: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    """Return the GenBank accession for a record, uppercase, or ''.

    Prefers the metadata field the import paths fill in, and falls back to the
    first token of the FASTA header, which is where both MycoMap's DB39 export
    and NCBI's own header put it.

    The compact ``MO123456`` token is accession-shaped and is also how every
    MycoMap local hit is labelled -- 19,661 records on disk carry it as their
    ``internal_id``. Reading those as accessions sent Mushroom Observer
    observation numbers to NCBI efetch and, worse, offered them to
    ``_merge_identifier_into_name`` as "the GenBank accession this tip lacks".
    So it counts as an accession only when the record is known to have come
    from GenBank, or when it carries a version suffix (which no Mushroom
    Observer label does).
    """
    from app.services.fasta_utils import is_genbank_accession

    metadata = metadata or {}
    provenance = record_provenance(header, metadata)

    def usable(text: str) -> bool:
        if not text or not is_genbank_accession(text):
            return False
        if not _COMPACT_MO_RE.match(text):
            return True
        return provenance == "genbank"

    for value in (metadata.get("accession"), metadata.get("internal_id")):
        text = str(value or "").strip()
        if usable(text):
            return text.upper()

    first = str(header or "").lstrip(">").strip().split(None, 1)
    if first and usable(first[0]):
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


def _normalized_with_positions(value: str):
    """``(normalized_text, original_index_of_each_normalized_character)``.

    Same normalization as ``_header_key`` -- lowercase, everything that is not
    a letter or digit dropped -- but keeping the link back to the original
    string, so a match can be checked against the characters that were removed.
    """
    text = str(value or "")
    chars = []
    positions = []
    for index, char in enumerate(text):
        if char.isalnum():
            chars.append(char.lower())
            positions.append(index)
    return "".join(chars), positions


def _contains_identifier(name: str, identifier: str) -> bool:
    """Is ``identifier`` already present in ``name`` as an identifier?

    Alan 9/14/26 - This used to be a bare substring test over the normalized
    text, and normalizing strips the punctuation that separates one identifier
    from the next. "iNat280384724" therefore read as already present inside
    "iNat2803847241" -- a different observation whose id merely starts with the
    same digits -- and the merge that should have added it was skipped, so the
    surviving tip silently lost the identifier of the record collapsed into it.

    Punctuation still has to be ignored, because these two strings are written
    differently on purpose: headers are sanitized on the way into the FASTA
    while sequence_metadata keeps the original text, and the same reference is
    written "iNat280384724", "iNat 280384724" and "iNat #280384724" by different
    sources. So the search runs over the normalized text and the *boundary* is
    checked against the original: a hit counts only when the characters
    immediately either side of it in the original string are not alphanumeric.
    That accepts "PX860295" inside "PX860295.1" and rejects it inside
    "PX8602951".
    """
    key = _header_key(identifier)
    if not key:
        return False
    haystack, positions = _normalized_with_positions(name)
    original = str(name or "")

    start = haystack.find(key)
    while start != -1:
        end = start + len(key)
        before_index = positions[start] - 1
        after_index = positions[end - 1] + 1
        before_ok = before_index < 0 or not original[before_index].isalnum()
        after_ok = (
            after_index >= len(original) or not original[after_index].isalnum()
        )
        if before_ok and after_ok:
            return True
        start = haystack.find(key, start + 1)
    return False


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
    resolve_genbank_references: bool = False,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collapse near-identical records that share an observation number.

    Returns (sequence_text, sequence_metadata, removed_records). Removed records
    keep their sequence so the viewer can offer a rebuild that puts them back.
    On any unexpected failure the input is returned untouched -- dropping tips is
    far worse than leaving a duplicate in.

    ``resolve_genbank_references`` allows one NCBI annotation lookup for the
    accessions whose observation number is not in the FASTA at all.

    Alan 9/14/26 - It defaults to False. This is a pure text function
    everywhere else, and a default that silently reaches the internet made it
    impossible to reason about (or test) without knowing which caller you were:
    the same call was free in the worker and up to DEFAULT_LOOKUP_SECONDS inside
    a Gunicorn request. The lookup is opted into by the orchestration layer --
    today only the worker, which has the time budget for it.
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
    """Store removed duplicates on job_params so the viewer can list them.

    Accumulates rather than replaces. Dedup can run more than once over the
    same job -- once offline at submit time and once in the worker, where the
    NCBI annotation lookup is affordable -- and overwriting here would leave the
    viewer listing only the second pass's removals while the first pass's
    records were already gone from the FASTA, with nothing to rebuild them from.
    """
    if not removed:
        return
    details = job_params.setdefault("import_filter_details", {})
    if not isinstance(details, dict):
        details = {}
        job_params["import_filter_details"] = details

    existing = details.get("duplicates")
    if not isinstance(existing, dict):
        existing = {}
    previous_records = list(existing.get("removed_records") or [])
    previous_labels = dict(existing.get("merged_labels") or {})

    # Identify a removal by the record it removed, so a repeated pass over an
    # already-deduped payload cannot list the same tip twice. Full FASTA headers
    # are not guaranteed unique at this stage: two distinct records may have
    # the same name but different sequence or provenance metadata, and both
    # must remain available to the rebuild action.
    def removal_identity(record):
        return (
            str(record.get("name") or ""),
            str(record.get("sequence") or ""),
            str(record.get("observation_reference") or ""),
            record.get("metadata"),
        )

    previous_identities = [removal_identity(record) for record in previous_records]
    combined = previous_records + [
        record for record in removed
        if removal_identity(record) not in previous_identities
    ]

    merged_labels = dict(previous_labels)
    for record in removed:
        label = record.get("merged_into_label")
        original = record.get("kept_original_name")
        if not label or not original:
            continue
        # A tip relabelled twice keeps its FIRST original name: that is the
        # label a rebuild has to restore, not the intermediate merged form.
        merged_labels.setdefault(str(label), str(original))
        if str(original) in merged_labels:
            merged_labels[str(label)] = merged_labels[str(original)]

    details["duplicates"] = {
        "label": "Duplicate observation records",
        "max_differences": OBSERVATION_NEAR_DUPLICATE_MAX_DIFFERENCES,
        "removed_count": len(combined),
        "removed_records": combined,
        # merged tip label -> the label it had before it absorbed a collapsed
        # record's identifier, so a rebuild that restores the duplicates can put
        # the original label back.
        "merged_labels": merged_labels,
    }


def apply_observation_dedup(
    job_params: Dict[str, Any],
    *,
    resolve_genbank_references: bool = False,
) -> int:
    """Run dedup over a job_params dict in place. Returns how many were removed.

    Honours job_params['skip_observation_dedup'], which the "rebuild including
    duplicates" action sets so restored records are not immediately re-removed.

    ``resolve_genbank_references`` turns on the NCBI annotation lookup that
    links a GenBank accession to the observation its submitter recorded. It is
    off by default because the caller has to be able to afford the network
    round trip: the submit-time pass runs inside a Gunicorn request slot and
    must not, the worker's pass runs with the job's own time budget and does.
    job_params['skip_genbank_observation_lookup'] still disables it outright.
    """
    if job_params.get("skip_observation_dedup"):
        return 0
    text, metadata, removed = dedupe_by_observation(
        job_params.get("sequence", ""),
        job_params.get("sequence_metadata", []),
        resolve_genbank_references=(
            resolve_genbank_references
            and not job_params.get("skip_genbank_observation_lookup")
        ),
    )
    if not removed:
        return 0
    job_params["sequence"] = text
    job_params["sequence_metadata"] = metadata
    record_dedup_details(job_params, removed)
    return len(removed)
