"""
Phylo job worker task.

This module contains the main RQ task for running phylogenetic analysis jobs.
It publishes real-time events via Redis PubSub for the SSE status dashboard.
"""

import copy
import collections
import json
import logging
import re
import time
import traceback
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from rq import Retry, get_current_job

from app.config import Config
from app.services.artifact_storage import (
    artifact_exists, artifact_size, discard_gzipped_form, open_artifact,
)
from app.services.log_context import (
    JobContextFilter, background_job_context, bind_background_context,
    background_user_identity,
    stable_fingerprint, utc_formatter,
)
from app.services.tree_parameter_validation import validate_iqtree_ufboot_count
from app.services.subprocess_utils import ToolExecutionError, log_tool_failure
from app.workers.events import (
    STEP_INPUT, STEP_ORIENT, STEP_BLAST, STEP_ITS, STEP_ALIGN, STEP_TRIM, STEP_TREE, STEP_POST,
    STATE_QUEUED, STATE_RUNNING, STATE_DONE, STATE_SKIPPED, STATE_FAILED,
    get_initial_steps_meta,
    publish_job_running, publish_job_queued, publish_job_completed, publish_job_failed,
    publish_step_start, publish_step_done, publish_step_failed,
    publish_overview, publish_log, publish_metric,
    update_job_meta, update_step_meta,
)

# Alan 8/15/26 - No logging.basicConfig() here.
#
# It was a no-op in every process that matters: create_app() installs the
# errors.log handler on the root logger first, and basicConfig does nothing once
# the root logger has any handler. Worse, relying on it meant that when that
# handler appeared the root logger silently stayed at WARNING and every INFO
# record in this module -- job.started, job.completed, the invariant checks, and
# everything the per-job pipeline.log handler exists to capture -- was dropped.
# Levels and handlers are now set explicitly in app._install_logging().
logger = logging.getLogger(__name__)

# Relocated to app/services/fasta_utils.py so the API can reject a malformed
# FASTA at submit time instead of accepting the job and failing it in the
# worker minutes later. Re-exported here because the pipeline still calls both.
from app.services.fasta_utils import (  # noqa: E402
    VALID_DNA_SYMBOLS,
    parse_fasta_records,
    validate_dna_fasta,
)

# Metadata reaches us from user-controlled imports, so "source" is not a safe
# label to echo into a log line: a crafted record could carry a specimen note or
# a token in that field. Anything unrecognised is counted as "other".
KNOWN_SEQUENCE_SOURCES = frozenset({
    "ncbi", "local", "mycomap", "inaturalist", "mushroom_observer", "genbank",
    "blast", "upload", "manual", "user", "unknown",
})


def failure_diagnostics(exc: Exception, fallback_tool: Optional[str] = None) -> dict:
    """Return diagnostics only when they belong to the operation that failed."""
    if not isinstance(exc, ToolExecutionError):
        return {
            "tool": fallback_tool,
            "exit_code": None,
            "failure_kind": None,
            "stats": {},
        }
    return {
        "tool": exc.tool,
        "exit_code": exc.exit_code,
        "failure_kind": exc.failure_kind,
        "stats": dict(exc.stats),
    }


def failure_metric_updates(
    error_msg: str,
    current_step: Optional[str],
    current_step_label: Optional[str],
    diagnostic: dict,
) -> dict:
    """Build the internal DB fields for one failure diagnostic."""
    stats = diagnostic.get("stats") or {}
    return {
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error": error_msg,
        "failed_step": current_step,
        "failed_step_label": current_step_label or None,
        "failed_tool": diagnostic.get("tool") or None,
        "exit_code": diagnostic.get("exit_code"),
        "failure_kind": diagnostic.get("failure_kind"),
        "failed_tool_signal": stats.get("signal"),
        "failed_tool_duration_seconds": stats.get("duration_seconds"),
        "failed_tool_stdout_lines": stats.get("stdout_lines"),
        "failed_tool_stderr_lines": stats.get("stderr_lines"),
    }


def summarize_job_params(job_params: dict) -> dict:
    """Return bounded diagnostics without sequences, metadata, notes, or secrets."""
    params = job_params if isinstance(job_params, dict) else {}
    sequence = params.get("sequence") if isinstance(params.get("sequence"), str) else ""
    records = parse_fasta_records(sequence) if sequence else []
    accessions = params.get("accessions") if isinstance(params.get("accessions"), list) else []
    metadata = params.get("sequence_metadata") if isinstance(params.get("sequence_metadata"), list) else []
    bounded_options = {}
    for key in (
        "alignment_method", "trimming_method", "trim_terminal_overhangs",
        "tree_method", "tree_model", "bootstrap", "alrt_replicates",
        "run_preset", "bootstrap_preset", "enable_bootstrap", "its_region",
        "blast_mode", "include_ncbi", "include_local", "moose_enabled",
    ):
        value = params.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            bounded_options[key] = str(value)[:80] if isinstance(value, str) else value
    sources = collections.Counter()
    for item in metadata[:10000]:
        if isinstance(item, dict):
            raw_source = str(item.get("hit_source") or item.get("source") or "unknown")
            source = raw_source.strip().lower()[:40]
            sources[source if source in KNOWN_SEQUENCE_SOURCES else "other"] += 1
    summary = {
        "input_type": str(params.get("input_type") or "unknown")[:40],
        "sequence_count": len(records),
        "total_bases": sum(len(seq) for _, seq in records),
        "accession_count": len(accessions),
        "imported_record_count": len(metadata),
        "sources": dict(sorted(sources.items())),
        "options": bounded_options,
    }
    summary["parameter_fingerprint"] = stable_fingerprint(
        json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str)
    )
    return summary


def _add_job_log_handler(job_id, log_file):
    """Capture all service logs for this job, without cross-job leakage."""
    handler = logging.FileHandler(log_file)
    handler.addFilter(JobContextFilter(job_id))
    # UTC, matching every other Dikarya log timestamp and what the log digest
    # assumes when it reads a timestamp carrying no offset.
    handler.setFormatter(utc_formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))
    logging.getLogger().addHandler(handler)
    return handler


def _remove_job_log_handler(handler):
    try:
        logging.getLogger().removeHandler(handler)
        handler.close()
    except Exception:
        pass


def validate_pipeline_outputs(job_dir, job_params, logger_obj=logger,
                              recompute: bool = False) -> dict:
    """Check completed artifacts without modifying them or job status."""
    from io import StringIO
    from Bio import Phylo, SeqIO
    from app.services.log_context import log_degradation

    if recompute:
        required = [
            job_dir / "alignment" / "alignment_pruned.fasta",
            job_dir / "alignment" / "alignment_pruned_aligned.fasta",
            job_dir / "alignment" / "alignment_pruned_trimmed.fasta",
            job_dir / "tree" / "tree_pruned.newick",
            job_dir / "tree" / "tree_pruned.nexus",
        ]
    else:
        required = [
            job_dir / "input" / "input_raw.fasta",
            job_dir / "alignment" / "alignment_raw.fasta",
            job_dir / "alignment" / "alignment_trimmed.fasta",
            job_dir / "tree" / "tree_original.newick",
            job_dir / "tree" / "tree_original.nexus",
        ]
    failures = []
    for path in required:
        # Validation may run long after the pipeline, by which point the cold
        # artifact sweep may have gzipped the alignments; artifact_size reports
        # the uncompressed size either way.
        if not artifact_exists(path) or artifact_size(path) == 0:
            failures.append(f"missing_or_empty:{path.relative_to(job_dir)}")

    fasta_counts = {}
    for path in required[:3]:
        if artifact_exists(path) and artifact_size(path):
            try:
                with open_artifact(path, "rt") as handle:
                    fasta_counts[path.name] = sum(1 for _ in SeqIO.parse(handle, "fasta"))
                if fasta_counts[path.name] == 0:
                    failures.append(f"unparseable_fasta:{path.name}")
            except Exception as exc:
                failures.append(f"unparseable_fasta:{path.name}:{type(exc).__name__}")

    terminal_count = None
    tree_quality = {}
    tree_path = required[3]
    if tree_path.is_file() and tree_path.stat().st_size:
        try:
            tree = Phylo.read(StringIO(tree_path.read_text(errors="replace")), "newick")
            terminal_count = len(tree.get_terminals())
            tree_quality = _summarize_tree_quality(
                tree, logger_obj,
                support_expected=_support_expected(job_params),
            )
        except Exception as exc:
            failures.append(f"unparseable_newick:{type(exc).__name__}")

    # The NEXUS export is a user-facing download, and for years it was written
    # by a writer that produced files no NEXUS reader could parse. Confirm it
    # is actually usable rather than only that it is non-empty.
    nexus_path = required[4]
    if nexus_path.is_file() and nexus_path.stat().st_size:
        from app.services.tree_io import validate_nexus_file

        nexus_ok, nexus_reason = validate_nexus_file(nexus_path)
        if not nexus_ok:
            failures.append(f"unparseable_nexus:{nexus_reason}")

    final_name = (
        "alignment_pruned_trimmed.fasta" if recompute else "alignment_trimmed.fasta"
    )
    final_count = fasta_counts.get(final_name)
    legitimately_pruned = False
    state_path = job_dir / "tree_state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
            legitimately_pruned = bool(state.get("pruned_taxa"))
        except (OSError, json.JSONDecodeError):
            # load_tree_state separately reports corrupt state; do not duplicate it.
            pass
    if (
        (recompute or not legitimately_pruned) and terminal_count is not None
        and final_count is not None and terminal_count != final_count
    ):
        failures.append(f"terminal_count_mismatch:fasta={final_count}:tree={terminal_count}")

    # run_phylo_job passes the raw params dict; run_recompute_job passes a
    # JobParams dataclass, which has no .get at all. Every attribute below is
    # dict-only, so treat anything else as "no source-import metadata" rather
    # than raising -- an AttributeError here reaches the caller's except and
    # marks a job failed after its tree was written successfully.
    params = job_params if isinstance(job_params, dict) else {}
    metadata = params.get("sequence_metadata")
    is_source_import = bool(
        params.get("mycomap_blast_url")
        or params.get("_inat_tree_preparation")
        or params.get("_mo_tree_preparation")
    )
    if is_source_import and isinstance(metadata, list) and metadata:
        present = collections.Counter(
            str(row.get("hit_source") or row.get("source") or "").lower()
            for row in metadata if isinstance(row, dict)
        )
        for source, flag in (("ncbi", "include_ncbi"), ("local", "include_local")):
            if params.get(flag) is True and present[source] == 0:
                log_degradation(
                    logger_obj, "requested_source_missing",
                    "A requested source set had no records in completed job metadata",
                    source=source,
                )

    if failures:
        logger_obj.error(
            "event=pipeline.invariant_failed Pipeline output invariant failed diagnostics=%s",
            ",".join(failures[:12]),
        )
    else:
        logger_obj.info(
            "event=pipeline.invariants_ok Pipeline outputs validated fasta_records=%s "
            "tree_terminals=%s quality=%s",
            final_count, terminal_count,
            json.dumps(tree_quality, sort_keys=True, separators=(",", ":")),
        )
    return {
        "ok": not failures,
        "failures": failures,
        "fasta_records": final_count,
        "tree_terminals": terminal_count,
        "tree_quality": tree_quality,
    }


