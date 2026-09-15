"""Observation-number lookup for GenBank accessions.

A GenBank record and an iNaturalist/Mushroom Observer record can be the same
physical collection reaching a job twice, but the GenBank side carries no
observation number in anything the pipeline sees: MycoMap's DB39 export gives
accession/taxon/voucher/location, and NCBI's own FASTA header gives the
DEFINITION line. The link is in the *record*, where submitters routinely put the
observation in ``isolate``, ``specimen_voucher`` or ``/note``:

    DEFINITION  Panaeolus cinctulus isolate S. D. Russell iNat # 280384724 ...
    /isolate="S. D. Russell iNat # 280384724"
    /note="Mycota Labs; Run119; iNaturalist.org #280384724;"

So PX860295 and iNat280384724 are one observation, and without this lookup the
observation dedup has nothing to group them by and the tree grows two tips for
one collection.

This only resolves the number. Deciding whether two records are actually the
same read stays with `sequence_dedup_service`, which still compares sequences.
"""

import logging
import threading
import time
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Source qualifiers that carry a collector's own identifier, best first. These
# are scanned ahead of the whole-record blob so a number sitting in an unrelated
# field cannot outrank the one the submitter actually labelled.
OBSERVATION_QUALIFIERS = (
    "isolate",
    "specimen_voucher",
    "strain",
    "clone",
    "note",
    "collected_by",
)

# A published record's observation reference never changes, so an in-process
# cache is enough: it only has to outlive a burst of submissions. "" is cached
# too -- "NCBI has no observation number for this accession" is an answer worth
# keeping, and it is the common case.
_CACHE_MAX_ENTRIES = 20000
_cache_lock = threading.Lock()
_reference_cache: Dict[str, str] = {}

# Ceiling on how long one dedup pass may spend asking NCBI about accessions.
#
# Alan 9/14/26 - This budget used to be the thing standing between a submission
# and NCBI's retry schedule, because the lookup ran inside POST /api/job and so
# held one of the (workers x threads) Gunicorn slots for every job containing an
# accession. It no longer runs there: dedupe_by_observation() defaults to
# resolve_genbank_references=False and only run_phylo_job() opts in, so the wait
# is spent against the job's own RQ time budget with nobody watching. The
# ceiling stays anyway -- a worker blocked on efetch is still a worker not
# building trees -- but it is now a courtesy bound, not a request-latency one.
# Running out of budget means some duplicates survive, never that the job fails.
DEFAULT_LOOKUP_SECONDS = 12.0

# Ceiling on how many accessions one dedup pass will ask about. A job with more
# GenBank records than this is a bulk import, where a couple of duplicate tips
# matter less than several minutes of efetch.
MAX_LOOKUP_ACCESSIONS = 400


def _cache_get(key: str) -> Optional[str]:
    with _cache_lock:
        return _reference_cache.get(key)


def _cache_put(keys: Iterable[str], reference: str) -> None:
    with _cache_lock:
        if len(_reference_cache) > _CACHE_MAX_ENTRIES:
            _reference_cache.clear()
        for key in keys:
            if key:
                _reference_cache[key] = reference


