"""Leave identical aligned sequences out of a long tree search, then add them back.

A sequence identical to another one adds nothing a likelihood or Bayesian tree
search can use. It lands beside its twin on a branch of length zero, yet every
taxon makes every likelihood evaluation, bootstrap replicate and MCMC generation
more expensive. RAxML-NG warns about such sequences and keeps them. IQ-TREE
drops all but two of each group and re-adds the rest at the end. MrBayes keeps
them all. So the methods whose runs take minutes to hours (RAxML-NG, the
advanced IQ-TREE method and MrBayes) get an alignment with one sequence per
identical group, and each group is put back on the finished tree as a
zero-length polytomy at the position of the sequence that was kept.

Replaying production RAxML-NG jobs, this cut CPU time by 14-39% on jobs with
12-34% duplicates. The re-added trees scored within 0.7 log-likelihood units of
full-alignment runs (on the full alignment), with differences no larger than a
change of seed produces (ops/raxml/README.md). Quick Tree is left alone,
because its run takes seconds.

"Identical" means the same characters after upper-casing, the same test
RAxML-NG and the pipeline's output check (`validate_pipeline_outputs`) use. A
gap and an N are different characters here, even though the models treat both
as missing data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Tree methods whose runs are long enough to be worth it. iqtree_fast (Quick
# Tree), FastTree and NJ finish in seconds and run on the alignment as given.
COLLAPSE_METHODS = frozenset({"raxml", "iqtree", "mrbayes"})

# RAxML-NG and MrBayes refuse fewer than four taxa. A job with fewer distinct
# sequences than this keeps its duplicates, exactly as it ran before.
MIN_DISTINCT_SEQUENCES = 4

# Characters that carry no information. A record made only of these is never
# grouped. Two empty rows are "identical", but saying so would be meaningless.
_UNINFORMATIVE = frozenset("-?NX.")

REDUCED_ALIGNMENT_NAME = "identical_collapsed_input.fasta"


@dataclass
class IdenticalCollapse:
    """One collapse: the reduced alignment and the groups to re-add."""

    reduced_fasta: Path
    # Each group lists tree labels (FASTA descriptions, the names the builders
    # restore onto the tree). The first label is the record that stayed in the
    # alignment, and the rest were left out.
    groups: List[List[str]]
    total_records: int

    @property
    def removed_count(self) -> int:
        return sum(len(group) - 1 for group in self.groups)

    def expansions(self) -> Dict[str, List[str]]:
        return {group[0]: group[1:] for group in self.groups}

    def metadata(self) -> Dict[str, object]:
        """The tree_metadata.json record of what was collapsed."""
        return {
            "sequences_in_alignment": self.total_records,
            "sequences_in_tree_search": self.total_records - self.removed_count,
            "removed": self.removed_count,
            "groups": [
                {"kept": group[0], "removed": group[1:]} for group in self.groups
            ],
        }


def _resolve_preferred(records, prefer: Optional[str]) -> Optional[int]:
    """Index of the record `prefer` names, matched as the outgroup is.

    Mirrors `raxml_validator.resolve_outgroup_id`: an exact description match
    first, then the record ID (first word). Returns -1 when the name matches
    nothing or more than one record, so the caller can leave the alignment
    alone and let the builder report the problem exactly as it did before.
    """
    if not prefer:
        return None
    matches = [i for i, record in enumerate(records) if record.description == prefer]
    if not matches:
        matches = [
            i for i, record in enumerate(records)
            if (record.description.split(None, 1) or [""])[0] == prefer
        ]
    return matches[0] if len(matches) == 1 else -1


def plan_identical_collapse(
    alignment_fasta: Path,
    work_dir: Path,
    *,
    prefer: Optional[str] = None,
    task_logger=None,
) -> Optional[IdenticalCollapse]:
    """Write a one-per-group copy of the alignment, or return None.

    None means "run on the alignment as given": there is nothing to collapse,
    too few distinct sequences would remain, two records share a name (so the
    re-added tips could not be placed unambiguously), or `prefer` (the
    outgroup) does not name exactly one record. `prefer` is always kept as its
    group's representative, because RAxML-NG roots on it before the duplicates
    are re-added.
    """
    from Bio import SeqIO

    from app.services.artifact_storage import open_artifact

    log = task_logger or logger
    with open_artifact(alignment_fasta, "rt") as handle:
        records = list(SeqIO.parse(handle, "fasta"))

    descriptions = [record.description for record in records]
    if len(set(descriptions)) != len(descriptions):
        log.info(
            "Identical sequences were not collapsed: two records share a name, "
            "so re-added tips could not be placed unambiguously."
        )
        return None

    preferred = _resolve_preferred(records, prefer)
    if preferred == -1:
        return None

    by_sequence: Dict[str, List[int]] = {}
    for index, record in enumerate(records):
        sequence = str(record.seq).upper()
        if not sequence or set(sequence) <= _UNINFORMATIVE:
            continue
        by_sequence.setdefault(sequence, []).append(index)

    groups = []
    for members in by_sequence.values():
        if len(members) < 2:
            continue
        if preferred in members:
            members = [preferred] + [i for i in members if i != preferred]
        groups.append(members)
    if not groups:
        return None

    removed = {i for members in groups for i in members[1:]}
    distinct = len(records) - len(removed)
    if distinct < MIN_DISTINCT_SEQUENCES:
        log.info(
            "Identical sequences were not collapsed: only %d distinct sequences "
            "would remain.", distinct,
        )
        return None

    groups.sort(key=lambda members: members[0])
    reduced = Path(work_dir) / REDUCED_ALIGNMENT_NAME
    with open(reduced, "w") as out:
        for index, record in enumerate(records):
            if index not in removed:
                out.write(f">{record.description}\n{record.seq}\n")

    return IdenticalCollapse(
        reduced_fasta=reduced,
        groups=[[descriptions[i] for i in members] for members in groups],
        total_records=len(records),
    )


def restore_identical_tips(tree_path: Path, collapse: IdenticalCollapse) -> None:
    """Re-add the left-out sequences to a finished Newick tree, in place."""
    from app.services.tree_io import expand_newick_tips

    text = Path(tree_path).read_text()
    expanded = expand_newick_tips(text, collapse.expansions())
    if expanded is None:
        raise RuntimeError(
            f"Could not add the {collapse.removed_count} identical sequences back "
            f"to {Path(tree_path).name}: a kept sequence was not found exactly "
            "once in the tree."
        )
    Path(tree_path).write_text(expanded)
