"""
Trimming service module.

Provides functions for trimming multiple sequence alignments:
- trimAl
- BMGE

When job_id is provided, streams log output to Redis for real-time SSE updates.
"""

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import Config
from app.services.subprocess_utils import (
    run_command,
    run_command_streaming,
    tool_failure_message,
)

# A terminal alignment column is kept if several sequences have a residue
# there. This trims singleton/doubleton flanking tails without letting a few
# ITS2-only reads collapse ITS1/5.8S signal that is shared by multiple longer
# sequences. Only the outermost qualifying columns bound the retained region,
# so internal low-coverage columns are always preserved.
MIN_TERMINAL_COVERED_SEQUENCES = 3


def run_trimming(
    input_alignment: Path,
    output_alignment: Path,
    trim_method: str,
    config: Config,
    logger,
    job_id: Optional[str] = None,
    trim_terminal_overhangs: bool = True,
) -> Dict[str, Any]:
    """
    Apply gap trimming to an alignment.
    
    trim_method options:
        - "none" - No trimming (copy input to output)
        - "trimal_gappy" - trimAl -gt 0.9 (drop columns that are >90% gaps).
          The default: removes alignment junk while leaving the variable ITS1/ITS2
          regions essentially intact.
        - "trimal" - trimAl -automated1. Aggressive; see _run_trimal for why this
          is a poor fit for ITS.
        - "bmge" - BMGE default settings
    
    Args:
        input_alignment: Path to input aligned FASTA
        output_alignment: Path for output trimmed FASTA
        trim_method: Trimming method name
        config: Application config
        logger: Logger instance
        job_id: Optional job ID for real-time event streaming
    """
    method = (trim_method or "none").lower()
    
    logger.info(f"Starting trimming with method: {method}")

    stats: Dict[str, Any] = {
        "method": method,
        "trim_terminal_overhangs": bool(trim_terminal_overhangs),
        "terminal_overhang_trim": {
            "enabled": bool(trim_terminal_overhangs),
            "removed_columns": 0,
        },
    }
    
    terminal_tmp: Optional[Path] = None
    report_path: Optional[Path] = None
    try:
        if method == "none" or not method:
            if trim_terminal_overhangs:
                stats["terminal_overhang_trim"] = _trim_terminal_overhangs(
                    input_alignment, output_alignment, logger
                )
                logger.info("External trimming skipped (method='none').")
            else:
                shutil.copy(input_alignment, output_alignment)
                logger.info("Trimming skipped (method='none'). Copied input to output.")
            return stats

        tool_input = input_alignment
        if trim_terminal_overhangs:
            terminal_tmp = output_alignment.with_name(f"{output_alignment.stem}.terminal_overhangs.fasta")
            stats["terminal_overhang_trim"] = _trim_terminal_overhangs(
                input_alignment, terminal_tmp, logger
            )
            tool_input = terminal_tmp

        report_path = output_alignment.with_name(f"{output_alignment.stem}_report.html")
        if report_path.exists():
            report_path.unlink()

        if method == "trimal_gappy":
            _run_trimal_gappy(tool_input, output_alignment, report_path, config, logger, job_id)
        elif method == "trimal":
            _run_trimal(tool_input, output_alignment, report_path, config, logger, job_id)
        elif method == "bmge":
            _run_bmge(tool_input, output_alignment, report_path, config, logger, job_id)
        else:
            # Fallback to none if unknown, or raise?
            # Let's raise to be strict.
            raise ValueError(f"Unsupported trimming method: {method}")

        if not output_alignment.exists() or output_alignment.stat().st_size == 0:
             raise RuntimeError(f"Trimming failed: Output file {output_alignment} is missing or empty.")

        _restore_trimmed_fasta_headers(tool_input, output_alignment, logger)
        if report_path.exists() and report_path.stat().st_size:
            stats["report_file"] = report_path.name
            stats["report_format"] = "html"
            stats["report_input_stage"] = (
                "after_terminal_overhang_trimming"
                if trim_terminal_overhangs
                else "unaltered_alignment"
            )
        else:
            logger.warning("Trimming report was not produced: %s", report_path)
        logger.info(f"Trimming completed successfully. Output: {output_alignment}")
        return stats

    except Exception as e:
        logger.error(f"Trimming failed: {e}")
        raise

    finally:
        # Always clean up the intermediate terminal-overhang file, even if the
        # external trimmer raised before the success path could remove it.
        if terminal_tmp is not None and terminal_tmp.exists():
            terminal_tmp.unlink()


