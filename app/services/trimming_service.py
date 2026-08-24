"""
Trimming service module.

Provides functions for trimming multiple sequence alignments:
- trimAl
- BMGE

When job_id is provided, streams log output to Redis for real-time SSE updates.
"""

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import Config
from app.services.artifact_storage import compress_artifact, discard_artifact
from app.services.fasta_utils import read_fasta_records
from app.services.subprocess_utils import (
    configured_tool_limits,
    configured_tool_time_limit_hours,
    configured_tool_timeout_seconds,
    log_tool_failure,
    run_command,
    run_command_streaming,
    ToolExecutionError,
    tool_failure_message,
)

_logger = logging.getLogger(__name__)

# A terminal alignment column is kept if several sequences have a residue
# there. This trims singleton/doubleton flanking tails without letting a few
# ITS2-only reads collapse ITS1/5.8S signal that is shared by multiple longer
# sequences. Only the outermost qualifying columns bound the retained region,
# so internal low-coverage columns are always preserved.
MIN_TERMINAL_COVERED_SEQUENCES = 3

# ...but a flat floor of 3 is meaningless once the alignment is large: on a
# 147-sequence ITS job three stray long reads were enough to retain every
# column, and the terminal trim removed nothing at all. The threshold is now
# whichever is greater of the flat floor and this fraction of the alignment, so
# it scales with the dataset.
MIN_TERMINAL_COVERAGE_FRACTION = 0.05

# Characters that do not demonstrate genuine terminal coverage. ``N`` is an
# unknown nucleotide, and Sanger reads are often quality-padded with terminal
# runs of Ns; counting those runs as residues let missing data set the retained
# boundary. Other IUPAC ambiguity codes still represent a constrained base and
# therefore count as coverage.
TERMINAL_GAP_CHARS = frozenset("-.?~Nn")


