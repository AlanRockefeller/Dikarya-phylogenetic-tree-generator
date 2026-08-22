"""Serialization of phylogenetic trees to Newick and NEXUS.

Every tree file under ``var/jobs/<id>/tree`` must be written through this
module. Two Biopython defaults are actively wrong for this project and both are
corrected here:

1. ``Phylo.write()`` formats branch lengths with ``"%1.5f"``, so any branch
   shorter than 5e-6 becomes a hard ``0.00000``. RAxML-NG's minimum branch
   length is 1e-6 and the trees in var/jobs carry nine decimal places with
   hundreds of branches at ~6e-9, so the default silently manufactured
   zero-length branches that read as "identical sequences" downstream.

2. Biopython's NEXUS *writer* emits the ``TaxLabels`` block unquoted and
   space-separated. Fungal labels routinely contain spaces, commas,
   parentheses and semicolons, so a 147-taxon tree came out declaring
   ``NTax=147`` above 800-odd whitespace-separated tokens, and a label
   containing ``(`` or ``;`` terminated the block early. Essentially every
   NEXUS file the site had ever served was malformed. `write_nexus_tree`
   replaces that writer entirely.

`write_tree_file` is the entry point for both formats; it is re-exported from
``tree_edit_service`` under the name the rest of the codebase already uses.
"""

import logging
import re
from io import StringIO
from pathlib import Path
from typing import Optional

try:
    from Bio import Phylo
    HAS_BIOPYTHON = True
except ImportError:  # pragma: no cover - Biopython is a hard dependency in prod
    HAS_BIOPYTHON = False

logger = logging.getLogger(__name__)

# Ten decimal places covers everything the tree builders emit and, unlike a
# "%g" format, never introduces exponent notation into a file the user may open
# in FigTree or MEGA.
NEWICK_BRANCH_LENGTH_FORMAT = "%1.10f"

# A label may be left unquoted only if it is purely alphanumeric. This is
# deliberately stricter than Newick alone requires, because the same label has
# to survive NEXUS too, where the punctuation set is wider
# (``(){}/\,;:=*'"`+-<>``) and where a bare underscore is read as a space --
# unquoted ``MO142746_2`` means "MO142746 2" to a strict NEXUS reader. Quoting
# on the stricter rule keeps one representation valid in both formats.
_UNQUOTED_LABEL_RE = re.compile(r"^[A-Za-z0-9]+$")


def quote_tree_label(name: str) -> str:
    """Quote a taxon label for Newick/NEXUS, doubling any internal quote."""
    name = "" if name is None else str(name)
    if _UNQUOTED_LABEL_RE.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def tree_to_newick_string(tree) -> str:
    """Return the tree as a Newick string at full branch-length precision."""
    handle = StringIO()
    Phylo.write(
        tree, handle, "newick",
        format_branch_length=NEWICK_BRANCH_LENGTH_FORMAT,
    )
    return handle.getvalue().strip()


def _terminal_labels(tree) -> list:
    """Ordered, de-duplicated tip labels; unnamed tips are skipped."""
    seen = set()
    labels = []
    for tip in tree.get_terminals():
        name = tip.name
        if not name or name in seen:
            continue
        seen.add(name)
        labels.append(name)
    return labels


