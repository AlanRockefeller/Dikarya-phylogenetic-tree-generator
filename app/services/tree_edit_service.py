import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Set
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
        
        # Default policy: Midpoint root the tree
        try:
            # Store original tree before midpoint rooting for toggle functionality
            original_newick_path = job_dir / "tree" / "tree_original.newick"
            if original_newick_path.exists():
                with open(original_newick_path, "r") as f:
                    state["pre_midpoint_newick"] = f.read().strip()
            
            # We must pass the state as we just created it
            state = midpoint_root(job_dir, state)
            state["is_midpoint_rooted"] = True
            logging.info("Applied default midpoint rooting.")
        except Exception as e:
            state["is_midpoint_rooted"] = False
            logging.warning(f"Default midpoint rooting skipped/failed: {e}")

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

def prune_taxa(job_dir: Path, tree_json: Dict, taxa_names: List[str]) -> Dict:
    """
    Remove one or more tips (or subtrees if internal nodes selected).
    Physically updates tree_pruned.newick and returns modified tree_json.
    """
    if not taxa_names:
        return tree_json

    if not HAS_BIOPYTHON:
        logger.error("BioPython not installed; cannot prune tree")
        raise RuntimeError("BioPython not installed; cannot prune tree")

    # Physical Pruning logic
    # Determine input path
    pruned_newick = job_dir / "tree" / "tree_pruned.newick"
    original_newick = job_dir / "tree" / "tree_original.newick"
    
    input_path = pruned_newick if pruned_newick.exists() else original_newick
    if not input_path.exists():
        raise FileNotFoundError("No tree file found to prune")

    try:
        tree = Phylo.read(str(input_path), "newick")
        
        # We need to prune multiple nodes. 
        targets = set(taxa_names)
        
        to_prune = []
        for clade in tree.find_clades():
            if clade.name in targets:
                to_prune.append(clade)
                
        if not to_prune:
             # Gather some available names for debugging
             available_tips = [t.name for t in tree.get_terminals() if t.name][:5]
             available_internal = [n.name for n in tree.get_nonterminals() if n.name][:5]
             msg = f"Targets {list(targets)[:3]}... not found in tree. " \
                   f"Sample tips: {available_tips}. Sample nodes: {available_internal}."
             logger.warning(msg)
             # Raise error to inform user in UI instead of silent failure
             raise ValueError(msg)
        
        # Only update metadata AFTER confirming targets exist in tree
        if "pruned_taxa" not in tree_json:
            tree_json["pruned_taxa"] = []
        
        for name in taxa_names:
            if name not in tree_json["pruned_taxa"]:
                tree_json["pruned_taxa"].append(name)
             
        # Map parents for manual removal
        parents = {c: p for p in tree.find_clades() for c in p.clades}
        
        
        # Iterative Pruning with Cleanup
        # 1. Start with explicit targets
        queue = to_prune[:]
        processed = set()
        
        while queue:
            current = queue.pop(0)
            if current in processed:
                continue
            processed.add(current)
            
            parent = parents.get(current)
            if not parent:
                # Root - cannot prune? Or if distinct root node, maybe?
                # Usually we don't prune root unless tree is empty
                continue
                
            # Remove from parent
            if current in parent.clades:
                parent.clades.remove(current)
                
            # Check status of parent
            # If parent now has 0 children, it has become a tip.
            # If parent was an original internal node (had children initially),
            # it is now an "artifact" tip. We should remove it too.
            # Exception: If parent is the Root, we might leave it or empty the tree.
            if len(parent.clades) == 0:
                 # It's empty. Is it the root?
                 if parent == tree.root:
                     # Attempt to leave empty root or handle gracefully
                     pass
                 else:
                     # Recursively prune this parent
                     queue.append(parent)
            
        # Post-pass: Collapse unifurcations (single-child internal nodes)
        # After pruning, internal nodes can end up with exactly one child,
        # creating visually odd "pass-through" nodes. We splice them out.
        _collapse_unifurcations(tree)

        # Save
        valid_path = job_dir / "tree"
        valid_path.mkdir(parents=True, exist_ok=True)
        Phylo.write(tree, str(valid_path / "tree_pruned.newick"), "newick")
        
        # Update JSON structure to match
        new_structure = _clade_to_json(tree.root)
        tree_json["tree_structure"] = new_structure
        
        renames = tree_json.get("renames", {})
        apply_state_to_structure(new_structure, renames, set()) 
        
        return tree_json
        
    except Exception as e:
        logger.error(f"Prune failed: {e}")
        raise

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

