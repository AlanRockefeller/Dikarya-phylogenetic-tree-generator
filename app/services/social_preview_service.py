"""Social-media link previews for a tree page (Open Graph / Twitter card).

Two things are produced for ``/job/<id>/view``:

* the page's title -- ``"<Genus> phylogenetic tree — Dikarya"`` -- rendered
  into the initial HTML, because Facebook, X, Discord etc. do not run
  JavaScript, and
* a 1200x630 PNG of the ACTUAL tree around the job's sequence of interest,
  served lazily by ``/job/<id>/social-preview.png``.

A large tree is never shrunk whole into the card. ``select_preview_tips()``
walks up from the focal tip and keeps whole sister groups while they fit, then
the phylogenetically nearest tips of the first group that does not, then one
nearest tip from each of a few deeper levels as context. Nothing here writes
to the tree files or ``tree_state.json``: the selection is drawn from an
induced copy, and the only files written are this module's own cache under
``var/jobs/<id>/social/``.

The viewer itself renders in the browser (phylotree/D3) and has no server-side
renderer to reuse, so the card is drawn with Pillow, in the viewer's own
conventions: a rectangular phylogram, the focal tip in the viewer's focal blue
(``#1f77b4``) and the site palette for everything else.

Cache: keyed on the stat signature of the tree file the viewer draws plus
``tree_state.json`` (renames, focal choice, rooting all live there) plus
``RENDER_VERSION``. Any edit therefore changes the key, and the og:image URL
carries it as ``?v=`` so crawlers that cache by URL fetch the new image too.
Bump ``RENDER_VERSION`` when the drawing or the selection changes.
"""
from __future__ import annotations

import hashlib
import heapq
import io
import json
import logging
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from app.services.artifact_storage import default_file_mode, open_artifact, resolve_artifact
from app.services.security_utils import validate_safe_file_path

logger = logging.getLogger(__name__)

RENDER_VERSION = 1

PREVIEW_WIDTH = 1200
PREVIEW_HEIGHT = 630
PREVIEW_DESCRIPTION = "View this phylogenetic tree created with Dikarya"
FALLBACK_TITLE = "Phylogenetic tree — Dikarya"
GENUS_TITLE = "{genus} phylogenetic tree — Dikarya"
FOCAL_IMAGE_ALT = "Phylogenetic tree around the highlighted sequence and its closest relatives"
OVERVIEW_IMAGE_ALT = "Overview of a phylogenetic tree built with Dikarya"

# A tree this small is drawn whole. Above it the card shows TARGET_TIPS:
# 630 px of height holds about 22 rows at a label size that survives being
# scaled down into a feed (~17 px here, ~7 px in a Facebook card).
FULL_TREE_MAX_TIPS = 26
TARGET_TIPS = 22
# Of TARGET_TIPS, this many are reserved for one representative from each of
# the deeper ancestor levels, so the focal group is shown against its context.
CONTEXT_TIPS = 4
# The largest Newick on disk is ~140 kB; anything wildly beyond that is not a
# tree this card should spend a request parsing.
MAX_NEWICK_BYTES = 8 * 1024 * 1024
MAX_LABEL_CHARS = 200

CACHE_DIRNAME = "social"
CACHE_META_NAME = "preview.json"
CACHE_PNG_PREFIX = "preview-"

# Drawing. Everything is drawn at SCALE x and downsampled, which antialiases
# the text, the marker and the branch ends.
SCALE = 2
BG = "#ffffff"
BRANCH = "#2d4a3e"        # journal-green
LABEL = "#1f2937"
MUTED = "#6b7280"
RULE = "#e5e7eb"
GOLD = "#c9a962"          # journal-gold
DARK = "#1a2f23"          # journal-dark
FOCAL = "#1f77b4"         # the viewer's focal / sequence-of-interest blue
FOCAL_BAND = "#e8f1f9"    # FOCAL at ~10% on white

_FONT_CANDIDATES = {
    False: (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
    ),
}


class PreviewUnavailable(Exception):
    """The job has no tree this module can draw (not an error worth a traceback)."""


# ---------------------------------------------------------------------------
# Tree model
# ---------------------------------------------------------------------------

