"""Claude review of a finished alignment + tree.

The model is not handed the raw alignment. Counting gaps, tallying support
values and spotting long branches across a few thousand sequences is work a
language model does slowly and unreliably, and a 2 MB FASTA would dominate the
context window without adding anything a summary does not. Everything numeric
is computed here, deterministically, and only the resulting summary goes to the
API. The model's job is the part that actually needs judgement: reading those
numbers together and telling the user whether the tree is worth trusting.

Two consequences worth knowing about:

* The metrics are the cache key. A review is stored under the fingerprint of the
  numbers it was based on, so re-opening the viewer is free and a re-run only
  happens once the tree or alignment actually changes.
* Every claim in the review can be traced back to a number in `metrics`, which
  is returned alongside the prose so the viewer can show both.

Requires ANTHROPIC_API_KEY. Without it `is_configured()` is False, the viewer
hides the button, and the endpoint answers 503 rather than failing mid-request.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import Config
from app.services.artifact_storage import artifact_exists, open_artifact

logger = logging.getLogger(__name__)

# Bump when the prompt or the metric set changes in a way that would make a
# stored review misleading. Cached reviews with a different version are ignored.
REVIEW_SCHEMA_VERSION = 1

CACHE_RELATIVE_PATH = Path("analysis") / "claude_review.json"

GAP_CHARS = frozenset("-.~?")
UNAMBIGUOUS = frozenset("ACGT")

# Column sampling kicks in only for alignments big enough that a full pass would
# be felt inside a request. 12M cells is roughly 3000 sequences x 4000 columns.
MAX_ALIGNMENT_CELLS = 12_000_000
MAX_PAIRWISE_COMPARISONS = 400
# How many worst-offender rows to name. Long enough to see a pattern, short
# enough that the prompt stays compact on a 2000-tip tree.
TOP_N = 12


class TreeAnalysisError(Exception):
    """A review could not be produced for a reason worth showing the user."""


class TreeAnalysisUnavailable(TreeAnalysisError):
    """The feature is switched off or temporarily out of capacity (503)."""


def _backend() -> str:
    return str(getattr(Config, "CLAUDE_REVIEW_BACKEND", "cli") or "cli").strip().lower()


def is_configured() -> bool:
    """True when the selected backend can actually run, so the UI can hide the
    button otherwise rather than offering one that can only answer 503."""
    if _backend() == "api":
        return bool(getattr(Config, "ANTHROPIC_API_KEY", ""))
    # The sudo wrapper is root-owned and installed out of band; until it exists
    # there is no route from the web process to the `tree` account's credentials.
    return os.access(str(Config.CLAUDE_REVIEW_WRAPPER), os.X_OK)


# =============================================================================
# Alignment metrics
# =============================================================================

def _read_alignment(path: Path) -> List[Tuple[str, str]]:
    """Return [(header, sequence)] from a possibly-gzipped aligned FASTA."""
    from Bio import SeqIO

    with open_artifact(path, "rt") as handle:
        return [
            ((record.description or record.id), str(record.seq).upper())
            for record in SeqIO.parse(handle, "fasta")
        ]


def _percent(part: float, whole: float) -> Optional[float]:
    if not whole:
        return None
    return round(100.0 * part / whole, 2)


def _quantiles(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Min / quartiles / max, rounded. Returns nulls for an empty input."""
    if not values:
        return {"min": None, "q1": None, "median": None, "q3": None, "max": None}
    ordered = sorted(values)

    def at(fraction: float) -> float:
        # Nearest-rank rather than interpolated: these feed a prose summary, and
        # a real observed value is easier to reconcile with the tree than an
        # interpolated one that no branch actually has.
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    return {
        "min": round(ordered[0], 6),
        "q1": round(at(0.25), 6),
        "median": round(at(0.5), 6),
        "q3": round(at(0.75), 6),
        "max": round(ordered[-1], 6),
    }


