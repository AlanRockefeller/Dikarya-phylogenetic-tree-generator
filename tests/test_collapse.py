import sys
import os
import io
import math
from Bio import Phylo

# --- Fix Import Path ---
# Add the parent directory to sys.path so we can import 'app'
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)
# -----------------------

from app.services.tree_edit_service import _collapse_unifurcations

def test_root_collapse():
    print("--- Testing Root Collapse ---")
    # Tree: Root has one child 'A' with length 0.1
    newick = "(A:0.1);" 
    tree = Phylo.read(io.StringIO(newick), "newick")
    
    print(f"Original Root Children: {len(tree.root.clades)}")

    _collapse_unifurcations(tree)

    # 1. Verify Root Identity
    if tree.root.name == "A":
        print("SUCCESS: Root is now 'A'")
    else:
        print(f"FAILURE: Root is '{tree.root.name}'")

    # 2. Verify Root Length (using math.isclose just in case)
    if tree.root.branch_length is not None and math.isclose(tree.root.branch_length, 0.1):
        print(f"SUCCESS: New Root Branch Length is {tree.root.branch_length}")
    else:
        print(f"FAILURE: New Root Branch Length is {tree.root.branch_length}")

def test_internal_collapse():
    print("\n--- Testing Internal Node Collapse ---")
    # Tree: Root -> (Internal -> (A), B)
    # A has length 0.1, Internal has length 0.2. 
    # Logic should remove 'Internal' and give A a length of 0.3.
    newick = "((A:0.1)Internal:0.2, B:1.0)Root;"
    tree = Phylo.read(io.StringIO(newick), "newick")
    
    print("Collapsing...")
    _collapse_unifurcations(tree)
    
    # Find A
    clade_a = next(n for n in tree.find_clades() if n.name == 'A')
    
    # Check branch length with floating point tolerance
    actual_len = getattr(clade_a, "branch_length", 0)
    expected_len = 0.3
    
    if math.isclose(actual_len, expected_len, rel_tol=1e-9):
        print(f"SUCCESS: Branch length summed correctly ({actual_len} ≈ 0.3)")
    else:
        print(f"FAILURE: Branch length is {actual_len} (expected 0.3)")

if __name__ == "__main__":
    test_root_collapse()
    test_internal_collapse()