class _Node:
    __slots__ = ("name", "length", "children", "parent", "source")

    def __init__(self, name=None, length=None, parent=None, source=None):
        self.name = name
        self.length = length
        self.children: List["_Node"] = []
        self.parent = parent
        # On an induced copy, the node of the full tree this tip came from.
        self.source = source


def _preorder(root: _Node) -> Iterator[_Node]:
    """Iterative: real trees nest thousands deep, past Python's recursion limit."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _tips_under(node: _Node) -> List[_Node]:
    return [n for n in _preorder(node) if not n.children]


def parse_newick(text: str) -> _Node:
    """Parse Newick with Biopython (quoting, '' escapes) into a light iterative model."""
    from Bio import Phylo

    tree = Phylo.read(io.StringIO(text), "newick")
    root = _Node(None, None)
    stack = [(tree.root, root)]
    while stack:
        clade, node = stack.pop()
        for child in clade.clades:
            length = child.branch_length
            try:
                length = float(length) if length is not None else None
            except (TypeError, ValueError):
                length = None
            if length is not None and (length != length or length < 0):  # NaN / negative
                length = None
            sub = _Node(child.name if not child.clades else None, length, parent=node)
            node.children.append(sub)
            stack.append((child, sub))
    return root


def _has_branch_lengths(root: _Node) -> bool:
    return any(n.length and n.length > 0 for n in _preorder(root) if n is not root)


def _distances_from(focal: _Node) -> Dict[int, Tuple[float, int]]:
    """(patristic distance, edge count) from the focal tip to every node."""
    dist = {id(focal): (0.0, 0)}
    stack = [focal]
    while stack:
        node = stack.pop()
        d, hops = dist[id(node)]
        neighbours = [(child, child.length) for child in node.children]
        if node.parent is not None:
            neighbours.append((node.parent, node.length))
        for other, edge in neighbours:
            if id(other) not in dist:
                dist[id(other)] = (d + (edge or 0.0), hops + 1)
                stack.append(other)
    return dist


# ---------------------------------------------------------------------------
# Tip selection
# ---------------------------------------------------------------------------

def select_preview_tips(root: _Node, focal: Optional[_Node], *,
                        full_tree_max: int = FULL_TREE_MAX_TIPS,
                        target: int = TARGET_TIPS,
                        context: int = CONTEXT_TIPS) -> Tuple[List[_Node], str]:
    """Choose the tips the card shows. Returns (tips, mode).

    mode is "full" (the whole tree fits), "focal" (a neighbourhood of the focal
    tip) or "overview" (a large tree with no focal tip: one representative per
    major clade). The focal tip is always in the result when there is one.
    """
    tips = _tips_under(root)
    if len(tips) <= full_tree_max:
        return tips, "full"
    if focal is None:
        return _overview_tips(root, target), "overview"

    order = {id(t): i for i, t in enumerate(tips)}
    dist = _distances_from(focal)
    use_lengths = _has_branch_lengths(root)

    def closeness(tip):
        patristic, hops = dist[id(tip)]
        if use_lengths:
            return (round(patristic, 12), hops, order[id(tip)])
        return (hops, order[id(tip)])

    selected: List[_Node] = [focal]
    chosen: Set[int] = {id(focal)}
    core_budget = max(1, target - context)

    # Ancestors of the focal tip, nearest first, each with the child on the path.
    levels = []
    child, node = focal, focal.parent
    while node is not None:
        levels.append((node, child))
        child, node = node, node.parent

    overflow_at = len(levels)
    for index, (ancestor, on_path) in enumerate(levels):
        siblings = [c for c in ancestor.children if c is not on_path]
        sibling_tips = [t for c in siblings for t in _tips_under(c)]
        if len(selected) + len(sibling_tips) <= core_budget:
            for tip in sibling_tips:
                selected.append(tip)
                chosen.add(id(tip))
            continue
        # The first sister group too big to show whole: keep its tips nearest the
        # focal one, but give every sister subtree (several, at a polytomy) a
        # representative first when there is room, so none silently vanishes.
        room = max(1, core_budget - len(selected))
        representatives = sorted((min(_tips_under(c), key=closeness) for c in siblings),
                                 key=closeness)
        picks = representatives[:room]
        picked = {id(t) for t in picks}
        rest = sorted((t for t in sibling_tips if id(t) not in picked), key=closeness)
        picks += rest[:room - len(picks)]
        for tip in picks:
            selected.append(tip)
            chosen.add(id(tip))
        overflow_at = index
        break

    # Context: the nearest tip from each deeper level, so the focal group sits
    # inside the branching that places it, not floating on its own.
    for ancestor, on_path in levels[overflow_at + 1:]:
        if len(selected) >= target:
            break
        candidates = [t for c in ancestor.children if c is not on_path
                      for t in _tips_under(c) if id(t) not in chosen]
        if candidates:
            nearest = min(candidates, key=closeness)
            selected.append(nearest)
            chosen.add(id(nearest))
    # Context budget a shallow tree had no levels for goes to more near relatives.
    if overflow_at < len(levels):
        ancestor, on_path = levels[overflow_at]
        spare = sorted((t for c in ancestor.children if c is not on_path
                        for t in _tips_under(c) if id(t) not in chosen), key=closeness)
        for tip in spare[:max(0, target - len(selected))]:
            selected.append(tip)
            chosen.add(id(tip))
    return selected, "focal"


def _overview_tips(root: _Node, target: int) -> List[_Node]:
    """One tip per clade after splitting the largest clades until `target` remain."""
    sizes: Dict[int, int] = {}
    for node in reversed(list(_preorder(root))):
        sizes[id(node)] = 1 if not node.children else sum(sizes[id(c)] for c in node.children)
    heap = [(-sizes[id(root)], 0, root)]
    counter = 1
    parts = 1
    final: List[_Node] = []
    while heap:
        _, _, node = heapq.heappop(heap)
        if node.children and parts - 1 + len(node.children) <= target:
            parts += len(node.children) - 1
            for child in node.children:
                heapq.heappush(heap, (-sizes[id(child)], counter, child))
                counter += 1
        else:
            final.append(node)
    return [_tips_under(node)[0] for node in final]


def induced_subtree(root: _Node, keep: Sequence[_Node]) -> _Node:
    """Copy of the tree restricted to `keep`, unary nodes merged (lengths summed)."""
    keep_ids = {id(t) for t in keep}
    built: Dict[int, Optional[_Node]] = {}
    for node in reversed(list(_preorder(root))):
        if not node.children:
            built[id(node)] = (_Node(node.name, node.length, source=node)
                               if id(node) in keep_ids else None)
            continue
        kids = [built.pop(id(c)) for c in node.children]
        kids = [k for k in kids if k is not None]
        if not kids:
            built[id(node)] = None
        elif len(kids) == 1:
            only = kids[0]
            if only.length is None and node.length is None:
                only.length = None
            else:
                only.length = (only.length or 0.0) + (node.length or 0.0)
            built[id(node)] = only
        else:
            copy = _Node(None, node.length)
            for kid in kids:
                kid.parent = copy
                copy.children.append(kid)
            built[id(node)] = copy
    new_root = built.get(id(root))
    if new_root is None:
        raise PreviewUnavailable("no tips selected")
    new_root.parent = None
    new_root.length = None
    return new_root


# ---------------------------------------------------------------------------
# Genus
# ---------------------------------------------------------------------------

_ENVIRONMENTAL_RE = re.compile(
    r"\b(uncultured|environmental|unidentified|unclassified|metagenome|"
    r"unknown|incertae|mixed\s+sample)\b", re.IGNORECASE)
_GENUS_RE = re.compile(r"[A-Z][a-z]{2,}")
_EPITHET_RE = re.compile(r"[a-z][\w.\-']*")
_HIGHER_RANK_RE = re.compile(r"(mycota|mycotina|mycetes|mycetidae|ales|aceae|ineae|oideae|eae)$")
_NOT_A_GENUS = frozenset({
    "Fungi", "Fungus", "Fungal", "Clone", "Isolate", "Strain", "Voucher",
    "Specimen", "Sample", "Culture", "Herbarium", "Collection", "Small", "Large",
    "Internal", "Partial", "Complete", "Genomic", "Sequence", "Ribosomal",
    "Mushroom", "Observer", "Leaf", "Soil", "Root", "Endophyte", "Lichen",
    "Mycorrhizal", "Basidiomycete", "Ascomycete", "Yeast", "Mold", "Mould",
    "Hypothetical", "Predicted", "Synthetic", "Contig", "Scaffold", "Chromosome",
})
_QUOTES = "'\"‘’“”"


def _is_id_token(token: str) -> bool:
    """Accession/observation-ish tokens that lead a label: KM387401, iNat123, MO123."""
    if any(ch.isdigit() for ch in token):
        return True
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return True
    if len(letters) >= 2 and all(ch.isupper() for ch in letters):
        return True
    return bool(re.match(r"^[a-z]+[A-Z]", token))


def extract_genus(label: Optional[str]) -> Optional[str]:
    """The genus a tip label names, or None when it cannot be read reliably.

    Conservative on purpose: only the first word after the leading accession /
    observation ids is considered, it must look like a Latin genus, must not be
    a higher rank or a descriptive word, and must be followed by an epithet, a
    quoted provisional name or nothing. "uncultured ..." / environmental labels
    yield nothing at all.
    """
    if not isinstance(label, str):
        return None
    text = label.replace("_", " ").strip()
    if not text or _ENVIRONMENTAL_RE.search(text):
        return None
    tokens = [tok.strip(".,;:()[]{}|") for tok in text.split()]
    tokens = [tok for tok in tokens if tok]
    index = 0
    while index < len(tokens) and _is_id_token(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None
    candidate = tokens[index]
    if candidate in ("×", "x"):
        index += 1
        if index >= len(tokens):
            return None
        candidate = tokens[index]
    if (not _GENUS_RE.fullmatch(candidate) or candidate in _NOT_A_GENUS
            or _HIGHER_RANK_RE.search(candidate)):
        return None
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if following is None or following[0] in _QUOTES or _EPITHET_RE.fullmatch(following):
        return candidate
    return None


def dominant_genus(labels: Sequence[str]) -> Optional[str]:
    """Most common genus among `labels`, given in priority order (focal first).

    Needs at least two tips agreeing (unless the card shows three or fewer);
    a tie goes to the genus met first, i.e. the focal tip's or its nearest.
    """
    genera = [extract_genus(label) for label in labels]
    counts = Counter(g for g in genera if g)
    if not counts:
        return None
    best = max(counts.values())
    if best < 2 and len(labels) > 3:
        return None
    for genus in genera:
        if genus and counts[genus] == best:
            return genus
    return None


def title_for_genus(genus: Optional[str]) -> str:
    return GENUS_TITLE.format(genus=genus) if genus else FALLBACK_TITLE


# ---------------------------------------------------------------------------
# Job inputs (read-only)
# ---------------------------------------------------------------------------

def _tree_file(job_dir: Path) -> Optional[Path]:
    """The Newick the viewer draws: the edited tree, else the builder's."""
    for rel in ("tree/tree_pruned.newick", "tree/tree_original.newick"):
        resolved = resolve_artifact(job_dir / rel)
        if resolved is not None and validate_safe_file_path(resolved, job_dir):
            return resolved
    return None


