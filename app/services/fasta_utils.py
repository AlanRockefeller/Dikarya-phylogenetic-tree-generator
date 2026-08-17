"""
FASTA utility module.

Provides functions for sanitizing FASTA headers to be compatible with tools
like RAxML, and restoring original headers in output files.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List

try:
    from Bio import SeqIO
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)


def clean_dna_sequence(raw_sequence: str, min_length: int = 100) -> str:
    """
    Clean a DNA sequence by extracting the longest contiguous run of valid nucleotides.
    
    This handles cases where:
    - The sequence has a FASTA header (>description...) on a separate line
    - A malformed one-line FASTA has a clearly separated sequence after its header
    - The sequence has garbage text at the start or end (species name, collection number, notes)
    - The sequence has whitespace/newlines
    
    Algorithm:
    1. Discard normal FASTA header lines
    2. Conservatively recover a sequence from a malformed one-line FASTA
    3. Remove all whitespace
    4. Find the longest contiguous run of valid IUPAC nucleotide characters
    5. Return that run if it meets minimum length, otherwise empty string
    
    Args:
        raw_sequence: Raw DNA sequence string that may contain non-DNA text
        min_length: Minimum length for a valid barcode (default 100bp for ITS)
        
    Returns:
        Cleaned DNA sequence containing only valid IUPAC nucleotide characters,
        or empty string if no valid run of sufficient length is found
    """
    if not raw_sequence:
        return ""
    
    # Valid IUPAC nucleotide characters (DNA + ambiguity codes + gap)
    valid_chars = set("ACGTRYSWKMBDHVNacgtryswkmbdhvn-")
    
    # Process non-empty lines so normal FASTA headers never become sequence data.
    lines = [line.strip() for line in raw_sequence.strip().splitlines() if line.strip()]
    processed_lines = []

    for line in lines:
        if line.startswith(">"):
            # Only recover same-line sequence data when this is the entire input,
            # a whitespace boundary separates it from the identifier, and the
            # whole suffix is long enough to be sequence-like. A directly glued
            # suffix is ambiguous and must not bleed into the sequence.
            if len(lines) == 1:
                parts = line[1:].strip().split(None, 1)
                if len(parts) == 2:
                    candidate = ''.join(parts[1].split())
                    recovery_min_length = max(min_length, 20)
                    if (
                        len(candidate) >= recovery_min_length
                        and all(char in valid_chars for char in candidate)
                    ):
                        processed_lines.append(parts[1])
            continue
        processed_lines.append(line)
    
    # Join all lines and remove whitespace
    combined = ''.join(''.join(line.split()) for line in processed_lines)
    
    if not combined:
        return ""
    
    # Find the longest contiguous run of valid DNA characters
    # This handles both prefix AND suffix garbage efficiently in O(n)
    best_start = 0
    best_length = 0
    current_start = None
    
    for i, c in enumerate(combined):
        if c in valid_chars:
            if current_start is None:
                current_start = i
        else:
            if current_start is not None:
                run_length = i - current_start
                if run_length > best_length:
                    best_start = current_start
                    best_length = run_length
                current_start = None
    
    # Check final run (if string ends with valid chars)
    if current_start is not None:
        run_length = len(combined) - current_start
        if run_length > best_length:
            best_start = current_start
            best_length = run_length
    
    # Extract the best run if it meets minimum length
    if best_length >= min_length:
        cleaned = combined[best_start:best_start + best_length]
        return cleaned.upper()
    
    # No valid run of sufficient length found
    return ""


def describe_degenerate_input(sequence_text: str, *, accession_count: int = 0,
                              blast_mode: str = "auto") -> List[str]:
    """Return user-facing warnings for input that cannot produce an informative tree.

    Two identical sequences make FastTree emit ``(A:0.0,B:0.0);`` -- a tree with
    no branch lengths, which cannot be midpoint rooted and tells the submitter
    nothing. That used to run the full pipeline and hand back a blank-looking
    result with no explanation. These checks run at submission so the warning
    arrives before the wait, not after it.

    BLAST is only a rescue for a *single* query (see ``_should_blast_single_only``
    in ``app/workers/tasks.py``): it is never run for multi-sequence input, so a
    two-sequence submission stays two sequences no matter what the mode is.
    """
    sequences = []
    for line in (sequence_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            sequences.append([])
        elif sequences:
            sequences[-1].append(line)

    count = len(sequences) or max(int(accession_count or 0), 0)
    if count == 0:
        # Empty input is rejected upstream; nothing useful to add here.
        return []

    if count == 1:
        if (blast_mode or "auto").strip().lower() != "off":
            # BLAST will add homologs, so one query is a normal submission.
            return []
        return [
            "Only one sequence was submitted and BLAST is turned off, so there is "
            "nothing to compare it against. A tree needs at least three sequences "
            "to show any relationship."
        ]

    warnings = []
    if count == 2:
        warnings.append(
            "Only two sequences were submitted. There is just one possible tree for "
            "two sequences, so the result cannot show any grouping. Add a third "
            "sequence (an outgroup or a reference) to get an informative tree."
        )

    # Compare on bases alone: gaps and case differ between import sources without
    # making the sequences meaningfully different.
    normalized = {
        re.sub(r"[^A-Z]", "", "".join(chunks).upper()) for chunks in sequences
    }
    normalized.discard("")
    if len(normalized) == 1 and len(sequences) > 1:
        warnings.append(
            f"All {count} submitted sequences are identical. Every branch length "
            "will be zero, so the tree cannot be rooted at its midpoint and its "
            "shape carries no information."
        )

    return warnings


def sanitize_fasta_headers(input_path: Path, output_path: Path) -> Dict[str, str]:
    """
    Read a FASTA file, rename sequences to safe IDs (e.g., SEQ0001),
    write the sanitized FASTA to output_path, and return a mapping
    of safe_id -> original_header.

    Args:
        input_path: Path to the original FASTA file.
        output_path: Path to write the sanitized FASTA file.

    Returns:
        Dictionary mapping sanitized IDs to original headers.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython is required for FASTA sanitization.")

    mapping = {}
    
    # We use SeqIO to read/write to handle multiline sequences gracefully
    records = []
    
    try:
        # Use existing format if possible, otherwise generic fasta
        # We'll just assume generic fasta
        original_records = list(SeqIO.parse(str(input_path), "fasta"))
        
        for i, record in enumerate(original_records):
            original_header = record.description
            # Create a safe ID. Using a simple counter format.
            safe_id = f"SEQ{i+1:06d}"
            
            mapping[safe_id] = original_header
            
            # Update record
            record.id = safe_id
            record.description = ""  # Clear description to avoid extra text in header
            record.name = safe_id
            
            records.append(record)
            
        SeqIO.write(records, str(output_path), "fasta")
        
        logger.info(f"Sanitized {len(records)} sequences in {input_path}")
        return mapping

    except Exception as e:
        logger.error(f"Failed to sanitize FASTA headers: {e}")
        raise


