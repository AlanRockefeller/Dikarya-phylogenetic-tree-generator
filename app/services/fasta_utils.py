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


# The unambiguous nucleotides. A run has to contain at least one of these to
# count as DNA -- '-' and 'N' are placeholders for the absence of a base, and a
# string of nothing but placeholders carries no sequence information at all.
CONCRETE_BASES = frozenset("ACGTacgt")


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
       that contains at least one concrete base
    5. Return that run if it meets minimum length, otherwise empty string

    Step 4's "at least one concrete base" is what stops a row of 100 dashes --
    which is entirely composed of valid characters -- from being accepted as a
    sequence and going on to align against real data. The bar is deliberately
    only one A/C/G/T: heavily ambiguous reads are still real reads, and a
    percentage threshold would start throwing away biology.
    
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
    current_has_base = False

    def _consider(start, length, has_base):
        # A run made entirely of gaps and ambiguity codes is not a sequence, so
        # it is not eligible to win -- a shorter run with real bases beside it
        # is the better answer, and no run at all is better than a row of dashes.
        nonlocal best_start, best_length
        if has_base and length > best_length:
            best_start = start
            best_length = length

    for i, c in enumerate(combined):
        if c in valid_chars:
            if current_start is None:
                current_start = i
                current_has_base = False
            if c in CONCRETE_BASES:
                current_has_base = True
        else:
            if current_start is not None:
                _consider(current_start, i - current_start, current_has_base)
                current_start = None
                current_has_base = False
    
    # Check final run (if string ends with valid chars)
    if current_start is not None:
        _consider(current_start, len(combined) - current_start, current_has_base)
    
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
    """Quote a taxon label for Newick/NEXUS.

    Delegates to tree_io.quote_tree_label, which quotes anything that is not
    purely alphanumeric. That is stricter than Newick alone needs, and
    deliberately so: restore_tree_names is called on NEXUS files too, where the
    punctuation set is wider (a bare ``-`` or ``=`` is punctuation) and where an
    unquoted underscore is read as a space. The old rule here covered only
    ``:;,()[]`` and whitespace, which left labels like ``E01-iNatFoltz193`` and
    ``MO142746_2`` unquoted and misparsed.
    """
    from app.services.tree_io import quote_tree_label

    return quote_tree_label(name)


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


# Symbols a sequence line may contain. Shared with the orientation service so
# the pipeline cannot disagree with itself about what counts as valid DNA.
VALID_DNA_SYMBOLS = frozenset("ACGTURYSWKMBDHVN-?")

def parse_fasta_records(fasta_text: str) -> list[tuple[str, str]]:
    """
    Returns list of (header_without_gt, sequence_string_no_whitespace).
    Assumes FASTA headers start with '>'.
    """
    records: list[tuple[str, str]] = []
    header: str | None = None
    seq_chunks: list[str] = []

    for raw_line in fasta_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith(">"):
            # flush previous record
            if header is not None:
                seq = "".join(seq_chunks)
                seq = "".join(seq.split())  # remove ALL whitespace
                records.append((header, seq))

            header = line[1:].strip()
            seq_chunks = []
        else:
            seq_chunks.append(line)

    # flush final record
    if header is not None:
        seq = "".join(seq_chunks)
        seq = "".join(seq.split())
        records.append((header, seq))

    return records

def read_fasta_records(path) -> list[tuple[str, str]]:
    """Parse a FASTA file on disk into ``(header, sequence)`` pairs, in file order.

    The single on-disk entry point, so trimming, ITS extraction and anything
    added later cannot drift into slightly different sequence semantics. Reads
    through artifact_storage, so a cold artifact stored as ``foo.fasta.gz`` is
    handled identically to a plain ``foo.fasta`` (the plain file always wins).
    """
    from app.services.artifact_storage import read_artifact_text

    return parse_fasta_records(read_artifact_text(path))


# Size ceilings for a submitted set. Dikarya aligns barcode markers -- ITS,
# LSU, RPB2 and friends -- and every limit here sits about 2x above the largest
# job that has ever actually produced a tree, measured across all 10,483
# successful runs in var/jobs:
#
#     longest sequence   max observed     21,658 bp   ->  limit  50,000 bp
#     total bases        max observed  3,654,324 bp   ->  limit   8,000,000 bp
#     record count       max observed      2,409      ->  limit   5,000
#
# So nothing that has historically worked is rejected. What they do stop is
# genomic input: MAFFT's cost grows with the product of length and record
# count, so a set of 205 contigs of ~43 kb each cannot finish inside the
# 8-hour step limit no matter how long it is left running. Before this check
# such a job was accepted, occupied the single worker slot for the full 8
# hours, failed, and blocked every other user's queued job behind it -- which
# is exactly what happened on 2026-09-03. Rejecting at submission costs the
# submitter a clear error instead of a wasted day.
MAX_SEQUENCE_LENGTH = 50_000
MAX_TOTAL_BASES = 8_000_000
MAX_RECORD_COUNT = 5_000