def _column_indices(n_columns: int, n_sequences: int) -> Tuple[List[int], bool]:
    """Pick which columns to score, sampling evenly only when the matrix is huge.

    An evenly spaced sample keeps the gappy ends and the conserved core in
    proportion, which a random sample of the same size would not guarantee.
    """
    if n_columns == 0 or n_sequences == 0:
        return [], False
    if n_columns * n_sequences <= MAX_ALIGNMENT_CELLS:
        return list(range(n_columns)), False
    budget = max(1, MAX_ALIGNMENT_CELLS // n_sequences)
    step = max(1, n_columns // budget)
    return list(range(0, n_columns, step)), True


def summarize_alignment(records: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Compute the alignment half of the review context.

    Reports occupancy, variability and per-sequence outliers. Sequences of
    unequal length mean the file is not actually aligned, which is itself worth
    reporting rather than crashing on.
    """
    if not records:
        raise TreeAnalysisError("The alignment file is empty.")

    lengths = {len(seq) for _, seq in records}
    ragged = len(lengths) > 1
    n_columns = max(lengths)
    n_sequences = len(records)

    indices, sampled = _column_indices(n_columns, n_sequences)

    occupancy_fractions: List[float] = []
    variable = 0
    parsimony_informative = 0
    invariant = 0
    all_gap_columns = 0

    for index in indices:
        counts: Dict[str, int] = {}
        gaps = 0
        ambiguous = 0
        for _, seq in records:
            char = seq[index] if index < len(seq) else "-"
            if char in GAP_CHARS:
                gaps += 1
            elif char in UNAMBIGUOUS:
                counts[char] = counts.get(char, 0) + 1
            else:
                ambiguous += 1
        occupied = n_sequences - gaps
        occupancy_fractions.append(occupied / n_sequences)
        if occupied == 0:
            all_gap_columns += 1
            continue
        distinct = len(counts)
        if distinct <= 1:
            invariant += 1
        else:
            variable += 1
            # Parsimony-informative in the standard sense: at least two states
            # each seen at least twice, so the column can distinguish topologies.
            if sum(1 for count in counts.values() if count >= 2) >= 2:
                parsimony_informative += 1

    scored = len(indices)
    total_cells = 0
    total_gaps = 0
    per_sequence: List[Dict[str, Any]] = []
    seen_sequences: Dict[str, List[str]] = {}

    for header, seq in records:
        gaps = sum(1 for char in seq if char in GAP_CHARS)
        ambiguous = sum(
            1 for char in seq if char not in GAP_CHARS and char not in UNAMBIGUOUS
        )
        ungapped = len(seq) - gaps
        total_cells += len(seq)
        total_gaps += gaps
        per_sequence.append({
            "name": header,
            "ungapped_length": ungapped,
            "gap_percent": _percent(gaps, len(seq)) or 0.0,
            "ambiguity_percent": _percent(ambiguous, ungapped) or 0.0,
        })
        # Identical rows are indistinguishable to the tree builder and turn up
        # as zero-length sister pairs; naming them saves the user a hunt.
        seen_sequences.setdefault(seq.replace("-", ""), []).append(header)

    duplicate_groups = [
        # Names are a sample, not the whole group: `count` is what matters, and
        # a species complex can put 30+ identical ITS reads in one group.
        {"count": len(names), "names": names[:6]}
        for names in seen_sequences.values()
        if len(names) > 1
    ]
    duplicate_groups.sort(key=lambda group: group["count"], reverse=True)

    gappiest = sorted(per_sequence, key=lambda row: row["gap_percent"], reverse=True)
    ambiguous_worst = sorted(
        per_sequence, key=lambda row: row["ambiguity_percent"], reverse=True
    )
    shortest = sorted(per_sequence, key=lambda row: row["ungapped_length"])

    ungapped_lengths = [row["ungapped_length"] for row in per_sequence]

    return {
        "sequences": n_sequences,
        "columns": n_columns,
        "ragged": ragged,
        "columns_scored": scored,
        "column_sampling_applied": sampled,
        "overall_gap_percent": _percent(total_gaps, total_cells) or 0.0,
        "mean_column_occupancy_percent": (
            round(100.0 * sum(occupancy_fractions) / len(occupancy_fractions), 2)
            if occupancy_fractions else None
        ),
        "columns_below_50_percent_occupancy": sum(
            1 for fraction in occupancy_fractions if fraction < 0.5
        ),
        "all_gap_columns": all_gap_columns,
        "invariant_column_percent": _percent(invariant, scored),
        "variable_column_percent": _percent(variable, scored),
        "parsimony_informative_columns": parsimony_informative,
        "parsimony_informative_percent": _percent(parsimony_informative, scored),
        "ungapped_length": _quantiles(ungapped_lengths),
        "mean_pairwise_identity_percent": _mean_pairwise_identity(records),
        "identical_sequence_groups": duplicate_groups[:TOP_N],
        "gappiest_sequences": [row for row in gappiest[:TOP_N] if row["gap_percent"] > 0],
        "most_ambiguous_sequences": [
            row for row in ambiguous_worst[:TOP_N] if row["ambiguity_percent"] > 0
        ],
        "shortest_sequences": shortest[:TOP_N],
    }


def _mean_pairwise_identity(records: List[Tuple[str, str]]) -> Optional[float]:
    """Mean identity over columns where both members of a pair have a base.

    Sampled rather than exhaustive: a full matrix is O(n^2) and the mean is
    stable well before that. Pairs are drawn with a fixed seed so a re-run
    produces the same number and does not invalidate the cached review.
    """
    if len(records) < 2:
        return None

    pairs: List[Tuple[int, int]] = []
    total_pairs = len(records) * (len(records) - 1) // 2
    if total_pairs <= MAX_PAIRWISE_COMPARISONS:
        pairs = [
            (i, j)
            for i in range(len(records))
            for j in range(i + 1, len(records))
        ]
    else:
        rng = random.Random(0)
        seen = set()
        while len(pairs) < MAX_PAIRWISE_COMPARISONS:
            i = rng.randrange(len(records))
            j = rng.randrange(len(records))
            if i == j:
                continue
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)

    identities: List[float] = []
    for i, j in pairs:
        left = records[i][1]
        right = records[j][1]
        compared = 0
        matches = 0
        for a, b in zip(left, right):
            if a in GAP_CHARS or b in GAP_CHARS:
                continue
            compared += 1
            if a == b:
                matches += 1
        if compared:
            identities.append(matches / compared)

    if not identities:
        return None
    return round(100.0 * sum(identities) / len(identities), 2)


# =============================================================================
# Tree metrics
# =============================================================================

_DUAL_SUPPORT_RE = re.compile(r"^(?:Node_\d+_)?(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)$")
_SINGLE_SUPPORT_RE = re.compile(r"^(?:Node_\d+_)?(\d+(?:\.\d+)?)$")


def _clade_support(clade) -> Tuple[Optional[float], Optional[Tuple[float, float]]]:
    """Return (thresholding value, dual SH-aLRT/UFBoot pair) for an internal node.

    Mirrors the viewer's extraction in tree_viewer_phylotree_v2.js, including
    resolving an IQ-TREE "82.7/87" dual label to its UFBoot half, so the review
    and the on-screen support badge never disagree about a node.
    """
    for raw in (clade.confidence, clade.name):
        if raw is None or raw == "":
            continue
        text = str(raw).strip()
        dual = _DUAL_SUPPORT_RE.match(text)
        if dual:
            return float(dual.group(2)), (float(dual.group(1)), float(dual.group(2)))
        single = _SINGLE_SUPPORT_RE.match(text)
        if single:
            return float(single.group(1)), None
    return None, None


def _classify_support(values: List[float], has_dual: bool, tree_method: str) -> str:
    """Name the support scale, using the same rules as the viewer's badge."""
    if has_dual:
        return "ALRT_UFBOOT"
    if not values:
        return "none"
    if tree_method == "fasttree":
        # FastTree emits SH-like local supports on 0-1. They look like posterior
        # probabilities and are routinely misread as such, so never call them PP.
        return "SH"
    if any(value > 1.0 for value in values):
        return "mixed" if any(0 < value < 1.0 for value in values) else "BS"
    return "PP"


SUPPORT_SCALE_NOTES = {
    "BS": "Bootstrap percentages (0-100). >=70 is conventionally moderate, >=95 strong.",
    "PP": "Bayesian posterior probabilities (0-1). >=0.95 is conventionally strong.",
    "SH": (
        "FastTree SH-like local support (0-1). NOT bootstrap and NOT posterior "
        "probability: it compares each node only against its two nearest-neighbour "
        "interchanges, is known to be anti-conservative, and cannot detect "
        "long-branch attraction. High values on long branches deserve scepticism."
    ),
    "ALRT_UFBOOT": (
        "IQ-TREE dual support written SH-aLRT/UFBoot, both percentages (0-100). "
        "A clade is normally called well supported at SH-aLRT >= 80 AND UFBoot >= 95."
    ),
    "mixed": "Mixed support scales in one tree; compare values across nodes with care.",
    "none": "This tree carries no node support values.",
}


def summarize_tree(
    newick_path: Path, tree_method: str
) -> Tuple[Dict[str, Any], List[str]]:
    """Compute the tree half of the review context from a Newick file.

    Returns the summary and the full list of tip names. The names are kept out
    of the summary itself because a 2000-tip roster would swamp the prompt; the
    caller uses them to line the alignment up with the tree.
    """
    from Bio import Phylo

    with open(newick_path, "r") as handle:
        tree = Phylo.read(handle, "newick")

    terminals = tree.get_terminals()
    internals = tree.get_nonterminals()
    if not terminals:
        raise TreeAnalysisError("The tree file contains no tips.")

    terminal_lengths: List[float] = []
    internal_lengths: List[float] = []
    tip_rows: List[Dict[str, Any]] = []

    for clade in terminals:
        length = float(clade.branch_length or 0.0)
        terminal_lengths.append(length)
        tip_rows.append({"name": str(clade.name or ""), "branch_length": round(length, 6)})
    for clade in internals:
        if clade.branch_length is not None:
            internal_lengths.append(float(clade.branch_length))

    support_values: List[float] = []
    dual_pairs: List[Tuple[float, float]] = []
    unsupported_internals = 0
    polytomies = 0

    for clade in internals:
        if len(clade.clades) > 2:
            polytomies += 1
        if clade is tree.root:
            # The root has no support in an unrooted-then-rooted tree; counting it
            # as unsupported would understate the tree.
            continue
        value, dual = _clade_support(clade)
        if value is None:
            unsupported_internals += 1
            continue
        support_values.append(value)
        if dual:
            dual_pairs.append(dual)

    support_type = _classify_support(support_values, bool(dual_pairs), tree_method)

    # Express the "well supported" fraction on whatever scale this tree uses, so
    # the number means the same thing a mycologist means by it.
    if support_type in ("BS", "ALRT_UFBOOT", "mixed"):
        strong_threshold, moderate_threshold = 95.0, 70.0
    else:
        strong_threshold, moderate_threshold = 0.95, 0.70

    scored = len(support_values)
    tip_rows.sort(key=lambda row: row["branch_length"], reverse=True)
    total_length = sum(terminal_lengths) + sum(internal_lengths)

    # A tip is called an outlier when it is far outside the bulk of terminal
    # branches: usually a misaligned read, an off-target amplicon or a genuine
    # distant relative, all of which the user wants to look at.
    outlier_cut = None
    outliers: List[Dict[str, Any]] = []
    if len(terminal_lengths) >= 4:
        quantiles = _quantiles(terminal_lengths)
        q1, q3 = quantiles["q1"], quantiles["q3"]
        if q1 is not None and q3 is not None:
            outlier_cut = q3 + 3.0 * (q3 - q1)
            if outlier_cut > 0:
                outliers = [
                    row for row in tip_rows if row["branch_length"] > outlier_cut
                ][:TOP_N]

    dual_summary = None
    if dual_pairs:
        dual_summary = {
            "nodes_meeting_both_thresholds": sum(
                1 for alrt, ufboot in dual_pairs if alrt >= 80.0 and ufboot >= 95.0
            ),
            "nodes_scored": len(dual_pairs),
        }

    summary = {
        "tips": len(terminals),
        "internal_nodes": len(internals),
        "total_branch_length": round(total_length, 6),
        "polytomies": polytomies,
        "zero_length_terminal_branches": sum(
            1 for length in terminal_lengths if length <= 0.0
        ),
        "terminal_branch_length": _quantiles(terminal_lengths),
        "internal_branch_length": _quantiles(internal_lengths),
        "support_type": support_type,
        "support_scale_note": SUPPORT_SCALE_NOTES[support_type],
        "support_nodes_scored": scored,
        "internal_nodes_without_support": unsupported_internals,
        "support_distribution": _quantiles(support_values),
        "strong_support_threshold": strong_threshold,
        "moderate_support_threshold": moderate_threshold,
        "strongly_supported_percent": _percent(
            sum(1 for value in support_values if value >= strong_threshold), scored
        ),
        "moderately_supported_percent": _percent(
            sum(1 for value in support_values if value >= moderate_threshold), scored
        ),
        "dual_support_summary": dual_summary,
        "longest_terminal_branches": tip_rows[:TOP_N],
        "outlier_branch_threshold": (
            round(outlier_cut, 6) if outlier_cut is not None else None
        ),
        "outlier_long_branch_tips": outliers,
    }
    return summary, [str(clade.name or "") for clade in terminals]


# =============================================================================
# Context assembly
# =============================================================================

def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        logger.warning("Could not read %s for Claude review", path.name)
        return {}


def _alignment_paths(job_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """Return (untrimmed, tree-builder input) alignments, either possibly absent."""
    alignment_dir = job_dir / "alignment"
    raw = next(
        (
            candidate
            for candidate in (
                alignment_dir / "alignment_raw.fasta",
                alignment_dir / "aligned.fasta",
            )
            if artifact_exists(candidate)
        ),
        None,
    )
    trimmed = next(
        (
            candidate
            for candidate in (
                alignment_dir / "alignment_trimmed.fasta",
                alignment_dir / "alignment_pruned_aligned.fasta",
            )
            if artifact_exists(candidate)
        ),
        None,
    )
    return raw, trimmed


def _normalize_name(name: str) -> str:
    """Loose key for matching a Newick tip label to a FASTA header."""
    return re.sub(r"\s+", " ", str(name).replace("_", " ")).strip().lower()


def _restrict_to_tree(
    records: List[Tuple[str, str]], tip_names: Sequence[str]
) -> Tuple[List[Tuple[str, str]], int]:
    """Drop alignment rows for sequences the user has pruned from the viewer.

    tree_pruned.newick is regenerated on every prune but the alignment on disk
    is not, so without this the review would report gap and identity figures for
    sequences that are no longer in the tree being reviewed. Returns the kept
    rows and how many were dropped; an empty intersection means the two files do
    not share a naming scheme, in which case the unfiltered set is the honest
    answer.
    """
    if not tip_names:
        return records, 0
    wanted = {_normalize_name(name) for name in tip_names if name}
    kept = [row for row in records if _normalize_name(row[0]) in wanted]
    if not kept:
        return records, 0
    return kept, len(records) - len(kept)


def _active_display_renames(job_dir: Path, tip_names: Sequence[str]) -> Dict[str, str]:
    """Viewer renames that apply to tips currently in the tree.

    The Newick files keep the original labels, so without this the review would
    name sequences the user can no longer find in their own viewer.
    """
    try:
        from app.services.tree_edit_service import load_tree_state

        renames = load_tree_state(job_dir).get("renames") or {}
    except Exception:
        return {}
    if not isinstance(renames, dict):
        return {}
    present = {str(name) for name in tip_names}
    active = {
        str(original): str(display)
        for original, display in renames.items()
        if str(original) in present and str(display).strip()
    }
    # A tree where every tip was renamed would otherwise double the prompt.
    return dict(list(active.items())[:60])


def build_context(job_dir: Path) -> Dict[str, Any]:
    """Assemble every number the review is based on.

    The tree is summarized first so the alignment can be restricted to the tips
    that are actually in it. The alignment reported is the one the tree builder
    consumed; the untrimmed alignment is measured too, but only for its column
    count, so the review can say how much trimming removed.
    """
    job_details = _load_json(job_dir / "input_info.json")
    tree_metadata = _load_json(job_dir / "tree" / "tree_metadata.json")

    from app.services.tree_edit_service import _editable_tree_input_path

    newick_path = _editable_tree_input_path(job_dir, what="review")
    tree_method = str(
        tree_metadata.get("method") or job_details.get("tree_method") or ""
    ).lower()
    tree, all_tip_names = summarize_tree(newick_path, tree_method)
    tree["source_file"] = newick_path.name
    tree["reflects_viewer_pruning"] = newick_path.name == "tree_pruned.newick"

    raw_path, trimmed_path = _alignment_paths(job_dir)
    alignment_path = trimmed_path or raw_path
    if alignment_path is None:
        raise TreeAnalysisError(
            "This job has no aligned FASTA, so there is nothing to review."
        )

    records = _read_alignment(alignment_path)
    records, excluded = _restrict_to_tree(records, all_tip_names)
    alignment = summarize_alignment(records)
    alignment["source_file"] = alignment_path.name
    alignment["is_trimmed_alignment"] = trimmed_path is not None
    alignment["sequences_excluded_by_viewer_pruning"] = excluded

    if raw_path is not None and trimmed_path is not None and raw_path != trimmed_path:
        try:
            raw_records = _read_alignment(raw_path)
            raw_columns = max((len(seq) for _, seq in raw_records), default=0)
            alignment["columns_before_trimming"] = raw_columns
            alignment["columns_removed_by_trimming"] = max(
                0, raw_columns - alignment["columns"]
            )
            alignment["percent_columns_removed_by_trimming"] = _percent(
                raw_columns - alignment["columns"], raw_columns
            )
        except (OSError, ValueError) as exc:
            logger.warning("Untrimmed alignment unreadable for review: %s", exc)

    renames = _active_display_renames(job_dir, all_tip_names)
    if renames:
        tree["viewer_renames_original_to_displayed"] = renames

    trimming_details = job_details.get("trimming_details") or {}
    pipeline = {
        "aligner": job_details.get("aligner") or job_details.get("alignment_method"),
        "trimming_method": (
            trimming_details.get("method")
            or job_details.get("trimming_method")
            or "none"
        ),
        "trimmed_terminal_overhangs": bool(
            (trimming_details.get("terminal_overhang_trim") or {}).get("enabled")
        ),
        "tree_method": tree_metadata.get("method") or job_details.get("tree_method"),
        "substitution_model": tree_metadata.get("model") or job_details.get("tree_model"),
        "bootstrap_replicates": tree_metadata.get("bootstrap")
        or job_details.get("bootstrap"),
        "alrt_replicates": job_details.get("alrt_replicates"),
        "outgroup": job_details.get("outgroup"),
        "blast_enabled": bool(job_details.get("run_blast")),
        "orientation_enabled": bool(job_details.get("run_orient")),
    }

    return {"pipeline": pipeline, "alignment": alignment, "tree": tree}


def fingerprint(context: Dict[str, Any]) -> str:
    """Stable hash of the review inputs, used as the cache key."""
    payload = json.dumps(
        {
            "version": REVIEW_SCHEMA_VERSION,
            "model": Config.CLAUDE_REVIEW_MODEL,
            "effort": Config.CLAUDE_REVIEW_EFFORT,
            "context": context,
        },
        sort_keys=True,
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


# =============================================================================
# Claude call
# =============================================================================

SYSTEM_PROMPT = """\
You are a phylogeneticist reviewing a fungal barcode analysis (usually ITS, LSU \
or TEF1) for a mycologist who is competent with the biology but is not a \
specialist in alignment or model selection. The dataset is typically a mix of \
field collections and GenBank reference sequences.

You are given precomputed statistics for one job: the pipeline settings, the \
alignment the tree builder consumed, and the tree it produced. You are not \
given the sequences themselves, so every claim you make must follow from a \
number you were given. Do not guess at taxonomy, sequence identity, or what a \
named sequence is - the names are user-supplied labels, not verified \
determinations.

Judge the analysis on whether its conclusions can be trusted, not on whether \
it followed a textbook procedure. What matters:

- Is there enough signal? A short or largely invariant alignment cannot resolve \
  a tree no matter how good the settings are. Parsimony-informative column \
  count is the number that decides this.
- Is the alignment sound? Very gappy columns, ragged ends, or a handful of \
  sequences far gappier than the rest usually mean sequences of unequal length \
  or off-target reads rather than real indels.
- Is the support real? Say plainly what the support scale is and what it does \
  and does not establish. Interpret the values on their own scale.
- Are individual sequences suspect? Long terminal branches, high ambiguity, and \
  unusually short sequences are the ones the user should look at, by name.
- Do the settings fit the data? Note a mismatch only when it plausibly changed \
  the result.

Be direct and specific. Cite the actual numbers. A clean dataset should be told \
it is clean in a sentence or two rather than padded with hedges; a broken one \
should be told exactly what is broken and what to do about it. Do not recommend \
a step the pipeline already performed, and do not suggest generic best practices \
that the numbers give no reason to raise.

Where you name a sequence, use its name verbatim so the user can find it. If \
tree.viewer_renames_original_to_displayed is present, that sequence has been \
renamed in the viewer: use the displayed name, because the original no longer \
appears on screen."""


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_rating": {
            "type": "string",
            "enum": ["strong", "usable", "caution", "unreliable"],
            "description": (
                "strong: conclusions are well supported. usable: broadly sound with "
                "specific caveats. caution: real problems that change how the tree "
                "should be read. unreliable: the tree should not be interpreted as is."
            ),
        },
        "headline": {
            "type": "string",
            "description": "One sentence, under 140 characters, stating the verdict.",
        },
        "summary": {
            "type": "string",
            "description": (
                "Two to four short paragraphs in Markdown giving the overall "
                "assessment and the reasoning behind the rating."
            ),
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What this analysis genuinely has going for it. May be empty.",
        },
        "concerns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "title": {"type": "string"},
                    "detail": {
                        "type": "string",
                        "description": "What is wrong, with the numbers that show it.",
                    },
                },
                "required": ["severity", "title", "detail"],
                "additionalProperties": False,
            },
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Concrete next actions, most useful first. Empty if nothing is "
                "worth changing."
            ),
        },
        "sequences_to_inspect": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
            "description": "Named sequences worth a manual look. Empty if none.",
        },
    },
    "required": [
        "overall_rating",
        "headline",
        "summary",
        "strengths",
        "concerns",
        "recommendations",
        "sequences_to_inspect",
    ],
    "additionalProperties": False,
}


