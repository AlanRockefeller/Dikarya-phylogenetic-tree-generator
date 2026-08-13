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
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from app.config import Config
from app.models import TreeBuilderParams

_RAXML_HELP_CACHE = None
from app.services.subprocess_utils import (
    run_command,
    run_command_streaming,
    tool_failure_message,
)
from app.services.fasta_utils import sanitize_fasta_headers, restore_tree_names

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
    
    metadata = {
        "method": method,
        "model": params.model,
        "bootstrap": params.bootstrap,
        "run_dir": str(output_newick.parent)
    }
    if method == "fasttree":
        # FastTree ignores params.bootstrap entirely: _run_fasttree hardcodes
        # -boot N, which computes SH-like local supports (0-1), not bootstrap
        # proportions. Recording a bootstrap count here made the viewer report a
        # number that was both wrong and the wrong kind of support value.
        metadata["bootstrap"] = None
        metadata["support_type"] = "sh_like"
        metadata["support_resamples"] = FASTTREE_SH_RESAMPLES
    if method == "iqtree" and (params.alrt_replicates or 0) > 0:
        # Node labels are dual "SH-aLRT/UFBoot" values, both on a 0-100 scale.
        metadata["support_type"] = "alrt_ufboot"
        metadata["alrt_replicates"] = params.alrt_replicates
    if method == "mrbayes":
        burnin_fraction = _normalize_mrbayes_burnin_fraction(
            params.mcmc_burnin_fraction
        )
        metadata.update({
            "mcmc_generations": params.mcmc_generations,
            "mcmc_nruns": params.mcmc_nruns,
            "mcmc_nchains": params.mcmc_nchains,
            "mcmc_burnin_fraction": burnin_fraction,
        })

    try:
        if method == "nj":
            _run_neighbor_joining(alignment_fasta, output_newick, output_nexus, task_logger, job_id)
        elif method == "raxml":
            _run_raxml(alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id)
        elif method == "iqtree":
            selected_model = _run_iqtree(
                alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id
            )
            if selected_model:
                # params.model stays as requested ("MFP"); model_selected is what
                # ModelFinder actually fit. Both matter: one is the setting, the
                # other is what you cite.
                metadata["model_selected"] = selected_model
                if _is_model_finder_request(params.model):
                    task_logger.info(f"ModelFinder selected substitution model: {selected_model}")
        elif method == "mrbayes":
            _run_mrbayes(alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id)
        elif method == "fasttree":
            _run_fasttree(alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id)
        else:
            raise ValueError(f"Unsupported tree building method: {method}")

        if not output_newick.exists() or output_newick.stat().st_size == 0:
             raise RuntimeError(f"Tree building failed: Output file {output_newick} is missing or empty.")

        task_logger.info(f"Tree building completed successfully. Output: {output_newick}")
        return metadata

    except Exception as e:
        task_logger.error(f"Tree building failed: {e}")
        raise


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


