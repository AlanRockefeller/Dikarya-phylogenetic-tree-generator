"""Regression coverage for the review fixes around artifacts and tree state."""

import gzip
import threading
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


JOB_ID = "12345678-1234-1234-1234-123456789abc"


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
        response, status = routes.recompute_tree_job.__wrapped__(JOB_ID)

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
        response, status = routes.recompute_tree_job.__wrapped__(JOB_ID)

    assert status == 202
    assert response.get_json()["status"] == "already_queued"


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


def test_tree_state_lock_preserves_annotation_and_tree_edits(tmp_path):
    """A second writer must load only after the first writer has committed."""
    job_dir = tmp_path / JOB_ID
    job_dir.mkdir()
    save_tree_state(job_dir, _annotation_state())

    first_has_lock = threading.Event()
    second_attempting = threading.Event()
    errors = []

    def rename_writer():
        try:
            with tree_state_lock(job_dir):
                state = load_tree_state(job_dir)
                first_has_lock.set()
                assert second_attempting.wait(timeout=2)
                state["renames"]["A"] = "Alpha"
                save_tree_state(job_dir, state)
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)

    def annotation_writer():
        try:
            assert first_has_lock.wait(timeout=2)
            second_attempting.set()
            with tree_state_lock(job_dir):
                state = load_tree_state(job_dir)
                state[CLADE_ANNOTATIONS_KEY][0]["label"] = "Updated"
                save_tree_state(job_dir, state)
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)

    first = threading.Thread(target=rename_writer)
    second = threading.Thread(target=annotation_writer)
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
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
