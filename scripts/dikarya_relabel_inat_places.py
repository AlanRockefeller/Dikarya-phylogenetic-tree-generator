#!/usr/bin/env python3
"""
Relabel the iNaturalist tips of an existing tree with standardized places.

Trees built before the standardized-place lookup landed carry tip labels whose
location came from the observer's free-text `place_guess`: a road name, a zip
code, or nothing at all ("iNat13923264 MS 39146 US Cyanoboletus bessettei").
This rewrites those tips to the label a fresh import produces today --
observation number, species, then the administrative place the coordinates fall
in ("iNat13923264 Cyanoboletus bessettei Madison Co. MS US").

Nothing is recomputed: the topology, branch lengths, alignment and support
values are untouched. The new labels go into tree_state.json's `renames`, the
same mechanism the viewer's Rename uses, so pruned membership, selection sets,
clade annotations and rooting -- all keyed on the ORIGINAL tip names -- keep
working. The whole batch is one undo checkpoint, so the user can revert it from
the viewer's Undo button.

The species comes from input_info.json's sequence_metadata rather than from
iNaturalist, so a tip keeps whatever name the tree was built with (including a
Provisional Species Name); only the location is looked up fresh.

MUST RUN AS THE `dikarya` USER -- var/jobs is dikarya-owned, and a tree_state
written by another user would be unwritable by the web process:

    sudo -u dikarya .venv/bin/python \
        scripts/dikarya_relabel_inat_places.py <job_id> --dry-run
    sudo -u dikarya .venv/bin/python \
        scripts/dikarya_relabel_inat_places.py <job_id> --apply

Dry run is the default.
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
INAT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])iNat(\d+)(?![0-9])", re.IGNORECASE)
INAT_SOURCES = {"inat_observation", "inaturalist"}


def _tip_labels(job_dir: Path, state: dict):
    """Every tip label the job knows about, pruned ones included."""
    labels = []
    seen = set()

    def add(value):
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            labels.append(name)

    def walk(node):
        if not isinstance(node, dict):
            return
        children = node.get("children") or []
        if not children:
            add(node.get("original_name") or node.get("name"))
            return
        for child in children:
            walk(child)

    walk(state.get("tree_structure") or {})
    for name in state.get("pruned_taxa") or []:
        add(name)
    # Backstop for tips in neither place (an older state, a partial structure).
    original_newick = job_dir / "tree" / "tree_original.newick"
    if original_newick.is_file():
        for match in re.findall(r"'([^']+)'", original_newick.read_text()):
            add(match)
    return labels


def plan_renames(job_dir: Path, state: dict):
    """Return (renames, skipped) without touching iNaturalist for non-iNat jobs."""
    from app.services.inaturalist_places import (
        fetch_observation_places,
        resolve_place_labels,
    )

    info = json.loads((job_dir / "input_info.json").read_text())
    metadata = {}
    for record in info.get("sequence_metadata") or []:
        header = str(record.get("fasta_header") or record.get("name") or "").strip()
        if header:
            metadata[header] = record

    renames, skipped = {}, []
    candidates = {}
    for label in _tip_labels(job_dir, state):
        record = metadata.get(label)
        if not record:
            skipped.append((label, "no sequence_metadata record"))
            continue
        source = str(record.get("hit_source") or "").strip()
        origin = str(record.get("source") or "").strip()
        if source not in INAT_SOURCES and origin not in INAT_SOURCES:
            continue  # not an iNaturalist tip; silently left alone
        match = INAT_TOKEN_RE.search(label)
        observation_id = record.get("observation_id") or (match.group(1) if match else "")
        try:
            observation_id = int(observation_id)
        except (TypeError, ValueError):
            skipped.append((label, "no observation id"))
            continue
        candidates[label] = (observation_id, record)

    if not candidates:
        return renames, skipped

    observations = fetch_observation_places(
        observation_id for observation_id, _record in candidates.values())
    labels_by_observation = resolve_place_labels(observations)

    for label, (observation_id, record) in candidates.items():
        location = labels_by_observation.get(observation_id, "")
        if not location:
            skipped.append((label, "iNaturalist has no standardized place"))
            continue
        organism = " ".join(str(record.get("organism") or "").split())
        parts = [f"iNat{observation_id}"]
        if organism:
            parts.append(organism)
        parts.append(location)
        new_label = " ".join(parts)
        if new_label == label:
            continue
        renames[label] = new_label

    duplicates = [name for name in set(renames.values())
                  if list(renames.values()).count(name) > 1]
    if duplicates:
        raise SystemExit(
            "Refusing to apply: these new labels would collide -- "
            + ", ".join(sorted(duplicates))
        )
    return renames, skipped


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="Show the renames without writing (default).")
    group.add_argument("--apply", action="store_true",
                       help="Write the renames into tree_state.json.")
    args = parser.parse_args()

    if not JOB_ID_RE.match(args.job_id):
        raise SystemExit(f"Not a job id: {args.job_id!r}")

    from app.config import Config
    from app.services.tree_edit_service import (
        load_tree_state,
        save_tree_state,
        tree_state_lock,
        validate_tip_rename,
    )
    from app.services.tree_undo_service import undo_checkpoint

    job_dir = Path(Config.JOB_DIR) / args.job_id
    if not job_dir.is_dir():
        raise SystemExit(f"No such job directory: {job_dir}")

    with tree_state_lock(job_dir):
        state = load_tree_state(job_dir)
        renames, skipped = plan_renames(job_dir, state)

        for label, reason in skipped:
            print(f"  skip  {label}\n        ({reason})")
        if not renames:
            print("Nothing to relabel.")
            return
        pairs = [validate_tip_rename(old, new) for old, new in renames.items()]
        for old, new in pairs:
            print(f"  {old}\n    -> {new}")
        print(f"\n{len(pairs)} tip(s) to relabel"
              f"{'' if args.apply else ' (dry run -- pass --apply to write)'}")
        if not args.apply:
            return

        from app.services.tree_edit_service import rename_tip
        label = f"rename of {len(pairs)} sequences"
        with undo_checkpoint(job_dir, "rename", label) as checkpoint:
            for old, new in pairs:
                state = rename_tip(state, old, new)
            save_tree_state(job_dir, state)
            checkpoint.commit()
    print("Applied. The viewer's Undo will revert the whole batch.")


if __name__ == "__main__":
    main()