def _load_state(job_dir: Path) -> Dict:
    """tree_state.json, read-only. Unlike load_tree_state() this never creates it."""
    path = job_dir / "tree_state.json"
    if not validate_safe_file_path(path, job_dir):
        return {}
    try:
        with open_artifact(path, "rt") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def source_signature(job_dir: Path) -> Optional[str]:
    """Cache key for the job's preview, or None when there is no tree to draw."""
    tree_path = _tree_file(job_dir)
    if tree_path is None:
        return None
    parts = [f"v{RENDER_VERSION}"]
    for path in (tree_path, job_dir / "tree_state.json"):
        try:
            st = path.stat()
            parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{path.name}:-")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _display_label(name: Optional[str], renames: Dict) -> str:
    label = renames.get(name) if isinstance(renames.get(name), str) else None
    label = (label or name or "").replace("_", " ")
    label = "".join(ch if ch.isprintable() else " " for ch in label)
    label = " ".join(label.split())
    return label[:MAX_LABEL_CHARS] or "(unnamed)"


@dataclass
class PreviewModel:
    root: _Node                  # induced subtree that is drawn
    focal: Optional[_Node]       # its focal tip, if any
    labels: Dict[int, str]       # id(tip in root) -> display label
    shown: int
    total: int
    mode: str                    # full | focal | overview
    genus: Optional[str]

    @property
    def title(self) -> str:
        return title_for_genus(self.genus)


