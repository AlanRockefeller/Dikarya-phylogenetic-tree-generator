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

A third Biopython default is corrected here for the same reason as the first:

3. Its Newick writer renders every node's branch length as
   ``clade.branch_length or 0.0``, so a clade that carries *no* branch length
   is written as an explicit zero. "Not measured" and "measured as zero" are
   different claims, and the second one reads as "these sequences are
   identical" -- the very inference item 1 exists to prevent. `_render_newick`
   keeps the distinction by omitting the ``:length`` token entirely for such a
   clade, which is what Newick uses to mean "unspecified".

`write_tree_file` is the entry point for both formats; it is re-exported from
``tree_edit_service`` under the name the rest of the codebase already uses.
"""

import logging
import re
from io import StringIO
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from Bio import Phylo
    HAS_BIOPYTHON = True
except ImportError:  # pragma: no cover - Biopython is a hard dependency in prod
    HAS_BIOPYTHON = False

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


# Candidate stand-ins for "this clade has no branch length". Negative, because
# no tree builder Dikarya runs emits a negative branch length -- but the choice
# is still verified against the actual tree rather than assumed (see
# `_absent_length_sentinel`), because Biopython's own NJ implementation can
# produce one.
def _absent_length_sentinel(tree) -> float:
    """Pick a branch length whose serialized token appears nowhere else.

    The comparison is on the *formatted* token, not on the float: two different
    floats can render to the same ten-decimal string, and it is the string that
    the substitution below removes.
    """
    taken = set()
    for clade in tree.find_clades():
        if clade.branch_length is not None:
            taken.add(NEWICK_BRANCH_LENGTH_FORMAT % clade.branch_length)
    # A label or a comment is copied into the output verbatim, so a sentinel
    # token occurring inside one would be deleted from it by the substitution.
    # Labels containing ':' are quoted, which does not protect them from a plain
    # str.replace, so they have to be checked explicitly.
    literals = []
    for clade in tree.find_clades():
        if clade.name is not None:
            literals.append(str(clade.name))
        comment = getattr(clade, "comment", None)
        if comment:
            literals.append(str(comment))
    # A finite candidate list creates a semantic failure path: a tree that uses
    # every entry makes absent lengths fall back to explicit zero. A finite tree
    # contains only finitely many formatted lengths and literal substrings, so
    # walking the negative integers must eventually find a collision-free token.
    candidate = -1.0
    while True:
        token = NEWICK_BRANCH_LENGTH_FORMAT % candidate
        if token not in taken and not any(token in literal for literal in literals):
            return candidate
        candidate -= 1.0


def _render_newick(tree) -> str:
    """Serialize to Newick, keeping "no branch length" distinct from zero.

    Biopython's Newick writer builds every node's suffix from
    ``clade.branch_length or 0.0``, so a clade with no branch length is written
    as an explicit zero and reloads as 0.0. Those are different statements: a
    zero-length terminal branch says two sequences are identical, which is
    exactly the false reading this module exists to prevent for short branches.
    No writer parameter reaches that expression, and hand-rolling a Newick
    emitter would put every tree the site produces behind new parsing code, so
    the absent lengths are carried through Biopython as a sentinel value and the
    sentinel's token is then removed -- leaving the clade with no ``:length`` at
    all, which is what "unspecified" looks like in Newick.

    The substitution is exact rather than merely improbable: the sentinel is
    chosen so its rendered token matches no real branch length, no taxon label
    and no comment in this tree.
    """
    absent = [clade for clade in tree.find_clades() if clade.branch_length is None]
    if not absent:
        return _biopython_newick(tree)

    sentinel = _absent_length_sentinel(tree)
    token = ":" + (NEWICK_BRANCH_LENGTH_FORMAT % sentinel)
    for clade in absent:
        clade.branch_length = sentinel
    try:
        text = _biopython_newick(tree)
    finally:
        # The caller's tree object must come back exactly as it was handed over.
        for clade in absent:
            clade.branch_length = None
    return text.replace(token, "")


def _biopython_newick(tree) -> str:
    handle = StringIO()
    Phylo.write(
        tree, handle, "newick",
        format_branch_length=NEWICK_BRANCH_LENGTH_FORMAT,
    )
    return handle.getvalue().strip()


def tree_to_newick_string(tree) -> str:
    """Return the tree as a Newick string at full branch-length precision."""
    for clade in tree.get_nonterminals():
        if clade.name is not None and clade.confidence is not None:
            # Biopython concatenates these two fields with no delimiter, turning
            # e.g. name="CladeA", confidence=95 into the invented label
            # "CladeA95". There is no portable Newick representation for two
            # independent internal annotations, so fail instead of corrupting
            # either one.
            raise ValueError(
                "Cannot serialize an internal node carrying both a name and a "
                "confidence value"
            )
    return _render_newick(tree)


def _terminal_labels(tree) -> list:
    """Return one existing, unique label per terminal or fail loudly."""
    labels = []
    seen = set()
    for position, tip in enumerate(tree.get_terminals(), start=1):
        if tip.name is None or not str(tip.name).strip():
            raise ValueError(
                f"Cannot write NEXUS: terminal taxon {position} has no label"
            )
        label = str(tip.name)
        if label in seen:
            raise ValueError(
                f"Cannot write NEXUS: duplicate terminal taxon label {label!r}"
            )
        seen.add(label)
        labels.append(label)
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

    terminals = tree.get_terminals()
    labels = _terminal_labels(tree)
    # Serialize with the tips renamed to their translate indices, then put the
    # original names back on the in-memory tree so the caller's object is
    # unchanged. Indices come from each tip's *position*, not from a lookup on
    # its name: two tips sharing a name would otherwise both be renamed to the
    # same index, and an unnamed tip would keep no index at all.
    original_names = [(tip, tip.name) for tip in terminals]
    try:
        for position, tip in enumerate(terminals, start=1):
            tip.name = str(position)
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
        for index, label in enumerate(labels, start=1)
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
    if fmt == "newick":
        # Through `tree_to_newick_string`, not `_render_newick` directly, so a
        # file on disk and a string in memory agree about clades that carry no
        # branch length *and* are subject to the same guard against an internal
        # node carrying both a name and a confidence. Rendering fully before
        # touching the path means a rejected tree leaves no truncated file
        # behind.
        text = tree_to_newick_string(tree)
        Path(path).write_text(text + "\n", encoding="utf-8")
        return
    Phylo.write(
        tree, str(path), fmt,
        format_branch_length=NEWICK_BRANCH_LENGTH_FORMAT,
    )


_NTAX_RE = re.compile(r"dimensions\s+ntax\s*=\s*(\d+)\s*;", re.IGNORECASE)
_TAXLABELS_RE = re.compile(r"taxlabels\b", re.IGNORECASE)


def _parse_taxlabels(text: str):
    """Return ``(declared_ntax, [label, ...])`` for a TAXA block, or None.

    Hand-scanned rather than matched with a regex because the block terminates
    at a semicolon *outside* quotes, and fungal labels are full of semicolons
    inside them -- a GenBank description reads "... partial sequence; 5.8S
    ribosomal RNA gene, complete sequence; and ...". A ``[^;]*`` capture stops
    at the first of those, truncating the list and reporting a bogus token
    count for a perfectly valid file.
    """
    ntax_match = _NTAX_RE.search(text)
    if not ntax_match:
        return None
    labels_match = _TAXLABELS_RE.search(text, ntax_match.end())
    if not labels_match:
        return None

    tokens: list[str] = []
    current = ""
    index, end = labels_match.end(), len(text)
    while index < end:
        char = text[index]
        if char == "'":
            # Quoted label; a doubled '' is an escaped quote, not the end.
            index += 1
            buffer = []
            while index < end:
                if text[index] == "'":
                    if index + 1 < end and text[index + 1] == "'":
                        buffer.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                buffer.append(text[index])
                index += 1
            tokens.append("".join(buffer))
            continue
        if char == ";":
            if current:
                tokens.append(current)
            return int(ntax_match.group(1)), tokens
        if char.isspace():
            if current:
                tokens.append(current)
                current = ""
            index += 1
            continue
        current += char
        index += 1
    return None  # unterminated block


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

    parsed = _parse_taxlabels(text)
    if parsed:
        declared, tokens = parsed
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
