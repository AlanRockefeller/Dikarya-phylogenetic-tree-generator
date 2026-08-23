"""Shared validation for scientifically meaningful tree-builder parameters."""

import math
import numbers
import re


_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


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

    if isinstance(bootstrap, bool):
        count = None
    elif isinstance(bootstrap, numbers.Integral):
        count = int(bootstrap)
    elif isinstance(bootstrap, numbers.Real):
        numeric_value = float(bootstrap)
        count = (
            int(numeric_value)
            if math.isfinite(numeric_value) and numeric_value.is_integer()
            else None
        )
    elif isinstance(bootstrap, str) and _INTEGER_TEXT.fullmatch(bootstrap.strip()):
        count = int(bootstrap.strip())
    else:
        count = None

    if count is None or count < 0:
        raise ValueError(
            "IQ-TREE ultrafast bootstrap (-B) requires a non-negative integer "
            "replicate count."
        )
    if 0 < count < 1000:
        raise ValueError(
            "IQ-TREE ultrafast bootstrap (-B) requires either 0 replicates "
            f"(disabled) or at least 1000; received {count}."
        )
    return count
