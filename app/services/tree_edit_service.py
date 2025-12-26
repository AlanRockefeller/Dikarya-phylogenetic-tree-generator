import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.config import Config
from app.models import JobParams, AlignmentParams, TrimmingParams, TreeBuilderParams
from app.services.subprocess_utils import run_command

# Try to import BioPython
try:
    from Bio import Phylo, AlignIO, SeqIO
    from io import StringIO
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)

def load_tree_state(job_dir: Path) -> Dict:
    """
    Load tree_state.json if available.
    If not present, parse tree_original.newick or tree_original.nexus
    and build the initial tree state JSON.
    """
    state_path = job_dir / "tree_state.json"
    if state_path.exists():
        with open(state_path, "r") as f:
            return json.load(f)
            
    # Initialize from original tree
    newick_path = job_dir / "tree" / "tree_original.newick"
    if newick_path.exists():
        tree_json = parse_newick_to_json(newick_path)
        # Add metadata wrapper
        state = {
            "current_tree": "original",
            "tree_structure": tree_json,
            "pruned_taxa": [],
            "renames": {},
            "root": None # Default
        }
        save_tree_state(job_dir, state)
        return state
        
    return {}

def save_tree_state(job_dir: Path, tree_json: Dict) -> None:
    """
    Save tree_json to tree_state.json with indentation.
    """
    state_path = job_dir / "tree_state.json"
    with open(state_path, "w") as f:
        json.dump(tree_json, f, indent=2)

def prune_tip(tree_json: Dict, tip_name: str) -> Dict:
    """
    Remove a tip (or subtree if internal node selected).
    Return modified tree_json.
    """
    # We mark it as pruned in the list, and also flag the node in the structure
    if "pruned_taxa" not in tree_json:
        tree_json["pruned_taxa"] = []
        
    if tip_name not in tree_json["pruned_taxa"]:
        tree_json["pruned_taxa"].append(tip_name)
        
    # Helper to traverse and mark
    def mark_pruned(node):
        if node.get("name") == tip_name or node.get("original_name") == tip_name:
            node["pruned"] = True
            return True
        
        if "children" in node:
            for child in node["children"]:
                if mark_pruned(child):
                    # If a child is pruned, do we prune the parent? 
                    # Usually no, unless all children are pruned.
                    # For now, just mark the specific target.
                    pass
        return False

    if "tree_structure" in tree_json:
        mark_pruned(tree_json["tree_structure"])
        
    return tree_json

def rename_tip(tree_json: Dict, old_name: str, new_name: str) -> Dict:
    """
    Change display_name of a tip. Preserve original_name.
    """
    if "renames" not in tree_json:
        tree_json["renames"] = {}
        
    tree_json["renames"][old_name] = new_name
    
    def apply_rename(node):
        if node.get("name") == old_name or node.get("original_name") == old_name:
            node["display_name"] = new_name
            # We keep 'name' as identifier usually, but display_name for UI
            return True
        if "children" in node:
            for child in node["children"]:
                apply_rename(child)
        return False

    if "tree_structure" in tree_json:
        apply_rename(tree_json["tree_structure"])
        
    return tree_json

def reroot_tree(tree_json: Dict, root_target: str) -> Dict:
    """
    Reroot using any valid node (tip or internal). 
    Update tree_json's root.
    """
    # For Part 7, we might just store the root target in metadata
    # Actual rerooting requires tree manipulation logic (BioPython)
    # If we have BioPython, we can try to reroot the structure.
    # But re-serializing to JSON structure is complex.
    # For now, let's store the intent.
    tree_json["root"] = root_target
    return tree_json

def extract_pruned_fasta(original_fasta: Path, tree_json: Dict, output_fasta: Path) -> None:
    """
    Write a new FASTA file containing only sequences corresponding to non-pruned tips.
    Use original_name to map FASTA headers.
    """
    pruned_taxa = set(tree_json.get("pruned_taxa", []))
    
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython required for FASTA manipulation")
        
    sequences = []
    with open(original_fasta, "r") as f:
        for record in SeqIO.parse(f, "fasta"):
            if record.id not in pruned_taxa:
                sequences.append(record)
                
    with open(output_fasta, "w") as f:
        SeqIO.write(sequences, f, "fasta")