def _min_terminal_covered(sequence_count: int) -> int:
    """Coverage threshold for the terminal trim at this alignment size."""
    if sequence_count <= 0:
        return MIN_TERMINAL_COVERED_SEQUENCES
    scaled = int(round(sequence_count * MIN_TERMINAL_COVERAGE_FRACTION))
    return max(1, min(sequence_count, max(MIN_TERMINAL_COVERED_SEQUENCES, scaled)))


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
        - "trimal_gappy" - trimAl -gt 0.1 (drop columns that are >90% gaps).
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
    # This is deliberately independent of the external trimmer. The terminal
    # rule has a three-sequence floor and treats N as missing coverage, whereas
    # trimAl -gt is a per-column gap score. Even when their percentage
    # thresholds are nested on a large ordinary alignment, they are not the
    # same scientific test.
    apply_terminal_overhangs = bool(trim_terminal_overhangs)
    
    logger.info(f"Starting trimming with method: {method}")

    stats: Dict[str, Any] = {
        "method": method,
        "trim_terminal_overhangs": bool(trim_terminal_overhangs),
        "terminal_overhang_trim": {
            "enabled": apply_terminal_overhangs,
            "removed_columns": 0,
        },
    }

    terminal_tmp: Optional[Path] = None
    report_path: Optional[Path] = None
    try:
        if method == "none" or not method:
            if apply_terminal_overhangs:
                stats["terminal_overhang_trim"] = _trim_terminal_overhangs(
                    input_alignment, output_alignment, logger
                )
                logger.info("External trimming skipped (method='none').")
            else:
                shutil.copy(input_alignment, output_alignment)
                logger.info("Trimming skipped (method='none'). Copied input to output.")
            return stats

        tool_input = input_alignment
        if apply_terminal_overhangs:
            terminal_tmp = output_alignment.with_name(f"{output_alignment.stem}.terminal_overhangs.fasta")
            stats["terminal_overhang_trim"] = _trim_terminal_overhangs(
                input_alignment, terminal_tmp, logger
            )
            tool_input = terminal_tmp

        report_path = output_alignment.with_name(f"{output_alignment.stem}_report.html")
        # Clear both forms: a rerun must not leave last run's report behind in
        # the other representation (see the gzip step after the trimmer runs).
        discard_artifact(report_path)

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
            # trimAl's -htmlout is one <span> per residue: ~1.65 MB per report,
            # 2.4 GiB across the job tree, and it compresses ~43x because of
            # that repetition. It is only ever read to build the trimming
            # inspection ZIP, so it is stored gzipped and decompressed there.
            stats["report_file"] = report_path.name
            stats["report_format"] = "html"
            stats["report_input_stage"] = (
                "after_terminal_overhang_trimming"
                if apply_terminal_overhangs
                else "unaltered_alignment"
            )
            compress_artifact(report_path)
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
    effective_terminal = bool(trim_terminal_overhangs)
    if not external and not effective_terminal:
        return False, "Trimming (skipped)", None
    if external and effective_terminal:
        label = f"Trimming ({method} + terminal overhangs)"
    elif effective_terminal:
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
    records = read_fasta_records(input_alignment)

    def _copy_through(reason: str, input_columns: int) -> Dict[str, Any]:
        shutil.copy(input_alignment, output_alignment)
        logger.warning("Terminal overhang trimming skipped: %s", reason)
        return {
            "enabled": True,
            "mode": "min_sequence_coverage_span",
            "skipped": True,
            "skipped_reason": reason,
            "min_covered_sequences": _min_terminal_covered(len(records)),
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
            if char not in TERMINAL_GAP_CHARS:
                coverage[idx] += 1

    min_covered = _min_terminal_covered(len(records))
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
    # Alan 8/14/26 - Track keys claimed by more than one input record. Trimmers shorten
    # a header to its first token, so two records sharing that token used to both
    # resolve to whichever header landed in the map first -- silently relabelling one
    # sequence with another's name and producing duplicate tips that then made the
    # tree unrootable ("Duplicate tip name found"). An ambiguous key restores nothing.
    ambiguous_keys: set[str] = set()

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
            if not key:
                continue
            if key in original_headers and original_headers[key] != header:
                ambiguous_keys.add(key)
            original_headers.setdefault(key, header)

    for key in ambiguous_keys:
        original_headers.pop(key, None)

    output_records = list(SeqIO.parse(str(output_alignment), "fasta"))

    # Alan 8/14/26 - Trimmers drop alignment columns, never records, so when the counts
    # match, position is an exact and collision-proof mapping. Fall back to key lookup
    # only when a trimmer did change the record count.
    positional = len(output_records) == len(input_records)

    # Only a degradation when the key map is actually consulted. On the
    # positional path the shared identifiers are never looked up, so every
    # trimmed job with two records sharing a first token was logging a DEGRADED
    # line for a restoration failure that did not happen.
    if ambiguous_keys and not positional:
        from app.services.log_context import log_degradation
        log_degradation(
            logger,
            "trimmed_header_ambiguous_ids",
            f"{len(ambiguous_keys)} identifier(s) are shared by multiple records "
            f"(e.g. {', '.join(sorted(ambiguous_keys)[:5])}); those records keep their "
            "trimmer-written headers rather than being restored to a possibly wrong name",
            shared_ids=len(ambiguous_keys),
        )

    if not original_headers and not positional:
        logger.warning("No source FASTA headers found to restore after trimming.")
        return

    restored = 0
    missing = 0

    for index, record in enumerate(output_records):
        output_header = (record.description or "").strip()

        if positional:
            original_header = (
                input_records[index].description or input_records[index].id or ""
            ).strip() or None
        else:
            output_first_token = output_header.split(None, 1)[0].strip() if output_header else ""
            keys = (
                (record.id or "").strip(),
                (record.name or "").strip(),
                output_first_token,
            )
            original_header = next(
                (original_headers[key] for key in keys if key in original_headers), None
            )

        if not original_header:
            missing += 1
            continue

        if output_header != original_header:
            # Alan 8/14/26 - Also realign record.id with the restored header's first
            # token. BioPython writes ">{id} {description}" unless the description
            # already starts with the id, so a stale id would duplicate the token in
            # the written header.
            record.id = original_header.split(None, 1)[0]
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
            **configured_tool_limits(config, "trimAl"),
        )

        if exit_code != 0:
            raise ToolExecutionError(
                "trimAl (gap threshold)", exit_code, stats, tool_failure_message(
                    "trimAl (gap threshold)", exit_code,
                    configured_tool_time_limit_hours(config, "trimAl")))
    else:
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file,
            timeout=configured_tool_timeout_seconds(config, "trimAl"),
        )

        if returncode != 0:
            raise RuntimeError(
                tool_failure_message(
                    "trimAl (gap threshold)", returncode,
                    configured_tool_time_limit_hours(config, "trimAl")
                )
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
            **configured_tool_limits(config, "trimAl"),
        )
        
        if exit_code != 0:
            log_tool_failure(_logger, "trimAl", exit_code, stats, job=job_id, step="trim")
            raise ToolExecutionError(
                "trimAl", exit_code, stats, tool_failure_message(
                    "trimAl", exit_code, configured_tool_time_limit_hours(config, "trimAl")))
    else:
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file,
            timeout=configured_tool_timeout_seconds(config, "trimAl"),
        )

        if returncode != 0:
            log_tool_failure(
                _logger, "trimAl", returncode,
                {"stdout_tail": (stdout or "").splitlines(),
                 "stderr_tail": (stderr or "").splitlines()},
                step="trim",
            )
            raise RuntimeError(tool_failure_message(
                "trimAl", returncode, configured_tool_time_limit_hours(config, "trimAl")))


