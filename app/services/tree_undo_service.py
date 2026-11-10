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
destroy deliberate user work (MycoMap label refreshes, the focal sequence)
therefore call ``clear_undo_checkpoint()`` instead of leaving a
checkpoint that would silently revert them. ``/tree/selection_sets`` is the one
exception and is left alone on purpose: the viewer saves selections
automatically as part of the normal edit flow (``runBackendAction`` clears the
selection and saves right after every prune), so clearing there would disable
Undo the instant it became useful -- and selection membership is exactly the
kind of state undo should carry back with the edit.

Recompute is never undoable. It also invalidates any checkpoint taken against
the previous topology, so it clears one.

ANNOTATIONS
-----------
Annotation saves are undoable too: they live in ``tree_state.json``, which is
already in the snapshot. Two of their properties need care:

* The viewer debounces colour, opacity and layer-name edits, so one drag of a
  colour picker can arrive as several saves. A save carrying the same
  ``coalesce_key`` as the current checkpoint within
  ``COALESCE_WINDOW_SECONDS`` of it extends that checkpoint
  (``extend_undo_checkpoint()``) instead of replacing it, so Undo takes back
  the whole burst rather than its last half-second. Discrete actions (add,
  delete, membership, layer order) never pass a key and always get their own.
* The label is chosen by the server from ``ANNOTATION_UNDO_LABELS``; the client
  only names a kind. Annotation names are user text and never reach a label.