def describe_trim_step(trim_method: Optional[str], trim_terminal_overhangs: bool):
    """Return (should_run, step_label, tool_token) for the trim pipeline step.

    Single source of truth for the trim step's UI label/tool and the skip
    decision, shared by the initial worker run and recompute so both paths stay
    in sync.
    """
    method = (trim_method or "none").lower()
    external = method not in ("", "none")
    if not external and not trim_terminal_overhangs:
        return False, "Trimming (skipped)", None
    if external and trim_terminal_overhangs:
        label = f"Trimming ({method} + terminal overhangs)"
    elif trim_terminal_overhangs:
        label = "Trimming (terminal overhangs)"
    else:
        label = f"Trimming ({method})"
    tool = method if external else "terminal-overhang"
    return True, label, tool


def format_trimming_detail(trim_method: Optional[str], trim_stats: Optional[Dict[str, Any]],
                           retained_columns: int) -> str:
    """Human-readable trim summary; shared by the worker and recompute paths."""
    terminal_stats = (trim_stats or {}).get("terminal_overhang_trim") or {}
    detail_parts = [f"{retained_columns} columns retained"]

    if terminal_stats.get("enabled"):
        removed = int(terminal_stats.get("removed_columns") or 0)
        left_removed = int(terminal_stats.get("left_removed") or 0)
        right_removed = int(terminal_stats.get("right_removed") or 0)
        min_covered = int(terminal_stats.get("min_covered_sequences") or MIN_TERMINAL_COVERED_SEQUENCES)
        detail_parts.append(
            f"terminal low-coverage trim removed {removed} columns "
            f"({left_removed} left, {right_removed} right)"
            f" using min {min_covered} sequences"
        )

    if trim_method and trim_method.lower() not in {"none", ""}:
        detail_parts.append(f"{trim_method} applied")

    return "; ".join(detail_parts)


def _read_fasta_records(path: Path) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    header: Optional[str] = None
    seq_parts: list[str] = []

    with open(path, "r") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if header is not None:
                    records.append((header, "".join(seq_parts)))
                header = line[1:].strip()
                seq_parts = []
            else:
                seq_parts.append(line)

    if header is not None:
        records.append((header, "".join(seq_parts)))

    return records


def _write_fasta_records(records: list[tuple[str, str]], path: Path) -> None:
    with open(path, "w") as handle:
        for header, sequence in records:
            handle.write(f">{header}\n")
            for idx in range(0, len(sequence), 80):
                handle.write(f"{sequence[idx:idx + 80]}\n")


