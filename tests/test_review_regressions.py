"""Regression coverage for the review fixes around artifacts and tree state."""

import fcntl
import gzip
import inspect
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from app.api import routes
from app.api_v1.jobs import artifact_path, list_available_artifacts
from app.config import Config
from app.services.tree_annotation_service import (
    ANNOTATION_LAYERS_KEY,
    CLADE_ANNOTATIONS_KEY,
)
from app.services.tree_edit_service import (
    load_tree_state,
    save_tree_state,
    tree_state_lock,
    validate_tip_rename,
)



def _undecorated(view):
    """The view function itself, with every decorator layer removed.

    Reaching for .__wrapped__ once only steps past the outermost decorator. The
    endpoint carries @limiter.limit today; the day it also carries an auth or
    validation decorator, a single-layer unwrap would silently start testing
    that decorator instead of the handler.
    """
    return inspect.unwrap(view)


JOB_ID = "12345678-1234-1234-1234-123456789abc"
V1_JOB_ID = "12345678-1234-4234-8234-123456789abc"


def _annotation_state():
    return {
        "tree_structure": {
            "children": [
                {"name": "A", "original_name": "A"},
                {"name": "B", "original_name": "B"},
            ],
        },
        "renames": {},
        ANNOTATION_LAYERS_KEY: [{"id": "layer_a", "name": "Layer", "order": 1}],
        CLADE_ANNOTATIONS_KEY: [{
            "id": "annotation_a",
            "layer_id": "layer_a",
            "label": "Saved",
            "member_tip_ids": ["A", "B"],
        }],
    }


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (None, None),
        ('{"layers":', "application/json"),
        ("[]", "application/json"),
    ],
)
def test_malformed_annotation_body_returns_400_without_clearing_state(
    tmp_path, body, content_type
):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    original = _annotation_state()
    save_tree_state(job_dir, original)

    app = Flask(__name__)
    kwargs = {"method": "POST"}
    if body is not None:
        kwargs.update(data=body, content_type=content_type)

    with (
        app.test_request_context(**kwargs),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
    ):
        response, status = routes.save_clade_annotations(JOB_ID)

    assert status == 400
    assert response.get_json()["status"] == "error"
    assert load_tree_state(job_dir) == original


def test_trimmed_fasta_download_decompresses_gzipped_artifact(tmp_path):
    job_dir = tmp_path / JOB_ID
    alignment_dir = job_dir / "alignment"
    alignment_dir.mkdir(parents=True)
    fasta = b">A\nACGT\n"
    with gzip.open(alignment_dir / "alignment_trimmed.fasta.gz", "wb") as handle:
        handle.write(fasta)

    app = Flask(__name__)
    with (
        app.test_request_context(method="GET"),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
    ):
        response = routes.download_fasta_trimmed(JOB_ID)
        response.direct_passthrough = False
        assert response.get_data() == fasta
        assert "sequences_trimmed.fasta" in response.headers["Content-Disposition"]


@pytest.mark.parametrize(
    "payload",
    [
        {"async": True, "use_current_input": True},
        {"async": True, "tree_method": "mrbayes"},
    ],
)
def test_mutated_recompute_request_conflicts_with_active_generation(tmp_path, payload):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text('{"tree_method":"raxml"}')

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json=payload),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
        patch.object(routes, "enqueue_recompute_job", return_value=(JOB_ID, False)),
        patch.object(routes, "url_for", return_value=f"/job/{JOB_ID}"),
    ):
        response, status = _undecorated(routes.recompute_tree_job)(JOB_ID)

    body = response.get_json()
    assert status == 409
    assert body["status"] == "conflict"
    assert "will not include" in body["error"]