def write_nexus_tree(tree, path, tree_name: str = "tree1",
                     comment: Optional[str] = None) -> None:
    """Write a valid NEXUS file for a single tree.

    Biopython's own NEXUS writer cannot be used here (see the module
    docstring). Two things make this one safe:

    * The taxon names appear only in TAXLABELS and TRANSLATE, one per line,
      where `quote_tree_label` can quote them unambiguously.
    * The tree string itself refers to taxa by **integer**, through a TRANSLATE
      block. This is what MrBayes and PAUP* do, and it is the only reliable
      way to carry a label containing a parenthesis or a semicolon: real-world
      readers (Biopython's included) match parentheses in a tree string without
      honouring quotes, so a quoted ``'...Zeng3026(FHMU1987)'`` sitting inline
      breaks the parse even though it is legal NEXUS.
    """
    if not HAS_BIOPYTHON:
        raise RuntimeError("BioPython is required to write NEXUS trees.")

    labels = _terminal_labels(tree)
    index_by_label = {label: i + 1 for i, label in enumerate(labels)}

    # Serialize with the tips renamed to their translate indices, then put the
    # original names back on the in-memory tree so the caller's object is
    # unchanged.
    original_names = [(tip, tip.name) for tip in tree.get_terminals()]
    try:
        for tip, name in original_names:
            if name in index_by_label:
                tip.name = str(index_by_label[name])
        newick = tree_to_newick_string(tree)
    finally:
        for tip, name in original_names:
            tip.name = name

    rooted_flag = "[&R]" if getattr(tree, "rooted", False) else "[&U]"

    lines = ["#NEXUS", ""]
    if comment:
        # NEXUS comments are bracketed; strip brackets from the text so a
        # caller cannot accidentally close the comment early.
        lines.append(f"[{comment.replace('[', '(').replace(']', ')')}]")
        lines.append("")
    lines.append("BEGIN TAXA;")
    lines.append(f"    DIMENSIONS NTAX={len(labels)};")
    lines.append("    TAXLABELS")
    lines.extend(f"        {quote_tree_label(label)}" for label in labels)
    lines.append("    ;")
    lines.append("END;")
    lines.append("")
    lines.append("BEGIN TREES;")
    lines.append("    TRANSLATE")
    translate_entries = [
        f"        {index} {quote_tree_label(label)}"
        for label, index in index_by_label.items()
    ]
    lines.append(",\n".join(translate_entries) + ";")
    lines.append(f"    TREE {tree_name} = {rooted_flag} {newick}")
    lines.append("END;")
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_tree_file(tree, path, fmt: str = "newick") -> None:
    """Serialize a Bio.Phylo tree without rounding short branches away.

    Use this instead of ``Phylo.write()`` for anything under
    ``var/jobs/<id>/tree``. ``fmt="nexus"`` routes to `write_nexus_tree` rather
    than to Biopython's broken NEXUS writer.
    """
    if fmt == "nexus":
        write_nexus_tree(tree, path)
        return
    Phylo.write(
        tree, str(path), fmt,
        format_branch_length=NEWICK_BRANCH_LENGTH_FORMAT,
    )


_NTAX_TAXLABELS_RE = re.compile(
    r"dimensions\s+ntax\s*=\s*(\d+)\s*;\s*taxlabels\s+([^;]*);",
    re.IGNORECASE,
)


def validate_nexus_file(path) -> tuple:
    """Return ``(ok, reason)`` for a NEXUS tree file on disk.

    Biopython parsing is tried first, but a failure there is not conclusive:
    Biopython cannot read MrBayes' own ``.con.tre`` (it raises "Two string
    taxonomies?" on the perfectly legal combination of a TAXLABELS block and a
    TRANSLATE block), and that file is copied through verbatim because it
    carries posterior annotations our own writer does not reproduce.

    So a parse failure falls back to a structural check aimed at the defect
    that actually occurred: TAXLABELS emitted unquoted and space-separated, so
    the token count stopped matching the declared NTAX and a label containing
    ``(`` or ``;`` truncated the block.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except OSError as exc:
        return False, f"unreadable:{type(exc).__name__}"

    if HAS_BIOPYTHON:
        try:
            Phylo.read(str(path), "nexus")
            return True, "parsed"
        except Exception:
            pass  # fall through to the structural check

    if "#NEXUS" not in text[:200].upper():
        return False, "missing_nexus_header"
    if not re.search(r"begin\s+trees\s*;", text, re.IGNORECASE):
        return False, "missing_trees_block"
    if not re.search(r"^\s*tree\s+\S+\s*=", text, re.IGNORECASE | re.MULTILINE):
        return False, "no_tree_statement"

    match = _NTAX_TAXLABELS_RE.search(text)
    if match:
        declared = int(match.group(1))
        # Quoted labels are one token each however much whitespace they hold.
        tokens = re.findall(r"'(?:[^']|'')*'|\S+", match.group(2))
        if len(tokens) != declared:
            return False, f"taxlabels_{len(tokens)}_vs_ntax_{declared}"

    # The fallback is deliberately narrow. A tree statement that names taxa
    # inline is legal NEXUS, but real readers -- Biopython among them -- match
    # parentheses without honouring quotes, so a label containing "(" breaks
    # them. Only the TRANSLATE form, where the tree refers to taxa by integer,
    # is portable enough to pass without a successful parse. That is what
    # MrBayes' own .con.tre uses, and what write_nexus_tree emits.
    if not re.search(r"\btranslate\b", text, re.IGNORECASE):
        return False, "unparseable_and_no_translate_block"

    return True, "structurally_valid"


def newick_file_to_nexus(newick_path, nexus_path, comment: Optional[str] = None) -> bool:
    """Convert a Newick file to a valid NEXUS file. Returns True on success.

    Reading through Biopython un-quotes the labels and `write_nexus_tree` then
    re-quotes them under the stricter rule, so this is also what repairs a
    Newick that was quoted for Newick only.
    """
    if not HAS_BIOPYTHON:
        return False
    try:
        tree = Phylo.read(str(newick_path), "newick")
        write_nexus_tree(tree, nexus_path, comment=comment)
        return True
    except Exception as exc:
        logger.error("Failed to convert %s to NEXUS: %s", newick_path, exc)
        return False
