"""Tree rooting heuristics for BLAST-result trees.

Provides choose_auto_root_target(), a conservative auto-rooting helper that
anchors on a known sequence of interest (focal tip) and picks the most distant
high-quality candidate as the display root. This is a visualization heuristic,
not a claim about the true evolutionary root.
"""
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from Bio import Phylo, SeqIO
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

# Blue used by the iNaturalist source-tip highlight in the Default selection
# set. Treat this as the canonical marker of "sequence of interest" when the
# state has not been set explicitly.
BLUE_HIGHLIGHT_COLOR = "#1f77b4"

# Conservative quality thresholds for candidate hits.
MIN_OVERLAP_FRACTION = 0.85
MIN_RAW_LENGTH_FRACTION = 0.90
MAX_AMBIGUOUS_FRACTION = 0.05
MAX_GAP_FRACTION = 0.50


@dataclass
class RootChoice:
    mode: str  # auto, most_divergent_hit, midpoint_fallback, prompt_for_sequence_of_interest, none
    query_tip: Optional[str] = None
    target_name: Optional[str] = None
    reason: str = ""
    warnings: List[str] = field(default_factory=list)
    score: Optional[float] = None
    candidate_count: int = 0
    rejected_count: int = 0

    def to_info(self, chosen_by: str) -> Dict[str, Any]:
        return {
            "query_tip": self.query_tip,
            "chosen_root_target": self.target_name,
            "chosen_by": chosen_by,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "candidate_count": self.candidate_count,
            "rejected_count": self.rejected_count,
            "score": self.score,
        }


# ---------------------------------------------------------------------------
# Focal-tip resolution
# ---------------------------------------------------------------------------

def _iter_tip_names(node: Dict[str, Any]):
    """Yield tip (terminal) names from a tree_structure dict."""
    if not isinstance(node, dict):
        return
    children = node.get("children")
    if children:
        for c in children:
            yield from _iter_tip_names(c)
    else:
        # Prefer original_name: selection_sets and pruned_taxa key on the stable
        # original tip id, not the post-rename display label. The iNat source
        # highlight renames the tip and stores the *pre-rename* id in Default.
        name = node.get("original_name") or node.get("name")
        if name:
            yield name


def _tree_tip_set(state: Dict[str, Any]) -> set:
    structure = state.get("tree_structure") or {}
    return set(_iter_tip_names(structure))


def resolve_sequence_of_interest(state: Dict[str, Any]) -> Tuple[Optional[str], str, List[str]]:
    """Resolve the focal tip from durable state.

    Resolution order:
      1. Explicit `sequence_of_interest` field (if still present in tree).
      2. Single member of the Default selection set when its color is blue
         (#1f77b4) — this is how iNat-imported jobs mark the source tip.

    Returns (tip_name, source_label, warnings).
    """
    warnings: List[str] = []
    tips = _tree_tip_set(state)

    explicit = state.get("sequence_of_interest")
    if explicit:
        if explicit in tips:
            return explicit, state.get("sequence_of_interest_source") or "explicit", warnings
        warnings.append(f"Stored sequence_of_interest '{explicit}' not present in tree.")

    sel_sets = state.get("selection_sets") or {}
    colors = state.get("selection_set_colors") or {}
    default_color = (colors.get("Default") or "").lower()
    default_members = sel_sets.get("Default") or []
    if isinstance(default_members, list) and default_color == BLUE_HIGHLIGHT_COLOR.lower():
        present = [m for m in default_members if m in tips]
        if len(present) == 1:
            return present[0], "inat_highlight", warnings
        if len(present) > 1:
            warnings.append(
                f"Multiple blue-highlighted tips ({len(present)}); explicit sequence_of_interest required."
            )
        elif default_members:
            warnings.append("Blue-highlighted tips not present in current tree.")

    return None, "unknown", warnings


# ---------------------------------------------------------------------------
# Alignment quality checks
# ---------------------------------------------------------------------------

