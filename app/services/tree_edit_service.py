import copy
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple
from app.config import Config
from app.models import JobParams, AlignmentParams, TrimmingParams, TreeBuilderParams
from app.services.security_utils import coerce_bool
from app.services.subprocess_utils import run_command

# Try to import BioPython
try:
    from Bio import Phylo, AlignIO, SeqIO
    from io import StringIO
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)

MAX_SEQUENCE_OF_INTEREST_LENGTH = 1000
MAX_SEQUENCE_OF_INTEREST_SOURCE_LENGTH = 64

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
        
        # Determine rooting policy
        should_midpoint = True
        
        # Check if outgroup was specified in job params
        input_info_path = job_dir / "input_info.json"
        if input_info_path.exists():
            try:
                with open(input_info_path, "r") as f:
                    info = json.load(f)
                    # Check metrics or raw params
                    # job params might be flattening, but 'outgroup' should be in tree_builder_params or metrics
                    # Let's check typical locations
                    metrics = info.get("metrics", {})
                    outgroup = info.get("outgroup") or metrics.get("outgroup")
                    
                    if outgroup:
                        should_midpoint = False
                        state["root"] = outgroup
                        state["root_mode"] = "OUTGROUP"
                        logging.info(f"Outgroup '{outgroup}' detected. Skipping default midpoint rooting.")
            except Exception as e:
                logging.warning(f"Failed to check input_info for outgroup: {e}")

        # Default policy: Midpoint root the tree (if not outgroup rooted)
        if should_midpoint:
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


def _iter_tip_names(node: Dict[str, Any]):
    if not isinstance(node, dict):
        return
    children = node.get("children")
    if children:
        for child in children:
            yield from _iter_tip_names(child)
        return
    for key in ("original_name", "name"):
        value = node.get(key)
        if isinstance(value, str) and value:
            yield value


def _tree_tip_set(tree_json: Dict[str, Any]) -> Set[str]:
    return set(_iter_tip_names(tree_json.get("tree_structure") or {}))


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _validate_sequence_of_interest(tree_json: Dict[str, Any],
                                   tip_name: Optional[str],
                                   source: Any) -> Optional[str]:
    if tip_name is None:
        return None
    if not isinstance(tip_name, str):
        raise ValueError("sequence_of_interest tip_name must be a string or null")
    if tip_name == "":
        return None
    if len(tip_name) > MAX_SEQUENCE_OF_INTEREST_LENGTH:
        raise ValueError("sequence_of_interest tip_name is too long")
    if _has_control_chars(tip_name):
        raise ValueError("sequence_of_interest tip_name contains control characters")
    if not isinstance(source, str):
        raise ValueError("sequence_of_interest source must be a string")
    if len(source) > MAX_SEQUENCE_OF_INTEREST_SOURCE_LENGTH:
        raise ValueError("sequence_of_interest source is too long")
    if _has_control_chars(source):
        raise ValueError("sequence_of_interest source contains control characters")
    if tip_name not in _tree_tip_set(tree_json):
        raise ValueError("sequence_of_interest tip_name is not present in the current tree")
    return tip_name


