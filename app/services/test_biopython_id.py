
from Bio import AlignIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Align import MultipleSeqAlignment
from io import StringIO

# Test Case 1: Space in header
fasta_str = """>seq1 Organism Name
ATGC
>seq2 Another Organism
ATGC
"""

print("--- Reading FASTA with AlignIO ---")
aln = AlignIO.read(StringIO(fasta_str), "fasta")
for record in aln:
    print(f"ID: '{record.id}'")
    print(f"Description: '{record.description}'")
    print(f"Name: '{record.name}'")

print("\n--- Writing default ---")
out = StringIO()
AlignIO.write(aln, out, "fasta")
print(out.getvalue())

# Test Case 2: Underscores
fasta_str_under = """>seq1_Organism_Name
ATGC
>seq2_Another_Organism
ATGC
"""
print("\n--- Reading Underscored FASTA ---")
aln_under = AlignIO.read(StringIO(fasta_str_under), "fasta")
for record in aln_under:
    print(f"ID: '{record.id}'")

print("\n--- Summary ---")
if aln[0].id == "seq1":
    print("Alignment ID assumes truncation at space.")
else:
    print("Alignment ID preserves whole header.")
