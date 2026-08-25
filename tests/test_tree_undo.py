"""Single-level undo for the tree viewer's persisted edits.

Undo restores a file-level snapshot (tree_state.json plus the editable
tree_pruned.* pair) taken immediately before a supported edit. The point of
these tests is that the snapshot is only ever published when the edit it guards
actually succeeded, that restoring it puts EVERY user-visible derived thing back
-- topology, pruned membership, renames, rooting, edited FASTA -- and that
operations which would make the snapshot misleading clear it instead of leaving
a broken Undo on screen.
"""

import json
import os
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from Bio import Phylo
from flask import Flask, g

from app.services.tree_edit_service import (
    _stable_internal_node_id,
    build_edited_fasta_text,
    load_tree_state,
    save_tree_state,
)
from app.services.tree_io import write_tree_file
from app.services.tree_undo_service import (
    SNAPSHOT_PATHS,
    UNDO_DIR_NAME,
    UndoUnavailable,
    clear_undo_checkpoint,
    describe_undo_checkpoint,
    undo_checkpoint,
    undo_last_edit,
)

JOB_ID = "12345678-1234-4234-8234-123456789abc"
TREE = "((A:0.1,B:0.2)AB:0.3,(C:0.4,D:0.5)CD:0.6)ROOT:0.0;\n"
FASTA = ">A\nAAAA\n>B\nCCCC\n>C\nGGGG\n>D\nTTTT\n"


def _make_job(root: Path, *, with_pruned_files: bool = True) -> Path:
    """A job directory in the shape the tree-edit endpoints expect."""
    job_dir = root / JOB_ID
    (job_dir / "tree").mkdir(parents=True)
    (job_dir / "input").mkdir(parents=True)
    tree = Phylo.read(StringIO(TREE), "newick")
    write_tree_file(tree, str(job_dir / "tree" / "tree_original.newick"), "newick")
    if with_pruned_files:
        write_tree_file(tree, str(job_dir / "tree" / "tree_pruned.newick"), "newick")
        write_tree_file(tree, str(job_dir / "tree" / "tree_pruned.nexus"), "nexus")
    (job_dir / "input" / "input_raw.fasta").write_text(FASTA)
    save_tree_state(job_dir, {
        "current_tree": "pruned" if with_pruned_files else "original",
        "tree_structure": {
            "name": "ROOT",
            "original_name": "ROOT",
            "children": [
                {"name": "AB", "original_name": "AB", "children": [
                    {"name": "A", "original_name": "A"},
                    {"name": "B", "original_name": "B"},
                ]},
                {"name": "CD", "original_name": "CD", "children": [
                    {"name": "C", "original_name": "C"},
                    {"name": "D", "original_name": "D"},
                ]},
            ],
        },
        "pruned_taxa": [],
        "renames": {},
        "root": None,
        "root_mode": "MIDPOINT",
        "is_midpoint_rooted": True,
    })
    return job_dir


def _tip_names(path: Path):
    return {tip.name for tip in Phylo.read(str(path), "newick").get_terminals()}


def _edited_fasta_headers(job_dir: Path):
    text = build_edited_fasta_text(
        job_dir / "input" / "input_raw.fasta", load_tree_state(job_dir)
    )
    return [line[1:] for line in text.splitlines() if line.startswith(">")]


