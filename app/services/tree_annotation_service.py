"""Layered clade-line and incoming-branch annotations for the tree viewer.

Annotations can be publication-style clade brackets, multiline branch text,
rounded branch bubbles, or a translucent band painted behind a whole clade.
They live in the existing per-job ``tree_state.json`` under two keys, so no
database model and no migration are involved:

``annotation_layers``
    Ordered annotation layers. ``order: 1`` is the INNERMOST clade-line lane;
    for branch annotations order controls same-branch stacking. Each layer
    carries the default font/colour/fill used by its annotations.

``clade_annotations``
    The annotations themselves. Every style field may be ``None``, which means
    "inherit from my layer" -- layer defaults are never copied into annotations,
    so editing a layer immediately restyles everything that has no override.

Membership identity
-------------------
``member_tip_ids`` holds CANONICAL leaf names, i.e. ``original_name`` from
``tree_structure`` (what the browser calls ``__original_name``). That is the
same key ``pruned_taxa``, ``renames``, ``selection_sets`` and
``sequence_of_interest`` already use, so an annotation survives display renames,
node rotation and rerooting without storing a single coordinate.

If the current tree somehow carries two leaves with the same canonical name,
that name is AMBIGUOUS and membership referencing it is rejected rather than
silently bound to whichever leaf happens to come first. Upstream FASTA/tree
validation normally prevents this; tree serialization fails loudly if it does
occur rather than silently changing a phylogenetic tip label.
"""

import math
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

ANNOTATION_LAYERS_KEY = "annotation_layers"
CLADE_ANNOTATIONS_KEY = "clade_annotations"

# Hard limits. Deliberately generous for a publication figure but small enough
# that a hostile payload cannot bloat tree_state.json without bound.
MAX_LAYERS = 20
MAX_ANNOTATIONS = 500
MAX_MEMBERS_PER_ANNOTATION = 5000
MAX_ID_LENGTH = 64
MAX_LAYER_NAME_LENGTH = 80
MAX_LABEL_LENGTH = 500
MAX_LABEL_LINES = 10
MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 72

# A curated list rather than arbitrary CSS, so a font value can never smuggle a
# style fragment into the SVG. The renderer maps each entry to a fallback stack.
ALLOWED_FONT_FAMILIES: Tuple[str, ...] = (
    "Inter",
    "Arial",
    "Helvetica",
    "Times New Roman",
    "Georgia",
    "Verdana",
    "Courier New",
    "serif",
    "sans-serif",
    "monospace",
)
_FONT_FAMILY_LOOKUP = {name.casefold(): name for name in ALLOWED_FONT_FAMILIES}

ALLOWED_FONT_STYLES: Tuple[str, ...] = ("normal", "italic")
ALLOWED_FONT_WEIGHTS: Tuple[str, ...] = ("normal", "bold")
ALLOWED_ANNOTATION_TYPES: Tuple[str, ...] = (
    "clade_line",
    "branch_text",
    "branch_bubble",
    # Alan 8/24/26 - A translucent band painted behind the clade, under the branches.
    "clade_highlight",
)

DEFAULT_FONT_FAMILY = "Arial"
# Matches the viewer's default tip-label size (tipBasePx), so a fresh layer
# looks like it belongs to the tree it annotates.
DEFAULT_FONT_SIZE = 12
DEFAULT_FONT_STYLE = "normal"
DEFAULT_FONT_WEIGHT = "normal"
DEFAULT_TEXT_COLOR = "#1f2937"
DEFAULT_LINE_COLOR = "#1f2937"
DEFAULT_FILL_COLOR = "#ffffff"
DEFAULT_FILL_OPACITY = 0.9
# Alan 8/24/26 - Clade highlights carry their OWN fill fields rather than reusing
# fill_color/fill_opacity, which mean "branch bubble background". Sharing them would
# give every highlight the bubble default of near-opaque white and bury the tree.
#
# Alan 8/24/26 - This value is the LIGHT half of the viewer's AUTO_HIGHLIGHT_OPACITY
# ({light: 0.2, dark: 0.26} in tree_viewer_phylotree_v2.js), and must stay equal to
# it. An untouched highlight is drawn with the theme-aware automatic opacity, so a
# shared default that differed from it made every layer card report a number the
# figure did not use. Change one and you must change the other.
DEFAULT_HIGHLIGHT_COLOR = "#c9a962"
DEFAULT_HIGHLIGHT_OPACITY = 0.2
# The value layers stored for "nobody has chosen a highlight opacity" before the
# field became optional. See _normalize_layer().
LEGACY_DEFAULT_HIGHLIGHT_OPACITY = 0.15