def build_preview_model(job_dir: Path) -> PreviewModel:
    tree_path = _tree_file(job_dir)
    if tree_path is None:
        raise PreviewUnavailable("no tree file")
    with open_artifact(tree_path, "rt") as handle:
        text = handle.read(MAX_NEWICK_BYTES + 1)
    if len(text) > MAX_NEWICK_BYTES:
        raise PreviewUnavailable("tree file too large")
    if not text.strip():
        raise PreviewUnavailable("empty tree file")
    full = parse_newick(text)
    tips = _tips_under(full)
    if len(tips) < 2:
        raise PreviewUnavailable("fewer than two tips")

    state = _load_state(job_dir)
    renames = state.get("renames") if isinstance(state.get("renames"), dict) else {}
    focal_name = _resolve_focal_name(state, [t.name for t in tips if t.name])
    focal = next((t for t in tips if focal_name and t.name == focal_name), None)

    selected, mode = select_preview_tips(full, focal)
    sub = induced_subtree(full, selected)
    sub_tips = _tips_under(sub)
    labels = {id(t): _display_label(t.name, renames) for t in sub_tips}
    sub_focal = next((t for t in sub_tips if focal is not None and t.source is focal), None)

    # Genus from what the card shows, focal first then nearest, so a tie goes
    # to the focal tip's own genus.
    if mode == "focal":
        priority = [_display_label(t.name, renames) for t in selected]
    else:
        ordered = ([sub_focal] if sub_focal else []) + [t for t in sub_tips if t is not sub_focal]
        priority = [labels[id(t)] for t in ordered]
    genus = dominant_genus(priority)
    return PreviewModel(root=sub, focal=sub_focal, labels=labels, shown=len(sub_tips),
                        total=len(tips), mode=mode, genus=genus)


