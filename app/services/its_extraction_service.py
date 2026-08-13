"""Extract a single ITS subregion (ITS1, 5.8S, or ITS2) before alignment.

Aligning whole ITS amplicons mixes sequences that cover different parts of the
locus: a 300 bp ITS1-only read and a 700 bp full-ITS read share only a fraction
of their columns, so most pairs in the alignment are compared over a small and
inconsistent overlap. Restricting the job to one subregion puts every remaining
sequence on the same homologous stretch.

The cost is that sequences without enough of the target subregion have to be
dropped, so this module reports exactly what it removed and why.

Region detection uses pyitsx (an in-process ITSx via pyhmmer) against the ITSx
HMM profiles. Both are optional: if either is missing the caller gets a clear
error rather than a silently unfiltered job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.services.trimming_service import _read_fasta_records

# Region keys accepted from the API/UI. "none" means "do not extract".
REGION_NONE = "none"
REGION_ITS1 = "its1"
REGION_58S = "s58"
REGION_ITS2 = "its2"
REGION_FULL = "full_its"

VALID_REGIONS = (REGION_NONE, REGION_ITS1, REGION_58S, REGION_ITS2, REGION_FULL)

REGION_LABELS = {
    REGION_ITS1: "ITS1",
    REGION_58S: "5.8S",
    REGION_ITS2: "ITS2",
    REGION_FULL: "full ITS",
}

# Minimum extracted length, per region, below which a sequence is dropped.
# Observed medians on the Dikarya corpus are ITS1 ~204 bp, 5.8S ~158 bp,
# ITS2 ~186 bp, full ITS ~570 bp; these floors keep roughly half-length
# fragments while discarding stubs that would only add gap columns.
DEFAULT_MIN_LENGTH = {
    REGION_ITS1: 100,
    REGION_58S: 100,
    REGION_ITS2: 100,
    REGION_FULL: 300,
}

# Reasons a sequence can be removed, in the order they are checked.
REASON_NOT_DETECTED = "not_detected"
REASON_TOO_SHORT = "too_short"

REASON_LABELS = {
    REASON_NOT_DETECTED: "Region not detected",
    REASON_TOO_SHORT: "Below minimum length",
}


class ItsExtractionError(RuntimeError):
    """Raised when extraction cannot run at all (missing dependency or profiles)."""


@dataclass
class ItsExtractionStats:
    region: str
    region_label: str
    min_length: int
    input_count: int = 0
    kept_count: int = 0
    dropped_records: List[Dict[str, Any]] = field(default_factory=list)
    kept_lengths: List[int] = field(default_factory=list)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_records)

    def to_dict(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for record in self.dropped_records:
            reason = record.get("reason") or REASON_NOT_DETECTED
            counts[reason] = counts.get(reason, 0) + 1
        lengths = sorted(self.kept_lengths)
        return {
            "region": self.region,
            "region_label": self.region_label,
            "min_length": self.min_length,
            "input_count": self.input_count,
            "kept_count": self.kept_count,
            "dropped_count": self.dropped_count,
            "counts": counts,
            "dropped_records": self.dropped_records,
            "kept_length_min": lengths[0] if lengths else None,
            "kept_length_max": lengths[-1] if lengths else None,
            "kept_length_median": lengths[len(lengths) // 2] if lengths else None,
        }


def normalize_region(value: Any) -> str:
    """Coerce a user-supplied region to a known key, defaulting to no extraction."""
    key = str(value or REGION_NONE).strip().lower().replace("-", "_").replace(".", "")
    aliases = {
        "": REGION_NONE,
        "off": REGION_NONE,
        "full": REGION_FULL,
        "fullits": REGION_FULL,
        "full_its": REGION_FULL,
        "58s": REGION_58S,
        "s58": REGION_58S,
        "5_8s": REGION_58S,
    }
    key = aliases.get(key, key)
    return key if key in VALID_REGIONS else REGION_NONE


def default_min_length(region: str) -> int:
    return DEFAULT_MIN_LENGTH.get(region, 100)


def resolve_min_length(region: str, value: Any) -> int:
    """Clamp a user-supplied minimum length, falling back to the region default."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default_min_length(region)
    if parsed <= 0:
        return default_min_length(region)
    return max(1, min(parsed, 5000))


def describe_its_step(region: str) -> Tuple[bool, str]:
    """Return (should_run, step_label) so the worker and UI agree on wording."""
    region = normalize_region(region)
    if region == REGION_NONE:
        return False, "ITS Region Extraction (skipped)"
    return True, f"ITS Region Extraction ({REGION_LABELS[region]})"


def _load_profile_db(config):
    """Build a pyitsx ProfileDB, raising a caller-friendly error when unavailable."""
    try:
        import pyitsx  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on deployment
        raise ItsExtractionError(
            "ITS region extraction requires the 'pyitsx' package. "
            "Install it with: pip install pyitsx"
        ) from exc

    import pyitsx

    hmm_dir = getattr(config, "ITSX_HMM_DIR", None)
    hmm_path = Path(hmm_dir) if hmm_dir else None
    if hmm_path is not None and not hmm_path.is_dir():
        raise ItsExtractionError(
            f"ITSx HMM profile directory not found at {hmm_path}. "
            "Set ITSX_HMM_DIR to the ITSx 'HMMs' directory."
        )
    try:
        return pyitsx.ProfileDB(hmm_dir=hmm_path, organism="F")
    except Exception as exc:  # pragma: no cover - depends on deployment
        raise ItsExtractionError(f"Could not load ITSx HMM profiles: {exc}") from exc