def apply_state_to_structure(node: Dict, renames: Dict, pruned_taxa: Set[str]):
    """
    Recursively apply metadata (renames, prune status) to the tree structure.
    """
    original_name = node.get("original_name")
    if original_name:
        # Apply Rename
        if original_name in renames:
            node["display_name"] = renames[original_name]
        
        # Apply Prune
        if original_name in pruned_taxa:
            node["pruned"] = True
            
    if "children" in node:
        for child in node["children"]:
            apply_state_to_structure(child, renames, pruned_taxa)

def reroot_tree(job_dir: Path, tree_json: Dict, root_target: str) -> Dict:
    """
    Reroot using any valid node (tip or internal). 
    Update tree_json's root and save the modified tree to tree_pruned.newick.
    """
    if not HAS_BIOPYTHON:
        return tree_json # Return unchanged if no library

    # Determine input path: prefer existing modified tree
    pruned_newick = job_dir / "tree" / "tree_pruned.newick"
    original_newick = job_dir / "tree" / "tree_original.newick"
    
    input_path = pruned_newick if pruned_newick.exists() else original_newick
    if not input_path.exists():
        raise FileNotFoundError("No tree file found to reroot")

    try:
        # Load the tree
        tree = Phylo.read(str(input_path), "newick")
        
        # Validate target
        if not root_target:
             raise ValueError("Reroot target cannot be empty")
             
        # Find the target clade (search ANY clade, not just terminals)
        target_clade = None
        for clade in tree.find_clades():
            if clade.name == root_target:
                target_clade = clade
                break
        
        if target_clade is None:
             # Useful debug info in the error
             internal = [c.name for c in tree.get_nonterminals() if c.name][:15]
             tips = [c.name for c in tree.get_terminals() if c.name][:15]
             raise ValueError(
                f"Root target not found: {root_target}. "
                f"Example internal names: {internal}. Example tip names: {tips}"
             )
        
        # Reroot
        tree.root_with_outgroup(target_clade)
        
        # Ladderize (Deterministic)
        ladderize_tree(tree)

        # FIX: Ensure confidence is dropped for named nodes before saving/returning
        _drop_confidence_when_named(tree)

        # Build structure
        new_structure = _clade_to_json(tree.root)
        
        # Update state
        tree_json["root"] = root_target
        tree_json["root_mode"] = "TIP"
        tree_json["root_target"] = root_target
        tree_json["current_tree"] = "pruned"
        tree_json["tree_structure"] = new_structure
        
        # Re-apply metadata
        renames = tree_json.get("renames", {})
        pruned_taxa = set(tree_json.get("pruned_taxa", []))
        apply_state_to_structure(new_structure, renames, pruned_taxa)
        
        # Save physical file
        valid_path = job_dir / "tree"
        valid_path.mkdir(parents=True, exist_ok=True)
        Phylo.write(tree, str(valid_path / "tree_pruned.newick"), "newick")
        logging.info(f"Successfully wrote rerooted tree to {valid_path / 'tree_pruned.newick'}")
        
        return tree_json
        
    except Exception as e:
        logger.error(f"Reroot failed: {e}")
        raise

    return tree_json