def _load_alignment(job_dir: Path) -> Dict[str, str]:
    """Return {tip_name: aligned_sequence}. Prefers trimmed alignment."""
    if not HAS_BIOPYTHON:
        return {}
    candidates = [
        job_dir / "alignment" / "alignment_trimmed.fasta",
        job_dir / "alignment" / "alignment_raw.fasta",
        job_dir / "alignment" / "alignment_pruned_trimmed.fasta",
        job_dir / "alignment" / "alignment_pruned_aligned.fasta",
    ]
    for path in candidates:
        if path.exists():
            try:
                seqs: Dict[str, str] = {}
                for rec in SeqIO.parse(str(path), "fasta"):
                    seq = str(rec.seq).upper()
                    for name in (rec.id, rec.name, rec.description):
                        if name:
                            seqs[str(name).strip()] = seq
                if seqs:
                    return seqs
            except Exception as e:
                logger.warning("Failed to parse alignment %s: %s", path, e)
    return {}


def _seq_stats(seq: str) -> Tuple[int, int, int]:
    """Return (raw_length, gap_count, ambiguous_count)."""
    gaps = seq.count("-") + seq.count(".")
    raw_len = len(seq) - gaps
    ambiguous = sum(1 for c in seq if c not in "ACGTU-.")
    return raw_len, gaps, ambiguous


def _aligned_overlap(focal: str, other: str) -> int:
    """Count alignment columns where both have a non-gap residue."""
    n = min(len(focal), len(other))
    overlap = 0
    for i in range(n):
        a = focal[i]
        b = other[i]
        if a not in "-." and b not in "-.":
            overlap += 1
    return overlap


def _assess_candidate(focal_seq: str, focal_raw_len: int,
                      cand_seq: str) -> Tuple[bool, Optional[str]]:
    """Return (acceptable, rejection_reason)."""
    if not cand_seq:
        return False, "no_aligned_sequence"
    raw_len, gaps, amb = _seq_stats(cand_seq)
    if raw_len < int(MIN_RAW_LENGTH_FRACTION * focal_raw_len):
        return False, "too_short"
    total = len(cand_seq) or 1
    if gaps / total > MAX_GAP_FRACTION:
        return False, "mostly_gaps"
    if raw_len and amb / raw_len > MAX_AMBIGUOUS_FRACTION:
        return False, "high_ambiguity"
    overlap = _aligned_overlap(focal_seq, cand_seq)
    if overlap < int(MIN_OVERLAP_FRACTION * focal_raw_len):
        return False, "low_overlap"
    return True, None


# ---------------------------------------------------------------------------
# Tree / distance helpers
# ---------------------------------------------------------------------------

def _load_phylo_tree(job_dir: Path):
    if not HAS_BIOPYTHON:
        return None
    for name in ("tree_pruned.newick", "tree_original.newick"):
        path = job_dir / "tree" / name
        if path.exists():
            try:
                return Phylo.read(str(path), "newick")
            except Exception as e:
                logger.warning("Failed to read %s: %s", path, e)
    return None


def _tree_has_branch_lengths(tree) -> bool:
    return any((c.branch_length or 0) > 0 for c in tree.find_clades())