def require_valid_pipeline_outputs(job_dir, job_params, logger_obj=logger,
                                   recompute: bool = False) -> dict:
    """Validate completed artifacts and stop the success path on hard failures."""
    result = validate_pipeline_outputs(
        job_dir, job_params, logger_obj, recompute=recompute
    )
    if not result["ok"]:
        diagnostics = ", ".join(result["failures"][:12])
        raise RuntimeError(f"Pipeline output validation failed: {diagnostics}")
    return result


def _pipeline_param(job_params, name, default=None):
    """Read one flattened or dataclass-backed pipeline parameter."""
    if isinstance(job_params, dict):
        if name in job_params:
            return job_params[name]
        nested = job_params.get("tree_builder_params")
        if isinstance(nested, dict):
            nested_name = "method" if name == "tree_method" else name
            return nested.get(nested_name, default)
        return default

    direct = getattr(job_params, name, None)
    if direct is not None:
        return direct
    tree_params = getattr(job_params, "tree_builder_params", None)
    nested_name = "method" if name == "tree_method" else name
    return getattr(tree_params, nested_name, default) if tree_params is not None else default


def _support_expected(job_params) -> Optional[bool]:
    """Whether the selected method/settings were asked to calculate support."""
    method = str(_pipeline_param(job_params, "tree_method", "") or "").lower()
    if method == "nj":
        return False
    if method in {"fasttree", "mrbayes"}:
        return True
    if method == "iqtree":
        try:
            bootstrap = int(_pipeline_param(job_params, "bootstrap", 0) or 0)
        except (TypeError, ValueError):
            bootstrap = 0
        try:
            alrt = int(_pipeline_param(job_params, "alrt_replicates", 0) or 0)
        except (TypeError, ValueError):
            alrt = 0
        return bootstrap > 0 or alrt > 0
    if method == "raxml":
        value = _pipeline_param(job_params, "enable_bootstrap", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)
    return None