# --------------------------------------------------------------------------
# The checkpoint itself
# --------------------------------------------------------------------------
class CheckpointLifecycleTests(unittest.TestCase):
    def test_a_fresh_job_has_nothing_to_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})
            with self.assertRaises(UndoUnavailable):
                undo_last_edit(job_dir)

    def test_a_committed_edit_leaves_exactly_one_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            with undo_checkpoint(job_dir, "prune", "the last prune") as checkpoint:
                checkpoint.commit("prune of 2 sequences")

            described = describe_undo_checkpoint(job_dir)
            self.assertTrue(described["available"])
            self.assertEqual(described["operation"], "prune")
            self.assertEqual(described["label"], "prune of 2 sequences")
            # One directory, not a stack of them.
            self.assertEqual(
                [p.name for p in job_dir.iterdir() if p.name.startswith(".tree_undo")],
                [UNDO_DIR_NAME],
            )

    def test_a_second_edit_replaces_the_single_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            state = load_tree_state(job_dir)
            state["renames"] = {"A": "first"}
            with undo_checkpoint(job_dir, "rename", "rename") as checkpoint:
                save_tree_state(job_dir, state)
                checkpoint.commit()

            state["renames"] = {"A": "second"}
            with undo_checkpoint(job_dir, "rotate", "node rotation") as checkpoint:
                save_tree_state(job_dir, state)
                checkpoint.commit()

            self.assertEqual(describe_undo_checkpoint(job_dir)["operation"], "rotate")
            # Undo must land on the state saved by the FIRST edit, i.e. one step
            # back, not two.
            undo_last_edit(job_dir)
            self.assertEqual(load_tree_state(job_dir)["renames"], {"A": "first"})

    def test_a_failed_edit_does_not_replace_a_valid_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            with undo_checkpoint(job_dir, "prune", "prune of 1 sequence") as checkpoint:
                checkpoint.commit()

            with self.assertRaises(RuntimeError):
                with undo_checkpoint(job_dir, "rename", "rename") as checkpoint:
                    raise RuntimeError("the edit blew up")

            described = describe_undo_checkpoint(job_dir)
            self.assertTrue(described["available"])
            self.assertEqual(described["label"], "prune of 1 sequence")
            self.assertFalse(
                [p for p in job_dir.iterdir() if p.name.startswith(".tree_undo.pending")],
                "an abandoned capture left staging behind",
            )

    def test_an_uncommitted_edit_does_not_replace_a_valid_checkpoint(self):
        # A handler can decide the edit was a no-op and simply never commit.
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            with undo_checkpoint(job_dir, "prune", "prune of 1 sequence") as checkpoint:
                checkpoint.commit()
            with undo_checkpoint(job_dir, "prune", "prune of 0 sequences"):
                pass

            self.assertEqual(
                describe_undo_checkpoint(job_dir)["label"], "prune of 1 sequence"
            )

    def test_undo_is_consumed_by_a_single_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            with undo_checkpoint(job_dir, "rename", "rename") as checkpoint:
                checkpoint.commit()

            undo_last_edit(job_dir)
            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})
            with self.assertRaises(UndoUnavailable):
                undo_last_edit(job_dir)

    def test_undo_deletes_a_tree_file_the_edit_created(self):
        # An outgroup-rooted job has no tree_pruned.* until its first edit. Undo
        # has to remove what the edit created, or _editable_tree_input_path()
        # keeps reading the edited tree it was told to discard.
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp), with_pruned_files=False)
            with undo_checkpoint(job_dir, "prune", "prune of 1 sequence") as checkpoint:
                write_tree_file(
                    Phylo.read(StringIO("(A:0.1,B:0.2);"), "newick"),
                    str(job_dir / "tree" / "tree_pruned.newick"), "newick",
                )
                checkpoint.commit()

            self.assertTrue((job_dir / "tree" / "tree_pruned.newick").is_file())
            undo_last_edit(job_dir)
            self.assertFalse((job_dir / "tree" / "tree_pruned.newick").exists())

    def test_an_expired_checkpoint_is_dropped_rather_than_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            with undo_checkpoint(job_dir, "prune", "prune of 1 sequence") as checkpoint:
                checkpoint.commit()

            manifest_path = job_dir / UNDO_DIR_NAME / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["created_at"] = time.time() - (30 * 24 * 3600)
            manifest_path.write_text(json.dumps(manifest))

            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})
            self.assertFalse((job_dir / UNDO_DIR_NAME).exists())

    def test_a_checkpoint_without_its_state_is_refused_not_half_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            with undo_checkpoint(job_dir, "prune", "prune of 1 sequence") as checkpoint:
                checkpoint.commit()
            (job_dir / UNDO_DIR_NAME / "tree_state.json").unlink()

            with self.assertRaises(UndoUnavailable):
                undo_last_edit(job_dir)
            # Refused AND cleared, so the button stops offering a restore that
            # can never complete.
            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})

    def test_staging_left_by_an_interrupted_capture_is_swept_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = _make_job(Path(tmp))
            stale = job_dir / ".tree_undo.pending.abandoned"
            stale.mkdir()
            os.utime(stale, (time.time() - 7200, time.time() - 7200))
            fresh = job_dir / ".tree_undo.pending.inflight"
            fresh.mkdir()

            describe_undo_checkpoint(job_dir)

            self.assertFalse(stale.exists(), "an abandoned capture was left behind")
            self.assertTrue(fresh.exists(), "a capture still in flight must survive")

    def test_the_snapshot_covers_the_files_the_edits_actually_write(self):
        """A file added to an edit path but not to SNAPSHOT_PATHS breaks undo."""
        self.assertEqual(
            set(SNAPSHOT_PATHS),
            {"tree_state.json", "tree/tree_pruned.newick", "tree/tree_pruned.nexus"},
        )