@dataclass
class _Slot:
    """Handle for the Redis concurrency counter, released in a finally block."""

    key: str
    client: Any

    def release(self) -> None:
        try:
            self.client.decr(self.key)
        except Exception:  # pragma: no cover - never fail a served response
            logger.warning("Could not release Claude review concurrency slot")


def _acquire_slot() -> Optional[_Slot]:
    """Take one of the global review slots, or raise if they are all in use.

    Flask-Limiter caps a single client; it cannot stop eight different users
    from starting eight reviews and filling every Gunicorn request slot at once.
    Returns None when Redis is unreachable, which degrades to unlimited rather
    than blocking reviews outright.
    """
    limit = max(1, int(Config.CLAUDE_REVIEW_MAX_CONCURRENT))
    try:
        from app.workers.queue import get_redis_connection

        client = get_redis_connection()
        key = "dikarya:claude_review:in_flight"
        current = client.incr(key)
        # The counter is only ever decremented by its own request, so a process
        # killed mid-review would leak a slot forever without this expiry.
        client.expire(key, 900)
        if current > limit:
            client.decr(key)
            raise TreeAnalysisUnavailable(
                "Claude is reviewing other trees right now. Try again in a moment."
            )
        return _Slot(key=key, client=client)
    except TreeAnalysisUnavailable:
        raise
    except Exception as exc:
        logger.warning("Claude review concurrency guard unavailable: %s", exc)
        return None


