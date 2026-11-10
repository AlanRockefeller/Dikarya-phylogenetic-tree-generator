"""Which tree tips are type specimens, and what kind.

Three sources, merged per accession:

* **MycoMap's type-specimen list** (``https://mycomap.org/api/type-specimens``),
  snapshotted into ``mycomap_type_specimens.json`` by
  ``scripts/dikarya_refresh_type_specimens.py``. Read-only here.
* **MycoMap.org's curation flag** (``POST /api/mycoblast/genbank-curation``,
  ``isType``). A curator can designate a GenBank record a type on MycoMap.org
  (``typeBasis: "manual"``) without it ever reaching the list above -- OP739129,
  the Podaxis inyoensis holotype that GenBank files as P. pistillaris, is one.
  ``mycomap_org_service.curated_genbank_rows()`` hands every answer it gets to
  ``remember_mycomap_curation()``, which appends to
  ``mycomap_curation_types.jsonl``; the refresh script re-asks about recent
  jobs' accessions weekly, because a curator can set or clear the flag at any
  time. Only a positive flag with a trusted ``typeBasis`` marks a tip (see
  ``TRUSTED_CURATION_BASES``): ``isType: false`` means "not designated on
  MycoMap", not "not a type" (GenBank holotypes answer false), and the
  ``source`` basis is a keyword match that flags "RFLP type 52".
* **GenBank's own annotation**: the ``/type_material`` source qualifier, or
  RefSeq's "from TYPE material" DEFINITION marker. ``_parse_genbank_xml()``
  hands every record it parses to ``remember_genbank_records()``, which appends
  the type-bearing ones to ``genbank_type_material.jsonl``. The refresh script
  backfills the same file for accessions already sitting in old jobs. The two
  sources overlap but neither contains the other: the MycoMap list holds
  essentially no RefSeq ``NR_`` records, which are among the most common
  references in these trees and are very often built from type material.

Matching is by exact accession only (version dropped), never by organism name:
a name match would mark every sequence of a species as its type. Accessions
are resolved with ``record_accession()`` so a Mushroom Observer ``MO123456``
label is never taken for the GenBank accession of the same shape.

Everything here is display metadata. Nothing is written into a job directory,
so tree state, undo and recompute are unaffected, and an old job picks up type
markers as soon as any source learns about one of its accessions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from app.config import Config

logger = logging.getLogger(__name__)

# Module-level so the test suite can redirect it (tests/conftest.py) -- the
# GenBank parser writes here as a side effect, and a test run must not deposit
# fixture records next to real ones.
DATA_DIR = Path(Config.TYPE_SPECIMEN_DIR)
MYCOMAP_SNAPSHOT_NAME = "mycomap_type_specimens.json"
BUNDLED_MYCOMAP_PATH = Config.BASE_DIR / "mycomap-type-specimens.json"
GENBANK_CACHE_NAME = "genbank_type_material.jsonl"
CURATION_CACHE_NAME = "mycomap_curation_types.jsonl"
# genbank-curation's typeBasis says why MycoMap.org calls a record a type
# (first match wins), and only these are trusted. "manual" is a curator's
# designation (OP739129), "refseq" an NR_ record not "from reference/verified
# material", "type_material" NCBI's own /type_material qualifier (added by
# MycoMap.org PR #18; 0/30 false in its audit), "mycomap" MycoMap's stored
# flag. Not yet: "title" (a type named in the DEFINITION line) and "source" (a
# note, voucher or strain). "source" was a bare match on the word "type" and
# was wrong for every one checked on 2026-11-08 -- "RFLP type 52", "its type
# b", "type: OTU: KGP46", "study type", three on uncultured fungi. PR #18
# rewrites both with type-word rules (0/30 false in its audit); add them here
# once that is deployed AND its recheck has been applied to .org's
# genbank_curation table, because until then .org still serves the old flags.
# An unknown basis is not trusted until someone has looked at what it matches.
TRUSTED_CURATION_BASES = frozenset({"manual", "refseq", "type_material", "mycomap"})
# scripts/dikarya_refresh_type_specimens.py appends its statistics and one
# event=type_specimens.* line per type accession it adds, drops or reclassifies
# here, and scripts/dikarya_log_digest.py reports them. Keep the default in
# the shared type-specimen directory so scheduled and manual runs by different
# accounts read the same file.
REFRESH_LOG_PATH = Path(
    os.environ.get("DIKARYA_TYPE_SPECIMEN_REFRESH_LOG")
    or DATA_DIR / "refresh.log"
)

# Type categories as NCBI's /type_material vocabulary spells them (MycoMap's
# GenBank rows are copied from that qualifier). The iso-/para- forms are
# separate words, so a whole-word search never confuses "isotype" with "type".
_TYPE_WORDS = (
    "holotype", "isotype", "epitype", "lectotype", "neotype", "paratype",
    "syntype", "isoparatype", "isosyntype", "isolectotype", "isoepitype",
    "isoneotype", "topotype",
)
# Misspellings that occur in the MycoMap list itself (2026-09-24 snapshot).
_MISSPELLINGS = {"holtoype": "holotype", "holotpye": "holotype", "isoype": "isotype"}
_TYPE_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(set(_TYPE_WORDS) | set(_MISSPELLINGS), key=len, reverse=True)) + r")\b"
)
_GENERIC_TYPE_RE = re.compile(r"\btype(?:[ _]material|[ _]strain)?\b")
# "non-type specimen", "not type material", "not a holotype": a \b search finds
# the type word inside each of these, so a negation in front of it vetoes it.
_NEGATED_TYPE_RE = re.compile(r"\b(?:non|not|no)[\s-]*(?:an?\s+)?\w*type\b")
# NCBI appends this to the DEFINITION of a record built from type material.
FROM_TYPE_MATERIAL_RE = re.compile(r"\bfrom TYPE material\b")
# MAFFT prefixes a sequence it reverse-complemented with _R_, and that prefix
# survives into the tree's tip labels.
_REVERSED_PREFIX = "_R_"

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def classify_type_material(text: Any) -> Optional[str]:
    """Reduce a type-material description to a short status, or None.

    ``"holotype of Cortinarius rubrobrunneus"`` -> ``"holotype"``;
    ``"culture from epitype of X"`` -> ``"ex-epitype"``;
    ``"type material of X"`` -> ``"type"``. ``"reference material"`` is not
    type material (NCBI uses it for authoritative non-type specimens), so it
    returns None rather than being marked, as does a negated description such
    as ``"non-type specimen"``.
    """
    norm = " ".join(str(text or "").split()).lower()
    if not norm:
        return None
    if _NEGATED_TYPE_RE.search(norm):
        return None
    culture = norm.startswith("culture from ") or norm.startswith("ex-")
    match = _TYPE_WORD_RE.search(norm)
    if match:
        status = _MISSPELLINGS.get(match.group(1), match.group(1))
    elif _GENERIC_TYPE_RE.search(norm):
        status = "type"
    else:
        return None
    return f"ex-{status}" if culture else status


def accession_root(value: Any) -> str:
    """Uppercase accession without its version suffix, or ''."""
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0] if text else ""


def _strip_reversed_prefix(name: str) -> str:
    return name[len(_REVERSED_PREFIX):] if name.startswith(_REVERSED_PREFIX) else name


# --------------------------------------------------------------------------
# Loading the two sources (cached per process, reloaded when the file changes)
# --------------------------------------------------------------------------

def _file_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _cached_load(name: str, loader, *, path: Optional[Path] = None):
    path = path or DATA_DIR / name
    signature = _file_signature(path)
    key = (str(path), name)
    with _lock:
        entry = _cache.get(key)
        if entry and entry[0] == signature:
            return entry[1]
    value = loader(path) if signature else {}
    with _lock:
        _cache[key] = (signature, value)
    return value


def _load_mycomap(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("event=type_specimens.snapshot_unreadable path=%s error=%s", path, exc)
        return {}
    records = payload.get("records") if isinstance(payload, dict) else None
    return records if isinstance(records, dict) else {}


def build_mycomap_snapshot(rows):
    """Normalize raw MycoMap rows for both the bundled fallback and refresh."""
    from app.services.fasta_utils import is_genbank_accession

    records, ranks = {}, {}
    skipped = {"not_an_accession": 0, "not_type_material": 0}
    for row in rows:
        acc = accession_root(row.get("accessionNumber"))
        if not acc or not is_genbank_accession(acc):
            skipped["not_an_accession"] += 1
            continue
        type_material = " ".join(str(row.get("typeMaterial") or "").split())
        status = classify_type_material(type_material)
        if status is None:
            if type_material:
                skipped["not_type_material"] += 1
                continue
            status = "type"
        record = {
            "status": status,
            "type_material": type_material,
            "organism": " ".join(str(row.get("organism") or "").split()),
            "voucher": " ".join(str(row.get("specimenVoucher") or row.get("isolate") or "").split()),
            "source": row.get("source") or "",
        }
        row_id = row.get("id")
        rank = (status != "type", bool(type_material), bool(record["voucher"]),
                -row_id if isinstance(row_id, int) else 0)
        if acc not in records or rank > ranks[acc]:
            records[acc], ranks[acc] = record, rank
    return records, skipped


def _load_bundled_mycomap(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("specimens") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("missing specimens list")
        records, _ = build_mycomap_snapshot(rows)
        return records
    except (OSError, ValueError) as exc:
        logger.warning("event=type_specimens.bundled_snapshot_unreadable path=%s error=%s", path, exc)
        return {}


def _load_genbank(path: Path) -> Dict[str, Dict[str, Any]]:
    entries: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn last line from a crashed writer
                acc = accession_root(entry.get("accession")) if isinstance(entry, dict) else ""
                if acc:
                    entries[acc] = entry  # later lines win
    except OSError as exc:
        logger.warning("event=type_specimens.genbank_cache_unreadable path=%s error=%s", path, exc)
    return entries


def mycomap_index() -> Dict[str, Dict[str, Any]]:
    live_path = DATA_DIR / MYCOMAP_SNAPSHOT_NAME
    if live_path.is_file():
        return _cached_load(MYCOMAP_SNAPSHOT_NAME, _load_mycomap)
    return _cached_load("bundled_mycomap", _load_bundled_mycomap,
                        path=BUNDLED_MYCOMAP_PATH)


def genbank_index() -> Dict[str, Dict[str, Any]]:
    return _cached_load(GENBANK_CACHE_NAME, _load_genbank)


def curation_index() -> Dict[str, Dict[str, Any]]:
    """MycoMap.org curation answers, latest per accession (same format as GenBank's)."""
    return _cached_load(CURATION_CACHE_NAME, _load_genbank)


def curation_marks(entry: Optional[Dict[str, Any]]) -> bool:
    """True when a recorded curation answer should mark its tip as a type.

    Checked when read rather than only when written, so narrowing
    TRUSTED_CURATION_BASES takes effect on entries already in the file.
    """
    return bool(entry and entry.get("status")
                and entry.get("type_basis") in TRUSTED_CURATION_BASES)


def lookup(accession: Any, genbank_types: Optional[Dict[str, Any]] = None,
           mycomap_types: Optional[Dict[str, Any]] = None,
           curation_types: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Merged type information for one accession, or None if it is not a type.

    A caller resolving many accessions passes the indexes in, so each file is
    checked for changes once rather than once per accession.
    """
    acc = accession_root(accession)
    if not acc:
        return None
    genbank = (genbank_index() if genbank_types is None else genbank_types).get(acc)
    if genbank and not genbank.get("status"):
        genbank = None  # a recorded "fetched, not a type" answer
    mycomap = (mycomap_index() if mycomap_types is None else mycomap_types).get(acc)
    curation = (curation_index() if curation_types is None else curation_types).get(acc)
    if not curation_marks(curation):
        curation = None  # cleared since, or a basis that is not trusted
    if not genbank and not mycomap and not curation:
        return None

    sources = []
    if genbank:
        # An entry with no /type_material text was recognised from RefSeq's
        # DEFINITION marker alone (genbank_type_entry), and must not be
        # described as carrying a qualifier it does not have.
        sources.append("genbank" if genbank.get("type_material") else "genbank_definition")
    if mycomap:
        sources.append("mycomap")
    if curation:
        sources.append("mycomap_curation")

    def first(field, order):
        return next((str(rec.get(field)).strip() for rec in order
                     if rec and str(rec.get(field) or "").strip()), "")

    status = ((genbank or {}).get("status") or (mycomap or {}).get("status")
              or (curation or {}).get("status") or "type")
    return {
        "accession": acc,
        "status": status,
        "type_material": first("type_material", (genbank, mycomap)),
        # MycoMap's curated name outranks GenBank's: the curators correct
        # misidentified GenBank records there, and GenBank cannot be corrected.
        "organism": first("organism", (curation, genbank, mycomap)),
        "voucher": first("voucher", (genbank, mycomap)),
        "sources": sources,
    }


# --------------------------------------------------------------------------
# Recording GenBank's own annotation
# --------------------------------------------------------------------------

def _voucher_from_qualifiers(quals: Dict[str, Any]) -> str:
    for key in ("specimen_voucher", "culture_collection", "strain", "isolate"):
        value = " ".join(str(quals.get(key) or "").split())
        if value:
            return value
    return ""


def genbank_type_entry(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The cache entry for a parsed GenBank record, or None if it is not a type."""
    type_material = " ".join(str(record.get("type_material") or "").split())
    status = classify_type_material(type_material) if type_material else None
    if not status and FROM_TYPE_MATERIAL_RE.search(str(record.get("definition") or "")):
        status = "type"
    if not status:
        return None
    return {
        "accession": accession_root(record.get("accession")),
        "status": status,
        "type_material": type_material,
        "organism": " ".join(str(record.get("organism") or "").split()),
        "voucher": _voucher_from_qualifiers(record.get("source_features") or {}),
    }


def _append_entries(entries, name: str = GENBANK_CACHE_NAME) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = "".join(
        json.dumps({**entry, "recorded": stamp}, ensure_ascii=False, sort_keys=True) + "\n"
        for entry in entries
    )
    # One write() on an O_APPEND descriptor, so the web and worker processes
    # appending at the same moment interleave whole lines, not fragments.
    created = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    if created:
        try:
            from app.services.artifact_storage import default_file_mode
            os.chmod(path, default_file_mode())
        except OSError:
            pass


def remember_missing_accessions(accessions: Iterable[Any]) -> int:
    """Record accessions NCBI returned no record for (withdrawn, suppressed).

    Written as a negative answer marked ``missing`` so the backfill does not
    put them at the front of every run; it retries them only once the answer
    is old (see ``scripts/dikarya_refresh_type_specimens.py``).
    """
    entries = [{"accession": acc, "status": None, "missing": True}
               for acc in sorted({accession_root(a) for a in accessions} - {""})]
    if entries:
        _append_entries(entries)
    return len(entries)


def remember_genbank_records(records: Iterable[Dict[str, Any]], include_negatives: bool = False) -> int:
    """Record the type status of parsed GenBank records; returns lines written.

    Called from ``_parse_genbank_xml()`` for every GenBank fetch the app makes,
    so it must never raise and must stay cheap: known answers are skipped, and
    only type-bearing records are written unless ``include_negatives`` (the
    backfill, which needs to remember what it already asked NCBI about).
    """
    try:
        known = genbank_index()
        new_entries = []
        for record in records:
            if not isinstance(record, dict):
                continue
            acc = accession_root(record.get("accession"))
            if not acc:
                continue
            entry = genbank_type_entry(record)
            previous = known.get(acc)
            if entry is None:
                if include_negatives and (previous is None or previous.get("missing")):
                    new_entries.append({"accession": acc, "status": None})
                continue
            if previous and all(previous.get(k) == entry.get(k) for k in ("status", "type_material")):
                continue
            new_entries.append(entry)
        if new_entries:
            _append_entries(new_entries)
        return len(new_entries)
    except Exception as exc:  # display metadata must never break a fetch
        logger.warning("event=type_specimens.record_failed error=%s", exc)
        return 0


def remember_mycomap_curation(rows: Dict[str, Any]) -> int:
    """Record MycoMap.org's ``isType`` answers; returns lines written.

    ``rows`` is ``{ACCESSION: genbank-curation result row}``. A trusted
    positive flag (``TRUSTED_CURATION_BASES``) is written when it is new or its
    name changed, and anything else only when it clears a flag recorded earlier
    (later lines win), so the file holds the designations rather than one line
    per accession ever asked about. Called on every curation lookup, so it must
    never raise.
    """
    try:
        known = curation_index()
        new_entries = []
        for key, row in (rows or {}).items():
            acc = accession_root(key)
            if not acc or not isinstance(row, dict) or not isinstance(row.get("isType"), bool):
                continue
            previous = known.get(acc) or {}
            basis = " ".join(str(row.get("typeBasis") or "").split())
            if row["isType"] and basis in TRUSTED_CURATION_BASES:
                entry = {
                    "accession": acc,
                    "status": "type",
                    "organism": " ".join(str(row.get("scientificName") or "").split()),
                    "type_basis": basis,
                }
                if all(previous.get(k) == entry[k] for k in ("status", "organism", "type_basis")):
                    continue
                new_entries.append(entry)
            elif previous.get("status"):
                new_entries.append({"accession": acc, "status": None})
        if new_entries:
            _append_entries(new_entries, CURATION_CACHE_NAME)
        return len(new_entries)
    except Exception as exc:  # display metadata must never break a fetch
        logger.warning("event=type_specimens.curation_record_failed error=%s", exc)
        return 0


# --------------------------------------------------------------------------
# Per-job resolution
# --------------------------------------------------------------------------

def job_headers(job_dir: Path):
    from app.services.artifact_storage import artifact_exists, open_artifact

    path = Path(job_dir) / "input" / "input_raw.fasta"
    if not artifact_exists(path):
        return
    with open_artifact(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith(">"):
                header = line[1:].strip()
                if header:
                    yield header


def type_specimens_for_job(job_dir: Path, sequence_metadata: Any = None) -> Dict[str, Any]:
    """Type specimens among a job's sequences, shaped for the viewer.

    Returns ``{"records": {ACCESSION: info}, "names": {header: ACCESSION}}``,
    listing only the sequences that are types. ``names`` is keyed by the exact
    input header, which is what a tip is labelled with; a tip whose label is
    not in it falls back to ``record_accession()`` on the label, and
    ``records`` only ever holds accessions this job's own headers resolved to.
    """
    from app.services.sequence_dedup_service import record_accession

    metadata_by_name: Dict[str, Dict[str, Any]] = {}
    for item in sequence_metadata or []:
        if not isinstance(item, dict):
            continue
        for key in ("fasta_header", "name", "display_label"):
            name = str(item.get(key) or "").strip()
            if name:
                metadata_by_name.setdefault(name, item)

    records: Dict[str, Dict[str, Any]] = {}
    names: Dict[str, str] = {}
    try:
        headers = list(job_headers(job_dir))
    except OSError as exc:
        logger.warning("event=type_specimens.headers_unreadable job_dir=%s error=%s", job_dir, exc)
        return {"records": {}, "names": {}}

    genbank_types = genbank_index()
    mycomap_types = mycomap_index()
    curation_types = curation_index()
    for header in headers:
        metadata = metadata_by_name.get(header) or {}
        acc = accession_root(record_accession(header, metadata))
        if not acc:
            continue
        info = records.get(acc) or lookup(acc, genbank_types, mycomap_types, curation_types)
        if info is None and FROM_TYPE_MATERIAL_RE.search(str(metadata.get("raw_ncbi_description") or "")):
            # MycoMap BLAST imports keep NCBI's DEFINITION line, which carries
            # RefSeq's marker even for a job whose accession was never fetched.
            info = {"accession": acc, "status": "type", "type_material": "", "organism":
                    str(metadata.get("organism") or ""), "voucher": str(metadata.get("voucher") or ""),
                    "sources": ["genbank_definition"]}
        if info is None:
            continue
        records[acc] = info
        names[header] = acc
    return {"records": records, "names": names}


def resolve_tip(name: Any, job_types: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Type info for one tree tip label, using a ``type_specimens_for_job`` result.

    Mirrors ``_typeSpecimenForName()`` in tree_viewer_phylotree_v2.js. The
    fallback for a label that is not an input header goes through
    ``record_accession()`` exactly as the headers did, so a bare Mushroom
    Observer ``MO123456`` label can never borrow the type status of a GenBank
    record ``MO123456.1`` in the same job.
    """
    from app.services.sequence_dedup_service import record_accession

    label = _strip_reversed_prefix(str(name or "").strip())
    if not label:
        return None
    records = job_types.get("records") or {}
    acc = (job_types.get("names") or {}).get(label)
    if not acc:
        acc = accession_root(record_accession(label, {}))
    return records.get(acc) if acc else None


def append_type_status(label: str, info: Optional[Dict[str, Any]]) -> str:
    """``label`` with `` (holotype)`` appended, unless it already says so.

    Mirrors ``appendTypeStatusToLabel()`` in tree_viewer_phylotree_v2.js.
    """
    status = str((info or {}).get("status") or "").strip()
    if not status:
        return label
    base = status[3:] if status.startswith("ex-") else status
    if re.search(r"\b" + re.escape(base) + r"\b", label, re.IGNORECASE):
        return label  # e.g. a BLAST header that already ends in "holotype"
    return f"{label} ({status})"


def type_labeled_name(name: Any, job_types: Dict[str, Any]) -> Optional[str]:
    """A tip name with its type status appended, or None if it does not change."""
    info = resolve_tip(name, job_types)
    if not info:
        return None
    labelled = append_type_status(str(name), info)
    return labelled if labelled != name else None


def label_tree_with_type_status(tree, job_types: Dict[str, Any]) -> int:
    """Append type status to a Biopython tree's tip names in place; returns count."""
    changed = 0
    for tip in tree.get_terminals():
        labelled = type_labeled_name(tip.name, job_types)
        if labelled is not None:
            tip.name = labelled
            changed += 1
    return changed


def job_sequence_metadata(job_dir: Path):
    from app.services.artifact_storage import artifact_exists, open_artifact

    path = Path(job_dir) / "input_info.json"
    if not artifact_exists(path):
        return []
    try:
        with open_artifact(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    metadata = payload.get("sequence_metadata") if isinstance(payload, dict) else None
    return metadata if isinstance(metadata, list) else []


def type_labeled_tree_text(job_dir: Path, newick_path: Path, fmt: str = "newick") -> Optional[str]:
    """A job's tree as Newick or NEXUS with type status appended to tip labels.

    Returns None when nothing would change (no type tips) or the tree cannot be
    rewritten, so the caller serves the ordinary file instead. Newick is edited
    as text -- only the type tips' labels change -- because the Original Newick
    download promises the builder's own file, and a Biopython round trip rounds
    every support value to two decimals. NEXUS is rebuilt through the same
    tree_io renderer as the plain NEXUS download. Nothing is written back to
    the job directory.
    """
    from Bio import Phylo

    from app.services.tree_io import relabel_newick_text, tree_to_nexus_text

    job_types = type_specimens_for_job(job_dir, job_sequence_metadata(job_dir))
    if not job_types["records"]:
        return None
    try:
        if fmt != "nexus":
            original = Path(newick_path).read_text(encoding="utf-8")
            labelled = relabel_newick_text(original, lambda name: type_labeled_name(name, job_types))
            if labelled is None:
                logger.warning("event=type_specimens.relabel_unmatched path=%s", newick_path)
            return labelled if labelled != original else None
        tree = Phylo.read(str(newick_path), "newick")
        if not label_tree_with_type_status(tree, job_types):
            return None
        return tree_to_nexus_text(tree)
    except Exception as exc:
        logger.warning(
            "event=type_specimens.label_download_failed path=%s format=%s error=%s",
            newick_path, fmt, exc,
        )
        return None
