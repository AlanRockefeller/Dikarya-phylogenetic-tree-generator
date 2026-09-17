"""Read the completed bootstrap outcome from RAxML-NG result files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional


_CONVERGED_RE = re.compile(
    r"Bootstrapping converged after\s+(\d+)\s+replicates", re.IGNORECASE
)


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def read_raxml_bootstrap_summary(
    tree_dir: Path, *, cap: Any = None, prefix: str = "raxml_run"
) -> Dict[str, Any]:
    """Return the replicate count and AutoMRE outcome for a finished run.

    RAxML-NG writes one Newick tree per completed replicate, so the line count
    is the ground truth even for older jobs whose metadata predates this
    summary.  The log is the ground truth for whether AutoMRE declared
    convergence; a short file alone could instead mean an interrupted run.
    """
    tree_dir = Path(tree_dir)
    bootstrap_path = tree_dir / f"{prefix}.raxml.bootstraps"
    log_path = tree_dir / f"{prefix}.raxml.log"

    completed = None
    if bootstrap_path.is_file():
        try:
            with bootstrap_path.open("rt", errors="replace") as handle:
                completed = sum(1 for line in handle if line.strip())
        except OSError:
            completed = None

    converged = False
    converged_after = None
    if log_path.is_file():
        try:
            with log_path.open("rt", errors="replace") as handle:
                for line in handle:
                    match = _CONVERGED_RE.search(line)
                    if match:
                        converged = True
                        converged_after = int(match.group(1))
        except OSError:
            pass

    # The explicit convergence line and the bootstrap file should agree. If a
    # legacy/copy operation lost the latter, the logged count remains usable;
    # if both survive, the file count wins because it measures delivered trees.
    if completed is None and converged_after is not None:
        completed = converged_after

    resolved_cap = _positive_int(cap)
    stopped_early = bool(
        converged and completed and resolved_cap and completed < resolved_cap
    )
    return {
        "bootstrap_replicates_completed": completed,
        "bootstrap_converged": converged,
        "bootstrap_stopped_early": stopped_early,
    }