def _bmge_command(bmge_bin, config: Config) -> list:
    """Build the BMGE invocation, sizing the JVM heap to fit inside RLIMIT_AS.

    Every external tool is spawned under ``prlimit --as=SUBPROCESS_MEMORY_LIMIT_MB``.
    A JVM reserves its whole ``-Xmx`` as address space at startup, so the conda
    shim's hardcoded ``-Xmx128G`` could never start under a 9 GB cap -- BMGE
    trimming failed for every job after that limit was introduced, and only
    after the alignment step had already completed. Passing an explicit heap
    below the limit is what makes the two settings agree.
    """
    if not str(bmge_bin).endswith(".jar"):
        # A wrapper script owns its own -Xmx and we cannot override it, so the
        # limit and the wrapper will disagree unless no limit is set at all.
        if int(getattr(config, "SUBPROCESS_MEMORY_LIMIT_MB", 0) or 0) > 0:
            _logger.warning(
                "event=bmge.unmanaged_heap BMGE_BINARY=%s is not a .jar, so its "
                "JVM heap cannot be sized to fit SUBPROCESS_MEMORY_LIMIT_MB",
                bmge_bin,
            )
        return [str(bmge_bin)]

    cmd = ["java"]
    memory_mb = int(getattr(config, "SUBPROCESS_MEMORY_LIMIT_MB", 0) or 0)
    if memory_mb > 0:
        floor_mb = int(getattr(config, "JVM_MIN_ADDRESS_SPACE_MB", 4096) or 0)
        if memory_mb < floor_mb:
            _logger.warning(
                "event=bmge.limit_too_low SUBPROCESS_MEMORY_LIMIT_MB=%s is below "
                "the %s MB a JVM needs to start under RLIMIT_AS; BMGE trimming "
                "will fail until the limit is raised",
                memory_mb, floor_mb,
            )
        percent = int(getattr(config, "JVM_HEAP_PERCENT", 60) or 60)
        heap_mb = max(256, memory_mb * percent // 100)
        cmd.extend([
            f"-Xmx{heap_mb}m",
            f"-XX:CompressedClassSpaceSize={int(getattr(config, 'JVM_CLASS_SPACE_MB', 128))}m",
            f"-XX:MaxMetaspaceSize={int(getattr(config, 'JVM_METASPACE_MB', 256))}m",
        ])
    cmd.extend(["-jar", str(bmge_bin)])
    return cmd


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
    cmd = _bmge_command(bmge_bin, config)

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
            **configured_tool_limits(config, "BMGE"),
        )
        
        if exit_code != 0:
            log_tool_failure(_logger, "BMGE", exit_code, stats, job=job_id, step="trim")
            raise ToolExecutionError(
                "BMGE", exit_code, stats, tool_failure_message(
                    "BMGE", exit_code, configured_tool_time_limit_hours(config, "BMGE")))
    else:
        returncode, stdout, stderr = run_command(
            cmd, log_file=log_file,
            timeout=configured_tool_timeout_seconds(config, "BMGE"),
        )

        if returncode != 0:
            log_tool_failure(
                _logger, "BMGE", returncode,
                {"stdout_tail": (stdout or "").splitlines(),
                 "stderr_tail": (stderr or "").splitlines()},
                step="trim",
            )
            raise RuntimeError(tool_failure_message(
                "BMGE", returncode, configured_tool_time_limit_hours(config, "BMGE")))
