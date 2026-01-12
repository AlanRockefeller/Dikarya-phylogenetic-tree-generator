"""
Phylo job worker task.

This module contains the main RQ task for running phylogenetic analysis jobs.
It publishes real-time events via Redis PubSub for the SSE status dashboard.
"""

import copy
import json
import logging
import time
import traceback
from datetime import datetime, timezone

from rq import get_current_job

from app.config import Config
from app.workers.events import (
    STEP_INPUT, STEP_ORIENT, STEP_BLAST, STEP_ALIGN, STEP_TRIM, STEP_TREE, STEP_POST,
    STATE_QUEUED, STATE_RUNNING, STATE_DONE, STATE_SKIPPED, STATE_FAILED,
    get_initial_steps_meta,
    publish_job_running, publish_job_completed, publish_job_failed,
    publish_step_start, publish_step_done, publish_step_failed,
    publish_overview, publish_log, publish_metric,
    update_job_meta, update_step_meta,
)

# Configure logging for the worker
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_fasta_records(fasta_text: str) -> list[tuple[str, str]]:
    """
    Returns list of (header_without_gt, sequence_string_no_whitespace).
    Assumes FASTA headers start with '>'.
    """
    records: list[tuple[str, str]] = []
    header: str | None = None
    seq_chunks: list[str] = []

    for raw_line in fasta_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith(">"):
            # flush previous record
            if header is not None:
                seq = "".join(seq_chunks)
                seq = "".join(seq.split())  # remove ALL whitespace
                records.append((header, seq))

            header = line[1:].strip()
            seq_chunks = []
        else:
            seq_chunks.append(line)

    # flush final record
    if header is not None:
        seq = "".join(seq_chunks)
        seq = "".join(seq.split())
        records.append((header, seq))

    return records


def dedupe_and_uniquify_fasta(fasta_text: str) -> tuple[str, dict]:
    """
    - Drops records that are exact duplicates of a prior record (same full header AND same sequence).
    - Ensures unique record IDs (first token of header). If repeated, appends _2, _3...
    Returns: (new_fasta_text, stats dict)
    """
    records = parse_fasta_records(fasta_text)

    seen_exact: set[tuple[str, str]] = set()
    id_counts: dict[str, int] = {}

    kept: list[tuple[str, str]] = []
    dropped_exact = 0
    renamed = 0

    for header, seq in records:
        key = (header, seq)
        if key in seen_exact:
            dropped_exact += 1
            continue
        seen_exact.add(key)

        # split header into id + description
        if header.strip():
            parts = header.split(None, 1)
            rec_id = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
        else:
            rec_id = "seq"
            desc = ""
        
        # Cap header lengths to prevent abuse (e.g., 200KB pasted headers)
        MAX_SEQ_ID_LEN = 100
        MAX_DESC_LEN = 300
        rec_id = rec_id[:MAX_SEQ_ID_LEN]
        desc = desc[:MAX_DESC_LEN]

        # make ID unique
        id_counts[rec_id] = id_counts.get(rec_id, 0) + 1
        n = id_counts[rec_id]
        if n > 1:
            new_id = f"{rec_id}_{n}"
            renamed += 1
        else:
            new_id = rec_id

        new_header = f"{new_id} {desc}".rstrip()
        kept.append((new_header, seq))

    # write back out with wrapped sequence lines (optional; here: 80 cols)
    out_lines: list[str] = []
    for header, seq in kept:
        out_lines.append(f">{header}")
        for i in range(0, len(seq), 80):
            out_lines.append(seq[i:i+80])

    stats = {
        "input_records": len(records),
        "kept_records": len(kept),
        "dropped_exact_duplicates": dropped_exact,
        "renamed_due_to_duplicate_ids": renamed,
    }
    return "\n".join(out_lines) + "\n", stats


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


def _count_alignment_stats(fasta_path) -> tuple[int, int]:
    """Count sequences and alignment columns from a FASTA file."""
    try:
        with open(fasta_path, 'r') as f:
            content = f.read()
        records = parse_fasta_records(content)
        if not records:
            return 0, 0
        n_seqs = len(records)
        n_cols = len(records[0][1]) if records else 0
        return n_seqs, n_cols
    except Exception:
        return 0, 0