def test_unchanged_duplicate_recompute_remains_idempotent(tmp_path):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    (job_dir / "input_info.json").write_text('{"tree_method":"raxml"}')

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json={"async": True}),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
        patch.object(routes, "enqueue_recompute_job", return_value=(JOB_ID, False)),
        patch.object(routes, "url_for", return_value=f"/job/{JOB_ID}"),
    ):
        response, status = _undecorated(routes.recompute_tree_job)(JOB_ID)

    assert status == 202
    assert response.get_json()["status"] == "already_queued"


@pytest.mark.parametrize(("created", "checkpoint_survives"), [(True, False), (False, True)])
def test_v1_recompute_clears_undo_only_when_a_new_job_is_created(
    tmp_path, created, checkpoint_survives
):
    from app.api_v1 import routes as v1_routes

    job_dir = tmp_path / V1_JOB_ID
    checkpoint = job_dir / ".tree_undo"
    checkpoint.mkdir(parents=True)
    (checkpoint / "sentinel").write_text("old topology")
    (job_dir / "input_info.json").write_text(json.dumps({"tree_method": "raxml"}))
    job = SimpleNamespace(status="completed", metrics={})

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json={}),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(v1_routes, "get_owned_job_or_404", return_value=job),
        patch.object(
            v1_routes, "enqueue_recompute_job", return_value=("rq-job", created)
        ),
        patch.object(v1_routes, "url_for", return_value="/api/v1/job"),
        patch.object(v1_routes, "db"),
    ):
        response = _undecorated(v1_routes.recompute_job)(V1_JOB_ID)

    assert response.status_code == 202
    assert response.get_json()["data"]["status"] == (
        "queued" if created else "already_queued"
    )
    assert checkpoint.exists() is checkpoint_survives


@pytest.mark.parametrize(
    ("bootstrap", "message"),
    [(999, "at least 1000"), ("many", "integer"), (1000.5, "integer")],
)
def test_recompute_rejects_invalid_iqtree_ufboot_before_enqueue(
    tmp_path, bootstrap, message
):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    input_info = job_dir / "input_info.json"
    original = '{"tree_method":"iqtree","bootstrap":1000}'
    input_info.write_text(original)

    app = Flask(__name__)
    with (
        app.test_request_context(method="POST", json={"bootstrap": bootstrap}),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
        patch.object(routes, "enqueue_recompute_job") as enqueue,
    ):
        response, status = _undecorated(routes.recompute_tree_job)(JOB_ID)

    assert status == 400
    assert message in response.get_json()["error"]
    enqueue.assert_not_called()
    assert input_info.read_text() == original


@pytest.mark.parametrize(
    ("old_name", "new_name"),
    [
        (None, "Safe name"),
        ("A", 42),
        ("A", ""),
        ("A", "   "),
        ("A", "x" * 257),
        ("A", "broken\nheader"),
        ("A", "broken\x7fheader"),
        ("A", "A:0.5"),
    ],
)
def test_tip_rename_validation_rejects_unsafe_external_values(old_name, new_name):
    with pytest.raises(ValueError):
        validate_tip_rename(old_name, new_name)


def test_browser_rename_rejects_invalid_label_without_changing_state(tmp_path):
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    original = {
        "tree_structure": {"name": "A", "original_name": "A"},
        "renames": {},
    }
    save_tree_state(job_dir, original)

    app = Flask(__name__)
    with (
        app.test_request_context(
            method="POST",
            json={"old_name": "A", "new_name": "broken\n>injected"},
        ),
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(routes, "check_job_access", return_value=(None, None, 200)),
    ):
        response, status = routes.rename_tree_tip(JOB_ID)

    assert status == 400
    assert "control characters" in response.get_json()["error"]
    assert load_tree_state(job_dir) == original


def test_v1_nexus_prefers_pruned_tree_and_falls_back_to_original(tmp_path):
    tree_dir = tmp_path / JOB_ID / "tree"
    tree_dir.mkdir(parents=True)
    original = tree_dir / "tree_original.nexus"
    pruned = tree_dir / "tree_pruned.nexus"
    original.write_text("original")

    with patch.object(Config, "JOB_DIR", tmp_path):
        assert artifact_path(JOB_ID, "tree.nexus") == original

        pruned.write_text("edited")
        assert artifact_path(JOB_ID, "tree.nexus") == pruned
        listed = {item["name"]: item for item in list_available_artifacts(JOB_ID)}
        assert listed["tree.nexus"]["size_bytes"] == len("edited")