def midpoint_root(job_dir: Path, tree_json: Dict) -> Dict:
    """
    Reroot tree at midpoint.
    Requires branch lengths.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("Biopython not installed; midpoint rooting unavailable")

    # Determine input path
    pruned_newick = job_dir / "tree" / "tree_pruned.newick"
    original_newick = job_dir / "tree" / "tree_original.newick"
    
    input_path = pruned_newick if pruned_newick.exists() else original_newick
    if not input_path.exists():
        raise FileNotFoundError("No tree file found to reroot")

    try:
        tree = Phylo.read(str(input_path), "newick")
        
        # Check for branch lengths
        # Heuristic: Verify we have at least one positive branch length.
        has_lengths = any((c.branch_length or 0) > 0 for c in tree.find_clades())
        if not has_lengths:
             raise ValueError("Cannot perform midpoint rooting: Tree has no valid branch lengths.")

        # Attempt BioPython midpoint rooting
        # This modifies the tree in-place usually, but sometimes returns new tree depending on version.
        # Phylo.NewickIO check: root_at_midpoint modifies in place.
        try:
            tree.root_at_midpoint()
        except Exception as e:
            logger.warning(f"Midpoint rooting failed (Math domain or topology?): {e}")
            raise ValueError(f"Midpoint rooting failed: {e}") from e

        # Ladderize (Deterministic)
        ladderize_tree(tree)

        # FIX: Ensure confidence is dropped for named nodes before saving/returning
        _drop_confidence_when_named(tree)

        # Build structure
        new_structure = _clade_to_json(tree.root)
        
        # Update state
        tree_json["root"] = None
        tree_json["root_mode"] = "MIDPOINT"
        tree_json["root_target"] = None
        tree_json["current_tree"] = "pruned"
        tree_json["tree_structure"] = new_structure
        tree_json["is_midpoint_rooted"] = True
        
        # Re-apply metadata
        renames = tree_json.get("renames", {})
        pruned_taxa = set(tree_json.get("pruned_taxa", []))
        apply_state_to_structure(new_structure, renames, pruned_taxa)
        
        # Save physical file
        valid_path = job_dir / "tree"
        valid_path.mkdir(parents=True, exist_ok=True)
        Phylo.write(tree, str(valid_path / "tree_pruned.newick"), "newick")
        
        return tree_json
        
    except Exception as e:
        logger.error(f"Midpoint root failed: {e}")
        raise


def undo_midpoint_root(job_dir: Path, tree_json: Dict) -> Dict:
    """
    Restore tree to pre-midpoint rooted state.
    Uses the stored pre_midpoint_newick from tree state.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("Biopython not installed; cannot restore tree")

    pre_midpoint_newick = tree_json.get("pre_midpoint_newick")
    if not pre_midpoint_newick:
        raise ValueError("No pre-midpoint tree backup found. Cannot undo midpoint rooting.")

    try:
        # Parse the stored original newick
        tree = Phylo.read(StringIO(pre_midpoint_newick), "newick")
        
        # Ensure unique labels
        ensure_unique_labels(tree)
        
        # Ladderize for consistent display
        ladderize_tree(tree)
        
        # FIX: Ensure confidence is dropped for named nodes
        _drop_confidence_when_named(tree)

        # Build structure
        new_structure = _clade_to_json(tree.root)
        
        # Update state
        tree_json["root"] = None
        tree_json["root_mode"] = "ORIGINAL"
        tree_json["root_target"] = None
        tree_json["current_tree"] = "pruned"
        tree_json["tree_structure"] = new_structure
        tree_json["is_midpoint_rooted"] = False
        
        # Re-apply metadata
        renames = tree_json.get("renames", {})
        pruned_taxa = set(tree_json.get("pruned_taxa", []))
        apply_state_to_structure(new_structure, renames, pruned_taxa)
        
        # Save physical file
        valid_path = job_dir / "tree"
        valid_path.mkdir(parents=True, exist_ok=True)
        Phylo.write(tree, str(valid_path / "tree_pruned.newick"), "newick")
        
        return tree_json
        
    except Exception as e:
        logger.error(f"Undo midpoint root failed: {e}")
        raise

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


