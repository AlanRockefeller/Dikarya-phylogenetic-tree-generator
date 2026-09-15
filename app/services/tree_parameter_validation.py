"""Shared validation for scientifically meaningful tree-builder parameters."""

import math
import numbers
import re


_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


def _coerce_ufboot_count(bootstrap):
    """Return ``bootstrap`` as an exact integer, or None if it is not one.

    Booleans are rejected on purpose: ``True`` would otherwise mean "one
    replicate", which no caller has ever intended. Non-finite floats and
    non-integral floats are rejected for the same reason -- ``-B`` takes a
    count, and ``nan``/``inf``/``2.5`` are not counts.
    """
    if isinstance(bootstrap, bool):
        return None
    if isinstance(bootstrap, numbers.Integral):
        return int(bootstrap)
    if isinstance(bootstrap, numbers.Real):
        numeric_value = float(bootstrap)
        if math.isfinite(numeric_value) and numeric_value.is_integer():
            return int(numeric_value)
        return None
    if isinstance(bootstrap, str) and _INTEGER_TEXT.fullmatch(bootstrap.strip()):
        return int(bootstrap.strip())
    return None


def _reject_unusable_count(count):
    if count is None or count < 0:
        raise ValueError(
            "IQ-TREE ultrafast bootstrap (-B) requires a non-negative integer "
            "replicate count."
        )
    return count


def validate_iqtree_ufboot_count(tree_method, bootstrap):
    """Reject IQ-TREE UFBoot counts that its ``-B`` option cannot run.

    Zero disables UFBoot. RAxML uses the same stored field for legacy reasons,
    but its bootstrap workflow has different semantics and is intentionally not
    constrained here. For IQ-TREE, return an explicitly normalized integer so
    internal callers cannot validate one representation and later pass a
    different, non-integral representation to the executable.
    """
    if str(tree_method or "").lower() != "iqtree":
        return bootstrap

    count = _reject_unusable_count(_coerce_ufboot_count(bootstrap))
    if 0 < count < MIN_IQTREE_UFBOOT_REPLICATES:
        raise ValueError(
            "IQ-TREE ultrafast bootstrap (-B) requires either 0 replicates "
            f"(disabled) or at least {MIN_IQTREE_UFBOOT_REPLICATES}; "
            f"received {count}."
        )
    return count


# IQ-TREE's own lower bound for ``-B``. Exposed so callers normalizing a stored
# value do not re-hardcode it.
MIN_IQTREE_UFBOOT_REPLICATES = 1000


def normalize_inherited_iqtree_ufboot_count(tree_method, bootstrap):
    """Return a runnable UFBoot count for a value *inherited* from an old job.

    Recompute re-validates the whole stored parameter set, including fields the
    caller never mentioned. Jobs predating the ``-B >= 1000`` rule carry counts
    such as 500, so applying `validate_iqtree_ufboot_count` to an untouched
    inherited value made those jobs permanently unrecomputable -- the user had
    no way to run the tree they already had. Anything the caller actually
    supplies still goes through the strict validator; only an inherited value
    is lifted to the supported minimum here.

    The lift is deliberately narrow: exactly the old-but-valid range 1-999.
    Wrapping the strict validator in ``except ValueError`` instead turned
    *every* unusable stored value -- "banana", -5, NaN, a dict left behind by a
    malformed edit -- into a silent request for 1000 replicates, which is a
    scientifically meaningful instruction the user never gave. Corruption must
    still surface as an error.
    """
    if str(tree_method or "").lower() != "iqtree":
        return bootstrap

    count = _reject_unusable_count(_coerce_ufboot_count(bootstrap))
    if 0 < count < MIN_IQTREE_UFBOOT_REPLICATES:
        return MIN_IQTREE_UFBOOT_REPLICATES
    return count


# ---------------------------------------------------------------------------
# Quick Tree submission limits
# ---------------------------------------------------------------------------

# Quick Tree is the two-click path: fixed MAFFT --auto / trimAl / FastTree, no
# parameter form. It exists for barcode-scale exploratory phylogenies, and MAFFT
# --auto's cost grows with sequence LENGTH as well as count, so one pathological
# record can turn a ten-second job into one that holds the single worker slot
# for hours. Measured across the 11,670 job directories on disk: 1,515,220
# submitted records, of which 400 (0.026%) exceed this and the longest is a
# 149 kb complete phage genome.
#
# This is the one authoritative definition; both the browser check in
# sequence_entry.html and the server check in app/api/routes.py read the value
# from here (the browser through the template's QUICK_TREE_MAX_SEQUENCE_BP
# constant, which is asserted against this one by
# tests/test_quick_tree_limits.py).
#
# Deliberately NOT a site-wide cap: the advanced tree builder still accepts
# longer loci, because a 30 kb mitochondrial region with eight taxa is a
# perfectly reasonable RAxML run.
QUICK_TREE_MAX_SEQUENCE_BP = 10_000

QUICK_TREE_TOO_LONG_MESSAGE = (
    "Quick Tree is intended for barcode-length sequences and does not accept "
    f"individual sequences over {QUICK_TREE_MAX_SEQUENCE_BP:,} bp. Use the "
    "advanced tree builder for longer loci."
)