def _patristic_distances(tree, focal_name: str) -> Dict[str, float]:
    """Return patristic distance from the focal tip to every other tip.

    Single-source traversal of the tree graph (each edge weighted by the child
    clade's branch length) instead of calling tree.distance() once per tip,
    which re-walks the tree on every call (O(N^2) for N tips).
    """
    focal = next((t for t in tree.get_terminals() if t.name == focal_name), None)
    if focal is None:
        return {}

    # One pass to build parent links and child lists (clades are hashable by identity).
    parents: Dict[Any, Any] = {}
    children: Dict[Any, list] = {}
    for clade in tree.find_clades():
        kids = list(clade.clades)
        children[clade] = kids
        for child in kids:
            parents[child] = clade

    # BFS out from the focal tip. The branch leading to a clade is that clade's
    # branch_length, so stepping up uses the node's length and stepping down uses
    # the child's length.
    dist: Dict[Any, float] = {focal: 0.0}
    queue = deque([focal])
    while queue:
        node = queue.popleft()
        base = dist[node]
        parent = parents.get(node)
        if parent is not None and parent not in dist:
            dist[parent] = base + float(node.branch_length or 0.0)
            queue.append(parent)
        for child in children.get(node, ()):
            if child not in dist:
                dist[child] = base + float(child.branch_length or 0.0)
                queue.append(child)

    out: Dict[str, float] = {}
    for t in tree.get_terminals():
        if t is focal or not t.name:
            continue
        if t in dist:
            out[t.name] = float(dist[t])
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def choose_auto_root_target(job_dir: Path, state: Dict[str, Any],
                             mode: str = "auto") -> RootChoice:
    """Choose a rooting target.

    mode:
      - "auto": most distant high-quality acceptable hit
      - "most_divergent_hit": same algorithm; returned mode tag differs
    """
    warnings: List[str] = []

    if not HAS_BIOPYTHON:
        return RootChoice(mode="none", reason="biopython_unavailable",
                          warnings=["BioPython not installed"])

    focal, focal_source, focal_warn = resolve_sequence_of_interest(state)
    warnings.extend(focal_warn)
    if not focal:
        return RootChoice(
            mode="prompt_for_sequence_of_interest",
            reason="no_focal_tip",
            warnings=warnings,
        )

    tree = _load_phylo_tree(job_dir)
    if tree is None:
        return RootChoice(mode="none", query_tip=focal,
                          reason="no_tree_file", warnings=warnings)

    if not _tree_has_branch_lengths(tree):
        warnings.append("tree_missing_branch_lengths")
        return RootChoice(mode="midpoint_fallback", query_tip=focal,
                          reason="no_branch_lengths", warnings=warnings)

    distances = _patristic_distances(tree, focal)
    if not distances:
        warnings.append("focal_tip_not_in_tree")
        return RootChoice(mode="midpoint_fallback", query_tip=focal,
                          reason="focal_not_in_tree", warnings=warnings)

    alignment = _load_alignment(job_dir)
    focal_seq = alignment.get(focal, "")
    focal_raw_len = _seq_stats(focal_seq)[0] if focal_seq else 0

    if focal_raw_len == 0:
        warnings.append("focal_alignment_missing")
        return RootChoice(
            mode="midpoint_fallback", query_tip=focal,
            reason="focal_alignment_missing",
            warnings=warnings,
            candidate_count=0,
            rejected_count=len(distances),
        )

    rejected = 0
    accepted: List[Tuple[str, float]] = []
    for tip_name, dist in distances.items():
        if tip_name == focal:
            continue
        cand_seq = alignment.get(tip_name, "")
        ok, _reason = _assess_candidate(focal_seq, focal_raw_len, cand_seq)
        if ok:
            accepted.append((tip_name, dist))
        else:
            rejected += 1

    if not accepted:
        warnings.append("no_acceptable_candidates")
        return RootChoice(
            mode="midpoint_fallback", query_tip=focal,
            reason="no_acceptable_candidates",
            warnings=warnings,
            candidate_count=0,
            rejected_count=rejected,
        )

    # Pick the most distant acceptable candidate.
    accepted.sort(key=lambda x: x[1], reverse=True)
    target_name, best_dist = accepted[0]

    out_mode = "most_divergent_hit" if mode == "most_divergent_hit" else "auto"
    return RootChoice(
        mode=out_mode,
        query_tip=focal,
        target_name=target_name,
        reason=f"most_distant_acceptable_hit:source={focal_source}",
        warnings=warnings,
        score=best_dist,
        candidate_count=len(accepted),
        rejected_count=rejected,
    )


def is_blast_result_job(job_dir: Path) -> bool:
    """Heuristic: a BLAST-result job has blast/blast_results.json."""
    return (job_dir / "blast" / "blast_results.json").exists()
