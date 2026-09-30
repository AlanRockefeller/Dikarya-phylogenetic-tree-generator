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


def _split_key(clade, all_ids: frozenset, anchor) -> Optional[frozenset]:
    """The unrooted bipartition below ``clade``, or None if it is trivial.

    Keyed on terminal object identity rather than on names, so duplicate or
    missing tip labels cannot collide. The side NOT containing ``anchor`` is
    used, which makes the key independent of where the tree is rooted.
    """
    below = frozenset(id(t) for t in clade.get_terminals())
    if len(below) < 2 or len(all_ids) - len(below) < 2:
        return None
    return all_ids - below if anchor in below else below


def reroot_preserving_support(tree, reroot) -> None:
    """Run ``reroot()`` on ``tree`` and keep each support value on its split.

    Newick stores a branch's support on the node *below* that branch.
    Biopython's ``root_with_outgroup`` (and ``root_at_midpoint``, which calls
    it) reverses parent/child along the path to the new root but leaves each
    node's ``confidence`` and ``name`` where they were, so every support on
    that path ends up describing the neighbouring branch. On real jobs 444 of
    463 midpoint-rooted trees had shifted values: an SH-aLRT of 83.4 moved from
    the 91-tip clade it tested onto a 73-tip clade, and the 91-tip clade read 0.

    Support belongs to an unrooted bipartition, not to a node, so it is
    recorded per split before rerooting and written back per split afterwards.
    Both children of a bifurcating root share one split and therefore both
    carry its value; a split that became trivial (an outgroup tip's sibling)
    carries none. Internal ``name`` values are moved the same way, because
    IQ-TREE's dual "SH-aLRT/UFBoot" labels do not parse as a float and land in
    ``name`` rather than ``confidence``.
    """
    terminals = tree.get_terminals()
    all_ids = frozenset(id(t) for t in terminals)
    anchor = id(terminals[0]) if terminals else None

    labels = {}
    for clade in tree.get_nonterminals():
        if clade is tree.root:
            continue
        key = _split_key(clade, all_ids, anchor)
        if key is not None and (clade.confidence is not None or clade.name is not None):
            labels[key] = (clade.confidence, clade.name)

    reroot()

    for clade in tree.get_nonterminals():
        key = None if clade is tree.root else _split_key(clade, all_ids, anchor)
        clade.confidence, clade.name = labels.get(key, (None, None))


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


def _newick_tip_label_spans(text: str) -> list:
    """``(start, end)`` of every tip label in a Newick string, in order.

    An unlabelled tip gets an empty span at the point its label would go, so
    the list lines up one-to-one with ``tree.get_terminals()``. Quoted labels
    and bracketed comments are skipped whole, so a parenthesis or comma inside
    either is never read as structure.
    """
    spans = []
    expect_tip = False
    i, n = 0, len(text)

    def skip_quoted(pos: int) -> int:
        pos += 1
        while pos < n:
            if text[pos] == "'":
                if pos + 1 < n and text[pos + 1] == "'":
                    pos += 2
                    continue
                return pos + 1
            pos += 1
        return pos

    while i < n:
        ch = text[i]
        if ch == "[":
            depth = 0
            while i < n:
                depth += {"[": 1, "]": -1}.get(text[i], 0)
                i += 1
                if depth == 0:
                    break
            continue
        if ch.isspace():
            i += 1
            continue
        if ch == "(" or (ch == "," and not expect_tip):
            expect_tip = True
            i += 1
            continue
        if expect_tip:
            expect_tip = False
            if ch in ",:);":
                spans.append((i, i))
                continue
            start = i
            if ch == "'":
                i = skip_quoted(i)
            else:
                while i < n and text[i] not in "(),:;[]'" and not text[i].isspace():
                    i += 1
            spans.append((start, i))
            continue
        i = skip_quoted(i) if ch == "'" else i + 1
    return spans


def _unquote_newick_label(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1].replace("''", "'")
    return token


