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
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from flask import current_app

from app.config import Config
from app.services.artifact_storage import artifact_exists, open_artifact

logger = logging.getLogger(__name__)

# Bump when the prompt or the metric set changes in a way that would make a
# stored review misleading. Cached reviews with a different version are ignored.
REVIEW_SCHEMA_VERSION = 5

CACHE_RELATIVE_PATH = Path("analysis") / "claude_review.json"

GAP_CHARS = frozenset("-.~?")
# str.translate runs in C; a generator comprehension over every character of a
# 2400 x 3300 alignment does not, and this is on the request path.
_GAP_DELETE_TABLE = {ord(char): None for char in GAP_CHARS}
UNAMBIGUOUS = frozenset("ACGT")

# Column sampling kicks in only for alignments big enough that a full pass would
# be felt inside a request. 12M cells is roughly 3000 sequences x 4000 columns.
MAX_ALIGNMENT_CELLS = 12_000_000
MAX_PAIRWISE_COMPARISONS = 400
# A column carrying fewer than four UNAMBIGUOUS residues cannot separate two
# clades of two, so it is neither invariant nor informative in any useful sense.
# Invariant percentages over a gappy barcode alignment are dominated by such
# columns. Ambiguous characters occupy a column but carry no state, so they do
# not count towards this threshold.
MIN_INFORMATIVE_RESIDUES = 4
# Two sequences overlapping in fewer comparable columns than this share less
# than a short barcode's worth of evidence, and their pairwise identity says
# very little about how related they are.
LOW_PAIRWISE_OVERLAP_COLUMNS = 100
# Below this a sequence's interior gaps are a handful of columns, which is not
# what "possible misalignment" means.
NOTEWORTHY_INTERNAL_GAP_PERCENT = 1.0
# Branch lengths in these trees run to nine decimal places, and a "zero-length"
# branch is in practice anything below the resolution the substitution model
# could distinguish. Metrics computed with this tolerance are named near_zero_*
# so an exact-zero tally is never confused with a tolerance-based one.
NEAR_ZERO_BRANCH_LENGTH = 1e-6
# How many worst-offender rows to name. Long enough to see a pattern, short
# enough that the prompt stays compact on a 2000-tip tree.
TOP_N = 12

# --- Topology digest ---------------------------------------------------------
# Every other metric here describes the tree in aggregate. None of them say
# which tips group with which, which is the one thing a user opens the viewer
# to find out, so the review could report that a tree was well supported
# without ever being able to say what it supported. The digest is a bounded
# answer to that: the maximal well-supported clades and their members.
MAX_CLADE_GROUPS = 20
CLADE_TIP_LIMIT = 8
# Ceiling on how many times an oversized group may be reopened. A chain of
# nested strong clades is descended one level per attempt, and each attempt
# rescans that subtree, so an unbounded descent down a 2000-deep caterpillar
# tree would be quadratic work inside a Gunicorn request slot.
MAX_CLADE_REOPEN_ATTEMPTS = 200
# A taxon label scattered across more groups than this is not a polyphyly
# finding, it is a label being used loosely across the whole tree.
MAX_SPLIT_LABELS = 8

# --- Alignment excerpt -------------------------------------------------------
# The one place actual residues are sent. No summary statistic can separate a
# real indel from a misalignment -- internal_gap_percent says a row has interior
# gaps, not whether they sit where a biologist would expect -- so the worst
# offenders get a narrow window of the alignment around their largest gap, with
# well-behaved neighbours beside them for contrast. Deliberately tiny: a couple
# of windows of a few hundred columns, never the alignment.
EXCERPT_MAX_WINDOWS = 2
EXCERPT_MAX_COLUMNS = 160
EXCERPT_FLANK_COLUMNS = 40
EXCERPT_MAX_ROWS = 8
EXCERPT_CONTRAST_ROWS = 3
# Two windows landing on the same region of the alignment say the same thing
# twice; the second is dropped once this much of it is already covered.
EXCERPT_OVERLAP_REJECT = 0.5

# --- Provenance --------------------------------------------------------------
# Fields copied from input_info.json["sequence_metadata"] onto a flagged row.
# `identity` and `query_cover` are the submitted sequence's BLAST metrics
# against the record it pulled in, and are published only where the entry says
# they were actually computed.
_PROVENANCE_FIELDS = ("source", "hit_source", "accession", "taxon", "location")
_PROVENANCE_BLAST_FIELDS = ("identity", "query_cover", "subject_cover")
# Below this a reference is far enough from the query set to be worth naming.
LOW_REFERENCE_IDENTITY_PERCENT = 90.0


class TreeAnalysisError(Exception):
    """A review could not be produced for a reason worth showing the user."""


class TreeAnalysisNoTree(TreeAnalysisError):
    """The job has not produced a tree file, so there is nothing to review (404).

    Separate from the other TreeAnalysisError cases because it is the one that
    is genuinely "no such resource" rather than "this request is wrong": the
    endpoint answered it with 404 through a `except FileNotFoundError` branch
    that stopped being reachable once _review_newick_path took over the check.
    """


class TreeAnalysisUpstreamError(TreeAnalysisError):
    """Claude itself failed: no reply, an unusable reply, or one that broke the
    response contract (502).

    Separate from TreeAnalysisError so the endpoint can tell "this job has
    nothing to review", which is about the request, apart from "the model
    answered with something we cannot show", which is about the service behind
    us. The old code reported both as 400 and blamed the browser for an upstream
    failure.
    """


class TreeAnalysisUnavailable(TreeAnalysisError):
    """The feature is switched off or temporarily out of capacity (503)."""


class TreeAnalysisRateLimited(TreeAnalysisUnavailable):
    """The upstream reviewer refused a call until a later time (429)."""

    def __init__(self, message: str, retry_after_seconds: int = 60):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class TreeAnalysisInProgress(TreeAnalysisUnavailable):
    """An identical review of this job is already running (409).

    Answered rather than queued: the caller is a browser holding one of eight
    Gunicorn request slots, and blocking it behind a call that can take 90
    seconds would spend a second slot to produce a result the first request is
    already about to cache.
    """

    def __init__(self, retry_after_seconds: int):
        super().__init__(
            "A review of this tree is already running. It will be ready in a "
            "moment -- reopen the review rather than starting a second one."
        )
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class TreeAnalysisDailyLimit(TreeAnalysisUnavailable):
    """No more potentially billed reviews may start before the UTC reset."""

    def __init__(self, limit: int, retry_after_seconds: int):
        super().__init__(
            f"Claude's daily review limit ({limit}) has been reached. "
            "Cached reviews are still available; new reviews reset at 00:00 UTC."
        )
        self.retry_after_seconds = retry_after_seconds


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


def _round_metric(value: float) -> float:
    """Round for display without rounding a real branch length away.

    These trees carry branch lengths down to 6e-9 and hundreds of branches sit
    below 1e-6, so a flat round(x, 6) turned a fifth of the tree into hard
    zeros -- the same failure `write_tree_file()` exists to prevent on the way
    out. Small magnitudes keep three significant figures instead.
    """
    if value == 0 or abs(value) >= 1e-4:
        return round(value, 6)
    return float(f"{value:.3g}")