def _resolve_focal_name(state: Dict, tip_names: List[str]) -> Optional[str]:
    """The viewer's own focal resolution, checked against the tree actually drawn."""
    from app.services.tree_rooting_service import resolve_sequence_of_interest

    probe = dict(state)
    probe["tree_structure"] = {"children": [{"name": name} for name in tip_names]}
    try:
        name, _source, _warnings = resolve_sequence_of_interest(probe)
    except Exception:
        return None
    return name


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES[bold]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _fit_text(draw, text: str, font, max_width: float) -> str:
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid].rstrip() + ellipsis, font=font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ellipsis) if lo else ""


def _draw_footer(draw, caption: str) -> None:
    S = SCALE
    w, h = PREVIEW_WIDTH * S, PREVIEW_HEIGHT * S
    top = h - 52 * S
    draw.line([(36 * S, top), (w - 36 * S, top)], fill=RULE, width=max(1, S))
    mid = top + 26 * S
    brand_font = _font(22 * S, True)
    site_font = _font(16 * S)
    site = "dikarya.us"
    site_w = draw.textlength(site, font=site_font)
    brand_w = draw.textlength("Dikarya", font=brand_font)
    x_site = w - 36 * S - site_w
    x_brand = x_site - 14 * S - brand_w
    r = 7 * S
    x_dot = x_brand - 10 * S - r
    draw.ellipse([x_dot - r, mid - r, x_dot + r, mid + r], fill=GOLD)
    draw.text((x_brand, mid), "Dikarya", font=brand_font, fill=DARK, anchor="lm")
    draw.text((x_site, mid), site, font=site_font, fill=MUTED, anchor="lm")
    caption_font = _font(16 * S)
    caption = _fit_text(draw, caption, caption_font, x_dot - r - 24 * S - 36 * S)
    if caption:
        draw.text((36 * S, mid), caption, font=caption_font, fill=MUTED, anchor="lm")


def _caption(model: PreviewModel) -> str:
    if model.mode == "focal":
        return (f"{model.shown} of {model.total} sequences · "
                "closest relatives of the highlighted sequence")
    if model.mode == "overview":
        return f"Overview · {model.shown} of {model.total} sequences, one per clade"
    return f"{model.total} sequences"


