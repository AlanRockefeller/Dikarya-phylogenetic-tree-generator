"""Reviewable FASTA formatting repairs and retained failed submissions."""
import gzip
import json
import logging
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.services.fasta_utils import (
    FastaValidationError, VALID_DNA_SYMBOLS, analyze_dna_fasta, validate_dna_fasta,
)
from app.services.log_context import _current_context

logger = logging.getLogger(__name__)
ARCHIVE_DIR = Path(__file__).resolve().parents[2] / "var/logs/fasta-rejections"


def record_fasta_rejection(text, analysis, *, source, filename=None):
    """Keep the complete input; archive failures must not mask validation errors."""
    diagnostic = uuid.uuid4().hex
    try:
        now = datetime.now(timezone.utc)
        directory = ARCHIVE_DIR / now.strftime("%Y-%m-%d")
        for folder in (ARCHIVE_DIR, directory):
            folder.mkdir(mode=0o770, parents=True, exist_ok=True)
            info = folder.stat()
            if info.st_uid == os.geteuid():
                # Separate tries: a refused chown (process without the shared
                # group) must not also skip the g+rwx, as it did for the API
                # archive on 2026-11-08 (see api_diagnostics._archive_directory).
                try:
                    group = ARCHIVE_DIR.parent.stat().st_gid
                    if info.st_gid != group:
                        os.chown(folder, -1, group)
                except OSError:
                    pass
                try:
                    folder.chmod(stat.S_IMODE(info.st_mode) | stat.S_ISGID | stat.S_IRWXG)
                except OSError:
                    # RestrictSUIDSGID can refuse setgid even though the folder
                    # is already writable by both service and operator accounts.
                    pass
        context = dict(_current_context())
        from flask import has_request_context, request
        if has_request_context():
            context.update({"path": request.path, "method": request.method,
                            "client_ip": request.remote_addr,
                            "user_agent": request.headers.get("User-Agent")})
        payload = {"id": diagnostic, "captured_at": now.isoformat(),
                   "source": source, "filename": filename, "context": context,
                   "analysis": analysis, "sequence": text}
        fd = os.open(directory / f"{diagnostic}.json.gz",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
        with os.fdopen(fd, "wb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb") as archive:
                archive.write(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
    except Exception:
        logger.exception("event=fasta.diagnostic_write_failed source=%s", source)
        diagnostic = None
    first = analysis["issues"][0]
    logger.warning(
        "event=fasta.rejected diagnostic=%s source=%s code=%s record=%s "
        "line=%s column=%s issues=%s detail=%s",
        diagnostic or "-", source, first["code"], first.get("record_number", "-"),
        first.get("line", "-"), first.get("column", "-"), analysis["issue_count"],
        json.dumps(first, ensure_ascii=True)[:2000],
    )
    return diagnostic


def validate_submitted_fasta(text, *, source, filename=None):
    """Validate an entry point and retain an exact failed submission."""
    try:
        return validate_dna_fasta(text)
    except FastaValidationError as exc:
        exc.diagnostic_id = record_fasta_rejection(
            text, exc.analysis, source=source, filename=filename,
        )
        raise


def _is_dna(text):
    bare = "".join(text.split())
    return bool(bare) and all(c.upper() in VALID_DNA_SYMBOLS for c in bare)


def _looks_like_header_text(text):
    """Name-like text: several symbols that are not DNA, not a stray typo."""
    bare = "".join(text.split())
    invalid = sum(1 for c in bare if c.upper() not in VALID_DNA_SYMBOLS)
    return invalid >= 3 and invalid >= 0.15 * len(bare)


def _shorten(text, limit=60):
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _split_dna_head(line):
    """Split a line into its leading run of DNA symbols and the rest."""
    run = 0
    for position, char in enumerate(line):
        if char.isspace() or char.upper() in VALID_DNA_SYMBOLS:
            run = position + 1
        else:
            break
    return line[:run].strip(), line[run:].strip()


def _header_containing(headers, text):
    """Line number of a header that contains ``text``, if any."""
    for line_number, header in headers:
        if text in header:
            return line_number
    return None


def _repair_header_text_on_sequence_lines(text):
    """Propose fixes for header text that lost its '>' or its line break.

    Three shapes seen in real rejected pastes (one user, nine in 13 minutes):
    a header line with no '>' between two records, stray text before the '>'
    (``HFS>HFSONT74_...``), and a header glued onto the end of the previous
    sequence line (``...AACTTAAONT74_ITS4...``). Each is only proposed when the
    next non-empty line is DNA, so the header gets a sequence, and every change
    is listed for the user to review before applying it.
    """
    lines = text.split("\n")
    out = []
    changes = []

    # Index of the next non-empty line after each line, from one backward pass;
    # slicing the rest of the input per call was quadratic on long pastes.
    next_nonempty = [None] * len(lines)
    following = None
    for position in range(len(lines) - 1, -1, -1):
        next_nonempty[position] = following
        if lines[position].strip():
            following = position

    def next_is_dna(index):
        later_index = next_nonempty[index]
        if later_index is None:
            return False
        later = lines[later_index]
        return not later.strip().startswith(">") and _is_dna(later)

    # Include headers with stray text before their '>' (fixed below).
    headers = [(i + 1, l.split(">", 1)[1].strip()) for i, l in enumerate(lines)
               if ">" in l]
    seen_header = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        number = index + 1
        if not line or line.startswith(">"):
            seen_header = seen_header or line.startswith(">")
            out.append(raw)
            continue
        if ">" in line:
            prefix, rest = line.split(">", 1)
            rest = rest.strip()
            if rest and not _is_dna(rest):
                prefix = prefix.strip()
                if _is_dna(prefix):
                    out.extend([prefix, ">" + rest])
                    changes.append(
                        f"Line {number}: move the header '>{_shorten(rest)}' onto its "
                        "own line; it was pasted onto the end of a sequence line.")
                else:
                    out.append(">" + rest)
                    changes.append(
                        f"Line {number}: remove the stray text '{_shorten(prefix)}' "
                        f"before the header '>{_shorten(rest)}'.")
                seen_header = True
                continue
        if seen_header and not _is_dna(line) and not next_is_dna(index):
            head, tail = _split_dna_head(line)
            source = _header_containing(headers, tail) if len(tail) >= 12 else None
            if len("".join(head.split())) >= 20 and source is not None:
                out.append(head)
                changes.append(
                    f"Line {number}: remove '{_shorten(tail)}' from the end of the "
                    f"sequence; it repeats the header on line {source} and has no "
                    "sequence of its own.")
                continue
        if seen_header and not _is_dna(line) and next_is_dna(index):
            head, tail = _split_dna_head(line)
            if len("".join(head.split())) >= 20 and _looks_like_header_text(tail):
                out.extend([head, ">" + tail])
                changes.append(
                    f"Line {number}: '{_shorten(tail)}' looks like part of a header pasted "
                    "onto the end of a sequence line; move it onto its own header line. "
                    "Check where the name starts: letters that are also DNA symbols "
                    "stay with the sequence.")
                continue
            if _looks_like_header_text(line) and len(line) <= 500:
                out.append(">" + line)
                changes.append(
                    f"Line {number}: '{_shorten(line)}' looks like a header that lost its "
                    "'>'; add it.")
                continue
        out.append(raw)
    return "\n".join(out), changes


def propose_fasta_repair(text):
    """Offer formatting edits without guessing, deleting or substituting bases.

    Line numbers are removed only as separate numeric tokens on a line whose
    remaining tokens are entirely valid DNA. Every proposal needs user approval.
    Unknown punctuation and empty records remain visible as errors.
    """
    changes = []
    candidate = text
    if candidate.startswith("\ufeff"):
        candidate = candidate[1:]
        changes.append("Remove the UTF-8 byte-order mark before the first header.")
    if "\r" in candidate:
        candidate = candidate.replace("\r\n", "\n").replace("\r", "\n")
        changes.append("Normalize line endings.")

    lines = []
    numbered_lines = []
    empty_headers = []
    record_number = 0
    raw_lines = candidate.splitlines()
    nonempty = [i for i, line in enumerate(raw_lines) if line.strip()]
    if len(nonempty) > 1:
        first = nonempty[0]
        header = raw_lines[first].strip()
        following = []
        for index in nonempty[1:]:
            line = raw_lines[index].strip()
            if line.startswith(">"):
                break
            following.append(line)
        # Only suggest the marker when the entire following block is DNA.
        # A DNA-looking first line could itself be sequence, so never rename it.
        if (not header.startswith(">") and not all(c.upper() in VALID_DNA_SYMBOLS for c in re.sub(r"\s+", "", header))
                and following and all(c.upper() in VALID_DNA_SYMBOLS for c in "".join("".join(following).split()))):
            raw_lines[first] = ">" + header
            changes.append(f"Add the missing '>' before the first header on line {first + 1}.")
            candidate = "\n".join(raw_lines)
    candidate, header_changes = _repair_header_text_on_sequence_lines(candidate)
    changes.extend(header_changes)
    existing_headers = {line.strip()[1:].strip().split()[0]
                        for line in candidate.splitlines()
                        if line.strip().startswith(">") and line.strip()[1:].strip()}
    for line_number, raw in enumerate(candidate.splitlines(), 1):
        line = raw.strip()
        if line.startswith(">"):
            record_number += 1
            if not line[1:].strip():
                name = f"sequence_{record_number}"
                while name in existing_headers:
                    name += "_unnamed"
                existing_headers.add(name)
                line = ">" + name
                empty_headers.append(f"line {line_number}: {name}")
        elif line:
            tokens = line.split()
            dna_tokens = [token for token in tokens if not token.isdecimal()]
            # Do not reinterpret embedded digits, prose or punctuation as DNA.
            if (len(tokens) > 1 and dna_tokens and len(dna_tokens) < len(tokens)
                    and all(all(c.upper() in VALID_DNA_SYMBOLS for c in token)
                            for token in dna_tokens)
                    and all(not token.isdecimal() for token in tokens[1:-1])):
                line = "".join(dna_tokens)
                numbered_lines.append(line_number)
            else:
                line = re.sub(r"\s+", "", line)
        lines.append(line)

    if numbered_lines:
        changes.append("Remove separate sequence line-number tokens on line(s): "
                       + ", ".join(map(str, numbered_lines)) + ".")
    if empty_headers:
        changes.append("Name empty headers (" + "; ".join(empty_headers) + ").")
    candidate = "\n".join(lines).strip() + "\n"
    if not any(line.startswith(">") for line in lines):
        bare = "".join(candidate.split())
        if bare and all(c.upper() in VALID_DNA_SYMBOLS for c in bare):
            candidate = ">sequence_1\n" + bare + "\n"
            changes.append("Add the missing FASTA header: >sequence_1.")
    if not changes and candidate != text:
        changes.append("Normalize surrounding whitespace and sequence-line spacing.")
    if candidate == text or not changes:
        return None
    analysis = analyze_dna_fasta(candidate)
    return {"sequence": candidate, "changes": changes,
            "valid": analysis["valid"], "issues": analysis["issues"]}