def test_tree_state_lock_blocks_a_second_writer_until_the_first_commits(tmp_path):
    """Writer B must not run the protected body while writer A holds the lock.

    The proof does not depend on scheduling. tree_state_lock() serializes with
    fcntl.flock(LOCK_EX), so this patches flock itself and lets B announce its
    arrival *from inside the blocking call*. "B is waiting for the lock" is then
    an observed fact rather than something inferred from a sleep, and the two
    ways the lock can be broken both fail deterministically:

      * replaced by a no-op context manager -- flock is never called, B never
        announces, and A's bounded wait fails with a specific message;
      * acquired non-exclusively -- B enters the body while A holds the lock,
        and the ordering assertion fails.

    Every wait is bounded and every thread exception is surfaced, so a failure
    reports rather than hanging.
    """
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    save_tree_state(job_dir, _annotation_state())

    first_holds_lock = threading.Event()
    second_reached_lock = threading.Event()
    second_entered_body = threading.Event()
    first_released = threading.Event()
    errors = []
    order = []
    threads = {}

    real_flock = fcntl.flock

    def instrumented_flock(handle, operation):
        if operation == fcntl.LOCK_EX and threading.current_thread() is threads.get("second"):
            second_reached_lock.set()
        return real_flock(handle, operation)

    def rename_writer():
        try:
            with tree_state_lock(job_dir):
                state = load_tree_state(job_dir)
                first_holds_lock.set()
                assert second_reached_lock.wait(timeout=10), (
                    "the second writer never reached fcntl.flock -- "
                    "tree_state_lock() is not taking the lock at all"
                )
                # B is inside an exclusive acquire that this thread owns, so it
                # cannot be running the body. This is guaranteed by flock, not
                # by how long anything slept.
                assert not second_entered_body.is_set(), (
                    "the second writer entered the critical section while the "
                    "first still held the lock"
                )
                state["renames"]["A"] = "Alpha"
                save_tree_state(job_dir, state)
                order.append("first-committed")
            first_released.set()
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)
            # Never leave the other thread waiting on an event this one owns.
            first_holds_lock.set()
            first_released.set()

    def annotation_writer():
        try:
            assert first_holds_lock.wait(timeout=10)
            with tree_state_lock(job_dir):
                second_entered_body.set()
                order.append("second-entered")
                # A released the lock only after committing, so its rename is
                # already on disk. Reading anything else means B loaded a stale
                # snapshot and is about to save over an edit it never saw.
                state = load_tree_state(job_dir)
                assert state["renames"] == {"A": "Alpha"}
                state[CLADE_ANNOTATIONS_KEY][0]["label"] = "Updated"
                save_tree_state(job_dir, state)
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)
            second_reached_lock.set()

    first = threading.Thread(target=rename_writer, name="rename_writer")
    second = threading.Thread(target=annotation_writer, name="annotation_writer")
    threads["first"], threads["second"] = first, second

    with patch("fcntl.flock", instrumented_flock):
        first.start()
        second.start()
        first.join(timeout=20)
        second.join(timeout=20)

    assert not first.is_alive(), "the first writer did not finish"
    assert not second.is_alive(), "the second writer did not finish"
    assert not errors, errors
    assert order == ["first-committed", "second-entered"], (
        f"the writers were not serialized: {order}"
    )

    # Both edits survive: neither writer clobbered the other's field.
    state = load_tree_state(job_dir)
    assert state["renames"] == {"A": "Alpha"}
    assert state[CLADE_ANNOTATIONS_KEY][0]["label"] == "Updated"