def render_preview_png(model: PreviewModel) -> bytes:
    """Draw the model as a 1200x630 PNG."""
    from PIL import Image, ImageDraw

    S = SCALE
    W, H = PREVIEW_WIDTH * S, PREVIEW_HEIGHT * S
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, W, 6 * S], fill=GOLD)

    left, right = 40 * S, W - 36 * S
    top, bottom = 26 * S, H - 64 * S
    tips = _tips_under(model.root)
    n = len(tips)
    row = (bottom - top) / n
    font_px = int(max(12, min(24, row / S * 0.68))) * S
    font = _font(font_px)
    bold = _font(font_px, True)

    # y: tips in tree order, parents centred over their first and last child.
    y: Dict[int, float] = {}
    for i, tip in enumerate(tips):
        y[id(tip)] = top + (i + 0.5) * row
    nodes = list(_preorder(model.root))
    for node in reversed(nodes):
        if node.children:
            y[id(node)] = (y[id(node.children[0])] + y[id(node.children[-1])]) / 2

    # x: phylogram when the drawn tree has branch lengths, cladogram otherwise.
    depth: Dict[int, float] = {}
    if _has_branch_lengths(model.root):
        for node in nodes:
            if node.parent is None:
                depth[id(node)] = 0.0
            else:
                depth[id(node)] = depth[id(node.parent)] + (node.length or 0.0)
    else:
        height: Dict[int, int] = {}
        for node in reversed(nodes):
            height[id(node)] = 0 if not node.children else 1 + max(height[id(c)] for c in node.children)
        tallest = height[id(model.root)]
        for node in nodes:
            depth[id(node)] = float(tallest - height[id(node)])
    max_depth = max(depth[id(t)] for t in tips) or 1.0

    # Labels get what the longest one needs, but the tree keeps at least 30%.
    gap = 10 * S
    longest = max(draw.textlength(model.labels[id(t)], font=font) for t in tips)
    tree_width = max((right - left) * 0.30, min((right - left) * 0.55, right - left - longest - gap))
    x = {id(node): left + depth[id(node)] / max_depth * tree_width for node in nodes}

    focal_path: Set[int] = set()
    node = model.focal
    while node is not None:
        focal_path.add(id(node))
        node = node.parent

    if model.focal is not None:
        fy = y[id(model.focal)]
        half = max(row / 2 - S, 6 * S)
        draw.rounded_rectangle([left - 14 * S, fy - half, right + 10 * S, fy + half],
                               radius=6 * S, fill=FOCAL_BAND)

    width = max(2, int(2.5 * S))
    focal_width = 4 * S
    # Vertical connectors first, then horizontals over them; the focal path last.
    for on_focal in (False, True):
        for node in nodes:
            if node.children:
                ys = [y[id(c)] for c in node.children]
                draw.line([(x[id(node)], min(ys) - width / 2), (x[id(node)], max(ys) + width / 2)],
                          fill=BRANCH, width=width)
                for child in node.children:
                    is_focal = id(child) in focal_path
                    if is_focal != on_focal:
                        continue
                    if is_focal:
                        draw.line([(x[id(node)], y[id(node)]), (x[id(node)], y[id(child)])],
                                  fill=FOCAL, width=focal_width)
                    draw.line([(x[id(node)], y[id(child)]), (x[id(child)], y[id(child)])],
                              fill=FOCAL if is_focal else BRANCH,
                              width=focal_width if is_focal else width)

    for tip in tips:
        is_focal = tip is model.focal
        tx, ty = x[id(tip)], y[id(tip)]
        label_x = tx + gap
        if is_focal:
            r = max(5 * S, min(8 * S, row * 0.28))
            draw.ellipse([tx - r, ty - r, tx + r, ty + r], fill=FOCAL)
            label_x = tx + r + gap
        text = _fit_text(draw, model.labels[id(tip)], bold if is_focal else font, right - label_x)
        if text:
            draw.text((label_x, ty), text, font=bold if is_focal else font,
                      fill=FOCAL if is_focal else LABEL, anchor="lm")

    _draw_footer(draw, _caption(model))
    return _to_png(image)


