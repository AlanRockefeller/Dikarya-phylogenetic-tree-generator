from flask import jsonify, request, send_file
from flask_login import current_user
from app.api import bp
from app.workers.queue import enqueue_job, get_job_status
from app.config import Config
from app.extensions import db
from app.models import Job
import logging
import re
from datetime import datetime


# =============================================================================
# BLAST API Endpoint
# =============================================================================

def _is_genbank_accession(text):
    """Check if text looks like a GenBank accession number."""
    # Common patterns: NC_012345, NM_001234567, AB123456, etc.
    pattern = r'^[A-Z]{1,2}_?\d{5,}(\.\d+)?$'
    return bool(re.match(pattern, text.strip(), re.IGNORECASE))


def _parse_fasta_sequences(text):
    """Parse FASTA text into list of {name, sequence} dicts."""
    sequences = []
    current_name = None
    current_seq = []
    
    for line in text.strip().split('\n'):
        line = line.strip()
        if line.startswith('>'):
            if current_name is not None:
                sequences.append({
                    'name': current_name,
                    'sequence': ''.join(current_seq)
                })
            current_name = line[1:].split()[0]  # Take first word after >
            current_seq = []
        elif line and current_name is not None:
            current_seq.append(line)
    
    # Don't forget the last sequence
    if current_name is not None:
        sequences.append({
            'name': current_name,
            'sequence': ''.join(current_seq)
        })
    
    return sequences


