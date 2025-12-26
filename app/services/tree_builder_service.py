import os
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
from app.config import Config
from app.models import TreeBuilderParams
from app.services.subprocess_utils import run_command

# Try to import Bio.Phylo for NJ, or implement simple fallback
try:
    from Bio import Phylo, AlignIO
    from Bio.Phylo.TreeConstruction import DistanceCalculator, DistanceTreeConstructor
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

def run_tree_builder(
    alignment_fasta: Path,
    output_newick: Path,
    output_nexus: Path,
    params: TreeBuilderParams,
    config: Config,
    logger
) -> Dict[str, Any]:
    """
    Run the selected tree building algorithm.
    Methods: "nj", "raxml", "iqtree", "mrbayes"
    """
    method = params.method.lower()
    logger.info(f"Starting tree building with method: {method}")
    
    metadata = {
        "method": method,
        "model": params.model,
        "bootstrap": params.bootstrap,
        "run_dir": str(output_newick.parent)
    }

    try:
        if method == "nj":
            _run_neighbor_joining(alignment_fasta, output_newick, output_nexus, logger)
        elif method == "raxml":
            _run_raxml(alignment_fasta, output_newick, output_nexus, params, config, logger)
        elif method == "iqtree":
            _run_iqtree(alignment_fasta, output_newick, output_nexus, params, config, logger)
        elif method == "mrbayes":
            _run_mrbayes(alignment_fasta, output_newick, output_nexus, params, config, logger)
        else:
            raise ValueError(f"Unsupported tree building method: {method}")

        if not output_newick.exists() or output_newick.stat().st_size == 0:
             # MrBayes might produce nexus only, check logic below
             # But generally we want newick.
             # If method is mrbayes, we try to convert.
             raise RuntimeError(f"Tree building failed: Output file {output_newick} is missing or empty.")

        logger.info(f"Tree building completed successfully. Output: {output_newick}")
        return metadata

    except Exception as e:
        logger.error(f"Tree building failed: {e}")
        raise

def _get_thread_count(params: TreeBuilderParams) -> int:
    if params.threads:
        return params.threads
    return min(8, os.cpu_count() or 1)