def render_generic_png() -> bytes:
    """The site-wide card used when a job's own preview cannot be drawn."""
    from PIL import Image, ImageDraw

    S = SCALE
    W, H = PREVIEW_WIDTH * S, PREVIEW_HEIGHT * S
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, W, 6 * S], fill=GOLD)

    # A small stylised rectangular tree, in the card's own drawing style: tips
    # are rows 0-5 at x=300, each internal node is (x, children) and the
    # focal tip (row 3) is drawn like the real card's.
    ox, oy, step = 150 * S, 190 * S, 50 * S
    tip_x = ox + 300 * S
    nodes = {
        "a": (140, [0, 1]), "c": (190, [2, 3]), "b": (110, ["c", 4]),
        "d": (60, ["a", "b"]), "root": (20, ["d", 5]),
    }
    focal_path = {"root", "d", "b", "c", 3}

    def node_y(key):
        if isinstance(key, int):
            return oy + key * step
        ys = [node_y(child) for child in nodes[key][1]]
        return (min(ys) + max(ys)) / 2

    width = 5 * S
    for key, (nx, children) in nodes.items():
        px = ox + nx * S
        ys = [node_y(child) for child in children]
        draw.line([(px, min(ys) - width / 2), (px, max(ys) + width / 2)], fill=BRANCH, width=width)
        for child, cy in zip(children, ys):
            cx = tip_x if isinstance(child, int) else ox + nodes[child][0] * S
            on_path = child in focal_path
            if on_path:
                draw.line([(px, node_y(key)), (px, cy)], fill=FOCAL, width=7 * S)
            draw.line([(px, cy), (cx, cy)], fill=FOCAL if on_path else BRANCH,
                      width=7 * S if on_path else width)
    draw.line([(ox, node_y("root")), (ox + 20 * S, node_y("root"))], fill=FOCAL, width=7 * S)
    r = 11 * S
    focal_y = node_y(3)
    draw.ellipse([tip_x - r, focal_y - r, tip_x + r, focal_y + r], fill=FOCAL)

    tx = 560 * S
    draw.text((tx, 250 * S), "Dikarya", font=_font(96 * S, True), fill=DARK, anchor="ls")
    draw.text((tx, 310 * S), "Phylogenetic trees for fungi", font=_font(36 * S), fill=BRANCH,
              anchor="ls")
    draw.text((tx, 370 * S), PREVIEW_DESCRIPTION, font=_font(24 * S), fill=MUTED, anchor="ls")
    draw.line([(36 * S, H - 52 * S), (W - 36 * S, H - 52 * S)], fill=RULE, width=S)
    draw.text((W - 36 * S, H - 26 * S), "dikarya.us", font=_font(18 * S), fill=MUTED, anchor="rm")
    return _to_png(image)


def _to_png(image) -> bytes:
    from PIL import Image

    image = image.resize((PREVIEW_WIDTH, PREVIEW_HEIGHT), Image.Resampling.LANCZOS)
    # A handful of flat colours plus their antialiasing: a palette PNG is about
    # a quarter of the truecolour size and visually identical.
    image = image.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Cache + public entry points
# ---------------------------------------------------------------------------

def _cache_dir(job_dir: Path) -> Path:
    return job_dir / CACHE_DIRNAME


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".preview.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp creates 0600; group-writable like the rest of the job dir.
        os.chmod(temp_name, default_file_mode())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_meta(job_dir: Path) -> Dict:
    path = _cache_dir(job_dir) / CACHE_META_NAME
    if not validate_safe_file_path(path, job_dir):
        return {}
    try:
        meta = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _write_meta(job_dir: Path, meta: Dict) -> None:
    try:
        _atomic_write(_cache_dir(job_dir) / CACHE_META_NAME,
                      json.dumps(meta, separators=(",", ":")).encode())
    except OSError as exc:
        # A read-only job dir costs a recomputation per view, nothing more.
        logger.warning("event=social_preview.cache_write_failed file=%s error=%s",
                       CACHE_META_NAME, type(exc).__name__)