def _summarize_tree_quality(tree, logger_obj,
                            support_expected: Optional[bool] = None) -> dict:
    """Report the tree-shaped failure modes this pipeline actually produces.

    The invariant check counted records and confirmed the Newick parsed, which
    a badly degraded tree passes easily. These are the things that have gone
    wrong in production: branch lengths rounded away to exact zeros, a tree
    with no support values where the method should have produced them, and a
    topology so unresolved it carries no information.
    """
    from app.services.log_context import log_degradation

    branch_lengths = []
    zero_branches = 0
    supported = 0
    internal = 0
    max_children = 0

    for clade in tree.find_clades():
        if clade.branch_length is not None:
            branch_lengths.append(clade.branch_length)
            if clade.branch_length == 0:
                zero_branches += 1
        if clade.clades:
            internal += 1
            max_children = max(max_children, len(clade.clades))
            if (
                clade.confidence is not None
                or (
                    isinstance(clade.name, str)
                    and re.fullmatch(
                        r"\s*\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*",
                        clade.name,
                    )
                )
            ):
                supported += 1

    terminals = len(tree.get_terminals())
    summary = {
        "terminals": terminals,
        "internal_nodes": internal,
        "zero_length_branches": zero_branches,
        "branches_with_length": len(branch_lengths),
        "internal_nodes_with_support": supported,
        "max_polytomy": max_children,
    }

    if branch_lengths and zero_branches / len(branch_lengths) > 0.25:
        log_degradation(
            logger_obj, "tree_many_zero_length_branches",
            "More than a quarter of branches have length exactly zero",
            zero_branches=zero_branches, total=len(branch_lengths),
        )
    # "no support values where the method should have produced them" -- NJ never
    # would, and _strip_generated_inner_labels() now clears the InnerN names that
    # used to make this look satisfied, so without the method check every single
    # NJ job logged a DEGRADED line and made `grep DEGRADED` uncountable.
    if internal and supported == 0 and terminals > 3 and support_expected is True:
        log_degradation(
            logger_obj, "tree_without_support_values",
            "Tree carries no support values on any internal node",
            internal_nodes=internal,
        )
    if terminals > 3 and max_children > max(3, terminals // 2):
        log_degradation(
            logger_obj, "tree_largely_unresolved",
            "Tree contains a polytomy spanning most of the taxa",
            max_polytomy=max_children, terminals=terminals,
        )

    return summary


def _save_job_params(input_info_path, job_params: dict) -> None:
    # Compact rather than indented. `sequence` (the original submitted FASTA)
    # and `sequence_metadata` are 90% of this file and neither is read by eye.
    # Note `sequence` is NOT redundant with input/input_raw.fasta: that file is
    # the processed input after dedup/orientation/BLAST augmentation, while this
    # is what the user actually submitted, which recompute and the
    # restore-removed-duplicates endpoint both need.
    with open(input_info_path, "w") as f:
        json.dump(job_params, f, separators=(",", ":"))




def cap_fasta_headers(fasta_text: str) -> tuple[str, int]:
    """Bound every header in a FASTA document, leaving sequence lines untouched.

    Rewrites header lines in place rather than reparsing and re-emitting the
    file, so the user's own line wrapping survives verbatim and only the names
    change. Returns (text, number_of_headers_capped).
    """
    from app.services.security_utils import cap_fasta_header

    lines = fasta_text.splitlines(keepends=True)
    capped = 0

    for index, raw_line in enumerate(lines):
        if not raw_line.startswith(">"):
            continue

        stripped = raw_line.rstrip("\r\n")
        line_ending = raw_line[len(stripped):]
        header = stripped[1:]
        safe_header = cap_fasta_header(header)

        if safe_header != header:
            lines[index] = f">{safe_header}{line_ending}"
            capped += 1

    return "".join(lines), capped




def _format_fasta_records(records: list[tuple[str, str]]) -> str:
    out_lines: list[str] = []
    for header, seq in records:
        out_lines.append(f">{header}")
        for i in range(0, len(seq), 80):
            out_lines.append(seq[i:i+80])
    return "\n".join(out_lines) + "\n" if out_lines else ""


def uniquify_fasta_identifiers(fasta_text: str) -> tuple[str, dict]:
    """Keep every record while making each first-token tool ID unique."""
    records = parse_fasta_records(fasta_text)
    reserved_ids = {
        (header.split(None, 1)[0] if header.strip() else "seq")[:100]
        for header, _sequence in records
    }
    used_ids: set[str] = set()
    next_suffix: dict[str, int] = {}
    unique_records: list[tuple[str, str]] = []
    renamed = 0

    for header, sequence in records:
        parts = header.split(None, 1) if header.strip() else ["seq"]
        base_id = parts[0][:100] or "seq"
        description = (parts[1] if len(parts) > 1 else "")[:300]
        unique_id = base_id
        if unique_id in used_ids:
            suffix = next_suffix.get(base_id, 2)
            unique_id = f"{base_id}_{suffix}"
            while unique_id in used_ids or unique_id in reserved_ids:
                suffix += 1
                unique_id = f"{base_id}_{suffix}"
            next_suffix[base_id] = suffix + 1
            renamed += 1
        used_ids.add(unique_id)
        unique_records.append((f"{unique_id} {description}".rstrip(), sequence))

    stats = {
        "input_records": len(records),
        "kept_records": len(unique_records),
        "dropped_exact_duplicates": 0,
        "renamed_due_to_duplicate_ids": renamed,
    }
    return _format_fasta_records(unique_records), stats


def dedupe_and_uniquify_fasta(fasta_text: str) -> tuple[str, dict]:
    """Drop exact records, then make the surviving tool identifiers unique."""
    records = parse_fasta_records(fasta_text)
    seen_exact: set[tuple[str, str]] = set()
    kept: list[tuple[str, str]] = []
    for record in records:
        if record in seen_exact:
            continue
        seen_exact.add(record)
        kept.append(record)

    unique_fasta, stats = uniquify_fasta_identifiers(_format_fasta_records(kept))
    stats["input_records"] = len(records)
    stats["dropped_exact_duplicates"] = len(records) - len(kept)
    return unique_fasta, stats


def fasta_record_count(fasta_text: str) -> int:
    """Count FASTA headers. Sufficient for deciding BLAST policy."""
    return sum(1 for line in fasta_text.splitlines() if line.startswith(">"))


def _normalize_input_type(input_type: str | None) -> str | None:
    aliases = {
        "beginner": "pasted_sequence",   # Beginner Quick Tree sends pasted FASTA in job_params["sequence"]
        "sequence": "pasted_sequence",
        "accessions": "accession_list",
        "fasta": "fasta_upload",
    }
    return aliases.get(input_type, input_type)


def _normalize_blast_mode(blast_mode: str | None) -> str:
    """
    blast_mode:
      - "auto" (default): BLAST only if exactly one query sequence/accession
      - "on": force BLAST (still refused for multi-query inputs per your policy)
      - "off": never BLAST
    """
    mode = (blast_mode or "auto").strip().lower()
    if mode not in ("auto", "on", "off"):
        return "auto"
    return mode


def _should_blast_single_only(blast_mode: str, n_queries: int) -> bool:
    """
    Your policy:
      - Never BLAST multiple queries (multi FASTA or multiple accessions)
      - BLAST only when there's exactly 1 query, unless blast_mode="off"
      - blast_mode="on" still must respect the "single only" rule (we enforce below)
    """
    if blast_mode == "off":
        return False
    return n_queries == 1  # for "auto" and effectively for "on" too


def blast_expected_at_start(input_type: str | None, blast_mode: str) -> Optional[bool]:
    """Will this job BLAST? Tri-state, from normalized params alone.

    ``True``/``False`` when the answer is already settled; ``None`` when it
    depends on the record count, which is not known until the input step has
    parsed the FASTA. Both arguments must already be normalized by
    ``_normalize_input_type`` / ``_normalize_blast_mode``.

    This exists because the initial SSE metadata used to test
    ``input_type == "accession" and blast_mode == "optional"`` -- two values no
    normalizer can ever produce -- so every job, including the accession-list
    jobs that *require* BLAST to fetch their sequences, opened with the BLAST
    step pre-marked "skipped" and then flipped to running moments later. The
    computation was always correct; only the displayed pipeline lied.
    """
    if blast_mode == "off":
        # accession_list raises rather than running, so it is still not a BLAST.
        return False
    if input_type == "accession_list":
        # Exactly one accession is enforced downstream; BLAST is how the
        # sequence is fetched at all, so it is never optional here.
        return True
    if input_type in ("pasted_sequence", "fasta_upload"):
        # _should_blast_single_only() needs the record count.
        return None
    # Unknown input type: the input step raises before BLAST is reached.
    return False


def _count_alignment_stats(fasta_path) -> tuple[int, int]:
    """Count sequences and alignment columns from a FASTA file."""
    try:
        with open_artifact(fasta_path, 'rt') as f:
            content = f.read()
        records = parse_fasta_records(content)
        if not records:
            return 0, 0
        n_seqs = len(records)
        n_cols = len(records[0][1]) if records else 0
        return n_seqs, n_cols
    except Exception:
        return 0, 0


def _resolved_alignment_method(job_params: dict) -> str:
    """Return the alignment method this job will actually run, lowercased.

    ``"default"`` (and a missing value) resolve to the configured beginner
    aligner. Steps that need to know whether the input is already aligned --
    ORIENT, which runs long before STEP_ALIGN -- must use this rather than
    reading ``alignment_method`` raw, or a job submitted with ``"default"``
    would be classified differently in the two places.
    """
    method = job_params.get("alignment_method") or "default"
    method = str(method).strip().lower()
    if method == "default":
        method = str(Config.BEGINNER_DEFAULT_ALIGNER).lower()
    return method


def _resolve_orientation_plan(job_params: dict):
    """Return ``(requested, effective)`` orientation correction for this job.

    ``requested`` is the user's own setting, coerced through the project-wide
    `coerce_bool` -- ``bool("false")`` is True, so a stored string form of the
    flag used to silently re-enable correction for a user who had turned it off.

    ``effective`` additionally accounts for ``alignment_method="none"``, which
    asserts the submitted records are ALREADY ALIGNED. Reverse-complementing one
    row of an existing alignment destroys the column correspondence that makes
    it an alignment, so no correction happens for such a job however the flag is
    set. `run_alignment` already declines to correct direction for ``none``; the
    ORIENT step runs much earlier and used to rewrite input_raw.fasta anyway.
    """
    from app.services.security_utils import coerce_bool

    params = job_params or {}
    requested = coerce_bool(params.get("fix_orientation"), True)[0]
    prealigned = _resolved_alignment_method(params) == "none"
    return requested, (requested and not prealigned)


def _check_and_maybe_fix_orientation(input_path, fix_orientation: bool) -> dict:
    """Classify orientations, only rewriting the FASTA when correction is on."""
    from app.services.orientation_service import fix_sequence_orientation

    orient_fasta = input_path.read_text(encoding="utf-8", errors="replace")
    fixed_fasta, orient_stats = fix_sequence_orientation(orient_fasta)
    if fix_orientation:
        input_path.write_text(fixed_fasta, encoding="utf-8")
    return orient_stats


@background_job_context(0)
def run_recompute_job(job_id: str, params_dict: dict) -> dict:
    """Background task for recomputing an existing tree while streaming status events."""
    job = get_current_job()
    job_dir = Config.JOB_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "logs").mkdir(exist_ok=True)

    log_file = job_dir / "logs" / "pipeline.log"
    file_handler = _add_job_log_handler(job_id, log_file)

    try:
        from app import create_app
        from app.extensions import db
        from app.models import Job
        from app.services.security_utils import coerce_bool
        from app.services.tree_edit_service import build_recompute_job_params, recompute_tree

        _app = create_app()

        if job:
            job.meta.setdefault("steps", get_initial_steps_meta())
            job.meta["current_step"] = None
            job.meta["current_tool"] = None
            job.meta["started_at"] = time.time()
            job.save_meta()

        with _app.app_context():
            db_job = Job.query.get(job_id)
            if db_job:
                bind_background_context(user=background_user_identity(db_job))
                metrics = db_job.metrics or {}
                metrics["recompute_started_at"] = datetime.now(timezone.utc).isoformat()
                db_job.metrics = metrics
                db_job.status = "running"
                db.session.commit()

        publish_job_running(job_id)
        publish_overview(job_id, "Starting tree recompute...")

        job_params = build_recompute_job_params(params_dict)
        result = recompute_tree(
            job_dir,
            job_params,
            Config,
            logger,
            event_job_id=job_id,
            rq_job=job,
            use_current_input=coerce_bool(params_dict.get("use_current_input"), False)[0]
        )
        require_valid_pipeline_outputs(
            job_dir, job_params, logger, recompute=True
        )

        with _app.app_context():
            db_job = Job.query.get(job_id)
            if db_job:
                metrics = db_job.metrics or {}
                metrics["recompute_completed_at"] = datetime.now(timezone.utc).isoformat()
                db_job.metrics = metrics
                db_job.status = "completed"
                db.session.commit()

        publish_job_completed(
            job_id,
            view_url=f"/job/{job_id}/view",
            result_files={
                "tree_newick": f"/api/job/{job_id}/download/tree/newick",
                "tree_nexus": f"/api/job/{job_id}/download/tree/nexus",
                "fasta_original": f"/api/job/{job_id}/download/fasta/original",
                **({"mrbayes": f"/api/job/{job_id}/download/mrbayes"}
                   if (job_dir / "tree" / "mrbayes_input.nex").is_file() else {}),
                **({"alignment_inspection": f"/api/job/{job_id}/download/alignment/inspection"}
                   if artifact_exists(job_dir / "alignment" / "alignment_trimmed_report.html") else {}),
            }
        )
        publish_overview(job_id, "Recompute complete! Redirecting to tree viewer...")

        return {
            "job_id": job_id,
            "status": "completed",
            "message": "Tree recompute finished successfully",
            "result": result,
        }

    except Exception as e:
        error_msg = str(e)
        tb = traceback.format_exc()
        logger.exception("event=job.recompute_failed Recompute job failed")

        failed_step = "unknown"
        failed_step_label = "Recompute"
        current_tool = None
        if job:
            failed_step = job.meta.get("current_step") or failed_step
            current_tool = job.meta.get("current_tool")
            step_info = (job.meta.get("steps") or {}).get(failed_step, {})
            failed_step_label = step_info.get("label") or failed_step_label
            if failed_step != "unknown":
                publish_step_failed(job_id, failed_step, error_msg, tool=current_tool)
                update_step_meta(job, failed_step, {"state": STATE_FAILED, "error": error_msg})

        publish_job_failed(
            job_id,
            failed_step=failed_step,
            failed_step_label=failed_step_label,
            error_summary=error_msg,
            tool=current_tool or ""
        )

        try:
            from app import create_app
            from app.extensions import db
            from app.models import Job

            _app = create_app()
            with _app.app_context():
                db_job = Job.query.get(job_id)
                if db_job:
                    metrics = db_job.metrics or {}
                    metrics["recompute_failed_at"] = datetime.now(timezone.utc).isoformat()
                    metrics["error"] = error_msg
                    metrics["failed_step"] = failed_step
                    db_job.metrics = metrics
                    db_job.status = "failed"
                    db.session.commit()
        except Exception as db_err:
            logger.error(f"Failed to update DB on recompute error: {db_err}")

        raise

    finally:
        try:
            _remove_job_log_handler(file_handler)
        except Exception:
            pass


@background_job_context()
def run_mycomap_blast_refresh_job(params: dict) -> dict:
    """
    Refresh a MycoMap BLAST result (local always, NCBI optionally) and gather
    its sequences for the browser's sequence queue.

    Local reruns complete synchronously. When an NCBI rebuild is requested,
    this task queues it at MycoMap, then returns ``Retry`` so RQ resumes the
    job after a wait instead of blocking a worker slot for ~10 minutes.
    """
    from app.services.mycomap_service import (
        MycoMapRerunError,
        get_mycomap_ncbi_rerun_wait_seconds,
        rerun_mycomap_blast,
        validate_mycomap_rerun_limit,
        validate_mycomap_url,
    )

    job = get_current_job()
    url = params.get("url", "")
    blast_id = validate_mycomap_url(url)
    if not blast_id:
        return {"status": "error", "error": "Invalid Mycomap URL."}

    resuming = bool(job and job.meta.get("mycomap_refresh_stage") == "waiting_for_ncbi")
    warnings = list((job.meta.get("mycomap_refresh_warnings") or [])) if (job and resuming) else []

    if not resuming:
        local_limit, local_error = validate_mycomap_rerun_limit(params.get("local_limit"), "local")
        if local_error:
            return {"status": "error", "error": local_error}
        try:
            rerun_mycomap_blast(blast_id, result_type="local", limit=local_limit)
        except MycoMapRerunError as exc:
            warning = f"MycoMap local BLAST could not be refreshed; using saved results instead. {exc}"
            logger.warning("%s blast_id=%s", warning, blast_id)
            warnings.append(warning)

        if params.get("rebuild_ncbi"):
            ncbi_limit, ncbi_error = validate_mycomap_rerun_limit(params.get("ncbi_limit"), "ncbi")
            if ncbi_error:
                return {"status": "error", "error": ncbi_error}
            try:
                rerun_mycomap_blast(blast_id, result_type="ncbi", limit=ncbi_limit)
                wait_seconds = get_mycomap_ncbi_rerun_wait_seconds()
                if job:
                    job.meta["mycomap_refresh_stage"] = "waiting_for_ncbi"
                    job.meta["mycomap_refresh_warnings"] = warnings
                    job.save_meta()
                return Retry(max=1, interval=wait_seconds)
            except MycoMapRerunError as exc:
                warning = f"MycoMap NCBI BLAST could not be rebuilt; using saved results instead. {exc}"
                logger.warning("%s blast_id=%s", warning, blast_id)
                warnings.append(warning)

    from app.api.routes import gather_mycomap_sequences_for_queue

    payload, err = gather_mycomap_sequences_for_queue(
        url,
        include_ncbi=bool(params.get("include_ncbi", True)),
        include_local=bool(params.get("include_local", True)),
    )
    if err is not None:
        body, _status = err
        return {"status": "error", "error": body.get("error", "MycoMap BLAST refresh failed.")}

    if warnings:
        payload["warnings"] = warnings
    return payload