"""

import json
import logging
import math
import os
import secrets
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.artifact_storage import default_file_mode, discard_gzipped_form

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

# How long after the last save in a burst a following save with the same
# coalesce_key still counts as part of it. Measured from the newest save, not
# the first, so a long continuous colour drag stays one undo step.
COALESCE_WINDOW_SECONDS = 10

# What the viewer may say an annotation save was. The client sends only a key;
# anything unrecognised falls back to the generic label.
ANNOTATION_UNDO_LABELS = {
    "add": "new annotation",
    "edit": "annotation edit",
    "delete": "annotation deletion",
    "members": "annotation membership change",
    "layer_add": "new annotation layer",
    "layer_delete": "annotation layer deletion",
    "layer_order": "annotation layer reorder",
    "layer_visibility": "annotation layer visibility change",
    "layer_style": "annotation layer style change",
    "layer_rename": "annotation layer rename",
}
ANNOTATION_UNDO_DEFAULT_LABEL = "annotation change"
# Kinds the viewer saves on a debounce; only these may coalesce.
ANNOTATION_COALESCING_KINDS = frozenset({"layer_style", "layer_rename"})

# A capture killed between mkdtemp() and the rename leaves its staging directory
# behind, and the weekly reclaim script's globs do not match it. Anything this
# old cannot belong to a request still in flight, so reads sweep it up.
STALE_PENDING_AGE_SECONDS = 3600


def _undo_dir(job_dir: Path) -> Path:
    return Path(job_dir) / UNDO_DIR_NAME


def _make_staging_dir(job_dir: Path, prefix: str) -> Path:
    """``tempfile.mkdtemp()``, but with the mode the rest of a job gets.

    mkdtemp() creates 0700 by design, and the capture's staging directory is
    renamed into place as the live checkpoint, so every ``.tree_undo`` came out
    owner-only in a job directory the ``dikarya`` group otherwise maintains
    (AGENTS.md: "Writing to var/jobs"). ``mkdir(0o777)`` applies the process
    umask instead -- 0775 under the services' ``UMask=0002``, 0755 under the
    usual 0022 -- and the kernel carries the job directory's setgid down, with
    no chmod for the workers' ``RestrictSUIDSGID`` to refuse.
    """
    for _ in range(100):
        candidate = Path(job_dir) / f"{prefix}{secrets.token_hex(4)}"
        try:
            candidate.mkdir(mode=0o777)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"no unused staging name under {job_dir}")


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
        self.coalesce_key: Optional[str] = None
        self.committed = False

    def commit(self, label: Optional[str] = None,
               coalesce_key: Optional[str] = None) -> None:
        """Mark the snapshot as the job's active undo point.

        ``label`` may be refined here, because the honest description of an
        edit ("prune of 18 sequences") is often only known once it has run.
        ``coalesce_key`` lets a later save of the same kind extend this
        checkpoint instead of replacing it (see ``extend_undo_checkpoint()``).
        """
        if label is not None:
            self.label = label
        self.coalesce_key = coalesce_key
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
        staging = _make_staging_dir(job_dir, PENDING_PREFIX)
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
    if handle.coalesce_key:
        manifest["coalesce_key"] = handle.coalesce_key
        manifest["updated_at"] = manifest["created_at"]
    active = _undo_dir(job_dir)
    backup: Optional[Path] = None
    try:
        with open(staging / MANIFEST_NAME, "w") as f:
            json.dump(manifest, f)
        if active.exists():
            backup = job_dir / f"{PENDING_PREFIX}backup.{time.time_ns()}"
            os.replace(active, backup)
        os.replace(staging, active)
    except OSError as exc:
        logger.warning("Could not activate the undo checkpoint: %s", exc)
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists():
            try:
                os.replace(backup, active)
            except OSError as rollback_exc:
                logger.error(
                    "Could not restore the previous undo checkpoint: %s",
                    rollback_exc,
                )
    else:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def extend_undo_checkpoint(job_dir: Path, operation: str, coalesce_key: str,
                           label: Optional[str] = None) -> bool:
    """Fold this save into the current checkpoint if it continues the same burst.

    Returns True when the existing checkpoint was for the same ``operation`` and
    ``coalesce_key`` and was last extended within ``COALESCE_WINDOW_SECONDS``;
    its snapshot (the state from before the burst began) is then kept and only
    its timestamp and label move forward. Returns False, changing nothing, in
    every other case, and the caller takes a fresh checkpoint as usual.

    Must be called with ``tree_state_lock`` held.
    """
    if not coalesce_key:
        return False
    job_dir = Path(job_dir)
    manifest = _read_manifest(job_dir)
    if manifest is None or _checkpoint_is_expired(manifest):
        return False
    if manifest.get("operation") != operation or manifest.get("coalesce_key") != coalesce_key:
        return False
    last = manifest.get("updated_at", manifest.get("created_at"))
    if (
        isinstance(last, bool)
        or not isinstance(last, (int, float))
        or not math.isfinite(last)
    ):
        return False
    now = time.time()
    if not 0 <= now - last <= COALESCE_WINDOW_SECONDS:
        return False

    manifest["updated_at"] = now
    if label is not None:
        manifest["label"] = label
    manifest_path = _undo_dir(job_dir) / MANIFEST_NAME
    temp_name: Optional[str] = None
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=str(manifest_path.parent), prefix=".manifest.", suffix=".tmp"
        )
        # mkstemp creates 0600; keep the manifest's existing mode (AGENTS.md).
        try:
            mode = manifest_path.stat().st_mode & 0o7777
        except OSError:
            mode = default_file_mode()
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(manifest, handle)
        os.replace(temp_name, manifest_path)
    except OSError as exc:
        logger.warning("Could not extend the undo checkpoint: %s", exc)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        return False
    return True


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
            os.chmod(temp_name, default_file_mode())
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


def _restore_snapshot(job_dir: Path, checkpoint: Path,
                      present: List[str], absent: List[str]) -> None:
    """Restore all snapshot paths, rolling every changed path back on failure."""
    rollback = _make_staging_dir(job_dir, PENDING_PREFIX + "restore.")
    paths = list(dict.fromkeys(present + absent))
    previously_present = set()
    changed: List[str] = []
    try:
        # Capture every destination before changing any of them. This makes a
        # failure during staging harmless and gives the restore loop a complete
        # rollback set before its first replacement.
        for relative in paths:
            destination = job_dir / relative
            if destination.is_file():
                shutil.copy2(destination, rollback / _flat_name(relative))
                previously_present.add(relative)

        for relative in present:
            changed.append(relative)
            _restore_file(checkpoint / _flat_name(relative), job_dir / relative)

        for relative in absent:
            changed.append(relative)
            target = job_dir / relative
            if target.is_file():
                target.unlink()
    except BaseException:
        rollback_failed = False
        for relative in reversed(changed):
            destination = job_dir / relative
            try:
                if relative in previously_present:
                    os.replace(rollback / _flat_name(relative), destination)
                elif destination.is_file():
                    destination.unlink()
            except OSError as rollback_exc:
                rollback_failed = True
                logger.error("Could not roll back %s after undo failed: %s",
                             relative, rollback_exc)
        if not rollback_failed:
            shutil.rmtree(rollback, ignore_errors=True)
        raise
    else:
        shutil.rmtree(rollback, ignore_errors=True)


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

    _restore_snapshot(job_dir, checkpoint, present, absent)

    result = {
        "operation": manifest.get("operation") or "edit",
        "label": manifest.get("label") or "the last edit",
    }
    clear_undo_checkpoint(job_dir)
    return result