def _collapse_unifurcations(tree) -> None:
    """
    Collapse internal nodes that have exactly one child (unifurcations).
    
    After pruning, you can end up with internal nodes that only have one child,
    which is structurally redundant and looks odd in visualizations.
    This function splices out such nodes by promoting the child to replace
    the single-child parent.
    
    Branch lengths are summed when collapsing: if parent has length 0.1 and
    child has length 0.2, the surviving child gets length 0.3.
    
    Modifies tree in-place.
    """
    # Handle root unifurcation first
    # If root has exactly one child, promote that child to be the new root
    while tree.root.clades and len(tree.root.clades) == 1:
        only_child = tree.root.clades[0]
        # Sum branch lengths (root typically has no length, but be safe)
        if tree.root.branch_length and only_child.branch_length:
            only_child.branch_length += tree.root.branch_length
        elif tree.root.branch_length:
            only_child.branch_length = tree.root.branch_length
        # Promote child to root
        tree.root = only_child
        # Continue loop in case the new root is also a unifurcation
    
    # Build parent map for non-root collapses
    parents = {c: p for p in tree.find_clades() for c in p.clades}
    
    # Iteratively collapse until no unifurcations remain
    # We need to iterate because collapsing one node might create another
    changed = True
    while changed:
        changed = False
        # Get fresh list of non-terminals each iteration
        for clade in list(tree.get_nonterminals()):
            # Skip if it's the root or doesn't have exactly one child
            if clade == tree.root or len(clade.clades) != 1:
                continue
                
            only_child = clade.clades[0]
            parent = parents.get(clade)
            
            if parent is None:
                # This shouldn't happen if we handled root above, but be safe
                continue
            
            # Sum branch lengths
            new_length = only_child.branch_length
            if clade.branch_length is not None:
                if new_length is not None:
                    new_length += clade.branch_length
                else:
                    new_length = clade.branch_length
            only_child.branch_length = new_length
            
            # Replace clade with only_child in parent's children list
            idx = parent.clades.index(clade)
            parent.clades[idx] = only_child
            
            # Update parent map for the promoted child
            parents[only_child] = parent
            
            changed = True
            # Rebuild parent map since tree structure changed
            parents = {c: p for p in tree.find_clades() for c in p.clades}
            break  # Restart iteration with fresh non-terminal list


def _drop_confidence_when_named(tree) -> None:
    """
    Biopython Newick writer concatenates clade.name + clade.confidence.
    If we set name (e.g., Node_12_100), confidence must be None to prevent 
    outputting Node_12_100100.
    """
    for clade in tree.get_nonterminals():
        if clade.name and clade.confidence is not None:
            clade.confidence = None

def ensure_unique_labels(tree) -> bool:
    """
    Traverse the tree and ensure every internal node has a unique name.
    Tips are PROTECTED: Duplicate tip names will raise ValueError.
    Internal nodes with no name or duplicate/numeric names will be assigned 'Node_{i}'.
    Returns True if changes were made.
    """
    seen_names = set()
    changes_made = False
    counter = 1
    
    # Pass 1: Collect tip names and validate uniqueness
    for clade in tree.get_terminals():
        if not clade.name:
             # Should practically never happen for a valid Newick tip, but strict check
             raise ValueError("Tip node missing name")
        if clade.name in seen_names:
             raise ValueError(f"Duplicate tip name found: '{clade.name}'. Tips must be unique.")
        seen_names.add(clade.name)
        
    # Pass 2: Handle Internal Nodes
    # We want to preserve existing unique non-numeric internal names if possible
    
    # Collect existing internal names to avoid collisions
    existing_internal_names = set()
    for clade in tree.get_nonterminals():
        if clade.name:
            existing_internal_names.add(clade.name)

    # Assign/Sanitize
    seen_in_pass = set()
    import re
    numeric_pattern = re.compile(r'^\d+(\.\d+)?$')

    for clade in tree.get_nonterminals():
        needs_rename = False
        
        # Check original numeric value (Name or Confidence)
        original_numeric_match = None
        is_numeric_name = False
        
        if clade.name:
            if numeric_pattern.match(clade.name):
                is_numeric_name = True
                # Filter out likely IDs (e.g. 6100)
                try:
                    val = float(clade.name)
                    if val <= 100:
                        original_numeric_match = clade.name
                except ValueError:
                    pass
        elif clade.confidence is not None:
             # Check confidence if name missing
             conf_str = str(clade.confidence)
             if numeric_pattern.match(conf_str):
                 # Filter out likely IDs
                 try:
                    val = float(conf_str)
                    if val <= 100:
                        original_numeric_match = conf_str
                 except ValueError:
                    pass

        # Criteria for renaming internal node:
        # 1. Empty name
        # 2. Duplicate of matched Tip (cannot clash with tips)
        # 3. Duplicate of already seen internal name
        # 4. Numeric name (matches regex) - sanitize to avoid confusion
        
        if not clade.name:
            needs_rename = True
        elif clade.name in seen_names: # Clash with tip
            needs_rename = True
        elif clade.name in seen_in_pass: # Clash with other internal
            needs_rename = True
        elif is_numeric_name: # Numeric safety
            needs_rename = True
            
        if needs_rename:
            changes_made = True
            # Generate unique name
            while True:
                candidate = f"Node_{counter}"
                
                # Hybrid Logic: If we are renaming, preserve value if we found one
                if original_numeric_match:
                    candidate = f"{candidate}_{original_numeric_match}"
                    
                counter += 1
                
                # Must be unique globally (not in Tips, not in Existing Internal, not in Newly Assigned)
                if (candidate not in seen_names and 
                    candidate not in existing_internal_names and 
                    candidate not in seen_in_pass):
                    clade.name = candidate
                    # Add to "existing" so we don't re-generate it
                    existing_internal_names.add(candidate)
                    
                    # Probably not needed, but just in case
                    clade.confidence = None
                    break
        
        if clade.name:
            seen_in_pass.add(clade.name)
            
    # CRITICAL FIX: Prevent BioPython from doubling up (Name + Confidence)
    # This applies to ALL named internal nodes, whether we renamed them or they came named.
    _drop_confidence_when_named(tree)
    
    return changes_made