def _build_user_message(context: Dict[str, Any]) -> str:
    return (
        "Review this phylogenetic analysis.\n\n"
        "```json\n"
        + json.dumps(context, indent=2, sort_keys=True, default=str)
        + "\n```\n"
    )


def _validate_review(review: Any) -> Dict[str, Any]:
    """Reject a reply that does not carry the fields the viewer renders."""
    if not isinstance(review, dict):
        raise TreeAnalysisError("Claude returned a malformed review.")
    missing = [key for key in RESPONSE_SCHEMA["required"] if key not in review]
    if missing:
        raise TreeAnalysisError(
            f"Claude's review was missing {', '.join(missing)}."
        )
    return review


def _call_claude_cli(context: Dict[str, Any]) -> Dict[str, Any]:
    """Run the review through the `tree` account's Claude Code CLI.

    The web process runs as `dikarya` and cannot read `tree`'s Claude Code
    credentials (mode 600), so it goes through a root-owned sudo wrapper that
    pins every CLI flag. Nothing here is passed as an argument: the prompt goes
    on stdin, and the few knobs the wrapper accepts travel as environment
    variables that it validates against its own allowlist.

    Deliberately not routed through subprocess_utils: those helpers exist for
    the bioinformatics tools and apply a memory/CPU limiter that would wrap
    `sudo`, and neither of them can write to a child's stdin.
    """
    import subprocess

    wrapper = str(Config.CLAUDE_REVIEW_WRAPPER)
    # Wall-clock budget: the wrapper's own `timeout` fires first, so this is the
    # backstop for sudo itself wedging before the wrapper ever starts.
    wrapper_timeout = int(Config.CLAUDE_REVIEW_TIMEOUT_SECONDS)
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "DIKARYA_CLAUDE_MODEL": str(Config.CLAUDE_REVIEW_MODEL),
        "DIKARYA_CLAUDE_EFFORT": str(Config.CLAUDE_REVIEW_EFFORT),
        "DIKARYA_CLAUDE_TIMEOUT": str(wrapper_timeout),
        "DIKARYA_CLAUDE_MAX_BUDGET": str(Config.CLAUDE_REVIEW_MAX_BUDGET_USD),
    }

    try:
        completed = subprocess.run(
            # -n so a missing sudoers rule fails immediately instead of blocking
            # on a password prompt that nothing can ever answer.
            ["sudo", "-n", "-u", "tree", wrapper],
            input=_build_user_message(context),
            capture_output=True,
            text=True,
            env=env,
            timeout=wrapper_timeout + 20,
        )
    except FileNotFoundError as exc:
        raise TreeAnalysisUnavailable(
            "Claude review is not installed correctly on this server."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TreeAnalysisError(
            "Claude did not finish the review in time. Try again in a moment."
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:300]
        logger.error(
            "event=claude_review.cli_failed rc=%s detail=%s",
            completed.returncode, detail or "(no stderr)",
        )
        if completed.returncode in (124, 137):  # timeout / SIGKILL from `timeout`
            raise TreeAnalysisError(
                "Claude did not finish the review in time. Try again in a moment."
            )
        if "sudo" in detail.lower() or completed.returncode == 1:
            raise TreeAnalysisUnavailable(
                "Claude review is not configured correctly on this server."
            )
        raise TreeAnalysisError("Claude could not complete the review.")

    try:
        envelope = json.loads(completed.stdout)
    except ValueError as exc:
        raise TreeAnalysisError("Claude returned a malformed review.") from exc

    if envelope.get("is_error"):
        logger.error(
            "event=claude_review.cli_error subtype=%s status=%s",
            envelope.get("subtype"), envelope.get("api_error_status"),
        )
        raise TreeAnalysisError("Claude could not complete the review.")

    # --json-schema puts the validated object on structured_output; `result` is
    # the same content as a string. Prefer the parsed form and fall back only if
    # a future CLI version stops populating it.
    review = envelope.get("structured_output")
    if not isinstance(review, dict):
        raw = envelope.get("result")
        if not isinstance(raw, str) or not raw.strip():
            raise TreeAnalysisError("Claude returned an empty review.")
        try:
            review = json.loads(raw)
        except ValueError as exc:
            raise TreeAnalysisError("Claude returned a malformed review.") from exc

    usage = envelope.get("usage") or {}
    # modelUsage lists every model the CLI touched, including the small one it
    # uses for its own side tasks (19 output tokens on a run where the reviewer
    # produced 7523). Report the one that actually wrote the review: the biggest
    # output-token producer, not whichever key happens to come first.
    model_usage = envelope.get("modelUsage") or {}
    reviewer_model = str(Config.CLAUDE_REVIEW_MODEL)
    if model_usage:
        reviewer_model = max(
            model_usage.items(),
            key=lambda item: (item[1] or {}).get("outputTokens") or 0,
        )[0]
    return {
        "review": _validate_review(review),
        "model": reviewer_model,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cost_usd": envelope.get("total_cost_usd"),
        },
    }


