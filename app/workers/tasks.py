import json
import logging
from pathlib import Path

from rq import get_current_job

from app.config import Config

# Configure logging for the worker
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def parse_fasta_records(fasta_text: str) -> list[tuple[str, str]]:
    """
    Returns list of (header_without_gt, sequence_string_no_whitespace).
    Very small parser: assumes headers start with '>'.
    """
    records: list[tuple[str, str]] = []
    header: str | None = None
    seq_chunks: list[str] = []

    for line in fasta_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(seq_chunks).replace(" ", "")))
            header = line[1:].strip()
            seq_chunks = []
        else:
            seq_chunks.append(line)

    if header is not None:
        records.append((header, "".join(seq_chunks).replace(" ", "")))

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


def run_phylo_job(job_params: dict) -> dict:
    job = get_current_job()
    job_id = job.id if job else "local_debug"

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

    # 2b. Initialize Metrics
    from app import create_app
    from app.extensions import db
    from app.models import Job
    from datetime import datetime

    _app = create_app()
    with _app.app_context():
        db_job = Job.query.get(job_id)
        if db_job:
            metrics = db_job.metrics or {}
            metrics["started_at"] = datetime.utcnow().isoformat()
            db_job.metrics = metrics
            db_job.status = "running"
            db.session.commit()

    # Per-job pipeline log
    log_file = job_dir / "logs" / "pipeline.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)

    input_raw_path = job_dir / "input" / "input_raw.fasta"

    try:
        # 3. Input Processing & (optional) BLAST
        input_type = _normalize_input_type(job_params.get("input_type"))
        blast_mode = _normalize_blast_mode(job_params.get("blast_mode"))
        blast_result = None

        if input_type == "accession_list":
            accessions = job_params.get("accessions", []) or []
            logger.info(f"Processing accession list: {accessions}")

            if len(accessions) != 1:
                raise ValueError("accession_list jobs must contain exactly one accession (refusing to BLAST multiple).")

            # Under your policy, accession_list means “BLAST this one accession”
            # (and then proceed with alignment/tree on the returned dataset).
            if blast_mode == "off":
                raise ValueError("BLAST is disabled (blast_mode=off), but accession_list requires BLAST to fetch sequences.")

            from app.services.blast_service import blast_from_accessions
            blast_result = blast_from_accessions(accessions, Config, logger)

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

            if do_blast:
                logger.info("BLAST enabled (single-sequence input)")
                from app.services.blast_service import blast_from_sequence
                blast_result = blast_from_sequence(sequence, Config, logger)
            else:
                logger.info("Skipping BLAST (multi-sequence input or blast_mode=off)")

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

            if do_blast:
                logger.info("BLAST enabled (single-sequence FASTA upload)")
                from app.services.blast_service import blast_from_sequence
                blast_result = blast_from_sequence(uploaded, Config, logger)
            else:
                logger.info("Skipping BLAST for FASTA upload (multi-sequence input or blast_mode=off)")

        else:
            raise ValueError(f"Unknown input type: {input_type}")

        # If BLAST ran and produced an expanded dataset, save it and (optionally) replace input_raw.fasta
        if blast_result:
            with open(job_dir / "blast" / "blast_results.json", "w") as f:
                json.dump(blast_result, f, indent=2)

            fasta_path = blast_result.get("fasta_path")
            if fasta_path:
                # Keep a copy of BLAST sequences
                with open(fasta_path, "r") as src, open(job_dir / "blast" / "blast_sequences.fasta", "w") as dst:
                    dst.write(src.read())

                # For the phylogeny pipeline: align the BLAST-expanded dataset
                with open(fasta_path, "r") as src:
                    input_raw_path.write_text(src.read(), encoding="utf-8")
                logger.info(f"Replaced input FASTA with BLAST-expanded dataset from {fasta_path}")

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

        # 4. Alignment
        logger.info("Starting Alignment...")
        alignment_raw_path = job_dir / "alignment" / "alignment_raw.fasta"
        alignment_trimmed_path = job_dir / "alignment" / "alignment_trimmed.fasta"

        try:
            align_method = job_params.get("alignment_method", "default")
            align_opts = job_params.get("alignment_options", {})

            from app.models import AlignmentParams
            from app.services.alignment_service import run_alignment

            align_params = AlignmentParams(method=align_method, advanced_options=align_opts)
            run_alignment(input_raw_path, alignment_raw_path, align_params, Config, logger)

        except Exception as e:
            logger.error(f"Alignment failed: {e}")
            return {"job_id": job_id, "status": "error", "error": f"Alignment failed: {str(e)}"}

        # 5. Trimming
        logger.info("Starting Trimming...")
        try:
            trim_method = job_params.get("trimming_method", "none")
            if trim_method == "default":
                trim_method = Config.BEGINNER_DEFAULT_TRIMMING

            from app.services.trimming_service import run_trimming
            run_trimming(alignment_raw_path, alignment_trimmed_path, trim_method, Config, logger)

        except Exception as e:
            logger.error(f"Trimming failed: {e}")
            return {"job_id": job_id, "status": "error", "error": f"Trimming failed: {str(e)}"}

        # 6. Tree Building
        logger.info("Starting Tree Building...")
        tree_newick_path = job_dir / "tree" / "tree_original.newick"
        tree_nexus_path = job_dir / "tree" / "tree_original.nexus"
        tree_metadata_path = job_dir / "tree" / "tree_metadata.json"

        try:
            tree_method = job_params.get("tree_method", "nj")
            tree_model = job_params.get("tree_model", Config.DEFAULT_ML_MODEL)
            bootstrap = int(job_params.get("bootstrap", Config.DEFAULT_BOOTSTRAPS))
            mcmc_gens = int(job_params.get("mcmc_generations", Config.DEFAULT_MCMC_GENERATIONS))

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
            )

            with open(tree_metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

            from app.services.tree_edit_service import load_tree_state
            load_tree_state(job_dir)

        except Exception as e:
            logger.error(f"Tree building failed: {e}")
            return {"job_id": job_id, "status": "error", "error": f"Tree building failed: {str(e)}"}

        logger.info(f"Job {job_id} completed successfully.")

        # Mark completion in DB
        with _app.app_context():
            db_job = Job.query.get(job_id)
            if db_job:
                metrics = db_job.metrics or {}
                metrics["completed_at"] = datetime.utcnow().isoformat()
                db_job.metrics = metrics
                db_job.status = "completed"
                db.session.commit()

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
        logger.error(f"Input processing/BLAST failed: {e}")
        return {"job_id": job_id, "status": "error", "error": str(e)}

    finally:
        # Always remove the per-job handler so logs don't duplicate across jobs
        try:
            logger.removeHandler(file_handler)
            file_handler.close()
        except Exception:
            pass