@bp.route('/blast', methods=['POST'])
def run_blast():
    """
    Run BLAST on a single sequence or accession.
    
    Request: { "query": "<sequence or accession>" }
    Response: { "status": "success", "sequences": [...], "message": "..." }
    """
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    
    if not query:
        return jsonify({"status": "error", "error": "No query provided"}), 400
    
    try:
        from app.services.blast_service import blast_from_sequence, blast_from_accessions
        from pathlib import Path
        
        # Determine if query is an accession or a sequence
        if _is_genbank_accession(query):
            logging.info(f"BLAST API: Detected accession: {query}")
            result = blast_from_accessions([query], Config)
        else:
            # Assume it's a raw sequence
            logging.info(f"BLAST API: Using sequence query ({len(query)} chars)")
            result = blast_from_sequence(query, Config)
        
        # Read FASTA content from the file path returned by blast service
        fasta_path = result.get('fasta_path', '')
        fasta_content = ''
        if fasta_path:
            path = Path(fasta_path)
            if path.exists():
                fasta_content = path.read_text()
        
        sequences = _parse_fasta_sequences(fasta_content)
        
        # Merge organism info from hit_details into sequences
        hit_details = result.get('hit_details', [])
        organism_map = {h['accession']: h.get('organism', '') for h in hit_details}
        
        for seq in sequences:
            # Try to match accession (with or without version)
            acc = seq['name'].split('.')[0]  # Remove version if present
            seq['organism'] = organism_map.get(acc, organism_map.get(seq['name'], ''))
        
        return jsonify({
            "status": "success",
            "sequences": sequences,
            "accessions": result.get('hit_accessions', []),
            "message": f"Found {len(sequences)} related sequences"
        })
        
    except Exception as e:
        logging.error(f"BLAST API error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


def _check_job_access(job_id):
    """
    Check if current user can access the given job.
    
    Returns:
        (db_job, error_response) - If error_response is not None, return it immediately.
    """
    db_job = Job.query.get(job_id)
    job_dir = Config.JOB_DIR / job_id
    
    # Check if job exists
    if not db_job and not job_dir.exists():
        return None, (jsonify({"status": "error", "error": "Job not found"}), 404)
    
    # If job has an owner, verify the current user is that owner
    if db_job and db_job.user_id is not None:
        if not current_user.is_authenticated:
            return None, (jsonify({"status": "error", "error": "Authentication required"}), 401)
        if current_user.id != db_job.user_id:
            return None, (jsonify({"status": "error", "error": "Access denied"}), 403)
    
    return db_job, None

@bp.route('/job', methods=['POST'])
def create_job():
    data = request.get_json() or {}
    # Basic validation or default params could go here
    job_params = {
        "input_type": data.get("input_type", "unknown"),
        "notes": data.get("notes", ""),
        "sequence": data.get("sequence", ""),
        "accessions": data.get("accessions", []),
        "alignment_method": data.get("alignment_method", "default"),
        "trimming_method": data.get("trimming_method", "none"),
        "alignment_options": data.get("alignment_options", {}),
        "tree_method": data.get("tree_method", "nj"),
        "tree_model": data.get("tree_model", "GTR+G"),
        "bootstrap": data.get("bootstrap", 1000),
        "mcmc_generations": data.get("mcmc_generations", 50000),
        # Add other params as needed
    }
    
    try:
        job_id = enqueue_job(job_params)
        
        # Create DB record
        job_record = Job(
            id=job_id,
            status="queued",
            job_dir=str(Config.JOB_DIR / job_id),
            input_type=job_params["input_type"],
            metrics={
                "tree_method": job_params["tree_method"],
                "notes": job_params["notes"],
                "alignment_method": job_params["alignment_method"],
                "trimming_method": job_params["trimming_method"]
            }
        )
        
        if current_user.is_authenticated:
            job_record.user_id = current_user.id
            
        db.session.add(job_record)
        db.session.commit()
        
        return jsonify({"status": "queued", "job_id": job_id}), 202
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@bp.route('/job/<job_id>/status', methods=['GET'])
def get_job_status_route(job_id):
    status_info = get_job_status(job_id)
    return jsonify(status_info)

@bp.route('/job/<job_id>/tree/state', methods=['GET'])
def get_tree_state(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    try:
        from app.services.tree_edit_service import load_tree_state
        state = load_tree_state(job_dir)
        return jsonify(state)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/tree/prune', methods=['POST'])
def prune_tree(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    data = request.get_json()
    tip_name = data.get("tip_name")
    
    try:
        from app.services.tree_edit_service import load_tree_state, prune_tip, save_tree_state
        state = load_tree_state(job_dir)
        state = prune_tip(state, tip_name)
        save_tree_state(job_dir, state)
        return jsonify(state)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/tree/rename', methods=['POST'])
def rename_tree_tip(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    data = request.get_json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    
    try:
        from app.services.tree_edit_service import load_tree_state, rename_tip, save_tree_state
        state = load_tree_state(job_dir)
        state = rename_tip(state, old_name, new_name)
        save_tree_state(job_dir, state)
        return jsonify(state)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/tree/reroot', methods=['POST'])
def reroot_tree_endpoint(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    data = request.get_json(silent=True) or {}
    target = data.get("root_target") or data.get("target") or data.get("node_name")

    if not target:
        return jsonify({"status": "error", "error": "Missing root_target"}), 400
    
    try:
        from app.services.tree_edit_service import load_tree_state, reroot_tree, save_tree_state
        state = load_tree_state(job_dir)
        state = reroot_tree(job_dir, state, target)
        save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/tree/midpoint_root', methods=['POST'])
def midpoint_root_endpoint(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    try:
        from app.services.tree_edit_service import load_tree_state, midpoint_root, save_tree_state
        state = load_tree_state(job_dir)
        state = midpoint_root(job_dir, state)
        save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/tree/recompute', methods=['POST'])
def recompute_tree_job(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    try:
        import json
        from app.models import JobParams, AlignmentParams, TrimmingParams, TreeBuilderParams
        from app.services.tree_edit_service import recompute_tree
        
        # Load original params
        input_info_path = job_dir / "input_info.json"
        params_dict = {}
        if input_info_path.exists():
            with open(input_info_path, "r") as f:
                params_dict = json.load(f)
        
        # Merge with request data
        req_data = request.get_json(silent=True) or {}
        params_dict.update(req_data)
        
        # Construct JobParams object
        align_params = AlignmentParams(
            method=params_dict.get("alignment_method", "default"),
            advanced_options=params_dict.get("alignment_options", {})
        )
        
        trim_params = TrimmingParams(
            method=params_dict.get("trimming_method", "none")
        )
        
        tree_params = TreeBuilderParams(
            method=params_dict.get("tree_method", "nj"),
            model=params_dict.get("tree_model", "GTR+G"),
            bootstrap=int(params_dict.get("bootstrap", 100)),
            mcmc_generations=int(params_dict.get("mcmc_generations", 50000)),
            mcmc_nruns=int(params_dict.get("mcmc_nruns", 2)),
            mcmc_nchains=int(params_dict.get("mcmc_nchains", 4))
        )
        
        job_params = JobParams(
            input_type="recompute",
            notes=params_dict.get("notes", ""),
            sequence=params_dict.get("sequence", ""),
            accessions=params_dict.get("accessions", []),
            alignment_params=align_params,
            trimming_params=trim_params,
            tree_builder_params=tree_params,
            allow_recompute=True
        )
        
        result = recompute_tree(job_dir, job_params, Config, logging.getLogger(__name__))
        return jsonify(result)
        
    except Exception as e:
        import traceback
        logging.error(f"Recompute error: {e}\n{traceback.format_exc()}")
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/download/tree/newick', methods=['GET'])
def download_newick(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    # Prefer pruned tree if available, else initialize from original
    pruned_path = job_dir / "tree" / "tree_pruned.newick"
    
    if not pruned_path.exists():
        try:
            from app.services.tree_edit_service import initialize_tree
            pruned_path = initialize_tree(job_dir)
        except Exception as e:
            logging.error(f"Failed to auto-init tree: {e}")
            # Fallback to original if initialization fails
            pruned_path = job_dir / "tree" / "tree_original.newick"
    
    path = pruned_path
    if not path.exists():
        return jsonify({"status": "error", "error": "Tree file not found"}), 404
        
    response = send_file(path, as_attachment=True, download_name="tree.newick")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@bp.route('/job/<job_id>/download/tree/newick/original', methods=['GET'])
def download_newick_original(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    path = job_dir / "tree" / "tree_original.newick"
    logging.info(f"Serving original newick from: {path}, Exists: {path.exists()}")
    if not path.exists():
        logging.error(f"File not found: {path} (BASE_DIR={Config.BASE_DIR}, JOB_DIR={Config.JOB_DIR})")
        return jsonify({"status": "error", "error": "Tree file not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="tree_original.newick")

@bp.route('/job/<job_id>/download/tree/newick/pruned', methods=['GET'])
def download_newick_pruned(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    path = job_dir / "tree" / "tree_pruned.newick"
    
    if not path.exists():
         return jsonify({"status": "error", "error": "Tree file not found"}), 404

    response = send_file(path, as_attachment=True, download_name="tree_pruned.newick")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@bp.route('/job/<job_id>/download/tree/nexus', methods=['GET'])
def download_nexus(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    pruned_path = job_dir / "tree" / "tree_pruned.nexus"
    original_path = job_dir / "tree" / "tree_original.nexus"
    
    path = pruned_path if pruned_path.exists() else original_path
    if not path.exists():
        return jsonify({"status": "error", "error": "Tree file not found"}), 404
        
    response = send_file(path, as_attachment=True, download_name="tree.nexus")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@bp.route('/job/<job_id>/download/fasta/original', methods=['GET'])
def download_fasta_original(job_id):
    # Check authorization
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    path = job_dir / "input" / "input_raw.fasta"
    if not path.exists():
        return jsonify({"status": "error", "error": "FASTA file not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_original.fasta")

@bp.route('/job/<job_id>/download/fasta/pruned', methods=['GET'])
def download_fasta_pruned(job_id):
    # Check authorization
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    path = job_dir / "alignment" / "alignment_pruned.fasta"
    if not path.exists():
        return jsonify({"status": "error", "error": "Pruned FASTA not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_pruned.fasta")

@bp.route('/job/<job_id>/download/fasta/aligned', methods=['GET'])
def download_fasta_aligned(job_id):
    """Download the aligned (but not trimmed) FASTA file."""
    # Check authorization
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    
    # Return the raw alignment (before trimming)
    path = job_dir / "alignment" / "alignment_raw.fasta"
    if not path.exists():
        path = job_dir / "alignment" / "aligned.fasta"
    
    if not path.exists():
        return jsonify({"status": "error", "error": "Aligned FASTA not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_aligned.fasta")

@bp.route('/job/<job_id>/download/fasta/trimmed', methods=['GET'])
def download_fasta_trimmed(job_id):
    """Download the trimmed FASTA file (only available if trimming was performed)."""
    # Check authorization
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    path = job_dir / "alignment" / "alignment_trimmed.fasta"
    
    if not path.exists():
        return jsonify({"status": "error", "error": "Trimmed FASTA not found (trimming may not have been performed)"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_trimmed.fasta")


# =============================================================================
# SSE Real-Time Events Endpoint
# =============================================================================

def _read_log_tail(log_path, max_bytes=65536, max_lines=200):
    """
    Read the last N lines from a log file efficiently.
    
    Reads last max_bytes of file, splits into lines, returns last max_lines.
    """
    try:
        if not log_path.exists():
            return []
        
        file_size = log_path.stat().st_size
        
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
                # Skip partial first line
                f.readline()
            
            lines = f.readlines()
        
        # Strip and return last max_lines
        return [line.rstrip() for line in lines[-max_lines:]]
    
    except Exception as e:
        logging.warning(f"Failed to read log tail from {log_path}: {e}")
        return []


def _build_snapshot(job_id: str) -> dict:
    """
    Build initial snapshot for SSE connection.
    
    Includes job status, meta, and log tails.
    """
    import time
    from datetime import datetime
    
    job_dir = Config.JOB_DIR / job_id
    
    # Get RQ job status
    from app.workers.queue import get_job_status
    rq_status = get_job_status(job_id)
    
    # Get DB job for additional info
    db_job = Job.query.get(job_id)
    
    # Determine status
    status = rq_status.get('status', 'unknown')
    if db_job and db_job.status:
        # DB status is authoritative for completed/failed
        if db_job.status in ('completed', 'failed'):
            status = db_job.status
    
    # Normalize: RQ uses 'finished', we use 'completed'
    if status == 'finished':
        status = 'completed'
    
    # Build job info
    job_info = {
        "id": job_id,
        "status": status,
        "started_at": rq_status.get('started_at'),
        "ended_at": rq_status.get('ended_at'),
        "elapsed_seconds": None,
        "error_summary": None,
        "failed_step": None,
        "failed_step_label": None,
        "tool": None,
        "exit_code": None,
        "result_files": None,
        "meta": {},
    }
    
    # Calculate elapsed time
    if rq_status.get('started_at'):
        try:
            started = datetime.fromisoformat(rq_status['started_at'].replace('Z', '+00:00'))
            if rq_status.get('ended_at'):
                ended = datetime.fromisoformat(rq_status['ended_at'].replace('Z', '+00:00'))
                job_info["elapsed_seconds"] = (ended - started).total_seconds()
            else:
                from datetime import timezone
                now = datetime.now(timezone.utc)
                job_info["elapsed_seconds"] = (now - started).total_seconds()
        except Exception:
            pass
    
    # Get RQ job meta
    try:
        from app.workers.queue import get_queue
        q = get_queue()
        rq_job = q.fetch_job(job_id)
        if rq_job and rq_job.meta:
            job_info["meta"] = rq_job.meta
    except Exception:
        pass
    
    # If job failed, extract failure info
    if status == 'failed':
        if db_job and db_job.metrics:
            job_info["error_summary"] = db_job.metrics.get('error')
            job_info["failed_step"] = db_job.metrics.get('failed_step')
        
        # Try to get more from RQ result
        result = rq_status.get('result', {})
        if isinstance(result, dict):
            job_info["error_summary"] = job_info["error_summary"] or result.get('error')
            
        # Get failed step label from meta
        failed_step = job_info.get("failed_step")
        if failed_step and "steps" in job_info["meta"]:
            step_info = job_info["meta"]["steps"].get(failed_step, {})
            job_info["failed_step_label"] = step_info.get("label", failed_step)
            job_info["tool"] = step_info.get("tool")
    
    # If job completed, include result files
    if status == 'completed':
        job_info["result_files"] = {
            "tree_newick": f"/api/job/{job_id}/download/tree/newick",
            "tree_nexus": f"/api/job/{job_id}/download/tree/nexus",
            "fasta_original": f"/api/job/{job_id}/download/fasta/original",
        }
    
    # Read log tails (generous limits to capture most output for completed jobs)
    logs_dir = job_dir / "logs"
    log_tails = {
        "pipeline": _read_log_tail(logs_dir / "pipeline.log", max_lines=500),
        "alignment": _read_log_tail(logs_dir / "alignment.log", max_lines=500),
        "tree_builder": _read_log_tail(logs_dir / "tree_builder.log", max_lines=1000),
    }
    
    return {
        "job": job_info,
        "log_tails": log_tails,
    }


@bp.route('/job/<job_id>/events', methods=['GET'])
def job_events_stream(job_id):
    """
    SSE endpoint for real-time job status updates.
    
    Protocol:
    - Emits `event: snapshot` with initial job state and log tails
    - Emits plain `data:` lines for all subsequent live events
    - Emits `event: ping` with `data: {}` every 15s as keepalive
    
    On disconnect/reconnect, server sends fresh snapshot.
    """
    import json
    import time
    import redis
    from flask import Response, stream_with_context
    
    # Check authorization
    db_job, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    
    def generate():
        # Connect to Redis for PubSub
        redis_url = Config.REDIS_URL
        r = redis.from_url(redis_url)
        pubsub = r.pubsub()
        
        channel = f"job:{job_id}:events"
        pubsub.subscribe(channel)
        
        try:
            # Send initial snapshot
            snapshot = _build_snapshot(job_id)
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            
            # Check if job is already terminal
            job_status = snapshot["job"]["status"]
            if job_status in ('completed', 'failed'):
                # Still keep connection open briefly for any final events
                pass
            
            # Throttle timers (use monotonic clock for reliable intervals)
            last_ping = time.monotonic()
            last_db_poll = 0.0  # Start at 0 to trigger immediate first poll
            
            # Tunable interval for DB polling (seconds)
            DB_POLL_INTERVAL = 1.0
            
            while True:
                # Check for PubSub messages (non-blocking with short timeout)
                # Use shorter timeout to allow responsive loop with brief sleep
                message = pubsub.get_message(timeout=0.1)
                
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                    
                    # Check if this is a terminal event
                    try:
                        event = json.loads(data)
                        if event.get('type') == 'job_state' and event.get('status') in ('completed', 'failed'):
                            # Send final event and close after brief delay
                            time.sleep(0.5)
                            break
                    except json.JSONDecodeError:
                        pass
                
                now = time.monotonic()
                
                # Send keepalive ping every 15 seconds
                if now - last_ping >= 15:
                    yield "event: ping\ndata: {}\n\n"
                    last_ping = now
                
                # Poll DB for job status at most once per DB_POLL_INTERVAL
                if job_status not in ('completed', 'failed'):
                    if now - last_db_poll >= DB_POLL_INTERVAL:
                        last_db_poll = now
                        db.session.expire_all()
                        db_job_check = Job.query.get(job_id)
                        logging.debug(f"SSE DB poll for job {job_id}: status={db_job_check.status if db_job_check else 'None'}")
                        if db_job_check and db_job_check.status in ('completed', 'failed'):
                            job_status = db_job_check.status
                            # Give a moment for final events from Redis
                            time.sleep(1)
                            break
                
                # Brief sleep to prevent CPU spin (50-100ms effective with pubsub timeout)
                time.sleep(0.05)
        
        finally:
            pubsub.unsubscribe()
            pubsub.close()
    
    response = Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # Disable nginx buffering
            'Connection': 'keep-alive',
        }
    )
    return response


# =============================================================================
# Log Download Endpoints
# =============================================================================

@bp.route('/job/<job_id>/logs/<log_name>', methods=['GET'])
def download_log(job_id, log_name):
    """
    Download job log files.
    
    Valid log_name values:
    - pipeline: pipeline.log
    - alignment: alignment.log  
    - tree_builder: tree_builder.log
    """
    # Check authorization
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    
    # Map log names to files
    log_files = {
        "pipeline": "pipeline.log",
        "alignment": "alignment.log",
        "tree_builder": "tree_builder.log",
    }
    
    if log_name not in log_files:
        return jsonify({
            "status": "error", 
            "error": f"Invalid log name. Valid options: {', '.join(log_files.keys())}"
        }), 400
    
    log_path = job_dir / "logs" / log_files[log_name]
    
    if not log_path.exists():
        return jsonify({"status": "error", "error": "Log file not found"}), 404
    
    return send_file(
        log_path,
        as_attachment=True,
        download_name=f"{job_id}_{log_files[log_name]}"
    )
