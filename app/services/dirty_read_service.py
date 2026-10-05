"""
Suspected dirty read ends: detection only, nothing is ever trimmed.

A low-quality Sanger trace end that was never trimmed shows up as a block of
mismatches and indels at one end of a read whose remainder matches its
relatives closely (job i4h1's HQ328784: ~22 substitutions and a 10 bp indel in
its first ~70 columns, then one difference in the next ~250). Left in, it
lengthens the tip's branch and inflates the variable-column count.

Each row is compared to its closest relatives -- the ``NEIGHBOURS`` most
similar rows at >= ``MIN_IDENTITY`` that also cover most of the read -- not to
the whole alignment: across a genus the ITS1 start differs genuinely between
clades, and a block several sister reads share is biology, not a bad trace.
The coverage rule matters as much: a short read that stops before the dirty
end matches perfectly and cannot vouch for the part in question.

Walking in from each end, a mismatch with the neighbours' majority scores +3
and a match -1, so the running score climbs only while more than 25% of
positions disagree; the end segment runs to the score's peak. An indel counts
once however long it is. A segment is flagged only when the peak rules out a
stray error, it holds at least ``MIN_BASES`` bases, and the rest of the read
is clean -- so a genuinely divergent sequence (different throughout) is not.

The result is computed on the alignment the Alignment Viewer shows (column
ranges refer to it) and served to both viewers, so the tree's marker and the
alignment's hatching cannot disagree. It is cached beside that alignment as
``alignment/dirty_read_ends.json``, keyed by the source file's size and mtime.
Display metadata only: it is never written into the tree, its state or any
download.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

import numpy as np

from app.services.artifact_storage import default_file_mode, open_artifact, resolve_artifact

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
CACHE_NAME = "dirty_read_ends.json"

NEIGHBOURS = 5
MIN_NEIGHBOURS = 2
MIN_IDENTITY = 0.88
# A neighbour must cover this share of the row's bases.
MIN_NEIGHBOUR_COVER = 0.8
MIN_OVERLAP = 100
MIN_COVER = 2
MIN_SCORE = 12
MIN_MISMATCHES = 6
MAX_REST_RATE = 0.08
MIN_REST = 50
# Fewer bases than this at an end is primer/edge noise, not worth a flag.
MIN_BASES = 20
# Above this many pair-columns the neighbour search samples columns.
FULL_SCAN_CELLS = 60e6
# Past this the alignment is not scanned at all (the viewer caps at 2000 rows).
MAX_ROWS = 3000

_GAP = 4
_NONE = -1


def viewer_alignment_path(job_dir: Path):
    """The alignment the Alignment Viewer serves (same order as alignment_view())."""
    alignment_dir = Path(job_dir) / "alignment"
    for name in ("alignment_pruned_aligned.fasta", "alignment_raw.fasta", "aligned.fasta"):
        candidate = alignment_dir / name
        if resolve_artifact(candidate) is not None:
            return candidate
    return None


def _read_fasta(path):
    names, seqs, current = [], [], []
    with open_artifact(path, "rt") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if line.startswith(">"):
                if names:
                    seqs.append("".join(current))
                names.append(line[1:].strip())
                current = []
            elif names:
                current.append(line.strip())
    if names:
        seqs.append("".join(current))
    return names, seqs


def _encode(seqs):
    length = max((len(s) for s in seqs), default=0)
    table = np.full(256, _NONE, dtype=np.int8)
    for ch, code in (("A", 0), ("C", 1), ("G", 2), ("T", 3), ("U", 3)):
        table[ord(ch)] = table[ord(ch.lower())] = code
    table[ord("-")] = table[ord(".")] = _GAP
    raw = np.full((len(seqs), length), ord("-"), dtype=np.uint8)
    for i, s in enumerate(seqs):
        b = s.encode("ascii", "replace")
        raw[i, :len(b)] = np.frombuffer(b, dtype=np.uint8)
    codes = table[raw]
    is_gap = codes == _GAP
    spans = []
    for i in range(len(seqs)):
        data = np.flatnonzero(~is_gap[i])
        spans.append((int(data[0]), int(data[-1]) + 1) if data.size else (0, 0))
        lo, hi = spans[-1]
        # End gaps are missing data, not indels.
        codes[i, :lo] = _NONE
        codes[i, hi:] = _NONE
    return codes, spans


def detect_dirty_ends(names, seqs):
    """Return {name: {"start": seg|None, "end": seg|None}}; seg = range/bases/mismatches/compared."""
    n_rows = len(seqs)
    if n_rows < MIN_NEIGHBOURS + 1 or n_rows > MAX_ROWS:
        return {}
    codes, spans = _encode(seqs)
    length = codes.shape[1]
    if not length:
        return {}

    pair_cells = n_rows * n_rows / 2 * length
    stride = int(np.ceil(pair_cells / FULL_SCAN_CELLS)) if pair_cells > FULL_SCAN_CELLS else 1
    min_overlap = max(10, MIN_OVERLAP // stride)
    sampled = codes[:, ::stride]
    base = (sampled >= 0) & (sampled < _GAP)
    own_bases = base.sum(axis=1)
    base_f = base.astype(np.float32)
    # Pairwise identity over columns where both rows hold a base.
    overlap = base_f @ base_f.T
    same = np.zeros((n_rows, n_rows), dtype=np.float32)
    for code in range(4):
        m = (sampled == code).astype(np.float32)
        same += m @ m.T
    with np.errstate(divide="ignore", invalid="ignore"):
        ident = np.where(overlap >= min_overlap, same / np.maximum(overlap, 1), -1.0)
    np.fill_diagonal(ident, -1.0)
    eligible = (ident >= MIN_IDENTITY) & (overlap >= MIN_NEIGHBOUR_COVER * own_bases[:, None])

    out = {}
    for ri in range(n_rows):
        cands = np.flatnonzero(eligible[ri])
        if cands.size < MIN_NEIGHBOURS:
            continue
        nb = cands[np.argsort(-ident[ri, cands], kind="stable")][:NEIGHBOURS]
        lo, hi = spans[ri]
        own = codes[ri, lo:hi]
        nbc = codes[nb, lo:hi]
        tally = np.stack([(nbc == s).sum(axis=0) for s in range(5)])  # 5 x cols
        total = tally.sum(axis=0)
        best = tally.argmax(axis=0)  # ties -> lowest code, as in a first-max scan
        own_count = np.where(own >= 0, tally[np.clip(own, 0, 4), np.arange(own.size)], 0)
        comparable = (own >= 0) & (total >= MIN_COVER) & ~((own == _GAP) & (best == _GAP))
        bad = (own != best) & (own_count * 2 < total)

        pos, mis = [], []
        in_gap_run = False
        for k in np.flatnonzero(comparable):
            b = bool(bad[k])
            gap_event = b and (own[k] == _GAP or best[k] == _GAP)
            if gap_event and in_gap_run:
                continue
            in_gap_run = gap_event
            pos.append(lo + int(k))
            mis.append(1 if b else 0)
        n = len(pos)
        if n < MIN_REST + 10:
            continue

        def scan(forward):
            score = peak = m = peak_mis = 0
            peak_at = -1
            for k in range(n):
                j = k if forward else n - 1 - k
                if mis[j]:
                    score += 3
                    m += 1
                else:
                    score -= 1
                if score > peak:
                    peak, peak_at, peak_mis = score, k, m
                if score < peak - 30:
                    break
            if peak < MIN_SCORE or peak_mis < MIN_MISMATCHES:
                return None
            rest_len = n - (peak_at + 1)
            if rest_len < MIN_REST:
                return None
            rest = mis[peak_at + 1:] if forward else mis[:n - 1 - peak_at]
            if sum(rest) / rest_len > MAX_REST_RATE:
                return None
            edge = pos[peak_at if forward else n - 1 - peak_at]
            rng = (lo, edge + 1) if forward else (edge, hi)
            bases = sum(1 for ch in seqs[ri][rng[0]:rng[1]] if ch not in "-.")
            if bases < MIN_BASES:
                return None
            return {"range": [int(rng[0]), int(rng[1])], "bases": bases,
                    "mismatches": peak_mis, "compared": peak_at + 1}

        start = scan(True)
        end = scan(False)
        if start or end:
            out[names[ri]] = {"start": start, "end": end}
    return out


def _source_key(stored: Path):
    st = stored.stat()
    return {"source": stored.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "version": CACHE_VERSION}


def _write_cache(path: Path, payload):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dirty_read_ends.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = default_file_mode()
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dirty_read_ends_for_job(job_dir: Path):
    """{"names": {alignment header: {...}}} for the job's viewer alignment, cached."""
    job_dir = Path(job_dir)
    path = viewer_alignment_path(job_dir)
    if path is None:
        return {"names": {}}
    stored = resolve_artifact(path)
    key = _source_key(stored)
    cache_path = job_dir / "alignment" / CACHE_NAME
    try:
        with open_artifact(cache_path, "rt") as handle:
            cached = json.load(handle)
        if isinstance(cached, dict) and cached.get("key") == key and isinstance(cached.get("names"), dict):
            return {"names": cached["names"]}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Ignoring unreadable dirty-read cache %s: %s", cache_path, exc)

    names, seqs = _read_fasta(path)
    found = detect_dirty_ends(names, seqs)
    try:
        _write_cache(cache_path, {"key": key, "names": found})
    except OSError as exc:
        logger.warning("Could not cache dirty-read ends for %s: %s", job_dir.name, exc)
    return {"names": found}