def _run_neighbor_joining(alignment_fasta: Path, output_newick: Path, output_nexus: Path, logger):
    """
    Build a fast NJ tree using BioPython if available.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython is required for NJ but not installed.")
        
    logger.info("Running Neighbor Joining using BioPython...")
    
    # Read alignment
    aln = AlignIO.read(str(alignment_fasta), "fasta")
    
    # Calculate distance matrix
    calculator = DistanceCalculator('identity')
    dm = calculator.get_distance(aln)
    
    # Build tree
    constructor = DistanceTreeConstructor()
    tree = constructor.nj(dm)
    
    # Write Newick
    Phylo.write(tree, str(output_newick), "newick")
    
    # Write Nexus
    Phylo.write(tree, str(output_nexus), "nexus")

def _run_raxml(alignment_fasta: Path, output_newick: Path, output_nexus: Path, params: TreeBuilderParams, config: Config, logger):
    # RAxML-NG: raxml-ng --msa input.fa --model GTR+G --prefix job --threads N --bs-trees 100 --all
    
    prefix = str(output_newick.parent / "raxml_run")
    threads = _get_thread_count(params)
    
    cmd = [
        config.RAXML_BINARY,
        "--msa", str(alignment_fasta),
        "--model", params.model,
        "--prefix", prefix,
        "--threads", str(threads),
        "--seed", "12345" # Reproducibility
    ]
    
    if params.bootstrap and params.bootstrap > 0:
        cmd.extend(["--all", "--bs-trees", str(params.bootstrap)])
    else:
        # Just ML search
        cmd.append("--search")
        
    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"
    returncode, stdout, stderr = run_command(cmd, log_file=log_file)
    
    if returncode != 0:
        raise RuntimeError(f"RAxML failed with return code {returncode}. See logs.")
        
    # Output handling
    # RAxML-NG outputs: <prefix>.raxml.bestTree (if no BS) or <prefix>.raxml.support (if BS)
    # We prefer support tree if available.
    
    best_tree = Path(f"{prefix}.raxml.bestTree")
    support_tree = Path(f"{prefix}.raxml.support")
    
    source_tree = support_tree if support_tree.exists() else best_tree
    
    if source_tree.exists():
        shutil.copy(source_tree, output_newick)
        # Convert to Nexus
        _convert_newick_to_nexus(output_newick, output_nexus)
    else:
        raise RuntimeError("RAxML output tree not found.")

def _run_iqtree(alignment_fasta: Path, output_newick: Path, output_nexus: Path, params: TreeBuilderParams, config: Config, logger):
    # iqtree2 -s input.fa -m GTR+G -B 100 -nt AUTO -pre job
    
    prefix = str(output_newick.parent / "iqtree_run")
    threads = _get_thread_count(params)
    
    cmd = [
        config.IQTREE_BINARY,
        "-s", str(alignment_fasta),
        "-m", params.model,
        "-nt", str(threads),
        "-pre", prefix,
        "-redo"
    ]
    
    if params.bootstrap and params.bootstrap > 0:
        cmd.extend(["-B", str(params.bootstrap)])
        
    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"
    returncode, stdout, stderr = run_command(cmd, log_file=log_file)
    
    if returncode != 0:
        raise RuntimeError(f"IQ-TREE failed with return code {returncode}. See logs.")
        
    # Output: <prefix>.treefile (Newick)
    # <prefix>.contree (Consensus if BS) -> usually .treefile is the ML tree, .contree is consensus
    # If -B is used, .treefile is the ML tree, .contree is the consensus tree with support values.
    # We usually want the tree with support values.
    
    treefile = Path(f"{prefix}.treefile")
    contree = Path(f"{prefix}.contree")
    
    source_tree = contree if contree.exists() else treefile
    
    if source_tree.exists():
        shutil.copy(source_tree, output_newick)
        _convert_newick_to_nexus(output_newick, output_nexus)
    else:
        raise RuntimeError("IQ-TREE output tree not found.")

def _run_mrbayes(alignment_fasta: Path, output_newick: Path, output_nexus: Path, params: TreeBuilderParams, config: Config, logger):
    # MrBayes requires Nexus input with a block.
    # 1. Convert FASTA to Nexus
    nexus_input = output_newick.parent / "mrbayes_input.nex"
    _convert_fasta_to_nexus(alignment_fasta, nexus_input)
    
    # 2. Append MrBayes block
    with open(nexus_input, "a") as f:
        f.write("\nbegin mrbayes;\n")
        f.write(f"   set autoclose=yes;\n")
        f.write(f"   lset nst=6 rates=gamma;\n") # GTR+G equivalent
        f.write(f"   mcmc ngen={params.mcmc_generations} nchains={params.mcmc_nchains} nruns={params.mcmc_nruns} burninfrac=0.25;\n")
        f.write(f"   sump;\n")
        f.write(f"   sumt;\n")
        f.write("end;\n")
        
    # 3. Run MrBayes
    # mb input.nex
    # MrBayes is interactive by default, but with input file it should run.
    # We might need to pipe "quit" or ensure it exits? 
    # Usually "set autoclose=yes" helps.
    
    cmd = [config.MRBAYES_BINARY, str(nexus_input)]
    
    log_file = output_newick.parent.parent / "logs" / "tree_builder.log"
    # MrBayes prints to stdout
    returncode, stdout, stderr = run_command(cmd, log_file=log_file)
    
    if returncode != 0:
        raise RuntimeError(f"MrBayes failed with return code {returncode}. See logs.")
        
    # 4. Output: <input>.con.tre (Consensus tree)
    con_tree = Path(f"{nexus_input}.con.tre")
    
    if con_tree.exists():
        shutil.copy(con_tree, output_nexus)
        # Convert Nexus tree to Newick
        _convert_nexus_to_newick(output_nexus, output_newick)
    else:
        raise RuntimeError("MrBayes consensus tree not found.")

def _convert_newick_to_nexus(newick_path: Path, nexus_path: Path):
    if HAS_BIOPYTHON:
        try:
            tree = Phylo.read(str(newick_path), "newick")
            Phylo.write(tree, str(nexus_path), "nexus")
        except Exception:
            pass # Best effort

def _convert_nexus_to_newick(nexus_path: Path, newick_path: Path):
    if HAS_BIOPYTHON:
        try:
            tree = Phylo.read(str(nexus_path), "nexus")
            Phylo.write(tree, str(newick_path), "newick")
        except Exception:
            pass

def _convert_fasta_to_nexus(fasta_path: Path, nexus_path: Path):
    if HAS_BIOPYTHON:
        AlignIO.convert(str(fasta_path), "fasta", str(nexus_path), "nexus", alphabet=None)
    else:
        # Simple fallback if needed, but BioPython is standard
        raise RuntimeError("BioPython required for format conversion.")