# Alan 8/24/26 - "Is this highlight's colour chosen, or worked out from the tree?"
# stored explicitly rather than inferred from the colour value.
#
# Automatic is not one colour: it is the clade's own persistent colour group when
# it has one, otherwise the next palette colour. Before this field existed the
# renderer read "the value is still DEFAULT_HIGHLIGHT_COLOR" as "automatic", which
# left a user who deliberately picked that exact gold unable to keep it. The value
# is still honoured that way when the field is ABSENT, so state saved before this
# existed keeps behaving as it did.
HIGHLIGHT_COLOR_MODE_AUTO = "auto"
HIGHLIGHT_COLOR_MODE_FIXED = "fixed"
ALLOWED_HIGHLIGHT_COLOR_MODES: Tuple[str, ...] = (
    HIGHLIGHT_COLOR_MODE_AUTO,
    HIGHLIGHT_COLOR_MODE_FIXED,
)
DEFAULT_HIGHLIGHT_COLOR_MODE = HIGHLIGHT_COLOR_MODE_AUTO

# Alan 8/24/26 - The palette slot an automatic highlight was given when it was first
# saved. Persisted so a highlight keeps the colour the user published it with: deriving
# the colour from the annotation's position in the list meant deleting one highlight
# silently recoloured every highlight after it. Purely a cache of the automatic
# decision -- it is not a colour the user chose, and a highlight carrying one is still
# Auto, still follows its colour group if it gains one, and still takes the automatic
# theme-aware opacity.
MAX_HIGHLIGHT_SLOT = 999

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# IDs are generated by the client; keep them to a boring charset so they are
# safe as SVG/DOM attribute values and as JSON keys.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

_LAYER_STYLE_FIELDS = (
    ("default_font_family", "font_family", DEFAULT_FONT_FAMILY),
    ("default_font_size", "font_size", DEFAULT_FONT_SIZE),
    ("default_font_style", "font_style", DEFAULT_FONT_STYLE),
    ("default_font_weight", "font_weight", DEFAULT_FONT_WEIGHT),
    ("default_text_color", "text_color", DEFAULT_TEXT_COLOR),
    ("default_line_color", "line_color", DEFAULT_LINE_COLOR),
    ("default_fill_color", "fill_color", DEFAULT_FILL_COLOR),
    ("default_fill_opacity", "fill_opacity", DEFAULT_FILL_OPACITY),
    ("default_highlight_color", "highlight_color", DEFAULT_HIGHLIGHT_COLOR),
    (
        "default_highlight_color_mode",
        "highlight_color_mode",
        DEFAULT_HIGHLIGHT_COLOR_MODE,
    ),
    ("default_highlight_opacity", "highlight_opacity", DEFAULT_HIGHLIGHT_OPACITY),
)

# Alan 8/24/26 - Layer fields that are NOT backfilled with the shared default when a
# layer does not carry them. Every other layer field always stores a concrete value,
# which is why the renderer can only read "untouched" as "still equal to the shared
# default" -- a sentinel that makes the control inert at exactly that value. For
# highlight_opacity the automatic value is theme-dependent, so the difference between
# "nothing chose this" and "the user typed 0.2" is visible, and absence has to mean
# absence. highlight_color solves the same problem with an explicit mode field.
_LAYER_OPTIONAL_STYLE_FIELDS = frozenset({"default_highlight_opacity"})