def _call_claude(context: Dict[str, Any]) -> Dict[str, Any]:
    """Send the metrics to Claude and return the parsed review plus usage."""
    if _backend() != "api":
        return _call_claude_cli(context)

    import anthropic

    # The timeout is per attempt, so one retry means worst-case wall clock is
    # twice CLAUDE_REVIEW_TIMEOUT_SECONDS. That is the tradeoff for surviving a
    # 529 overload without making the user click again; the concurrency ceiling
    # is what bounds how many request slots this can occupy at once.
    client = anthropic.Anthropic(
        api_key=Config.ANTHROPIC_API_KEY,
        timeout=Config.CLAUDE_REVIEW_TIMEOUT_SECONDS,
        max_retries=1,
    )

    try:
        # Streamed so the large max_tokens cannot trip the SDK's non-streaming
        # timeout guard; the response is only used once it is complete.
        with client.messages.stream(
            model=Config.CLAUDE_REVIEW_MODEL,
            max_tokens=Config.CLAUDE_REVIEW_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": Config.CLAUDE_REVIEW_EFFORT,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
            messages=[{"role": "user", "content": _build_user_message(context)}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APITimeoutError as exc:
        raise TreeAnalysisError(
            "Claude did not respond in time. Try again in a moment."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise TreeAnalysisUnavailable(
            "Claude is rate limiting requests right now. Try again shortly."
        ) from exc
    except anthropic.AuthenticationError as exc:
        logger.error("Claude review rejected the configured API key")
        raise TreeAnalysisUnavailable(
            "Claude review is not configured correctly on this server."
        ) from exc
    except anthropic.APIStatusError as exc:
        logger.error(
            "Claude review failed: status=%s type=%s", exc.status_code, exc.type
        )
        raise TreeAnalysisError("Claude could not complete the review.") from exc
    except anthropic.APIConnectionError as exc:
        raise TreeAnalysisError("Could not reach Claude from this server.") from exc

    if message.stop_reason == "refusal":
        raise TreeAnalysisError("Claude declined to review this dataset.")

    text = next(
        (block.text for block in message.content if block.type == "text"), ""
    )
    if not text:
        # max_tokens with adaptive thinking on is the realistic way to get here:
        # the budget went to reasoning and no JSON was emitted.
        raise TreeAnalysisError(
            "Claude returned an empty review"
            + (" (output limit reached)." if message.stop_reason == "max_tokens" else ".")
        )

    try:
        review = json.loads(text)
    except ValueError as exc:
        raise TreeAnalysisError("Claude returned a malformed review.") from exc

    return {
        "review": _validate_review(review),
        "model": message.model,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_read_input_tokens": getattr(
                message.usage, "cache_read_input_tokens", 0
            ),
        },
    }


# =============================================================================
# Cache
# =============================================================================

def _cache_path(job_dir: Path) -> Path:
    return job_dir / CACHE_RELATIVE_PATH


def load_cached_review(job_dir: Path, expected_fingerprint: str) -> Optional[Dict[str, Any]]:
    """Return a stored review when it was produced from the current numbers."""
    path = _cache_path(job_dir)
    if not path.exists():
        return None
    try:
        with open(path, "r") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    if stored.get("fingerprint") != expected_fingerprint:
        return None
    if stored.get("schema_version") != REVIEW_SCHEMA_VERSION:
        return None
    stored["cached"] = True
    return stored


def _store_review(job_dir: Path, payload: Dict[str, Any]) -> None:
    path = _cache_path(job_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        with open(temporary, "w") as handle:
            json.dump(payload, handle, indent=2, default=str)
        os.replace(temporary, path)
    except OSError as exc:
        # A review the user can read but we could not cache is still a success.
        logger.warning("Could not cache Claude review: %s", exc)


# =============================================================================
# Entry point
# =============================================================================

def review_job(job_dir: Path, *, force_refresh: bool = False) -> Dict[str, Any]:
    """Produce (or reuse) a Claude review of this job's alignment and tree."""
    if not is_configured():
        raise TreeAnalysisUnavailable("Claude review is not enabled on this server.")

    context = build_context(job_dir)
    key = fingerprint(context)

    if not force_refresh:
        cached = load_cached_review(job_dir, key)
        if cached is not None:
            return cached

    slot = _acquire_slot()
    started = time.monotonic()
    try:
        result = _call_claude(context)
    finally:
        if slot is not None:
            slot.release()

    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "fingerprint": key,
        "generated_at": time.time(),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model": result["model"],
        "usage": result["usage"],
        "metrics": context,
        "review": result["review"],
        "cached": False,
    }
    _store_review(job_dir, payload)
    return payload