# Shared tail for the size errors, so all three tell the user the same story
# about what this site is for.
_SIZE_GUIDANCE = (
    "Dikarya builds trees from barcode markers such as ITS, LSU or RPB2, "
    "which are typically 500-1,500 bp per sequence. Whole-genome, "
    "metagenomic and long-read contig sets cannot be aligned here. Extract "
    "the barcode region first -- the \"ITS Region\" setting on the Tree "
    "Builder page will pull ITS1, ITS2 or the full ITS out of longer "
    "sequences -- then resubmit."
)


def check_fasta_size_limits(records: list[tuple[str, str]]) -> None:
    """Reject a submission too large to align, before it can occupy a worker.

    Raises ``ValueError`` with the same message the API returns to the user.
    """
    if len(records) > MAX_RECORD_COUNT:
        raise ValueError(
            f"This submission has {len(records):,} sequences, above the "
            f"{MAX_RECORD_COUNT:,} limit. Build the tree from a representative "
            "subset instead. " + _SIZE_GUIDANCE
        )

    total_bases = 0
    for index, (header, sequence) in enumerate(records, start=1):
        total_bases += len(sequence)
        if len(sequence) > MAX_SEQUENCE_LENGTH:
            record_name = header or f"record {index}"
            raise ValueError(
                f"FASTA record '{record_name[:100]}' is {len(sequence):,} bp, "
                f"above the {MAX_SEQUENCE_LENGTH:,} bp per-sequence limit. "
                + _SIZE_GUIDANCE
            )

    if total_bases > MAX_TOTAL_BASES:
        raise ValueError(
            f"This submission totals {total_bases:,} bases across "
            f"{len(records):,} sequences, above the {MAX_TOTAL_BASES:,} base "
            "limit. " + _SIZE_GUIDANCE
        )


def validate_dna_fasta(fasta_text: str) -> int:
    """Validate FASTA structure, DNA symbols and size, returning the record count."""
    records = parse_fasta_records(fasta_text)
    if not records:
        raise ValueError(
            "No FASTA records were found. Start each record with a header line "
            "beginning with '>', followed by its DNA sequence on the next line."
        )

    # Checked before the per-symbol scan so an oversized set is rejected
    # without walking every base of it.
    check_fasta_size_limits(records)

    for index, (header, sequence) in enumerate(records, start=1):
        record_name = header or f"record {index}"
        if not header:
            raise ValueError(
                f"FASTA record {index} has an empty header. Add a name after '>', "
                "for example '>sample_1', then retry."
            )
        if not sequence:
            raise ValueError(
                f"FASTA record '{record_name[:100]}' has no DNA sequence. Add the "
                "nucleotide sequence on the line after its header, then retry."
            )

        invalid_symbols = sorted({
            symbol
            for symbol in sequence.upper()
            if symbol not in VALID_DNA_SYMBOLS
        })
        if invalid_symbols:
            shown_symbols = ", ".join(repr(symbol) for symbol in invalid_symbols[:10])
            if len(invalid_symbols) > 10:
                shown_symbols += ", ..."
            raise ValueError(
                f"FASTA record '{record_name[:100]}' contains invalid DNA "
                f"symbol(s): {shown_symbols}. Remove labels, punctuation, and "
                "other non-sequence text from sequence lines. Valid symbols are "
                "A, C, G, T/U, IUPAC ambiguity codes, '-' and '?'."
            )

    return len(records)


# --- Sanitized-ID name maps -------------------------------------------------
#
# NEXUS's interleaved MATRIX block is whitespace-delimited, so a taxon label
# there cannot contain a space -- and MrBayes is stricter still, rejecting most
# punctuation. That is a real restriction of the format, unlike the ones the
# tree files carry with quoting, so the pipeline substitutes the SEQnnnnnn ids
# from sanitize_fasta_headers() before running MrBayes. Those ids then reach
# the user through the "MrBayes Analysis Files" download, where every taxon
# label had been replaced by an opaque number with nothing to decode it.
#
# These two helpers are the decoder ring: the map is written beside the run for
# new jobs, and reconstructed from the alignment for jobs that predate that.