# What the Quick Tree buttons actually post. Every value, not just the tree
# method: "FastTree" on its own is a perfectly ordinary advanced choice, and an
# earlier version of this check treated any FastTree request that omitted the
# advanced parameter block as the preset -- which misclassified a deliberate
# FastTree + MUSCLE + no-trimming API request as Quick Tree and capped it.
QUICK_TREE_PRESET = {
    "alignment_method": "mafft",
    "trimming_method": "trimal_gappy",
    "tree_method": "fasttree",
    "tree_model": "gtr+g",
}

# The explicit marker the Tree Builder sends. This is what decides the question;
# the preset match below is only the fallback for a request that carries no
# marker at all.
SUBMISSION_MODE_FIELD = "submission_mode"
QUICK_TREE_SUBMISSION_MODE = "quick_tree"
ADVANCED_SUBMISSION_MODE = "advanced"

# Fields only the advanced tree-builder form sends. Used to narrow the
# *fallback* only -- see submission_is_quick_tree for why that matters and why
# it is no longer the whole test.
ADVANCED_TREE_BUILDER_FIELDS = frozenset({
    "alrt_replicates",
    "bootstrap_cap",
    "bootstrap_preset",
    "early_stopping",
    "enable_bootstrap",
    "fix_orientation",
    "its_min_length",
    "its_region",
    "mcmc_burnin_fraction",
    "mcmc_generations",
    "mcmc_nchains",
    "mcmc_nruns",
    "mcmc_stop_early",
    "moose_enabled",
    "outgroup",
    "run_preset",
    "seed",
    "start_tree_override",
})


def _matches_quick_tree_preset(data) -> bool:
    """Does this body carry the Quick Tree preset exactly?

    All four fixed values plus terminal-overhang trimming. Narrower than "uses
    FastTree" by a long way: the advanced form can select FastTree with MUSCLE,
    or with no trimming, or with a different model, and none of those is the
    preset.

    The advanced-field check is a second narrowing on top of that, for the one
    case the preset match cannot settle on its own: the advanced form *can*
    reproduce the preset's four values exactly (mafft + trimAl-gappy + FastTree
    + GTR+G is a reasonable thing to choose by hand). It always sends the whole
    parameter block, so their presence distinguishes it. This is why the marker
    exists and why this is only the fallback -- a caller can still dodge the
    fallback by sending `seed: null`, which is exactly as effective as sending
    submission_mode="advanced", i.e. not a boundary anyone is relying on.
    """
    for key, value in QUICK_TREE_PRESET.items():
        if str(data.get(key) or "").strip().lower() != value:
            return False
    if not data.get("trim_terminal_overhangs"):
        return False
    return not (ADVANCED_TREE_BUILDER_FIELDS & set(data.keys()))


def submission_is_quick_tree(data) -> bool:
    """True when a /api/job body is a Quick Tree submission.

    Decided by the explicit ``submission_mode`` the Tree Builder sends: both
    Quick Tree buttons post ``"quick_tree"`` and the advanced form posts
    ``"advanced"``.

    Only those two values are acted on. A body with no marker -- or with a
    marker nobody recognises -- falls back to matching the preset exactly. That
    covers a browser still running a cached copy of the page from before the
    marker existed, a script replicating the preset, and, deliberately, a typo:
    ``submission_mode="quicktree"`` would otherwise switch the guardrail off
    silently, which is exactly the class of accident it exists to catch. Only a
    deliberate ``"advanced"`` opts out.

    **This is a guardrail, not an abuse boundary.** A caller who declares
    ``advanced`` is not capped, and that is deliberate: the advanced tree
    builder has always accepted a 150 kb locus, so declaring advanced mode opens
    nothing that was not already open. What the cap prevents is the accident --
    a genome pasted into the two-click preset that exists for barcode-length
    reads. If a real ceiling on pipeline cost is ever wanted, it has to be tied
    to the work itself (total bases x sequence count against the aligner that
    will run), not to which button was pressed.
    """
    if not isinstance(data, dict):
        return False
    mode = str(data.get(SUBMISSION_MODE_FIELD) or "").strip().lower()
    if mode == QUICK_TREE_SUBMISSION_MODE:
        return True
    if mode == ADVANCED_SUBMISSION_MODE:
        return False
    # Unrecognised or absent: fall back rather than fail open.
    return _matches_quick_tree_preset(data)


def oversized_quick_tree_records(sequence_text,
                                 limit: int = QUICK_TREE_MAX_SEQUENCE_BP):
    """Return ``[(header, length)]`` for records longer than ``limit`` bases.

    Exactly ``limit`` is accepted; ``limit + 1`` is not.
    """
    from app.services.fasta_utils import parse_fasta_records

    return [
        (header, len(sequence))
        for header, sequence in parse_fasta_records(str(sequence_text or ""))
        if len(sequence) > limit
    ]


def quick_tree_length_error(data, sequence_text,
                            limit: int = QUICK_TREE_MAX_SEQUENCE_BP):
    """The user-facing rejection for a Quick Tree body, or None if it is fine.

    Returns None for any submission that is not Quick-Tree-shaped, so the
    advanced workflows keep accepting the long loci they legitimately handle.
    """
    if not submission_is_quick_tree(data):
        return None
    oversized = oversized_quick_tree_records(sequence_text, limit)
    if not oversized:
        return None
    longest = max(length for _header, length in oversized)
    return (
        f"{QUICK_TREE_TOO_LONG_MESSAGE} "
        f"{len(oversized)} sequence(s) exceed the limit; the longest is "
        f"{longest:,} bp."
    )