def _trim_terminal_overhangs(input_alignment: Path, output_alignment: Path, logger) -> Dict[str, Any]:
    """Trim ragged terminal columns covered by fewer than several sequences.

    Only the leading/trailing under-covered columns are removed; internal
    low-coverage columns are always kept. If the alignment is unusable for
    trimming (empty, unequal lengths, or no column meets the coverage
    threshold) the input is copied through unchanged rather than raising, so an
    unusual dataset degrades to "no trimming" instead of failing the job.
    """
    records = _read_fasta_records(input_alignment)

    def _copy_through(reason: str, input_columns: int) -> Dict[str, Any]:
        shutil.copy(input_alignment, output_alignment)
        logger.warning("Terminal overhang trimming skipped: %s", reason)
        return {
            "enabled": True,
            "mode": "min_sequence_coverage_span",
            "skipped": True,
            "skipped_reason": reason,
            "min_covered_sequences": min(MIN_TERMINAL_COVERED_SEQUENCES, len(records) or 1),
            "input_columns": input_columns,
            "retained_columns": input_columns,
            "removed_columns": 0,
            "left_removed": 0,
            "right_removed": 0,
            "removed_ranges": [],
            "sequence_count": len(records),
        }

    if not records:
        return _copy_through("input alignment contains no FASTA records", 0)

    alignment_length = len(records[0][1])
    if alignment_length == 0:
        return _copy_through("input alignment contains empty sequences", 0)
    if any(len(sequence) != alignment_length for _, sequence in records):
        return _copy_through("input alignment has unequal sequence lengths", alignment_length)

    # Per-column count of sequences with a residue (non-gap) at that position.
    # An all-gap sequence simply contributes zeros and never aborts the trim.
    coverage = [0] * alignment_length
    for _, sequence in records:
        for idx, char in enumerate(sequence):
            if char != "-":
                coverage[idx] += 1

    min_covered = max(1, min(MIN_TERMINAL_COVERED_SEQUENCES, len(records)))
    covered_columns = [idx for idx, count in enumerate(coverage) if count >= min_covered]
    if not covered_columns:
        return _copy_through("no column meets the terminal sequence coverage threshold", alignment_length)

    left_cut = covered_columns[0]
    right_cut = covered_columns[-1]

    retained_columns = right_cut - left_cut + 1
    left_removed = left_cut
    right_removed = alignment_length - right_cut - 1
    removed_columns = left_removed + right_removed

    trimmed_records = [
        (header, sequence[left_cut:right_cut + 1])
        for header, sequence in records
    ]
    _write_fasta_records(trimmed_records, output_alignment)

    removed_ranges = []
    if left_removed:
        removed_ranges.append({"start": 1, "end": left_removed})
    if right_removed:
        removed_ranges.append({"start": right_cut + 2, "end": alignment_length})

    stats = {
        "enabled": True,
        "mode": "min_sequence_coverage_span",
        "min_covered_sequences": min_covered,
        "input_columns": alignment_length,
        "retained_columns": retained_columns,
        "removed_columns": removed_columns,
        "left_removed": left_removed,
        "right_removed": right_removed,
        "removed_ranges": removed_ranges,
        "sequence_count": len(records),
    }

    logger.info(
        "Terminal overhang trimming retained %s/%s columns and removed %s "
        "(left=%s, right=%s).",
        retained_columns,
        alignment_length,
        removed_columns,
        left_removed,
        right_removed,
    )

    return stats


def _make_log_callback(job_id: Optional[str], step: str, stream: str):
    """Create a callback function for streaming output to Redis."""
    if not job_id:
        return None
    
    from app.workers.events import publish_log
    
    def callback(line: str):
        publish_log(job_id, step, stream, line)
    
    return callback


def _restore_trimmed_fasta_headers(
    input_alignment: Path,
    output_alignment: Path,
    logger,
) -> None:
    """Restore full FASTA descriptions that external trimmers may shorten."""
    try:
        from Bio import SeqIO
    except ImportError:
        logger.warning("BioPython unavailable; could not restore trimmed FASTA headers.")
        return

    input_records = list(SeqIO.parse(str(input_alignment), "fasta"))
    original_headers: dict[str, str] = {}

    for record in input_records:
        header = (record.description or record.id or "").strip()
        if not header:
            continue

        keys = {
            (record.id or "").strip(),
            (record.name or "").strip(),
            header.split(None, 1)[0].strip(),
        }
        for key in keys:
            if key:
                original_headers.setdefault(key, header)

    if not original_headers:
        logger.warning("No source FASTA headers found to restore after trimming.")
        return

    output_records = list(SeqIO.parse(str(output_alignment), "fasta"))
    restored = 0
    missing = 0

    for record in output_records:
        output_header = (record.description or "").strip()
        output_first_token = output_header.split(None, 1)[0].strip() if output_header else ""
        keys = (
            (record.id or "").strip(),
            (record.name or "").strip(),
            output_first_token,
        )
        original_header = next((original_headers[key] for key in keys if key in original_headers), None)

        if not original_header:
            missing += 1
            continue

        if output_header != original_header:
            record.description = original_header
            record.name = record.id
            restored += 1

    if restored:
        SeqIO.write(output_records, str(output_alignment), "fasta")

    logger.info(
        "Restored full FASTA headers for %s/%s trimmed records (%s unmatched).",
        restored,
        len(output_records),
        missing,
    )


