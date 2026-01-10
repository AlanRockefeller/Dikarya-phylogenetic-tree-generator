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

from app.services.security_utils import validate_job_id, validate_blast_query

logger = logging.getLogger(__name__)


# =============================================================================
# BLAST API Endpoint
# =============================================================================

def _is_genbank_accession(text):
    """Check if text looks like a GenBank accession number."""
    # Common patterns: NC_012345, NM_001234567, AB123456, etc.
    pattern = r'^[A-Z]{1,2}_?\d{5,}(\.\d+)?$'
    return bool(re.match(pattern, text.strip(), re.IGNORECASE))


def _parse_fasta_sequences(text):
    """Parse FASTA text into list of {name, sequence} dicts.
    
    Preserves the full header (after >) for display in tree tips.
    """
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
            # Keep the full header (everything after >) for tree tip labels
            current_name = line[1:].strip()
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
    
    is_valid_query, error_msg = validate_blast_query(query)
    if not is_valid_query:
         return jsonify({"status": "error", "error": f"Invalid query: {error_msg}"}), 400
    
    try:
        from app.services.blast_service import blast_from_sequence, blast_from_accessions
        from pathlib import Path
        
        # Determine if query is an accession or a sequence
        if _is_genbank_accession(query):
            logger.info(f"BLAST API: Detected accession: {query}")
            result = blast_from_accessions([query], Config)
        else:
            # Assume it's a raw sequence
            logger.info(f"BLAST API: Using sequence query ({len(query)} chars)")
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
            # Extract accession from the full name (first word, without version)
            # Full name could be "AY702745.1 Mycena amicta..." - we need "AY702745" for lookup
            first_word = seq['name'].split()[0] if seq['name'] else ''
            acc_no_version = first_word.split('.')[0]
            seq['organism'] = organism_map.get(acc_no_version, organism_map.get(first_word, ''))
        
        return jsonify({
            "status": "success",
            "sequences": sequences,
            "accessions": result.get('hit_accessions', []),
            "message": f"Found {len(sequences)} related sequences"
        })
        
    except Exception as e:
        logger.error(f"BLAST API error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


@bp.route('/mycomap', methods=['POST'])
def fetch_mycomap():
    """
    Fetch sequences from a Mycomap BLAST results URL.
    
    Request: { 
        "url": "<mycomap URL>", 
        "include_ncbi": true,
        "include_local": true 
    }
    Response: { "status": "success", "sequences": [...], "message": "..." }
    """
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    include_ncbi = data.get('include_ncbi', True)
    include_local = data.get('include_local', True)
    
    if not url:
        return jsonify({"status": "error", "error": "No URL provided"}), 400
    
    # Validate at least one checkbox is selected
    if not include_ncbi and not include_local:
        return jsonify({
            "status": "error", 
            "error": "Select at least one result type (NCBI or Local)"
        }), 400
    
    try:
        from app.services.mycomap_service import validate_mycomap_url, fetch_mycomap_fasta
        
        # Validate and extract blast_id
        blast_id = validate_mycomap_url(url)
        if not blast_id:
            return jsonify({
                "status": "error",
                "error": "Invalid Mycomap URL. URL must be from mycomap.com and contain a result ID (e.g., r12345)"
            }), 400
        
        logger.info(f"Mycomap API: Fetching sequences for blast_id={blast_id} (ncbi={include_ncbi}, local={include_local})")
        
        # Fetch FASTA from Mycomap
        result = fetch_mycomap_fasta(blast_id, include_ncbi, include_local)
        
        # Check for errors
        if result['errors'] and not result['fasta_content']:
            return jsonify({
                "status": "error",
                "error": "; ".join(result['errors'])
            }), 502
        
        # Parse FASTA into sequences
        sequences = _parse_fasta_sequences(result['fasta_content'])
        
        # Build success message
        parts = []
        if include_ncbi:
            parts.append(f"{result['ncbi_count']} NCBI")
        if include_local:
            parts.append(f"{result['local_count']} local")
        msg = f"Fetched {' + '.join(parts)} sequences from Mycomap"
        
        # Include warnings if there were non-fatal errors
        if result['errors']:
            msg += f" (warnings: {'; '.join(result['errors'])})"
        
        return jsonify({
            "status": "success",
            "sequences": sequences,
            "ncbi_count": result['ncbi_count'],
            "local_count": result['local_count'],
            "message": msg
        })
        
    except Exception as e:
        logger.error(f"Mycomap API error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


@bp.route('/inaturalist', methods=['POST'])
def fetch_inaturalist():
    """
    Fetch DNA sequences from iNaturalist observations.
    
    Request: { 
        "url": "<iNaturalist URL>",
        "action": "analyze" | "fetch_sequences"
    }
    
    Response (analyze): {
        "status": "success",
        "total_observations": int,
        "dna_count": int,
        "provisional_species": [{"name": "...", "count": int}, ...],
        "is_single": bool,
        "can_blast": bool
    }
    
    Response (fetch_sequences): {
        "status": "success", 
        "sequences": [...],
        "message": "..."
    }
    """
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    action = data.get('action', 'analyze').strip()
    
    # Input sanitization
    if not url:
        return jsonify({"status": "error", "error": "No URL provided"}), 400
    
    # Limit URL length to prevent abuse
    if len(url) > 2000:
        return jsonify({"status": "error", "error": "URL too long"}), 400
    
    # Validate action
    if action not in ('analyze', 'fetch_sequences'):
        return jsonify({"status": "error", "error": "Invalid action"}), 400
    
    try:
        from app.services.inaturalist_service import (
            validate_inaturalist_url,
            fetch_inaturalist_data
        )
        
        # Validate URL format first
        url_info = validate_inaturalist_url(url)
        if not url_info:
            return jsonify({
                "status": "error",
                "error": "Invalid iNaturalist URL. Please enter a valid observation URL "
                         "(e.g., https://www.inaturalist.org/observations/12345) or "
                         "observations search URL."
            }), 400
        
        logger.info(f"iNaturalist API: Processing URL type={url_info['type']}, action={action}")
        
        if action == 'analyze':
            # Fetch and analyze observations (default mode='all' gets both ITS and PSN stats)
            result = fetch_inaturalist_data(url, mode='all')

            # Return analysis without full sequences
            # Defensive: check sequences list exists and has items before accessing
            sequences = result.get('sequences', [])
            seq = sequences[0]['sequence'] if sequences else None
            
            # Determine if this is a single result based on actual ITS data
            # Use total_its_observations if available, falling back to total_observations
            total_its = result.get('total_its_observations', result['total_observations'])
            
            # is_single reflects the URL type (single observation URL)
            is_single_url = result.get('is_single', False)
            
            # can_blast requires exactly one usable ITS sequence found
            can_blast = bool(total_its == 1 and result['dna_count'] == 1 and len(sequences) == 1 and seq)
            
            return jsonify({
                "status": "success",
                "total_observations": result['total_observations'],
                "total_its_observations": result.get('total_its_observations', 0),
                "total_psn_observations": result.get('total_psn_observations', 0),
                "fetched_its_count": result.get('fetched_its_count', 0),
                "fetched_psn_count": result.get('fetched_psn_count', 0),
                "dna_count": result['dna_count'],
                "provisional_species": result['provisional_species'],
                "is_single": is_single_url,
                "can_blast": can_blast,
                # For single obs with DNA, include the sequence for BLAST
                "sequence": seq if can_blast else None,
                "truncated": result.get('truncated', False),
                "total_available": result.get('total_available', 0),
                "message": f"Found {result['dna_count']} observation(s) with DNA Barcode ITS"
            })
        else:
            # Optimize: Only fetch ITS sequences for queue adding (skip PSN stats)
            result = fetch_inaturalist_data(url, mode='its_only')
            
            # Return full sequences for queue
            return jsonify({
                "status": "success",
                "sequences": result['sequences'],
                "truncated": result.get('truncated', False),
                "total_available": result.get('total_available', 0),
                "message": f"Fetched {len(result['sequences'])} DNA sequences from iNaturalist"
            })
        
    except ValueError as e:
        logger.warning(f"iNaturalist API validation error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        logger.error(f"iNaturalist API error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


def _check_job_access(job_id):
    """
    Check if current user can access the given job.
    
    Returns:
        (db_job, error_response) - If error_response is not None, return it immediately.
    """
    if not validate_job_id(job_id):
        return None, (jsonify({"status": "error", "error": "Invalid job ID format"}), 400)

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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400
        
    status_info = get_job_status(job_id)
    return jsonify(status_info)

@bp.route('/job/<job_id>/tree/state', methods=['GET'])
def get_tree_state(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400
        
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response

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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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

@bp.route('/job/<job_id>/tree/midpoint_root_toggle', methods=['POST'])
def midpoint_root_toggle_endpoint(job_id):
    """Toggle midpoint rooting on/off."""
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    try:
        from app.services.tree_edit_service import (
            load_tree_state, midpoint_root, undo_midpoint_root, save_tree_state
        )
        state = load_tree_state(job_dir)
        
        # Check current state and toggle
        if state.get("is_midpoint_rooted", False):
            # Currently midpoint rooted - undo it
            state = undo_midpoint_root(job_dir, state)
        else:
            # Not midpoint rooted - apply it
            state = midpoint_root(job_dir, state)
        
        save_tree_state(job_dir, state)
        return jsonify(state)
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/tree/recompute', methods=['POST'])
def recompute_tree_job(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
        
        result = recompute_tree(job_dir, job_params, Config, logger)
        return jsonify(result)
        
    except Exception as e:
        import traceback
        logger.error(f"Recompute error: {e}\n{traceback.format_exc()}")
        return jsonify({"status": "error", "error": str(e)}), 500

@bp.route('/job/<job_id>/download/tree/newick', methods=['GET'])
def download_newick(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
            logger.error(f"Failed to auto-init tree: {e}")
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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    path = job_dir / "tree" / "tree_original.newick"
    logger.info(f"Serving original newick from: {path}, Exists: {path.exists()}")
    if not path.exists():
        logger.error(f"File not found: {path} (BASE_DIR={Config.BASE_DIR}, JOB_DIR={Config.JOB_DIR})")
        return jsonify({"status": "error", "error": "Tree file not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="tree_original.newick")

@bp.route('/job/<job_id>/download/tree/newick/pruned', methods=['GET'])
def download_newick_pruned(job_id):
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
        logger.warning(f"Failed to read log tail from {log_path}: {e}")
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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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
                        logger.debug(f"SSE DB poll for job {job_id}: status={db_job_check.status if db_job_check else 'None'}")
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
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "error": "Invalid job ID format"}), 400

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

@bp.route('/log/client', methods=['POST'])
def log_client_error():
    """
    Log client-side errors to the server log.
    Expected JSON: { "message": "...", "stack": "...", "url": "...", "context": "..." }
    """
    from flask import current_app
    
    data = request.get_json(silent=True) or {}
    # Apply size limits to prevent log spam
    msg = (data.get("message") or "Unknown client error")[:2000]
    stack = (data.get("stack") or "")[:20000]
    context = (data.get("context") or "")[:1000]
    context = (data.get("context") or "")[:1000]
    # Sanitize URL to prevent log injection
    url = (data.get("url") or "")[:500].replace('\n', '').replace('\r', '')
    
    # Request metadata for debugging
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    user_agent = (request.headers.get("User-Agent") or "")[:200]
    
    # Format log message with metadata
    log_msg = f"Client Error: {msg}"
    log_msg += f" [IP: {client_ip}]"
    if url:
        log_msg += f" [URL: {url}]"
    if context:
        log_msg += f" [Context: {context}]"
    if user_agent:
        log_msg += f" [UA: {user_agent}]"
    if stack:
        log_msg += f"\nStack: {stack}"
        
    current_app.logger.error(log_msg)
    return jsonify({"status": "logged"}), 200

@bp.route('/job/<job_id>/sequences/add', methods=['POST'])
def add_sequences_to_job(job_id):
    """
    Add sequences to an existing job's input file.
    
    Request: { "input": "<fasta or accession list>" }
    Response: { "status": "success", "count": int, "message": "..." }
    """
    # Check authorization
    _, error_response = _check_job_access(job_id)
    if error_response:
        return error_response
    
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    data = request.get_json() or {}
    input_text = data.get("input", "").strip()
    
    if not input_text:
        return jsonify({"status": "error", "error": "No input provided"}), 400
        
    try:
        from app.services.blast_service import fetch_fasta_for_accessions
        
        sequences_to_add = []
        is_accession_input = False
        
        # Heuristic: Check if input looks like a list of accessions (no > at start, short lines)
        first_line = input_text.splitlines()[0].strip()
        if not first_line.startswith(">"):
            # Assume accessions
            is_accession_input = True
            
            # Split by commas, spaces, newlines
            import re
            tokens = re.split(r'[,\s]+', input_text)
            accessions = [t.strip() for t in tokens if t.strip()]
            
            if accessions:
                logger.info(f"Adding sequences from accessions: {accessions}")
                fasta_content = fetch_fasta_for_accessions(accessions)
                sequences_to_add = _parse_fasta_sequences(fasta_content)
        else:
            # Assume FASTA
            sequences_to_add = _parse_fasta_sequences(input_text)
            
        if not sequences_to_add:
             return jsonify({"status": "error", "error": "No valid sequences found in input"}), 400
             
        # Append to input_raw.fasta
        input_path = job_dir / "input" / "input_raw.fasta"
        
        # Read existing to check for duplicates (by name)
        existing_names = set()
        if input_path.exists():
            existing_seqs = _parse_fasta_sequences(input_path.read_text())
            existing_names = {s['name'] for s in existing_seqs}
            
        added_count = 0
        with open(input_path, "a") as f:
            # Ensure newline at end of file before appending
            if input_path.stat().st_size > 0:
                 f.write("\n")
                 
            for seq in sequences_to_add:
                # Limit name length if needed, but for now allow typical
                # Basic duplicate check - logic can be refined
                if seq['name'] not in existing_names:
                    f.write(f">{seq['name']}\n{seq['sequence']}\n")
                    added_count += 1
                else:
                    # Optional: Rename duplicate? For now, skip or append as is?
                    # Let's append with suffix to allow user to see it
                    # But actually keying is by name, so maybe skip.
                    # User asked to "Add", so let's add. But strictly unique IDs needed for tree?
                    # Unique IDs are enforced by tree service during recompute/init.
                    # Best to append.
                    f.write(f">{seq['name']}_added\n{seq['sequence']}\n")
                    added_count += 1
                    
        return jsonify({
            "status": "success", 
            "count": added_count,
            "message": f"Added {added_count} sequences."
        })
        
    except Exception as e:
        logger.error(f"Failed to add sequences: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500