class AnnotationValidationError(ValueError):
    """Raised for any malformed annotation payload; surfaces as HTTP 400."""


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _require_id(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise AnnotationValidationError(f"{what} id must be a string")
    value = value.strip()
    if not value:
        raise AnnotationValidationError(f"{what} id is required")
    if len(value) > MAX_ID_LENGTH:
        raise AnnotationValidationError(
            f"{what} id is too long (maximum {MAX_ID_LENGTH} characters)"
        )
    if not _ID_RE.match(value):
        raise AnnotationValidationError(
            f"{what} id may only contain letters, digits, '_', '-', '.' and ':'"
        )
    return value


def _require_text(value: Any, what: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise AnnotationValidationError(f"{what} must be a string")
    value = value.strip()
    if not value:
        raise AnnotationValidationError(f"{what} is required")
    if len(value) > max_length:
        raise AnnotationValidationError(
            f"{what} is too long (maximum {max_length} characters)"
        )
    if _has_control_chars(value):
        raise AnnotationValidationError(f"{what} contains control characters")
    return value


def _require_multiline_label(value: Any) -> str:
    """Validate a plain-text annotation label while retaining intentional lines."""
    if not isinstance(value, str):
        raise AnnotationValidationError("Annotation label must be a string")
    value = (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", "    ")
        .strip()
    )
    if not value:
        raise AnnotationValidationError("Annotation label is required")
    if len(value) > MAX_LABEL_LENGTH:
        raise AnnotationValidationError(
            f"Annotation label is too long (maximum {MAX_LABEL_LENGTH} characters)"
        )
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in value):
        raise AnnotationValidationError("Annotation label contains control characters")
    if value.count("\n") + 1 > MAX_LABEL_LINES:
        raise AnnotationValidationError(
            f"Annotation label may contain at most {MAX_LABEL_LINES} lines"
        )
    # Tabs have browser- and SVG-dependent widths and were normalized above.
    return value


def _require_bool(value: Any, what: str, default: bool = True) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AnnotationValidationError(f"{what} must be true or false")
    return value


def _validate_font_family(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise AnnotationValidationError(f"{what} must be a string")
    resolved = _FONT_FAMILY_LOOKUP.get(value.strip().casefold())
    if not resolved:
        raise AnnotationValidationError(
            f"{what} must be one of: " + ", ".join(ALLOWED_FONT_FAMILIES)
        )
    return resolved


def _require_finite_number(value: Any, what: str) -> float:
    """Accept only a real, finite number.

    ``json.loads`` accepts ``NaN``/``Infinity``/``-Infinity`` by default even
    though strict JSON does not, so those floats really can reach here from a
    request body. ``round()`` on them raises ValueError/OverflowError, which
    would escape validation and surface as an HTTP 500 instead of the normal
    400 an invalid payload deserves.
    """
    # bool is an int subclass; reject it explicitly so True does not become 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnnotationValidationError(f"{what} must be a number")
    try:
        # A JSON integer literal has no size limit, and float() of a 400-digit
        # int raises OverflowError rather than returning inf.
        number = float(value)
    except (OverflowError, ValueError):
        raise AnnotationValidationError(f"{what} must be a finite number")
    if not math.isfinite(number):
        raise AnnotationValidationError(f"{what} must be a finite number")
    return number


def _validate_font_size(value: Any, what: str) -> int:
    size = int(round(_require_finite_number(value, what)))
    if size < MIN_FONT_SIZE or size > MAX_FONT_SIZE:
        raise AnnotationValidationError(
            f"{what} must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE} pixels"
        )
    return size


def _validate_choice(value: Any, what: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str):
        raise AnnotationValidationError(f"{what} must be a string")
    normalized = value.strip().casefold()
    for candidate in allowed:
        if candidate == normalized:
            return candidate
    raise AnnotationValidationError(f"{what} must be one of: " + ", ".join(allowed))


def _validate_color(value: Any, what: str) -> str:
    """Accept only a normalized #RRGGBB literal.

    This is what keeps ``url(...)``, ``var(...)`` and injected style fragments
    out of the rendered SVG; the renderer writes these values straight into
    ``fill``/``stroke``.
    """
    if not isinstance(value, str):
        raise AnnotationValidationError(f"{what} must be a string")
    value = value.strip()
    if not _HEX_COLOR_RE.match(value):
        raise AnnotationValidationError(f"{what} must be a hex color such as #1f2937")
    return value.lower()


def _validate_opacity(value: Any, what: str) -> float:
    opacity = _require_finite_number(value, what)
    if opacity < 0 or opacity > 1:
        raise AnnotationValidationError(f"{what} must be between 0 and 1")
    return opacity


def _validate_style_value(field: str, value: Any, what: str):
    if field == "font_family":
        return _validate_font_family(value, what)
    if field == "font_size":
        return _validate_font_size(value, what)
    if field == "font_style":
        return _validate_choice(value, what, ALLOWED_FONT_STYLES)
    if field == "font_weight":
        return _validate_choice(value, what, ALLOWED_FONT_WEIGHTS)
    if field == "highlight_color_mode":
        return _validate_choice(value, what, ALLOWED_HIGHLIGHT_COLOR_MODES)
    # Alan 8/24/26 - Both opacity fields are bounded 0..1; every other style value
    # that is not a font property is a colour.
    if field in ("fill_opacity", "highlight_opacity"):
        return _validate_opacity(value, what)
    return _validate_color(value, what)


def _validate_annotation_type(value: Any) -> str:
    """Validate the persisted type while accepting the short-lived old aliases."""
    if value == "line":
        value = "clade_line"
    elif value == "bubble":
        value = "branch_bubble"
    return _validate_choice(value, "Annotation type", ALLOWED_ANNOTATION_TYPES)


# --- leaf identity ---------------------------------------------------------

def iter_canonical_leaf_ids(tree_json: Dict[str, Any]) -> Iterator[str]:
    """Yield one canonical name per leaf, in tree_structure document order.

    Deliberately different from ``_tree_tip_set``, which yields BOTH
    ``original_name`` and ``name`` for every leaf. Annotations need exactly one
    identity per biological leaf, and it has to be the one that survives a
    display rename -- that is ``original_name``.
    """
    def visit(node: Any) -> Iterator[str]:
        if not isinstance(node, dict):
            return
        children = node.get("children")
        if children:
            for child in children:
                yield from visit(child)
            return
        for key in ("original_name", "name"):
            value = node.get(key)
            if isinstance(value, str) and value:
                yield value
                return

    yield from visit(tree_json.get("tree_structure") or {})


def build_leaf_identity_map(tree_json: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """Return ``(resolvable_leaf_ids, ambiguous_leaf_ids)`` for the current tree.

    A canonical name that appears on more than one leaf cannot identify a single
    biological leaf, so it goes in the ambiguous set and is refused as
    annotation membership.
    """
    counts: Dict[str, int] = {}
    for name in iter_canonical_leaf_ids(tree_json):
        counts[name] = counts.get(name, 0) + 1
    resolvable = {name for name, count in counts.items() if count == 1}
    ambiguous = {name for name, count in counts.items() if count > 1}
    return resolvable, ambiguous


# --- read / write ----------------------------------------------------------

def get_annotation_config(tree_json: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Read the stored configuration, tolerating old state that has neither key."""
    layers = tree_json.get(ANNOTATION_LAYERS_KEY)
    annotations = tree_json.get(CLADE_ANNOTATIONS_KEY)
    return {
        ANNOTATION_LAYERS_KEY: layers if isinstance(layers, list) else [],
        CLADE_ANNOTATIONS_KEY: annotations if isinstance(annotations, list) else [],
    }


def apply_annotation_config(tree_json: Dict[str, Any],
                            config: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Replace ONLY the two annotation keys, leaving all other state untouched."""
    tree_json[ANNOTATION_LAYERS_KEY] = config[ANNOTATION_LAYERS_KEY]
    tree_json[CLADE_ANNOTATIONS_KEY] = config[CLADE_ANNOTATIONS_KEY]
    return tree_json


def _normalize_layer(raw: Any, index: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise AnnotationValidationError("Each annotation layer must be an object")

    layer: Dict[str, Any] = {
        "id": _require_id(raw.get("id"), "Layer"),
        "name": _require_text(raw.get("name"), "Layer name", MAX_LAYER_NAME_LENGTH),
        "visible": _require_bool(raw.get("visible"), "Layer visibility", True),
    }

    order = raw.get("order")
    if order is None:
        layer["order"] = index + 1
    else:
        layer["order"] = int(round(_require_finite_number(order, "Layer order")))

    for stored_field, style_field, default in _LAYER_STYLE_FIELDS:
        value = raw.get(stored_field)
        if value is None:
            layer[stored_field] = (
                None if stored_field in _LAYER_OPTIONAL_STYLE_FIELDS else default
            )
        else:
            layer[stored_field] = _validate_style_value(
                style_field, value, f"Layer {stored_field.replace('_', ' ')}"
            )

    # Alan 8/24/26 - A layer saved before the mode field existed says what it means
    # only through its colour: the shared default meant "nothing chose this", which
    # is Auto, and anything else was a deliberate pick. Inferring it once, here,
    # makes the stored state explicit from now on -- so a user who later picks that
    # same gold on purpose gets Fixed and keeps it.
    legacy_layer = raw.get("default_highlight_color_mode") is None
    if legacy_layer:
        layer["default_highlight_color_mode"] = (
            DEFAULT_HIGHLIGHT_COLOR_MODE
            if raw.get("default_highlight_color") in (None, DEFAULT_HIGHLIGHT_COLOR)
            else HIGHLIGHT_COLOR_MODE_FIXED
        )

    # Alan 8/24/26 - Same one-time migration for the opacity, gated on the same marker.
    # A layer written before the highlight fields existed cannot carry the mode, and it
    # always carried a concrete opacity, where the old shared default was the only thing
    # "nobody chose this" could look like. Clearing it once -- and ONLY for such a layer,
    # never on every save -- is what makes a deliberately typed 0.15 stick from now on.
    if (legacy_layer
            and layer.get("default_highlight_opacity")
            == LEGACY_DEFAULT_HIGHLIGHT_OPACITY):
        layer["default_highlight_opacity"] = None
    return layer


def _normalize_layers(raw_layers: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_layers, list):
        raise AnnotationValidationError("'layers' must be a list")
    if len(raw_layers) > MAX_LAYERS:
        raise AnnotationValidationError(
            f"No more than {MAX_LAYERS} annotation layers are supported"
        )

    layers = [_normalize_layer(raw, index) for index, raw in enumerate(raw_layers)]

    seen: Set[str] = set()
    for layer in layers:
        if layer["id"] in seen:
            raise AnnotationValidationError(f"Duplicate layer id: {layer['id']}")
        seen.add(layer["id"])

    # Deterministic, gap-free ordering. Ties keep submission order so a client
    # that sends every layer with order 1 still gets a stable inward-to-outward
    # sequence back.
    ordered = sorted(
        enumerate(layers), key=lambda pair: (pair[1]["order"], pair[0])
    )
    for position, (_, layer) in enumerate(ordered, start=1):
        layer["order"] = position
    return [layer for _, layer in ordered]


def _normalize_annotation(raw: Any, layer_ids: Set[str],
                          resolvable: Set[str],
                          ambiguous: Set[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise AnnotationValidationError("Each annotation must be an object")

    annotation: Dict[str, Any] = {
        "id": _require_id(raw.get("id"), "Annotation"),
        "label": _require_multiline_label(raw.get("label")),
        "layer_id": _require_id(raw.get("layer_id"), "Annotation layer"),
        # Older saved annotations predate selectable callout styles and retain the
        # existing publication-style clade line automatically.
        "annotation_type": _validate_annotation_type(
            raw.get("annotation_type", "clade_line")
        ),
    }
    if annotation["layer_id"] not in layer_ids:
        raise AnnotationValidationError(
            f"Annotation '{annotation['label']}' references an unknown layer"
        )

    members = raw.get("member_tip_ids")
    if not isinstance(members, list):
        raise AnnotationValidationError("member_tip_ids must be a list")
    if len(members) > MAX_MEMBERS_PER_ANNOTATION:
        raise AnnotationValidationError(
            f"An annotation may contain at most {MAX_MEMBERS_PER_ANNOTATION} members"
        )

    normalized_members: List[str] = []
    seen_members: Set[str] = set()
    for member in members:
        if not isinstance(member, str):
            raise AnnotationValidationError("member_tip_ids must contain only strings")
        member = member.strip()
        if not member:
            continue
        if member in seen_members:
            continue
        if member in ambiguous:
            raise AnnotationValidationError(
                f"'{member}' names more than one tip in this tree, so an annotation "
                "cannot be bound to it. Rename the duplicate tips first."
            )
        if member not in resolvable:
            raise AnnotationValidationError(
                f"'{member}' is not a tip in the current tree"
            )
        seen_members.add(member)
        normalized_members.append(member)

    if not normalized_members:
        raise AnnotationValidationError(
            f"Annotation '{annotation['label']}' has no members in the current tree"
        )
    annotation["member_tip_ids"] = normalized_members

    # null / missing means "inherit from the layer", so keep the key present and
    # null rather than baking the layer's current value into the annotation.
    for _, style_field, _ in _LAYER_STYLE_FIELDS:
        value = raw.get(style_field)
        if value is None:
            annotation[style_field] = None
        else:
            annotation[style_field] = _validate_style_value(
                style_field, value, f"Annotation {style_field.replace('_', ' ')}"
            )

    # Alan 8/24/26 - Optional, and absent on everything saved before it existed, in
    # which case the renderer works a slot out on the fly. Stored as the palette INDEX
    # rather than a colour so it keeps palette semantics: retuning a palette entry
    # restyles every highlight using it, exactly as changing a layer default does.
    slot = raw.get("automatic_highlight_slot")
    if slot is None:
        annotation["automatic_highlight_slot"] = None
    else:
        slot = _require_finite_number(slot, "Annotation automatic highlight slot")
        if slot != int(slot):
            raise AnnotationValidationError(
                "Annotation automatic highlight slot must be a whole number"
            )
        slot = int(slot)
        if slot < 0 or slot > MAX_HIGHLIGHT_SLOT:
            raise AnnotationValidationError(
                "Annotation automatic highlight slot must be between 0 and "
                f"{MAX_HIGHLIGHT_SLOT}"
            )
        annotation["automatic_highlight_slot"] = slot
    return annotation


def _require_collection(payload: Dict[str, Any], name: str, alias: str) -> Any:
    """Return one explicitly supplied collection, or refuse the request.

    This endpoint REPLACES the whole configuration, so an omitted collection can
    never be read as "an empty one". Treating a missing ``annotations`` key as
    ``[]`` would turn a syntactically valid but incomplete request -- a client
    bug, a truncated body, an old caller -- into a silent deletion of every
    annotation the user has. Sending ``[]`` explicitly still clears them.
    """
    for key in (name, alias):
        if key in payload:
            value = payload[key]
            if value is None:
                raise AnnotationValidationError(f"'{name}' must be a list")
            return value
    raise AnnotationValidationError(
        f"'{name}' is required. This endpoint replaces the complete annotation "
        "configuration, so send both 'layers' and 'annotations'; use an empty "
        "list to clear one of them."
    )


def normalize_annotation_config(tree_json: Dict[str, Any],
                                payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Validate a complete submitted configuration and return the stored form.

    All-or-nothing: any invalid layer or annotation raises, and the caller keeps
    the previously persisted configuration untouched. Both collections must be
    present -- see ``_require_collection`` -- so an incomplete payload cannot
    erase the half it forgot to mention.
    """
    if not isinstance(payload, dict):
        raise AnnotationValidationError("Request body must be a JSON object")

    raw_layers = _require_collection(payload, "layers", ANNOTATION_LAYERS_KEY)
    raw_annotations = _require_collection(
        payload, "annotations", CLADE_ANNOTATIONS_KEY
    )

    layers = _normalize_layers(raw_layers)
    layer_ids = {layer["id"] for layer in layers}

    if not isinstance(raw_annotations, list):
        raise AnnotationValidationError("'annotations' must be a list")
    if len(raw_annotations) > MAX_ANNOTATIONS:
        raise AnnotationValidationError(
            f"No more than {MAX_ANNOTATIONS} annotations are supported"
        )

    resolvable, ambiguous = build_leaf_identity_map(tree_json)
    if raw_annotations and not resolvable and not ambiguous:
        raise AnnotationValidationError(
            "The tree structure for this job is unavailable, so annotation "
            "membership cannot be verified"
        )

    annotations = [
        _normalize_annotation(raw, layer_ids, resolvable, ambiguous)
        for raw in raw_annotations
    ]

    seen: Set[str] = set()
    for annotation in annotations:
        if annotation["id"] in seen:
            raise AnnotationValidationError(
                f"Duplicate annotation id: {annotation['id']}"
            )
        seen.add(annotation["id"])

    return {
        ANNOTATION_LAYERS_KEY: layers,
        CLADE_ANNOTATIONS_KEY: annotations,
    }


# --- topology cleanup ------------------------------------------------------

def _rewrite_members(tree_json: Dict[str, Any], keep) -> int:
    """Drop members failing ``keep`` and delete annotations left with none.

    Returns the number of annotations that were removed entirely. An annotation
    that still has ONE member is kept: the renderer draws a single-tip tick for
    it rather than throwing the user's label away.
    """
    annotations = tree_json.get(CLADE_ANNOTATIONS_KEY)
    if not isinstance(annotations, list) or not annotations:
        return 0

    surviving: List[Dict[str, Any]] = []
    removed = 0
    for annotation in annotations:
        if not isinstance(annotation, dict):
            removed += 1
            continue
        members = annotation.get("member_tip_ids")
        if not isinstance(members, list):
            removed += 1
            continue
        kept = [m for m in members if isinstance(m, str) and keep(m)]
        if not kept:
            removed += 1
            continue
        annotation["member_tip_ids"] = kept
        surviving.append(annotation)

    tree_json[CLADE_ANNOTATIONS_KEY] = surviving
    return removed


def remove_pruned_members_from_annotations(tree_json: Dict[str, Any],
                                           removed_names: Set[str]) -> int:
    """Pruning cleanup, the annotation analogue of ``_selection_sets_without_names``.

    Only the pruned leaves leave the annotation; every other member is preserved
    and the annotation simply spans fewer tips.
    """
    if not removed_names:
        return 0
    return _rewrite_members(tree_json, lambda member: member not in removed_names)


def restrict_annotations_to_current_leaves(tree_json: Dict[str, Any]) -> int:
    """Drop members whose leaves no longer exist at all (used after recompute).

    Annotations whose members still exist are kept verbatim even when they no
    longer form one clade; the viewer flags those instead of deleting them.
    """
    resolvable, ambiguous = build_leaf_identity_map(tree_json)
    if not resolvable and not ambiguous:
        # No usable structure to compare against -- leave the user's annotations
        # alone rather than deleting them on the strength of a failed parse.
        return 0
    present = resolvable | ambiguous
    return _rewrite_members(tree_json, lambda member: member in present)
