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
