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

def validate_safe_file_path(path: Path, base_dir: Path) -> bool:
    """
    Ensure path is a real file inside base_dir and not a symlink.
    Returns True if safe, False otherwise.
    
    Checks:
    1. File exists
    2. Is a file (not directory)
    3. Is NOT a symlink (to prevent planting symlinks to /etc/passwd)
    4. Resolves to a path inside base_dir
    """
    try:
        # 1. Must exist (otherwise we can't check what it is)
        if not path.exists():
            return False
            
        # 3. Must NOT be a symlink (check before resolve)
        # We start with this because looking at stats of a symlink that points nowhere is tricky
        if path.is_symlink():
            return False

        # 2. Must be a file
        if not path.is_file():
            return False
            
        # 4. Resolve and check location (prevent traversal via ..)
        resolved_path = path.resolve()
        resolved_base = base_dir.resolve()
        
        # Ensure it is strictly inside
        return resolved_path.is_relative_to(resolved_base)
    except Exception:
        return False