# --------------------------------------------------------------------------
# End to end through the API routes
# --------------------------------------------------------------------------
class _RouteHarness(unittest.TestCase):
    """Calls the real view functions against a temporary job directory."""

    def _call(self, root, view, *, method="POST", body=None, access_error=None):
        from app.api import routes
        from app.config import Config

        app = Flask(__name__)
        access = (None, None, 200) if access_error is None else (None, access_error[0], access_error[1])
        kwargs = {"method": method}
        if body is not None:
            kwargs["json"] = body
        with (
            app.test_request_context(**kwargs),
            patch.object(Config, "JOB_DIR", root),
            patch.object(routes, "check_job_access", return_value=access),
        ):
            # _server_error() reads the request id the real app's before_request
            # installs; a bare test_request_context has none.
            g.request_id = "test"
            result = view(JOB_ID)
        if isinstance(result, tuple):
            return result[1], result[0].get_json()
        return 200, result.get_json()

    def _prune(self, root, tips):
        from app.api import routes
        return self._call(root, routes.prune_tree, body={"tip_names": tips})

    def _undo(self, root, **kwargs):
        from app.api import routes
        return self._call(root, routes.undo_tree_edit, **kwargs)

    def _undo_state(self, root, **kwargs):
        from app.api import routes
        return self._call(root, routes.get_tree_undo_state, method="GET", **kwargs)


class PruneUndoTests(_RouteHarness):
    def test_pruning_tips_is_fully_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            newick = job_dir / "tree" / "tree_pruned.newick"
            before_newick = newick.read_text()

            # A rename and a rooting choice made before the prune must survive the
            # round trip untouched.
            state = load_tree_state(job_dir)
            state["renames"] = {"D": "Amanita muscaria D"}
            state["root_mode"] = "MIDPOINT"
            state["is_midpoint_rooted"] = True
            save_tree_state(job_dir, state)

            status, _ = self._prune(root, ["A", "B"])
            self.assertEqual(status, 200)
            self.assertEqual(_tip_names(newick), {"C", "D"})
            self.assertEqual(
                _edited_fasta_headers(job_dir), ["C", "Amanita muscaria D"]
            )

            status, payload = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(payload["undone"]["operation"], "prune")
            self.assertEqual(payload["undone"]["label"], "prune of 2 sequences")

            restored = load_tree_state(job_dir)
            self.assertEqual(_tip_names(newick), {"A", "B", "C", "D"})
            self.assertEqual(newick.read_text(), before_newick)
            self.assertEqual(restored["pruned_taxa"], [])
            self.assertEqual(restored["renames"], {"D": "Amanita muscaria D"})
            self.assertEqual(restored["root_mode"], "MIDPOINT")
            self.assertTrue(restored["is_midpoint_rooted"])
            self.assertEqual(
                _edited_fasta_headers(job_dir),
                ["A", "B", "C", "Amanita muscaria D"],
            )
            self.assertFalse(self._undo_state(root)[1]["available"])

    def test_pruning_a_whole_clade_is_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            newick = job_dir / "tree" / "tree_pruned.newick"
            tree = Phylo.read(str(newick), "newick")
            ab = next(c for c in tree.get_nonterminals() if c.name == "AB")

            self._prune(root, [_stable_internal_node_id(ab)])
            self.assertEqual(_tip_names(newick), {"C", "D"})

            status, payload = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(payload["undone"]["label"], "prune of 2 sequences")
            self.assertEqual(_tip_names(newick), {"A", "B", "C", "D"})
            self.assertEqual(load_tree_state(job_dir)["pruned_taxa"], [])

    def test_a_prune_that_removes_nothing_leaves_the_earlier_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)

            self._prune(root, ["A"])
            self.assertEqual(
                self._undo_state(root)[1]["label"], "prune of 1 sequence"
            )
            # Same target again: already pruned, so nothing is removed and the
            # useful checkpoint must not be overwritten with a no-op one.
            self._prune(root, ["A"])
            self.assertEqual(
                self._undo_state(root)[1]["label"], "prune of 1 sequence"
            )

            self._undo(root)
            self.assertEqual(
                _tip_names(job_dir / "tree" / "tree_pruned.newick"),
                {"A", "B", "C", "D"},
            )

    def test_a_failed_prune_leaves_the_previous_checkpoint_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])

            # Pruning everything that is left is refused atomically.
            status, _ = self._prune(root, ["B", "C", "D"])
            self.assertEqual(status, 500)
            self.assertEqual(
                self._undo_state(root)[1]["label"], "prune of 1 sequence"
            )

            self._undo(root)
            self.assertEqual(
                _tip_names(job_dir / "tree" / "tree_pruned.newick"),
                {"A", "B", "C", "D"},
            )


