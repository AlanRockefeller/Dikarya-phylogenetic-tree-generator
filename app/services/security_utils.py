import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse


# Valid job_id pattern: UUID4 format only
JOB_ID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.IGNORECASE)

BOOL_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
BOOL_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})

# Maximum length of a FASTA header written into a job's input.
#
# Sequence *data* is already restricted to the IUPAC alphabet by
# validate_dna_fasta, and the tree builders never see a user-controlled name at
# all (sanitize_fasta_headers rewrites every record to SEQ###### first). Headers
# are the one piece of arbitrary user text that reaches a C parser unmodified:
# MAFFT and trimAl both read the submitted names. Without this cap a single
# record could hand them a header the size of the whole request body.
#
# 500 is deliberately the same limit _normalize_sequence_metadata already applies
# to fasta_header, so this cannot desynchronise a header from its metadata: a
# header longer than 500 was already truncated on the metadata side. The longest
# header in 56,502 real production records is 324 characters, so this never
# touches legitimate input.
MAX_FASTA_HEADER_LEN = 500


def coerce_bool(value, default: bool = True) -> Tuple[bool, bool]:
    """Coerce a request value to a bool. Single source of truth for the API,
    api_v1, and worker paths so they can't drift apart.

    Returns ``(result, recognized)``. ``recognized`` is False only when the
    value was a string matching neither the true nor false token set. A
    validating caller can reject it (e.g. HTTP 422), while a lenient caller can
    ignore the flag and fall back to ``default``.
    """
    if value is None:
        return default, True
    if isinstance(value, bool):
        return value, True
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in BOOL_TRUE_TOKENS:
            return True, True
        if clean in BOOL_FALSE_TOKENS:
            return False, True
        return default, False
    return bool(value), True

def validate_job_id(job_id: str) -> bool:
    """Validate job_id is a valid UUID4 format. Prevents directory traversal."""
    if not job_id or not isinstance(job_id, str):
        return False
    return bool(JOB_ID_PATTERN.match(job_id))

def cap_fasta_header(header: str) -> str:
    """Strip control characters from a FASTA header and cap it to a sane length.

    Applied to every record on the way into a job so the alignment and trimming
    binaries never receive an unbounded name. See MAX_FASTA_HEADER_LEN.
    """
    if not header:
        return ""
    cleaned = "".join(ch for ch in header if ord(ch) >= 32 or ch == "\t")
    return cleaned.strip()[:MAX_FASTA_HEADER_LEN].strip()


# The canonical nucleotide alphabet accepted in submitted sequence data: the
# four DNA bases, U for RNA, every IUPAC ambiguity code, and the gap character
# used by pre-aligned input.
#
# Written once and case-folded below rather than as two hand-maintained
# strings. The lowercase half used to be typed out separately and was missing
# w, b, d and h, so a lowercase ambiguity code was silently deleted -- which
# shortens the sequence and shifts every coordinate after it.
NUCLEOTIDE_ALPHABET = "ACGTUNRYKMSWBDHV"
FASTA_SEQUENCE_GAP_CHARS = "-"
ALLOWED_FASTA_SEQUENCE_CHARS = frozenset(
    NUCLEOTIDE_ALPHABET + NUCLEOTIDE_ALPHABET.lower() + FASTA_SEQUENCE_GAP_CHARS
)


def sanitize_fasta_sequence(seq: str) -> str:
    """Remove any non-standard characters from FASTA sequence."""
    # Allow only valid nucleotide characters (either case) and the gap symbol.
    return ''.join(c for c in seq if c in ALLOWED_FASTA_SEQUENCE_CHARS)

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



# Characters a browser will strip or normalize before it ever issues the
# request, which is what makes them dangerous inside a redirect target: the
# string we validate is not the string the browser navigates to.
_REDIRECT_FORBIDDEN_RE = re.compile(r'[\x00-\x20\x7f\\]')


def safe_next_url(next_url: Optional[str]) -> Optional[str]:
    """Return ``next_url`` if it is a same-origin path, else ``None``.

    Single source of truth for every ``?next=`` / ``next`` form field, because
    checking this in two places produced two different answers: the login form
    parsed the URL and rejected a netloc, while the What's New delete form
    accepted anything starting with ``/`` -- so ``//evil.tld/phish`` was an open
    redirect there.

    Rejected:

    * an absolute URL (``https://evil.tld``) -- any scheme or netloc at all;
    * a protocol-relative URL (``//evil.tld``), which browsers treat as absolute;
    * anything containing a backslash (``/\\evil.tld``), which browsers normalize
      to ``/`` before navigating, turning it back into the case above;
    * control characters and whitespace, including the newline that would make
      this usable for header injection.

    A legitimate internal target -- ``/user/jobs``, ``/whats-new?edit=1``,
    ``/job/<uuid>#tree`` -- passes through unchanged.
    """
    if not next_url:
        return None
    candidate = str(next_url)
    if _REDIRECT_FORBIDDEN_RE.search(candidate):
        return None
    if not candidate.startswith('/') or candidate.startswith('//'):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return None
    return candidate
