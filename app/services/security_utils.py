import re
from pathlib import Path
from typing import Optional, Tuple

# Valid job_id pattern: UUID4 format only
JOB_ID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.IGNORECASE)

def validate_job_id(job_id: str) -> bool:
    """Validate job_id is a valid UUID4 format. Prevents directory traversal."""
    if not job_id or not isinstance(job_id, str):
        return False
    return bool(JOB_ID_PATTERN.match(job_id))

def sanitize_fasta_sequence(seq: str) -> str:
    """Remove any non-standard characters from FASTA sequence."""
    # Allow only valid nucleotide/amino acid characters
    allowed = set('ACGTUNRYKMSWBDHVacgtunrykmsv-')
    return ''.join(c for c in seq if c in allowed)

def validate_blast_query(query: str) -> Tuple[bool, Optional[str]]:
    """Validate BLAST query for dangerous patterns."""
    if not query:
        return False, "Empty query"
    
    # Check for suspicious shell-like patterns
    dangerous_patterns = [';', '|', '`', '$', '&&', '||', '\n', '\r']
    for pattern in dangerous_patterns:
        if pattern in query:
            return False, f"Query contains invalid character: {pattern}"
    
    return True, None