@background_job_context()
def run_phylo_job(job_params: dict) -> dict:
    """
    Main phylogenetic analysis job.
    
    Publishes real-time events for the SSE status dashboard.
    Uses centralized exception handling - no early returns on error.
    """
    job = get_current_job()
    job_id = job.id if job else "local_debug"

    # RQ cancellation and worker dequeue are not one atomic operation. DELETE
    # commits a DB-visible guard first, and every real RQ execution checks it
    # before creating even the top-level job directory. A missing row means a
    # successful deletion already committed; `deleting` means it is in flight.
    from app import create_app
    from app.extensions import db
    from app.models import Job

    _app = create_app()
    if job is not None:
        with _app.app_context():
            db_job = Job.query.get(job_id)
            if db_job is None or db_job.status == "deleting":
                logger.info(
                    "event=job.start_suppressed job=%s reason=%s",
                    job_id, "missing" if db_job is None else "deleting",
                )
                return {"status": "cancelled", "job_id": job_id}
    
    # Track current step for error reporting
    current_step = None
    current_tool = None
    current_step_label = None
    
    from app.services.security_utils import coerce_bool
    job_params = dict(job_params or {})
    inat_preparation = job_params.get("_inat_tree_preparation")
    mo_preparation = job_params.get("_mo_tree_preparation")
    if isinstance(inat_preparation, dict):
        tree_preparation = inat_preparation
        tree_preparation_kind = "inat"
    elif isinstance(mo_preparation, dict):
        tree_preparation = mo_preparation
        tree_preparation_kind = "mo"
    else:
        tree_preparation = None
        tree_preparation_kind = None
    tree_preparation_pending = isinstance(tree_preparation, dict)
    tree_preparation_meta_key = (
        f"{tree_preparation_kind}_tree_preparation" if tree_preparation_kind else None
    )
    tree_resuming_after_ncbi = bool(
        job
        and tree_preparation_meta_key
        and job.meta.get(tree_preparation_meta_key) == "waiting_for_ncbi"
    )
    job_params["trim_terminal_overhangs"] = coerce_bool(
        job_params.get("trim_terminal_overhangs"), True
    )[0]
    # Same treatment for fix_orientation: bool("false") is True, so a stored
    # string form of the flag used to re-enable orientation correction for a
    # user who had switched it off.
    job_params["fix_orientation"] = coerce_bool(
        job_params.get("fix_orientation"), True
    )[0]

    # 1. Create Job Directory
    job_dir = Config.JOB_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (job_dir / "input").mkdir(exist_ok=True)
    (job_dir / "blast").mkdir(exist_ok=True)
    (job_dir / "alignment").mkdir(exist_ok=True)
    (job_dir / "tree").mkdir(exist_ok=True)
    (job_dir / "logs").mkdir(exist_ok=True)

    # 2. Save job_params
    input_info_path = job_dir / "input_info.json"
    _save_job_params(input_info_path, job_params)

    # 2b. Initialize with single app context
    # Per-job pipeline log
    log_file = job_dir / "logs" / "pipeline.log"
    file_handler = _add_job_log_handler(job_id, log_file)
    logger.info(
        "event=job.started Starting job summary=%s",
        json.dumps(summarize_job_params(job_params), sort_keys=True, separators=(",", ":")),
    )

    input_raw_path = job_dir / "input" / "input_raw.fasta"
    blast_result = None

    try:
        # Use single app context for all DB operations
        with _app.app_context():
            # Initialize DB status
            db_job = Job.query.get(job_id)
            if db_job:
                bind_background_context(user=background_user_identity(db_job))
                metrics = db_job.metrics or {}
                metrics["started_at"] = datetime.now(timezone.utc).isoformat()
                
                # Check for validation warnings passed from API
                validation_warnings = job_params.get("validation_warnings", [])
                if validation_warnings:
                    metrics["validation_warnings"] = validation_warnings
                    logger.warning(f"Job started with params warnings: {validation_warnings}")

                db_job.metrics = metrics
                db_job.status = "running"
                db.session.commit()
            
            # Initialize job meta for SSE
            if job:
                job.meta["steps"] = get_initial_steps_meta()
                job.meta["current_step"] = None
                job.meta["current_tool"] = None
                job.meta["started_at"] = time.time()
                
                # Pre-mark optional steps as skipped based on job parameters
                # This ensures the UI shows correct pipeline from the start
                input_type = _normalize_input_type(job_params.get("input_type"))
                blast_mode = _normalize_blast_mode(job_params.get("blast_mode"))
                from app.services.trimming_service import (
                    describe_trim_step, resolve_trimming_method,
                )
                trim_method = resolve_trimming_method(job_params)
                # Already normalized to a bool near the top of run_phylo_job.
                trim_terminal_overhangs = bool(job_params.get("trim_terminal_overhangs", True))

                # Only pre-mark BLAST skipped when that is already certain.
                # When it depends on the record count (None), leave the step
                # queued and let the BLAST step itself set the final state --
                # which it does on both branches.
                if blast_expected_at_start(input_type, blast_mode) is False:
                    job.meta["steps"][STEP_BLAST]["state"] = STATE_SKIPPED
                    job.meta["steps"][STEP_BLAST]["label"] = "BLAST Search (skipped)"

                # Trim is skipped only when both external and terminal trimming are disabled.
                should_trim, _trim_label, _trim_tool = describe_trim_step(trim_method, trim_terminal_overhangs)
                if not should_trim:
                    job.meta["steps"][STEP_TRIM]["state"] = STATE_SKIPPED
                    job.meta["steps"][STEP_TRIM]["label"] = "Trimming (skipped)"

                if tree_preparation_pending:
                    job.meta["steps"][STEP_INPUT]["label"] = "MycoMap Input Preparation"
                    job.meta[tree_preparation_meta_key] = "running"
                
                job.save_meta()
            
            # Publish job running
            publish_job_running(job_id)
            publish_overview(job_id, "Starting pipeline...")

            if tree_preparation_pending:
                current_step = STEP_INPUT
                current_step_label = "MycoMap Input Preparation"
                current_tool = None
                if job:
                    job.meta["current_step"] = current_step
                    job.save_meta()
                publish_step_start(
                    job_id,
                    STEP_INPUT,
                    current_step_label,
                    "Checking source data and collecting MycoMap results",
                )
                update_step_meta(job, STEP_INPUT, {
                    "state": STATE_RUNNING,
                    "label": current_step_label,
                })
                publish_overview(
                    job_id,
                    "Checking the source observation for ITS data and MycoMap BLAST results...",
                )

                if tree_preparation_kind == "mo":
                    from app.services.mushroom_observer_service import prepare_tree_job

                    prepared = prepare_tree_job(
                        tree_preparation,
                        defer_after_ncbi_rerun=not tree_resuming_after_ncbi,
                        skip_mycomap_refresh=tree_resuming_after_ncbi,
                        mycomap_rerun_details=(
                            job.meta.get("mycomap_rerun_details") if job else None
                        ),
                    )
                else:
                    from app.services.inaturalist_tree_service import prepare_inat_tree_job

                    prepared = prepare_inat_tree_job(
                        int(tree_preparation["observation_id"]),
                        include_ncbi=bool(tree_preparation.get("include_ncbi", True)),
                        include_local=bool(tree_preparation.get("include_local", True)),
                        rebuild_ncbi_blast=bool(tree_preparation.get("rebuild_ncbi_blast")),
                        recreate_existing_tree=bool(
                            tree_preparation.get("recreate_existing_tree")
                        ),
                        keep_existing_tree_url=bool(
                            tree_preparation.get("keep_existing_tree_url")
                        ),
                        mycomap_local_limit=tree_preparation.get("mycomap_local_limit"),
                        mycomap_ncbi_limit=tree_preparation.get("mycomap_ncbi_limit"),
                        defer_after_ncbi_rerun=not tree_resuming_after_ncbi,
                        skip_mycomap_refresh=tree_resuming_after_ncbi,
                        mycomap_rerun_details=(
                            job.meta.get("mycomap_rerun_details") if job else None
                        ),
                    )
                if prepared.get("status") == "waiting_for_ncbi":
                    from app.services.mycomap_service import (
                        get_mycomap_ncbi_poll_interval_seconds,
                        get_mycomap_ncbi_poll_max_attempts,
                        get_mycomap_ncbi_rerun_wait_seconds,
                    )

                    rerun_details = prepared.get("mycomap_rerun_details") or {}
                    auto_created = bool(rerun_details.get("auto_created"))
                    wait_seconds = (
                        get_mycomap_ncbi_poll_interval_seconds()
                        if auto_created else get_mycomap_ncbi_rerun_wait_seconds()
                    )
                    max_retry_attempts = (
                        get_mycomap_ncbi_poll_max_attempts() if auto_created else 1
                    )
                    resume_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
                    refresh_warnings = list(rerun_details.get("warnings") or [])
                    if auto_created:
                        poll_attempt = int(rerun_details.get("ncbi_poll_attempt") or 0)
                        queue_position = rerun_details.get("ncbi_queue_position")
                        queue_suffix = (
                            f" MycoMap reports this search is at position "
                            f"{queue_position} in its NCBI BLAST queue."
                            if queue_position is not None else ""
                        )
                        if rerun_details.get("creation_pending"):
                            from app.services.mycomap_service import (
                                get_mycomap_creation_discovery_max_seconds,
                            )

                            discovery_minutes = max(1, round(
                                get_mycomap_creation_discovery_max_seconds() / 60
                            ))
                            waiting_message = (
                                "MycoMap accepted the BLAST request and queued it. "
                                "Dikarya is waiting for the result page to appear and "
                                "will continue in one minute; if MycoMap's queue has "
                                f"not produced results within {discovery_minutes} "
                                f"minute{'s' if discovery_minutes != 1 else ''}, this "
                                "tree will stop and can be rebuilt later."
                            )
                        else:
                            if tree_preparation_kind == "mo":
                                waiting_message = (
                                    "MycoMap BLAST was created from the selected Mushroom "
                                    "Observer ITS sequence. NCBI results are not ready yet; "
                                    f"check {poll_attempt + 1} will run in one minute."
                                    f"{queue_suffix}"
                                )
                            else:
                                waiting_message = (
                                    "MycoMap BLAST was created from the observation's DNA "
                                    "Barcode ITS and its URL was added to iNaturalist. "
                                    f"NCBI results are not ready yet; check {poll_attempt + 1} "
                                    f"will run in one minute.{queue_suffix}"
                                )
                    else:
                        waiting_message = (
                            "MycoMap NCBI BLAST was queued. This tree will resume "
                            f"in about {max(1, round(wait_seconds / 60))} minute"
                            f"{'s' if max(1, round(wait_seconds / 60)) != 1 else ''}; "
                            "other tree jobs can run while it waits."
                        )

                    db_job = Job.query.get(job_id)
                    if db_job:
                        metrics = dict(db_job.metrics or {})
                        metrics.update({
                            "notes": prepared.get("notes") or metrics.get("notes"),
                            "mycomap_blast_url": prepared.get("mycomap_blast_url"),
                            "mycomap_blast_rerun": rerun_details,
                            "mycomap_local_blast_rebuilt": (
                                rerun_details.get("local_status") == "completed"
                            ),
                            "mycomap_ncbi_blast_rebuilt": False,
                            "mycomap_blast_auto_created": auto_created,
                            "mycomap_preparation_status": "waiting_for_ncbi",
                            "mycomap_ncbi_resume_at": resume_at.isoformat(),
                        })
                        if tree_preparation_kind == "inat":
                            metrics["inat_genus"] = prepared.get("inat_genus") or ""
                        if refresh_warnings:
                            metrics["mycomap_refresh_warnings"] = refresh_warnings
                        db_job.metrics = metrics
                        db_job.status = "queued"
                        db.session.commit()

                    if job:
                        job.meta[tree_preparation_meta_key] = "waiting_for_ncbi"
                        job.meta["mycomap_rerun_details"] = rerun_details
                        job.meta["mycomap_ncbi_resume_at"] = resume_at.isoformat()
                        if refresh_warnings:
                            job.meta["mycomap_refresh_warnings"] = refresh_warnings
                        job.meta["steps"][STEP_INPUT].update({
                            "state": STATE_QUEUED,
                            "label": "Waiting for MycoMap NCBI Results",
                            "detail": waiting_message,
                        })
                        job.meta["current_step"] = STEP_INPUT
                        job.save_meta()

                    for warning in refresh_warnings:
                        publish_overview(job_id, warning)
                    publish_overview(job_id, waiting_message)
                    publish_job_queued(job_id)
                    return Retry(max=max_retry_attempts, interval=wait_seconds)

                job_params = prepared["job_params"]
                _save_job_params(input_info_path, job_params)

                db_job = Job.query.get(job_id)
                if db_job:
                    metrics = dict(db_job.metrics or {})
                    metrics.update(prepared["metrics"])
                    db_job.metrics = metrics
                    db_job.input_type = job_params["input_type"]
                    db.session.commit()

                tree_preparation_pending = False
                refresh_warnings = list(
                    prepared["metrics"].get("mycomap_refresh_warnings") or []
                )
                if job:
                    job.meta[tree_preparation_meta_key] = "completed"
                    if refresh_warnings:
                        job.meta["mycomap_refresh_warnings"] = refresh_warnings
                    job.save_meta()
                for warning in refresh_warnings:
                    publish_overview(job_id, warning)
                publish_overview(
                    job_id,
                    "MycoMap results imported. Validating the tree input...",
                )
            
            # =========================================================
            # STEP: INPUT PROCESSING
            # =========================================================
            current_step = STEP_INPUT
            current_step_label = "Input Processing"
            current_tool = None
            
            if job:
                job.meta["current_step"] = current_step
                job.save_meta()
            
            publish_step_start(job_id, STEP_INPUT, "Input Processing", "Validating input data")
            update_step_meta(job, STEP_INPUT, {
                "state": STATE_RUNNING,
                "label": "Input Processing",
                "detail": "Validating input data",
            })
            
            input_type = _normalize_input_type(job_params.get("input_type"))
            blast_mode = _normalize_blast_mode(job_params.get("blast_mode"))
            do_blast = False
            n_records = 0

            if input_type == "accession_list":
                accessions = job_params.get("accessions", []) or []
                logger.info(f"Processing accession list: {accessions}")

                if len(accessions) != 1:
                    raise ValueError("accession_list jobs must contain exactly one accession (refusing to BLAST multiple).")

                if blast_mode == "off":
                    raise ValueError("BLAST is disabled (blast_mode=off), but accession_list requires BLAST to fetch sequences.")

                do_blast = True
                n_records = 1

            elif input_type == "pasted_sequence":
                sequence = (job_params.get("sequence") or "").strip()
                logger.info("Processing pasted sequence")

                if not sequence:
                    raise ValueError(
                        "No DNA sequence was provided. Paste FASTA text such as "
                        "'>sample_1' followed by the DNA sequence on the next line."
                    )
                if not sequence.lstrip().startswith(">"):
                    raise ValueError(
                        "Pasted input is not FASTA. Start each record with a header "
                        "line beginning with '>', for example '>sample_1', followed "
                        "by its DNA sequence on the next line."
                    )

                # Bound header lengths before anything downstream reads them:
                # MAFFT and trimAl parse these names directly.
                sequence, capped = cap_fasta_headers(sequence)
                if capped:
                    logger.warning(f"Capped {capped} over-long FASTA header(s) in pasted input")

                n_records = validate_dna_fasta(sequence)

                # Always write what the user gave us
                input_raw_path.write_text(sequence + "\n", encoding="utf-8")
                logger.info(f"Wrote input FASTA: {input_raw_path} ({input_raw_path.stat().st_size} bytes)")

                logger.info(f"Input FASTA records: {n_records}")

                # Optional BLAST only if exactly 1 record
                if blast_mode == "on" and n_records != 1:
                    raise ValueError("Refusing to BLAST multi-sequence FASTA (blast_mode=on requested). Provide exactly 1 record.")

                do_blast = _should_blast_single_only(blast_mode, n_records)

            elif input_type == "fasta_upload":
                logger.info("Processing FASTA upload")

                if not input_raw_path.exists():
                    raise FileNotFoundError(
                        "The uploaded FASTA file was not available when processing "
                        "started. Return to Tree Builder, upload the file again, and "
                        "resubmit the job. If this repeats, report the job ID."
                    )

                uploaded = input_raw_path.read_text(encoding="utf-8", errors="replace")

                # Same header cap as the pasted path. The upload landed on disk
                # unfiltered, so rewrite the file before the pipeline reads it.
                uploaded, capped = cap_fasta_headers(uploaded)
                if capped:
                    logger.warning(f"Capped {capped} over-long FASTA header(s) in uploaded file")
                    input_raw_path.write_text(uploaded, encoding="utf-8")

                n_records = validate_dna_fasta(uploaded)
                logger.info(f"Uploaded FASTA records: {n_records}")

                if blast_mode == "on" and n_records != 1:
                    raise ValueError("Refusing to BLAST multi-sequence FASTA upload (blast_mode=on requested). Provide exactly 1 record.")

                do_blast = _should_blast_single_only(blast_mode, n_records)

            else:
                raise ValueError(f"Unknown input type: {input_type}")

            publish_step_done(job_id, STEP_INPUT, f"{n_records} sequence(s) validated")
            publish_overview(job_id, f"Input validated: {n_records} sequence(s)")
            update_step_meta(job, STEP_INPUT, {"state": STATE_DONE, "detail": f"{n_records} sequences"})

            # =========================================================
            # STEP: BLAST (optional)
            # =========================================================
            current_step = STEP_BLAST
            current_step_label = "BLAST Search"
            
            if job:
                job.meta["current_step"] = current_step
                job.save_meta()

            if do_blast:
                publish_step_start(job_id, STEP_BLAST, "BLAST Search", "Searching NCBI database")
                update_step_meta(job, STEP_BLAST, {"state": STATE_RUNNING})
                
                if input_type == "accession_list":
                    accessions = job_params.get("accessions", [])
                    from app.services.blast_service import blast_from_accessions
                    blast_result = blast_from_accessions(accessions, Config, logger)
                else:
                    sequence = input_raw_path.read_text(encoding="utf-8")
                    from app.services.blast_service import blast_from_sequence
                    blast_result = blast_from_sequence(sequence, Config, logger)
                
                if blast_result:
                    with open(job_dir / "blast" / "blast_results.json", "w") as f:
                        json.dump(blast_result, f, indent=2)

                    fasta_path = blast_result.get("fasta_path")
                    if fasta_path:
                        with open(fasta_path, "r") as src, open(job_dir / "blast" / "blast_sequences.fasta", "w") as dst:
                            dst.write(src.read())
                        with open(fasta_path, "r") as src:
                            input_raw_path.write_text(src.read(), encoding="utf-8")
                        logger.info(f"Replaced input FASTA with BLAST-expanded dataset from {fasta_path}")
                    
                    hit_count = blast_result.get("sequence_count", 0)
                    publish_step_done(job_id, STEP_BLAST, f"{hit_count} sequences found")
                    publish_overview(job_id, f"BLAST complete: {hit_count} related sequences found")
                    update_step_meta(job, STEP_BLAST, {"state": STATE_DONE, "detail": f"{hit_count} sequences"})
            else:
                # BLAST skipped
                skip_reason = "multi-sequence input" if n_records > 1 else "disabled"
                update_step_meta(job, STEP_BLAST, {"state": STATE_SKIPPED, "label": "BLAST Search (skipped)"})
                publish_overview(job_id, f"BLAST skipped ({skip_reason})")
                logger.info(f"Skipping BLAST ({skip_reason})")

            # Final guard: alignment requires input_raw.fasta
            if not input_raw_path.exists() or input_raw_path.stat().st_size == 0:
                raise FileNotFoundError(f"Missing or empty input FASTA: {input_raw_path}")

            # After input is finalized: ensure enough sequences to build alignment/tree
            final_fasta = input_raw_path.read_text(encoding="utf-8", errors="replace")
            validate_dna_fasta(final_fasta)
            if job_params.get("preserve_exact_duplicate_records"):
                final_fasta, stats = uniquify_fasta_identifiers(final_fasta)
            else:
                final_fasta, stats = dedupe_and_uniquify_fasta(final_fasta)
            input_raw_path.write_text(final_fasta, encoding="utf-8")

            logger.info(
                "FASTA cleanup: input_records=%s kept=%s dropped_exact=%s renamed_ids=%s",
                stats["input_records"],
                stats["kept_records"],
                stats["dropped_exact_duplicates"],
                stats["renamed_due_to_duplicate_ids"],
            )

            final_n = fasta_record_count(final_fasta)
            logger.info(f"Final FASTA records going into alignment: {final_n}")
            if final_n < 2:
                raise ValueError(
                    "At least two distinct DNA sequences are required to build a "
                    "tree. Add another FASTA record, or enable BLAST when submitting "
                    "a single query sequence."
                )

            # =========================================================
            # STEP: ORIENTATION CHECK
            # =========================================================
            current_step = STEP_ORIENT
            current_step_label = "Orientation Check"
            current_tool = None
            
            if job:
                job.meta["current_step"] = current_step
                job.save_meta()
            
            publish_step_start(job_id, STEP_ORIENT, "Orientation Check", "Checking sequence orientations")
            update_step_meta(job, STEP_ORIENT, {"state": STATE_RUNNING})
            
            # One helper resolves both the flag and the already-aligned case, so
            # ORIENT and STEP_ALIGN cannot disagree about either.
            fix_orientation, correct_orientation = _resolve_orientation_plan(
                job_params
            )
            input_is_prealigned = _resolved_alignment_method(job_params) == "none"
            orient_stats = _check_and_maybe_fix_orientation(
                input_raw_path, correct_orientation
            )
            
            reversed_count = orient_stats.get("reverse", 0)
            uncertain_count = orient_stats.get("uncertain", 0)
            
            if input_is_prealigned:
                orient_detail = (
                    "Orientation correction skipped (input is already aligned)"
                )
                if reversed_count > 0:
                    orient_detail += f"; {reversed_count} reverse sequence(s) detected"
                if uncertain_count > 0:
                    orient_detail += f", {uncertain_count} uncertain"
            elif not fix_orientation:
                orient_detail = "Orientation correction disabled"
                if reversed_count > 0:
                    orient_detail += f"; {reversed_count} reverse sequence(s) detected"
                if uncertain_count > 0:
                    orient_detail += f", {uncertain_count} uncertain"
            elif reversed_count > 0:
                orient_detail = f"{reversed_count} sequence(s) reverse complemented"
                if uncertain_count > 0:
                    orient_detail += f", {uncertain_count} uncertain"
            elif uncertain_count > 0:
                orient_detail = f"All forward, {uncertain_count} uncertain orientation"
            else:
                orient_detail = "All sequences correctly oriented"
            
            logger.info(
                "Orientation check: correction_enabled=%s total=%s forward=%s reverse=%s uncertain=%s",
                correct_orientation,
                orient_stats.get("total", 0),
                orient_stats.get("forward", 0),
                reversed_count,
                uncertain_count,
            )
            
            publish_step_done(job_id, STEP_ORIENT, orient_detail)
            update_step_meta(job, STEP_ORIENT, {"state": STATE_DONE, "detail": orient_detail})
            
            if correct_orientation and reversed_count > 0:
                publish_metric(job_id, STEP_ORIENT, "reversed", reversed_count)

            # =========================================================
            # STEP: ITS REGION EXTRACTION (optional)
            # =========================================================
            current_step = STEP_ITS
            current_tool = None
            from app.services.its_extraction_service import (
                describe_its_step,
                format_its_detail,
                normalize_region,
                resolve_min_length,
                run_its_extraction,
            )

            its_region = normalize_region(job_params.get("its_region"))
            should_extract, current_step_label = describe_its_step(its_region)
            its_stats = None

            if job:
                job.meta["current_step"] = current_step
                job.save_meta()

            if should_extract:
                its_min_length = resolve_min_length(its_region, job_params.get("its_min_length"))
                publish_step_start(
                    job_id, STEP_ITS, current_step_label,
                    f"minimum {its_min_length} bp",
                )
                update_step_meta(job, STEP_ITS, {
                    "state": STATE_RUNNING,
                    "label": current_step_label,
                })

                its_output_path = job_dir / "input" / "its_extracted.fasta"
                its_stats = run_its_extraction(
                    input_raw_path,
                    its_output_path,
                    its_region,
                    Config,
                    logger,
                    min_length=its_min_length,
                    job_id=job_id,
                )
                # Downstream steps all read input_raw_path, so swap in the
                # extracted sequences the same way the orientation step does.
                input_raw_path.write_text(
                    its_output_path.read_text(encoding="utf-8"), encoding="utf-8"
                )

                its_detail = format_its_detail(its_stats)
                publish_step_done(job_id, STEP_ITS, its_detail)
                publish_overview(job_id, f"ITS region extraction: {its_detail}")
                update_step_meta(job, STEP_ITS, {
                    "state": STATE_DONE,
                    "label": current_step_label,
                    "detail": its_detail,
                })
                publish_metric(job_id, STEP_ITS, "kept", its_stats.get("kept_count", 0))
                publish_metric(job_id, STEP_ITS, "dropped", its_stats.get("dropped_count", 0))
            else:
                update_step_meta(job, STEP_ITS, {
                    "state": STATE_SKIPPED,
                    "label": current_step_label,
                })

            # Persist for the job viewer's Generation Details panel.
            job_params["its_extraction_details"] = its_stats
            _save_job_params(input_info_path, job_params)

            # =========================================================
            # STEP: ALIGNMENT
            # =========================================================
            current_step = STEP_ALIGN
            # Resolved by the same helper the ORIENT step used, so the two
            # steps can never disagree about whether this job skips alignment.
            align_method = _resolved_alignment_method(job_params)
            
            current_tool = align_method.lower()
            current_step_label = f"Alignment ({align_method.upper()})"
            
            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = current_tool
                job.save_meta()
            
            alignment_raw_path = job_dir / "alignment" / "alignment_raw.fasta"
            alignment_trimmed_path = job_dir / "alignment" / "alignment_trimmed.fasta"

            # This run is about to write both alignments fresh. If a previous
            # run's outputs were gzipped by the cold-artifact sweep, drop them
            # now so the job never carries a stale compressed copy alongside the
            # new plain one.
            discard_gzipped_form(alignment_raw_path)
            discard_gzipped_form(alignment_trimmed_path)

            threads = min(8, __import__('os').cpu_count() or 1)
            publish_step_start(job_id, STEP_ALIGN, current_step_label, f"{threads} threads", tool=current_tool)
            update_step_meta(job, STEP_ALIGN, {
                "state": STATE_RUNNING,
                "label": current_step_label,
                "tool": current_tool
            })

            align_opts = job_params.get("alignment_options", {})
            # Pass tree_method so MAFFT can use faster settings for NJ
            align_opts["tree_method"] = job_params.get("tree_method", "nj")
            from app.models import AlignmentParams
            from app.services.alignment_service import run_alignment

            # Default on: a backwards sequence is a wrong tree, and only MAFFT
            # notices without help. Off is for input the user has already
            # oriented and does not want touched.
            align_params = AlignmentParams(
                method=align_method,
                fix_orientation=fix_orientation,
                advanced_options=align_opts,
            )
            align_stats = run_alignment(
                input_raw_path, alignment_raw_path, align_params, Config, logger, job_id=job_id,
                # ORIENT still classifies sequences when correction is disabled,
                # but both it and the aligner leave the sequence data untouched.
                orient_uncertain=uncertain_count,
            ) or {}

            n_seqs, n_cols = _count_alignment_stats(alignment_raw_path)
            detail = f"{n_seqs} sequences, {n_cols} columns"

            # MAFFT runs --adjustdirectionaccurately, so it makes its own
            # orientation call after ORIENT already made one. A flip here means
            # the two disagreed, and it used to be stripped out silently along
            # with MAFFT's _R_ marker.
            aligner_reversed = int(align_stats.get("reversed_by_aligner") or 0)
            if aligner_reversed:
                detail += f", {aligner_reversed} reverse-complemented by the aligner"
                publish_metric(job_id, STEP_ALIGN, "reversed_by_aligner", aligner_reversed)

            publish_step_done(job_id, STEP_ALIGN, detail)
            publish_overview(job_id, f"Alignment complete: {detail}")
            update_step_meta(job, STEP_ALIGN, {"state": STATE_DONE, "detail": detail})
            publish_metric(job_id, STEP_ALIGN, "sequences", n_seqs)
            publish_metric(job_id, STEP_ALIGN, "columns", n_cols)

            # =========================================================
            # STEP: TRIMMING
            # =========================================================
            current_step = STEP_TRIM
            from app.services.trimming_service import (
                run_trimming, describe_trim_step, format_trimming_detail,
                resolve_trimming_method,
            )
            trim_method = resolve_trimming_method(job_params)
            # Already normalized to a bool near the top of run_phylo_job.
            trim_terminal_overhangs = bool(job_params.get("trim_terminal_overhangs", True))
            should_trim, current_step_label, current_tool = describe_trim_step(trim_method, trim_terminal_overhangs)

            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = current_tool
                job.save_meta()

            if should_trim:
                publish_step_start(job_id, STEP_TRIM, current_step_label, "", tool=current_tool)
                update_step_meta(job, STEP_TRIM, {
                    "state": STATE_RUNNING,
                    "label": current_step_label,
                    "tool": current_tool
                })

                trim_stats = run_trimming(
                    alignment_raw_path,
                    alignment_trimmed_path,
                    trim_method,
                    Config,
                    logger,
                    job_id=job_id,
                    trim_terminal_overhangs=trim_terminal_overhangs,
                )

                _, trimmed_cols = _count_alignment_stats(alignment_trimmed_path)
                detail = format_trimming_detail(trim_method, trim_stats, trimmed_cols)
                job_params["trimming_details"] = trim_stats
                _save_job_params(input_info_path, job_params)
                
                publish_step_done(job_id, STEP_TRIM, detail)
                publish_overview(job_id, f"Trimming complete: {detail}")
                update_step_meta(job, STEP_TRIM, {"state": STATE_DONE, "detail": detail})
            else:
                # Trimming skipped
                import shutil
                shutil.copy(alignment_raw_path, alignment_trimmed_path)
                job_params["trimming_details"] = {
                    "method": trim_method,
                    "trim_terminal_overhangs": False,
                    "terminal_overhang_trim": {
                        "enabled": False,
                        "removed_columns": 0,
                    },
                }
                _save_job_params(input_info_path, job_params)
                update_step_meta(job, STEP_TRIM, {"state": STATE_SKIPPED, "label": "Trimming (skipped)"})
                publish_overview(job_id, "Trimming skipped (method: none)")
                current_tool = None
                current_step_label = "Trimming (skipped)"

            # =========================================================
            # STEP: TREE BUILDING
            # =========================================================
            current_step = STEP_TREE
            tree_method = job_params.get("tree_method", "nj")
            # IQ-TREE defaults to ModelFinder (MFP) rather than a fixed GTR+G.
            # Resolved here, not in the web form, so the API, iNaturalist and
            # Mushroom Observer paths get the same default. An explicitly
            # supplied model always wins.
            if tree_method == "iqtree":
                tree_model = job_params.get("tree_model") or Config.DEFAULT_IQTREE_MODEL
            else:
                tree_model = job_params.get("tree_model") or Config.DEFAULT_ML_MODEL
            bootstrap = validate_iqtree_ufboot_count(
                tree_method,
                job_params.get("bootstrap", Config.DEFAULT_BOOTSTRAPS),
            )
            bootstrap = int(bootstrap)
            mcmc_gens = int(job_params.get("mcmc_generations", Config.DEFAULT_MCMC_GENERATIONS))
            mcmc_runs = int(job_params.get("mcmc_nruns", Config.DEFAULT_MCMC_NRNS))
            mcmc_chains = int(job_params.get("mcmc_nchains", Config.DEFAULT_MCMC_CHAINS))
            mcmc_burnin = float(
                job_params.get("mcmc_burnin_fraction", Config.DEFAULT_MCMC_BURNIN_FRACTION)
            )
            # Absent means the job predates the setting, so it ran without the
            # stop rule; every current submission path stores it explicitly.
            mcmc_stop_early = coerce_bool(job_params.get("mcmc_stop_early"), False)[0]
            
            current_tool = tree_method.lower()
            current_step_label = f"Tree Building ({tree_method.upper()})"
            
            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = current_tool
                job.save_meta()
            
            tree_newick_path = job_dir / "tree" / "tree_original.newick"
            tree_nexus_path = job_dir / "tree" / "tree_original.nexus"
            tree_metadata_path = job_dir / "tree" / "tree_metadata.json"
            
            # IQ-TREE defaults to UFBoot + SH-aLRT. Resolved here rather than in the web
            # form so the API, iNaturalist and Mushroom Observer paths get the same
            # defaults. An explicit alrt_replicates (including 0) is always honoured.
            if tree_method == "iqtree":
                alrt_replicates = job_params.get(
                    "alrt_replicates", Config.DEFAULT_IQTREE_ALRT
                )
                try:
                    alrt_replicates = max(0, min(10_000, int(alrt_replicates)))
                except (TypeError, ValueError):
                    alrt_replicates = Config.DEFAULT_IQTREE_ALRT
            else:
                alrt_replicates = 0

            # Persist the resolved values so the viewer's details panel can report
            # them even when the caller relied on the defaults.
            if (job_params.get("alrt_replicates") != alrt_replicates
                    or job_params.get("tree_model") != tree_model):
                job_params["alrt_replicates"] = alrt_replicates
                job_params["tree_model"] = tree_model
                _save_job_params(input_info_path, job_params)

            # "MFP"/"MF" mean nothing to a mycologist reading the progress line.
            detail_parts = [
                "ModelFinder (auto-select)"
                if tree_method == "iqtree" and tree_model.upper() in ("MFP", "MF", "TEST", "TESTNEW")
                else tree_model
            ]
            if tree_method in ("raxml", "iqtree") and bootstrap:
                label = "UFBoot" if tree_method == "iqtree" else "bootstraps"
                detail_parts.append(f"{bootstrap} {label}")
            if tree_method == "iqtree" and alrt_replicates:
                detail_parts.append(f"{alrt_replicates} SH-aLRT")
            if tree_method == "mrbayes":
                stop_rule_active = mcmc_stop_early and mcmc_runs > 1
                detail_parts.append(
                    f"up to {mcmc_gens} generations" if stop_rule_active
                    else f"{mcmc_gens} generations"
                )
                if stop_rule_active:
                    detail_parts.append("stop early at convergence criterion")
                detail_parts.append(f"{mcmc_runs} runs")
                detail_parts.append(f"{mcmc_chains} chains/run")
                detail_parts.append(f"{mcmc_burnin * 100:g}% burn-in")
            
            publish_step_start(job_id, STEP_TREE, current_step_label, ", ".join(detail_parts), tool=current_tool)
            update_step_meta(job, STEP_TREE, {
                "state": STATE_RUNNING,
                "label": current_step_label,
                "tool": current_tool
            })

            from app.models import TreeBuilderParams
            from app.services.tree_builder_service import run_tree_builder

            tree_params = TreeBuilderParams(
                method=tree_method,
                model=tree_model,
                bootstrap=bootstrap,
                alrt_replicates=alrt_replicates,
                mcmc_generations=mcmc_gens,
                mcmc_nruns=mcmc_runs,
                mcmc_nchains=mcmc_chains,
                mcmc_burnin_fraction=mcmc_burnin,
                mcmc_stop_early=mcmc_stop_early,
                # RAxML Params
                run_preset=job_params.get("run_preset", "fast_good"),
                bootstrap_preset=job_params.get("bootstrap_preset", "standard"),
                bootstrap_cap=job_params.get("bootstrap_cap"),
                enable_bootstrap=job_params.get("enable_bootstrap", True),
                start_tree_override=job_params.get("start_tree_override"),
                moose_enabled=job_params.get("moose_enabled", False),
                early_stopping=job_params.get("early_stopping", False),
                seed=job_params.get("seed"),
                outgroup=job_params.get("outgroup")
            )

            metadata = run_tree_builder(
                alignment_trimmed_path,
                tree_newick_path,
                tree_nexus_path,
                tree_params,
                Config,
                logger,
                job_id=job_id,
            )

            with open(tree_metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

            from app.services.tree_edit_service import load_tree_state
            load_tree_state(job_dir)

            require_valid_pipeline_outputs(job_dir, job_params, logger)

            publish_step_done(job_id, STEP_TREE, "Tree generated")
            publish_overview(job_id, f"Tree built using {tree_method.upper()}")
            update_step_meta(job, STEP_TREE, {"state": STATE_DONE, "detail": "Tree generated"})

            # =========================================================
            # STEP: POST-PROCESSING
            # =========================================================
            current_step = STEP_POST
            current_step_label = "Post-Processing"
            current_tool = None
            
            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = None
                job.save_meta()
            
            publish_step_start(job_id, STEP_POST, "Post-Processing", "Generating output files")
            update_step_meta(job, STEP_POST, {"state": STATE_RUNNING})

            # All files are already generated, just finalize
            publish_step_done(job_id, STEP_POST, "All files ready")
            update_step_meta(job, STEP_POST, {"state": STATE_DONE})

            # =========================================================
            # JOB COMPLETED
            # =========================================================
            logger.info("event=job.completed Job completed successfully")
            
            # Update DB
            db_job = Job.query.get(job_id)
            if db_job:
                metrics = db_job.metrics or {}
                metrics["completed_at"] = datetime.now(timezone.utc).isoformat()
                metrics["trim_terminal_overhangs"] = job_params.get("trim_terminal_overhangs")
                metrics["trimming_details"] = job_params.get("trimming_details")
                db_job.metrics = metrics
                db_job.status = "completed"
                db.session.commit()

                # If this tree was built from local-only MycoMap results
                # because NCBI's queue hadn't cleared in time, kick off an
                # hourly background re-check that appends NCBI hits and
                # rebuilds the tree once they finally show up.
                if (db_job.metrics or {}).get("mycomap_blast_rerun", {}).get(
                    "ncbi_fallback_local_only"
                ):
                    try:
                        from app.services.inaturalist_tree_service import (
                            schedule_initial_ncbi_recheck,
                        )
                        schedule_initial_ncbi_recheck(job_id)
                    except Exception:
                        from app.services.log_context import log_degradation
                        log_degradation(
                            logger, "ncbi_recheck_schedule_failed",
                            "Completed local-only tree could not schedule its NCBI follow-up",
                        )

            # iNaturalist post-completion hook: if this job came from the
            # /api/inaturalist/tree flow, write the public tree URL back to
            # the source observation. Failures here MUST NOT fail the job.
            try:
                if db_job and (db_job.metrics or {}).get("via") == "inat_phylogenetic_tree":
                    from sqlalchemy.orm.attributes import flag_modified
                    from app.services.inaturalist_tree_service import (
                        highlight_source_observation_tip,
                        post_completed_tree_to_inaturalist,
                    )
                    # Highlight the source observation's tip(s) in the tree
                    # viewer's default (blue) selection set before posting.
                    try:
                        _m = db_job.metrics or {}
                        extras = []
                        if _m.get("inat_added_its_name"):
                            extras.append(_m["inat_added_its_name"])
                        elif _m.get("inat_matched_its_tip"):
                            extras.append(_m["inat_matched_its_tip"])
                        highlighted_tip = highlight_source_observation_tip(
                            job_id,
                            int(_m.get("inat_observation_id") or 0),
                            extra_tip_names=extras,
                            display_name=_m.get("inat_source_display_name"),
                        )
                    except Exception as exc:
                        from app.services.log_context import log_degradation
                        log_degradation(
                            logger, "inat_tip_highlight_failed",
                            "Completed tree could not highlight its source observation tip",
                            exception=type(exc).__name__,
                        )
                        highlighted_tip = None
                    inat_result = post_completed_tree_to_inaturalist(
                        job_id, db_job.metrics or {}
                    )
                    # Build a fresh dict so SQLAlchemy reliably persists the
                    # change. The JSON column is not wrapped in MutableDict,
                    # so in-place mutation of the existing dict is not
                    # detected as dirty.
                    metrics = dict(db_job.metrics or {})
                    metrics["inat_update_status"] = inat_result.get("status", "failed")
                    metrics["inat_updated_at"] = datetime.now(timezone.utc).isoformat()
                    if inat_result.get("inat_tree_url"):
                        metrics["inat_tree_url"] = inat_result["inat_tree_url"]
                    if inat_result.get("inat_observation_field_value_id"):
                        metrics["inat_observation_field_value_id"] = (
                            inat_result["inat_observation_field_value_id"]
                        )
                    if inat_result.get("error"):
                        metrics["inat_update_error"] = inat_result["error"][:300]
                    if highlighted_tip:
                        # highlight_source_observation_tip returns a list of names.
                        if isinstance(highlighted_tip, list):
                            metrics["inat_highlighted_tips"] = [
                                str(t)[:300] for t in highlighted_tip
                            ]
                        else:
                            metrics["inat_highlighted_tips"] = [str(highlighted_tip)[:300]]
                    db_job.metrics = metrics
                    flag_modified(db_job, "metrics")
                    db.session.commit()
                    if inat_result.get("status") == "success":
                        publish_overview(
                            job_id,
                            "Posted Phylogenetic Tree link to iNaturalist observation."
                        )
                    else:
                        publish_overview(
                            job_id,
                            "Tree built; iNaturalist update did not succeed "
                            "(tree is still available)."
                        )
            except Exception as _inat_err:
                from app.services.log_context import log_degradation
                log_degradation(
                    logger, "inat_post_completion_failed",
                    "Tree completed but the iNaturalist delivery hook failed",
                    exception=type(_inat_err).__name__,
                )

            # Mushroom Observer post-completion hook: highlight the source tip
            # and post the public tree URL as a comment. Reporting failures do
            # not change the successfully completed tree job.
            try:
                if db_job and (db_job.metrics or {}).get("via") == "mo_phylogenetic_tree":
                    from sqlalchemy.orm.attributes import flag_modified
                    from app.services.mushroom_observer_service import (
                        highlight_source_observation_tip as highlight_mo_source_tip,
                        post_completed_tree_comment,
                    )

                    mo_metrics = dict(db_job.metrics or {})
                    extra_names = []
                    if mo_metrics.get("mo_added_its_name"):
                        extra_names.append(mo_metrics["mo_added_its_name"])
                    elif mo_metrics.get("mo_matched_its_tip"):
                        extra_names.append(mo_metrics["mo_matched_its_tip"])
                    highlighted_tips = highlight_mo_source_tip(
                        job_id,
                        int(mo_metrics.get("mo_observation_id") or 0),
                        extra_tip_names=extra_names,
                        display_name=mo_metrics.get("mo_source_display_name"),
                    )
                    mo_result = post_completed_tree_comment(job_id, mo_metrics)
                    metrics = dict(db_job.metrics or {})
                    metrics["mo_comment_status"] = mo_result.get("status", "failed")
                    metrics["mo_comment_updated_at"] = datetime.now(timezone.utc).isoformat()
                    if mo_result.get("mo_tree_url"):
                        metrics["mo_tree_url"] = mo_result["mo_tree_url"]
                    if mo_result.get("mo_comment_id"):
                        metrics["mo_comment_id"] = mo_result["mo_comment_id"]
                    if mo_result.get("error"):
                        metrics["mo_comment_error"] = mo_result["error"][:300]
                    if highlighted_tips:
                        metrics["mo_highlighted_tips"] = [
                            str(tip)[:300] for tip in highlighted_tips
                        ]
                    db_job.metrics = metrics
                    flag_modified(db_job, "metrics")
                    db.session.commit()
                    if mo_result.get("status") == "success":
                        publish_overview(
                            job_id,
                            "Posted the phylogenetic tree link to Mushroom Observer."
                        )
                    else:
                        publish_overview(
                            job_id,
                            "Tree built; the Mushroom Observer comment did not succeed "
                            "(the tree is still available)."
                        )
            except Exception as _mo_err:
                from app.services.log_context import log_degradation
                log_degradation(
                    logger, "mo_post_completion_failed",
                    "Tree completed but the Mushroom Observer delivery hook failed",
                    exception=type(_mo_err).__name__,
                )

            # Publish completion
            publish_job_completed(
                job_id,
                view_url=f"/job/{job_id}/view",
                result_files={
                    "tree_newick": f"/api/job/{job_id}/download/tree/newick",
                    "tree_nexus": f"/api/job/{job_id}/download/tree/nexus",
                    "fasta_original": f"/api/job/{job_id}/download/fasta/original",
                    **({"mrbayes": f"/api/job/{job_id}/download/mrbayes"}
                       if (job_dir / "tree" / "mrbayes_input.nex").is_file() else {}),
                    **({"alignment_inspection": f"/api/job/{job_id}/download/alignment/inspection"}
                       if artifact_exists(job_dir / "alignment" / "alignment_trimmed_report.html") else {}),
                }
            )
            publish_overview(job_id, "Pipeline complete! Redirecting to tree viewer...")

            return {
                "job_id": job_id,
                "status": "completed",
                "job_dir": str(job_dir),
                "message": "Job finished successfully",
                "blast_result": blast_result,
                "result_files": {
                    "alignment": str(alignment_trimmed_path),
                    "tree": str(tree_newick_path),
                    "tree_nexus": str(tree_nexus_path),
                    "metadata": str(tree_metadata_path),
                },
            }

    except Exception as e:
        # =========================================================
        # CENTRALIZED ERROR HANDLING
        # =========================================================
        error_msg = str(e)
        tb = traceback.format_exc()
        
        logger.exception(
            "event=job.failed Job failed at step=%s exception=%s",
            current_step or "unknown", type(e).__name__,
        )
        
        # A structured tool exception carries data from the failed invocation.
        # Ordinary exceptions deliberately get no process stats: substituting a
        # previous successful stage here would be worse than recording none.
        diagnostic = failure_diagnostics(e, current_tool)
        current_tool = diagnostic["tool"]
        failed_stats = diagnostic["stats"]
        stderr_tail = failed_stats.get("stderr_tail", [])
        exit_code = diagnostic["exit_code"]
        failure_kind = diagnostic["failure_kind"]
        if exit_code is not None:
            log_tool_failure(
                logger,
                current_tool or "unknown",
                exit_code,
                failed_stats,
                job=job_id,
                step=current_step or "unknown",
                failure_kind=failure_kind,
            )
        
        # Publish step failure
        if current_step:
            publish_step_failed(
                job_id,
                current_step,
                error_msg,
                tool=current_tool,
                exit_code=exit_code,
                stderr_tail=stderr_tail
            )
            update_step_meta(job, current_step, {"state": STATE_FAILED, "error": error_msg})
        
        # Publish job failure
        publish_job_failed(
            job_id,
            failed_step=current_step or "unknown",
            failed_step_label=current_step_label or "Unknown Step",
            error_summary=error_msg,
            tool=current_tool,
            exit_code=exit_code,
            stderr_tail=stderr_tail
        )
        
        # Update DB
        try:
            with _app.app_context():
                db_job = Job.query.get(job_id)
                if db_job:
                    metrics = dict(db_job.metrics or {})
                    if tree_preparation_pending:
                        metrics["mycomap_preparation_status"] = "failed"
                    metrics.update(failure_metric_updates(
                        error_msg,
                        current_step,
                        current_step_label,
                        diagnostic,
                    ))
                    # Persist the diagnostic detail too. It was already published
                    # live over SSE, but nothing stored it, so reloading the page
                    # (or opening it later) rebuilt the snapshot without any of it
                    # and the error panel showed a bare "An error occurred" with an
                    # empty output box. Keep it bounded so metrics stays small.
                    if stderr_tail:
                        metrics["stderr_tail"] = [
                            str(line)[:500] for line in list(stderr_tail)[-30:]
                        ]
                    # Last few traceback frames: the exception message alone often
                    # doesn't say where it came from.
                    tb_lines = [l for l in (tb or "").splitlines() if l.strip()]
                    if tb_lines:
                        metrics["traceback_tail"] = [l[:500] for l in tb_lines[-12:]]
                    db_job.metrics = metrics
                    db_job.status = "failed"
                    db.session.commit()
        except Exception as db_err:
            logger.error(f"Failed to update DB on error: {db_err}")
        
        # Re-raise so RQ marks job as failed
        raise

    finally:
        # Always remove the per-job handler so logs don't duplicate across jobs
        try:
            _remove_job_log_handler(file_handler)
        except Exception:
            pass
