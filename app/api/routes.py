from flask import jsonify, request, send_file
from flask_login import current_user
from app.api import bp
from app.workers.queue import enqueue_job, get_job_status
from app.config import Config
from app.extensions import db
from app.models import Job
import logging

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
            input_type=job_params["input_type"]
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
        
    data = request.get_json()
    target = data.get("target")
    
    try:
        from app.services.tree_edit_service import load_tree_state, reroot_tree, save_tree_state
        state = load_tree_state(job_dir)
        state = reroot_tree(state, target)
        save_tree_state(job_dir, state)
        return jsonify(state)
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
        
    # Prefer pruned tree if available, else original
    pruned_path = job_dir / "tree" / "tree_pruned.newick"
    original_path = job_dir / "tree" / "tree_original.newick"
    
    path = pruned_path if pruned_path.exists() else original_path
    if not path.exists():
        return jsonify({"status": "error", "error": "Tree file not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="tree.newick")

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
        
    return send_file(path, as_attachment=True, download_name="tree.nexus")

@bp.route('/job/<job_id>/download/fasta/original', methods=['GET'])
def download_fasta_original(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    path = job_dir / "input" / "input_raw.fasta"
    if not path.exists():
        return jsonify({"status": "error", "error": "FASTA file not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_original.fasta")

@bp.route('/job/<job_id>/download/fasta/pruned', methods=['GET'])
def download_fasta_pruned(job_id):
    job_dir = Config.JOB_DIR / job_id
    if not job_dir.exists():
        return jsonify({"status": "error", "error": "Job not found"}), 404
        
    path = job_dir / "alignment" / "alignment_pruned.fasta"
    if not path.exists():
        return jsonify({"status": "error", "error": "Pruned FASTA not found"}), 404
        
    return send_file(path, as_attachment=True, download_name="sequences_pruned.fasta")