def _quote_newick_name(name: str) -> str:
    """
    Quote a name for Newick format if it contains special characters.
    Standard Newick quoting uses single quotes, with internal single quotes doubled.
    """
    # Characters that definitely require quoting in Newick
    # Whitespace, colon, semicolon, comma, parens, brackets
    special_chars = set(":;,()[] ")
    
    # Check if quoting is needed
    if any(c in special_chars for c in name) or "'" in name:
        # Escape existing single quotes
        escaped_name = name.replace("'", "''")
        return f"'{escaped_name}'"
        
    return name


def restore_tree_names(tree_path: Path, mapping: Dict[str, str]) -> None:
    """
    Read a tree file (Newick, Nexus, etc.) as text, replace safe IDs
    with their original counterparts, and overwrite the file.
    
    Uses token-aware replacement (regex mapping) and ensures correct 
    Newick quoting for names containing special characters.

    Args:
        tree_path: Path to the tree file.
        mapping: Dictionary mapping safe IDs to original headers.
    """
    if not tree_path.exists():
        logger.warning(f"Tree file not found for name restoration: {tree_path}")
        return

    try:
        content = tree_path.read_text()
        
        # Helper for regex replacement
        def replace_match(match):
            safe_id = match.group(0)
            if safe_id in mapping:
                return _quote_newick_name(mapping[safe_id])
            return safe_id

        # Replace all occurrences of SEQxxxxxx using a regex
        # We match word boundaries (\b) to ensure we don't partial-match
        # Pattern matches SEQ followed by one or more digits
        new_content = re.sub(r"\bSEQ\d+\b", replace_match, content)
            
        tree_path.write_text(new_content)
        logger.info(f"Restored names in {tree_path}")

    except Exception as e:
        logger.error(f"Failed to restore tree names in {tree_path}: {e}")
        raise