def _int_param(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_param(params_dict: Dict[str, Any], key: str, default: bool) -> bool:
    return coerce_bool(params_dict.get(key), default)[0]


def build_recompute_job_params(params_dict: Dict[str, Any]) -> JobParams:
    """Build the dataclass params used by tree recomputation from stored job JSON."""
    align_params = AlignmentParams(
        method=params_dict.get("alignment_method", "default"),
        advanced_options=params_dict.get("alignment_options", {}) or {}
    )

    trim_params = TrimmingParams(
        method=params_dict.get("trimming_method", "none"),
        trim_terminal_overhangs=_bool_param(params_dict, "trim_terminal_overhangs", True),
    )

    tree_params = TreeBuilderParams(
        method=params_dict.get("tree_method", "nj"),
        model=params_dict.get("tree_model", "GTR+G"),
        bootstrap=_int_param(params_dict.get("bootstrap", 100), 100),
        mcmc_generations=_int_param(params_dict.get("mcmc_generations", 50000), 50000),
        mcmc_nruns=_int_param(params_dict.get("mcmc_nruns", 2), 2),
        mcmc_nchains=_int_param(params_dict.get("mcmc_nchains", 4), 4),
        run_preset=params_dict.get("run_preset", "fast_good"),
        bootstrap_preset=params_dict.get("bootstrap_preset", "standard"),
        bootstrap_cap=params_dict.get("bootstrap_cap"),
        enable_bootstrap=_bool_param(params_dict, "enable_bootstrap", True),
        start_tree_override=params_dict.get("start_tree_override"),
        moose_enabled=_bool_param(params_dict, "moose_enabled", False),
        early_stopping=_bool_param(params_dict, "early_stopping", False),
        seed=params_dict.get("seed"),
        outgroup=params_dict.get("outgroup")
    )

    return JobParams(
        input_type="recompute",
        notes=params_dict.get("notes", ""),
        sequence=params_dict.get("sequence", ""),
        accessions=params_dict.get("accessions", []),
        alignment_params=align_params,
        trimming_params=trim_params,
        tree_builder_params=tree_params,
        allow_recompute=True
    )


def _selection_sets_without_names(tree_json: Dict, removed_names: Set[str]) -> None:
    """Remove pruned node IDs from persisted selection sets in-place."""
    if not removed_names:
        return

    selection_sets = tree_json.get("selection_sets")
    if not isinstance(selection_sets, dict):
        return

    for set_name, member_ids in list(selection_sets.items()):
        if not isinstance(member_ids, list):
            continue
        selection_sets[set_name] = [
            member_id for member_id in member_ids
            if member_id not in removed_names
        ]


def _stable_internal_node_id_from_names(tip_names: List[str]) -> str:
    """Build the same stable internal-node ID used by the browser viewer."""
    key = "\x1f".join(sorted(str(name) for name in tip_names if name))
    hash_value = 2166136261
    for char in key:
        hash_value ^= ord(char)
        hash_value = (hash_value * 16777619) & 0xFFFFFFFF
    return f"internal:{hash_value:08x}"


def _stable_internal_node_id(clade) -> Optional[str]:
    """Return a stable ID for an internal clade based on descendant tip labels."""
    if not getattr(clade, "clades", None):
        return None
    tip_names = [terminal.name for terminal in clade.get_terminals() if terminal.name]
    if not tip_names:
        return None
    return _stable_internal_node_id_from_names(tip_names)


def _editable_tree_input_path(job_dir: Path, what: str = "edit") -> Path:
    """Return the current editable tree file (pruned if present, else original).

    Shared by rotate/prune/reroot/midpoint so the pruned-vs-original selection
    lives in one place instead of being copy-pasted into each.
    """
    pruned_newick = job_dir / "tree" / "tree_pruned.newick"
    original_newick = job_dir / "tree" / "tree_original.newick"
    input_path = pruned_newick if pruned_newick.exists() else original_newick
    if not input_path.exists():
        raise FileNotFoundError(f"No tree file found to {what}")
    return input_path


def rotate_node(job_dir: Path, tree_json: Dict, node_id: str) -> Dict:
    """
    Reverse the immediate child order for one internal node.
    This changes only display order; parents, branch lengths, labels, and tips stay intact.
    """
    if not node_id:
        raise ValueError("Missing node_id")

    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython not installed; cannot rotate tree")

    input_path = _editable_tree_input_path(job_dir, "rotate")

    try:
        tree = Phylo.read(str(input_path), "newick")

        target_clade = None
        for clade in tree.find_clades(order="preorder"):
            # Alan 6/2/26 - Only internal nodes with >=2 children are rotatable. Skipping
            # leaves and unary pass-through nodes also avoids a stable-id collision where a
            # unary parent shares its only child's descendant-leaf set (preorder would hit
            # the parent first and wrongly fail on a legitimate node).
            if not getattr(clade, "clades", None) or len(clade.clades) < 2:
                continue
            stable_id = _stable_internal_node_id(clade)
            if stable_id == node_id or (clade.name and clade.name == node_id):
                target_clade = clade
                break

        if target_clade is None:
            raise ValueError("Unknown internal node")

        target_clade.clades.reverse()
        _drop_confidence_when_named(tree)

        valid_path = job_dir / "tree"
        valid_path.mkdir(parents=True, exist_ok=True)
        Phylo.write(tree, str(valid_path / "tree_pruned.newick"), "newick")
        Phylo.write(tree, str(valid_path / "tree_pruned.nexus"), "nexus")

        new_structure = _clade_to_json(tree.root)
        tree_json["current_tree"] = "pruned"
        tree_json["tree_structure"] = new_structure
        tree_json["last_rotated_node_id"] = node_id

        renames = tree_json.get("renames", {})
        pruned_taxa = set(tree_json.get("pruned_taxa", []))
        apply_state_to_structure(new_structure, renames, pruned_taxa)
        return tree_json

    except Exception as e:
        logger.error(f"Rotate node failed: {e}")
        raise


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
    input_path = _editable_tree_input_path(job_dir, "prune")

    try:
        tree = Phylo.read(str(input_path), "newick")
        
        # We need to prune multiple nodes. 
        targets = set(taxa_names)
        
        to_prune = []
        for clade in tree.find_clades():
            stable_id = _stable_internal_node_id(clade)
            if clade.name in targets or (stable_id and stable_id in targets):
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

        pruned_names = {name for name in taxa_names if name}
        for clade in to_prune:
            if clade.name:
                pruned_names.add(clade.name)
            for terminal in clade.get_terminals():
                if terminal.name:
                    pruned_names.add(terminal.name)
        
        # Only update metadata AFTER confirming targets exist in tree
        if "pruned_taxa" not in tree_json:
            tree_json["pruned_taxa"] = []
        
        for name in sorted(pruned_names):
            if name not in tree_json["pruned_taxa"]:
                tree_json["pruned_taxa"].append(name)
        _selection_sets_without_names(tree_json, pruned_names)

        # Invalidate the focal sequence if it was pruned out. Only flag that Auto root needs
        # a new focal tip when an auto-style mode is actually active; midpoint/manual/unrooted
        # modes must not surface a stale "pick a sequence of interest" prompt after reload.
        if tree_json.get("sequence_of_interest") in pruned_names:
            tree_json["sequence_of_interest"] = None
            auto_modes = ("auto", "most_divergent_hit")
            tree_json["needs_sequence_of_interest"] = (tree_json.get("root_mode") or "").lower() in auto_modes
             
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
        Phylo.write(tree, str(valid_path / "tree_pruned.nexus"), "nexus")
        
        # Update JSON structure to match
        new_structure = _clade_to_json(tree.root)
        tree_json["current_tree"] = "pruned"
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

    # Alan 6/2/26 - Do NOT rewrite sequence_of_interest to the new display name: the focal
    # tip is tracked by its ORIGINAL name (selection sets, patristic distances and the
    # alignment all key on original names), so syncing to the display name broke auto-root
    # resolution for a renamed focal tip. Leaving it as the original name keeps it resolvable.

    def apply_rename(node):
        if node.get("name") == old_name or node.get("original_name") == old_name:
            node["name"] = new_name
            node["display_name"] = new_name
            # Preserve original_name as the stable edit identifier while
            # making the visible node label update immediately.
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
            renamed = renames[original_name]
            node["name"] = renamed
            node["display_name"] = renamed
        
        # Apply Prune
        if original_name in pruned_taxa:
            node["pruned"] = True
            
    if "children" in node:
        for child in node["children"]:
            apply_state_to_structure(child, renames, pruned_taxa)


def _write_rerooted_tree(job_dir: Path, tree_json: Dict, tree, root_target: str) -> Dict:
    """Persist an already rerooted Bio.Phylo tree and mirror it into tree state."""
    ladderize_tree(tree)
    _drop_confidence_when_named(tree)

    new_structure = _clade_to_json(tree.root)

    tree_json["root"] = root_target
    tree_json["root_mode"] = "TIP"
    tree_json["root_target"] = root_target
    tree_json["current_tree"] = "pruned"
    tree_json["tree_structure"] = new_structure
    tree_json["is_midpoint_rooted"] = False
    tree_json["needs_sequence_of_interest"] = False
    tree_json["rooting_info"] = {
        "query_tip": tree_json.get("sequence_of_interest"),
        "chosen_root_target": root_target,
        "chosen_by": "manual",
        "reason": "user_manual_reroot",
        "warnings": [],
        "candidate_count": 0,
        "rejected_count": 0,
    }

    renames = tree_json.get("renames", {})
    pruned_taxa = set(tree_json.get("pruned_taxa", []))
    apply_state_to_structure(new_structure, renames, pruned_taxa)

    valid_path = job_dir / "tree"
    valid_path.mkdir(parents=True, exist_ok=True)
    Phylo.write(tree, str(valid_path / "tree_pruned.newick"), "newick")
    logging.info(f"Successfully wrote rerooted tree to {valid_path / 'tree_pruned.newick'}")

    return tree_json


def reroot_tree(job_dir: Path, tree_json: Dict, root_target: str) -> Dict:
    """
    Reroot using any valid node (tip or internal). 
    Update tree_json's root and save the modified tree to tree_pruned.newick.
    """
    if not HAS_BIOPYTHON:
        return tree_json # Return unchanged if no library

    # Determine input path: prefer existing modified tree
    input_path = _editable_tree_input_path(job_dir, "reroot")

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
             # Fallback: check confidence converted to string
             # This handles cases where Newick I/O converted numeric name -> confidence
             for clade in tree.find_clades():
                 if clade.confidence is not None and str(clade.confidence) == root_target:
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
        return _write_rerooted_tree(job_dir, tree_json, tree, root_target)
        
    except Exception as e:
        logger.error(f"Reroot failed: {e}")
        raise

    return tree_json


def _find_clade_path_by_tip(clade, tip_name: str, path: Optional[List[Any]] = None):
    path = list(path or [])
    current = path + [clade]
    if clade.name == tip_name and not getattr(clade, "clades", None):
        return current
    for child in getattr(clade, "clades", []) or []:
        found = _find_clade_path_by_tip(child, tip_name, current)
        if found:
            return found
    return None


def _terminal_names(clade) -> List[str]:
    return [terminal.name for terminal in clade.get_terminals() if terminal.name]


def _best_taxon_distinct_outgroup_clade(tree, focal_tip: str,
                                        target_tip: str) -> Tuple[Any, Dict[str, Any]]:
    from app.services.tree_rooting_service import _extract_binomial

    focal_taxon = _extract_binomial(focal_tip)
    target_taxon = _extract_binomial(target_tip)
    if not focal_taxon or not target_taxon or focal_taxon == target_taxon:
        target = next((t for t in tree.get_terminals() if t.name == target_tip), None)
        return target, {"tip_count": 1, "rooted_on": "target_tip"}

    path = _find_clade_path_by_tip(tree.root, target_tip)
    if not path:
        return None, {"tip_count": 0, "rooted_on": "target_missing"}

    best = None
    best_names: List[str] = []
    for clade in path:
        names = _terminal_names(clade)
        if target_tip not in names or focal_tip in names:
            continue
        has_focal_taxon = any(_extract_binomial(name) == focal_taxon for name in names)
        if has_focal_taxon:
            continue
        if len(names) > len(best_names):
            best = clade
            best_names = names

    if best is None:
        best = path[-1]
        best_names = _terminal_names(best)

    return best, {
        "tip_count": len(best_names),
        "rooted_on": "taxon_distinct_clade" if len(best_names) > 1 else "target_tip",
        "focal_taxon": " ".join(focal_taxon),
        "target_taxon": " ".join(target_taxon),
    }


def reroot_tree_on_best_outgroup_clade(job_dir: Path, tree_json: Dict,
                                       focal_tip: str,
                                       target_tip: str) -> Tuple[Dict, Dict[str, Any]]:
    """Root Auto mode on the best clade around a distinct-taxon target tip."""
    if not HAS_BIOPYTHON:
        return tree_json, {"tip_count": 0, "rooted_on": "biopython_unavailable"}

    input_path = _editable_tree_input_path(job_dir, "auto-root")
    tree = Phylo.read(str(input_path), "newick")
    target_clade, clade_info = _best_taxon_distinct_outgroup_clade(tree, focal_tip, target_tip)
    if target_clade is None:
        raise ValueError(f"Root target not found: {target_tip}")

    tree.root_with_outgroup(target_clade)
    return _write_rerooted_tree(job_dir, tree_json, tree, target_tip), clade_info


def midpoint_root(job_dir: Path, tree_json: Dict) -> Dict:
    """
    Reroot tree at midpoint.
    Requires branch lengths.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("Biopython not installed; midpoint rooting unavailable")

    # Determine input path
    input_path = _editable_tree_input_path(job_dir, "reroot")

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

        # Ensure unique labels for stable IDs
        ensure_unique_labels(tree)

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
    def clean_label(label) -> Optional[str]:
        if label is None:
            return None
        cleaned = str(label).strip()
        return cleaned or None

    pruned_taxa = {
        cleaned
        for name in tree_json.get("pruned_taxa", [])
        for cleaned in [clean_label(name)]
        if cleaned
    }
    
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython required for FASTA manipulation")
        
    sequences = []
    with open(original_fasta, "r") as f:
        for record in SeqIO.parse(f, "fasta"):
            record_labels = {
                cleaned
                for label in (record.id, record.name, record.description)
                for cleaned in [clean_label(label)]
                if cleaned
            }
            if record_labels.isdisjoint(pruned_taxa):
                sequences.append(record)
                
    with open(output_fasta, "w") as f:
        SeqIO.write(sequences, f, "fasta")

def _count_fasta_records(path: Path) -> int:
    try:
        with open(path, "r") as f:
            return sum(1 for line in f if line.startswith(">"))
    except Exception:
        return 0


def _count_fasta_columns(path: Path) -> int:
    """Alignment width = length of the first record's sequence."""
    try:
        seq_len = 0
        with open(path, "r") as f:
            in_first = False
            for line in f:
                if line.startswith(">"):
                    if in_first:
                        break
                    in_first = True
                    continue
                if in_first:
                    seq_len += len(line.strip())
        return seq_len
    except Exception:
        return 0


def _reapply_rooting_after_recompute(job_dir: Path, tree_json: Dict[str, Any],
                                     previous_mode: Optional[str],
                                     previous_target: Optional[str],
                                     task_logger=None) -> Dict[str, Any]:
    """Reapply the viewer's rooting intent after recompute writes a fresh tree."""
    mode = (previous_mode or "").lower()
    if not mode or mode == "original":
        return tree_json

    try:
        if mode in ("auto", "most_divergent_hit", "midpoint", "unrooted"):
            return apply_rooting_mode(job_dir, tree_json, mode)
        if mode in ("manual", "tip", "outgroup") and previous_target:
            return apply_rooting_mode(job_dir, tree_json, "manual", target=previous_target)

        # Reached when a manual/tip/outgroup mode has no usable target (e.g. the
        # target tip was pruned in this recompute) or the stored mode is
        # unrecognized. Don't silently keep the tree builder's arbitrary root:
        # log it and record that the prior rooting intent couldn't be reapplied.
        log = task_logger or logger
        reason = (
            "previous rooting target no longer present"
            if mode in ("manual", "tip", "outgroup")
            else f"unrecognized rooting mode: {mode}"
        )
        log.warning(
            "Could not reapply %r rooting after recompute (target=%r): %s",
            mode, previous_target, reason,
        )
        tree_json["root"] = None
        tree_json["root_target"] = None
        tree_json["root_mode"] = ""
        tree_json["is_midpoint_rooted"] = False
        tree_json["needs_sequence_of_interest"] = False
        tree_json["rooting_info"] = {
            "query_tip": tree_json.get("sequence_of_interest"),
            "chosen_root_target": previous_target,
            "chosen_by": "recompute_rooting_unresolved",
            "reason": f"reapply_unresolved:{mode}",
            "warnings": [reason],
            "candidate_count": 0,
            "rejected_count": 0,
        }
        return tree_json
    except Exception as e:
        log = task_logger or logger
        log.warning("Failed to reapply %s rooting after recompute: %s", mode, e)
        tree_json["root"] = None
        tree_json["root_target"] = None
        tree_json["root_mode"] = ""
        tree_json["is_midpoint_rooted"] = False
        tree_json["needs_sequence_of_interest"] = False
        tree_json["rooting_info"] = {
            "query_tip": tree_json.get("sequence_of_interest"),
            "chosen_root_target": previous_target,
            "chosen_by": "recompute_rooting_failed",
            "reason": f"reapply_failed:{mode}",
            "warnings": [str(e)],
            "candidate_count": 0,
            "rejected_count": 0,
        }
    return tree_json


def recompute_tree(
    job_dir: Path,
    job_params: JobParams,
    config: Config,
    logger,
    event_job_id: Optional[str] = None,
    rq_job=None,
    use_current_input: bool = False
) -> Dict[str, Any]:
    """
    Re-run the alignment, trimming, and tree inference pipeline 
    using only sequences present in the pruned tree, or the current input FASTA
    when use_current_input is true.
    """
    logger.info("Starting tree recomputation...")

    if event_job_id:
        from app.workers.events import (
            STEP_INPUT, STEP_ORIENT, STEP_BLAST, STEP_ALIGN, STEP_TRIM, STEP_TREE, STEP_POST,
            STATE_RUNNING, STATE_DONE, STATE_SKIPPED,
            publish_step_start, publish_step_done, publish_overview, update_step_meta,
        )

        def start_step(step, label, detail="", tool=""):
            if rq_job:
                rq_job.meta["current_step"] = step
                rq_job.meta["current_tool"] = tool or None
                rq_job.save_meta()
            publish_step_start(event_job_id, step, label, detail, tool=tool)
            update_step_meta(rq_job, step, {"state": STATE_RUNNING, "label": label, "detail": detail, "tool": tool})

        def finish_step(step, detail=""):
            publish_step_done(event_job_id, step, detail)
            update_step_meta(rq_job, step, {"state": STATE_DONE, "detail": detail})

        def skip_step(step, label):
            update_step_meta(rq_job, step, {"state": STATE_SKIPPED, "label": label})

        def overview(message):
            publish_overview(event_job_id, message)
    else:
        def start_step(step, label, detail="", tool=""):
            return None

        def finish_step(step, detail=""):
            return None

        def skip_step(step, label):
            return None

        def overview(message):
            return None
    
    # 1. Load state
    tree_json = load_tree_state(job_dir)
    previous_root_mode = tree_json.get("root_mode")
    previous_root_target = tree_json.get("root_target") or tree_json.get("root")
    
    # 2. Extract pruned FASTA
    # We need the original input (unaligned)
    input_raw = job_dir / "input" / "input_raw.fasta"
    alignment_pruned_path = job_dir / "alignment" / "alignment_pruned.fasta" # Unaligned input for re-run

    start_step("input", "Sequence Queue", "Preparing queued sequences")
    if use_current_input:
        if tree_json.get("pruned_taxa"):
            extract_pruned_fasta(input_raw, tree_json, alignment_pruned_path)
            input_detail = f"{_count_fasta_records(alignment_pruned_path)} queued unpruned sequence(s)"
        else:
            shutil.copy(input_raw, alignment_pruned_path)
            input_detail = f"{_count_fasta_records(alignment_pruned_path)} queued sequence(s)"
    else:
        extract_pruned_fasta(input_raw, tree_json, alignment_pruned_path)
        input_detail = f"{_count_fasta_records(alignment_pruned_path)} selected sequence(s)"
    finish_step("input", input_detail)
    overview(f"Recompute input ready: {input_detail}")
    skip_step("orient", "Orientation Check (skipped)")
    skip_step("blast", "BLAST Search (skipped)")
    
    # 3. Re-align
    # We need alignment params. Assuming they are in job_params or we use defaults.
    # If job_params passed to this function has them, great.
    # But wait, job_params usually comes from the initial job submission.
    # The recompute endpoint might pass updated params? 
    # For now, let's reuse original params or defaults.
    
    align_params = job_params.alignment_params or AlignmentParams(method="default")
    alignment_pruned_aligned_path = job_dir / "alignment" / "alignment_pruned_aligned.fasta" # Result of alignment
    
    from app.services.alignment_service import run_alignment
    align_method = (align_params.method or "default").lower()
    align_label = f"Alignment ({align_method.upper()})" if align_method != "default" else "Alignment"
    start_step("align", align_label, "", tool=align_method)
    run_alignment(alignment_pruned_path, alignment_pruned_aligned_path, align_params, config, logger, job_id=event_job_id)
    align_detail = f"{_count_fasta_records(alignment_pruned_aligned_path)} sequence(s) aligned"
    finish_step("align", align_detail)
    overview(align_detail)
    
    # 4. Re-trim
    trim_params = job_params.trimming_params or TrimmingParams(method="none")
    alignment_pruned_trimmed_path = job_dir / "alignment" / "alignment_pruned_trimmed.fasta"
    
    from app.services.trimming_service import run_trimming, describe_trim_step, format_trimming_detail
    trim_method = (trim_params.method or "none").lower()
    trim_terminal_overhangs = bool(trim_params.trim_terminal_overhangs)
    should_trim, trim_label, trim_tool = describe_trim_step(trim_method, trim_terminal_overhangs)
    if not should_trim:
        run_trimming(
            alignment_pruned_aligned_path,
            alignment_pruned_trimmed_path,
            trim_method,
            config,
            logger,
            job_id=event_job_id,
            trim_terminal_overhangs=False,
        )
        skip_step("trim", trim_label)
        overview("Trimming skipped (method: none)")
    else:
        start_step("trim", trim_label, "", tool=trim_tool)
        trim_stats = run_trimming(
            alignment_pruned_aligned_path,
            alignment_pruned_trimmed_path,
            trim_method,
            config,
            logger,
            job_id=event_job_id,
            trim_terminal_overhangs=trim_terminal_overhangs,
        )
        retained_columns = _count_fasta_columns(alignment_pruned_trimmed_path)
        trim_detail = format_trimming_detail(trim_method, trim_stats, retained_columns)
        finish_step("trim", trim_detail)
        overview(trim_detail)
    
    # 5. Re-build Tree
    tree_params = job_params.tree_builder_params or TreeBuilderParams(method="nj")
    tree_pruned_newick = job_dir / "tree" / "tree_pruned.newick"
    tree_pruned_nexus = job_dir / "tree" / "tree_pruned.nexus"
    
    from app.services.tree_builder_service import run_tree_builder
    tree_method = (tree_params.method or "nj").lower()
    start_step("tree", f"Tree Building ({tree_method.upper()})", tree_params.model or "", tool=tree_method)
    metadata = run_tree_builder(
        alignment_pruned_trimmed_path,
        tree_pruned_newick,
        tree_pruned_nexus,
        tree_params,
        config,
        logger,
        job_id=event_job_id
    )
    finish_step("tree", "Tree generated")
    overview(f"Tree rebuilt using {tree_method.upper()}")
    
    # 6. Update State
    start_step("post", "Post-Processing", "Saving recomputed tree")
    tree_json["current_tree"] = "pruned"
    # We might want to update the tree structure in JSON too?
    # Yes, parse the new tree
    new_structure = parse_newick_to_json(tree_pruned_newick)
    tree_json["tree_structure"] = new_structure
    tree_json = _reapply_rooting_after_recompute(
        job_dir,
        tree_json,
        previous_root_mode,
        previous_root_target,
        task_logger=logger,
    )
    save_tree_state(job_dir, tree_json)
    
    # Save metadata
    with open(job_dir / "tree" / "tree_pruned_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    finish_step("post", "All files ready")
    overview("Recompute complete")
        
    return {
        "status": "completed",
        "newick": str(tree_pruned_newick),
        "nexus": str(tree_pruned_nexus),
        "metadata": metadata
    }

def apply_rooting_mode(job_dir: Path, tree_json: Dict, mode: str,
                        target: Optional[str] = None,
                        sequence_of_interest: Optional[str] = None) -> Dict:
    """Apply one of the user-facing rooting modes.

    mode: "auto" | "midpoint" | "most_divergent_hit" | "unrooted" | "manual"
    """
    from app.services.tree_rooting_service import choose_auto_root_target

    if sequence_of_interest is not None:
        tree_json = set_sequence_of_interest(tree_json, sequence_of_interest, source="user_selected")

    mode = (mode or "auto").lower()

    if mode == "midpoint":
        tree_json = midpoint_root(job_dir, tree_json)
        tree_json["root_mode"] = "midpoint"
        tree_json["root_target"] = None
        # Alan 6/2/26 - Non-auto modes never need a focal tip; clear any stale prompt flag.
        tree_json["needs_sequence_of_interest"] = False
        tree_json["rooting_info"] = {
            "query_tip": tree_json.get("sequence_of_interest"),
            "chosen_root_target": None,
            "chosen_by": "midpoint",
            "reason": "user_selected_midpoint",
            "warnings": [],
            "candidate_count": 0,
            "rejected_count": 0,
        }
        return tree_json

    if mode == "manual":
        if not target:
            raise ValueError("manual rooting requires target")
        tree_json = reroot_tree(job_dir, tree_json, target)
        tree_json["root_mode"] = "manual"
        tree_json["root_target"] = target
        # Rerooting on a tip replaces midpoint rooting; keep the flag honest so
        # the Midpoint button doesn't show "(on)" for a non-midpoint root.
        tree_json["is_midpoint_rooted"] = False
        # Alan 6/2/26 - Manual rooting doesn't need a focal tip; clear any stale prompt flag.
        tree_json["needs_sequence_of_interest"] = False
        tree_json["rooting_info"] = {
            "query_tip": tree_json.get("sequence_of_interest"),
            "chosen_root_target": target,
            "chosen_by": "manual",
            "reason": "user_manual_reroot",
            "warnings": [],
            "candidate_count": 0,
            "rejected_count": 0,
        }
        return tree_json

    if mode == "unrooted":
        # Renderer doesn't yet support an unrooted layout; record intent so the
        # UI can show it and so we don't silently lie. Leave the underlying
        # tree file as-is.
        tree_json["root_mode"] = "unrooted"
        tree_json["root_target"] = None
        tree_json["is_midpoint_rooted"] = False
        # Alan 6/2/26 - Unrooted intent doesn't need a focal tip; clear any stale prompt flag.
        tree_json["needs_sequence_of_interest"] = False
        tree_json["rooting_info"] = {
            "query_tip": tree_json.get("sequence_of_interest"),
            "chosen_root_target": None,
            "chosen_by": "unrooted",
            "reason": "unrooted_layout_not_implemented_state_only",
            "warnings": ["renderer_does_not_yet_support_unrooted_layout"],
            "candidate_count": 0,
            "rejected_count": 0,
        }
        return tree_json

    # auto or most_divergent_hit
    choice = choose_auto_root_target(job_dir, tree_json, mode=mode)

    if choice.mode == "prompt_for_sequence_of_interest":
        tree_json["root_mode"] = "auto"
        tree_json["root_target"] = None
        tree_json["rooting_info"] = choice.to_info("user_prompt_required")
        tree_json["needs_sequence_of_interest"] = True
        return tree_json

    if choice.mode in ("midpoint_fallback", "none"):
        # Fall back to midpoint and record why.
        try:
            tree_json = midpoint_root(job_dir, tree_json)
        except Exception as e:
            choice.warnings.append(f"midpoint_fallback_failed:{type(e).__name__}")
            tree_json["root_mode"] = mode
            tree_json["root_target"] = None
            # Alan 6/2/26 - Fallback couldn't reroot; clear any stale target and prompt flag.
            tree_json["needs_sequence_of_interest"] = False
            tree_json["rooting_info"] = choice.to_info("midpoint_fallback")
            return tree_json
        tree_json["root_mode"] = mode
        tree_json["root_target"] = None
        # Alan 6/2/26 - We rooted via midpoint fallback; no focal-tip prompt is pending.
        tree_json["needs_sequence_of_interest"] = False
        tree_json["rooting_info"] = choice.to_info("midpoint_fallback")
        return tree_json

    # Successful auto/most_divergent_hit pick.
    clade_info = {}
    if choice.mode == "auto" and choice.query_tip:
        try:
            tree_json, clade_info = reroot_tree_on_best_outgroup_clade(
                job_dir, tree_json, choice.query_tip, choice.target_name
            )
        except Exception as e:
            logger.warning("Auto clade rooting failed; falling back to tip root: %s", e)
            clade_info = {"rooted_on": "target_tip_fallback", "warning": str(e)}
            tree_json = reroot_tree(job_dir, tree_json, choice.target_name)
    else:
        tree_json = reroot_tree(job_dir, tree_json, choice.target_name)
    tree_json["root_mode"] = choice.mode
    tree_json["root_target"] = choice.target_name
    # Auto root reroots on a chosen tip, not the midpoint; clear the flag so the
    # Midpoint button reflects reality instead of showing a stale "(on)".
    tree_json["is_midpoint_rooted"] = False
    tree_json["needs_sequence_of_interest"] = False
    tree_json["rooting_info"] = choice.to_info(choice.mode)
    if clade_info:
        tree_json["rooting_info"]["root_clade"] = clade_info
    return tree_json


def apply_auto_root_default(job_dir: Path, tree_json: Dict, focal_tip: str,
                            source: str = "user_selected") -> Dict:
    """Make Auto root the default for a single-focal-tip job, but only when the tree is
    still on its build-time default rooting ("" or "MIDPOINT") so a deliberate user
    rooting choice or an explicit outgroup is never overridden.

    Centralizes the auto-root-default policy (was an iNat-specific special case) and
    operates on a copy, only adopting it on success so a failed auto-root never persists
    a half-applied state (focal tip set but no rooting performed).
    """
    if not focal_tip:
        return tree_json
    if (tree_json.get("root_mode") or "") not in ("", "MIDPOINT"):
        return tree_json
    trial = copy.deepcopy(tree_json)
    trial["sequence_of_interest"] = focal_tip
    trial["sequence_of_interest_source"] = source
    try:
        return apply_rooting_mode(job_dir, trial, "auto", sequence_of_interest=focal_tip)
    except Exception:
        logger.warning("Auto-root default failed for %s", job_dir, exc_info=True)
        return tree_json


def set_sequence_of_interest(tree_json: Dict, tip_name: Optional[str],
                              source: str = "user_selected") -> Dict:
    """Persist the focal tip in `sequence_of_interest` only.

    The focal highlight is rendered directly from this field (the tree viewer's
    focal styling and the alignment viewer's preferredName both read it), so we do
    NOT mirror it into the user-editable Default color group — doing so corrupted
    user colorings by adding/removing members. resolve_sequence_of_interest() still
    reads a single blue Default member as a legacy/iNat fallback.
    """
    if source is None or source == "":
        source = "user_selected"
    tip_name = _validate_sequence_of_interest(tree_json, tip_name, source)

    if not tip_name:
        tree_json["sequence_of_interest"] = None
        tree_json["sequence_of_interest_source"] = None
        return tree_json

    tree_json["sequence_of_interest"] = tip_name
    tree_json["sequence_of_interest_source"] = source
    tree_json["needs_sequence_of_interest"] = False
    return tree_json


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
        "confidence": clade.confidence if clade.confidence is not None else getattr(clade, '_poly_confidence', None)
    }
    if clade.clades:
        node["stable_id"] = _stable_internal_node_id(clade)
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
            # Stash confidence so it's not lost when we clear it for Newick write safety
            clade._poly_confidence = clade.confidence
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
                    if clade.confidence is not None:
                         clade._poly_confidence = clade.confidence
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