class OtherEditUndoTests(_RouteHarness):
    def test_rename_is_undoable(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)

            self._call(root, routes.rename_tree_tip,
                       body={"old_name": "A", "new_name": "Renamed A"})
            self.assertEqual(load_tree_state(job_dir)["renames"], {"A": "Renamed A"})
            self.assertEqual(_edited_fasta_headers(job_dir)[0], "Renamed A")

            status, payload = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(payload["undone"]["label"], "rename")
            self.assertEqual(load_tree_state(job_dir)["renames"], {})
            self.assertEqual(_edited_fasta_headers(job_dir)[0], "A")

    def test_renaming_several_tips_at_once_undoes_as_one_action(self):
        # The Rename modal renames the whole selection. If that were N requests
        # the checkpoint would sit between them and Undo would hand back one.
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)

            status, _ = self._call(root, routes.rename_tree_tip, body={
                "renames": {"A": "Alpha", "B": "Beta", "C": "Gamma"},
            })
            self.assertEqual(status, 200)
            self.assertEqual(
                load_tree_state(job_dir)["renames"],
                {"A": "Alpha", "B": "Beta", "C": "Gamma"},
            )
            self.assertEqual(
                _edited_fasta_headers(job_dir), ["Alpha", "Beta", "Gamma", "D"]
            )

            status, payload = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(payload["undone"]["label"], "rename of 3 sequences")
            self.assertEqual(load_tree_state(job_dir)["renames"], {})
            self.assertEqual(_edited_fasta_headers(job_dir), ["A", "B", "C", "D"])

    def test_a_rejected_name_in_a_batch_renames_nothing(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)

            status, payload = self._call(root, routes.rename_tree_tip, body={
                # A semicolon would be Newick punctuation in a written label.
                "renames": {"A": "Alpha", "B": "Bad;Name"},
            })
            self.assertEqual(status, 400)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(load_tree_state(job_dir)["renames"], {})
            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})

    def test_rotate_is_undoable(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            newick = job_dir / "tree" / "tree_pruned.newick"
            tree = Phylo.read(str(newick), "newick")
            node_id = _stable_internal_node_id(tree.root)
            before = newick.read_text()

            status, _ = self._call(root, routes.rotate_tree_node, body={"node_id": node_id})
            self.assertEqual(status, 200)
            self.assertNotEqual(newick.read_text(), before)

            status, payload = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(payload["undone"]["label"], "node rotation")
            self.assertEqual(newick.read_text(), before)

    def test_reroot_is_undoable_and_restores_the_rooting_metadata(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            newick = job_dir / "tree" / "tree_pruned.newick"
            before = newick.read_text()

            status, _ = self._call(root, routes.reroot_tree_endpoint,
                                   body={"root_target": "D"})
            self.assertEqual(status, 200)
            rerooted = load_tree_state(job_dir)
            self.assertEqual(rerooted["root_mode"], "TIP")
            self.assertFalse(rerooted["is_midpoint_rooted"])

            status, payload = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(payload["undone"]["label"], "reroot")
            restored = load_tree_state(job_dir)
            self.assertEqual(restored["root_mode"], "MIDPOINT")
            self.assertTrue(restored["is_midpoint_rooted"])
            self.assertEqual(newick.read_text(), before)

    def test_the_rooting_mode_endpoint_is_undoable(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)

            status, _ = self._call(root, routes.set_rooting_mode_endpoint,
                                   body={"mode": "manual", "target": "C"})
            self.assertEqual(status, 200)
            self.assertNotEqual(load_tree_state(job_dir)["root_mode"], "MIDPOINT")

            status, _ = self._undo(root)
            self.assertEqual(status, 200)
            self.assertEqual(load_tree_state(job_dir)["root_mode"], "MIDPOINT")


class CheckpointInvalidationTests(_RouteHarness):
    def test_recompute_clears_the_checkpoint(self):
        from app.services.tree_edit_service import commit_recompute_tree_state
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])
            self.assertTrue(describe_undo_checkpoint(job_dir)["available"])

            commit_recompute_tree_state(
                job_dir,
                {"name": "ROOT", "original_name": "ROOT", "children": [
                    {"name": "B", "original_name": "B"},
                    {"name": "C", "original_name": "C"},
                ]},
            )

            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})
            status, payload = self._undo(root)
            self.assertEqual(status, 409)
            self.assertEqual(payload["status"], "error")

    def test_saving_annotations_clears_the_checkpoint(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])
            self.assertTrue(describe_undo_checkpoint(job_dir)["available"])

            # An annotation is deliberate authoring work that undo would revert
            # along with the prune, so the checkpoint goes instead.
            status, _ = self._call(root, routes.save_clade_annotations, body={
                "layers": [{"id": "layer-1", "name": "Sections", "order": 1}],
                "annotations": [{
                    "id": "ann-1", "layer_id": "layer-1", "label": "Group",
                    "member_tip_ids": ["C", "D"],
                }],
            })
            self.assertEqual(status, 200)
            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})

    def test_setting_the_sequence_of_interest_clears_the_checkpoint(self):
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])

            status, _ = self._call(root, routes.set_sequence_of_interest_endpoint,
                                   body={"tip_name": "C"})
            self.assertEqual(status, 200)
            self.assertEqual(describe_undo_checkpoint(job_dir), {"available": False})

    def test_saving_selection_colors_keeps_the_checkpoint(self):
        """The viewer saves selections automatically after every prune.

        Clearing there would disable Undo the instant it became useful, and
        selection membership is state undo should carry back with the edit.
        """
        from app.api import routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])

            status, _ = self._call(root, routes.save_selection_sets,
                                   body={"sets": {"Default": ["C"]}})
            self.assertEqual(status, 200)
            self.assertTrue(describe_undo_checkpoint(job_dir)["available"])


