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
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from app.config import Config
from app.models import TreeBuilderParams
from app.services.subprocess_utils import run_command, run_command_streaming

# Try to import Bio.Phylo for NJ, or implement simple fallback
try:
    from Bio import Phylo, AlignIO
    from Bio.Phylo.TreeConstruction import DistanceCalculator, DistanceTreeConstructor
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)


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

    try:
        if method == "nj":
            _run_neighbor_joining(alignment_fasta, output_newick, output_nexus, task_logger, job_id)
        elif method == "raxml":
            _run_raxml(alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id)
        elif method == "iqtree":
            _run_iqtree(alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id)
        elif method == "mrbayes":
            _run_mrbayes(alignment_fasta, output_newick, output_nexus, params, config, task_logger, job_id)
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
    
    # Publish progress if job_id provided
    if job_id:
        from app.workers.events import publish_log
        publish_log(job_id, "tree", "stderr", "Reading alignment...")
    
    # Read alignment
    aln = AlignIO.read(str(alignment_fasta), "fasta")
    
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


def _run_raxml(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    task_logger,
    job_id: Optional[str] = None
):
    """Run RAxML-NG tree inference."""
    prefix = str(output_newick.parent / "raxml_run")
    threads = _get_thread_count(params)
    
    cmd = [
        config.RAXML_BINARY,
        "--msa", str(alignment_fasta),
        "--model", params.model,
        "--prefix", prefix,
        "--threads", str(threads),
        "--seed", "12345"  # Reproducibility
    ]
    
    if params.bootstrap and params.bootstrap > 0:
        cmd.extend(["--all", "--bs-trees", str(params.bootstrap)])
    else:
        # Just ML search
        cmd.append("--search")
        
    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "tree", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "tree", "stdout"),  # RAxML writes progress to stdout
            on_stderr_line=_make_log_callback(job_id, "tree", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"RAxML failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"RAxML failed with return code {returncode}. See logs.")
        
    # Output handling
    best_tree = Path(f"{prefix}.raxml.bestTree")
    support_tree = Path(f"{prefix}.raxml.support")
    
    source_tree = support_tree if support_tree.exists() else best_tree
    
    if source_tree.exists():
        shutil.copy(source_tree, output_newick)
        _convert_newick_to_nexus(output_newick, output_nexus)
    else:
        raise RuntimeError("RAxML output tree not found.")


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
    
    cmd = [
        config.IQTREE_BINARY,
        "-s", str(alignment_fasta),
        "-m", params.model,
        "-nt", str(threads),
        "-pre", prefix,
        "-redo"
    ]
    
    if params.bootstrap and params.bootstrap > 0:
        cmd.extend(["-B", str(params.bootstrap)])
        
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
            raise RuntimeError(f"IQ-TREE failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"IQ-TREE failed with return code {returncode}. See logs.")
        
    # Output handling
    treefile = Path(f"{prefix}.treefile")
    contree = Path(f"{prefix}.contree")
    
    source_tree = contree if contree.exists() else treefile
    
    if source_tree.exists():
        shutil.copy(source_tree, output_newick)
        _convert_newick_to_nexus(output_newick, output_nexus)
    else:
        raise RuntimeError("IQ-TREE output tree not found.")


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
    # MrBayes requires Nexus input with a block
    nexus_input = output_newick.parent / "mrbayes_input.nex"
    _convert_fasta_to_nexus(alignment_fasta, nexus_input)
    
    # Append MrBayes block
    with open(nexus_input, "a") as f:
        f.write("\nbegin mrbayes;\n")
        f.write(f"   set autoclose=yes;\n")
        f.write(f"   lset nst=6 rates=gamma;\n")  # GTR+G equivalent
        f.write(f"   mcmc ngen={params.mcmc_generations} nchains={params.mcmc_nchains} nruns={params.mcmc_nruns} burninfrac=0.25;\n")
        f.write(f"   sump;\n")
        f.write(f"   sumt;\n")
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
            raise RuntimeError(f"MrBayes failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            task_logger.error(f"MrBayes failed. RC={returncode}")
            task_logger.error(f"STDOUT: {stdout}")
            task_logger.error(f"STDERR: {stderr}")
            raise RuntimeError(f"MrBayes failed with return code {returncode}. Error: {stderr}")
        
    # Output: <input>.con.tre (Consensus tree)
    con_tree = Path(f"{nexus_input}.con.tre")
    
    if con_tree.exists():
        shutil.copy(con_tree, output_nexus)
        _convert_nexus_to_newick(output_nexus, output_newick)
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