NAME_MAP_FILENAME = "sequence_names.tsv"

NAME_MAP_HEADER = (
    "# MrBayes taxon ids and the sequence names they stand for.\n"
    "# NEXUS matrix labels cannot contain spaces and MrBayes rejects most\n"
    "# punctuation, so the pipeline renames each sequence before the run.\n"
    "# Columns: mrbayes_id<TAB>original_name\n"
)


def format_name_map(mapping: Dict[str, str]) -> str:
    """Render a ``safe_id -> original header`` map as commented TSV text."""
    lines = [NAME_MAP_HEADER]
    for safe_id, original in sorted(mapping.items()):
        # A header cannot contain a newline, but it can contain a tab; the tab
        # is the column separator here, so fold it to a space.
        cleaned = str(original).replace("\t", " ").replace("\n", " ").strip()
        lines.append(f"{safe_id}\t{cleaned}\n")
    return "".join(lines)


def write_name_map(mapping: Dict[str, str], path: Path) -> None:
    """Write a ``safe_id -> original header`` map as a commented TSV."""
    Path(path).write_text(format_name_map(mapping), encoding="utf-8")


def reconstruct_name_map(alignment_path) -> Dict[str, str]:
    """Rebuild the ``SEQnnnnnn -> original header`` map from an alignment file.

    ``sanitize_fasta_headers`` numbers records by position and nothing else, so
    reading the same FASTA back in order reproduces the map exactly. This is
    what recovers the labels for the ~10,000 MrBayes jobs that ran before the
    map was written to disk.
    """
    return {
        f"SEQ{index:06d}": header
        for index, (header, _sequence) in enumerate(
            read_fasta_records(alignment_path), start=1
        )
    }


# INSDC nucleotide accessions come in a small number of fixed shapes, and a
# catch-all (1-6 letters + 5-9 digits) is loose enough to accept things that
# are not accessions at all: an iNaturalist observation id pasted into the
# accession box ("INAT125467754", 4 letters + 9 digits) matched, was sent to
# NCBI, and came back as an opaque 400 that took the rest of its batch down
# with it. Matching the real shapes rejects it here, by name, instead.
#
#   1 letter  + 5 digits            e.g. U49845
#   2 letters + 6 digits            e.g. OR807397, AF123456
#   2 letters + 8 digits            e.g. KY12345678
#   RefSeq: 2 letters + '_' + 6, 8 or 9 digits   e.g. NC_012345, NM_001234567
#   WGS: 4 letters + 2-digit assembly version + 6 or 8 contig digits, so
#        exactly 4+8 or 4+10 e.g. AAAA01000001. Notably never 4+9, which is
#        what keeps the observed iNaturalist id from matching this arm.
#   WGS (6-letter prefix): 6 letters + 2-digit assembly version + 7 or 9
#        contig digits e.g. AAAAAA010000001. No iNaturalist id has ever had
#        six leading letters, so this arm costs nothing to allow -- and
#        without it a perfectly ordinary INSDC accession was rejected as "not
#        an accession" before any NCBI call.
#
# A 4+8 id is genuinely ambiguous -- "INAT12546775" is shape-identical to a
# real WGS accession, and no pattern can separate them. That case still reaches
# NCBI, which is why _fetch_genbank_xml_batch also isolates a failing accession
# rather than letting it void its whole batch.
#
# Lives here rather than in app/api/routes.py so services can use it without
# importing a route module; routes re-exports it under its old private name.
GENBANK_ACCESSION_RE = re.compile(
    r'^(?:'
    r'[A-Z]\d{5}'
    r'|[A-Z]{2}\d{6}'
    r'|[A-Z]{2}\d{8}'
    r'|[A-Z]{2}_\d{6}'
    r'|[A-Z]{2}_\d{8,9}'
    r'|[A-Z]{4}\d{8}'
    r'|[A-Z]{4}\d{10}'
    r'|[A-Z]{6}\d{9}'
    r'|[A-Z]{6}\d{11}'
    r')(?:\.\d+)?$',
    re.IGNORECASE,
)


def is_genbank_accession(text: str) -> bool:
    """Check if text looks like a GenBank accession number."""
    return bool(GENBANK_ACCESSION_RE.match((text or "").strip()))
