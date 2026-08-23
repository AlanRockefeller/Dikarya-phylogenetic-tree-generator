"""
Tree builder service module.

Provides functions for phylogenetic tree inference:
- Neighbor Joining (BioPython)
- RAxML-NG
- IQ-TREE
- MrBayes

When job_id is provided, streams log output to Redis for real-time SSE updates.
"""

import os
import re
import shutil
import logging
import math
from collections import Counter
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from app.config import Config
from app.models import TreeBuilderParams

_RAXML_HELP_CACHE = None
from app.services.subprocess_utils import (
    configured_tool_limits,
    configured_tool_time_limit_hours,
    configured_tool_timeout_seconds,
    run_command,
    run_command_streaming,
    tool_failure_message,
)
from app.services.fasta_utils import sanitize_fasta_headers, restore_tree_names
from app.services.tree_io import (
    newick_file_to_nexus,
    write_tree_file,
)

# Try to import Bio.Phylo for NJ, or implement simple fallback
try:
    from Bio import Phylo, AlignIO, SeqIO
    from Bio.Phylo.TreeConstruction import DistanceCalculator, DistanceTreeConstructor
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)

# Number of resamples FastTree uses for its SH-like local support test (-boot N).
# Despite the flag name, these are NOT bootstrap replicates: the resulting node
# values are SH-like local supports on a 0-1 scale.
FASTTREE_SH_RESAMPLES = 1000

# IQ-TREE model strings (e.g. "GTR+F+I+G4", "MFP", "TIM2e+R4",
# "GTR+G:part1,HKY+I:part2") are made up of model/modifier names, digits,
# and a small set of punctuation for modifiers/partitions/params. This is a
# character-allowlist (not a semantic check like raxml_validator.py) --
# IQ-TREE itself rejects unrecognized model names. Argv passing already
# prevents shell injection; this is hygiene, matching the outgroup check in
# raxml_validator.py.
_IQTREE_MODEL_ALLOWED_RE = re.compile(r"^[A-Za-z0-9+\-_.,:{}*/]+$")
_IQTREE_MODEL_MAX_LEN = 256
_IQTREE_DEFAULT_MODEL = "GTR+G"

# IQ-TREE model strings that ask ModelFinder to pick the model rather than
# naming one. MF selects only; MFP selects then infers the tree. TEST/TESTNEW
# are the jModelTest-compatible aliases.
_MODEL_FINDER_REQUESTS = frozenset({"MF", "MFP", "TEST", "TESTNEW", "TESTONLY", "TESTNEWONLY"})


def _is_model_finder_request(model_str: str) -> bool:
    """True when the model string delegates model choice to ModelFinder."""
    base = (model_str or "").strip().upper().split("+")[0]
    return base in _MODEL_FINDER_REQUESTS


def _validate_iqtree_model(model_str: str) -> str:
    """Return a safe IQ-TREE -m value, falling back to the default on rejection."""
    model_str = (model_str or "").strip()
    if not model_str:
        return _IQTREE_DEFAULT_MODEL
    if len(model_str) > _IQTREE_MODEL_MAX_LEN or not _IQTREE_MODEL_ALLOWED_RE.match(model_str):
        logger.warning(f"Rejected IQ-TREE model string '{model_str}'; using default '{_IQTREE_DEFAULT_MODEL}'.")
        return _IQTREE_DEFAULT_MODEL
    return model_str


def _normalize_mrbayes_burnin_fraction(value: Any) -> float:
    """Return a finite relative burn-in fraction accepted by MrBayes."""
    try:
        burnin_fraction = float(value)
    except (TypeError, ValueError):
        return 0.25
    if not math.isfinite(burnin_fraction):
        return 0.25
    return max(0.0, min(0.99, burnin_fraction))


