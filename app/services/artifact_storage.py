"""
Transparent gzip storage for cold job artifacts.

Several job artifacts are large, highly compressible, and read rarely or never
after the pipeline finishes -- the trimAl HTML report compresses ~43x, the
alignment FASTAs ~30x. Storing them gzipped costs a decompress on the rare read
and saves several GB across the job tree.

Every reader goes through the helpers here instead of bare `open()`, so an
artifact may exist as either `foo.fasta` or `foo.fasta.gz` and the caller does
not care. The plain file always wins when both are present: `compress_artifact`
renames the `.gz` into place *before* unlinking the original, so an interrupted
compression leaves a readable plain file plus a redundant `.gz`, never a
half-written archive.
"""

import gzip
import io
import logging
import os
import shutil
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

GZIP_SUFFIX = ".gz"

# Level 6 is the knee of the curve for this data: the trimAl reports and the
# FASTAs are already 25-45x smaller at 6, and level 9 costs several times the
# CPU for low single-digit percent more.
DEFAULT_COMPRESS_LEVEL = 6

PathLike = Union[str, Path]


def gz_path(path: PathLike) -> Path:
    """The .gz sibling of an artifact path."""
    path = Path(path)
    return path.with_name(path.name + GZIP_SUFFIX)


def resolve_artifact(path: PathLike) -> Optional[Path]:
    """
    Return the path that actually holds this artifact, or None if neither the
    plain file nor its .gz sibling exists.
    """
    path = Path(path)
    if path.is_file():
        return path
    compressed = gz_path(path)
    if compressed.is_file():
        return compressed
    return None


def artifact_exists(path: PathLike) -> bool:
    """True if the artifact is readable in either form."""
    return resolve_artifact(path) is not None


def artifact_size(path: PathLike) -> int:
    """
    Uncompressed size in bytes, or 0 if absent.

    For a gzipped artifact this reads the 4-byte ISIZE trailer rather than
    decompressing, so callers doing the usual `stat().st_size == 0` emptiness
    check stay cheap. ISIZE is modulo 2**32; that only matters for members over
    4 GiB, which these artifacts are not, and an over-4-GiB file will not report
    0 by accident.
    """
    resolved = resolve_artifact(path)
    if resolved is None:
        return 0
    if resolved.suffix != GZIP_SUFFIX:
        return resolved.stat().st_size
    try:
        with open(resolved, "rb") as handle:
            handle.seek(-4, os.SEEK_END)
            return int.from_bytes(handle.read(4), "little")
    except OSError:
        return 0


def open_artifact(path: PathLike, mode: str = "rt", **kwargs):
    """
    Open an artifact for reading, transparently decompressing a .gz sibling.

    Accepts the usual text/binary modes. Read-only by design -- writes must go
    to the plain path so that a later `compress_artifact` is the only thing that
    ever produces a .gz.
    """
    if "r" not in mode or "+" in mode or "w" in mode or "a" in mode:
        raise ValueError(f"open_artifact is read-only, got mode {mode!r}")

    resolved = resolve_artifact(path)
    if resolved is None:
        # Same failure the caller would have got from a bare open().
        raise FileNotFoundError(f"No such artifact: {path}")

    if resolved.suffix != GZIP_SUFFIX:
        return open(resolved, mode, **kwargs)

    if "b" in mode:
        return gzip.open(resolved, "rb", **kwargs)
    kwargs.setdefault("encoding", "utf-8")
    return gzip.open(resolved, "rt", **kwargs)


def read_artifact_bytes(path: PathLike) -> bytes:
    """Whole artifact as bytes, decompressing if needed."""
    with open_artifact(path, "rb") as handle:
        return handle.read()


def read_artifact_text(path: PathLike, encoding: str = "utf-8") -> str:
    """Whole artifact as text, decompressing if needed."""
    with open_artifact(path, "rt", encoding=encoding) as handle:
        return handle.read()