class UndoAccessTests(_RouteHarness):
    def test_a_read_only_viewer_is_told_it_cannot_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])

            from app.api import routes
            from app.config import Config

            app = Flask(__name__)

            def access(job_id, mode="view"):
                if mode == "edit":
                    return None, "You do not have permission to edit this job", 403
                return None, None, 200

            with (
                app.test_request_context(method="GET"),
                patch.object(Config, "JOB_DIR", root),
                patch.object(routes, "check_job_access", side_effect=access),
            ):
                payload = routes.get_tree_undo_state(JOB_ID).get_json()

            # The checkpoint exists, but this caller may not apply it, so the
            # viewer must not offer a persisted Undo.
            self.assertTrue(payload["available"])
            self.assertFalse(payload["can_undo"])

    def test_an_unauthorized_undo_is_rejected_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = _make_job(root)
            self._prune(root, ["A"])
            pruned_tree = (job_dir / "tree" / "tree_pruned.newick").read_text()

            status, payload = self._undo(
                root, access_error=("You do not have permission to edit this job", 403)
            )
            self.assertEqual(status, 403)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(
                (job_dir / "tree" / "tree_pruned.newick").read_text(), pruned_tree
            )
            self.assertTrue(describe_undo_checkpoint(job_dir)["available"])


if __name__ == "__main__":
    unittest.main()
