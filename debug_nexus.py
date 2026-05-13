from Bio import Phylo
import sys

try:
    path = "/var/www/dikarya/var/jobs/6ef2764f-1344-4c26-9b4c-7a9010ae0667/tree/mrbayes_input.nex.con.tre"
    trees = list(Phylo.parse(path, "nexus"))
    print(f"Found {len(trees)} trees")
    for tree in trees:
        print(f"Tree: {tree}")
        
    Phylo.write(trees, "debug_out.newick", "newick")
    print("Write success")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