def _run_neighbor_joining(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    task_logger,
    job_id: Optional[str] = None
):
    """
    Build a fast NJ tree using BioPython.
    
    NJ is fast and runs in-process, so no streaming needed.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython is required for NJ but not installed.")
        
    task_logger.info("Running Neighbor Joining using BioPython...")
    
    # Sanitize input FASTA to create safe IDs
    sanitized_fasta = output_newick.parent / "nj_input_sanitized.fasta"
    name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)
    
    # Publish progress if job_id provided
    if job_id:
        from app.workers.events import publish_log
        publish_log(job_id, "tree", "stderr", "Reading alignment...")
    
    # Read alignment (use sanitized version)
    aln = AlignIO.read(str(sanitized_fasta), "fasta")
    
    if job_id:
        from app.workers.events import publish_log
        publish_log(job_id, "tree", "stderr", "Calculating distance matrix...")
    
    # Calculate distance matrix
    calculator = DistanceCalculator('identity')
    dm = calculator.get_distance(aln)
    
    if job_id:
        from app.workers.events import publish_log
        publish_log(job_id, "tree", "stderr", "Building tree...")
    
    # Build tree
    constructor = DistanceTreeConstructor()
    tree = constructor.nj(dm)
    
    if job_id:
        from app.workers.events import publish_log
        publish_log(job_id, "tree", "stderr", "Writing output files...")
    
    # Write Newick
    Phylo.write(tree, str(output_newick), "newick")
    
    # Write Nexus
    Phylo.write(tree, str(output_nexus), "nexus")
    
    # Restore original names in output files
    restore_tree_names(output_newick, name_mapping)
    restore_tree_names(output_nexus, name_mapping)



def _check_raxml_feature(config: Config, feature_flag: str) -> bool:
    """Check if the installed RAxML-NG binary supports a specific flag/feature."""
    global _RAXML_HELP_CACHE
    
    if _RAXML_HELP_CACHE is not None:
         return feature_flag in _RAXML_HELP_CACHE

    from app.services.subprocess_utils import run_command
    try:
        cmd = [config.RAXML_BINARY, "--help"]
        _, stdout, _ = run_command(cmd)
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
    rc, stdout, stderr = run_command(cmd, log_file=log_file)
    
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
    """Run RAxML-NG tree inference with upgraded workflow."""
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
    
    # Check Support for Features
    has_moose_support = _check_raxml_feature(config, "--moose")
    has_early_stop_support = _check_raxml_feature(config, "--stop-rule")

    # 3. Handle MOOSE
    moose_mapping = None
    
    if resolved.enable_moose:
        if has_moose_support:
            sanitized_fasta_moose = output_newick.parent / "raxml_input_moose_sanitized.fasta"
            moose_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta_moose)
            
            best_model, _ = _run_moose(
                sanitized_fasta_moose, resolved, config, task_logger, job_id
            )
            if best_model:
                resolved.model = best_model
                 
        else:
            task_logger.warning("MOOSE requested but not supported by installed RAxML-NG. Using default model.")
            
    # 4. Handle Early Stopping Flag
    if resolved.enable_early_stopping and not has_early_stop_support:
        task_logger.warning("Early stopping requested but not supported/found in help. Disabling.")
        resolved.enable_early_stopping = False

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
        
        limit_hours = float(getattr(config, "RAXML_TIME_LIMIT_HOURS", 15) or 15)
        limit_seconds = int(limit_hours * 3600)

        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "tree", "stdout"),
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr"),
            timeout=limit_seconds,
            # RLIMIT_CPU is summed across threads, so the wall-clock budget has
            # to be multiplied by the thread count (plus headroom) or the kernel
            # kills RAxML a fraction of the way into the time it was granted.
            cpu_limit_seconds=int(limit_seconds * max(1, threads) * 1.2),
        )

        if exit_code != 0:
            raise RuntimeError(tool_failure_message("RAxML", exit_code, limit_hours))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
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
                Phylo.write(tree, str(output_newick), "newick")
            else:
                task_logger.warning(f"Outgroup {target_name} not found in tree. Skipping reroot.")
        except Exception as e:
            task_logger.error(f"Outgroup rerooting failed: {e}")

    # 8. Final Name Restoration & Nexus Conversion
    restore_tree_names(output_newick, name_mapping)
    _convert_newick_to_nexus(output_newick, output_nexus)


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
    
    cmd = [
        config.IQTREE_BINARY,
        "-s", str(sanitized_fasta),
        "-m", _validate_iqtree_model(params.model),
        "-nt", str(threads),
        "-pre", prefix,
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
        )
        
        if exit_code != 0:
            raise RuntimeError(tool_failure_message("IQ-TREE", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(tool_failure_message("IQ-TREE", returncode))
        
    # Output handling
    treefile = Path(f"{prefix}.treefile")
    contree = Path(f"{prefix}.contree")

    # .contree (the UFBoot consensus tree) carries UFBoot values only, so it
    # silently drops the SH-aLRT half of the dual labels. When SH-aLRT was
    # requested we must take .treefile, which carries both.
    if use_alrt and treefile.exists():
        source_tree = treefile
    else:
        source_tree = contree if contree.exists() else treefile

    if source_tree.exists():
        shutil.copy(source_tree, output_newick)
        
        # Restore original names in the Newick tree
        restore_tree_names(output_newick, name_mapping)
        
        _convert_newick_to_nexus(output_newick, output_nexus)
    else:
        raise RuntimeError("IQ-TREE output tree not found.")

    # When ModelFinder chose the model, "MFP" is what the user asked for but not
    # what was actually used. Report the concrete winner so the tree is
    # reproducible and citable.
    return _read_iqtree_selected_model(Path(f"{prefix}.iqtree"))


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


def _run_mrbayes(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
):
    """Run MrBayes Bayesian tree inference."""
    burnin_fraction = _normalize_mrbayes_burnin_fraction(
        params.mcmc_burnin_fraction
    )
    burnin_value = f"{burnin_fraction:.4f}".rstrip("0").rstrip(".")

    # Sanitize FASTA to create safe IDs before converting to NEXUS
    sanitized_fasta = output_newick.parent / "mrbayes_input_sanitized.fasta"
    name_mapping = sanitize_fasta_headers(alignment_fasta, sanitized_fasta)
    
    # MrBayes requires Nexus input with a block
    nexus_input = output_newick.parent / "mrbayes_input.nex"
    _convert_fasta_to_nexus(sanitized_fasta, nexus_input)
    
    # Append MrBayes block
    with open(nexus_input, "a") as f:
        f.write("\nbegin mrbayes;\n")
        f.write(f"   set autoclose=yes;\n")
        f.write(f"   lset nst=6 rates=gamma;\n")  # GTR+G equivalent
        f.write(
            f"   mcmc ngen={params.mcmc_generations} nchains={params.mcmc_nchains} "
            f"nruns={params.mcmc_nruns} relburnin=yes burninfrac={burnin_value};\n"
        )
        f.write(f"   sump relburnin=yes burninfrac={burnin_value};\n")
        f.write(f"   sumt relburnin=yes burninfrac={burnin_value};\n")
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
        )
        
        if exit_code != 0:
            raise RuntimeError(tool_failure_message("MrBayes", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            task_logger.error(f"MrBayes failed. RC={returncode}")
            task_logger.error(f"STDOUT: {stdout}")
            task_logger.error(f"STDERR: {stderr}")
            raise RuntimeError(tool_failure_message("MrBayes", returncode))
        
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


def _convert_newick_to_nexus(newick_path: Path, nexus_path: Path):
    """Convert Newick tree to NEXUS format."""
    if HAS_BIOPYTHON:
        try:
            tree = Phylo.read(str(newick_path), "newick")
            Phylo.write(tree, str(nexus_path), "nexus")
        except Exception:
            pass  # Best effort


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
            Phylo.write(tree, str(newick_path), "newick")
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
    cmd = [
        config.FASTTREE_BINARY,
        "-gtr",
        "-nt", 
        "-gamma",
        # Resamples for the SH-like local support test (not bootstrap replicates)
        "-boot", str(FASTTREE_SH_RESAMPLES)
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
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr")
        )
            
        if exit_code != 0:
             raise RuntimeError(tool_failure_message("FastTree", exit_code))

    else:
        # specific handling if we assume run_command captures stdout
        # run_command returns (returncode, stdout, stderr)
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
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