# Gap threshold for the default trimmer: keep a column if at least this fraction
# of sequences have a residue there. 0.1 means "drop columns that are more than
# 90% gaps".
TRIMAL_GAP_THRESHOLD = 0.1


def _run_trimal_gappy(
    input_alignment: Path,
    output_alignment: Path,
    report_path: Path,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """Run trimAl with a plain gap threshold (the default trimmer).

    Removes columns that are almost entirely gaps -- alignment junk that carries
    no signal -- without touching well-populated columns. On a 108-sequence fungal
    ITS alignment this cut 1316 columns to 767 while retaining 318/320 of ITS1 and
    199/201 of ITS2. Contrast _run_trimal (-automated1), which cut the same
    alignment to 449 columns and took 42% of ITS1 and 45% of ITS2 with it.
    """
    cmd = [
        config.TRIMAL_BINARY,
        "-in", str(input_alignment),
        "-out", str(output_alignment),
        "-htmlout", str(report_path),
        "-gt", str(TRIMAL_GAP_THRESHOLD),
    ]

    log_file = output_alignment.parent.parent / "logs" / "alignment.log"

    if job_id:
        from app.workers.events import publish_command
        publish_command(job_id, "trim", cmd)

        exit_code, stats = run_command_streaming(
            cmd,
            stderr_path=log_file,
            on_stdout_line=_make_log_callback(job_id, "trim", "stdout"),
            on_stderr_line=_make_log_callback(job_id, "trim", "stderr"),
        )

        if exit_code != 0:
            raise RuntimeError(tool_failure_message("trimAl (gap threshold)", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)

        if returncode != 0:
            raise RuntimeError(
                tool_failure_message("trimAl (gap threshold)", returncode)
            )


def _run_trimal(
    input_alignment: Path,
    output_alignment: Path,
    report_path: Path,
    config: Config,
    logger,
    job_id: Optional[str] = None
):
    """Run trimAl -automated1.

    Kept as an option, but a poor default for ITS: -automated1 optimises for
    phylogenomic/protein alignments and strips the indel-rich variable regions. On
    a 108-sequence fungal ITS alignment it retained 95% of the conserved 5.8S while
    discarding 42% of ITS1 and 45% of ITS2 -- i.e. it preferentially removed the
    characters that separate closely related fungi -- and the resulting IQ-TREE had
    fewer well-supported nodes (24 vs 32 at SH-aLRT>=80 & UFBoot>=95) and less
    resolution (79 vs 101 labelled nodes) than the untrimmed alignment.
    Use _run_trimal_gappy unless you specifically want -automated1.
    """
    cmd = [
        config.TRIMAL_BINARY,
        "-in", str(input_alignment),
        "-out", str(output_alignment),
        "-htmlout", str(report_path),
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
            raise RuntimeError(tool_failure_message("trimAl", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(tool_failure_message("trimAl", returncode))


def _run_bmge(
    input_alignment: Path,
    output_alignment: Path,
    report_path: Path,
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
        "-of", str(output_alignment),
        "-oh", str(report_path),
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
            raise RuntimeError(tool_failure_message("BMGE", exit_code))
    else:
        returncode, stdout, stderr = run_command(cmd, log_file=log_file)
        
        if returncode != 0:
            raise RuntimeError(tool_failure_message("BMGE", returncode))