def _pyitsx_region(region: str):
    import pyitsx

    return {
        REGION_ITS1: pyitsx.Region.ITS1,
        REGION_58S: pyitsx.Region.S58,
        REGION_ITS2: pyitsx.Region.ITS2,
        REGION_FULL: pyitsx.Region.FULL_ITS,
    }[region]


# pyhmmer rejects anything outside the DNA alphabet, and submitted FASTA can
# arrive pre-aligned or carrying stray characters.
_ALLOWED_BASES = set("ACGTRYSWKMBDHVN")


def _clean_for_hmm(sequence: str) -> str:
    upper = (sequence or "").upper()
    return "".join(ch for ch in upper if ch in _ALLOWED_BASES)


def run_its_extraction(
    input_fasta: Path,
    output_fasta: Path,
    region: str,
    config,
    logger,
    min_length: Optional[int] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Rewrite `input_fasta` to `output_fasta` keeping only the target region.

    Sequences whose target region is not detected, or is shorter than
    `min_length`, are dropped and listed in the returned stats.

    Raises ItsExtractionError if extraction cannot run, or if it would leave
    too few sequences to build a tree.
    """
    region = normalize_region(region)
    if region == REGION_NONE:
        raise ValueError("run_its_extraction called with region='none'")

    min_len = resolve_min_length(region, min_length)
    label = REGION_LABELS[region]
    stats = ItsExtractionStats(region=region, region_label=label, min_length=min_len)

    records = _read_fasta_records(input_fasta)
    stats.input_count = len(records)
    if not records:
        raise ItsExtractionError("No sequences available for ITS region extraction.")

    db = _load_profile_db(config)
    import pyitsx

    # pyitsx keys results by sequence id, so feed it unique synthetic ids and
    # map back afterwards; submitted headers are not guaranteed unique.
    keyed: List[Tuple[str, str]] = []
    for index, (_name, sequence) in enumerate(records):
        cleaned = _clean_for_hmm(sequence)
        if cleaned:
            keyed.append((f"seq{index}", cleaned))

    if job_id:
        from app.workers.events import publish_log
        publish_log(job_id, "its", "stderr", f"Scanning {len(keyed)} sequence(s) for {label}...")

    logger.info(
        "ITS extraction: region=%s min_length=%s input=%s",
        region, min_len, stats.input_count,
    )

    try:
        results = pyitsx.extract(keyed, db, regions=[_pyitsx_region(region)])
    except Exception as exc:
        raise ItsExtractionError(f"ITS region extraction failed: {exc}") from exc

    extracted: Dict[str, str] = {}
    for result in results:
        if result.sequence:
            extracted[str(result.seq_id)] = str(result.sequence)

    kept_lines: List[str] = []
    for index, (record_name, sequence) in enumerate(records):
        key = f"seq{index}"
        name = record_name or f"sequence_{index + 1}"
        original_length = len(_clean_for_hmm(sequence))
        sub = extracted.get(key)
        if not sub:
            stats.dropped_records.append({
                "name": name,
                "reason": REASON_NOT_DETECTED,
                "reason_label": REASON_LABELS[REASON_NOT_DETECTED],
                "original_length": original_length,
                "extracted_length": 0,
            })
            continue
        if len(sub) < min_len:
            stats.dropped_records.append({
                "name": name,
                "reason": REASON_TOO_SHORT,
                "reason_label": REASON_LABELS[REASON_TOO_SHORT],
                "original_length": original_length,
                "extracted_length": len(sub),
            })
            continue
        stats.kept_count += 1
        stats.kept_lengths.append(len(sub))
        kept_lines.append(f">{name}")
        for start in range(0, len(sub), 80):
            kept_lines.append(sub[start:start + 80])

    if stats.kept_count < 4:
        raise ItsExtractionError(
            f"Only {stats.kept_count} sequence(s) retained {label} of at least "
            f"{min_len} bp - not enough to build a tree. Lower the minimum length, "
            f"choose a different region, or turn off ITS region extraction."
        )

    output_fasta.parent.mkdir(parents=True, exist_ok=True)
    output_fasta.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")

    logger.info(
        "ITS extraction: region=%s kept=%s dropped=%s (not_detected=%s too_short=%s)",
        region,
        stats.kept_count,
        stats.dropped_count,
        sum(1 for r in stats.dropped_records if r["reason"] == REASON_NOT_DETECTED),
        sum(1 for r in stats.dropped_records if r["reason"] == REASON_TOO_SHORT),
    )
    return stats.to_dict()


def format_its_detail(stats: Optional[Dict[str, Any]]) -> str:
    """One-line summary for the pipeline step feed."""
    if not stats:
        return "ITS region extraction skipped"
    kept = stats.get("kept_count", 0)
    dropped = stats.get("dropped_count", 0)
    label = stats.get("region_label", "ITS")
    detail = f"{kept} sequence(s) kept for {label}"
    if dropped:
        detail += f", {dropped} dropped"
    return detail