# --- recompute commits into the LATEST state, never its own stale snapshot ---

def _recomputed_structure():
    """What parse_newick_to_json() would hand back for the rebuilt tree.

    Deliberately a DIFFERENT topology from _annotation_state(): C is new and the
    grouping changed, which is what "the recompute produced a new tree" means.
    """
    return {
        "name": None,
        "original_name": None,
        "children": [
            {"name": "A", "original_name": "A"},
            {
                "name": None,
                "original_name": None,
                "children": [
                    {"name": "B", "original_name": "B"},
                    {"name": "C", "original_name": "C"},
                ],
            },
        ],
    }


def _leaf_names(structure):
    children = structure.get("children") or []
    if not children:
        return [structure.get("original_name") or structure.get("name")]
    names = []
    for child in children:
        names.extend(_leaf_names(child))
    return names


def test_recompute_commit_keeps_newer_user_state_and_installs_new_topology(tmp_path):
    """The classic stale-snapshot race, at the smallest unit that shows it.

    1. recompute starts from state A
    2. the user edits annotations / renames / selection sets -> state B
    3. recompute commits its new topology

    The result must contain the NEW tree plus the user's NEWER edits.
    """
    from app.services.tree_edit_service import commit_recompute_tree_state

    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    save_tree_state(job_dir, _annotation_state())

    # 1. Recompute reads its inputs and then spends minutes in MAFFT/RAxML.
    snapshot = load_tree_state(job_dir)

    # 2. Meanwhile the viewer commits unrelated user edits.
    with tree_state_lock(job_dir):
        newer = load_tree_state(job_dir)
        newer[CLADE_ANNOTATIONS_KEY][0]["label"] = "Edited while recomputing"
        newer[CLADE_ANNOTATIONS_KEY].append({
            "id": "annotation_b",
            "layer_id": "layer_a",
            "label": "Added while recomputing",
            "member_tip_ids": ["B"],
        })
        newer["renames"] = {"A": "Alpha"}
        newer["selection_sets"] = {"Default": ["B"]}
        newer["active_selection_set"] = "Default"
        newer["selection_set_colors"] = {"Default": "#1f77b4"}
        newer["sequence_of_interest"] = "B"
        save_tree_state(job_dir, newer)

    # 3. Recompute commits, holding only its stale snapshot.
    committed = commit_recompute_tree_state(
        job_dir, _recomputed_structure(), initial_state=snapshot
    )
    stored = load_tree_state(job_dir)
    assert stored == committed

    # The new topology is installed ...
    assert sorted(_leaf_names(stored["tree_structure"])) == ["A", "B", "C"]
    assert stored["current_tree"] == "pruned"

    # ... and every newer user-authored field survived.
    assert stored["renames"] == {"A": "Alpha"}
    assert stored["selection_sets"] == {"Default": ["B"]}
    assert stored["active_selection_set"] == "Default"
    assert stored["selection_set_colors"] == {"Default": "#1f77b4"}
    assert stored["sequence_of_interest"] == "B"
    labels = {a["id"]: a["label"] for a in stored[CLADE_ANNOTATIONS_KEY]}
    assert labels == {
        "annotation_a": "Edited while recomputing",
        "annotation_b": "Added while recomputing",
    }
    assert stored[ANNOTATION_LAYERS_KEY][0]["id"] == "layer_a"

    # Renames from the newer state are reapplied to the freshly parsed structure.
    renamed = [n for n in stored["tree_structure"]["children"] if n.get("original_name") == "A"]
    assert renamed and renamed[0]["name"] == "Alpha"


