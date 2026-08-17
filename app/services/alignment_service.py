"""
Alignment service module.

Provides functions for running multiple sequence alignment using various tools:
- MAFFT
- MUSCLE
- Clustal Omega
- IQ-TREE (built-in alignment)

When job_id is provided, streams log output to Redis for real-time SSE updates.
"""

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from app.config import Config
from app.models import AlignmentParams
from app.services.subprocess_utils import (
    run_command,
    run_command_streaming,
    tool_failure_message,
)


def _verify_already_aligned(input_fasta: Path, logger) -> None:
    """Fail early, and by name, when 'no alignment' is used on unaligned input.

    Choosing method='none' asserts the sequences are already aligned. When they
    are not, nothing here used to object: trimming logged a warning and copied
    the file through, and the first real complaint came from trimAl ("exit code
    255") or FastTree ("expected 611 but have 601 instead") -- neither of which
    tells the user what they actually did wrong. Three of the pipeline failures
    on record were this exact situation.

    Headers are still the user's own at this point (sanitization happens later,
    per tool), so the offending sequences can be named as submitted.
    """
    lengths = {}
    name = None
    with open(input_fasta) as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.startswith(">"):
                name = line[1:].strip()
                lengths[name] = 0
            elif name is not None:
                lengths[name] += len(line.strip())

    if len(lengths) < 2:
        return

    counts = {}
    for length in lengths.values():
        counts[length] = counts.get(length, 0) + 1
    if len(counts) == 1:
        return

    # The modal length is the intended column count; everything else is odd.
    expected = max(counts, key=lambda length: (counts[length], length))
    odd = [(n, l) for n, l in lengths.items() if l != expected]
    # Full GenBank descriptions run to hundreds of characters and would bury
    # the message; the leading accession is what identifies the record.
    shown = ", ".join(
        f"{n[:57] + '...' if len(n) > 60 else n} ({l})" for n, l in odd[:3]
    )
    if len(odd) > 3:
        shown += f", and {len(odd) - 3} more"

    logger.error(
        "Alignment method 'none' rejected: %d of %d sequences differ from the "
        "modal length of %d columns.", len(odd), len(lengths), expected
    )
    raise RuntimeError(
        f"You chose to skip alignment, which requires sequences that are "
        f"already aligned, but {len(odd)} of {len(lengths)} are not the same "
        f"length as the rest. Aligned sequences must all have the same number "
        f"of columns (including gaps). Most are {expected} columns; these "
        f"differ: {shown}. Either choose an alignment method such as MAFFT or "
        f"MUSCLE, or submit a file that is already aligned."
    )


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
            _verify_already_aligned(input_fasta, logger)
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


# MAFFT reports one stderr line per pairwise comparison and per progressive
# step. On a 484-sequence job that is ~105,000 of the log's 107,000 lines, and
# across var/jobs it accounted for 92% of the 0.77 GiB of alignment.log. The
# lines carry no information once the run is over -- they are a progress bar --
# so they are streamed live to the UI but not persisted.
_MAFFT_NOISE_PATTERNS = (
    # "001-0002-0 (thread    1) identical" / "... better" / "... worse"
    re.compile(r"^\d+-\d+-\d+ \(thread\s+\d+\)\s+(identical|better|worse)\s*$"),
    # "STEP     1 / 483 (thread    0) f" -- often with backspaces appended
    re.compile(r"^STEP\s+\d+\s*/\s*\d+\s+\(thread\s+\d+\).*$"),
    # "100 / 484 (thread 1)" and the bare "  100 / 484" counter
    re.compile(r"^\s*\d+\s*/\s*\d+(\s+\(thread\s+\d+\))?\s*$"),
)


def _keep_mafft_log_line(line: str) -> bool:
    """
    Decide whether a MAFFT stderr line is worth keeping on disk.

    Drops per-comparison progress chatter and the blank lines MAFFT emits
    between progress blocks, keeping the version banner, parameter echo,
    warnings, "Converged."/"done." markers and anything unrecognised. Unknown
    output is always kept -- this must never swallow a diagnostic.
    """
    stripped = line.strip()
    if not stripped:
        return False
    return not any(pattern.match(line.rstrip("\b \t")) for pattern in _MAFFT_NOISE_PATTERNS)


def _append_filtered_log(log_file: Path, cmd, stderr: str, returncode: int) -> None:
    """Append a MAFFT run's stderr to alignment.log with progress chatter removed."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        kept = [line for line in (stderr or "").splitlines() if _keep_mafft_log_line(line)]
        with open(log_file, "a") as handle:
            handle.write(f"CMD: {' '.join(str(part) for part in cmd)}\n")
            handle.write("-" * 40 + "\n")
            if kept:
                handle.write("\n".join(kept) + "\n")
            handle.write("-" * 40 + "\n")
            handle.write(f"Exit code: {returncode}\n")
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Could not write alignment log %s: %s", log_file, exc
        )


def _restore_mafft_direction_headers(
    input_fasta: Path,
    output_fasta: Path,
    logger,
) -> int:
    """Remove MAFFT's ``_R_`` marker while preserving genuine input headers."""
    input_headers = {
        line[1:].strip()
        for line in input_fasta.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith(">")
    }
    output_lines = output_fasta.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    restored_count = 0

    for index, line in enumerate(output_lines):
        header_line = line.rstrip("\r\n")
        if not header_line.startswith(">_R_"):
            continue

        restored_header = header_line[4:]
        if restored_header not in input_headers:
            continue

        newline = line[len(header_line):]
        output_lines[index] = f">{restored_header}{newline}"
        restored_count += 1

    if restored_count:
        output_fasta.write_text("".join(output_lines), encoding="utf-8")
        logger.info("Restored %s MAFFT direction-marked header(s)", restored_count)

    return restored_count


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
            stderr_file_filter=_keep_mafft_log_line,
        )
        
        if exit_code != 0:
            raise RuntimeError(tool_failure_message("MAFFT", exit_code))
    else:
        # Fallback to non-streaming for backward compatibility. log_file is
        # deliberately not passed to run_command: MAFFT's stdout *is* the
        # alignment, and run_command's generic handler would write the entire
        # aligned FASTA into alignment.log a second time (92 existing logs were
        # inflated this way, some over 1 MB).
        returncode, stdout, stderr = run_command(cmd)

        _append_filtered_log(log_file, cmd, stderr, returncode)

        if returncode != 0:
            raise RuntimeError(tool_failure_message("MAFFT", returncode))

        with open(output_fasta, "w") as f:
            f.write(stdout)

    _restore_mafft_direction_headers(input_fasta, output_fasta, logger)


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
            raise RuntimeError(tool_failure_message("MUSCLE", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(tool_failure_message("MUSCLE", returncode))


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
            raise RuntimeError(tool_failure_message("Clustal Omega", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(tool_failure_message("Clustal Omega", returncode))


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
            raise RuntimeError(tool_failure_message("IQ-TREE alignment", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(tool_failure_message("IQ-TREE alignment", returncode))
    
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
