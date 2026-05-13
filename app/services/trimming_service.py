"""
Trimming service module.

Provides functions for trimming multiple sequence alignments:
- trimAl
- BMGE

When job_id is provided, streams log output to Redis for real-time SSE updates.
"""

import shutil
from pathlib import Path
from typing import Optional

from app.config import Config
from app.services.subprocess_utils import run_command, run_command_streaming


def run_trimming(
    input_alignment: Path,
    output_alignment: Path,
    trim_method: str,
    config: Config,
    logger,
    job_id: Optional[str] = None
) -> None:
    """
    Apply gap trimming to an alignment.
    
    trim_method options:
        - "none" - No trimming (copy input to output)
        - "trimal" - trimAl default settings
        - "bmge" - BMGE default settings
    
    Args:
        input_alignment: Path to input aligned FASTA
        output_alignment: Path for output trimmed FASTA
        trim_method: Trimming method name
        config: Application config
        logger: Logger instance
        job_id: Optional job ID for real-time event streaming
    """
    method = trim_method.lower()
    
    logger.info(f"Starting trimming with method: {method}")
    
    try:
        if method == "none" or not method:
            shutil.copy(input_alignment, output_alignment)
            logger.info("Trimming skipped (method='none'). Copied input to output.")
            return

        if method == "trimal":
            _run_trimal(input_alignment, output_alignment, config, logger, job_id)
        elif method == "bmge":
            _run_bmge(input_alignment, output_alignment, config, logger, job_id)
        else:
            # Fallback to none if unknown, or raise? 
            # Let's raise to be strict.
            raise ValueError(f"Unsupported trimming method: {method}")

        if not output_alignment.exists() or output_alignment.stat().st_size == 0:
             raise RuntimeError(f"Trimming failed: Output file {output_alignment} is missing or empty.")

        logger.info(f"Trimming completed successfully. Output: {output_alignment}")

    except Exception as e:
        logger.error(f"Trimming failed: {e}")
        raise


def _make_log_callback(job_id: Optional[str], step: str, stream: str):
    """Create a callback function for streaming output to Redis."""
    if not job_id:
        return None
    
    from app.workers.events import publish_log
    
    def callback(line: str):
        publish_log(job_id, step, stream, line)
    
    return callback


def _run_trimal(
    input_alignment: Path,
    output_alignment: Path,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """Run trimAl alignment trimming."""
    cmd = [
        config.TRIMAL_BINARY,
        "-in", str(input_alignment),
        "-out", str(output_alignment),
        "-automated1"
    ]
    
    log_file = output_alignment.parent.parent / "logs" / "alignment.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "trim", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "trim", "stdout"),
            on_stderr_line=_make_log_callback(job_id, "trim", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"trimAl failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"trimAl failed with return code {returncode}. See logs.")


def _run_bmge(
    input_alignment: Path,
    output_alignment: Path,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """
    Run BMGE alignment trimming.
    
    Handles both JAR file and binary executable configurations.
    """
    bmge_bin = config.BMGE_BINARY
    cmd = []
    
    if str(bmge_bin).endswith(".jar"):
        cmd = ["java", "-jar", bmge_bin]
    else:
        cmd = [bmge_bin]
        
    cmd.extend([
        "-i", str(input_alignment),
        "-t", "DNA",
        "-of", str(output_alignment)
    ])
    
    log_file = output_alignment.parent.parent / "logs" / "alignment.log"
    
    if job_id:
        # Publish command line (displayed in green)
        from app.workers.events import publish_command
        publish_command(job_id, "trim", cmd)
        
        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "trim", "stdout"),
            on_stderr_line=_make_log_callback(job_id, "trim", "stderr"),
        )
        
        if exit_code != 0:
            raise RuntimeError(f"BMGE failed with exit code {exit_code}")
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(f"BMGE failed with return code {returncode}. See logs.")
