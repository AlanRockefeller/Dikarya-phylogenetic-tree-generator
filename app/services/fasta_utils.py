"""
FASTA utility module.

Provides functions for sanitizing FASTA headers to be compatible with tools
like RAxML, and restoring original headers in output files.
"""

import logging
import re
from pathlib import Path
from typing import Dict, Tuple

try:
    from Bio import AlignIO, SeqIO
    HAS_BIOPYTHON = True
except ImportError:
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)


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
        new_content = re.sub(r'\bSEQ\d+\b', replace_match, content)
            
        tree_path.write_text(new_content)
        logger.info(f"Restored names in {tree_path}")

    except Exception as e:
        logger.error(f"Failed to restore tree names in {tree_path}: {e}")
        raise