def test_recompute_commit_drops_only_members_whose_tips_disappeared(tmp_path):
    """Annotation cleanup after recompute stays minimal: no tip, no member."""
    from app.services.tree_edit_service import commit_recompute_tree_state

    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    state = _annotation_state()
    state[CLADE_ANNOTATIONS_KEY][0]["member_tip_ids"] = ["A", "B", "Gone"]
    state[CLADE_ANNOTATIONS_KEY].append({
        "id": "annotation_single",
        "layer_id": "layer_a",
        "label": "One tip",
        "member_tip_ids": ["C"],
    })
    state[CLADE_ANNOTATIONS_KEY].append({
        "id": "annotation_vanished",
        "layer_id": "layer_a",
        "label": "All members gone",
        "member_tip_ids": ["Gone"],
    })
    save_tree_state(job_dir, state)

    commit_recompute_tree_state(job_dir, _recomputed_structure(), initial_state=state)
    stored = load_tree_state(job_dir)
    by_id = {a["id"]: a for a in stored[CLADE_ANNOTATIONS_KEY]}

    # Members that still exist are kept, the tip that vanished is dropped ...
    assert by_id["annotation_a"]["member_tip_ids"] == ["A", "B"]
    # ... a one-member annotation is still an annotation ...
    assert by_id["annotation_single"]["member_tip_ids"] == ["C"]
    # ... and only the one left with nothing at all is removed.
    assert "annotation_vanished" not in by_id


# --- source-observation highlighting must not stomp concurrent edits --------

def _source_state(tip_names):
    state = _annotation_state()
    state["tree_structure"] = {
        "name": None,
        "original_name": None,
        "children": [{"name": n, "original_name": n} for n in tip_names],
    }
    return state


def test_inat_highlight_preserves_edits_made_during_the_remote_lookup(tmp_path):
    """The iNat display-name lookup is remote, so the state is read after it."""
    from app.services import inaturalist_tree_service as inat

    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    save_tree_state(job_dir, _source_state(["iNat123 Galerina", "Other_tip"]))

    def slow_fetch(observation_id):
        # Stands in for the HTTP round trip: a viewer edit lands while it runs.
        with tree_state_lock(job_dir):
            concurrent = load_tree_state(job_dir)
            concurrent[CLADE_ANNOTATIONS_KEY][0]["label"] = "Edited during lookup"
            concurrent["renames"] = {"Other_tip": "Renamed during lookup"}
            save_tree_state(job_dir, concurrent)
        return {"id": observation_id}

    with (
        patch.object(Config, "JOB_DIR", tmp_path),
        patch.object(inat, "fetch_observation", slow_fetch),
        patch.object(inat, "_build_inat_source_display_name",
                     lambda observation, observation_id: "Galerina marginata"),
    ):
        targets = inat.highlight_source_observation_tip(JOB_ID, 123)

    assert targets == ["iNat123 Galerina"]
    stored = load_tree_state(job_dir)
    # The highlight applied ...
    assert stored["selection_sets"]["Default"] == ["iNat123 Galerina"]
    # ... without discarding anything written while the lookup was in flight.
    assert stored[CLADE_ANNOTATIONS_KEY][0]["label"] == "Edited during lookup"
    assert stored["renames"]["Other_tip"] == "Renamed during lookup"


def test_mushroom_observer_highlight_preserves_unrelated_tree_state(tmp_path):
    from app.services import mushroom_observer_service as mo

    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    state = _source_state(["MO456 Amanita", "Other_tip"])
    state["renames"] = {"Other_tip": "Kept"}
    state["selection_set_colors"] = {"Default": "#1f77b4", "Extra": "#ff0000"}
    save_tree_state(job_dir, state)

    with patch.object(Config, "JOB_DIR", tmp_path):
        targets = mo.highlight_source_observation_tip(JOB_ID, 456)

    assert targets == ["MO456 Amanita"]
    stored = load_tree_state(job_dir)
    assert stored["selection_sets"]["Default"] == ["MO456 Amanita"]
    assert stored["renames"]["Other_tip"] == "Kept"
    assert stored["selection_set_colors"]["Extra"] == "#ff0000"
    assert stored[CLADE_ANNOTATIONS_KEY][0]["label"] == "Saved"
