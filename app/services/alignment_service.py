"""
Alignment service module.

Provides functions for running multiple sequence alignment using various tools:
- MAFFT
- MUSCLE
- Clustal Omega
- IQ-TREE (built-in alignment)

When job_id is provided, streams log output to Redis for real-time SSE updates.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

from app.config import Config
from app.models import AlignmentParams
from app.services.subprocess_utils import run_command, run_command_streaming


def run_alignment(
    input_fasta: Path,
    output_fasta: Path,
    params: AlignmentParams,
    config: Config,
    logger,
    job_id: Optional[str] = None
) -> None:
    """
    Run a multiple sequence alignment according to user-selected or default parameters.
    
    Supported methods:
      - mafft
      - muscle
      - clustalo
      - iqtree_builtin
      - default (beginner mode)
    
    Args:
        input_fasta: Path to input FASTA file
        output_fasta: Path for output aligned FASTA
        params: Alignment parameters
        config: Application config
        logger: Logger instance
        job_id: Optional job ID for real-time event streaming
    """
    method = params.method.lower()
    
    if method == "default":
        # Beginner mode default: use configured default aligner (e.g. mafft)
        method = config.BEGINNER_DEFAULT_ALIGNER.lower()
        logger.info(f"Using default aligner: {method}")

    logger.info(f"Starting alignment with method: {method}")
    
    try:
        if method == "none":
            logger.info("Skipping alignment (method='none'). Copying input to output.")
            shutil.copy(input_fasta, output_fasta)
        elif method == "mafft":
            _run_mafft(input_fasta, output_fasta, params, config, logger, job_id)
        elif method == "muscle":
            _run_muscle(input_fasta, output_fasta, params, config, logger, job_id)
        elif method == "clustalo":
            _run_clustalo(input_fasta, output_fasta, params, config, logger, job_id)
        elif method == "iqtree_builtin":
            _run_iqtree_builtin(input_fasta, output_fasta, params, config, logger, job_id)
        else:
            raise ValueError(f"Unsupported alignment method: {method}")
            
        if not output_fasta.exists() or output_fasta.stat().st_size == 0:
             raise RuntimeError(f"Alignment failed: Output file {output_fasta} is missing or empty.")

        logger.info(f"Alignment completed successfully. Output: {output_fasta}")

    except Exception as e:
        logger.error(f"Alignment failed: {e}")
        raise


def _get_thread_count():
    return min(8, os.cpu_count() or 1)


def _make_log_callback(job_id: Optional[str], step: str, stream: str):
    """Create a callback function for streaming output to Redis."""
    if not job_id:
        return None
    
    from app.workers.events import publish_log
    
    def callback(line: str):
        publish_log(job_id, step, stream, line)
    
    return callback


def _run_mafft(
    input_fasta: Path,
    output_fasta: Path,
    params: AlignmentParams,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """
    Run MAFFT alignment.
    
    MAFFT writes alignment to stdout, so we redirect stdout to file
    and stream stderr to Redis for progress updates.
    """
    threads = _get_thread_count()
    cmd = [config.MAFFT_BINARY, "--thread", "2", "--adjustdirectionaccurately"]
    
    # Check if this is a fast NJ tree build
    tree_method = params.advanced_options.get("tree_method", "").lower()
    
    # Add advanced options if any
    if params.advanced_options.get("auto", False):
         cmd.append("--auto")
    elif params.advanced_options.get("localpair", False):
         cmd.append("--localpair")
         cmd.append("--maxiterate")
         cmd.append("1000")
    elif params.advanced_options.get("globalpair", False):
         cmd.append("--globalpair")
         cmd.append("--maxiterate")
         cmd.append("1000")
    elif tree_method == "nj":
         # Fast settings for Neighbor-Joining quick tree
         cmd.extend(["--retree", "2", "--maxiterate", "2"])
    else:
         # Default reasonable option for ML/Bayesian trees
         cmd.append("--auto")

    cmd.append(str(input_fasta))
    
    log_file = output_fasta.parent.parent / "logs" / "alignment.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "align", cmd)
        
        # Use streaming runner: stdout → file, stderr → Redis + log
        exit_code, stats = run_command_streaming(
            cmd,
            stdout_path=output_fasta,  # MAFFT writes alignment to stdout
            stderr_path=log_file,
            on_stderr_line=_make_log_callback(job_id, "align", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"MAFFT failed with exit code {exit_code}")
    else:
        # Fallback to non-streaming for backward compatibility
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"MAFFT failed with return code {returncode}. See logs.")
            
        with open(output_fasta, "w") as f:
            f.write(stdout)


def _run_muscle(
    input_fasta: Path,
    output_fasta: Path,
    params: AlignmentParams,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """
    Run MUSCLE alignment.
    
    Uses MUSCLE v5 syntax (-align/-output).
    """
    # MUSCLE v5 uses -align and -output (v3 used -in/-out)
    cmd = [config.MUSCLE_BINARY, "-align", str(input_fasta), "-output", str(output_fasta)]
    
    log_file = output_fasta.parent.parent / "logs" / "alignment.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "align", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stderr_line=_make_log_callback(job_id, "align", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"MUSCLE failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"MUSCLE failed with return code {returncode}. See logs.")


def _run_clustalo(
    input_fasta: Path,
    output_fasta: Path,
    params: AlignmentParams,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """
    Run Clustal Omega alignment.
    
    Note: Clustal Omega threading support varies by version.
    """
    cmd = [config.CLUSTALO_BINARY, "-i", str(input_fasta), "-o", str(output_fasta), "--force"]
    
    log_file = output_fasta.parent.parent / "logs" / "alignment.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "align", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stderr_line=_make_log_callback(job_id, "align", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"Clustal Omega failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"Clustal Omega failed with return code {returncode}. See logs.")


def _run_iqtree_builtin(
    input_fasta: Path,
    output_fasta: Path,
    params: AlignmentParams,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """
    Run IQ-TREE built-in alignment.
    
    Uses IQ-TREE's --align-only mode.
    """
    prefix = str(output_fasta.with_suffix(''))  # remove .fasta
    threads = _get_thread_count()
    
    cmd = [
        config.IQTREE_BINARY,
        "-s", str(input_fasta),
        "--align-only",
        "-nt", str(threads),
        "-pre", prefix,
        "-redo"  # Overwrite existing
    ]
    
    log_file = output_fasta.parent.parent / "logs" / "alignment.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "align", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stderr_line=_make_log_callback(job_id, "align", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"IQ-TREE alignment failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"IQ-TREE alignment failed with return code {returncode}. See logs.")
    
    # IQ-TREE output file handling
    # Check for likely output files
    possible_outputs = [
        Path(f"{prefix}.fasta"),
        Path(f"{prefix}.fa"),
        Path(f"{prefix}.phy"),
        Path(f"{prefix}.nex")
    ]
    
    found = False
    for p in possible_outputs:
        if p.exists():
            if p != output_fasta:
                import shutil
                shutil.move(p, output_fasta)
            found = True
            break
            
    if not found:
        logger.warning("Could not locate IQ-TREE output file. Checking directory...")
