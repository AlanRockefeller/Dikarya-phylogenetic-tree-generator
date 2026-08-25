"""Single-level undo for the tree viewer's persisted edits.

WHAT THIS IS
------------
Prune, rename, rotate and the rooting operations all rewrite the same three
files inside a job directory::

    tree_state.json
    tree/tree_pruned.newick
    tree/tree_pruned.nexus

Nothing else on disk is touched by them (verified against every mutation in
``tree_edit_service``: rotate_node, prune_taxa, rename_tip, reroot_tree,
midpoint_root, undo_midpoint_root and apply_rooting_mode all write only that
set), and everything the viewer shows -- topology, pruned membership, renames,
rooting, selection colours, annotations -- is derived from them. The edited
FASTA download is likewise generated on demand from the ORIGINAL input plus
``tree_state.json`` (``build_edited_fasta_text``), so restoring those three
files restores the edited FASTA membership too.

That makes undo a file-level snapshot rather than an inverse operation. Trying
to invert a prune arithmetically would mean rebuilding branches that were
spliced out by ``_collapse_unifurcations()``; copying three files back cannot
get the branch lengths wrong.

ONE CHECKPOINT, NOT A HISTORY
-----------------------------
There is exactly one checkpoint per job. A new supported edit replaces it. It
is deliberately NOT a stack: an arbitrarily deep history of full tree states is
a storage and consistency problem this feature does not need.

The checkpoint is captured BEFORE the mutation and only becomes visible if the
mutation succeeded -- see ``undo_checkpoint()``. A failed edit therefore leaves
whatever checkpoint was already there, rather than replacing a good one with a
snapshot of a state the user never returned to.

WHAT UNDO RESTORES
------------------
The whole of the three files, so the restored state is internally consistent by
construction. That also means metadata written by a NON-undoable endpoint after
the undoable edit would be rolled back with it. Endpoints where that would
destroy deliberate user work (annotations, MycoMap label refreshes, the focal
sequence) therefore call ``clear_undo_checkpoint()`` instead of leaving a
checkpoint that would silently revert them. ``/tree/selection_sets`` is the one
exception and is left alone on purpose: the viewer saves selections
automatically as part of the normal edit flow (``runBackendAction`` clears the
selection and saves right after every prune), so clearing there would disable
Undo the instant it became useful -- and selection membership is exactly the
kind of state undo should carry back with the edit.

Recompute is never undoable. It also invalidates any checkpoint taken against
the previous topology, so it clears one.
"""

import json
import logging
import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.artifact_storage import discard_gzipped_form

logger = logging.getLogger(__name__)

# Directory names are dotted so they sort out of the way and so
# scripts/dikarya_reclaim_job_space.py's globs (tree/*_input_sanitized.fasta,
# .recompute-*) cannot match them.
UNDO_DIR_NAME = ".tree_undo"
PENDING_PREFIX = ".tree_undo.pending."
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

# Job-directory-relative paths covered by a checkpoint, in restore order.
SNAPSHOT_PATHS = (
    "tree_state.json",
    "tree/tree_pruned.newick",
    "tree/tree_pruned.nexus",
)

# A checkpoint is an "oops, undo that" affordance, not an archive. Past this
# age it is dropped on the next read rather than offering to restore a tree the
# user stopped thinking about a week ago.
MAX_CHECKPOINT_AGE_SECONDS = 7 * 24 * 3600

# A capture killed between mkdtemp() and the rename leaves its staging directory
# behind, and the weekly reclaim script's globs do not match it. Anything this
# old cannot belong to a request still in flight, so reads sweep it up.
STALE_PENDING_AGE_SECONDS = 3600


def _undo_dir(job_dir: Path) -> Path:
    return Path(job_dir) / UNDO_DIR_NAME


def _flat_name(relative: str) -> str:
    """Map 'tree/tree_pruned.newick' onto a flat name inside the checkpoint."""
    return relative.replace("/", "__")