def _quantile_at(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank quantile of an already-sorted sequence, unrounded.

    Nearest-rank rather than interpolated: these feed a prose summary, and a
    real observed value is easier to reconcile with the tree than an
    interpolated one that no branch actually has.
    """
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _quantiles(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Min / quartiles / max, rounded. Returns nulls for an empty input."""
    if not values:
        return {"min": None, "q1": None, "median": None, "q3": None, "max": None}
    ordered = sorted(values)
    return {
        "min": _round_metric(ordered[0]),
        "q1": _round_metric(_quantile_at(ordered, 0.25)),
        "median": _round_metric(_quantile_at(ordered, 0.5)),
        "q3": _round_metric(_quantile_at(ordered, 0.75)),
        "max": _round_metric(ordered[-1]),
    }


def _column_indices(n_columns: int, n_sequences: int) -> Tuple[List[int], bool]:
    """Pick which columns to score, sampling evenly only when the matrix is huge.

    An evenly spaced sample keeps the gappy ends and the conserved core in
    proportion, which a random sample of the same size would not guarantee.

    The second element is whether sampling actually happened, which is a
    property of the step and not of the cell ceiling: a very tall, narrow
    alignment can exceed MAX_ALIGNMENT_CELLS and still come out with a step of
    1, in which case every column is scored and the tallies are exact. Reporting
    those as estimates suppressed the exact counts for no reason.
    """
    if n_columns == 0 or n_sequences == 0:
        return [], False
    if n_columns * n_sequences <= MAX_ALIGNMENT_CELLS:
        return list(range(n_columns)), False
    budget = max(1, MAX_ALIGNMENT_CELLS // n_sequences)
    step = max(1, n_columns // budget)
    return list(range(0, n_columns, step)), step > 1


def _strip_gaps(sequence: str) -> str:
    """Sequence with every character GAP_CHARS calls a gap removed.

    Identical-sequence grouping used to strip only "-", so two rows padded with
    "." or "~" (or carrying "?" at the ends, as several GenBank ITS records do)
    were reported as different sequences while every other alignment metric in
    this module already treated those characters as gaps.
    """
    return sequence.translate(_GAP_DELETE_TABLE)


# str.lstrip/rstrip run in C, which matters on a 2400 x 3300 alignment.
_GAP_STRING = "".join(sorted(GAP_CHARS))


def _terminal_and_internal_gaps(sequence: str) -> Tuple[int, int]:
    """Split one row's gap characters into (terminal padding, interior gaps).

    A short barcode padded at both ends is missing data; a full-length sequence
    carrying gaps in its middle is either a real indel or a misalignment. The
    two look identical in a single gap percentage and are not the same problem.
    """
    leading = len(sequence) - len(sequence.lstrip(_GAP_STRING))
    if leading == len(sequence):
        # Nothing but gaps: all of it is terminal, none of it interior.
        return len(sequence), 0
    trailing = len(sequence) - len(sequence.rstrip(_GAP_STRING))
    core = sequence[leading:len(sequence) - trailing]
    interior = len(core) - len(_strip_gaps(core))
    return leading + trailing, interior


def _sequence_row(header: str, sequence: str) -> Dict[str, Any]:
    """Deterministic per-sequence facts, joined onto suspect rows downstream."""
    residues = _strip_gaps(sequence)
    ungapped = len(residues)
    gaps = len(sequence) - ungapped
    unambiguous = sum(residues.count(base) for base in UNAMBIGUOUS)
    ambiguous = ungapped - unambiguous
    terminal_gaps, internal_gaps = _terminal_and_internal_gaps(sequence)
    return {
        "name": header,
        "ungapped_length": ungapped,
        "gap_percent": _percent(gaps, len(sequence)) or 0.0,
        "terminal_gap_percent": _percent(terminal_gaps, len(sequence)) or 0.0,
        "internal_gap_percent": _percent(internal_gaps, len(sequence)) or 0.0,
        "ambiguity_percent": _percent(ambiguous, ungapped) or 0.0,
    }


def _scaled_to_full_alignment(
    sample_count: int, scored: int, n_columns: int
) -> Optional[int]:
    """Extrapolate a per-column tally from the scored sample to all columns.

    Only used when column sampling is active, and only under a name ending in
    `_estimated`, so a sampled figure can never be read as an exact count.
    """
    if not scored or not n_columns:
        return None
    return int(round(sample_count * (n_columns / scored)))


def _largest_internal_gap_run(sequence: str) -> Optional[Tuple[int, int]]:
    """Half-open bounds of the longest run of gaps inside a row's own residues.

    Terminal padding is excluded deliberately: a barcode padded at both ends is
    missing data, and a window centred on that padding shows the model a column
    of nothing. Only gaps between the sequence's own first and last residue can
    be an indel or a misalignment, and those are the ones worth looking at.
    """
    leading = len(sequence) - len(sequence.lstrip(_GAP_STRING))
    if leading >= len(sequence):
        return None
    trailing = len(sequence) - len(sequence.rstrip(_GAP_STRING))
    end = len(sequence) - trailing

    best: Optional[Tuple[int, int]] = None
    best_length = 0
    run_start: Optional[int] = None
    for index in range(leading, end):
        if sequence[index] in GAP_CHARS:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            if index - run_start > best_length:
                best_length = index - run_start
                best = (run_start, index)
            run_start = None
    if run_start is not None and end - run_start > best_length:
        best = (run_start, end)
    return best


def _excerpt_window(
    gap_run: Tuple[int, int], n_columns: int
) -> Tuple[int, int]:
    """A bounded window of columns around one gap run.

    Padded on both sides so the model sees the residues the gap interrupts --
    a gap shown on its own is uninterpretable -- and capped so a single
    thousand-column deletion cannot pull the whole alignment into the prompt.
    """
    start = max(0, gap_run[0] - EXCERPT_FLANK_COLUMNS)
    end = min(n_columns, gap_run[1] + EXCERPT_FLANK_COLUMNS)
    if end - start > EXCERPT_MAX_COLUMNS:
        # Keep the start of the run in view rather than a slice of its middle:
        # where a gap opens is what says whether it is an indel or a row that
        # slipped out of register.
        end = start + EXCERPT_MAX_COLUMNS
    return start, end


def _window_occupancy(sequence: str, start: int, end: int) -> float:
    """Fraction of a window a row actually holds residues in."""
    window = sequence[start:end]
    if not window:
        return 0.0
    return len(_strip_gaps(window)) / float(end - start)


def build_alignment_excerpt(
    records: List[Tuple[str, str]], per_sequence_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Narrow windows of real residues around the worst interior gaps.

    Everything else this module sends is a precomputed number, for the reasons
    in the module docstring. This is the one exception, and it is narrow on
    purpose. `internal_gap_percent` can tell the reviewer that a row carries
    interior gaps; nothing computable here can tell it whether those gaps fall
    at a plausible indel or whether the row has simply slipped out of register
    against its neighbours, and those two findings call for different advice.
    Seeing forty columns either side of the gap settles it.

    At most EXCERPT_MAX_WINDOWS windows of at most EXCERPT_MAX_COLUMNS columns,
    each carrying the offending row plus a few well-occupied neighbours for
    contrast -- a few kB, not a FASTA.
    """
    if not records:
        return []
    n_columns = max((len(seq) for _, seq in records), default=0)
    if not n_columns:
        return []

    candidates = sorted(
        (
            row for row in per_sequence_rows
            if row.get("internal_gap_percent", 0.0)
            >= NOTEWORTHY_INTERNAL_GAP_PERCENT
        ),
        key=lambda row: row["internal_gap_percent"],
        reverse=True,
    )
    if not candidates:
        return []

    sequences = {_normalize_name(header): seq for header, seq in records}
    # Contrast rows are chosen once, from the cleanest interiors: a window
    # showing only gappy rows cannot show that a gap is out of register.
    cleanest = sorted(
        per_sequence_rows, key=lambda row: row.get("internal_gap_percent", 0.0)
    )

    excerpts: List[Dict[str, Any]] = []
    covered: List[Tuple[int, int]] = []
    for candidate in candidates:
        if len(excerpts) >= EXCERPT_MAX_WINDOWS:
            break
        key = _normalize_name(candidate["name"])
        sequence = sequences.get(key)
        if not sequence:
            continue
        gap_run = _largest_internal_gap_run(sequence)
        if gap_run is None:
            continue
        start, end = _excerpt_window(gap_run, n_columns)
        span = end - start
        if span <= 0:
            continue
        if any(
            max(0, min(end, done_end) - max(start, done_start))
            > EXCERPT_OVERLAP_REJECT * span
            for done_start, done_end in covered
        ):
            continue

        rows: List[Dict[str, Any]] = [{
            "name": candidate["name"],
            "role": "flagged",
            "internal_gap_percent": candidate.get("internal_gap_percent"),
            "residues": sequence[start:end],
        }]
        for other in cleanest:
            if len(rows) >= min(EXCERPT_MAX_ROWS, 1 + EXCERPT_CONTRAST_ROWS):
                break
            other_key = _normalize_name(other["name"])
            if other_key == key:
                continue
            other_sequence = sequences.get(other_key)
            if not other_sequence:
                continue
            # A neighbour that is itself mostly padding here shows nothing.
            if _window_occupancy(other_sequence, start, end) < 0.5:
                continue
            rows.append({
                "name": other["name"],
                "role": "contrast",
                "internal_gap_percent": other.get("internal_gap_percent"),
                "residues": other_sequence[start:end],
            })
        if len(rows) < 2:
            # One row in isolation says nothing about register.
            continue

        covered.append((start, end))
        excerpts.append({
            "flagged_sequence": candidate["name"],
            "first_column": start + 1,
            "last_column": end,
            "columns_shown": span,
            "columns_in_alignment": n_columns,
            "largest_internal_gap_columns": gap_run[1] - gap_run[0],
            "rows": rows,
        })
    for excerpt in excerpts:
        excerpt["reading_note"] = (
            "Columns are 1-based positions in the current alignment. The "
            "`flagged` row is one of the most internally gapped sequences; "
            "the `contrast` rows are among the least internally gapped and "
            "are shown so the flagged row can be read against its "
            "neighbours. This window is a fragment of the alignment chosen "
            "around one gap, so nothing may be counted or generalised from "
            "it -- use it only to judge whether that gap looks like an "
            "indel or like a row out of register."
        )
    return excerpts


def summarize_alignment(
    records: List[Tuple[str, str]],
    *,
    per_sequence_out: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compute the alignment half of the review context.

    Reports occupancy, variability and per-sequence outliers. Sequences of
    unequal length mean the file is not actually aligned, which is itself worth
    reporting rather than crashing on.

    `per_sequence_out`, when given, is filled with the full per-sequence row for
    every record. The summary itself only ever carries the TOP_N worst rows --
    a 2400-sequence roster would swamp the prompt -- but the caller needs the
    whole set to join deterministic numbers onto the tips the tree half names.
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
    columns_with_enough_states = 0
    parsimony_informative_enough = 0
    invariant_enough = 0

    for index in indices:
        counts: Dict[str, int] = {}
        gaps = 0
        for _, seq in records:
            char = seq[index] if index < len(seq) else "-"
            if char in GAP_CHARS:
                gaps += 1
            elif char in UNAMBIGUOUS:
                counts[char] = counts.get(char, 0) + 1
            # Ambiguous residues occupy the column but contribute no state to
            # the parsimony test. They are counted through `occupied` below and
            # deliberately not through `state_bearing`.
        occupied = n_sequences - gaps
        occupancy_fractions.append(occupied / n_sequences)
        if occupied == 0:
            all_gap_columns += 1
            continue
        distinct = len(counts)
        informative = False
        if distinct <= 1:
            invariant += 1
        else:
            variable += 1
            # Parsimony-informative in the standard sense: at least two states
            # each seen at least twice, so the column can distinguish topologies.
            informative = sum(1 for count in counts.values() if count >= 2) >= 2
            if informative:
                parsimony_informative += 1
        # A column holding three state-bearing residues or fewer cannot separate
        # two clades of two whatever its states are, so calling it invariant
        # overstates how much of the alignment is genuinely conserved. The test
        # is on UNAMBIGUOUS residues, not on occupancy: four Ns occupy a column
        # but can neither establish invariance nor resolve a split, and counting
        # them here readmitted exactly the columns this denominator exists to
        # exclude. Report it alongside the raw one rather than instead of it.
        state_bearing = sum(counts.values())
        if state_bearing >= MIN_INFORMATIVE_RESIDUES:
            columns_with_enough_states += 1
            if distinct <= 1:
                invariant_enough += 1
            elif informative:
                parsimony_informative_enough += 1

    scored = len(indices)
    total_cells = 0
    total_gaps = 0
    per_sequence: List[Dict[str, Any]] = []
    seen_sequences: Dict[str, List[str]] = {}

    for header, seq in records:
        row = _sequence_row(header, seq)
        total_cells += len(seq)
        total_gaps += len(seq) - row["ungapped_length"]
        per_sequence.append(row)
        # Identical rows are indistinguishable to the tree builder and turn up
        # as zero-length sister pairs; naming them saves the user a hunt.
        seen_sequences.setdefault(_strip_gaps(seq), []).append(header)

    if per_sequence_out is not None:
        per_sequence_out.extend(per_sequence)

    duplicate_groups = [
        # Names are a sample, not the whole group: `count` is what matters, and
        # a species complex can put 30+ identical ITS reads in one group.
        {"count": len(names), "names": names[:6], "names_truncated": len(names) > 6}
        for names in seen_sequences.values()
        if len(names) > 1
    ]
    duplicate_groups.sort(key=lambda group: group["count"], reverse=True)
    identical_group_count = len(duplicate_groups)
    sequences_in_identical_groups = sum(
        group["count"] for group in duplicate_groups
    )

    gappiest = sorted(per_sequence, key=lambda row: row["gap_percent"], reverse=True)
    ambiguous_worst = sorted(
        per_sequence, key=lambda row: row["ambiguity_percent"], reverse=True
    )
    shortest = sorted(per_sequence, key=lambda row: row["ungapped_length"])
    internal_gappiest = sorted(
        per_sequence, key=lambda row: row["internal_gap_percent"], reverse=True
    )

    ungapped_lengths = [row["ungapped_length"] for row in per_sequence]
    terminal_gap_percents = [row["terminal_gap_percent"] for row in per_sequence]
    internal_gap_percents = [row["internal_gap_percent"] for row in per_sequence]

    low_occupancy_columns = sum(1 for fraction in occupancy_fractions if fraction < 0.5)

    pairwise = _pairwise_summary(records)

    summary: Dict[str, Any] = {
        "sequences": n_sequences,
        "columns": n_columns,
        "ragged": ragged,
        "columns_scored": scored,
        "column_sampling_applied": sampled,
        # One flag covering every per-column tally below, so a consumer never has
        # to work out which of them the sample touched.
        "column_metrics_are_estimates": sampled,
        "overall_gap_percent": _percent(total_gaps, total_cells) or 0.0,
        "occupancy_definition": (
            "Occupancy is NON-GAP occupancy: a column position counts as occupied "
            "whenever it holds any residue character, including ambiguous ones such "
            "as N. 100% occupancy therefore does not mean 100% confidently called "
            "bases -- see the ambiguity statistics for that."
        ),
        "column_percentage_definition": (
            "Column percentages are of columns_scored (= columns unless "
            "column_sampling_applied). Names ending "
            "_of_columns_with_at_least_4_unambiguous_residues use that stricter "
            "denominator: columns holding 4+ unambiguous A/C/G/T residues, the "
            "only ones that can resolve a split. An ambiguous character such as "
            "N occupies its column but does not count towards it."
        ),
        "mean_column_occupancy_percent": (
            round(100.0 * sum(occupancy_fractions) / len(occupancy_fractions), 2)
            if occupancy_fractions else None
        ),
        "columns_below_50_percent_occupancy_percent": _percent(
            low_occupancy_columns, scored
        ),
        "all_gap_column_percent": _percent(all_gap_columns, scored),
        "invariant_column_percent": _percent(invariant, scored),
        "variable_column_percent": _percent(variable, scored),
        "parsimony_informative_percent": _percent(parsimony_informative, scored),
        "columns_with_at_least_4_unambiguous_residues_percent": _percent(
            columns_with_enough_states, scored
        ),
        "parsimony_informative_percent_of_columns_with_at_least_4_unambiguous_residues": _percent(
            parsimony_informative_enough, columns_with_enough_states
        ),
        "invariant_percent_of_columns_with_at_least_4_unambiguous_residues": _percent(
            invariant_enough, columns_with_enough_states
        ),
        "ungapped_length": _quantiles(ungapped_lengths),
        "mean_pairwise_identity_percent": pairwise["mean_pairwise_identity_percent"],
        "pairwise_overlap": pairwise["pairwise_overlap"],
        "gap_composition_definition": (
            "Percentages of aligned length. terminal = leading/trailing padding "
            "(missing data); internal = gaps between a sequence's own first and "
            "last residue (indels or misalignment)."
        ),
        "mean_terminal_gap_percent": (
            round(sum(terminal_gap_percents) / len(terminal_gap_percents), 2)
            if terminal_gap_percents else None
        ),
        "mean_internal_gap_percent": (
            round(sum(internal_gap_percents) / len(internal_gap_percents), 2)
            if internal_gap_percents else None
        ),
        "internal_gap_percent_distribution": _quantiles(internal_gap_percents),
        "identical_sequence_group_count": identical_group_count,
        "sequences_in_identical_groups_total": sequences_in_identical_groups,
        "identical_sequence_groups": duplicate_groups[:TOP_N],
        "gappiest_sequences": [row for row in gappiest[:TOP_N] if row["gap_percent"] > 0],
        # Only rows with enough interior gaps to be worth looking at. A tenth of
        # a percent of internal gap is one column in a thousand and is not
        # evidence of misalignment; listing those spent prompt space on every
        # clean alignment in the system.
        "most_internally_gapped_sequences": [
            row for row in internal_gappiest[:TOP_N // 2]
            if row["internal_gap_percent"] >= NOTEWORTHY_INTERNAL_GAP_PERCENT
        ],
        "most_ambiguous_sequences": [
            row for row in ambiguous_worst[:TOP_N] if row["ambiguity_percent"] > 0
        ],
        "shortest_sequences": shortest[:TOP_N],
    }

    # A bare count is only published when every column was actually scored. Under
    # sampling the same quantity appears once, under an `_estimated` name, so no
    # field in this dict can be read as an exact tally that is not one.
    if sampled:
        summary.update({
            "parsimony_informative_columns_estimated": _scaled_to_full_alignment(
                parsimony_informative, scored, n_columns
            ),
            "columns_below_50_percent_occupancy_estimated": _scaled_to_full_alignment(
                low_occupancy_columns, scored, n_columns
            ),
            "all_gap_columns_estimated": _scaled_to_full_alignment(
                all_gap_columns, scored, n_columns
            ),
            "columns_with_at_least_4_unambiguous_residues_estimated": _scaled_to_full_alignment(
                columns_with_enough_states, scored, n_columns
            ),
        })
    else:
        summary.update({
            "parsimony_informative_columns": parsimony_informative,
            "columns_below_50_percent_occupancy": low_occupancy_columns,
            "all_gap_columns": all_gap_columns,
            "columns_with_at_least_4_unambiguous_residues": columns_with_enough_states,
        })
    return summary


def _pairwise_pairs(count: int) -> Tuple[List[Tuple[int, int]], bool]:
    """Which sequence pairs to compare, and whether that set is a sample.

    Pairs are drawn with a fixed seed so a re-run produces the same numbers and
    does not invalidate the cached review.
    """
    total_pairs = count * (count - 1) // 2
    if total_pairs <= MAX_PAIRWISE_COMPARISONS:
        return [(i, j) for i in range(count) for j in range(i + 1, count)], False

    rng = random.Random(0)
    pairs: List[Tuple[int, int]] = []
    seen = set()
    while len(pairs) < MAX_PAIRWISE_COMPARISONS:
        i = rng.randrange(count)
        j = rng.randrange(count)
        if i == j:
            continue
        key = (min(i, j), max(i, j))
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs, True


def _pairwise_summary(records: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Mean identity and overlap over columns where both members have a base.

    Identity alone hides the case this is really about: two ITS reads covering
    different halves of the locus can be 100% identical over the twenty columns
    they share. The overlap distribution is what tells the reviewer whether an
    identity figure rests on any evidence at all.
    """
    empty = {
        "mean_pairwise_identity_percent": None,
        "pairwise_overlap": {
            "pairs_compared": 0,
            "pairs_sampled": False,
            "note": "Fewer than two sequences, so no pair could be compared.",
        },
    }
    if len(records) < 2:
        return empty

    pairs, sampled = _pairwise_pairs(len(records))

    identities: List[float] = []
    overlaps: List[int] = []
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
        overlaps.append(compared)
        if compared:
            identities.append(matches / compared)

    return {
        "mean_pairwise_identity_percent": (
            round(100.0 * sum(identities) / len(identities), 2) if identities else None
        ),
        "pairwise_overlap": {
            "definition": (
                "Columns where both members of a pair carry a residue; the "
                "pairwise identity is measured only over those."
            ),
            "pairs_compared": len(pairs),
            "pairs_sampled": sampled,
            "sampling_note": (
                f"A fixed-seed sample of {len(pairs)} pairs, not every pair."
                if sampled else "Every pair was compared."
            ),
            "overlap_columns": _quantiles(overlaps),
            "pairs_with_no_comparable_columns": sum(1 for n in overlaps if n == 0),
            "pairs_below_100_overlap_columns": sum(
                1 for n in overlaps if n < LOW_PAIRWISE_OVERLAP_COLUMNS
            ),
            "low_overlap_threshold_columns": LOW_PAIRWISE_OVERLAP_COLUMNS,
        },
    }


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


# Which scale each tree builder actually writes. Provenance decides the scale;
# the numbers only decide it for a tree whose builder we do not recognise.
# Mirrored by normalizeTreeMethod()/classifySupportType() in
# tree_viewer_phylotree_v2.js -- change one and you must change the other.
_METHOD_SUPPORT_TYPE = {
    "fasttree": "SH",
    "raxml": "BS",
    # IQ-TREE's single-support form is the ultrafast bootstrap, which is not the
    # classical bootstrap and does not share its 70/95 reading.
    "iqtree": "UFBOOT",
    "mrbayes": "PP",
}

_METHOD_ALIASES = {
    "raxml-ng": "raxml",
    "raxmlng": "raxml",
    "raxml_ng": "raxml",
    "raxml8": "raxml",
    "iq-tree": "iqtree",
    "iqtree2": "iqtree",
    "iq-tree2": "iqtree",
    "mr_bayes": "mrbayes",
    "mrbayes3": "mrbayes",
    "fasttree2": "fasttree",
    "neighbor-joining": "nj",
    "neighbour-joining": "nj",
}


def _normalize_tree_method(tree_method: Any) -> str:
    """Canonical lower-case builder id, or "" when the builder is unknown.

    Whitespace is removed rather than merely trimmed, so the spellings that
    reach this from metadata and from user-facing labels -- "IQ-TREE 2",
    "RAxML NG", "Fast Tree" -- normalize onto the same builder as their
    hyphenated forms instead of falling through to the value-shape fallback.
    """
    method = re.sub(r"\s+", "", str(tree_method or "").lower())
    return _METHOD_ALIASES.get(method, method)


def _classify_support(
    values: List[float],
    has_dual: bool,
    tree_method: str,
    alrt_only: bool = False,
) -> str:
    """Name the support scale, using the same rules as the viewer's badge.

    Provenance first, value shape only as a last resort. RAxML-NG writes plain
    bootstrap and is perfectly capable of emitting 0, 1 and 0.95 for poorly
    supported clades; inferring from magnitude alone called those trees Bayesian
    posterior probabilities and told the user a bootstrap of 1% was a posterior
    of 0.01 on a 0-1 scale.

    IQ-TREE gets three answers rather than one, because it can write three
    different things into the same position: dual "SH-aLRT/UFBoot" labels when
    both tests ran, single SH-aLRT percentages when only `-alrt` was given, and
    single UFBoot percentages otherwise. Calling any of them classical bootstrap
    hands the reader the wrong thresholds -- UFBoot is anti-conservative and its
    cutoff is 95, and SH-aLRT's is 80.
    """
    if has_dual:
        return "ALRT_UFBOOT"
    if not values:
        return "none"

    declared = _METHOD_SUPPORT_TYPE.get(_normalize_tree_method(tree_method))
    if declared == "UFBOOT" and alrt_only:
        return "ALRT"
    if declared:
        return declared

    # Genuinely unknown or legacy builder: the numbers are all there is to go on.
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
    "UFBOOT": (
        "IQ-TREE ultrafast bootstrap (UFBoot) percentages (0-100). NOT the "
        "classical bootstrap scale: UFBoot is much less conservative, and the "
        "conventional cutoff is >=95 for strong support. Do not describe the "
        "70-94 band as 'moderate bootstrap support' -- that convention belongs "
        "to standard non-parametric bootstrap and overstates UFBoot values."
    ),
    "ALRT": (
        "IQ-TREE SH-aLRT branch-test percentages (0-100), written when -alrt ran "
        "without ultrafast bootstrap. >=80 is the conventional cutoff. This is a "
        "likelihood-ratio branch test, not a bootstrap proportion, and it is not "
        "comparable with bootstrap values on the same numeric scale."
    ),
    "ALRT_UFBOOT": (
        "IQ-TREE dual support written SH-aLRT/UFBoot, both percentages (0-100). "
        "A clade is normally called well supported at SH-aLRT >= 80 AND UFBoot >= 95. "
        "The single-value statistics below threshold the UFBoot half."
    ),
    "mixed": "Mixed support scales in one tree; compare values across nodes with care.",
    "none": "This tree carries no node support values.",
}

# (strong, moderate) cutoffs per scale. `None` for the moderate cutoff means the
# scale has no conventional middle band, and no *_moderate_* figure is published
# for it: inheriting the classical bootstrap's 70 would invent a convention that
# does not exist for ultrafast bootstrap or for SH-aLRT.
# The dual SH-aLRT/UFBoot label is the one scale whose conventional reading is
# not a single cutoff: both halves must clear their own. The scalar statistics
# threshold the UFBoot half (that is what `_clade_support()` returns), so any
# "strongly supported" figure derived from them is a UFBoot-only figure and is
# labelled as such -- the joint rule is published separately.
DUAL_ALRT_STRONG = 80.0
DUAL_UFBOOT_STRONG = 95.0
DUAL_SUPPORT_RULE = "SH-aLRT >= 80 AND UFBoot >= 95"

SUPPORT_THRESHOLDS: Dict[str, Tuple[Optional[float], Optional[float]]] = {
    "BS": (95.0, 70.0),
    "PP": (0.95, 0.70),
    "SH": (0.95, 0.70),
    "UFBOOT": (95.0, None),
    "ALRT": (80.0, None),
    "ALRT_UFBOOT": (95.0, None),
    "mixed": (95.0, 70.0),
    "none": (None, None),
}


def _dual_strong_count(pairs: Sequence[Tuple[float, float]]) -> int:
    """Nodes meeting both halves of the conventional SH-aLRT/UFBoot rule."""
    return sum(
        1 for alrt, ufboot in pairs
        if alrt >= DUAL_ALRT_STRONG and ufboot >= DUAL_UFBOOT_STRONG
    )


def _support_partition(
    entries: List[Dict[str, Any]],
    strong_threshold: Optional[float],
    dual: bool = False,
) -> Dict[str, Any]:
    """Count, distribution and strong fraction for one branch-length partition.

    `strongly_supported_percent` thresholds the scalar support, which for a dual
    SH-aLRT/UFBoot label is the UFBoot half alone. On its own that calls a node
    at SH-aLRT 20 / UFBoot 99 strongly supported -- precisely the disagreement
    this partition exists to expose -- so a dual tree also gets the joint figure,
    and it is the one to read.
    """
    values = [entry["support"] for entry in entries if entry["support"] is not None]
    partition = {
        "internal_nodes": len(entries),
        "nodes_with_support": len(values),
        "nodes_without_support": len(entries) - len(values),
        "support_distribution": _quantiles(values),
        "strongly_supported_percent": (
            _percent(sum(1 for v in values if v >= strong_threshold), len(values))
            if strong_threshold is not None else None
        ),
    }
    if dual:
        pairs = [entry["dual"] for entry in entries if entry["dual"]]
        partition["nodes_with_dual_support"] = len(pairs)
        partition["jointly_well_supported_percent"] = _percent(
            _dual_strong_count(pairs), len(pairs)
        )
        partition["jointly_well_supported_rule"] = DUAL_SUPPORT_RULE
    return partition


def _tip_names_by_clade(tree) -> Dict[int, List[str]]:
    """Tip names under every clade, in a single post-order pass.

    `clade.get_terminals()` per node is O(tips) each, which on a 2000-tip tree
    is four million operations inside a Gunicorn request slot. Level order
    reversed visits every child before its parent, so each clade only has to
    concatenate what its children already produced.
    """
    names: Dict[int, List[str]] = {}
    for clade in reversed(list(tree.find_clades(order="level"))):
        if clade.is_terminal():
            names[id(clade)] = [str(clade.name or "")]
            continue
        collected: List[str] = []
        for child in clade.clades:
            collected.extend(names.get(id(child), ()))
        names[id(clade)] = collected
    return names


def _is_strongly_supported(
    value: Optional[float],
    dual: Optional[Tuple[float, float]],
    strong_threshold: Optional[float],
) -> bool:
    """Whether one node clears the conventional bar for its own support scale.

    A dual SH-aLRT/UFBoot label must clear both halves, exactly as
    `_dual_strong_count` requires: thresholding the scalar alone would call a
    node at SH-aLRT 20 / UFBoot 99 well supported.
    """
    if dual is not None:
        return dual[0] >= DUAL_ALRT_STRONG and dual[1] >= DUAL_UFBOOT_STRONG
    if strong_threshold is None or value is None:
        return False
    return value >= strong_threshold


def _clade_entry(
    index: int, clade, tip_names: List[str], basis: str
) -> Dict[str, Any]:
    """One group in the topology digest."""
    value, dual = _clade_support(clade)
    entry: Dict[str, Any] = {
        "id": f"C{index}",
        "tips": len(tip_names),
        "tip_names": tip_names[:CLADE_TIP_LIMIT],
        "tip_names_truncated": len(tip_names) > CLADE_TIP_LIMIT,
        "basis": basis,
    }
    if value is not None:
        entry["support"] = value
    if dual is not None:
        entry["dual_support_alrt_ufboot"] = [dual[0], dual[1]]
    if clade.branch_length is not None:
        entry["subtending_branch_length"] = _round_metric(float(clade.branch_length))
    return entry


def _maximal_strong_clades(
    roots: Sequence[Any], strong_threshold: Optional[float]
) -> List[Any]:
    """Strongly supported clades below `roots` that no such clade contains.

    Descends only through nodes that fail the support test, so the first
    strongly supported node on each path is taken and its strongly supported
    descendants are not: nested groups would repeat the same membership at
    every depth and say nothing the outermost one does not.
    """
    found: List[Any] = []
    queue = list(roots)
    while queue:
        clade = queue.pop()
        if clade.is_terminal():
            continue
        value, dual = _clade_support(clade)
        if _is_strongly_supported(value, dual, strong_threshold):
            found.append(clade)
            continue
        queue.extend(clade.clades)
    return found


def _topology_digest(
    tree, tip_names_by_id: Dict[int, List[str]], strong_threshold: Optional[float]
) -> Dict[str, Any]:
    """Which tips group with which, as a bounded list of groups.

    Preferred basis is support: strongly supported clades, taken outermost
    first so no group is contained in another. Nested strong clades would
    repeat the same membership at every depth and blow the prompt out on a
    large tree, while the outermost ones partition the tips into the groups the
    support justifies and leave the rest explicitly unplaced.

    Taking ONLY the outermost ones is not enough on its own. A tree whose
    deepest split is strongly supported has exactly one maximal strong clade
    holding almost every tip, which is true and useless -- a 2409-tip tree in
    the job archive reduced to two groups, one of 2300 tips. So an oversized
    group is reopened and replaced by the strongly supported clades inside it,
    largest first, until the digest is either informative or genuinely cannot
    be subdivided any further. Everything stays support-based; reopening a
    group can leave some of its tips in none of the replacements, and those
    become unplaced, which is the honest description of them.

    A tree with no support values, or none that clear the bar, still has a
    topology worth describing, so it falls back to splitting the largest group
    repeatedly from the root. That basis is reported, because a group held
    together by nothing but the shape of the file is not a finding.
    """
    root = tree.root
    all_tips = tip_names_by_id.get(id(root), [])

    def size(clade) -> int:
        return len(tip_names_by_id.get(id(clade), []))

    basis = "strong_support"
    groups = _maximal_strong_clades(root.clades, strong_threshold)
    supported_total = len(groups)

    if groups:
        # Reopening is a repair for a degenerate digest, not a general
        # refinement, so the bar is deliberately high: a group has to hold more
        # than half the tree before it counts as describing the whole tree
        # rather than a part of it. Reopening always costs coverage -- tips of
        # the old group that sit in no supported subclade become unplaced -- and
        # at a lower bar that trade is a bad one. A 324-tip job in the archive
        # went from 11 groups leaving 52 tips unplaced to 20 groups leaving 116,
        # which is more groups and less information. The second floor leaves
        # small trees alone entirely, where any group is a large share of a
        # small total.
        oversized = max(MAX_CLADE_GROUPS, len(all_tips) // 2)
        settled: List[Any] = []
        attempts = 0
        while (
            len(groups) + len(settled) < MAX_CLADE_GROUPS
            and attempts < MAX_CLADE_REOPEN_ATTEMPTS
        ):
            candidates = [c for c in groups if size(c) > oversized]
            if not candidates:
                break
            largest = max(candidates, key=size)
            attempts += 1
            inner = _maximal_strong_clades(largest.clades, strong_threshold)
            groups.remove(largest)
            if inner:
                # A single result is not a dead end: strongly supported clades
                # nest, and a 2409-tip FastTree job in the archive had its whole
                # tree inside a chain of them. Descending through the chain is
                # what eventually reaches the level that actually branches.
                # Tips left outside the replacements were in no supported clade
                # at that level and are correctly reported as unplaced.
                groups.extend(inner)
            else:
                # Nothing inside it is separately supported; it stays whole.
                settled.append(largest)
        groups.extend(settled)
    else:
        # Nothing clears the support bar (or the tree carries no support at
        # all). Split the largest group repeatedly instead, and say so.
        basis = "topology_only"
        supported_total = 0
        frontier = [root]
        while len(frontier) < MAX_CLADE_GROUPS:
            splittable = [
                clade for clade in frontier
                if not clade.is_terminal() and len(clade.clades) > 1
                and size(clade) > 2
            ]
            if not splittable:
                break
            largest = max(splittable, key=size)
            frontier.remove(largest)
            frontier.extend(largest.clades)
        groups = [clade for clade in frontier if size(clade) >= 2]

    groups.sort(key=size, reverse=True)
    groups = groups[:MAX_CLADE_GROUPS]
    placed = sum(size(clade) for clade in groups)
    entries = [
        _clade_entry(position, clade, tip_names_by_id.get(id(clade), []), basis)
        for position, clade in enumerate(groups, start=1)
    ]

    return {
        "basis": basis,
        "definition": (
            "Strongly supported clades, taken outermost first so none contains "
            "another, with any group large enough to describe the whole tree "
            "reopened into the supported clades inside it. Together they "
            "partition the tips the support justifies grouping; every other "
            "tip is unplaced."
            if basis == "strong_support" else
            "No clade in this tree clears the conventional support threshold "
            "for its scale, so these groups come from the SHAPE of the tree "
            "alone -- the largest clade split repeatedly from the root. They "
            "are not supported groupings and must not be described as clades "
            "the tree establishes."
        ),
        "groups_listed": len(entries),
        "outermost_strongly_supported_clades_total": supported_total,
        "tips_in_listed_groups": placed,
        "tips_not_in_any_listed_group": len(all_tips) - placed,
        "tip_names_truncated_per_group_at": CLADE_TIP_LIMIT,
        "groups": entries,
    }


def summarize_tree(
    newick_path: Path, tree_method: str, alrt_only: bool = False
) -> Tuple[Dict[str, Any], List[str]]:
    """Compute the tree half of the review context from a Newick file.

    Returns the summary and the full list of tip names. The names are kept out
    of the summary itself because a 2000-tip roster would swamp the prompt; the
    caller uses them to line the alignment up with the tree.
    """
    from Bio import Phylo

    with open_artifact(newick_path, "rt") as handle:
        tree = Phylo.read(handle, "newick")

    terminals = tree.get_terminals()
    internals = tree.get_nonterminals()
    if not terminals:
        raise TreeAnalysisError("The tree file contains no tips.")

    root = tree.root

    # Support carried by each tip's own parent, so a suspect sequence can be
    # reported with the support of the clade it sits in rather than leaving the
    # reader to look it up on the tree.
    parent_support: Dict[int, Optional[float]] = {}
    for clade in internals:
        value, _dual = _clade_support(clade)
        for child in clade.clades:
            parent_support[id(child)] = value

    terminal_lengths: List[float] = []
    tip_rows: List[Dict[str, Any]] = []

    tips_missing_branch_length = 0
    for clade in terminals:
        if clade.branch_length is None:
            # A tip with no branch length at all is not a tip on a zero-length
            # branch. Folding the two together (the old `or 0.0`) invented
            # zero-length branches, dragged the quantiles and the total down,
            # and handed the reviewer evidence of identical sequences that the
            # file never contained.
            tips_missing_branch_length += 1
            continue
        length = float(clade.branch_length)
        terminal_lengths.append(length)
        row: Dict[str, Any] = {
            "name": str(clade.name or ""),
            "branch_length": _round_metric(length),
            # Every comparison and sum below runs on the raw length: the outlier
            # cut is derived from unrounded values, so testing the rounded ones
            # against it let a branch sitting on the cut change sides through
            # presentation alone. Stripped from the rows before they are
            # published, so only the rounded value is ever reported.
            "_raw_branch_length": length,
        }
        support = parent_support.get(id(clade))
        if support is not None:
            row["parent_support"] = support
        tip_rows.append(row)

    # --- internal splits ---------------------------------------------------
    # A Newick file always has a root, but an unrooted binary phylogeny written
    # through one has an *artificial* root: its two children are the two sides
    # of a single bipartition, joined by one edge that the file splits in half.
    # Counting them separately invented an extra internal node, usually an
    # unsupported one (only one of the pair carries the label), and halved that
    # edge in the branch-length quantiles.
    root_children = list(root.clades)
    merged_root_children = (
        root_children
        if len(root_children) == 2 and all(child.clades for child in root_children)
        else []
    )
    merged_ids = {id(child) for child in merged_root_children}

    splits: List[Dict[str, Any]] = []
    polytomies = 0
    for clade in internals:
        # The root of a rooted-as-bifurcating tree still has degree 3 in the
        # unrooted sense, and an unrooted tree's root legitimately has three
        # children. Counting it made every ordinary tree report one polytomy it
        # does not have.
        if clade is not root and len(clade.clades) > 2:
            polytomies += 1
        if clade is root or id(clade) in merged_ids:
            # The root carries no support of its own, and the merged pair is
            # added once below as a single split.
            continue
        value, dual = _clade_support(clade)
        splits.append({
            "support": value,
            "dual": dual,
            "length": (
                float(clade.branch_length) if clade.branch_length is not None else None
            ),
        })

    if merged_root_children:
        lengths = [
            float(child.branch_length)
            for child in merged_root_children
            if child.branch_length is not None
        ]
        value, dual = _clade_support(merged_root_children[0])
        if value is None:
            value, dual = _clade_support(merged_root_children[1])
        splits.append({
            "support": value,
            "dual": dual,
            # The unrooted edge is the two halves the file wrote either side of
            # the artificial root.
            "length": float(sum(lengths)) if lengths else None,
        })

    support_values = [s["support"] for s in splits if s["support"] is not None]
    dual_pairs = [s["dual"] for s in splits if s["dual"]]
    unsupported_internals = sum(1 for s in splits if s["support"] is None)
    internal_lengths = [s["length"] for s in splits if s["length"] is not None]
    internal_missing_length = sum(1 for s in splits if s["length"] is None)

    support_type = _classify_support(
        support_values, bool(dual_pairs), tree_method, alrt_only
    )
    strong_threshold, moderate_threshold = SUPPORT_THRESHOLDS.get(
        support_type, (None, None)
    )

    scored = len(support_values)
    tip_rows.sort(key=lambda row: row["_raw_branch_length"], reverse=True)
    total_length = sum(terminal_lengths) + sum(
        float(clade.branch_length)
        for clade in internals
        if clade.branch_length is not None
    )

    # A tip is called an outlier when it is far outside the bulk of terminal
    # branches: usually a misaligned read, an off-target amplicon or a genuine
    # distant relative, all of which the user wants to look at.
    #
    # The rule runs over the POSITIVE terminal branches only. Trees here are
    # routinely dominated by zero-length tips inside clusters of identical
    # sequences; with those in the sample Q1 and Q3 are both 0, the cut collapses
    # to 0 and either everything with a length is an outlier or -- because a cut
    # of 0 was discarded as meaningless -- nothing is, on a tree that plainly has
    # one 40x branch in it. The 5x-median floor stops the opposite failure, where
    # a tightly clustered set of tiny lengths gives an IQR of ~0 and every
    # slightly longer branch is flagged.
    positive_lengths = sorted(length for length in terminal_lengths if length > 0.0)
    positive_quantiles = _quantiles(positive_lengths)
    outlier_cut = None
    outlier_count = 0
    outliers: List[Dict[str, Any]] = []
    if len(positive_lengths) >= 4:
        # Computed from the unrounded values: a tree whose bulk sits at 1e-8
        # would otherwise have its whole rule collapse to zero in the rounding.
        q1 = _quantile_at(positive_lengths, 0.25)
        q3 = _quantile_at(positive_lengths, 0.75)
        median = _quantile_at(positive_lengths, 0.5)
        outlier_cut = max(q3 + 3.0 * (q3 - q1), 5.0 * median)
        outlier_rule = (
            "max(Q3 + 3*IQR, 5*median) computed over the "
            f"{len(positive_lengths)} terminal branches with a positive length; "
            "zero-length tips and tips carrying no length are excluded from the "
            "rule itself, though any tip may be flagged by it."
        )
        if outlier_cut > 0:
            # Count them all before slicing: `outlier_long_branch_tips` is a
            # bounded sample and was previously the only number available,
            # so a tree with 200 outliers looked like a tree with 12.
            all_outliers = [
                row for row in tip_rows if row["_raw_branch_length"] > outlier_cut
            ]
            outlier_count = len(all_outliers)
            outliers = all_outliers[:TOP_N]
    else:
        outlier_rule = (
            "not computed: fewer than four terminal branches have a positive "
            "length, so there is no bulk to be an outlier from."
        )

    longest_listed = tip_rows[:TOP_N]
    longest_share = _percent(
        sum(row["_raw_branch_length"] for row in longest_listed), total_length
    )

    # `longest_listed` and `outliers` hold the same row objects, so this drops
    # the private key from every list the summary carries.
    for row in tip_rows:
        row.pop("_raw_branch_length", None)

    dual_summary = None
    if dual_pairs:
        dual_summary = {
            "nodes_meeting_both_thresholds": _dual_strong_count(dual_pairs),
            "nodes_scored": len(dual_pairs),
            "rule": DUAL_SUPPORT_RULE,
        }

    near_zero_splits = [
        s for s in splits
        if s["length"] is not None and s["length"] <= NEAR_ZERO_BRANCH_LENGTH
    ]
    longer_splits = [
        s for s in splits
        if s["length"] is not None and s["length"] > NEAR_ZERO_BRANCH_LENGTH
    ]
    unknown_length_splits = [s for s in splits if s["length"] is None]

    summary = {
        "tips": len(terminals),
        # Informative splits, not Newick nodes: the root is excluded because it
        # carries no support by definition, and an artificial binary root's two
        # children are one split counted once. Reporting the raw node count made
        # scored + unscored disagree with it and manufactured a phantom
        # unsupported node on every ordinary rooted file.
        "internal_nodes": len(splits),
        "internal_node_definition": (
            "Internal nodes are informative splits, not Newick nodes: the root is "
            "not one, and an artificial binary root's two children are one split "
            "with one edge, of length equal to both halves."
        ),
        "artificial_root_edge_merged": bool(merged_root_children),
        "total_branch_length": _round_metric(total_length),
        "non_root_polytomies": polytomies,
        # Named for what it is: the degree of the root as serialized in this
        # Newick file. It is not the viewer's rooting state -- see tree.rooting
        # for that -- and a degree of 2 or 3 implies nothing about either.
        "file_root_degree": len(root_children),
        "zero_length_terminal_branches": sum(
            1 for length in terminal_lengths if length <= 0.0
        ),
        "near_zero_terminal_branches": sum(
            1 for length in terminal_lengths if length <= NEAR_ZERO_BRANCH_LENGTH
        ),
        # Distinct from the line above: these tips carry no branch length in the
        # file at all, and are excluded from every length statistic here.
        "tips_missing_branch_length": tips_missing_branch_length,
        "zero_length_internal_branches": sum(
            1 for length in internal_lengths if length <= 0.0
        ),
        "near_zero_internal_branches": sum(
            1 for length in internal_lengths if length <= NEAR_ZERO_BRANCH_LENGTH
        ),
        "internal_nodes_missing_branch_length": internal_missing_length,
        "near_zero_branch_length_tolerance": NEAR_ZERO_BRANCH_LENGTH,
        "near_zero_definition": (
            f"near_zero_* = length <= {NEAR_ZERO_BRANCH_LENGTH}; zero_length_* = "
            "exactly zero or negative. Neither counts a branch with no length."
        ),
        "terminal_branch_length": _quantiles(terminal_lengths),
        "positive_terminal_branch_length": positive_quantiles,
        "terminal_branches_with_positive_length": len(positive_lengths),
        "internal_branch_length": _quantiles(internal_lengths),
        "support_type": support_type,
        "support_scale_note": SUPPORT_SCALE_NOTES[support_type],
        "support_nodes_scored": scored,
        "internal_nodes_without_support": unsupported_internals,
        "support_distribution": _quantiles(support_values),
        "strong_support_threshold": strong_threshold,
        "moderate_support_threshold": moderate_threshold,
        "strongly_supported_percent": (
            _percent(
                sum(1 for value in support_values if value >= strong_threshold), scored
            ) if strong_threshold is not None else None
        ),
        # Cumulative: every node at or above the moderate threshold, strongly
        # supported ones included. The old name implied a moderate-only band.
        # Absent for scales with no conventional middle band.
        "at_least_moderate_percent": (
            _percent(
                sum(1 for value in support_values if value >= moderate_threshold),
                scored,
            ) if moderate_threshold is not None else None
        ),
        "dual_support_summary": dual_summary,
        # Support read against the length of the branch it sits on. A cluster of
        # identical sequences produces many arbitrarily resolved, effectively
        # zero-length internal branches whose weak support says nothing about the
        # backbone; mixing them into one distribution hides whichever of the two
        # is actually the problem.
        "support_by_subtending_branch_length": {
            "definition": (
                "Internal splits partitioned by the length of the branch below "
                "them. Weak support on near-zero branches often reflects an "
                "unresolved cluster of near-identical sequences, and may also "
                "reflect a very shallow divergence, a near-polytomy or simply "
                "too little signal at that depth; weak support on longer "
                "branches is a poorly resolved backbone. Where "
                "jointly_well_supported_percent is present it is the figure to "
                "read: strongly_supported_percent thresholds only the UFBoot "
                "half of a dual SH-aLRT/UFBoot label."
            ),
            "tolerance": NEAR_ZERO_BRANCH_LENGTH,
            "near_zero_branches": _support_partition(
                near_zero_splits, strong_threshold, dual=bool(dual_pairs)
            ),
            "longer_branches": _support_partition(
                longer_splits, strong_threshold, dual=bool(dual_pairs)
            ),
            "branches_without_length": len(unknown_length_splits),
        },
        # Which tips group with which. Everything above describes the tree in
        # aggregate; without this the review can say a tree is well supported
        # but never what it supports.
        "clade_structure": _topology_digest(
            tree, _tip_names_by_clade(tree), strong_threshold
        ),
        "longest_terminal_branches": longest_listed,
        "longest_terminal_branches_share_of_total_percent": longest_share,
        "longest_terminal_branches_listed": len(longest_listed),
        "longest_terminal_branches_share_definition": (
            "Share of total_branch_length held by the listed tips only, not by "
            "every terminal branch."
        ),
        "outlier_branch_threshold": (
            _round_metric(outlier_cut) if outlier_cut is not None else None
        ),
        "outlier_rule": outlier_rule,
        "outlier_tip_count": outlier_count,
        "outlier_long_branch_tips": outliers,
    }
    return summary, [str(clade.name or "") for clade in terminals]


# =============================================================================
# Context assembly
# =============================================================================

def _load_json(path: Path) -> Dict[str, Any]:
    """Read a job JSON artifact in whichever form it is stored.

    Going through artifact_storage rather than a bare open() so a metadata file
    that has been gzipped by the space-reclamation job still reaches the review
    instead of silently reading as an empty dict -- which would have quietly
    dropped the tree method, and with it the support classification.
    """
    if not artifact_exists(path):
        return {}
    try:
        with open_artifact(path, "rt") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        logger.warning("Could not read %s for Claude review", path.name)
        return {}


# The trim step writes its output next to its input under a fixed pair of names.
# Only these pairs license a "columns removed by trimming" figure: any other
# before/after comparison is measuring realignment or pruning, not trimming.
_TRIM_OUTPUT_SOURCES = {
    "alignment_trimmed.fasta": "alignment_raw.fasta",
    "alignment_pruned_trimmed.fasta": "alignment_pruned_aligned.fasta",
}


def _alignment_choice(
    job_dir: Path, recomputed: bool, trimming_ran: bool
) -> Dict[str, Any]:
    """Pick the alignment to measure, and record exactly what it is.

    Three different files can be the "current" alignment and they mean three
    different things:

    * `alignment_trimmed.fasta` is what the original tree builder consumed (a
      copy of `alignment_raw.fasta` when trimming was switched off);
    * `alignment_pruned_trimmed.fasta` is what a recompute's tree builder
      consumed, after the user pruned and the pipeline realigned;
    * `alignment_pruned_aligned.fasta` is the realigned-but-untrimmed set, which
      is not a trimming product at all.

    The old code took the first of `alignment_trimmed.fasta` /
    `alignment_pruned_aligned.fasta` that existed and called whichever it found
    "the trimmed alignment", which described a realigned file as trimmed and, on
    a recomputed job, measured the original builder's alignment while reporting
    on the recomputed tree.
    """
    alignment_dir = job_dir / "alignment"

    if recomputed:
        preference = ("alignment_pruned_trimmed.fasta", "alignment_pruned_aligned.fasta")
        # Only the trimmed file. `recompute_tree()` always builds from
        # alignment_pruned_trimmed.fasta -- with trimming off the trim step still
        # copies the realigned alignment into it -- so falling back to
        # alignment_pruned_aligned.fasta means the trimmed set is unavailable,
        # not that the builder consumed the untrimmed one.
        builder_inputs = {"alignment_pruned_trimmed.fasta"}
    else:
        preference = ("alignment_trimmed.fasta", "aligned.fasta", "alignment_raw.fasta")
        builder_inputs = {"alignment_trimmed.fasta", "aligned.fasta"}
        if not trimming_ran:
            # With trimming off the pipeline copies the raw alignment forward, so
            # the two files hold the same columns and either is the builder input.
            builder_inputs.add("alignment_raw.fasta")

    chosen = next(
        (
            alignment_dir / name
            for name in preference
            if artifact_exists(alignment_dir / name)
        ),
        None,
    )
    if chosen is None and recomputed:
        # A recomputed job whose staged alignments are gone: fall back to the
        # original builder's alignment, but do not claim it built this tree.
        chosen = next(
            (
                alignment_dir / name
                for name in ("alignment_trimmed.fasta", "aligned.fasta",
                             "alignment_raw.fasta")
                if artifact_exists(alignment_dir / name)
            ),
            None,
        )
        builder_inputs = set()

    if chosen is None:
        return {"path": None}

    untrimmed_name = _TRIM_OUTPUT_SOURCES.get(chosen.name)
    untrimmed = alignment_dir / untrimmed_name if untrimmed_name else None
    if untrimmed is not None and not artifact_exists(untrimmed):
        untrimmed = None

    return {
        "path": chosen,
        "is_tree_builder_input": chosen.name in builder_inputs,
        "is_trim_output": chosen.name in _TRIM_OUTPUT_SOURCES,
        "untrimmed_counterpart": untrimmed,
    }


def _normalize_name(name: str) -> str:
    """Loose key for matching a Newick tip label to a FASTA header."""
    return re.sub(r"\s+", " ", str(name).replace("_", " ")).strip().lower()


def _restrict_to_tree(
    records: List[Tuple[str, str]], tip_names: Sequence[str]
) -> Tuple[List[Tuple[str, str]], int, int, bool]:
    """Drop alignment rows for sequences the user has pruned from the viewer.

    tree_pruned.newick is regenerated on every prune but the alignment on disk
    is not, so without this the review would report gap and identity figures for
    sequences that are no longer in the tree being reviewed.

    Returns (kept rows, rows dropped, tree tips with no alignment row, whether
    the two files share a naming scheme at all). An empty intersection means
    they do not, in which case the unfiltered set is the honest answer.
    """
    named_tips = [str(name) for name in tip_names if name]
    if not named_tips:
        return records, 0, 0, False
    wanted = {_normalize_name(name) for name in named_tips}
    available = {_normalize_name(header) for header, _ in records}
    unmatched = sum(1 for name in named_tips if _normalize_name(name) not in available)
    kept = [row for row in records if _normalize_name(row[0]) in wanted]
    if not kept:
        return records, 0, len(named_tips), False
    return kept, len(records) - len(kept), unmatched, True


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
    return {
        str(original): str(display)
        for original, display in renames.items()
        if str(original) in present and str(display).strip()
    }


# A tree where every tip was renamed would otherwise double the prompt. The cap
# is on what is *sent*, not on what is known: the validator's set of acceptable
# sequence names is built from the whole map, or a review naming the 61st
# renamed tip would be rejected for using exactly the name the viewer shows.
PROMPT_RENAME_LIMIT = 60


# Rooting modes written by tree_edit_service, normalized to lower case. TIP,
# MANUAL and OUTGROUP all mean "rooted on a named target"; the target itself is
# reported alongside. A mode outside this table is not evidence of anything --
# `""` is written when a reapply failed and "none" when no auto-rooting was
# requested, and neither says how the tree ended up rooted.
_ROOT_MODE_DESCRIPTIONS = {
    "outgroup": "rooted on the outgroup submitted with the job",
    "tip": "rooted on a tip the user chose in the viewer",
    "manual": "rooted on a target the user chose in the viewer",
    "midpoint": "midpoint-rooted",
    "midpoint_fallback": "midpoint-rooted as a fallback after the requested rooting could not be applied",
    "auto": "rooted on an automatically chosen target",
    "most_divergent_hit": "rooted on the most divergent BLAST hit",
    "original": "left at the tree builder's own rooting",
    "unrooted": "explicitly unrooted by the user",
}

_UNKNOWN_ROOTING_NOTE = (
    "Do not infer the rooting from the root's degree, from tree.file_root_degree, "
    "or from a missing outgroup: Dikarya midpoint-roots by default, so neither "
    "is evidence either way."
)


def _rooting_state(job_dir: Path, job_details: Dict[str, Any]) -> Dict[str, Any]:
    """What the viewer is actually showing, read from the persisted tree state.

    A root of degree 3 does not mean unrooted here, and neither does a null
    outgroup: Dikarya midpoint-roots by default when no outgroup was submitted,
    so the commonest tree in the system is both rooted and outgroup-less. The
    only honest source is tree_state.json.

    A state file that exists but carries no rooting keys is also not evidence.
    Every rename and prune writes this file, and older states predate the
    rooting keys entirely; treating a missing `root_mode` as "unrooted" told the
    reviewer a midpoint-rooted tree was unrooted, which is exactly the claim
    this function exists to prevent. Only an explicit mode, or an explicit
    is_midpoint_rooted of true, licenses a statement about the rooting.
    """
    state = _load_json(job_dir / "tree_state.json")
    submitted_outgroup = job_details.get("outgroup")
    base = {
        "state_known": bool(state),
        "rooting_known": False,
        "root_mode": "unknown",
        "root_target": None,
        "is_midpoint_rooted": None,
        "submitted_outgroup": submitted_outgroup,
    }

    if not state:
        base["description"] = (
            "The viewer's rooting state could not be read, so how this tree is "
            "rooted is unknown. " + _UNKNOWN_ROOTING_NOTE
        )
        return base

    recorded_mode = str(state.get("root_mode") or "").strip().lower()
    target = state.get("root_target") or state.get("root")
    midpoint = state.get("is_midpoint_rooted")

    mode = recorded_mode
    if mode not in _ROOT_MODE_DESCRIPTIONS:
        # An unrecognised or blank mode alongside an explicit midpoint flag is
        # still a midpoint-rooted tree; that is how the auto-rooting fallbacks
        # record themselves.
        mode = "midpoint" if midpoint is True else ""

    if not mode:
        base["description"] = (
            "The viewer's saved state carries no rooting information"
            + (f" (it records root_mode {recorded_mode!r}, which does not identify a "
               "rooting scheme)" if recorded_mode else "")
            + ", so how this tree is rooted is unspecified. Do not say it is "
            "unrooted, midpoint-rooted or outgroup-rooted. " + _UNKNOWN_ROOTING_NOTE
        )
        if recorded_mode:
            base["recorded_root_mode"] = recorded_mode
        return base

    description = _ROOT_MODE_DESCRIPTIONS[mode]
    if target:
        description = f"{description} ({target})"
    return {
        "state_known": True,
        "rooting_known": True,
        "root_mode": mode,
        "root_target": str(target) if target else None,
        "is_midpoint_rooted": bool(midpoint) if midpoint is not None else None,
        "submitted_outgroup": submitted_outgroup,
        "description": description,
    }


def _review_newick_path(job_dir: Path) -> Path:
    """The tree the review reads: pruned if present, else the original.

    Same precedence as tree_edit_service._editable_tree_input_path(), but
    gzip-aware. The edit paths need a plain file because they rewrite it in
    place; the review only reads, so either stored form will do.
    """
    tree_dir = job_dir / "tree"
    for candidate in (tree_dir / "tree_pruned.newick", tree_dir / "tree_original.newick"):
        if artifact_exists(candidate):
            return candidate
    raise TreeAnalysisNoTree("This job has no tree yet, so there is nothing to review.")


def _tree_metadata(job_dir: Path, recomputed: bool) -> Dict[str, Any]:
    """Builder metadata for the tree actually on screen.

    A recomputed tree was built by its own run, which writes
    tree_pruned_metadata.json; the original tree_metadata.json describes a tree
    the viewer is no longer showing. Its keys are kept as a fallback for
    anything the recompute did not record.
    """
    metadata = _load_json(job_dir / "tree" / "tree_metadata.json")
    if recomputed:
        pruned = _load_json(job_dir / "tree" / "tree_pruned_metadata.json")
        if pruned:
            merged = dict(metadata)
            merged.update(pruned)
            return merged
    return metadata


def _first_positive_int(*values: Any) -> int:
    """First value that reads as a positive integer, else 0."""
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


def _iqtree_alrt_only(tree_metadata: Dict[str, Any], job_details: Dict[str, Any]) -> bool:
    """True when IQ-TREE ran -alrt without ultrafast bootstrap.

    IQ-TREE writes dual "SH-aLRT/UFBoot" labels only when both tests ran. With
    -alrt alone the node labels are single SH-aLRT percentages, and reading
    those as bootstrap gives the reader both the wrong test and the wrong
    threshold.
    """
    method = _normalize_tree_method(
        tree_metadata.get("method") or job_details.get("tree_method")
    )
    if method != "iqtree":
        return False
    alrt = _first_positive_int(
        tree_metadata.get("alrt_replicates"), job_details.get("alrt_replicates")
    )
    bootstrap = _first_positive_int(
        tree_metadata.get("bootstrap"), job_details.get("bootstrap")
    )
    return bool(alrt) and not bootstrap


def resolve_tree_support_context(job_dir: Path) -> Dict[str, Any]:
    """Tree method and support flags for the tree the viewer displays.

    The viewer used to resolve the builder from input_info.json alone while this
    module preferred the builder's own metadata, so a recomputed or
    metadata-corrected job could give the on-screen badge and the review two
    different answers about the same tree. Both now read this.
    """
    tree_dir = job_dir / "tree"
    recomputed = artifact_exists(tree_dir / "tree_pruned.newick") and artifact_exists(
        tree_dir / "tree_pruned_metadata.json"
    )
    job_details = _load_json(job_dir / "input_info.json")
    tree_metadata = _tree_metadata(job_dir, recomputed)
    method = str(
        tree_metadata.get("method") or job_details.get("tree_method") or ""
    ).lower()
    return {
        "tree_method": method,
        "normalized_tree_method": _normalize_tree_method(method),
        "alrt_only": _iqtree_alrt_only(tree_metadata, job_details),
    }


def _join_sequence_metrics(
    rows: List[Dict[str, Any]], by_name: Dict[str, Dict[str, Any]]
) -> None:
    """Attach the deterministic per-sequence alignment numbers to tip rows.

    So a long-branch tip arrives with its ungapped length, gap composition and
    ambiguity beside it, and neither the model nor the viewer has to pair the
    two lists up by name -- or, worse, describe a branch length in prose that
    nothing checked.
    """
    joined = (
        "ungapped_length", "gap_percent", "terminal_gap_percent",
        "internal_gap_percent", "ambiguity_percent",
    )
    for row in rows:
        source = by_name.get(_normalize_name(row.get("name", "")))
        if not source:
            continue
        for field in joined:
            if field in source:
                row[field] = source[field]


def _provenance_index(job_details: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per-sequence provenance from input_info.json, keyed for name matching.

    The submission already records, for every sequence, where it came from
    (a user's own read, a MycoMap record, an NCBI hit), its accession, the
    taxon label it arrived with, its collection locality, and -- for records
    pulled in by similarity -- how close it actually was to the query. None of
    that reached the review, so a long-branch tip could only ever be reported
    as a long-branch tip, never as "the only user-submitted collection among
    twelve references" or "a reference that matched at 87%".

    Every candidate spelling of the name is indexed, because the Newick label,
    the FASTA header and the viewer's display label are not always the same
    string and any of the three may be what a metrics row carries.
    """
    entries = job_details.get("sequence_metadata")
    if not isinstance(entries, list):
        return {}
    index: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record: Dict[str, Any] = {}
        for field in _PROVENANCE_FIELDS:
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                record[field] = value.strip()
            elif value not in (None, "", []):
                record[field] = value
        # Only where the submission says the comparison was actually made. A
        # bare 0.0 left over from an entry that never ran BLAST would read as
        # a sequence sharing no identity with anything.
        if entry.get("blast_metrics_available"):
            for field in _PROVENANCE_BLAST_FIELDS:
                value = entry.get(field)
                if isinstance(value, (int, float)):
                    record[field] = value
        if not record:
            continue
        for key_field in (
            "name", "fasta_header", "display_label", "raw_fasta_header",
            "internal_id", "accession",
        ):
            candidate = entry.get(key_field)
            if isinstance(candidate, str) and candidate.strip():
                index.setdefault(_normalize_name(candidate), record)
    return index


def _join_provenance(
    rows: Sequence[Dict[str, Any]], index: Dict[str, Dict[str, Any]]
) -> None:
    """Attach provenance to every flagged row, in place."""
    if not index:
        return
    for row in rows:
        record = index.get(_normalize_name(row.get("name", "")))
        if record:
            row["provenance"] = dict(record)


def _genus_of(taxon: Any) -> Optional[str]:
    """First word of a taxon label, which is the genus when it is a binomial.

    A label, not a determination -- see the note published beside it.
    """
    text = str(taxon or "").strip()
    if not text:
        return None
    first = text.split()[0]
    return first if first[:1].isalpha() else None


def _provenance_summary(
    index: Dict[str, Dict[str, Any]], tip_names: Sequence[str]
) -> Dict[str, Any]:
    """What the current tree's sequences are, in aggregate.

    Restricted to tips actually in the displayed tree, so a job whose viewer
    pruning removed every one of its references does not still report them.
    """
    records = [
        record for record in (
            index.get(_normalize_name(name)) for name in tip_names if name
        ) if record
    ]
    if not records:
        return {
            "sequences_with_metadata": 0,
            "note": (
                "This job carries no per-sequence provenance, so nothing is "
                "known about where its sequences came from. Do not infer it "
                "from the names."
            ),
        }

    by_source: Dict[str, int] = {}
    for record in records:
        key = str(record.get("source") or "unspecified")
        by_source[key] = by_source.get(key, 0) + 1
    by_hit_source: Dict[str, int] = {}
    for record in records:
        hit = record.get("hit_source")
        if hit:
            by_hit_source[str(hit)] = by_hit_source.get(str(hit), 0) + 1

    identities = [
        float(record["identity"]) for record in records
        if isinstance(record.get("identity"), (int, float))
    ]
    genera = {
        genus for genus in (_genus_of(record.get("taxon")) for record in records)
        if genus
    }
    taxa = {
        str(record["taxon"]) for record in records if record.get("taxon")
    }

    low_identity = sorted(
        (
            {
                "taxon": record.get("taxon"),
                "accession": record.get("accession"),
                "identity": record["identity"],
                "query_cover": record.get("query_cover"),
            }
            for record in records
            if isinstance(record.get("identity"), (int, float))
            and record["identity"] < LOW_REFERENCE_IDENTITY_PERCENT
        ),
        key=lambda row: row["identity"],
    )

    summary: Dict[str, Any] = {
        "sequences_with_metadata": len(records),
        "tips_in_tree": len([name for name in tip_names if name]),
        "by_source": by_source,
        "by_hit_source": by_hit_source or None,
        "distinct_taxon_labels": len(taxa),
        "distinct_genus_labels": len(genera),
        "label_note": (
            "taxon, genus and location are USER- AND DATABASE-SUPPLIED LABELS "
            "carried in with each sequence. They are not verified "
            "determinations and this analysis did not check them. Use them to "
            "describe what the dataset claims to contain and to point out "
            "where the tree disagrees with those claims; never present one as "
            "an identification, and never identify anything yourself."
        ),
    }
    if identities:
        summary["reference_identity_to_query_percent"] = _quantiles(identities)
        summary["sequences_with_identity_metrics"] = len(identities)
        summary["identity_definition"] = (
            "Percent identity recorded when the sequence was pulled into the "
            "job by similarity search, against the query that retrieved it. "
            "It is not a distance measured on this alignment or this tree."
        )
    if low_identity:
        summary["references_below_90_percent_identity"] = low_identity[:TOP_N]
        summary["references_below_90_percent_identity_total"] = len(low_identity)
    return summary


# A label carrying one of these is an explicit statement that the sequence was
# NOT identified to species, so finding two of them in different groups says
# nothing at all -- two records both labelled "sp." need not be the same thing.
_UNDETERMINED_LABEL_RE = re.compile(r"\b(?:sp|spp|cf|aff|indet)\.?(?:\s|$)", re.I)

# GenBank placeholders that read as a binomial but name nothing. Two records
# both labelled "Environmental Sample" landing in different groups is not a
# taxonomic inconsistency, and one was being reported as one.
_PLACEHOLDER_LABEL_RE = re.compile(
    r"^(?:environmental\s+sample|uncultured\b|unidentified\b|unclassified\b|"
    r"fungal\s+(?:sp|endophyte|sample)\b|no\s+match\b)",
    re.I,
)


def _determinate_taxon(taxon: Any) -> Optional[str]:
    """A taxon label specific enough that finding it twice means something.

    Genus alone is not it. An earlier version keyed this check on the genus and
    reported, of a dataset that was entirely Tyromyces, that Tyromyces appeared
    in seven groups -- true, trivial, and exactly the kind of padding the prompt
    tells the reviewer not to produce. A binomial is the level at which two
    records in unrelated parts of the tree is a real inconsistency.
    """
    text = re.sub(r"\s+", " ", str(taxon or "")).strip()
    if not text or len(text.split()) < 2:
        return None
    if _UNDETERMINED_LABEL_RE.search(text) or _PLACEHOLDER_LABEL_RE.match(text):
        return None
    return text


def _labels_split_across_clades(
    clade_structure: Dict[str, Any], index: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Taxon labels whose members land in more than one supported group.

    The single most useful thing the topology and the labels can say together:
    a named species sitting in two unrelated supported groups is either a
    misidentification in the dataset or a genuinely non-monophyletic taxon, and
    both are worth the user's attention. Computed only from groups whose
    membership the SUPPORT justifies -- on a shape-only digest a label appearing
    in two groups means nothing at all.
    """
    if not index or clade_structure.get("basis") != "strong_support":
        return []
    placement: Dict[str, Set[str]] = {}
    for group in clade_structure.get("groups", []):
        if group.get("tip_names_truncated"):
            # The listed names are a sample, so an absence here is not evidence
            # that the label is missing from the group.
            continue
        for name in group.get("tip_names", []):
            record = index.get(_normalize_name(name))
            label = _determinate_taxon((record or {}).get("taxon"))
            if label:
                placement.setdefault(label, set()).add(group["id"])
    split = [
        {"taxon_label": label, "groups": sorted(ids), "group_count": len(ids)}
        for label, ids in placement.items() if len(ids) > 1
    ]
    split.sort(key=lambda row: (-row["group_count"], row["taxon_label"]))
    return split[:MAX_SPLIT_LABELS]


def build_context(
    job_dir: Path, *, displayed_names_out: Optional[set] = None
) -> Dict[str, Any]:
    """Assemble every number the review is based on.

    The tree is summarized first so the alignment can be restricted to the tips
    that are actually in it. Which alignment that is, and whether it is the one
    the tree builder consumed, is reported rather than assumed: after a
    recompute the displayed tree came from the realigned pruned set, and after a
    viewer prune the alignment describes fewer sequences than the builder saw.

    `displayed_names_out`, when given, receives the tip names as the viewer
    shows them (renames applied), which is what a returned review's sequence
    names have to resolve against.
    """
    job_details = _load_json(job_dir / "input_info.json")

    newick_path = _review_newick_path(job_dir)
    recomputed = newick_path.name == "tree_pruned.newick" and artifact_exists(
        job_dir / "tree" / "tree_pruned_metadata.json"
    )
    tree_metadata = _tree_metadata(job_dir, recomputed)
    tree_method = str(
        tree_metadata.get("method") or job_details.get("tree_method") or ""
    ).lower()
    alrt_only = _iqtree_alrt_only(tree_metadata, job_details)

    tree, all_tip_names = summarize_tree(newick_path, tree_method, alrt_only)
    tree["source_file"] = newick_path.name
    tree["reflects_viewer_pruning"] = newick_path.name == "tree_pruned.newick"
    tree["rebuilt_by_recompute"] = recomputed
    tree["rooting"] = _rooting_state(job_dir, job_details)

    trimming_details = job_details.get("trimming_details") or {}
    trimming_method = str(
        trimming_details.get("method")
        or job_details.get("trimming_method")
        or "none"
    )
    trimming_ran = trimming_method.strip().lower() not in ("", "none")

    choice = _alignment_choice(job_dir, recomputed, trimming_ran)
    alignment_path = choice.get("path")
    if alignment_path is None:
        raise TreeAnalysisError(
            "This job has no aligned FASTA, so there is nothing to review."
        )

    records = _read_alignment(alignment_path)
    sequences_in_source = len(records)
    records, excluded, unmatched_tips, names_matched = _restrict_to_tree(
        records, all_tip_names
    )
    per_sequence_rows: List[Dict[str, Any]] = []
    alignment = summarize_alignment(records, per_sequence_out=per_sequence_rows)

    by_name = {_normalize_name(row["name"]): row for row in per_sequence_rows}
    _join_sequence_metrics(tree["longest_terminal_branches"], by_name)
    _join_sequence_metrics(tree["outlier_long_branch_tips"], by_name)

    # Where each sequence came from, joined onto every list that names one.
    # "Three tips have long terminal branches" and "the three long-branch tips
    # are the only user-submitted collections in the job" are different
    # findings, and only the second one tells the user what to do next.
    provenance_index = _provenance_index(job_details)
    for flagged in (
        tree["longest_terminal_branches"],
        tree["outlier_long_branch_tips"],
        alignment["gappiest_sequences"],
        alignment["most_internally_gapped_sequences"],
        alignment["most_ambiguous_sequences"],
        alignment["shortest_sequences"],
    ):
        _join_provenance(flagged, provenance_index)

    excerpts = build_alignment_excerpt(records, per_sequence_rows)
    if excerpts:
        alignment["excerpts"] = excerpts

    split_labels = _labels_split_across_clades(
        tree["clade_structure"], provenance_index
    )
    if split_labels:
        tree["clade_structure"]["taxon_labels_in_multiple_groups"] = split_labels
        tree["clade_structure"]["taxon_labels_in_multiple_groups_note"] = (
            "Taxon labels, taken from the submitted metadata, whose members "
            "fall in more than one strongly supported group. Computed only "
            "over groups whose member list was not truncated, and only over "
            "labels determined to species: a label reading sp., cf. or aff. is "
            "an explicit statement that the sequence was not identified, so two "
            "of them in different groups mean nothing. A label is not a "
            "determination, so this is evidence that the dataset's labels and "
            "its tree disagree, not proof of non-monophyly."
        )

    alignment["source_file"] = alignment_path.name
    # Named for what the calculation actually establishes rather than for the
    # commonest cause. Viewer pruning is the usual one, but a tree builder can
    # drop a record and a naming mismatch can hide one.
    alignment["alignment_is_tree_builder_input"] = bool(choice["is_tree_builder_input"])
    alignment["alignment_is_trim_output"] = bool(choice["is_trim_output"])
    alignment["alignment_restricted_to_current_tips"] = excluded > 0
    alignment["alignment_names_matched_tree"] = names_matched
    alignment["sequences_in_source_file"] = sequences_in_source
    alignment["sequences_in_current_tree"] = len(all_tip_names)
    alignment["alignment_sequences_absent_from_current_tree"] = excluded
    alignment["tree_tips_unmatched_in_alignment"] = unmatched_tips
    if choice["is_tree_builder_input"]:
        alignment["sequences_in_builder_alignment"] = sequences_in_source
    alignment["scope_note"] = _alignment_scope_note(
        builder_input=bool(choice["is_tree_builder_input"]),
        restricted=excluded > 0,
        source_file=alignment_path.name,
        displayed=alignment["sequences"],
        in_source=sequences_in_source,
    )

    if trimming_ran and choice["is_trim_output"] and choice["untrimmed_counterpart"]:
        # Only a genuine before/after pair from the trim step. A realigned or
        # pruned alignment differs from the original for reasons that have
        # nothing to do with trimming, and reporting that difference as columns
        # removed by trimAl was simply a wrong number.
        try:
            raw_records = _read_alignment(choice["untrimmed_counterpart"])
            raw_columns = max((len(seq) for _, seq in raw_records), default=0)
            alignment["trimming_measured_from"] = choice["untrimmed_counterpart"].name
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
        tree["viewer_renames_original_to_displayed"] = dict(
            list(renames.items())[:PROMPT_RENAME_LIMIT]
        )
    if displayed_names_out is not None:
        for name in all_tip_names:
            if not name:
                continue
            # Both spellings are acceptable: the model is told to use the
            # displayed name, but a review cached before a rename, or one that
            # quotes the Newick label, is naming a real tip either way.
            displayed_names_out.add(str(name))
            if name in renames:
                displayed_names_out.add(str(renames[name]))

    pipeline = {
        "aligner": job_details.get("aligner") or job_details.get("alignment_method"),
        "trimming_method": trimming_method,
        "trimming_ran": trimming_ran,
        "trimmed_terminal_overhangs": bool(
            (trimming_details.get("terminal_overhang_trim") or {}).get("enabled")
        ),
        "tree_method": tree_metadata.get("method") or job_details.get("tree_method"),
        "tree_method_normalized": _normalize_tree_method(tree_method),
        "substitution_model": tree_metadata.get("model") or job_details.get("tree_model"),
        # FastTree ignores the replicate count entirely and emits SH-like local
        # support instead. Passing the requested number through unqualified told
        # the reviewer 1000 bootstraps had run when none had.
        "bootstrap_replicates": _effective_bootstrap(
            tree_metadata.get("method") or job_details.get("tree_method"),
            tree_metadata.get("bootstrap") or job_details.get("bootstrap"),
        ),
        "alrt_replicates": (
            tree_metadata.get("alrt_replicates") or job_details.get("alrt_replicates")
        ),
        "iqtree_support_mode": (
            "sh_alrt_only" if alrt_only else None
        ),
        "tree_rebuilt_after_pruning": recomputed,
        "outgroup": job_details.get("outgroup"),
        "blast_enabled": bool(job_details.get("run_blast")),
        "orientation_enabled": bool(job_details.get("run_orient")),
    }

    return {
        "pipeline": pipeline,
        "provenance": _provenance_summary(provenance_index, all_tip_names),
        "alignment": alignment,
        "tree": tree,
    }


def _alignment_scope_note(
    *, builder_input: bool, restricted: bool, source_file: str,
    displayed: int, in_source: int,
) -> str:
    """One sentence saying what these alignment numbers actually describe."""
    if builder_input and not restricted:
        return (
            f"These statistics describe {source_file}, the alignment the tree "
            "builder consumed, with every one of its sequences still in the tree."
        )
    if builder_input and restricted:
        return (
            f"These statistics were recalculated over the {displayed} of "
            f"{in_source} sequences of {source_file} that are still in the "
            "displayed tree. The tree's branch support was estimated by the "
            "builder on the full alignment, so support values and alignment "
            "statistics do not describe the same set of sequences; do not "
            "present these numbers as what the tree builder saw."
        )
    return (
        f"These statistics describe {source_file}, which is NOT the alignment "
        "this tree's builder consumed"
        + (f" (restricted to the {displayed} of {in_source} sequences still in "
           "the displayed tree)" if restricted else "")
        + ". Branch support came from the builder's own alignment; treat these "
        "figures as a description of the current sequences, not of the tree's input."
    )


def _effective_bootstrap(tree_method: Any, requested: Any) -> Any:
    """What the tree builder actually did with the requested replicate count.

    FastTree has no bootstrap mode; it reports SH-like local support regardless
    of what was asked for. MrBayes and neighbour-joining ignore it too. Reporting
    the request as though it were performed misleads both the reviewer and anyone
    reading the metrics, so it is replaced by an explicit statement of what
    actually happened.

    The method goes through the same normalization as the support classifier, so
    a job recorded as "FastTree2" or "IQ-TREE 2" is recognised here as well.
    Matching on the raw string meant an aliased spelling silently fell through
    and reported FastTree's ignored replicate count as though it had run.
    """
    method = _normalize_tree_method(tree_method)
    instead = {
        "fasttree": "FastTree reports SH-like local support instead",
        "mrbayes": "MrBayes reports Bayesian posterior probabilities from MCMC",
        "nj": "neighbour-joining produces no node support",
    }.get(method)
    if instead is None:
        return requested
    if requested:
        return f"not run ({requested} requested; {instead})"
    return None


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

You are given precomputed statistics for one job: the pipeline settings, where \
each sequence came from, the alignment, and the tree. You are not given the \
sequences themselves, apart from the narrow windows described under ALIGNMENT \
EXCERPTS below. Every claim you make must therefore follow from a number you \
were given, or from what one of those windows plainly shows. Do not guess at \
taxonomy, sequence identity, or what a named sequence is - the names and taxon \
labels are user- and database-supplied, not verified determinations.

Judge the analysis on whether its conclusions can be trusted, not on whether \
it followed a textbook procedure. What matters:

- Is there enough signal? A short or largely invariant alignment cannot resolve \
  a tree no matter how good the settings are. The parsimony-informative \
  column count, or its estimate, is the number that decides this.
- Is the alignment sound? Very gappy columns, ragged ends, or a handful of \
  sequences far gappier than the rest usually mean sequences of unequal length \
  or off-target reads rather than real indels.
- Is the support real? Say plainly what the support scale is and what it does \
  and does not establish. Interpret the values on their own scale.
- Are individual sequences suspect? Long terminal branches, high ambiguity, and \
  unusually short sequences are the ones the user should look at, by name. Say \
  what they are as well as what is wrong with them: a user's own collection on \
  a long branch and a distant reference on a long branch mean different things.
- What does the tree actually group? tree.clade_structure is the only thing you \
  are given about which tips sit together. Where the dataset's own labels \
  disagree with those groups, that is usually the most useful thing you can \
  tell the user.
- Do the settings fit the data? Note a mismatch only when it plausibly changed \
  the result.

Be direct and specific. Cite the actual numbers. A clean dataset should be told \
it is clean in a sentence or two rather than padded with hedges; a broken one \
should be told exactly what is broken and what to do about it. Do not recommend \
a step the pipeline already performed, and do not suggest generic best practices \
that the numbers give no reason to raise.

RULES FOR USING THE NUMBERS

Never invent a count. A quantile tells you where a boundary sits, not how many \
sequences lie on either side of it. From a median you may say "at least half"; \
from q3, "at least a quarter" or "at least three quarters" as appropriate. \
Writing "48 of 49 sequences are shorter than X" is only permitted when a field \
actually reports that count. The same applies to percentages: convert one to a \
count only when you were given the denominator it applies to, and say which.

Missing data is not misalignment, and neither is a gap. The aligner and every \
tree builder here treat gap and missing characters as missing data, not as \
evidence. Low occupancy therefore reduces the signal available - fewer columns \
carrying usable comparisons - but it does not by itself bias substitution-model \
fitting or inflate branch lengths, and you must not claim that it does merely \
because gaps are present. Keep these four apart, because the fixes differ:

- Terminal missing data / short fragments: alignment.mean_terminal_gap_percent, \
  the terminal_gap_percent on a row, alignment.shortest_sequences. A short \
  barcode padded at both ends is incomplete, not misaligned.
- Low overlap: alignment.pairwise_overlap. Two sequences that share few \
  comparable columns cannot be compared reliably however identical they look \
  over those columns.
- Internal gaps / possible misalignment: internal_gap_percent and \
  alignment.most_internally_gapped_sequences. Gaps between a sequence's own \
  first and last residue are indels or genuine misalignment, and are the ones \
  worth acting on.
- Real divergence: long terminal branches and low pairwise identity with ample \
  overlap.

Alignment scope. alignment.scope_note says what the alignment statistics \
describe. When alignment.alignment_restricted_to_current_tips is true, they \
were recalculated over only the tips still displayed, while the tree's branch \
support was estimated by the tree builder on the full alignment it was given \
(alignment.sequences_in_builder_alignment, when known). Say so rather than \
presenting post-pruning statistics as what the builder saw, and do not compute \
a support value's "expected" alignment from these numbers. If \
alignment.alignment_is_tree_builder_input is false, these statistics are not \
the builder's input at all.

Trimming. Only report columns as removed by trimming when \
alignment.columns_removed_by_trimming is present; it is published only for a \
genuine trim step, from the named file in alignment.trimming_measured_from. \
Its absence means no trimming ran, or that the current alignment is a \
realigned or pruned set whose column count differs for reasons that are not \
trimming.

Column statistics are exact only when alignment.column_metrics_are_estimates \
is false. When it is true the alignment was sampled: use the `*_estimated` \
fields, and treat every percentage as a percentage of the \
alignment.columns_scored sample columns rather than of the whole alignment. \
Say the figure is an estimate from alignment.columns_scored of \
alignment.columns columns. Never state an estimate as a count.

Occupancy is non-gap occupancy. An ambiguous base such as N occupies its \
column but carries no state, so high occupancy is not evidence of confidently \
called bases; the ambiguity fields are what speak to that. A column holding \
fewer than four unambiguous residues cannot separate two clades of two, so it \
is counted as invariant while informing nothing: prefer \
parsimony_informative_percent_of_columns_with_at_least_4_unambiguous_residues \
and invariant_percent_of_columns_with_at_least_4_unambiguous_residues on a \
gappy alignment, and name the denominator you used. Those denominators count \
unambiguous A/C/G/T residues only, so a column of Ns is outside them.

Rooting is given in tree.rooting, taken from the viewer's own state. Only make \
a statement about the rooting - midpoint-rooted, rooted on an outgroup, \
explicitly unrooted - when tree.rooting.rooting_known is true; otherwise say \
the rooting is unspecified. A null pipeline.outgroup does not mean the tree is \
unrooted (Dikarya midpoint-roots by default), and neither does \
tree.file_root_degree, which describes only how the Newick file happens to be \
written.

Branch lengths. tree.zero_length_terminal_branches are branches whose length is \
present and zero; tree.near_zero_* count branches at or below \
tree.near_zero_branch_length_tolerance. tree.tips_missing_branch_length are tips \
carrying no length in the file at all, are evidence of nothing, and are excluded \
from every length statistic. tree.outlier_rule states exactly how the long-branch \
cut was derived; quote it rather than describing the cut as a plain IQR rule.

Internal nodes are informative splits: the root is not one, and an artificial \
binary root's two children are counted once as a single edge. \
tree.support_by_subtending_branch_length separates support on effectively \
zero-length branches - often arbitrary resolution inside a cluster of \
near-identical sequences, though a very shallow divergence or a near-polytomy \
produces the same pattern - from support on branches with real length. Use it \
before calling a tree poorly supported: weak support confined to the near-zero \
partition is a different finding from a weakly supported backbone, and you \
should name which one this is. Do not assert that near-zero branches ARE \
identical sequences unless alignment.identical_sequence_group_count says so. \
Within each partition, read jointly_well_supported_percent where it is present; \
strongly_supported_percent thresholds only the UFBoot half of a dual label.

Interpret support on the scale named by tree.support_type and explained by \
tree.support_scale_note, which comes from the tree builder that produced the \
file. Do not re-derive the scale from the size of the numbers: bootstrap \
values of 0, 1 or 0.95 are still bootstrap. In particular:

- BS is the classical non-parametric bootstrap: >=70 moderate, >=95 strong.
- UFBOOT is IQ-TREE's ultrafast bootstrap. Its conventional cutoff is >=95. It \
  is anti-conservative relative to the classical bootstrap, so do NOT apply the \
  70 "moderate" convention to it or call a UFBoot of 80 moderately supported in \
  the bootstrap sense; below 95 it is simply not well supported.
- ALRT is an SH-aLRT branch test (>=80 conventional), not a bootstrap.
- ALRT_UFBOOT is the dual SH-aLRT/UFBoot label, well supported at SH-aLRT >= 80 \
  AND UFBoot >= 95; tree.dual_support_summary counts the nodes meeting both.
- SH is FastTree's local support and PP is a Bayesian posterior probability; \
  neither is a bootstrap proportion.
Where a threshold is published (tree.strong_support_threshold, \
tree.moderate_support_threshold), use it; a null moderate threshold means that \
scale has no conventional middle band, so do not invent one.

Every list of named sequences is truncated. Where a total is given - \
tree.outlier_tip_count, alignment.identical_sequence_group_count, \
alignment.sequences_in_identical_groups_total, a group's own `count`, a \
group's names_truncated flag - quote the total and describe the named rows \
as examples.

TOPOLOGY

tree.clade_structure lists groups of tips, each with an id such as C3. Read \
tree.clade_structure.basis first and obey it:

- `strong_support`: the groups are strongly supported clades - each clears the \
  conventional threshold for this support scale and none contains another. \
  These are real, citable groupings. tips_not_in_any_listed_group is how much \
  of the tree the support does not place; a large number there is a finding in \
  itself.
- `topology_only`: NOTHING in this tree clears its support threshold and the \
  groups come from the shape of the file alone. Do not call them clades, do not \
  say the tree groups anything, and do not name their members as related. Report \
  that the tree is unresolved and use the groups only to describe the shape.

Membership lists are truncated at tip_names_truncated_per_group_at. When \
tip_names_truncated is true the names you see are examples, and a tip's absence \
from the list is not evidence it is outside the group. `tips` is the group's \
true size; groups_listed is how many groups you were shown, and \
outermost_strongly_supported_clades_total counts the supported clades found \
before any oversized one was reopened - do not report either as a count of \
clades in the tree.

tree.clade_structure.taxon_labels_in_multiple_groups, where present, names \
taxon labels whose members land in more than one supported group. Report it as \
a disagreement between the dataset's labels and its tree - a possible \
misidentification among the submitted sequences, or a genuinely non-monophyletic \
taxon - and say which groups are involved. Never resolve it: do not decide which \
placement is correct, and do not re-identify a sequence. Its absence is not \
evidence that the labels agree with the tree; it is computed only over the \
groups whose membership was listed in full.

WHERE THE SEQUENCES CAME FROM

The `provenance` block, and the `provenance` field on individual flagged rows, \
describe each sequence's origin: `source`/`hit_source` (a user's own submission \
versus a record pulled from MycoMap or NCBI), `accession`, `taxon`, `location`, \
and for retrieved records the `identity` and `query_cover` of the search that \
retrieved them. Use it to make a finding actionable - which of the suspect \
sequences are the user's own material and which are references, whether a clade \
is references only, whether a reference was pulled in at low identity \
(provenance.references_below_90_percent_identity) and may not belong in the \
analysis at all.

`identity` is the percent identity from the search that retrieved the record, \
against the query that retrieved it. It is NOT a distance measured on this \
alignment or this tree, so never compare it with a branch length, present it as \
alignment identity, or contrast it with mean_pairwise_identity_percent.

Obey provenance.label_note. `taxon`, its genus and `location` are labels the \
sequences arrived with; this analysis verified none of them. You may say the \
dataset's labels are inconsistent with its tree. You may not say what anything \
is, confirm or reject an identification, or infer relatedness from a shared \
label. When provenance.sequences_with_metadata is 0, nothing is known about \
origin and you must not infer it from the names.

ALIGNMENT EXCERPTS

alignment.excerpts, when present, is the only raw sequence you are given: at \
most two windows of at most a couple of hundred columns, chosen around the \
largest interior gap of the most internally gapped sequences. Each window \
carries the `flagged` row and `contrast` rows drawn from the least internally \
gapped sequences.

Use them for exactly one judgement: whether the flagged row's gap looks like a \
plausible indel - a clean block, with the row in register with its neighbours \
either side of it - or like a row that has slipped out of register, where its \
residues no longer line up with the contrast rows across the window. That \
distinction changes the advice, and no statistic in this context can make it.

Nothing else may be drawn from a window. It is a fragment chosen for being \
unusual, so do not count columns, estimate a percentage, judge overall \
alignment quality, compare sequences base by base, or describe the contrast \
rows as representative. Every quantitative claim still comes from the alignment \
statistics. The absence of alignment.excerpts means no sequence had interior \
gaps worth showing, which is mildly good news, not a gap in the evidence.

RATING

Choose the rating that matches your own concerns, and keep the two consistent:

- Do not rate an analysis `strong` while raising a high-severity concern. If \
  something is high severity and unresolved, the tree is not strong.
- Rate an analysis `unreliable` only when you can name a concrete high-severity \
  reason it should not be interpreted as it stands.
- `usable` may carry a high-severity concern when that problem is clearly \
  localized - specific sequences or one clade - and the rest of the tree remains \
  interpretable. Say which part is affected and which part is not.
- `caution` is for real problems that change how the tree should be read \
  without confining themselves to a few rows.

Where you name a sequence, use its name verbatim so the user can find it, and \
name only sequences that appear in the metrics you were given. If \
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
            "description": (
                "One sentence stating the verdict. Hard limit 140 characters; "
                "a longer headline is rejected."
            ),
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


# Room for the request to notice its own timeout and release cleanly before
# anything else may reclaim what it holds.
SLOT_GRACE_SECONDS = 30
# Retries the API backend is allowed. It buys survival of a 529 overload without
# making the user click again; `_max_transport_seconds()` is what keeps the Redis
# guards' lifetimes honest about the wall clock that costs.
CLAUDE_API_MAX_RETRIES = 1
SLOT_REGISTRY_KEY = "dikarya:claude_review:in_flight"
FINGERPRINT_LOCK_PREFIX = "dikarya:claude_review:lock:"

# Delete only if we still own the value. A plain DEL would let a request that
# overran its TTL delete the lock a *different* request has since taken.
_RELEASE_IF_OWNED = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _max_transport_seconds() -> int:
    """Worst-case wall clock of one review call, whichever backend is selected.

    The CLI path is a single `subprocess.run` bounded by the timeout. The API
    path is not: `anthropic.Anthropic(max_retries=1)` applies the timeout PER
    ATTEMPT, so a first attempt that times out or 529s is followed by a second
    with a full budget of its own. A lock lifetime of one timeout therefore
    expired while an API review was still legitimately running, and the next
    request took the same fingerprint lock and started a second billed review of
    the same tree -- the exact duplicate this guard exists to prevent.
    """
    timeout = int(Config.CLAUDE_REVIEW_TIMEOUT_SECONDS)
    return timeout * (1 + CLAUDE_API_MAX_RETRIES) if _backend() == "api" else timeout


def _slot_ttl_seconds() -> int:
    """How long a slot or lock may be held before it is treated as abandoned."""
    return _max_transport_seconds() + SLOT_GRACE_SECONDS


@dataclass
class _Slot:
    """Handle for this request's entry in the Redis concurrency registry."""

    key: str
    token: str
    client: Any

    def release(self) -> None:
        try:
            self.client.zrem(self.key, self.token)
        except Exception:  # pragma: no cover - never fail a served response
            logger.warning("Could not release Claude review concurrency slot")


@dataclass
class _FingerprintLock:
    """Handle for the per-fingerprint in-progress lock."""

    key: str
    token: str
    client: Any

    def release(self) -> None:
        try:
            self.client.eval(_RELEASE_IF_OWNED, 1, self.key, self.token)
        except Exception:  # pragma: no cover - never fail a served response
            logger.warning("Could not release Claude review fingerprint lock")


def _acquire_slot() -> Optional[_Slot]:
    """Take one of the global review slots, or raise if they are all in use.

    Flask-Limiter caps a single client; it cannot stop eight different users
    from starting eight reviews and filling every Gunicorn request slot at once.
    Returns None when Redis is unreachable, which degrades to unlimited rather
    than blocking reviews outright.

    A sorted set of request tokens scored by acquisition time, rather than a
    shared integer with a TTL. The integer had three failure modes that all
    showed up as the feature refusing to work: a *rejected* request refreshed
    the key's expiry, so a busy site could hold a leaked count alive forever; a
    worker killed mid-review leaked its increment until that expiry; and a
    decrement arriving after the key expired drove the counter negative, which
    then took several requests to climb back to zero. Entries here expire
    individually by score, so a leak clears itself one timeout after the request
    that caused it and no other request can prolong it.
    """
    limit = max(1, int(Config.CLAUDE_REVIEW_MAX_CONCURRENT))
    ttl = _slot_ttl_seconds()
    token = f"{time.time():.6f}:{os.getpid()}:{uuid4().hex}"
    try:
        from app.workers.queue import get_redis_connection

        client = get_redis_connection()
        now = time.time()
        pipeline = client.pipeline()
        pipeline.zremrangebyscore(SLOT_REGISTRY_KEY, "-inf", now - ttl)
        pipeline.zadd(SLOT_REGISTRY_KEY, {token: now})
        pipeline.zcard(SLOT_REGISTRY_KEY)
        # Housekeeping only: every live holder is younger than one TTL, so this
        # cannot drop a slot that is still in use, and it keeps an idle key from
        # lingering after the last review finishes.
        pipeline.expire(SLOT_REGISTRY_KEY, ttl * 2)
        held = int(pipeline.execute()[2])
        if held > limit:
            client.zrem(SLOT_REGISTRY_KEY, token)
            raise TreeAnalysisUnavailable(
                "Claude is reviewing other trees right now. Try again in a moment."
            )
        return _Slot(key=SLOT_REGISTRY_KEY, token=token, client=client)
    except TreeAnalysisUnavailable:
        raise
    except Exception as exc:
        logger.warning("Claude review concurrency guard unavailable: %s", exc)
        return None


def _acquire_fingerprint_lock(key: str) -> Optional[_FingerprintLock]:
    """Claim the right to run this exact review, or raise if it is already running.

    Two requests for the same job and the same numbers -- a double click, a
    reload while the first call is still out, two people on a shared link --
    produced two identical Claude calls, spent two of the day's reviews and
    wrote the same result twice. The global slot ceiling did not stop it: two
    concurrent reviews are within the ceiling.

    Redis unreachable returns None, matching the concurrency guard: the duplicate
    protection is a cost optimisation, and losing it must not take the feature
    down. The daily spending guard remains fail-closed.
    """
    lock_key = FINGERPRINT_LOCK_PREFIX + key
    ttl = _slot_ttl_seconds()
    token = uuid4().hex
    try:
        from app.workers.queue import get_redis_connection

        client = get_redis_connection()
        if not client.set(lock_key, token, nx=True, ex=ttl):
            raise TreeAnalysisInProgress(ttl)
        return _FingerprintLock(key=lock_key, token=token, client=client)
    except TreeAnalysisInProgress:
        raise
    except Exception as exc:
        logger.warning("Claude review duplicate guard unavailable: %s", exc)
        return None


def _build_user_message(context: Dict[str, Any]) -> str:
    return (
        "Review this phylogenetic analysis.\n\n"
        "```json\n"
        + json.dumps(context, indent=2, sort_keys=True, default=str)
        + "\n```\n"
    )


def _unescape_literal_newlines(value: Any, _key: str = "") -> Any:
    """Turn literal ``\\n`` sequences in model output into real newlines.

    When the model fills a long multi-paragraph field through the structured
    output tool it sometimes escapes the newlines a second time, so the JSON
    string carries the two characters backslash-n rather than a newline. Those
    reach the viewer verbatim and render as "\\n\\n" mid-sentence. Observed on
    `summary`, the only field long enough to hold paragraph breaks, but applied
    to the other prose fields too so a prompt change cannot reintroduce it.

    `name` is skipped: it carries a taxon/accession label copied from the tree,
    and a label like ``Russula \\name`` would be corrupted by this rewrite.
    Only the whitespace escapes are touched for the same reason.
    """
    if isinstance(value, str):
        if _key == "name":
            return value
        return (value.replace("\\r\\n", "\n")
                     .replace("\\n", "\n")
                     .replace("\\t", "\t"))
    if isinstance(value, list):
        return [_unescape_literal_newlines(v, _key) for v in value]
    if isinstance(value, dict):
        return {k: _unescape_literal_newlines(v, k) for k, v in value.items()}
    return value


def _enum_values(*path: str) -> List[str]:
    """Pull an enum out of RESPONSE_SCHEMA so the validator cannot drift from it."""
    node: Any = RESPONSE_SCHEMA
    for key in path:
        node = node[key]
    return list(node["enum"])


def _require_object(value: Any, fields: Sequence[str], where: str) -> None:
    """Every named field must be present and a string."""
    if not isinstance(value, dict):
        raise TreeAnalysisUpstreamError(
            f"Claude's review had a malformed {where} entry."
        )
    for field in fields:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise TreeAnalysisUpstreamError(
                f"Claude's review had a malformed {where} entry ({field})."
            )


HEADLINE_MAX_CHARACTERS = 140


def _validate_review(
    review: Any, displayed_names: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Enforce the response contract before a reply is cached or rendered.

    Presence of the required keys was the only check here, so a reply carrying
    `overall_rating: "excellent"`, a list where prose belonged, or a concern
    with no severity was stored and shown. A rating outside the enum then fell
    through the viewer's lookup onto the "usable" styling, which turns a
    malformed answer into a favourable one. Structurally invalid values are
    rejected rather than coerced.

    `displayed_names`, when given, is the set of tip names the viewer is
    currently showing. A named sequence that is not one of them is a name the
    user cannot act on -- either invented, or copied from a pre-rename label --
    and the point of that list is that every row can be found on the tree.

    Deliberately hand-written against RESPONSE_SCHEMA rather than pulled in with
    a JSON Schema library: the contract is seven fields deep and adding a
    dependency to the web process for it is not worth the exposure.
    """
    if not isinstance(review, dict):
        raise TreeAnalysisUpstreamError("Claude returned a malformed review.")

    missing = [key for key in RESPONSE_SCHEMA["required"] if key not in review]
    if missing:
        raise TreeAnalysisUpstreamError(
            f"Claude's review was missing {', '.join(missing)}."
        )

    rating = review.get("overall_rating")
    if rating not in _enum_values("properties", "overall_rating"):
        raise TreeAnalysisUpstreamError(
            f"Claude returned an unknown overall rating ({rating!r})."
        )

    for field in ("headline", "summary"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise TreeAnalysisUpstreamError(f"Claude's review had an empty {field}.")

    # The schema asks for one sentence under 140 characters and nothing enforced
    # it, so a paragraph could arrive in the field the viewer renders as a
    # single headline line beside the rating chip.
    if len(review["headline"]) > HEADLINE_MAX_CHARACTERS:
        raise TreeAnalysisUpstreamError(
            f"Claude's headline was {len(review['headline'])} characters; the "
            f"limit is {HEADLINE_MAX_CHARACTERS}."
        )

    for field in ("strengths", "recommendations"):
        value = review.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise TreeAnalysisUpstreamError(f"Claude's review had a malformed {field} list.")

    severities = _enum_values(
        "properties", "concerns", "items", "properties", "severity"
    )
    concerns = review.get("concerns")
    if not isinstance(concerns, list):
        raise TreeAnalysisUpstreamError("Claude's review had a malformed concerns list.")
    for concern in concerns:
        _require_object(concern, ("severity", "title", "detail"), "concern")
        if concern["severity"] not in severities:
            raise TreeAnalysisUpstreamError(
                f"Claude's review used an unknown severity ({concern['severity']!r})."
            )

    # The two rating/severity combinations the prompt rules out outright. A
    # "strong" verdict alongside an unresolved high-severity concern and an
    # "unreliable" verdict with nothing high-severity behind it are each a
    # review that argues against its own headline, and the chip is the part
    # users read. Everything in between is left to the model's judgement:
    # "usable" may well carry a high-severity concern that is confined to a
    # handful of sequences.
    has_high = any(concern["severity"] == "high" for concern in concerns)
    if rating == "strong" and has_high:
        raise TreeAnalysisUpstreamError(
            "Claude rated this tree strong while raising a high-severity concern."
        )
    if rating == "unreliable" and not has_high:
        raise TreeAnalysisUpstreamError(
            "Claude rated this tree unreliable without a high-severity concern."
        )

    suspects = review.get("sequences_to_inspect")
    if not isinstance(suspects, list):
        raise TreeAnalysisUpstreamError("Claude's review had a malformed sequence list.")
    for suspect in suspects:
        _require_object(suspect, ("name", "reason"), "sequences_to_inspect")
    if displayed_names:
        known = {_normalize_name(name) for name in displayed_names}
        kept = [
            suspect for suspect in suspects
            if _normalize_name(suspect["name"]) in known
        ]
        unknown = [
            suspect["name"] for suspect in suspects
            if _normalize_name(suspect["name"]) not in known
        ]
        if unknown:
            # Dropped, not rejected. A name the viewer cannot show is one bad
            # row in an otherwise usable review, and the daily allowance was
            # already spent before the call -- throwing the whole payload away
            # burned a review, showed the user a 502, and let a systematic
            # normalization mismatch (a quoted FASTA description against a
            # sanitized tip label) exhaust the site-wide quota one retry at a
            # time. Recorded as a degradation so it stays countable.
            from app.services.log_context import log_degradation
            log_degradation(
                logger,
                "claude_review_unknown_sequences",
                "Claude's review named sequences that are not tips of this "
                "tree; those rows were dropped from the review",
                unknown=",".join(str(name) for name in unknown[:10]),
                unknown_count=len(unknown),
                kept_count=len(kept),
            )
            review["sequences_to_inspect"] = kept

    return _unescape_literal_newlines(review)


def _call_claude_cli(
    context: Dict[str, Any], displayed_names: Optional[Set[str]] = None
) -> Dict[str, Any]:
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
        raise TreeAnalysisUpstreamError(
            "Claude did not finish the review in time. Try again in a moment."
        ) from exc

    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, ValueError):
        envelope = None

    # Claude Code reports API failures as a JSON result on stdout and exits 1;
    # stderr is empty. Treating every exit 1 as a sudo/configuration problem hid
    # the actual provider error (including quota reset times) from both the log
    # and the user.
    if isinstance(envelope, dict) and envelope.get("is_error"):
        status = envelope.get("api_error_status")
        result = envelope.get("result")
        # Alan 8/24/26 - A 429 is expected operation, not a defect: the branch
        # below turns it into a user-facing Retry-After and the viewer retries.
        # Logging it at ERROR put it in errors.log and in the log digest's
        # exception list, where routine quota exhaustion read as a bug.
        if status == 429:
            # Keep the complete provider text in the raw log. JSON encoding
            # preserves embedded newlines and quotes on one physical log line,
            # so a later review can distinguish session exhaustion, account
            # limits, and ordinary transient throttling without losing detail.
            logger.warning(
                "event=claude_review.cli_error subtype=%s status=%s "
                "terminal_reason=%s provider_message=%s",
                envelope.get("subtype"), status, envelope.get("terminal_reason"),
                json.dumps(result, ensure_ascii=False),
            )
        else:
            logger.error(
                "event=claude_review.cli_error subtype=%s status=%s terminal_reason=%s",
                envelope.get("subtype"), status, envelope.get("terminal_reason"),
            )
        if status == 429:
            reset_match = re.search(
                r"\bresets?\s+([0-9]{1,2}:[0-9]{2}\s*(?:am|pm)\s*\(UTC\))",
                result if isinstance(result, str) else "",
                flags=re.IGNORECASE,
            )
            if reset_match:
                reset_text = reset_match.group(1)
                reset_clock = re.sub(r"\s*\(UTC\)\s*$", "", reset_text, flags=re.I)
                now = datetime.now(timezone.utc)
                try:
                    parsed_clock = datetime.strptime(
                        reset_clock.strip().upper(), "%I:%M %p"
                    ).time()
                except ValueError:
                    raise TreeAnalysisRateLimited(
                        "Claude's session limit has been reached. Reviews should be "
                        f"available again after {reset_text}.",
                        60,
                    )
                reset_at = datetime.combine(now.date(), parsed_clock, tzinfo=timezone.utc)
                if reset_at <= now:
                    reset_at += timedelta(days=1)
                retry_after = max(1, math.ceil((reset_at - now).total_seconds()))
                raise TreeAnalysisRateLimited(
                    "Claude's session limit has been reached. Reviews should be "
                    f"available again after {reset_text}.",
                    retry_after,
                )
            raise TreeAnalysisRateLimited(
                "Claude is rate limiting reviews right now. Try again shortly.",
                60,
            )
        if status in (401, 403):
            raise TreeAnalysisUnavailable(
                "Claude review is not configured correctly on this server."
            )
        raise TreeAnalysisUpstreamError("Claude could not complete the review.")

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:300]
        logger.error(
            "event=claude_review.cli_failed rc=%s detail=%s",
            completed.returncode, detail or "(no stderr)",
        )
        if completed.returncode in (124, 137):  # timeout / SIGKILL from `timeout`
            raise TreeAnalysisUpstreamError(
                "Claude did not finish the review in time. Try again in a moment."
            )
        if "sudo" in detail.lower():
            raise TreeAnalysisUnavailable(
                "Claude review is not configured correctly on this server."
            )
        raise TreeAnalysisUpstreamError("Claude could not complete the review.")

    if not isinstance(envelope, dict):
        raise TreeAnalysisUpstreamError("Claude returned a malformed review.")

    # --json-schema puts the validated object on structured_output; `result` is
    # the same content as a string. Prefer the parsed form and fall back only if
    # a future CLI version stops populating it.
    review = envelope.get("structured_output")
    if not isinstance(review, dict):
        raw = envelope.get("result")
        if not isinstance(raw, str) or not raw.strip():
            raise TreeAnalysisUpstreamError("Claude returned an empty review.")
        try:
            review = json.loads(raw)
        except ValueError as exc:
            raise TreeAnalysisUpstreamError("Claude returned a malformed review.") from exc

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
        "review": _validate_review(review, displayed_names),
        "model": reviewer_model,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cost_usd": envelope.get("total_cost_usd"),
        },
    }


def _call_claude(
    context: Dict[str, Any], displayed_names: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Send the metrics to Claude and return the parsed review plus usage."""
    if _backend() != "api":
        return _call_claude_cli(context, displayed_names)

    import anthropic

    # The timeout is per attempt, so the retry means worst-case wall clock is
    # CLAUDE_API_MAX_RETRIES + 1 times CLAUDE_REVIEW_TIMEOUT_SECONDS. That is the
    # tradeoff for surviving a 529 overload without making the user click again;
    # the concurrency ceiling bounds how many request slots this can occupy at
    # once, and `_max_transport_seconds()` keeps the Redis slot and fingerprint
    # lock alive for the whole of it rather than for one attempt.
    client = anthropic.Anthropic(
        api_key=Config.ANTHROPIC_API_KEY,
        timeout=Config.CLAUDE_REVIEW_TIMEOUT_SECONDS,
        max_retries=CLAUDE_API_MAX_RETRIES,
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
        raise TreeAnalysisUpstreamError(
            "Claude did not respond in time. Try again in a moment."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise TreeAnalysisRateLimited(
            "Claude is rate limiting requests right now. Try again shortly.",
            60,
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
        raise TreeAnalysisUpstreamError("Claude could not complete the review.") from exc
    except anthropic.APIConnectionError as exc:
        raise TreeAnalysisUpstreamError("Could not reach Claude from this server.") from exc

    if message.stop_reason == "refusal":
        raise TreeAnalysisUpstreamError("Claude declined to review this dataset.")

    text = next(
        (block.text for block in message.content if block.type == "text"), ""
    )
    if not text:
        # max_tokens with adaptive thinking on is the realistic way to get here:
        # the budget went to reasoning and no JSON was emitted.
        raise TreeAnalysisUpstreamError(
            "Claude returned an empty review"
            + (" (output limit reached)." if message.stop_reason == "max_tokens" else ".")
        )

    try:
        review = json.loads(text)
    except ValueError as exc:
        raise TreeAnalysisUpstreamError("Claude returned a malformed review.") from exc

    return {
        "review": _validate_review(review, displayed_names),
        "model": message.model,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_read_input_tokens": getattr(
                message.usage, "cache_read_input_tokens", 0
            ),
            # The API SDK reports tokens but not billed dollars. Keep this
            # explicitly unavailable instead of allowing monitoring to turn it
            # into a misleading $0.00.
            "cost_usd": None,
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
    # Through artifact_storage like every other job artifact read. Nothing
    # compresses this file today, but a bare open() here is the same latent bug
    # that silently dropped gzipped tree metadata: it would read as "no cached
    # review" and quietly bill a fresh call for a review already on disk.
    if not artifact_exists(path):
        return None
    try:
        with open_artifact(path, "rt") as handle:
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
    # Also applied here, not just at generation: reviews cached before the
    # unescaping existed would otherwise keep rendering their literal "\n".
    if isinstance(stored.get("review"), dict):
        stored["review"] = _unescape_literal_newlines(stored["review"])
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

USAGE_LOG_PATH = Path("var/logs/claude_reviews.jsonl")


def _usage_log_path() -> Path:
    """Absolute path to the append-only usage log."""
    if USAGE_LOG_PATH.is_absolute():
        return USAGE_LOG_PATH
    return Path(current_app.root_path).parent / USAGE_LOG_PATH


def _logged_reviews_for_utc_day(day_start: int) -> int:
    """Count completed billed reviews today to seed a missing Redis counter.

    Redis normally preserves the counter across web restarts. Seeding it from
    the durable usage log also keeps today's allowance accurate after a Redis
    restart or when this limit is first deployed partway through a day.
    """
    day_end = day_start + 86400
    try:
        with open(_usage_log_path(), "r") as handle:
            count = 0
            for line in handle:
                try:
                    timestamp = json.loads(line).get("ts")
                except (AttributeError, ValueError):
                    continue
                if isinstance(timestamp, (int, float)) and day_start <= timestamp < day_end:
                    count += 1
            return count
    except FileNotFoundError:
        return 0
    except OSError as exc:
        # The gate below fails closed if Redis also has no counter, so an
        # unreadable log can never silently reset the owner's spending limit.
        raise TreeAnalysisUnavailable(
            "Claude reviews are temporarily unavailable because today's usage could not be checked."
        ) from exc


def _reserve_daily_review() -> None:
    """Atomically reserve one site-wide review from today's UTC allowance.

    This runs only after a cache miss, immediately before `_call_claude()`, so
    free cache hits never consume the quota. A reservation is deliberately not
    refunded on model failure: a timeout or malformed response may still have
    incurred provider cost.
    """
    limit = max(1, int(Config.CLAUDE_REVIEW_MAX_DAILY))
    now = time.time()
    day_start = int(now // 86400) * 86400
    retry_after = max(1, day_start + 86400 - int(now))
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    key = f"dikarya:claude_review:daily:{day}"

    try:
        from app.workers.queue import get_redis_connection

        client = get_redis_connection()
        # SET NX makes the durable-log seed atomic across all Gunicorn workers.
        # Keep the old dated key briefly past midnight for operational inspection;
        # the next UTC day always uses a new key.
        if not client.exists(key):
            seed = _logged_reviews_for_utc_day(day_start)
            client.set(key, seed, nx=True, ex=retry_after + 300)
        current = int(client.incr(key))
        if current > limit:
            client.decr(key)
            raise TreeAnalysisDailyLimit(limit, retry_after)
    except (TreeAnalysisDailyLimit, TreeAnalysisUnavailable):
        raise
    except Exception as exc:
        # Spending protection is fail-closed: a Redis outage must not turn the
        # configured ceiling into an unlimited model endpoint.
        logger.warning("Claude review daily spending guard unavailable: %s", exc)
        raise TreeAnalysisUnavailable(
            "Claude reviews are temporarily unavailable because the daily spending limit could not be checked."
        ) from exc


def _append_usage_log(job_dir: Path, payload: Dict[str, Any]) -> None:
    """Record one billed review so the monitoring page can total them.

    The per-job cache holds only the newest review for that job and is
    overwritten on every refresh, so it cannot answer "how much did this
    feature use this month". This log is append-only and records only the
    review that was actually paid for -- cache hits never reach here.

    Best-effort by design: a monitoring line must never cost a user their
    review, so every failure is swallowed after being logged.
    """
    usage = payload.get("usage") or {}
    record = {
        "ts": payload.get("generated_at"),
        "job_id": job_dir.name,
        "model": payload.get("model"),
        "effort": str(Config.CLAUDE_REVIEW_EFFORT),
        "backend": _backend(),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "cost_usd": usage.get("cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        # `input_tokens` counts only the uncached remainder, which for this
        # prompt is 2-4 tokens -- everything else is billed as cache creation
        # or cache read. Anyone summing `input_tokens` to reason about prompt
        # size or cost got an answer three orders of magnitude too small, so
        # record the total explicitly rather than leaving it to be rederived.
        "total_input_tokens": (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
        ),
        "rating": (payload.get("review") or {}).get("overall_rating"),
    }
    try:
        path = _usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as exc:  # pragma: no cover - monitoring must not break reviews
        logger.warning("Could not append Claude review usage log: %s", exc)


def review_job(job_dir: Path, *, force_refresh: bool = False) -> Dict[str, Any]:
    """Produce (or reuse) a Claude review of this job's alignment and tree."""
    if not is_configured():
        raise TreeAnalysisUnavailable("Claude review is not enabled on this server.")

    displayed_names: Set[str] = set()
    context = build_context(job_dir, displayed_names_out=displayed_names)
    key = fingerprint(context)

    if not force_refresh:
        cached = load_cached_review(job_dir, key)
        if cached is not None:
            return cached

    # Claimed before the slot and before the daily reservation: a duplicate of a
    # review already running must not spend either.
    lock = _acquire_fingerprint_lock(key)
    slot = None
    started = time.monotonic()
    try:
        slot = _acquire_slot()
        _reserve_daily_review()
        result = _call_claude(context, displayed_names)
    finally:
        if slot is not None:
            slot.release()
        if lock is not None:
            lock.release()

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
    _append_usage_log(job_dir, payload)
    return payload