def ladderize_tree(tree, ascending: bool = False):
    """
    Sorts clades in-place to ensure deterministic visual output.
    Sort keys:
      1. Number of descendant tips (size).
      2. Lexicographically smallest tip name (min_leaf_name).
    """
    
    # Pre-calculate metrics to avoid re-traversing constantly
    # We'll attach temp attributes to nodes
    
    def get_metrics(clade):
        if hasattr(clade, "_ladder_metrics"):
            return clade._ladder_metrics
            
        if clade.is_terminal():
            count = 1
            min_name = clade.name or ""
        else:
            count = 0
            min_name = None
            for c in clade.clades:
                c_metrics = get_metrics(c)
                count += c_metrics[0]
                c_min = c_metrics[1]
                if min_name is None or (c_min is not None and c_min < min_name):
                    min_name = c_min
                    
            if min_name is None: min_name = "" # Should not happen if tips named
            
        clade._ladder_metrics = (count, min_name)
        return (count, min_name)

    # Calculate all metrics from root
    get_metrics(tree.root)
    
    # Sort recursively
    def sort_clade(clade):
        if not clade.is_terminal():
            # Sort children
            # Python's sort is stable.
            # We want primary: count, secondary: min_name
            # If ascending=True: Small counts first.
            
            clade.clades.sort(key=lambda c: (c._ladder_metrics[0], c._ladder_metrics[1]), reverse=not ascending)
            
            for c in clade.clades:
                sort_clade(c)

    sort_clade(tree.root)
    
    # Cleanup
    for clade in tree.find_clades():
        if hasattr(clade, "_ladder_metrics"):
            del clade._ladder_metrics

def initialize_tree(job_dir: Path) -> Path:
    """
    Ensure tree_pruned.newick exists and has unique node labels.
    If missing, copy original -> pruned, sanitize labels, and save.
    Returns path to pruned tree.
    """
    pruned_path = job_dir / "tree" / "tree_pruned.newick"
    original_path = job_dir / "tree" / "tree_original.newick"
    
    if pruned_path.exists():
        return pruned_path
        
    if not original_path.exists():
        raise FileNotFoundError("Original tree not found")
        
    if not HAS_BIOPYTHON:
        # Just copy if we can't sanitize
        shutil.copy(original_path, pruned_path)
        return pruned_path
        
    try:
        tree = Phylo.read(str(original_path), "newick")
        ensure_unique_labels(tree)
        
        # Save to pruned
        Phylo.write(tree, str(pruned_path), "newick")
        
        # Initialize basic state JSON too
        tree_json = _clade_to_json(tree.root)
        state = {
            "current_tree": "pruned",
            "tree_structure": tree_json,
            "pruned_taxa": [],
            "renames": {},
            "root": None
        }
        save_tree_state(job_dir, state)
        
    except Exception as e:
        logger.error(f"Failed to initialize tree: {e}")
        # Fallback copy
        shutil.copy(original_path, pruned_path)
        
    return pruned_path

def json_to_newick(tree_json: Dict) -> str:
    """
    Serialize JSON tree back to Newick.
    """
    # If tree_json has "tree_structure", use that
    node = tree_json.get("tree_structure", tree_json)
    
    def _node_to_newick(n):
        name = n.get("name", "")
        length = n.get("branch_length")
        length_str = f":{length}" if length is not None else ""
        
        if "children" in n and n["children"]:
            children_str = ",".join([_node_to_newick(c) for c in n["children"]])
            return f"({children_str}){name}{length_str}"
        else:
            return f"{name}{length_str}"

    return _node_to_newick(node) + ";"