def _read_manifest(job_dir: Path) -> Optional[Dict[str, Any]]:
    manifest_path = _undo_dir(job_dir) / MANIFEST_NAME
    try:
        with open(manifest_path, "r") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("version") != MANIFEST_VERSION:
        return None
    return manifest


def _checkpoint_is_expired(manifest: Dict[str, Any]) -> bool:
    """Apply the one checkpoint-age policy used by describe and restore."""
    created_at = manifest.get("created_at")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(created_at)
    ):
        return True
    return time.time() - created_at > MAX_CHECKPOINT_AGE_SECONDS


def clear_undo_checkpoint(job_dir: Path) -> bool:
    """Drop the job's checkpoint. Returns True if one was actually removed."""
    target = _undo_dir(job_dir)
    if not target.exists():
        return False
    # Renaming first means a concurrent reader either sees the whole checkpoint
    # or none of it, never a directory being emptied underneath it.
    doomed = Path(job_dir) / f"{PENDING_PREFIX}discard.{time.time_ns()}"
    try:
        os.replace(target, doomed)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()
    shutil.rmtree(doomed, ignore_errors=True)
    return True


def _sweep_stale_pending(job_dir: Path) -> None:
    """Remove staging directories left behind by an interrupted capture."""
    cutoff = time.time() - STALE_PENDING_AGE_SECONDS
    try:
        candidates = list(Path(job_dir).glob(PENDING_PREFIX + "*"))
    except OSError:
        return
    for stale in candidates:
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue


def describe_undo_checkpoint(job_dir: Path) -> Dict[str, Any]:
    """Report what a caller's Undo would restore, if anything.

    Expired checkpoints are removed here rather than offered, so simply opening
    the viewer eventually reclaims them.
    """
    _sweep_stale_pending(job_dir)
    manifest = _read_manifest(job_dir)
    if manifest is None:
        # A directory with no readable manifest is debris from an interrupted
        # capture; nothing can be restored from it.
        if _undo_dir(job_dir).exists():
            clear_undo_checkpoint(job_dir)
        return {"available": False}

    created_at = manifest.get("created_at")
    if _checkpoint_is_expired(manifest):
        clear_undo_checkpoint(job_dir)
        return {"available": False}

    return {
        "available": True,
        "operation": manifest.get("operation") or "edit",
        "label": manifest.get("label") or "the last edit",
        "created_at": created_at,
    }


class _PendingCheckpoint:
    """Handle yielded by ``undo_checkpoint()``.

    Nothing it captured becomes reachable until ``commit()`` is called AND the
    ``with`` block exits without an exception.
    """

    def __init__(self, job_dir: Path, staging: Path, operation: str, label: str):
        self.job_dir = Path(job_dir)
        self.staging = staging
        self.operation = operation
        self.label = label
        self.committed = False

    def commit(self, label: Optional[str] = None) -> None:
        """Mark the snapshot as the job's active undo point.

        ``label`` may be refined here, because the honest description of an
        edit ("prune of 18 sequences") is often only known once it has run.
        """
        if label is not None:
            self.label = label
        self.committed = True

    def cancel(self) -> None:
        """Abandon the snapshot and leave any existing checkpoint in place."""
        self.committed = False