def materialize_artifact(path: PathLike, scratch_dir: PathLike) -> Optional[Path]:
    """
    Return a real uncompressed file for tools that cannot take a file object.

    If the artifact is stored plain, its own path is returned and nothing is
    copied. If it is gzipped, it is decompressed into scratch_dir and that path
    is returned -- the caller owns the temporary copy.
    """
    resolved = resolve_artifact(path)
    if resolved is None:
        return None
    if resolved.suffix != GZIP_SUFFIX:
        return resolved

    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    target = scratch_dir / Path(path).name
    with gzip.open(resolved, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    return target


def compress_artifact(
    path: PathLike,
    level: int = DEFAULT_COMPRESS_LEVEL,
    min_bytes: int = 4096,
) -> Optional[Path]:
    """
    Replace an artifact with a gzipped copy, atomically.

    Returns the .gz path on success, or None when there was nothing to do (file
    absent, already compressed, or below min_bytes where the gzip header would
    eat most of the win). Errors are logged and swallowed: failing to reclaim
    space must never break a pipeline run.
    """
    path = Path(path)
    if not path.is_file():
        return None

    target = gz_path(path)
    try:
        if path.stat().st_size < min_bytes:
            return None

        tmp = target.with_name(target.name + ".tmp")
        with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=level) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)

        # Carry mode across so the web process keeps the access it had; the
        # owner is inherited from the creating process either way.
        shutil.copymode(path, tmp)
        os.replace(tmp, target)
        # Only now is the original redundant.
        path.unlink()
        return target
    except OSError as exc:
        logger.warning("Could not compress artifact %s: %s", path, exc)
        try:
            tmp = target.with_name(target.name + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None


def discard_gzipped_form(path: PathLike) -> int:
    """
    Remove only the .gz sibling, leaving any plain file alone.

    Called before a step rewrites an artifact, so a freshly written plain file
    is never shadowed by a stale compressed copy of the previous run.
    """
    compressed = gz_path(path)
    try:
        if compressed.is_file():
            size = compressed.stat().st_size
            compressed.unlink()
            return size
    except OSError as exc:
        logger.warning("Could not remove stale %s: %s", compressed, exc)
    return 0


_DEFAULT_FILE_MODE: Optional[int] = None


def default_file_mode() -> int:
    """The mode a plain `open(path, "w")` would produce in this process.

    Files written atomically -- write a `tempfile.mkstemp()` file, then
    `os.replace()` it over the target -- do not get this for free: mkstemp
    creates 0600 by design, so the mode has to be set explicitly. Where the
    target already exists its mode is preserved; where it does not, the
    fallback used to be a hardcoded 0644, which silently ignored the service
    UMask. That mattered once the units set `UMask=0002` so the `dikarya`
    group can maintain jobs: every other file in a new job directory came out
    0664 while `tree_state.json` -- created by this path, and then *kept* at
    its existing mode by every later save -- stayed 0644 forever.

    `os.umask()` is a process-wide read-modify-write and Gunicorn runs
    `--threads 8`, so the value is read from /proc rather than toggled: a
    concurrent request creating a file inside the toggle window would get the
    wrong mode. The umask does not change over a process's life, so it is read
    once.
    """
    global _DEFAULT_FILE_MODE
    if _DEFAULT_FILE_MODE is None:
        mode = 0o644
        try:
            with open("/proc/self/status", "r") as status:
                for line in status:
                    if line.startswith("Umask:"):
                        mode = 0o666 & ~int(line.split()[1], 8)
                        break
        except (OSError, ValueError, IndexError):
            pass
        _DEFAULT_FILE_MODE = mode
    return _DEFAULT_FILE_MODE


def discard_artifact(path: PathLike) -> int:
    """
    Delete an artifact in whichever form it exists. Returns bytes reclaimed.

    Used for scratch files that are pure tool input and are never read back.
    """
    total = 0
    for candidate in (Path(path), gz_path(path)):
        try:
            if candidate.is_file():
                total += candidate.stat().st_size
                candidate.unlink()
        except OSError as exc:
            logger.warning("Could not remove %s: %s", candidate, exc)
    return total