def relabel_newick_text(text: str, relabel) -> Optional[str]:
    """Rename tips in a Newick string without re-serializing anything else.

    ``relabel(name)`` returns the new tip name, or None to leave it. Only the
    renamed labels are rewritten, so branch lengths, support values and
    comments stay byte-for-byte as the tree builder wrote them -- a Biopython
    round trip would round every support value to two decimals. Returns None if
    the text cannot be matched tip-for-tip against Biopython's own parse, which
    decides what each label means, so the caller can serve the file unchanged.
    """
    if not HAS_BIOPYTHON:
        return None
    try:
        tree = Phylo.read(StringIO(text), "newick")
    except Exception as exc:
        logger.warning("Cannot parse Newick for relabelling: %s", exc)
        return None
    terminals = tree.get_terminals()
    spans = _newick_tip_label_spans(text)
    if len(spans) != len(terminals):
        return None
    edits = []
    for (start, end), tip in zip(spans, terminals):
        if _unquote_newick_label(text[start:end]) != (tip.name or ""):
            return None
        renamed = relabel(tip.name) if tip.name else None
        if renamed is not None and renamed != tip.name:
            edits.append((start, end, quote_tree_label(renamed)))
    for start, end, label in reversed(edits):
        text = text[:start] + label + text[end:]
    return text


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
    """Write a valid NEXUS file for a single tree (see `tree_to_nexus_text`)."""
    Path(path).write_text(tree_to_nexus_text(tree, tree_name, comment), encoding="utf-8")


def tree_to_nexus_text(tree, tree_name: str = "tree1",
                       comment: Optional[str] = None) -> str:
    """Render a single tree as valid NEXUS text.

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

    return "\n".join(lines)


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
    text = newick_file_to_nexus_text(newick_path, comment=comment)
    if text is None:
        return False
    Path(nexus_path).write_text(text, encoding="utf-8")
    return True


def newick_file_to_nexus_text(newick_path, comment: Optional[str] = None) -> Optional[str]:
    """Render a Newick file as NEXUS text, or None if it cannot be read.

    The in-memory half of `newick_file_to_nexus`, for callers that want to hand
    the result straight to a client instead of putting it on disk.
    """
    if not HAS_BIOPYTHON:
        return None
    try:
        tree = Phylo.read(str(newick_path), "newick")
        return tree_to_nexus_text(tree, comment=comment)
    except Exception as exc:
        logger.error("Failed to convert %s to NEXUS: %s", newick_path, exc)
        return None


def build_nexus_download(job_dir) -> Optional[tuple]:
    """Return ``(nexus_bytes, source_filename)`` for a job's NEXUS download.

    Serving ``tree/*.nexus`` off disk directly is not safe, for two reasons
    that both show up as "my NEXUS file will not open":

    * Almost every stored NEXUS predates `write_nexus_tree` and was produced by
      Biopython's writer, which emits TAXLABELS unquoted and space-separated.
      Any label with a space -- i.e. essentially all of them -- inflates the
      token count past the declared NTAX, and a label containing ``(`` or ``;``
      truncates the block outright. 82% of the ~10,500 files on disk fail
      `validate_nexus_file`; regenerating from the sibling Newick repairs all
      of them, because the Newick carries the same labels correctly quoted.
    * `tree_pruned.nexus` exists for only ~5% of jobs that have a
      `tree_pruned.newick`, so a job whose tree has been edited served the
      *unpruned* original under a name that promised the current tree, while
      the Newick download beside it served the pruned one.

    So the Newick is treated as the source of truth and the stored NEXUS is
    used only when it is both valid and no older than that Newick. Nothing is
    written back: a download is not a tree edit, and regenerating in memory
    keeps it clear of tree_state locking and of the undo snapshot.
    """
    job_dir = Path(job_dir)
    tree_dir = job_dir / "tree"

    def usable(path) -> bool:
        # The same containment rule validate_safe_file_path() applies at the
        # route: a real file, never a symlink, resolving inside the job dir.
        try:
            if path.is_symlink() or not path.is_file():
                return False
            return path.resolve().is_relative_to(job_dir.resolve())
        except OSError:
            return False

    # Same preference order as /download/tree/newick, so the two downloads can
    # never describe different trees.
    for nexus_name, newick_name in (
        ("tree_pruned.nexus", "tree_pruned.newick"),
        ("tree_original.nexus", "tree_original.newick"),
    ):
        nexus_path = tree_dir / nexus_name
        newick_path = tree_dir / newick_name
        has_nexus = usable(nexus_path)
        has_newick = usable(newick_path)
        if not has_nexus and not has_newick:
            continue

        if has_nexus and validate_nexus_file(nexus_path)[0]:
            fresh = not has_newick or (
                nexus_path.stat().st_mtime >= newick_path.stat().st_mtime
            )
            if fresh:
                return nexus_path.read_bytes(), nexus_name

        if has_newick:
            text = newick_file_to_nexus_text(newick_path)
            if text is not None:
                return text.encode("utf-8"), newick_name

        if has_nexus:
            # Unparseable and unrepairable. Handing back what we have beats a
            # 404 -- the user can still see the labels in it.
            logger.warning("Serving unrepaired NEXUS %s", nexus_path)
            return nexus_path.read_bytes(), nexus_name

    return None