def _fallback_meta(signature: Optional[str] = None, error: Optional[str] = None) -> Dict:
    meta = {"signature": signature, "title": FALLBACK_TITLE, "genus": None,
            "has_preview": False, "image_alt": OVERVIEW_IMAGE_ALT}
    if error:
        meta["error"] = error
    return meta


def _meta_for(model: PreviewModel, signature: str) -> Dict:
    return {
        "signature": signature,
        "title": model.title,
        "genus": model.genus,
        "has_preview": True,
        "mode": model.mode,
        "shown": model.shown,
        "total": model.total,
        "image_alt": FOCAL_IMAGE_ALT if model.focal is not None else OVERVIEW_IMAGE_ALT,
    }


def social_metadata(job_dir: Path) -> Dict:
    """Title, image availability and cache version for the job's page head.

    Never raises: anything unexpected yields the generic title and image.
    Returns keys title, description, has_preview, version, image_alt.
    """
    try:
        meta = _social_metadata(job_dir)
    except Exception:
        logger.exception("event=social_preview.metadata_failed")
        meta = _fallback_meta()
    return {
        "title": meta.get("title") or FALLBACK_TITLE,
        "description": PREVIEW_DESCRIPTION,
        "has_preview": bool(meta.get("has_preview")),
        "version": meta.get("signature"),
        "image_alt": meta.get("image_alt") or OVERVIEW_IMAGE_ALT,
    }


def _social_metadata(job_dir: Path) -> Dict:
    signature = source_signature(job_dir)
    if signature is None:
        return _fallback_meta()
    cached = _read_meta(job_dir)
    if cached.get("signature") == signature and "title" in cached:
        return cached
    try:
        meta = _meta_for(build_preview_model(job_dir), signature)
    except PreviewUnavailable as exc:
        meta = _fallback_meta(signature, str(exc))
    except Exception as exc:
        # A malformed tree must never break the page. Cached with its signature
        # so the next view does not reparse it; any edit retries.
        logger.warning("event=social_preview.build_failed error=%s detail=%s",
                       type(exc).__name__, str(exc)[:200])
        meta = _fallback_meta(signature, type(exc).__name__)
    _write_meta(job_dir, meta)
    return meta


def preview_png(job_dir: Path, *, force: bool = False) -> Tuple[Optional[bytes], Optional[str]]:
    """(png bytes, signature) for the job's own card, or (None, None) if it has none.

    Served from the cache when the signature matches; otherwise drawn, cached
    and any older rendition removed. `force` redraws regardless.
    """
    signature = source_signature(job_dir)
    if signature is None:
        return None, None
    cache = _cache_dir(job_dir)
    png_path = cache / f"{CACHE_PNG_PREFIX}{signature}.png"
    if not force and validate_safe_file_path(png_path, job_dir):
        try:
            return png_path.read_bytes(), signature
        except OSError:
            pass
    meta = _read_meta(job_dir)
    if not force and meta.get("signature") == signature and meta.get("has_preview") is False:
        return None, None
    try:
        model = build_preview_model(job_dir)
        data = render_preview_png(model)
    except PreviewUnavailable as exc:
        _write_meta(job_dir, _fallback_meta(signature, str(exc)))
        return None, None
    except Exception as exc:
        logger.warning("event=social_preview.render_failed error=%s detail=%s",
                       type(exc).__name__, str(exc)[:200])
        _write_meta(job_dir, _fallback_meta(signature, type(exc).__name__))
        return None, None
    try:
        _atomic_write(png_path, data)
        for old in cache.glob(f"{CACHE_PNG_PREFIX}*.png"):
            if old.name != png_path.name:
                try:
                    old.unlink()
                except OSError:
                    pass
    except OSError as exc:
        logger.warning("event=social_preview.cache_write_failed file=png error=%s",
                       type(exc).__name__)
    _write_meta(job_dir, _meta_for(model, signature))
    return data, signature


DEFAULT_IMAGE_STATIC_PATH = "img/social-preview-default.png"


def generic_png(static_folder: Optional[str] = None) -> bytes:
    """The committed generic card, or a fresh rendering if it is missing."""
    if static_folder:
        try:
            return (Path(static_folder) / DEFAULT_IMAGE_STATIC_PATH).read_bytes()
        except OSError:
            pass
    return render_generic_png()
