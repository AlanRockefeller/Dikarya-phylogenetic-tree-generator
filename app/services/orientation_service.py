"""
Sequence orientation service module.

Auto-orients fungal ITS sequences using conserved motifs (ITS1-F, 5.8S core, ITS4).
Based on fixfasta.py by Alan Rockefeller, adapted for pipeline integration.

Key features:
- Uses IUPAC-aware fuzzy matching (≤4 mismatches over 20bp)
- Conservative reversal: requires 5.8S core + at least one flanking motif
- Safe for non-ITS sequences: without strong motif evidence, sequences pass unchanged
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Conserved ITS motifs in forward orientation
FWD_MOTIFS = [
    "TCCGTAGGTGAACCTGCGG",    # ITS1-F  (18S end)
    "GCATCGATGAAGAACGCAGC",   # 5.8S core
    "TCCTCCGCTTATTGATATGC"    # ITS4    (28S start)
]

DEFAULT_MAX_MM = 4   # Per-motif mismatch ceiling
TIE_EPS = 0.3        # Score delta below which we call it a tie
WRAP_WIDTH = 80      # Output FASTA line length

CORE_IDX = 1          # 5.8S core is motif #1
FLANK_IDXS = (0, 2)   # ITS1-F and ITS4
CORE_MAX_MM = 2       # Require a reasonably good 5.8S hit to reverse

# IUPAC complement table
IUPAC_COMP = str.maketrans(
    "ACGTRYMKWSVHDBNacgtrymkwsvhdbn-",
    "TGCAYRKMWSBDHVNtgcayrkmwsbdhvn-"
)

# Precomputed bitmask lookup for IUPAC codes
I2M: Dict[str, int] = {
    **{b: 1 << i for i, b in enumerate("ACGT")},
    "R": 5, "Y": 10, "S": 6, "W": 9,
    "K": 12, "M": 3, "B": 14, "D": 13,
    "H": 11, "V": 7, "N": 15, "-": 0
}
I2M.update({k.lower(): v for k, v in I2M.items()})


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

def revcomp(seq: str) -> str:
    """Return reverse complement of a sequence."""
    return seq.translate(IUPAC_COMP)[::-1]


# Precompute reverse complements of motifs
REV_MOTIFS = [revcomp(m) for m in FWD_MOTIFS]


@dataclass(frozen=True)
class Hit:
    """Represents a motif hit with mismatches and position."""
    mism: int
    pos: int


def best_hit(seq: str, motif: str, max_mm: int = DEFAULT_MAX_MM) -> Optional[Hit]:
    """
    Find best (fewest mismatches, earliest) fuzzy occurrence of motif in seq.

    IMPORTANT: 'N' and '-' are treated as mismatches (no evidence), to avoid
    false motif hits in low-quality/ambiguous regions.
    """
    motif_len = len(motif)
    seq_len = len(seq)
    if seq_len < motif_len:
        return None

    best: Optional[Hit] = None
    seq_upper = seq.upper()
    motif_upper = motif.upper()

    motif_masks = [I2M.get(c, 0) for c in motif_upper]

    for i in range(seq_len - motif_len + 1):
        mism = 0
        for j in range(motif_len):
            c = seq_upper[i + j]

            # Treat ambiguous/unknown as mismatch
            if c == "N" or c == "-":
                mism += 1
                if mism > max_mm:
                    break
                continue

            seq_mask = I2M.get(c, 0)
            if not (seq_mask & motif_masks[j]):
                mism += 1
                if mism > max_mm:
                    break
        else:
            if best is None or (mism, i) < (best.mism, best.pos):
                best = Hit(mism, i)
                if mism == 0:
                    return best

    return best


@dataclass
class OrientationStats:
    """Statistics for orientation decision."""
    hits: List[Hit]
    total_mm: int
    best: Optional[Hit]
    hit_map: Dict[int, Hit]


def collect_stats(seq: str, motifs: List[str], max_mm: int = DEFAULT_MAX_MM) -> OrientationStats:
    """Collect statistics for a set of motifs."""
    hits: List[Hit] = []
    hit_map: Dict[int, Hit] = {}

    for idx, motif in enumerate(motifs):
        hit = best_hit(seq, motif, max_mm)
        if hit:
            hits.append(hit)
            hit_map[idx] = hit

    if not hits:
        return OrientationStats([], float('inf'), None, hit_map={})

    total_mm = sum(h.mism for h in hits)
    best = min(hits, key=lambda h: (h.mism, h.pos))

    return OrientationStats(hits, total_mm, best, hit_map=hit_map)


def has_core_and_flank(stats: OrientationStats) -> bool:
    """Check if stats have both core and at least one flanking motif."""
    return (CORE_IDX in stats.hit_map) and any(i in stats.hit_map for i in FLANK_IDXS)


def decide_orientation(seq: str, max_mm: int = DEFAULT_MAX_MM) -> Tuple[str, OrientationStats, OrientationStats]:
    """
    Determine sequence orientation based on motif matches.

    Returns: (orientation, forward_stats, reverse_stats)
    orientation is one of: "forward", "reverse", "uncertain"
    """
    fwd = collect_stats(seq, FWD_MOTIFS, max_mm)
    rev = collect_stats(seq, REV_MOTIFS, max_mm)

    # No hits at all
    if not fwd.hits and not rev.hits:
        return "uncertain", fwd, rev

    def reverse_allowed() -> bool:
        """Check if reversing is allowed based on conservative criteria."""
        # Check 1: Must have core + at least one flank
        if not has_core_and_flank(rev):
            return False

        # Check 2: Require a decent 5.8S-core hit in the reverse orientation
        rev_core = rev.hit_map.get(CORE_IDX)
        if rev_core is None or rev_core.mism > CORE_MAX_MM:
            return False

        # Check 3: Positional sanity. In a reversed raw sequence:
        # rev(ITS4) [idx 2] should be BEFORE core [idx 1].
        # rev(ITS1-F) [idx 0] should be AFTER core [idx 1].
        if 2 in rev.hit_map and rev.hit_map[2].pos > rev_core.pos:
            return False
        if 0 in rev.hit_map and rev.hit_map[0].pos < rev_core.pos:
            return False

        return True

    # Primary winner selection (hits, then total mismatches, then best hit)
    if len(fwd.hits) != len(rev.hits):
        winner = "forward" if len(fwd.hits) > len(rev.hits) else "reverse"
    elif fwd.total_mm != rev.total_mm:
        winner = "forward" if fwd.total_mm < rev.total_mm else "reverse"
    else:
        # Final tie-breaker: earliest best hit (position scaled)
        assert fwd.best is not None and rev.best is not None
        score_f = fwd.best.mism * 10 + fwd.best.pos / 1000
        score_r = rev.best.mism * 10 + rev.best.pos / 1000

        if abs(score_f - score_r) < TIE_EPS:
            return "uncertain", fwd, rev

        winner = "forward" if score_f < score_r else "reverse"

    # Enforce conservative reversal rule
    if winner == "reverse" and not reverse_allowed():
        # If forward has at least one real hit, keep forward; otherwise uncertain
        return ("forward" if fwd.hits else "uncertain"), fwd, rev

    return winner, fwd, rev


def fasta_reader(fasta_text: str) -> List[Tuple[str, str]]:
    """
    Parse FASTA text into list of (header, sequence) tuples.
    
    Handles stray '>' symbols in sequence lines.
    """
    records: List[Tuple[str, str]] = []
    header: Optional[str] = None
    sequence_parts: List[str] = []
    line_num = 0

    for raw_line in fasta_text.splitlines():
        line_num += 1
        line = raw_line.rstrip()

        if line.startswith(">"):
            # Yield previous sequence if exists
            if header is not None:
                records.append((header, "".join(sequence_parts)))

            # Start new sequence
            header = line[1:].strip() or f"<empty_header_line_{line_num}>"
            sequence_parts = []
        else:
            # Handle stray '>' symbols in sequence lines
            if ">" in line:
                parts = line.split(">")
                sequence_parts.append(parts[0].strip())

                # Each subsequent part becomes a new sequence
                for i, part in enumerate(parts[1:], 1):
                    logger.warning(
                        f"Line {line_num}: Stray '>' found, creating new sequence "
                        f"'{part.strip() or f'<empty_from_stray_{line_num}_{i}'}'"
                    )
                    if header is not None:
                        records.append((header, "".join(sequence_parts)))
                    header = part.strip() or f"<empty_from_stray_{line_num}_{i}>"
                    sequence_parts = []
            else:
                # Normal sequence line
                sequence_parts.append(line.strip())

    # Don't forget the last sequence
    if header is not None:
        records.append((header, "".join(sequence_parts)))

    return records


def format_fasta(header: str, seq: str, wrap_width: int = WRAP_WIDTH) -> str:
    """Format a single FASTA record with wrapped sequence lines."""
    lines = [f">{header}"]
    for chunk in wrap(seq, wrap_width):
        lines.append(chunk)
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Main Service Function
# -----------------------------------------------------------------------------

def fix_sequence_orientation(
    fasta_text: str,
    max_mm: int = DEFAULT_MAX_MM,
) -> Tuple[str, Dict[str, int]]:
    """
    Auto-orient ITS sequences using conserved motifs.
    
    This function analyzes each sequence for ITS motifs and reverse-complements
    sequences that appear to be in the wrong orientation. Non-ITS sequences
    (or sequences without strong motif evidence) pass through unchanged.
    
    Args:
        fasta_text: Input FASTA text content
        max_mm: Maximum mismatches per motif (default: 4)
    
    Returns:
        Tuple of (fixed_fasta_text, stats_dict)
        stats_dict contains: total, forward, reverse, uncertain, empty
    """
    records = fasta_reader(fasta_text)
    
    stats = {
        "total": 0,
        "forward": 0,
        "reverse": 0,
        "uncertain": 0,
        "empty": 0,
    }
    
    output_records: List[str] = []
    reversed_headers: List[str] = []
    uncertain_headers: List[str] = []
    
    for header, seq in records:
        stats["total"] += 1
        
        if not seq:
            stats["empty"] += 1
            logger.warning(f"Empty sequence for '{header}'")
            output_records.append(format_fasta(header, seq))
            continue
        
        # Validate sequence (warn about non-IUPAC characters)
        invalid_chars = re.findall(r"[^ACGTRYMKWSVHDBNacgtrymkwsvhdbn\-]", seq)
        if invalid_chars:
            unique_chars = sorted(set(invalid_chars))
            logger.warning(f"Non-IUPAC symbols in '{header}': {', '.join(unique_chars)}")
        
        # Determine orientation
        orientation, fwd_stats, rev_stats = decide_orientation(seq, max_mm)
        stats[orientation] += 1
        
        # Track reversed and uncertain sequences
        if orientation == "reverse":
            reversed_headers.append(header)
            output_records.append(format_fasta(header, revcomp(seq).upper()))
        elif orientation == "uncertain":
            uncertain_headers.append(header)
            output_records.append(format_fasta(header, seq.upper()))
        else:
            # Forward - keep as is
            output_records.append(format_fasta(header, seq.upper()))
        
        # Log uncertain orientations
        if orientation == "uncertain":
            logger.debug(f"Uncertain orientation for '{header}'")
    
    # Log summary
    if reversed_headers:
        logger.info(f"Reversed {len(reversed_headers)} sequence(s): {reversed_headers[:5]}{'...' if len(reversed_headers) > 5 else ''}")
    
    if uncertain_headers:
        logger.debug(f"Uncertain orientation for {len(uncertain_headers)} sequence(s)")
    
    output_text = "\n".join(output_records) + "\n" if output_records else ""
    
    return output_text, stats


def fix_sequence_orientation_file(
    input_path: Path,
    output_path: Optional[Path] = None,
    max_mm: int = DEFAULT_MAX_MM,
) -> Dict[str, int]:
    """
    Auto-orient ITS sequences in a file.
    
    Args:
        input_path: Path to input FASTA file
        output_path: Path to output FASTA file (defaults to overwriting input)
        max_mm: Maximum mismatches per motif
    
    Returns:
        Stats dictionary with orientation counts
    """
    if output_path is None:
        output_path = input_path
    
    fasta_text = input_path.read_text(encoding="utf-8", errors="replace")
    fixed_text, stats = fix_sequence_orientation(fasta_text, max_mm)
    output_path.write_text(fixed_text, encoding="utf-8")
    
    return stats