def run_tree_builder(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Run the selected tree building algorithm.
    
    Methods: "nj", "raxml", "iqtree", "mrbayes"
    
    Args:
        alignment_fasta: Path to aligned FASTA file
        output_newick: Path for output Newick tree
        output_nexus: Path for output NEXUS tree
        params: Tree builder parameters
        config: Application config
        task_logger: Logger instance
        job_id: Optional job ID for real-time event streaming
    
    Returns:
        Metadata dict with method, model, etc.
    """
    method = params.method.lower()
    task_logger.info(f"Starting tree building with method: {method}")
    
    # `model` and `bootstrap` describe what the user asked for. Anything a
    # method does not actually honour is nulled out below rather than echoed
    # back: this file is what the viewer and the downstream analysis cite, and
    # a setting reported as though it had been applied is worse than no
    # setting at all.
    metadata = {
        "method": method,
        "model": params.model,
        "bootstrap": params.bootstrap,
        "run_dir": str(output_newick.parent)
    }
    if method == "nj":
        # NJ applies no substitution model in the ML sense and does no
        # bootstrapping; both fields used to be echoed back from the submitted
        # params, so every NJ tree claimed "GTR+G" with a replicate count.
        metadata["model"] = None
        metadata["bootstrap"] = None
        metadata["support_type"] = None
    if method == "fasttree":
        # FastTree ignores params.bootstrap entirely: _run_fasttree hardcodes
        # -boot N, which computes SH-like local supports (0-1), not bootstrap
        # proportions. Recording a bootstrap count here made the viewer report a
        # number that was both wrong and the wrong kind of support value.
        metadata["bootstrap"] = None
        metadata["support_type"] = "sh_like"
        metadata["support_resamples"] = FASTTREE_SH_RESAMPLES
    if method == "iqtree":
        has_alrt = (params.alrt_replicates or 0) > 0
        has_ufboot = (params.bootstrap or 0) > 0
        if has_alrt and has_ufboot:
            # Dual "SH-aLRT/UFBoot" labels, both on a 0-100 scale.
            metadata["support_type"] = "alrt_ufboot"
        elif has_alrt:
            metadata["support_type"] = "alrt"
        elif has_ufboot:
            metadata["support_type"] = "ufboot"
        else:
            metadata["support_type"] = None
        if has_alrt:
            metadata["alrt_replicates"] = params.alrt_replicates
    if method == "mrbayes":
        burnin_fraction = _normalize_mrbayes_burnin_fraction(
            params.mcmc_burnin_fraction
        )
        # A Bayesian run has no bootstrap replicates; node values are posterior
        # probabilities.
        metadata["bootstrap"] = None
        metadata["support_type"] = "posterior"
        metadata.update({
            # A maximum, not a promise: with the stop rule on, MrBayes may end
            # the run well before this. mcmc_generations_completed (recorded by
            # _run_mrbayes when it can be read) is what actually ran.
            "mcmc_generations": params.mcmc_generations,
            "mcmc_nruns": params.mcmc_nruns,
            "mcmc_nchains": params.mcmc_nchains,
            "mcmc_burnin_fraction": burnin_fraction,
            "mcmc_stop_early_requested": bool(params.mcmc_stop_early),
        })

    try:
        if method == "nj":
            metadata.update(_run_neighbor_joining(
                alignment_fasta, output_newick, output_nexus, task_logger, job_id
            ))
        elif method == "raxml":
            # Alan 8/15/26 - MOOSE routinely overrides params.model (a
            # GTR+G request came back as TPM2+FE+R2), but only the requested
            # model was ever recorded, so the viewer cited a model the tree was
            # not built under. Same model_selected contract as IQ-TREE below.
            effective_model, selected_by, raxml_meta = _run_raxml(
                alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id
            )
            metadata.update(raxml_meta)
            if effective_model and effective_model != params.model:
                metadata["model_selected"] = effective_model
                if selected_by:
                    metadata["model_selector"] = selected_by
        elif method == "iqtree":
            selected_model, iqtree_seed = _run_iqtree(
                alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id
            )
            metadata["seed"] = iqtree_seed
            if selected_model:
                # params.model stays as requested ("MFP"); model_selected is what
                # ModelFinder actually fit. Both matter: one is the setting, the
                # other is what you cite.
                metadata["model_selected"] = selected_model
                if _is_model_finder_request(params.model):
                    metadata["model_selector"] = "ModelFinder"
                    task_logger.info(f"ModelFinder selected substitution model: {selected_model}")
        elif method == "mrbayes":
            metadata.update(_run_mrbayes(
                alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id
            ))
        elif method == "fasttree":
            metadata.update(_run_fasttree(
                alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id
            ))
        else:
            raise ValueError(f"Unsupported tree building method: {method}")

        if not output_newick.exists() or output_newick.stat().st_size == 0:
             raise RuntimeError(f"Tree building failed: Output file {output_newick} is missing or empty.")

        task_logger.info(f"Tree building completed successfully. Output: {output_newick}")
        return metadata

    except Exception as e:
        task_logger.error(f"Tree building failed: {e}")
        raise

    finally:
        _discard_scratch_inputs(output_newick.parent, task_logger)


def _discard_scratch_inputs(tree_dir: Path, task_logger) -> None:
    """
    Remove the `*_input_sanitized.fasta` copies once the builder has exited.

    Every builder writes a header-sanitized copy of the trimmed alignment purely
    to hand a path to an external binary; the name mapping it needs is returned
    in memory and persisted to tree_metadata.json. Nothing reads these files
    back, and they were the single largest category of dead weight in var/jobs
    (~1.3 GiB across the job tree). The trimmed alignment they were derived from
    is retained, so nothing is lost on the failure path either.
    """
    from app.services.artifact_storage import discard_artifact

    try:
        scratch = sorted(tree_dir.glob("*_input_sanitized.fasta"))
    except OSError:
        return
    reclaimed = sum(discard_artifact(path) for path in scratch)
    if reclaimed:
        task_logger.info(
            "Removed %d sanitized tree input(s), reclaiming %.1f MB",
            len(scratch), reclaimed / (1024 * 1024),
        )


def _get_thread_count(params: TreeBuilderParams) -> int:
    if params.threads:
        return params.threads
    return min(8, os.cpu_count() or 1)


def _make_log_callback(job_id: Optional[str], step: str, stream: str):
    """Create a callback function for streaming output to Redis."""
    if not job_id:
        return None

    from app.workers.events import publish_log

    def callback(line: str):
        publish_log(job_id, step, stream, line)

    return callback


def _tool_time_limit_hours(config: Config, tool: str) -> float:
    return configured_tool_time_limit_hours(config, tool)


def _tool_timeout_seconds(config: Config, tool: str) -> int:
    return configured_tool_timeout_seconds(config, tool)


def _tool_limits(config: Config, tool: str, threads: int) -> Dict[str, Any]:
    return configured_tool_limits(config, tool, threads)


def _resolve_seed(params: TreeBuilderParams) -> int:
    """Return the RNG seed to use, inventing (and returning) one if unset.

    Only RAxML was ever seeded, so no IQ-TREE, MrBayes or FastTree result on
    the site could be reproduced -- and nothing recorded the seed that had been
    used. The caller writes the value returned here into tree_metadata.json.
    """
    seed = getattr(params, "seed", None)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = None
    if seed is None or seed <= 0:
        import random

        seed = random.SystemRandom().randrange(1, 2 ** 31 - 1)
    return seed


# Nucleotide codes for the K2P distance. Anything else (gap, N, IUPAC
# ambiguity, '?') is treated as missing data and excluded pairwise.
_NT_CODES = {"A": 0, "C": 1, "G": 2, "T": 3, "U": 3}
_NT_MISSING = 255

# Below this many shared unambiguous columns a pairwise distance is guesswork.
# ITS reads that barely overlap used to get a small distance simply because
# they had few columns to disagree in, which pulled unrelated taxa together.
MIN_NJ_PAIRWISE_OVERLAP = 30

# Cap for a pair whose corrected distance is undefined (saturated) or that has
# too little overlap to estimate. Large enough to keep such a pair apart,
# finite so NJ still runs.
NJ_SATURATED_DISTANCE = 2.0

# A production sample separated routine NJ corrections (0--16% negative limbs,
# no capped pairs through the 90th percentile) from clearly non-additive trees
# at roughly 22--30%.  One quarter is therefore the point at which this stops
# being an occasional representation correction and becomes a quality warning.
NJ_DEGRADED_FRACTION = 0.25


def _encode_alignment(aln):
    """Encode an alignment as an (n_seq x n_col) uint8 array of A/C/G/T codes."""
    import numpy as np

    lookup = np.full(256, _NT_MISSING, dtype=np.uint8)
    for char, code in _NT_CODES.items():
        lookup[ord(char)] = code
        lookup[ord(char.lower())] = code

    rows = [
        np.frombuffer(str(record.seq).encode("ascii", "replace"), dtype=np.uint8)
        for record in aln
    ]
    return lookup[np.stack(rows)]


def _k2p_distance_matrix(aln, task_logger, name_mapping=None):
    """Kimura 2-parameter distances with pairwise deletion of missing data.

    Replaces Biopython's ``DistanceCalculator('identity')``, which is an
    uncorrected p-distance that also counts every gap as a mismatch. On ITS --
    hypervariable, gap-rich, and routinely compared across genera -- that
    understated deep divergences (no multiple-hit correction) while pushing
    short Sanger reads away from everything on the strength of their missing
    columns alone.

    Falls back per pair: K2P -> Jukes-Cantor -> capped, so a saturated or
    barely-overlapping pair degrades instead of raising.
    """
    import numpy as np
    from Bio.Phylo.TreeConstruction import DistanceMatrix

    encoded = _encode_alignment(aln)
    n_seqs = encoded.shape[0]
    names = [record.id for record in aln]

    matrix = [[0.0] * (i + 1) for i in range(n_seqs)]
    saturated = 0
    low_overlap = 0
    ordinary_k2p = 0
    jc_fallback = 0
    total_pairs = 0
    overlap_histogram = Counter()
    insufficient_by_taxon = [0] * n_seqs

    for i in range(n_seqs):
        if i + 1 >= n_seqs:
            break
        row = encoded[i]
        others = encoded[i + 1:]

        valid = (row != _NT_MISSING) & (others != _NT_MISSING)
        n_valid = valid.sum(axis=1)
        total_pairs += len(n_valid)
        for overlap, count in zip(*np.unique(n_valid, return_counts=True)):
            overlap_histogram[int(overlap)] += int(count)

        # With A=0,C=1,G=2,T=3 an XOR of exactly 2 is A<->G or C<->T, i.e.
        # precisely the transitions; any other non-zero XOR is a transversion.
        xor = np.bitwise_xor(row, others)
        transitions = ((xor == 2) & valid).sum(axis=1)
        transversions = ((xor != 0) & (xor != 2) & valid).sum(axis=1)

        with np.errstate(divide="ignore", invalid="ignore"):
            safe_n = np.where(n_valid > 0, n_valid, 1)
            p = transitions / safe_n
            q = transversions / safe_n

            term_1 = 1.0 - 2.0 * p - q
            term_2 = 1.0 - 2.0 * q
            k2p = -0.5 * np.log(term_1) - 0.25 * np.log(term_2)

            # Jukes-Cantor on the total proportion of differences, used where
            # K2P's logs are undefined.
            p_dist = p + q
            jc = -0.75 * np.log(1.0 - (4.0 / 3.0) * p_dist)

        usable_k2p = (term_1 > 0) & (term_2 > 0)
        usable_jc = (~usable_k2p) & (p_dist < 0.75)
        distances = np.where(usable_k2p, k2p, np.where(usable_jc, jc, NJ_SATURATED_DISTANCE))

        too_thin = n_valid < MIN_NJ_PAIRWISE_OVERLAP
        distances = np.where(too_thin, NJ_SATURATED_DISTANCE, distances)
        distances = np.nan_to_num(distances, nan=NJ_SATURATED_DISTANCE)
        distances = np.clip(distances, 0.0, NJ_SATURATED_DISTANCE)

        eligible = ~too_thin
        ordinary_k2p += int((usable_k2p & eligible).sum())
        jc_fallback += int((usable_jc & eligible).sum())
        saturated += int((~usable_k2p & ~usable_jc & eligible).sum())
        low_overlap += int(too_thin.sum())
        for offset in np.flatnonzero(too_thin):
            insufficient_by_taxon[i] += 1
            insufficient_by_taxon[i + 1 + int(offset)] += 1

        for offset, value in enumerate(distances):
            matrix[i + 1 + offset][i] = float(value)

    capped_pairs = low_overlap + saturated
    capped_fraction = capped_pairs / total_pairs if total_pairs else 0.0
    if capped_pairs:
        task_logger.warning(
            "Capped %d/%d NJ pairwise distances (%d insufficient overlap, %d saturated).",
            capped_pairs, total_pairs, low_overlap, saturated,
        )

    if total_pairs and capped_fraction >= NJ_DEGRADED_FRACTION:
        from app.services.log_context import log_degradation

        log_degradation(
            task_logger, "nj_distance_estimates_capped",
            "A substantial share of NJ pairwise distances could not be estimated; "
            "the quick NJ topology is poorly determined, so an ML method such as "
            "IQ-TREE is preferable",
            low_overlap_pairs=low_overlap, saturated_pairs=saturated,
            total_pairs=total_pairs, capped_fraction=round(capped_fraction, 4),
            min_overlap=MIN_NJ_PAIRWISE_OVERLAP,
        )

    def _overlap_quantile(rank: int):
        seen = 0
        for overlap in sorted(overlap_histogram):
            seen += overlap_histogram[overlap]
            if seen > rank:
                return overlap
        return None

    median_overlap = None
    if total_pairs:
        lower = _overlap_quantile((total_pairs - 1) // 2)
        upper = _overlap_quantile(total_pairs // 2)
        median_overlap = (lower + upper) / 2

    unusual_cutoff = max(3, math.ceil(max(0, n_seqs - 1) * 0.25))
    original_names = name_mapping or {}
    insufficient_taxa = [
        {
            "taxon": original_names.get(names[index], names[index]),
            "pairs": count,
            "fraction": round(count / max(1, n_seqs - 1), 4),
        }
        for index, count in sorted(
            enumerate(insufficient_by_taxon), key=lambda item: (-item[1], names[item[0]])
        )
        if count >= unusual_cutoff
    ][:10]

    def _fraction(count):
        return count / total_pairs if total_pairs else 0.0

    return DistanceMatrix(names, matrix), {
        "total_pairwise_distances": total_pairs,
        "ordinary_k2p_pairs": ordinary_k2p,
        "ordinary_k2p_fraction": _fraction(ordinary_k2p),
        "jc_fallback_pairs": jc_fallback,
        "jc_fallback_fraction": _fraction(jc_fallback),
        "saturated_pairs": saturated,
        "saturated_fraction": _fraction(saturated),
        "low_overlap_pairs": low_overlap,
        "low_overlap_fraction": _fraction(low_overlap),
        "capped_pairwise_fraction": capped_fraction,
        "minimum_pairwise_overlap": min(overlap_histogram) if overlap_histogram else None,
        "median_pairwise_overlap": median_overlap,
        "taxa_with_many_low_overlap_pairs": insufficient_taxa,
    }


def _looks_like_protein(aln) -> bool:
    """True when the alignment is clearly amino-acid rather than nucleotide."""
    residues = 0
    protein_only = 0
    for record in aln[:10]:
        seq = str(record.seq).upper()
        for char in seq:
            if char in "-.?N":
                continue
            residues += 1
            if char in "EFILPQZ":
                protein_only += 1
    return residues > 0 and (protein_only / residues) > 0.05


def _strip_generated_inner_labels(tree) -> int:
    """Remove Biopython's auto-generated ``InnerN`` internal node names.

    DistanceTreeConstructor names every internal node ``Inner1``, ``Inner2``,
    ... and those were written straight into the delivered Newick, where a
    reader sees them as node labels sitting exactly where support values
    belong.
    """
    removed = 0
    for clade in tree.get_nonterminals():
        if clade.name and re.fullmatch(r"Inner\d+", clade.name):
            clade.name = None
            removed += 1
    return removed


def _run_neighbor_joining(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    task_logger,
    job_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Build a fast NJ tree using BioPython.

    NJ is fast and runs in-process, so no streaming needed. Returns the
    distance-model metadata the caller records in tree_metadata.json.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython is required for NJ but not installed.")

    task_logger.info("Running Neighbor Joining using BioPython...")

    # Sanitize input FASTA to create safe IDs
    sanitized_fasta = output_newick.parent / "nj_input_sanitized.fasta"
    name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)

    def progress(message: str):
        if job_id:
            from app.workers.events import publish_log
            publish_log(job_id, "tree", "stderr", message)

    progress("Reading alignment...")
    aln = AlignIO.read(str(sanitized_fasta), "fasta")

    progress("Calculating distance matrix...")
    if _looks_like_protein(aln):
        # No nucleotide substitution model applies; BLOSUM62 is the sensible
        # default for the rare protein alignment that reaches this path.
        calculator = DistanceCalculator("blosum62")
        dm = calculator.get_distance(aln)
        distance_model = "blosum62"
        distance_stats = {}
    else:
        dm, distance_stats = _k2p_distance_matrix(aln, task_logger, name_mapping)
        distance_model = "K2P"
    task_logger.info("NJ distance model: %s", distance_model)

    progress("Building tree...")
    constructor = DistanceTreeConstructor()
    tree = constructor.nj(dm)

    # NJ can produce negative limbs when the estimated pairwise matrix does not
    # fit an additive tree. Newick consumers generally cannot usefully display
    # them, so clamp as a representation choice and record exactly how often it
    # happened; zero here does *not* mean "no evidence of divergence".
    total_branches = 0
    negative_branches = 0
    for clade in tree.find_clades():
        if clade.branch_length is None:
            continue
        total_branches += 1
        if clade.branch_length < 0:
            clade.branch_length = 0.0
            negative_branches += 1
    if negative_branches:
        task_logger.info(
            "Clamped %d of %d negative NJ branch length(s) to zero.",
            negative_branches, total_branches,
        )
    if total_branches and negative_branches / total_branches >= NJ_DEGRADED_FRACTION:
        # Pairwise deletion makes the distance matrix non-additive (each pair is
        # measured over a different column subset), and on a set of
        # near-identical ITS sequences that shows up as many small negative
        # branches. Worth saying out loud: an NJ tree in this state is a quick
        # preview, not a result to publish.
        from app.services.log_context import log_degradation

        log_degradation(
            task_logger, "nj_many_negative_branches",
            "A large share of NJ branch lengths were negative and clamped to zero; "
            "the distance matrix fits an additive tree poorly, so the quick NJ "
            "topology is poorly determined and an ML method such as IQ-TREE is preferable",
            negative=negative_branches, total=total_branches,
        )

    inner_labels_removed = _strip_generated_inner_labels(tree)

    progress("Writing output files...")
    write_tree_file(tree, output_newick, "newick")

    # Restore names in the canonical Newick first, then parse that exact tree to
    # produce NEXUS. Textually replacing safe IDs in an already-written NEXUS
    # bypassed the serializer's duplicate/unnamed-tip validation.
    restore_tree_names(output_newick, name_mapping)
    _convert_newick_to_nexus(output_newick, output_nexus)

    metadata = {
        "distance_model": distance_model,
        "negative_branches_clamped": negative_branches,
        "inner_labels_removed": inner_labels_removed,
    }
    metadata.update(distance_stats)
    return metadata



def _check_raxml_feature(config: Config, feature_flag: str) -> bool:
    """Check if the installed RAxML-NG binary supports a specific flag/feature."""
    global _RAXML_HELP_CACHE
    
    if _RAXML_HELP_CACHE is not None:
         return feature_flag in _RAXML_HELP_CACHE

    from app.services.subprocess_utils import run_command
    try:
        cmd = [config.RAXML_BINARY, "--help"]
        _, stdout, _ = run_command(
            cmd, timeout=min(60, _tool_timeout_seconds(config, "RAxML"))
        )
        _RAXML_HELP_CACHE = stdout
        return feature_flag in stdout
    except Exception:
        return False




def _get_raxml_cmd(
    params: Any, # ResolvedRaxmlParams
    config: Config,
    sanitized_fasta: Path,
    prefix: str,
    threads: int
) -> list[str]:
    """Construct the main RAxML-NG command from resolved parameters."""
    
    cmd = [
        config.RAXML_BINARY,
        "--msa", str(sanitized_fasta),
        "--model", params.model,
        "--prefix", prefix,
        "--threads", str(threads),
        "--seed", str(params.seed),
        "--redo",
    ]
    
    # Starting Trees
    # raxml-ng --tree pars{N},rand{M} 
    cmd.extend(["--tree", params.start_tree_spec])
    
    # Bootstrapping vs ML-only
    if params.enable_bootstrap:
        # --all includes ML search + Bootstrap
        cmd.append("--all")
        # --bs-trees autoMRE{cap}
        cmd.extend(["--bs-trees", f"autoMRE{{{params.bootstrap_cap}}}"])
    else:
        # ML check only
        # Verified ML-only mode: --search
        cmd.append("--search")
        
    # Early Stopping (KH)
    # Only add if enabled AND supported (checked by caller or we assume caller checked)
    if params.enable_early_stopping:
        cmd.extend(["--stop-rule", "kh-mult"]) # or kh, but kh-mult often safer/standard

    return cmd

def _run_moose(
    alignment_fasta: Path,
    params: Any, # ResolvedRaxmlParams
    config: Config,
    task_logger,
    job_id: str
) -> Tuple[Optional[str], Path]:
    """
    Run MOOSE model selection.
    Returns (best_model, alignment_path_to_use).
    Returns ``None`` for the model when MOOSE cannot provide a usable
    selection, so the caller keeps the model the user configured. The original
    sanitized alignment is always retained so model partition ranges cannot
    drift from a MOOSE-reduced alignment.
    """
    task_logger.info("Running MOOSE model selection...")
    from app.services.subprocess_utils import run_command
    configured_model = getattr(params, "model", None) or "the configured model"
    
    prefix = str(alignment_fasta.parent / "moose_run")
    
    # Constrained search space
    # DNA: GTR, HKY, TN93. RHAS: G, I+G
    # AA: LG, JTT, WAG. RHAS: G, I+G
    if params.data_type == "DNA":
        moose_opts = "criterion=bic/rhas=G,R,I+G,I+R/freerate-categories=2-5"

    else:
        moose_opts = "criterion=bic/rhas=G,R,I+G,I+R/freerate-categories=2-5/substitution-models=LG,JTT,WAG"
        
    cmd = [
        config.RAXML_BINARY,
        "--moose",
        "--msa", str(alignment_fasta),
        "--data-type", params.data_type,
        "--moose-options", moose_opts,
        "--prefix", prefix,
        "--threads", "auto", 
        "--seed", str(params.seed),
        "--redo",
    ]
    
    if job_id:
        from app.workers.events import publish_command
        publish_command(job_id, "tree", cmd)
    
    log_file = alignment_fasta.parent.parent / "logs" / "moose.log"
    # MOOSE model selection is RAxML-NG over the whole alignment, so it gets
    # RAxML's budget. Without one it was the last unbounded call in this module,
    # and a wedged run here holds the single worker exactly as a wedged tree
    # search does -- before a tree has even been started.
    rc, stdout, stderr = run_command(
        cmd, log_file=log_file, timeout=_tool_timeout_seconds(config, "RAxML")
    )
    
    if rc != 0:
        task_logger.warning(
            f"MOOSE failed (RC={rc}). See moose.log. "
            f"Keeping {configured_model}."
        )
        return None, alignment_fasta
        
    # Check for reduced alignment
    # Wiki says: <prefix>.raxml.reduced.phy
    reduced_aln = Path(f"{prefix}.raxml.reduced.phy")
    if reduced_aln.exists():
        task_logger.info(
            "MOOSE produced a reduced alignment; retaining the original "
            "sanitized alignment to keep the selected model compatible."
        )

    # Parse output .bestModel
    # Wiki says .moose.bestModel
    best_model_file = Path(f"{prefix}.raxml.moose.bestModel")
    if not best_model_file.exists():
        # Fallback to old check just in case
        best_model_file = Path(f"{prefix}.raxml.bestModel")

    if best_model_file.exists():
        try:
            raw_model = best_model_file.read_text().strip()
            selected_model = raw_model.rsplit(",", 1)[0].strip()
            base_model = selected_model.split("+", 1)[0].split("{", 1)[0]
            if selected_model and base_model and base_model[0].isalpha():
                task_logger.info(f"MOOSE selected model configuration: {raw_model}")
                task_logger.info(f"Using MOOSE-selected model: {selected_model}")
                return selected_model, alignment_fasta
        except Exception as exc:
            task_logger.warning(f"Could not read MOOSE model output: {exc}")

        task_logger.warning(
            f"MOOSE returned an unusable model. Keeping {configured_model}."
        )
        return None, alignment_fasta

    task_logger.warning(
        f"MOOSE output file not found. Keeping {configured_model}."
    )
    return None, alignment_fasta

def _run_raxml(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
):
    """
    Run RAxML-NG tree inference with upgraded workflow.

    Returns ``(effective_model, selected_by)``. The effective model is the one
    RAxML was actually handed, which is not necessarily ``params.model`` --
    MOOSE overrides it, and the validator can substitute a data-type default.
    ``selected_by`` names the selector ("MOOSE") when something other than the
    user's setting picked the model, else ``None``.
    """
    from app.services.raxml_validator import validate_and_resolve_raxml_params
    
    # 1. Detect Data Type
    # Simple heuristic or check BioPython
    mol_type = "DNA"
    if HAS_BIOPYTHON:
        try:
             # Fast check first record using SeqIO.parse (stops lazy iterator after 1)
             # Avoids loading full alignment into memory
             # Fast heuristic check using first few records
             dna_votes = 0
             total_checked = 0
             
             for i, record in enumerate(SeqIO.parse(str(alignment_fasta), "fasta")):
                 if i >= 10: break # Check max 10 records
                 
                 seq = str(record.seq).upper().replace("-", "").replace("?", "")
                 if not seq: continue
                 
                 # Strong protein indicators: E, F, I, L, P, Q, Z
                 # If we see these, it is almost certainly Protein (or bad data, but assume Protein)
                 if any(c in seq for c in "EFILPQZ"):
                     # Found protein-specific char
                     total_checked += 1
                     continue # Counts as non-DNA (AA)
                     
                 # DNA heuristic: Count ACGTU + N
                 # Ambiguous DNA (R, Y, etc) exists, but high % of canonical bases implies DNA
                 dna_chars = sum(1 for c in seq if c in "ACGTUN")
                 if dna_chars / len(seq) > 0.85:
                     dna_votes += 1
                 total_checked += 1
                 
             # If majority of checked sequences look like AA, switch to AA.
             # (Default is DNA, so we only switch if evidence suggests AA)
             if total_checked > 0 and (dna_votes / total_checked) < 0.5:
                 mol_type = "AA"
                 
        except Exception:
            pass # Default DNA

    # 2. Validate & Resolve Parameters
    # We resolve again to sure we have the ResolvedRaxmlParams object and handle defaults robustly
    # Convert TreeBuilderParams to dict for validator
    params_dict = {
        "run_preset": params.run_preset,
        "bootstrap_preset": params.bootstrap_preset,
        "bootstrap_cap": params.bootstrap_cap,
        "enable_bootstrap": params.enable_bootstrap,
        "start_tree_override": params.start_tree_override,
        "moose_enabled": params.moose_enabled,
        "enable_early_stopping": params.early_stopping,
        "seed": params.seed,
        "outgroup": params.outgroup,
        "model": params.model
    }
    
    resolved = validate_and_resolve_raxml_params(params_dict, data_type=mol_type)
    requested_parameters = {
        "model": params.model,
        "enable_bootstrap": params.enable_bootstrap,
        "bootstrap_cap": params.bootstrap_cap,
        "bootstrap_preset": params.bootstrap_preset,
        "start_tree_override": params.start_tree_override,
        "run_preset": params.run_preset,
        "seed": params.seed,
        "outgroup": params.outgroup,
        "moose_enabled": params.moose_enabled,
        "early_stopping": params.early_stopping,
    }
    
    # Check Support for Features
    has_moose_support = _check_raxml_feature(config, "--moose")
    has_early_stop_support = _check_raxml_feature(config, "--stop-rule")

    # 3. Handle MOOSE
    moose_mapping = None
    model_selected_by = None
    moose_applied = False

    if resolved.enable_moose:
        if has_moose_support:
            sanitized_fasta_moose = output_newick.parent / "raxml_input_moose_sanitized.fasta"
            moose_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta_moose)

            best_model, _ = _run_moose(
                sanitized_fasta_moose, resolved, config, task_logger, job_id
            )
            if best_model:
                resolved.model = best_model
                model_selected_by = "MOOSE"
                moose_applied = True
            else:
                resolved.warnings.append(
                    "MOOSE did not produce a usable model; retained the validated configured model."
                )

        else:
            task_logger.warning(
                "MOOSE requested but not supported by installed RAxML-NG; "
                "using the validated configured model or data-type default."
            )
            resolved.warnings.append(
                "MOOSE was requested but is not supported by the installed RAxML-NG; "
                "using the validated configured model or data-type default."
            )

    if not resolved.model:
        # The validator deliberately leaves the model blank while MOOSE is
        # expected to choose it. If MOOSE is unavailable or fails, an empty
        # ``--model`` is not a fallback at all; install the data-type default and
        # record the substitution.
        resolved.model = "GTR+G" if resolved.data_type == "DNA" else "LG+G"
        resolved.warnings.append(
            f"No usable model remained after model selection; applied the "
            f"{resolved.data_type} default {resolved.model}."
        )
            
    # 4. Handle Early Stopping Flag
    if resolved.enable_early_stopping and not has_early_stop_support:
        task_logger.warning("Early stopping requested but not supported/found in help. Disabling.")
        resolved.enable_early_stopping = False
        resolved.warnings.append(
            "Early stopping was requested but is unsupported by the installed "
            "RAxML-NG; it was disabled."
        )

    # 5. Prepare Main Run
    prefix = str(output_newick.parent / "raxml_run")
    threads = _get_thread_count(params)
    
    if moose_mapping:
        sanitized_fasta = sanitized_fasta_moose
        name_mapping = moose_mapping
    else:
        sanitized_fasta = output_newick.parent / "raxml_input_sanitized.fasta"
        name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)
    
    cmd = _get_raxml_cmd(resolved, config, sanitized_fasta, prefix, threads)
    
    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"
    
    if job_id:
        from app.workers.events import publish_command
        publish_command(job_id, "tree", cmd)
        
        limit_hours = _tool_time_limit_hours(config, "RAxML")

        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "tree", "stdout"),
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr"),
            **_tool_limits(config, "RAxML", threads),
        )

        if exit_code != 0:
            raise RuntimeError(tool_failure_message("RAxML", exit_code, limit_hours))
    else:
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file, timeout=_tool_timeout_seconds(config, "RAxML")
        )
        if returncode != 0:
            raise RuntimeError(tool_failure_message("RAxML", returncode))
            
    # 6. Output Handling
    best_tree = Path(f"{prefix}.raxml.bestTree")
    support_tree = Path(f"{prefix}.raxml.support")
    
    # Prefer supported tree if available (BS run), else best ML tree
    source_tree = support_tree if support_tree.exists() else best_tree
    
    if not source_tree.exists():
        raise RuntimeError("RAxML output tree not found.")
        
    # Copy to final internal location
    shutil.copy(source_tree, output_newick)
    
    # 7. Post-Processing: Outgroup Rerooting
    applied_outgroup = None
    if resolved.outgroup and HAS_BIOPYTHON:
        try:
            task_logger.info(f"Rerooting tree on outgroup: {resolved.outgroup}")
            tree = Phylo.read(str(output_newick), "newick")
            
            # RAxML output uses sanitized IDs while the UI supplies an original
            # record ID. Resolve the requested ID before rerooting.
            target_name = resolved.outgroup

            for safe_id, original_header in name_mapping.items():
                original_id = original_header.split(None, 1)[0]
                if resolved.outgroup in (original_id, original_header):
                    target_name = safe_id
                    break

            if target_name == resolved.outgroup:
                for safe_id, original_header in name_mapping.items():
                    if resolved.outgroup in original_header:
                        target_name = safe_id
                        break

            # Find clade
            target_clade = next((c for c in tree.find_clades() if c.name == target_name), None)
            
            if target_clade:
                tree.root_with_outgroup(target_clade)
                # Overwrite output_newick with rooted version
                write_tree_file(tree, output_newick, "newick")
                applied_outgroup = resolved.outgroup
            else:
                task_logger.warning(f"Outgroup {target_name} not found in tree. Skipping reroot.")
                resolved.warnings.append(
                    f"Requested outgroup {resolved.outgroup!r} was not found in the "
                    "inferred tree; the tree was not outgroup-rooted."
                )
        except Exception as e:
            task_logger.error(f"Outgroup rerooting failed: {e}")
            resolved.warnings.append(
                f"Requested outgroup rooting failed ({type(e).__name__}); the tree "
                "was retained without that rooting operation."
            )
    elif resolved.outgroup:
        resolved.warnings.append(
            "Requested outgroup rooting could not be applied because Biopython "
            "tree support is unavailable."
        )

    # 8. Final Name Restoration & Nexus Conversion
    restore_tree_names(output_newick, name_mapping)
    _convert_newick_to_nexus(output_newick, output_nexus)

    applied_parameters = {
        "model": resolved.model,
        "enable_bootstrap": resolved.enable_bootstrap,
        "bootstrap_cap": resolved.bootstrap_cap if resolved.enable_bootstrap else None,
        "start_tree_spec": resolved.start_tree_spec,
        "seed": resolved.seed,
        "outgroup": applied_outgroup,
        "moose_enabled": moose_applied,
        "early_stopping": resolved.enable_early_stopping,
        "data_type": resolved.data_type,
    }
    if resolved.warnings:
        from app.services.log_context import log_degradation

        log_degradation(
            task_logger, "raxml_parameters_adjusted",
            "RAxML-NG ran with scientifically meaningful parameter adjustments; "
            "see tree_metadata.json for requested, applied, and warning details",
            warning_count=len(resolved.warnings),
        )

    return resolved.model, model_selected_by, {
        # RAxML is run with --bs-trees autoMRE{cap}, an adaptive replicate
        # count, so the submitted `bootstrap` number was never what ran.
        "bootstrap": None,
        "bootstrap_spec": (
            f"autoMRE{{{resolved.bootstrap_cap}}}" if resolved.enable_bootstrap else None
        ),
        "support_type": "bootstrap" if resolved.enable_bootstrap else None,
        "seed": resolved.seed,
        "data_type": resolved.data_type,
        "parameters_requested": requested_parameters,
        "parameters_applied": applied_parameters,
        "parameter_warnings": list(resolved.warnings),
    }


def _run_iqtree(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
):
    """Run IQ-TREE maximum likelihood tree inference."""
    prefix = str(output_newick.parent / "iqtree_run")
    threads = _get_thread_count(params)
    
    # Sanitize FASTA to create safe IDs
    sanitized_fasta = output_newick.parent / "iqtree_input_sanitized.fasta"
    name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)
    
    seed = _resolve_seed(params)
    cmd = [
        config.IQTREE_BINARY,
        "-s", str(sanitized_fasta),
        "-m", _validate_iqtree_model(params.model),
        "-nt", str(threads),
        "-pre", prefix,
        # Without an explicit seed IQ-TREE seeds from the clock and the run
        # cannot be reproduced. The value is recorded in tree_metadata.json.
        "-seed", str(seed),
        "-redo"
    ]

    if params.bootstrap and params.bootstrap > 0:
        cmd.extend(["-B", str(params.bootstrap)])

    # SH-aLRT branch test. With both -alrt and -B, IQ-TREE writes dual
    # "SH-aLRT/UFBoot" labels (e.g. "82.7/87") into <prefix>.treefile.
    alrt = params.alrt_replicates or 0
    use_alrt = alrt > 0
    if use_alrt:
        cmd.extend(["-alrt", str(alrt)])

    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "tree", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "tree", "stdout"),  # IQ-TREE writes progress to stdout
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr"),
            **_tool_limits(config, "IQ-TREE", threads),
        )

        if exit_code != 0:
            raise RuntimeError(tool_failure_message(
                "IQ-TREE", exit_code, _tool_time_limit_hours(config, "IQ-TREE")))
    else:
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file, timeout=_tool_timeout_seconds(config, "IQ-TREE")
        )

        if returncode != 0:
            raise RuntimeError(tool_failure_message(
                "IQ-TREE", returncode, _tool_time_limit_hours(config, "IQ-TREE")))
        
    # Output handling
    treefile = Path(f"{prefix}.treefile")
    contree = Path(f"{prefix}.contree")

    # The site promises the inferred maximum-likelihood tree. ``.contree`` is a
    # UFBoot consensus tree and can have a different topology; selecting it only
    # when SH-aLRT was disabled made the delivered topology depend on a support
    # toggle. IQ-TREE leaves the consensus alongside the run as a separate raw
    # artifact, but it is never substituted for the primary tree.
    if treefile.exists():
        shutil.copy(treefile, output_newick)
        
        # Restore original names in the Newick tree
        restore_tree_names(output_newick, name_mapping)
        
        _convert_newick_to_nexus(output_newick, output_nexus)
    else:
        consensus_note = " (a bootstrap consensus exists, but the ML tree is missing)" \
            if contree.exists() else ""
        raise RuntimeError(f"IQ-TREE maximum-likelihood .treefile not found{consensus_note}.")

    # When ModelFinder chose the model, "MFP" is what the user asked for but not
    # what was actually used. Report the concrete winner so the tree is
    # reproducible and citable.
    return _read_iqtree_selected_model(Path(f"{prefix}.iqtree")), seed


# ModelFinder writes exactly one such line into <prefix>.iqtree, e.g.
#   Best-fit model according to BIC: TIM2+F+I+G4
_IQTREE_BEST_MODEL_RE = re.compile(
    r"^Best-fit model according to \w+:\s*(\S+)\s*$", re.MULTILINE
)


def _read_iqtree_selected_model(report_path: Path) -> Optional[str]:
    """Return the model ModelFinder picked, or None if it did not run."""
    try:
        report_text = report_path.read_text(errors="replace")
    except OSError:
        return None
    match = _IQTREE_BEST_MODEL_RE.search(report_text)
    if not match:
        return None
    selected = match.group(1)
    # Same character hygiene as the inbound model string; this value is echoed
    # into JSON that the viewer renders.
    if len(selected) > _IQTREE_MODEL_MAX_LEN or not _IQTREE_MODEL_ALLOWED_RE.match(selected):
        return None
    return selected


# MrBayes' substitution models, as (nst, equal_base_frequencies, label).
# nst=1 is JC/F81, nst=2 is K80/HKY, nst=6 is GTR. Whether base frequencies are
# fixed equal or estimated is *not* part of nst -- MrBayes expresses it through
# `prset statefreqpr`, so JC vs F81, K2P vs HKY and SYM vs GTR are distinguished
# only by that prior. Emitting nst alone silently ran JC as F81 and SYM as GTR.
#
# The third element is the model that actually runs, which is what gets reported
# as model_selected. TN93, TIM and TVM have no MrBayes form at all; they collapse
# onto GTR and must be named GTR, not echoed back under the requested name.
_MRBAYES_MODELS = {
    "JC":    (1, True,  "JC"),
    "JC69":  (1, True,  "JC"),
    "F81":   (1, False, "F81"),
    "K80":   (2, True,  "K80"),
    "K2P":   (2, True,  "K2P"),
    "HKY":   (2, False, "HKY"),
    "HKY85": (2, False, "HKY"),
    "SYM":   (6, True,  "SYM"),
    "GTR":   (6, False, "GTR"),
    "TN93":  (6, False, "GTR"),
    "TIM":   (6, False, "GTR"),
    "TVM":   (6, False, "GTR"),
}

# Target number of posterior samples retained per run. MrBayes' default
# samplefreq=500 gave 100 samples at the old 50,000-generation default, of which a
# 25% burn-in left 75 trees to build a consensus from -- far too few for stable
# posterior probabilities.
MRBAYES_TARGET_SAMPLES_PER_RUN = 2000

# Standard convergence thresholds. ASDSF below 0.01 and PSRF within 0.02 of 1.0
# are the values the MrBayes manual recommends; ESS >= 200 is the usual floor
# for a parameter estimate to be trustworthy.
# Single source of truth for the split-frequency threshold: the same number is
# written into the MrBayes block as stopval and used to judge the ASDSF the run
# reports back, so the two can never drift apart.
MRBAYES_MAX_ASDSF = Config.DEFAULT_MCMC_STOPVAL
MRBAYES_MAX_PSRF = 1.02
MRBAYES_MIN_ESS = 200.0


def _mrbayes_lset_from_model(model_str: str, task_logger) -> Tuple[int, str, bool, str]:
    """Map a requested model string onto MrBayes ``lset``/``prset`` settings.

    Returns ``(nst, rates, equal_base_frequencies, effective_label)``.
    Previously this function did not exist and the block hardcoded
    ``nst=6 rates=gamma``, so every Bayesian run was GTR+G regardless of what
    the user selected -- while tree_metadata.json reported the selection back to
    them as though it had been honoured.

    ``effective_label`` describes the model that MrBayes will actually run, not
    the one that was asked for. Reporting the request would reintroduce the same
    defect on a smaller scale: TN93/TIM/TVM have no MrBayes form and run as GTR,
    so that is what they are called.
    """
    raw = (model_str or "").strip() or _IQTREE_DEFAULT_MODEL
    parts = [p.strip().upper() for p in raw.split("+") if p.strip()]
    base = parts[0] if parts else "GTR"
    modifiers = set(parts[1:])

    entry = _MRBAYES_MODELS.get(base.split("{", 1)[0])
    unrecognised = entry is None
    if unrecognised:
        # Includes ModelFinder requests ("MFP"), which MrBayes cannot honour.
        # Fall back to GTR+G -- the old hardcoded behaviour -- rather than to a
        # bare GTR, which would be a strictly worse model than what ran before.
        task_logger.warning(
            "Model '%s' has no MrBayes equivalent; using GTR+G (nst=6, gamma).", raw
        )
        entry = _MRBAYES_MODELS["GTR"]

    nst, equal_freqs, effective_base = entry
    if not unrecognised and effective_base != base.split("{", 1)[0]:
        task_logger.warning(
            "MrBayes has no %s model; running %s (nst=%d), which is the closest "
            "it can do.", base, effective_base, nst,
        )
    base = effective_base

    has_gamma = any(m.startswith("G") or m.startswith("R") for m in modifiers) or unrecognised
    has_invariant = "I" in modifiers
    if any(m.startswith("R") for m in modifiers):
        # MrBayes has no free-rate model; gamma is the closest available.
        task_logger.warning(
            "MrBayes has no free-rate (+R) model; using gamma-distributed rates instead."
        )

    if has_gamma and has_invariant:
        rates, suffix = "invgamma", "+I+G"
    elif has_gamma:
        rates, suffix = "gamma", "+G"
    elif has_invariant:
        rates, suffix = "propinv", "+I"
    else:
        rates, suffix = "equal", ""

    return nst, rates, equal_freqs, f"{base}{suffix}"


def _read_mrbayes_convergence(nexus_input: Path, task_logger) -> Dict[str, Any]:
    """Read the convergence diagnostics MrBayes writes beside the tree.

    MrBayes has always written these files; nothing read them, so a run that
    had not converged was delivered as a finished tree with posterior
    probabilities on it and no warning anywhere.

    - ``.mcmc`` last column is the average standard deviation of split
      frequencies (ASDSF), present whenever nruns > 1.
    - ``.pstat`` carries per-parameter minESS/avgESS and PSRF.
    """
    diagnostics: Dict[str, Any] = {}

    mcmc_path = Path(f"{nexus_input}.mcmc")
    try:
        header = None
        last_row = None
        for line in mcmc_path.read_text(errors="replace").splitlines():
            if not line or line.startswith("["):
                continue
            fields = line.split("\t")
            if header is None:
                header = fields
            else:
                last_row = fields
        if header and last_row and len(header) == len(last_row):
            for name, value in zip(header, last_row):
                lowered = name.strip().lower()
                if "stddev" in lowered:
                    diagnostics["asdsf"] = float(value)
                elif lowered == "gen":
                    # The last generation MrBayes actually sampled. With the
                    # stop rule on this is what ran, which is not necessarily
                    # the requested maximum.
                    diagnostics["mcmc_generations_completed"] = int(float(value))
    except (OSError, ValueError) as exc:
        task_logger.warning("Could not read MrBayes ASDSF: %s", exc)

    pstat_path = Path(f"{nexus_input}.pstat")
    try:
        rows = [
            line.split("\t")
            for line in pstat_path.read_text(errors="replace").splitlines()
            if line and not line.startswith("[")
        ]
        if len(rows) > 1:
            header = [h.strip().lower() for h in rows[0]]
            ess_idx = header.index("miness") if "miness" in header else None
            psrf_idx = header.index("psrf") if "psrf" in header else None
            ess_values, psrf_values = [], []
            for row in rows[1:]:
                if ess_idx is not None and ess_idx < len(row):
                    ess_values.append(float(row[ess_idx]))
                if psrf_idx is not None and psrf_idx < len(row):
                    psrf_values.append(float(row[psrf_idx]))
            if ess_values:
                diagnostics["min_ess"] = min(ess_values)
            if psrf_values:
                diagnostics["max_psrf"] = max(psrf_values)
    except (OSError, ValueError) as exc:
        task_logger.warning("Could not read MrBayes PSRF/ESS: %s", exc)

    problems = []
    asdsf = diagnostics.get("asdsf")
    if asdsf is not None and asdsf > MRBAYES_MAX_ASDSF:
        problems.append(f"ASDSF={asdsf:.4f} > {MRBAYES_MAX_ASDSF}")
    max_psrf = diagnostics.get("max_psrf")
    if max_psrf is not None and max_psrf > MRBAYES_MAX_PSRF:
        problems.append(f"max PSRF={max_psrf:.3f} > {MRBAYES_MAX_PSRF}")
    min_ess = diagnostics.get("min_ess")
    if min_ess is not None and min_ess < MRBAYES_MIN_ESS:
        problems.append(f"min ESS={min_ess:.0f} < {MRBAYES_MIN_ESS:.0f}")

    # "converged" is a claim, and it needs evidence. An empty `problems` list
    # means "nothing failed", which is not the same as "everything passed":
    # with mcmc_nruns=1 MrBayes writes no ASDSF and no PSRF at all, and a
    # truncated or missing .mcmc/.pstat yields nothing either. Reporting True
    # there stamped a tree as converged on no evidence whatsoever -- precisely
    # the failure mode this function exists to remove. The key is omitted
    # rather than set to False, because the run did not fail the check; it was
    # never checked.
    checked = [
        name for name in ("asdsf", "max_psrf", "min_ess")
        if diagnostics.get(name) is not None
    ]
    diagnostics["convergence_checked"] = bool(checked)
    if not checked:
        diagnostics["convergence_unavailable"] = (
            "MrBayes wrote no readable ASDSF, PSRF or ESS diagnostic"
        )
        from app.services.log_context import log_degradation

        log_degradation(
            task_logger, "mrbayes_convergence_unknown",
            "MrBayes convergence could not be assessed: no ASDSF, PSRF or ESS "
            "was readable (a single independent run writes none of them)",
        )
        task_logger.warning(
            "MrBayes convergence UNKNOWN: no diagnostic could be read from %s. "
            "The posterior probabilities on this tree are unverified.",
            nexus_input.name,
        )
        return diagnostics

    diagnostics["converged"] = not problems
    if problems:
        diagnostics["convergence_warnings"] = problems
        from app.services.log_context import log_degradation

        log_degradation(
            task_logger, "mrbayes_not_converged",
            "MrBayes run did not meet convergence thresholds: " + "; ".join(problems),
            asdsf=asdsf, max_psrf=max_psrf, min_ess=min_ess,
        )
        task_logger.warning(
            "MrBayes convergence check FAILED: %s. The posterior probabilities "
            "on this tree should not be trusted; re-run with more generations.",
            "; ".join(problems),
        )
    else:
        task_logger.info("MrBayes convergence check passed: %s", diagnostics)

    return diagnostics


def _run_mrbayes(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
) -> Dict[str, Any]:
    """Run MrBayes Bayesian tree inference.

    Returns metadata describing what was actually run, including the
    convergence diagnostics.
    """
    burnin_fraction = _normalize_mrbayes_burnin_fraction(
        params.mcmc_burnin_fraction
    )
    burnin_value = f"{burnin_fraction:.4f}".rstrip("0").rstrip(".")

    nst, rates, equal_freqs, effective_model = _mrbayes_lset_from_model(
        params.model, task_logger
    )

    nruns = max(1, int(params.mcmc_nruns or config.DEFAULT_MCMC_NRNS))
    nchains = max(1, int(params.mcmc_nchains or config.DEFAULT_MCMC_CHAINS))
    # The split-frequency stop rule compares independent runs against each
    # other, so it is meaningless with one run. Rather than quietly raising
    # nruns -- which would change the analysis the user asked for -- the rule is
    # dropped and the reason is logged and recorded in the metadata.
    stop_early_requested = bool(params.mcmc_stop_early)
    stop_early = stop_early_requested and nruns > 1
    stopval = MRBAYES_MAX_ASDSF

    # DEFAULT_MCMC_GENERATIONS is a ceiling chosen on the assumption that the
    # stop rule will cut the run short. When it cannot -- one independent run --
    # that ceiling turns into a promise to run the whole million generations,
    # 20x the previous default, on a worker that runs one job at a time. Only
    # the default is reduced; a generation count the user actually asked for is
    # always honoured.
    requested_ngen = int(params.mcmc_generations or 0)
    if requested_ngen > 0:
        ngen = max(1000, requested_ngen)
    elif stop_early:
        ngen = max(1000, int(config.DEFAULT_MCMC_GENERATIONS))
    else:
        ngen = max(1000, int(config.DEFAULT_MCMC_GENERATIONS_FIXED_RUN))
        task_logger.info(
            "No stop rule is in effect, so this run cannot end early; using the "
            "fixed-run default of %d generations rather than the %d-generation "
            "ceiling.", ngen, config.DEFAULT_MCMC_GENERATIONS,
        )

    if stop_early_requested and not stop_early:
        task_logger.warning(
            "MrBayes convergence-based early stopping was requested but needs at "
            "least 2 independent runs; running %d run(s) for the full %d "
            "generations instead.", nruns, ngen,
        )
    samplefreq = max(10, ngen // MRBAYES_TARGET_SAMPLES_PER_RUN)
    printfreq = max(100, ngen // 100)
    seed = _resolve_seed(params)

    # Sanitize FASTA to create safe IDs before converting to NEXUS
    sanitized_fasta = output_newick.parent / "mrbayes_input_sanitized.fasta"
    name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)

    # MrBayes requires Nexus input with a block
    nexus_input = output_newick.parent / "mrbayes_input.nex"
    _convert_fasta_to_nexus(sanitized_fasta, nexus_input)

    # Append MrBayes block
    with open(nexus_input, "a") as f:
        f.write("\nbegin mrbayes;\n")
        f.write("   set autoclose=yes nowarn=yes;\n")
        # Seeds are set explicitly so a Bayesian run is reproducible; MrBayes
        # otherwise seeds from the clock and records nothing.
        f.write(f"   set seed={seed} swapseed={seed};\n")
        f.write(f"   lset nst={nst} rates={rates};\n")
        if equal_freqs:
            # JC, K2P and SYM are their nst siblings with base frequencies held
            # equal instead of estimated. Without this prior MrBayes runs F81,
            # HKY and GTR respectively, which is what it silently did before.
            f.write("   prset statefreqpr=fixed(equal);\n")
        mcmc_opts = (
            f"ngen={ngen} nchains={nchains} nruns={nruns} "
            f"samplefreq={samplefreq} printfreq={printfreq} "
            f"relburnin=yes burninfrac={burnin_value}"
        )
        if stop_early:
            # ngen becomes an upper bound: MrBayes stops as soon as the average
            # standard deviation of split frequencies between the independent
            # runs drops below stopval, which saves computation without
            # claiming anything about ESS or PSRF -- those are still checked
            # after the run by _read_mrbayes_convergence().
            # mcmcdiagn is stated explicitly rather than relied on: the
            # diagnostics it writes are what the stop rule is evaluated from.
            mcmc_opts += (
                f" mcmcdiagn=yes stoprule=yes stopval={stopval}"
            )
        f.write(f"   mcmc {mcmc_opts};\n")
        f.write(f"   sump relburnin=yes burninfrac={burnin_value};\n")
        f.write(f"   sumt relburnin=yes burninfrac={burnin_value};\n")
        # Without an explicit quit MrBayes drops to its interactive prompt and
        # reads stdin, which the worker does not own.
        f.write("   quit;\n")
        f.write("end;\n")

    cmd = [config.MRBAYES_BINARY, str(nexus_input)]

    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"

    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "tree", cmd)

        # MrBayes prints progress to stdout
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "tree", "stdout"),  # MrBayes uses stdout
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr"),
            **_tool_limits(config, "MrBayes", _get_thread_count(params)),
        )

        if exit_code != 0:
            raise RuntimeError(tool_failure_message(
                "MrBayes", exit_code, _tool_time_limit_hours(config, "MrBayes")
            ))
    else:
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file, timeout=_tool_timeout_seconds(config, "MrBayes")
        )

        if returncode != 0:
            task_logger.error(f"MrBayes failed. RC={returncode}")
            task_logger.error(f"STDOUT: {stdout}")
            task_logger.error(f"STDERR: {stderr}")
            raise RuntimeError(tool_failure_message(
                "MrBayes", returncode, _tool_time_limit_hours(config, "MrBayes")
            ))

    # Output: <input>.con.tre (Consensus tree)
    con_tree = Path(f"{nexus_input}.con.tre")

    if con_tree.exists():
        shutil.copy(con_tree, output_nexus)
        _convert_nexus_to_newick(output_nexus, output_newick)

        # Restore original names in output files
        restore_tree_names(output_newick, name_mapping)
        restore_tree_names(output_nexus, name_mapping)
    else:
        raise RuntimeError("MrBayes consensus tree not found.")

    metadata = {
        "model_selected": effective_model,
        "mrbayes_lset": f"nst={nst} rates={rates}",
        "mrbayes_prset": (
            "statefreqpr=fixed(equal)" if equal_freqs else "statefreqpr=dirichlet (default)"
        ),
        "mcmc_samplefreq": samplefreq,
        # What was actually written into the MrBayes block, so a run can be
        # described accurately later without re-reading the NEXUS file.
        "mcmc_max_generations": ngen,
        "mcmc_nruns": nruns,
        "mcmc_nchains": nchains,
        "mcmc_burnin_fraction": burnin_fraction,
        "mcmc_stop_early_requested": stop_early_requested,
        "mcmc_stoprule": stop_early,
        "seed": seed,
    }
    if stop_early:
        metadata["mcmc_stopval"] = stopval
    metadata.update(_read_mrbayes_convergence(nexus_input, task_logger))

    # Was the run cut short by the stop rule? The .mcmc file's last sampled
    # generation is the only reliable record of that; a full-length run always
    # samples within one samplefreq of ngen, so a larger gap can only mean
    # MrBayes stopped itself. Left absent rather than guessed when the
    # generation count could not be read.
    completed = metadata.get("mcmc_generations_completed")
    if stop_early and isinstance(completed, int):
        metadata["mcmc_stopped_at_stopval"] = (ngen - completed) >= samplefreq
    return metadata


def _convert_newick_to_nexus(newick_path: Path, nexus_path: Path):
    """Convert Newick tree to NEXUS format.

    Goes through tree_io rather than Biopython's NEXUS writer, which emitted
    TAXLABELS unquoted and space-separated and so produced a file that no
    NEXUS reader could parse whenever a label contained a space, comma,
    parenthesis or semicolon -- i.e. almost every job on this site.
    """
    if not HAS_BIOPYTHON:
        return
    if not newick_file_to_nexus(newick_path, nexus_path):
        # The Newick is the authoritative artifact and already exists; a failed
        # NEXUS conversion is a degraded result, not a failed job.
        from app.services.log_context import log_degradation

        log_degradation(
            logger, "nexus_conversion_failed",
            "Tree was built but could not be exported to NEXUS",
            newick=str(newick_path),
        )


def _convert_nexus_to_newick(nexus_path: Path, newick_path: Path):
    """
    Convert NEXUS tree to Newick format.
    
    Handles MrBayes annotations like [&prob=...] by extracting
    posterior probabilities as node labels.
    """
    if HAS_BIOPYTHON:
        try:
            import re
            
            # Helper definitions for robust matching
            _NUM_CORE = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+\-]?[0-9]+)?"
            
            def _fmt_prob(s: str) -> str:
                try:
                    x = float(s)
                    return f"{x:.3f}".rstrip("0").rstrip(".")
                except (ValueError, TypeError):
                    return s

            content = nexus_path.read_text()
            
            # Case A: branch length comes first:  ): 0.05 [&prob=...]
            pat_a = re.compile(
                rf"\)(?:\s*:\s*(?P<branch>{_NUM_CORE}))\s*\[[^\]]*?\bprob=\s*(?P<prob>{_NUM_CORE})\s*[^\]]*?\]",
                re.IGNORECASE
            )

            # Case B: prob comes first:  )[&prob=...]:0.05
            pat_b = re.compile(
                rf"\)\s*\[[^\]]*?\bprob=\s*(?P<prob>{_NUM_CORE})\s*[^\]]*?\](?:\s*:\s*(?P<branch>{_NUM_CORE}))?",
                re.IGNORECASE
            )

            def repl(m: re.Match) -> str:
                prob_val = _fmt_prob(m.group('prob'))
                branch_val = m.group('branch')
                branch_str = f":{branch_val}" if branch_val else ""
                return f"){prob_val}{branch_str}"

            clean_content = pat_a.sub(repl, content)
            clean_content = pat_b.sub(repl, clean_content)

            # Remove any remaining [...] annotations
            clean_content = re.sub(r"\[[^\]]*\]", "", clean_content)
            
            from io import StringIO
            tree = Phylo.read(StringIO(clean_content), "nexus")
            write_tree_file(tree, newick_path, "newick")
        except Exception as e:
            logger.error(f"Failed to convert Nexus to Newick: {e}")
            raise


def _convert_fasta_to_nexus(fasta_path: Path, nexus_path: Path):
    """Convert aligned FASTA to NEXUS format for MrBayes."""
    if HAS_BIOPYTHON:
        # Read alignment
        aln = AlignIO.read(str(fasta_path), "fasta")
        
        # Detect molecule type
        if len(aln) > 0:
            seq_str = str(aln[0].seq).upper()
            dna_chars = set("ACGTN-")
            match_count = sum(1 for c in seq_str if c in dna_chars)
            is_dna = (match_count / len(seq_str)) > 0.8
            
            mol_type = "DNA" if is_dna else "protein"
            
            # Annotate records and sanitize IDs
            for record in aln:
                record.annotations["molecule_type"] = mol_type
                # Sanitize ID for MrBayes
                clean_id = record.id.replace("|", "_").replace(":", "_").replace("-", "_").replace("'", "").replace(" ", "_")
                clean_id = "".join(c for c in clean_id if c.isalnum() or c == "_")
                record.id = clean_id
                record.description = ""
                
        AlignIO.write(aln, str(nexus_path), "nexus")
    else:
        raise RuntimeError("BioPython required for format conversion.")


def _run_fasttree(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
):
    """
    Run FastTree 2.2.0 tree inference.

    FastTree writes the tree to stdout.
    Command: fasttree -gtr -nt -gamma -boot 1000 alignment.fasta > tree.newick

    Note: -boot sets the number of resamples for FastTree's SH-like local
    support test, not a bootstrap analysis. Node values are 0-1 SH-like
    supports. params.bootstrap is deliberately unused here.
    """
    
    # Check binary existence
    binary_path = Path(config.FASTTREE_BINARY)
    if not binary_path.exists() and not shutil.which(config.FASTTREE_BINARY):
        raise RuntimeError(f"FastTree binary not found at {config.FASTTREE_BINARY}")

    # Sanitize FASTA
    sanitized_fasta = output_newick.parent / "fasttree_input_sanitized.fasta"
    name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)
    
    # Build command
    seed = _resolve_seed(params)
    cmd = [
        config.FASTTREE_BINARY,
        "-gtr",
        "-nt",
        "-gamma",
        # Resamples for the SH-like local support test (not bootstrap replicates)
        "-boot", str(FASTTREE_SH_RESAMPLES),
        # FastTree's resampling is otherwise seeded from the clock, so the
        # support values changed between identical runs.
        "-seed", str(seed),
    ]

    # Check if alignment is valid (not empty)
    if sanitized_fasta.stat().st_size == 0:
         raise RuntimeError("Input alignment is empty.")

    cmd.append(str(sanitized_fasta))

    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"

    task_logger.info(f"Running FastTree: {' '.join(cmd)}")

    if job_id:
        from app.workers.events import publish_command
        # We perform the redirect manually, but show it in the command event
        display_cmd = cmd + [">", str(output_newick)]
        publish_command(job_id, "tree", display_cmd)

        # FastTree writes progress to stderr, tree to stdout
        # stdout_path will automatically be opened by run_command_streaming
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            stdout_path=output_newick,
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr"),
            # FastTree is single-threaded in this build, so the CPU budget
            # tracks the wall-clock one.
            **_tool_limits(config, "FastTree", 1),
        )

        if exit_code != 0:
             raise RuntimeError(tool_failure_message(
                 "FastTree", exit_code, _tool_time_limit_hours(config, "FastTree")))

    else:
        # specific handling if we assume run_command captures stdout
        # run_command returns (returncode, stdout, stderr)
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file, timeout=_tool_timeout_seconds(config, "FastTree")
        )

        if returncode != 0:
            task_logger.error(f"FastTree failed. RC={returncode}")
            task_logger.error(f"STDERR: {stderr}")
            raise RuntimeError(tool_failure_message("FastTree", returncode))

        # Write stdout to newick file
        with open(output_newick, "w") as f:
            f.write(stdout)

    # Check output
    if not output_newick.exists() or output_newick.stat().st_size == 0:
        raise RuntimeError("FastTree produced no output.")

    # Restore original names
    restore_tree_names(output_newick, name_mapping)

    # Convert to Nexus
    _convert_newick_to_nexus(output_newick, output_nexus)

    return {"seed": seed, "model_selected": "GTR+G"}