@contextmanager
def undo_checkpoint(job_dir: Path, operation: str, label: str):
    """Snapshot the undoable files, then publish the snapshot only on success.

    Must be called with ``tree_state_lock`` held and AFTER ``load_tree_state()``,
    which may itself initialize (and midpoint-root) a job that has no state yet.
    Snapshotting before that would capture a job directory the viewer never
    showed anybody.

    A failure to capture is never fatal: the edit still runs, and Undo is simply
    not offered for it. Losing the ability to undo is a smaller harm than
    failing a prune the user asked for.
    """
    job_dir = Path(job_dir)
    staging: Optional[Path] = None
    handle: Optional[_PendingCheckpoint] = None
    try:
        staging = Path(tempfile.mkdtemp(dir=str(job_dir), prefix=PENDING_PREFIX))
        present: List[str] = []
        absent: List[str] = []
        for relative in SNAPSHOT_PATHS:
            source = job_dir / relative
            if source.is_file():
                shutil.copy2(source, staging / _flat_name(relative))
                present.append(relative)
            else:
                # Recorded explicitly: the FIRST prune on an outgroup-rooted job
                # creates tree_pruned.newick, and undoing it has to delete that
                # file again so _editable_tree_input_path() falls back to
                # tree_original.newick instead of replaying the pruned tree.
                absent.append(relative)
        manifest = {
            "version": MANIFEST_VERSION,
            "operation": operation,
            "label": label,
            "created_at": time.time(),
            "present": present,
            "absent": absent,
        }
        handle = _PendingCheckpoint(job_dir, staging, operation, label)
    except OSError as exc:
        logger.warning("Could not capture an undo checkpoint: %s", exc)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        yield _PendingCheckpoint(job_dir, job_dir / "unused", operation, label)
        return

    try:
        yield handle
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if not handle.committed:
        shutil.rmtree(staging, ignore_errors=True)
        return

    manifest["label"] = handle.label
    try:
        with open(staging / MANIFEST_NAME, "w") as f:
            json.dump(manifest, f)
        clear_undo_checkpoint(job_dir)
        os.replace(staging, _undo_dir(job_dir))
    except OSError as exc:
        logger.warning("Could not activate the undo checkpoint: %s", exc)
        shutil.rmtree(staging, ignore_errors=True)


class UndoUnavailable(RuntimeError):
    """There is nothing to undo, or the checkpoint cannot be trusted."""


def _restore_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(destination.parent), prefix=".tree_undo_restore.", suffix=".tmp"
    )
    os.close(fd)
    try:
        shutil.copyfile(source, temp_name)
        try:
            os.chmod(temp_name, destination.stat().st_mode & 0o7777)
        except OSError:
            os.chmod(temp_name, 0o644)
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    # A stale .gz would otherwise keep shadowing the file we just put back,
    # because artifact_storage resolves the compressed form when the plain one
    # is missing and the plain one wins only while it exists.
    discard_gzipped_form(destination)


def undo_last_edit(job_dir: Path) -> Dict[str, Any]:
    """Restore the checkpoint and consume it.

    Must be called with ``tree_state_lock`` held. Returns
    ``{"operation", "label", "tree_state"}``. Raises ``UndoUnavailable`` when
    there is nothing to restore.
    """
    job_dir = Path(job_dir)
    manifest = _read_manifest(job_dir)
    if manifest is None:
        raise UndoUnavailable("There is nothing to undo.")
    if _checkpoint_is_expired(manifest):
        clear_undo_checkpoint(job_dir)
        raise UndoUnavailable("The saved undo point has expired.")

    checkpoint = _undo_dir(job_dir)
    present = [p for p in manifest.get("present") or [] if p in SNAPSHOT_PATHS]
    absent = [p for p in manifest.get("absent") or [] if p in SNAPSHOT_PATHS]

    if "tree_state.json" not in present:
        # Without the state there is nothing coherent to go back to; the tree
        # files alone would leave renames and pruned membership describing a
        # topology that no longer matches.
        clear_undo_checkpoint(job_dir)
        raise UndoUnavailable("The saved undo point is incomplete.")

    for relative in present:
        source = checkpoint / _flat_name(relative)
        if not source.is_file():
            # Verified up front so a partial restore is impossible, and cleared
            # because a checkpoint missing one of its own files can never be
            # applied -- leaving it would keep offering an Undo that only fails.
            clear_undo_checkpoint(job_dir)
            raise UndoUnavailable("The saved undo point is incomplete.")

    for relative in present:
        _restore_file(checkpoint / _flat_name(relative), job_dir / relative)

    for relative in absent:
        target = job_dir / relative
        try:
            if target.is_file():
                target.unlink()
        except OSError as exc:
            logger.warning("Could not remove %s during undo: %s", relative, exc)

    result = {
        "operation": manifest.get("operation") or "edit",
        "label": manifest.get("label") or "the last edit",
    }
    clear_undo_checkpoint(job_dir)
    return result