def run_phylo_job(job_params: dict) -> dict:
    """
    Main phylogenetic analysis job.
    
    Publishes real-time events for the SSE status dashboard.
    Uses centralized exception handling - no early returns on error.
    """
    job = get_current_job()
    job_id = job.id if job else "local_debug"
    
    # Track current step for error reporting
    current_step = None
    current_tool = None
    current_step_label = None
    
    # Stats from streaming command (for error context)
    last_stats = {}

    logger.info(f"Starting job {job_id} with params: {job_params}")

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
    with open(input_info_path, "w") as f:
        json.dump(job_params, f, indent=2)

    # 2b. Initialize with single app context
    from app import create_app
    from app.extensions import db
    from app.models import Job

    _app = create_app()
    
    # Per-job pipeline log
    log_file = job_dir / "logs" / "pipeline.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)

    input_raw_path = job_dir / "input" / "input_raw.fasta"
    blast_result = None

    try:
        # Use single app context for all DB operations
        with _app.app_context():
            # Initialize DB status
            db_job = Job.query.get(job_id)
            if db_job:
                metrics = db_job.metrics or {}
                metrics["started_at"] = datetime.now(timezone.utc).isoformat()
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
                trim_method = job_params.get("trimming_method", "none")
                if trim_method == "default":
                    trim_method = Config.BEGINNER_DEFAULT_TRIMMING
                
                # BLAST is only used for single accession with optional blast
                will_do_blast = (input_type == "accession" and blast_mode == "optional")
                if not will_do_blast:
                    job.meta["steps"][STEP_BLAST]["state"] = STATE_SKIPPED
                    job.meta["steps"][STEP_BLAST]["label"] = "BLAST Search (skipped)"
                
                # Trim is skipped if method is none
                if not trim_method or trim_method.lower() == "none":
                    job.meta["steps"][STEP_TRIM]["state"] = STATE_SKIPPED
                    job.meta["steps"][STEP_TRIM]["label"] = "Trimming (skipped)"
                
                job.save_meta()
            
            # Publish job running
            publish_job_running(job_id)
            publish_overview(job_id, "Starting pipeline...")
            
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
            update_step_meta(job, STEP_INPUT, {"state": STATE_RUNNING})
            
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
                    raise ValueError("No sequence provided for pasted_sequence input type")
                if not sequence.lstrip().startswith(">"):
                    raise ValueError("Pasted sequence must be FASTA (must start with '>')")

                # Always write what the user gave us
                input_raw_path.write_text(sequence + "\n", encoding="utf-8")
                logger.info(f"Wrote input FASTA: {input_raw_path} ({input_raw_path.stat().st_size} bytes)")

                n_records = fasta_record_count(sequence)
                logger.info(f"Input FASTA records: {n_records}")

                # Optional BLAST only if exactly 1 record
                if blast_mode == "on" and n_records != 1:
                    raise ValueError("Refusing to BLAST multi-sequence FASTA (blast_mode=on requested). Provide exactly 1 record.")

                do_blast = _should_blast_single_only(blast_mode, n_records)

            elif input_type == "fasta_upload":
                logger.info("Processing FASTA upload")

                if not input_raw_path.exists():
                    raise FileNotFoundError(f"Expected uploaded FASTA at {input_raw_path} but it does not exist")

                uploaded = input_raw_path.read_text(encoding="utf-8", errors="replace")
                n_records = fasta_record_count(uploaded)
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
                raise ValueError("Need at least 2 sequences to build an alignment/tree")

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
            
            from app.services.orientation_service import fix_sequence_orientation
            
            orient_fasta = input_raw_path.read_text(encoding="utf-8", errors="replace")
            fixed_fasta, orient_stats = fix_sequence_orientation(orient_fasta)
            input_raw_path.write_text(fixed_fasta, encoding="utf-8")
            
            reversed_count = orient_stats.get("reverse", 0)
            uncertain_count = orient_stats.get("uncertain", 0)
            
            if reversed_count > 0:
                orient_detail = f"{reversed_count} sequence(s) reverse complemented"
                if uncertain_count > 0:
                    orient_detail += f", {uncertain_count} uncertain"
            elif uncertain_count > 0:
                orient_detail = f"All forward, {uncertain_count} uncertain orientation"
            else:
                orient_detail = "All sequences correctly oriented"
            
            logger.info(
                "Orientation check: total=%s forward=%s reverse=%s uncertain=%s",
                orient_stats.get("total", 0),
                orient_stats.get("forward", 0),
                reversed_count,
                uncertain_count,
            )
            
            publish_step_done(job_id, STEP_ORIENT, orient_detail)
            update_step_meta(job, STEP_ORIENT, {"state": STATE_DONE, "detail": orient_detail})
            
            if reversed_count > 0:
                publish_metric(job_id, STEP_ORIENT, "reversed", reversed_count)

            # =========================================================
            # STEP: ALIGNMENT
            # =========================================================
            current_step = STEP_ALIGN
            align_method = job_params.get("alignment_method", "default")
            if align_method == "default":
                align_method = Config.BEGINNER_DEFAULT_ALIGNER
            
            current_tool = align_method.lower()
            current_step_label = f"Alignment ({align_method.upper()})"
            
            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = current_tool
                job.save_meta()
            
            alignment_raw_path = job_dir / "alignment" / "alignment_raw.fasta"
            alignment_trimmed_path = job_dir / "alignment" / "alignment_trimmed.fasta"
            
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

            align_params = AlignmentParams(method=align_method, advanced_options=align_opts)
            run_alignment(input_raw_path, alignment_raw_path, align_params, Config, logger, job_id=job_id)

            n_seqs, n_cols = _count_alignment_stats(alignment_raw_path)
            detail = f"{n_seqs} sequences, {n_cols} columns"
            
            publish_step_done(job_id, STEP_ALIGN, detail)
            publish_overview(job_id, f"Alignment complete: {detail}")
            update_step_meta(job, STEP_ALIGN, {"state": STATE_DONE, "detail": detail})
            publish_metric(job_id, STEP_ALIGN, "sequences", n_seqs)
            publish_metric(job_id, STEP_ALIGN, "columns", n_cols)

            # =========================================================
            # STEP: TRIMMING
            # =========================================================
            current_step = STEP_TRIM
            trim_method = job_params.get("trimming_method", "none")
            if trim_method == "default":
                trim_method = Config.BEGINNER_DEFAULT_TRIMMING
            
            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = trim_method if trim_method != "none" else None
                job.save_meta()
            
            if trim_method and trim_method.lower() != "none":
                current_tool = trim_method.lower()
                current_step_label = f"Trimming ({trim_method})"
                
                publish_step_start(job_id, STEP_TRIM, current_step_label, "", tool=current_tool)
                update_step_meta(job, STEP_TRIM, {
                    "state": STATE_RUNNING,
                    "label": current_step_label,
                    "tool": current_tool
                })

                from app.services.trimming_service import run_trimming
                run_trimming(alignment_raw_path, alignment_trimmed_path, trim_method, Config, logger, job_id=job_id)

                _, trimmed_cols = _count_alignment_stats(alignment_trimmed_path)
                detail = f"{trimmed_cols} columns retained"
                
                publish_step_done(job_id, STEP_TRIM, detail)
                publish_overview(job_id, f"Trimming complete: {detail}")
                update_step_meta(job, STEP_TRIM, {"state": STATE_DONE, "detail": detail})
            else:
                # Trimming skipped
                import shutil
                shutil.copy(alignment_raw_path, alignment_trimmed_path)
                update_step_meta(job, STEP_TRIM, {"state": STATE_SKIPPED, "label": "Trimming (skipped)"})
                publish_overview(job_id, "Trimming skipped (method: none)")
                current_tool = None
                current_step_label = "Trimming (skipped)"

            # =========================================================
            # STEP: TREE BUILDING
            # =========================================================
            current_step = STEP_TREE
            tree_method = job_params.get("tree_method", "nj")
            tree_model = job_params.get("tree_model", Config.DEFAULT_ML_MODEL)
            bootstrap = int(job_params.get("bootstrap", Config.DEFAULT_BOOTSTRAPS))
            mcmc_gens = int(job_params.get("mcmc_generations", Config.DEFAULT_MCMC_GENERATIONS))
            
            current_tool = tree_method.lower()
            current_step_label = f"Tree Building ({tree_method.upper()})"
            
            if job:
                job.meta["current_step"] = current_step
                job.meta["current_tool"] = current_tool
                job.save_meta()
            
            tree_newick_path = job_dir / "tree" / "tree_original.newick"
            tree_nexus_path = job_dir / "tree" / "tree_original.nexus"
            tree_metadata_path = job_dir / "tree" / "tree_metadata.json"
            
            detail_parts = [tree_model]
            if tree_method in ("raxml", "iqtree") and bootstrap:
                detail_parts.append(f"{bootstrap} bootstraps")
            if tree_method == "mrbayes":
                detail_parts.append(f"{mcmc_gens} generations")
            
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
                mcmc_generations=mcmc_gens,
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
            logger.info(f"Job {job_id} completed successfully.")
            
            # Update DB
            db_job = Job.query.get(job_id)
            if db_job:
                metrics = db_job.metrics or {}
                metrics["completed_at"] = datetime.now(timezone.utc).isoformat()
                db_job.metrics = metrics
                db_job.status = "completed"
                db.session.commit()
            
            # Publish completion
            publish_job_completed(
                job_id,
                view_url=f"/job/{job_id}/view",
                result_files={
                    "tree_newick": f"/api/job/{job_id}/download/tree/newick",
                    "tree_nexus": f"/api/job/{job_id}/download/tree/nexus",
                    "fasta_original": f"/api/job/{job_id}/download/fasta/original",
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
        
        logger.error(f"Job {job_id} failed at step '{current_step}': {error_msg}")
        logger.error(tb)
        
        # Get stderr tail from last command if available
        stderr_tail = last_stats.get("stderr_tail", [])
        exit_code = last_stats.get("exit_code")
        
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
                    metrics = db_job.metrics or {}
                    metrics["failed_at"] = datetime.now(timezone.utc).isoformat()
                    metrics["error"] = error_msg
                    metrics["failed_step"] = current_step
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
            logger.removeHandler(file_handler)
            file_handler.close()
        except Exception:
            pass