def recompute_tree(job_dir: Path, job_params: JobParams, config: Config, logger) -> Dict[str, Any]:
    """
    Re-run the alignment, trimming, and tree inference pipeline 
    using only sequences present in the pruned tree.
    """
    logger.info("Starting tree recomputation...")
    
    # 1. Load state
    tree_json = load_tree_state(job_dir)
    
    # 2. Extract pruned FASTA
    # We need the original input (unaligned)
    input_raw = job_dir / "input" / "input_raw.fasta"
    alignment_pruned_path = job_dir / "alignment" / "alignment_pruned.fasta" # Unaligned input for re-run
    
    extract_pruned_fasta(input_raw, tree_json, alignment_pruned_path)
    
    # 3. Re-align
    # We need alignment params. Assuming they are in job_params or we use defaults.
    # If job_params passed to this function has them, great.
    # But wait, job_params usually comes from the initial job submission.
    # The recompute endpoint might pass updated params? 
    # For now, let's reuse original params or defaults.
    
    align_params = job_params.alignment_params or AlignmentParams(method="default")
    alignment_pruned_aligned_path = job_dir / "alignment" / "alignment_pruned_aligned.fasta" # Result of alignment
    
    from app.services.alignment_service import run_alignment
    run_alignment(alignment_pruned_path, alignment_pruned_aligned_path, align_params, config, logger)
    
    # 4. Re-trim
    trim_params = job_params.trimming_params or TrimmingParams(method="none")
    alignment_pruned_trimmed_path = job_dir / "alignment" / "alignment_pruned_trimmed.fasta"
    
    from app.services.trimming_service import run_trimming
    run_trimming(alignment_pruned_aligned_path, alignment_pruned_trimmed_path, trim_params.method, config, logger)
    
    # 5. Re-build Tree
    tree_params = job_params.tree_builder_params or TreeBuilderParams(method="nj")
    tree_pruned_newick = job_dir / "tree" / "tree_pruned.newick"
    tree_pruned_nexus = job_dir / "tree" / "tree_pruned.nexus"
    
    from app.services.tree_builder_service import run_tree_builder
    metadata = run_tree_builder(
        alignment_pruned_trimmed_path,
        tree_pruned_newick,
        tree_pruned_nexus,
        tree_params,
        config,
        logger
    )
    
    # 6. Update State
    tree_json["current_tree"] = "pruned"
    # We might want to update the tree structure in JSON too?
    # Yes, parse the new tree
    new_structure = parse_newick_to_json(tree_pruned_newick)
    tree_json["tree_structure"] = new_structure
    save_tree_state(job_dir, tree_json)
    
    # Save metadata
    with open(job_dir / "tree" / "tree_pruned_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
        
    return {
        "status": "completed",
        "newick": str(tree_pruned_newick),
        "nexus": str(tree_pruned_nexus),
        "metadata": metadata
    }

def parse_newick_to_json(path: Path) -> Dict:
    """
    Convert a Newick tree to our JSON format.
    """
    if not HAS_BIOPYTHON:
        return {"error": "BioPython missing"}
        
    try:
        tree = Phylo.read(str(path), "newick")
        return _clade_to_json(tree.root)
    except Exception as e:
        logger.error(f"Failed to parse Newick: {e}")
        return {}

def _clade_to_json(clade):
    node = {
        "name": clade.name,
        "original_name": clade.name,
        "branch_length": clade.branch_length,
        "confidence": clade.confidence
    }
    if clade.clades:
        node["children"] = [_clade_to_json(c) for c in clade.clades]
    return node

def json_to_newick(tree_json: Dict) -> str:
    """
    Serialize JSON tree back to Newick.
    """
    # If tree_json has "tree_structure", use that
    node = tree_json.get("tree_structure", tree_json)
    
    def _node_to_newick(n):
        name = n.get("name", "")
        # Use display_name if available and different? 
        # Usually Newick uses the identifier. 
        # If we renamed, we might want to output the new name?
        # The prompt says "Change display_name of a tip. Preserve original_name."
        # But for recomputation, we might need the original names if they match the FASTA.
        # However, if we pruned, we are generating a new tree.
        # Let's stick to 'name' which should be the identifier.
        
        length = n.get("branch_length")
        length_str = f":{length}" if length is not None else ""
        
        if "children" in n and n["children"]:
            children_str = ",".join([_node_to_newick(c) for c in n["children"]])
            return f"({children_str}){name}{length_str}"
        else:
            return f"{name}{length_str}"

    return _node_to_newick(node) + ";"