def observation_reference_from_record(record: Dict) -> str:
    """Return 'inat:<id>'/'mo:<id>' for one parsed GenBank record, or ''.

    The answer feeds *destructive* deduplication -- two records that share a
    reference have one of them removed from the tree -- so "the first field that
    matched" is not good enough. The trusted fields (the DEFINITION line and the
    source qualifiers a collector actually writes their own identifier into) are
    all read, and their distinct references counted:

    * none            -> no reference
    * exactly one     -> use it, however many fields repeated it
    * two or more     -> the record contradicts itself. Log it and return ''
                         rather than guessing which observation it belongs to;
                         a duplicate tip is recoverable and a deleted record is
                         not.

    The blob -- definition + organism + every qualifier + the comment, run
    together -- is consulted only when the trusted fields say nothing, because a
    match in it means no more than "somewhere in this record", and an incidental
    number in a /PCR_primers or a citation must not outrank a clean /isolate.

    The bare ``MO123456`` token is never read as an observation here: this is by
    construction a GenBank record, where that token is an accession. Explicit
    Mushroom Observer references in the qualifiers still count -- see
    ``extract_mycomap_observation_reference``.
    """
    from app.services.mycomap_service import extract_mycomap_observation_references

    source = record.get("source_features") or {}
    trusted: List[str] = [str(record.get("definition") or "")]
    trusted.extend(str(source.get(qualifier) or "")
                   for qualifier in OBSERVATION_QUALIFIERS)

    found: List[str] = []
    for candidate in trusted:
        if not candidate:
            continue
        # Every reference in the field, not just the first. A single /note
        # reading "sequenced from iNat 280384724; compare iNat 999999999" names
        # two observations, and stopping at the first would group the record
        # with one of them on no evidence -- then delete whatever it collided
        # with. The same observation repeated across isolate/note/definition is
        # the normal case and is not a conflict; only distinct references are.
        for reference in extract_mycomap_observation_references(
            candidate, allow_compact_mo=False
        ):
            if reference not in found:
                found.append(reference)

    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        logger.warning(
            "event=dedup.genbank_reference_ambiguous accession=%s references=%s "
            "The record names more than one observation; it will not be used to "
            "deduplicate.",
            str(record.get("version") or record.get("accession") or "?"),
            ",".join(sorted(found)[:5]),
        )
        return ""

    blob = str(record.get("blob") or "")
    if not blob:
        return ""
    # Same rule for the fallback: the blob is the whole record run together, so
    # it is the candidate most likely to name two different things.
    from_blob = extract_mycomap_observation_references(blob, allow_compact_mo=False)
    if len(from_blob) == 1:
        return from_blob[0]
    if len(from_blob) > 1:
        logger.warning(
            "event=dedup.genbank_reference_ambiguous accession=%s references=%s "
            "source=blob The record names more than one observation; it will "
            "not be used to deduplicate.",
            str(record.get("version") or record.get("accession") or "?"),
            ",".join(sorted(from_blob)[:5]),
        )
    return ""


def lookup_observation_references(
    accessions: List[str],
    deadline: Optional[float] = None,
) -> Dict[str, str]:
    """Resolve GenBank accessions to observation references.

    Returns a mapping keyed by both the bare and the versioned accession
    (uppercase), holding only the accessions that had one. Accessions NCBI could
    not be asked about are simply absent: a missing key means "no observation
    number known", which is how the caller already treats every record.
    """
    from app.services.genbank_location_service import fetch_annotation_records

    requested: List[str] = []
    seen = set()
    for accession in accessions:
        normalized = str(accession or "").strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            requested.append(normalized)

    references: Dict[str, str] = {}
    to_fetch: List[str] = []
    for accession in requested:
        cached = _cache_get(accession)
        if cached is None:
            to_fetch.append(accession)
        elif cached:
            references[accession] = cached

    if to_fetch:
        if len(to_fetch) > MAX_LOOKUP_ACCESSIONS:
            logger.info(
                "event=dedup.genbank_lookup_capped requested=%d max=%d "
                "Only the first %d accessions were checked for observation "
                "numbers; duplicate tips may remain for the rest.",
                len(to_fetch), MAX_LOOKUP_ACCESSIONS, MAX_LOOKUP_ACCESSIONS,
            )
            to_fetch = to_fetch[:MAX_LOOKUP_ACCESSIONS]
        if deadline is None:
            deadline = time.monotonic() + DEFAULT_LOOKUP_SECONDS

        for _batch, records in fetch_annotation_records(to_fetch, deadline=deadline):
            if records is None:
                continue
            for record in records.values():
                reference = observation_reference_from_record(record)
                keys = [
                    str(record.get("accession") or "").upper(),
                    str(record.get("version") or "").upper(),
                ]
                _cache_put(keys, reference)
                if reference:
                    for key in keys:
                        if key:
                            references[key] = reference

    # A header may cite "PX860295" while NCBI answers with "PX860295.1", or the
    # other way round, so resolve each requested accession against its bare form.
    for accession in requested:
        if accession in references:
            continue
        base = accession.split(".")[0]
        if base in references:
            references[accession] = references[base]

    return references
