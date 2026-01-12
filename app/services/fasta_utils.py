"""
FASTA utility module.

Provides functions for sanitizing FASTA headers to be compatible with tools
like RAxML, and restoring original headers in output files.
"""

import logging
import re
from pathlib import Path
from typing import Dict

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
    - The sequence has a FASTA header (>description...) on a separate line or same line
    - The sequence has garbage text at the start or end (species name, collection number, notes)
    - The sequence has whitespace/newlines
    
    Algorithm:
    1. Strip FASTA header markers (>) but keep remainder of line
    2. Remove all whitespace
    3. Find the longest contiguous run of valid IUPAC nucleotide characters
    4. Return that run if it meets minimum length, otherwise empty string
    
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
    
    # Process lines: strip > marker but keep rest of each line
    lines = raw_sequence.strip().split('\n')
    processed_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Strip FASTA header marker but keep the rest (may contain sequence)
        if line.startswith(">"):
            # keep only content after first whitespace (if any)
            line = line[1:]
            parts = line.split(None, 1)
            line = parts[1] if len(parts) == 2 else ""

        
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
