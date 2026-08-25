from flask import (
    render_template, redirect, url_for, abort, request, current_app, flash,
    jsonify, session, send_from_directory, Response,
)
from flask_login import current_user
from jinja2 import TemplateNotFound
from app.main import bp
from app.services.security_utils import (
    safe_next_url, validate_safe_file_path, validate_job_id,
)
from app.services.access_control import check_job_access
from app.extensions import limiter, db
from io import BytesIO
import logging
import os
import re
from collections import deque
from datetime import datetime
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)


VOUCHER_LABEL_PRESETS = {
    "avery_5160": {
        "name": "Avery 5160 / 8160",
        "page_width": 8.5,
        "page_height": 11,
        "label_width": 2.625,
        "label_height": 1,
        "columns": 3,
        "rows": 10,
        "margin_left": 0.1875,
        "margin_top": 0.5,
        "gap_x": 0.125,
        "gap_y": 0,
    },
    "avery_5167": {
        "name": "Avery 5167 / 8167",
        "page_width": 8.5,
        "page_height": 11,
        "label_width": 1.75,
        "label_height": 0.5,
        "columns": 4,
        "rows": 20,
        "margin_left": 0.3125,
        # 5167 sheets are not symmetric: 0.3125 + 4x1.75 + 3x0.3 leaves 0.2875.
        "margin_right": 0.2875,
        "margin_top": 0.5,
        "gap_x": 0.3,
        "gap_y": 0,
    },
    "letter_auto": {
        "name": "8.5 x 11 letter",
        "page_width": 8.5,
        "page_height": 11,
        "label_width": 2.625,
        "label_height": 1,
        "columns": 3,
        "rows": 10,
        "margin_left": 0.25,
        "margin_top": 0.25,
        "gap_x": 0,
        "gap_y": 0,
        "auto": True,
    },
}

VOUCHER_PDF_FONT_CHOICES = {
    "helvetica": {"name": "Helvetica", "pdf": "Helvetica", "rtf": "Arial", "css": "Arial, Helvetica, sans-serif"},
    "helvetica_bold": {"name": "Helvetica Bold", "pdf": "Helvetica-Bold", "rtf": "Arial", "css": "Arial, Helvetica, sans-serif", "weight": "700"},
    "helvetica_oblique": {"name": "Helvetica Oblique", "pdf": "Helvetica-Oblique", "rtf": "Arial", "css": "Arial, Helvetica, sans-serif", "style": "italic"},
    "helvetica_bold_oblique": {"name": "Helvetica Bold Oblique", "pdf": "Helvetica-BoldOblique", "rtf": "Arial", "css": "Arial, Helvetica, sans-serif", "weight": "700", "style": "italic"},
    "times": {"name": "Times Roman", "pdf": "Times-Roman", "rtf": "Times New Roman", "css": "'Times New Roman', Times, serif"},
    "times_bold": {"name": "Times Bold", "pdf": "Times-Bold", "rtf": "Times New Roman", "css": "'Times New Roman', Times, serif", "weight": "700"},
    "times_italic": {"name": "Times Italic", "pdf": "Times-Italic", "rtf": "Times New Roman", "css": "'Times New Roman', Times, serif", "style": "italic"},
    "times_bold_italic": {"name": "Times Bold Italic", "pdf": "Times-BoldItalic", "rtf": "Times New Roman", "css": "'Times New Roman', Times, serif", "weight": "700", "style": "italic"},
    "courier": {"name": "Courier", "pdf": "Courier", "rtf": "Courier New", "css": "'Courier New', Courier, monospace"},
    "courier_bold": {"name": "Courier Bold", "pdf": "Courier-Bold", "rtf": "Courier New", "css": "'Courier New', Courier, monospace", "weight": "700"},
    "courier_oblique": {"name": "Courier Oblique", "pdf": "Courier-Oblique", "rtf": "Courier New", "css": "'Courier New', Courier, monospace", "style": "italic"},
    "courier_bold_oblique": {"name": "Courier Bold Oblique", "pdf": "Courier-BoldOblique", "rtf": "Courier New", "css": "'Courier New', Courier, monospace", "weight": "700", "style": "italic"},
    "symbol": {"name": "Symbol", "pdf": "Symbol", "rtf": "Symbol", "css": "Symbol, serif"},
    "zapf_dingbats": {"name": "Zapf Dingbats", "pdf": "ZapfDingbats", "rtf": "Zapf Dingbats", "css": "'Zapf Dingbats', serif"},
}

VOUCHER_RTF_FONT_CHOICES = {
    "ibm_plex_sans": {"name": "IBM Plex Sans", "pdf": "Helvetica", "rtf": "IBM Plex Sans", "css": "'IBM Plex Sans', system-ui, sans-serif"},
    "cormorant_garamond": {"name": "Cormorant Garamond", "pdf": "Times-Roman", "rtf": "Cormorant Garamond", "css": "'Cormorant Garamond', Georgia, serif"},
    "jetbrains_mono": {"name": "JetBrains Mono", "pdf": "Courier", "rtf": "JetBrains Mono", "css": "'JetBrains Mono', monospace"},
    "ibm_plex_mono": {"name": "IBM Plex Mono", "pdf": "Courier", "rtf": "IBM Plex Mono", "css": "'IBM Plex Mono', 'JetBrains Mono', monospace"},
    "arial": {"name": "Arial", "pdf": "Helvetica", "rtf": "Arial", "css": "Arial, Helvetica, sans-serif"},
    "calibri": {"name": "Calibri", "pdf": "Helvetica", "rtf": "Calibri", "css": "Calibri, Arial, sans-serif"},
    "cambria": {"name": "Cambria", "pdf": "Times-Roman", "rtf": "Cambria", "css": "Cambria, Georgia, serif"},
    "courier_new": {"name": "Courier New", "pdf": "Courier", "rtf": "Courier New", "css": "'Courier New', Courier, monospace"},
    "georgia": {"name": "Georgia", "pdf": "Times-Roman", "rtf": "Georgia", "css": "Georgia, 'Times New Roman', serif"},
    "symbol": {"name": "Symbol", "pdf": "Symbol", "rtf": "Symbol", "css": "Symbol, serif"},
    "times_new_roman": {"name": "Times New Roman", "pdf": "Times-Roman", "rtf": "Times New Roman", "css": "'Times New Roman', Times, serif"},
    "verdana": {"name": "Verdana", "pdf": "Helvetica", "rtf": "Verdana", "css": "Verdana, Geneva, sans-serif"},
    "wingdings": {"name": "Wingdings", "pdf": "ZapfDingbats", "rtf": "Wingdings", "css": "Wingdings, serif"},
    "zapf_dingbats": {"name": "Zapf Dingbats", "pdf": "ZapfDingbats", "rtf": "Zapf Dingbats", "css": "'Zapf Dingbats', serif"},
}


def can_edit_whats_new():
    if not current_user.is_authenticated:
        return False
    # Read through config, like _require_inat_oauth_admin below, rather than
    # os.environ: the environment is only consulted once at Config import time,
    # so a test (or any caller that sets app.config directly) sees the same
    # answer the request does. Unset means the User.is_admin flag decides,
    # which is the historical behaviour.
    editor_emails = set(current_app.config.get("WHATS_NEW_EDITOR_EMAILS") or ())
    if editor_emails:
        email = (getattr(current_user, "email", "") or "").strip().lower()
        return email in editor_emails
    return bool(getattr(current_user, "is_admin", False))


def require_whats_new_editor():
    if not can_edit_whats_new():
        abort(404)


def is_todo_admin():
    if not current_user.is_authenticated:
        return False
    admin_emails = set(current_app.config.get("TODO_ADMIN_EMAILS") or ())
    if admin_emails:
        email = (getattr(current_user, "email", "") or "").strip().lower()
        return email in admin_emails
    return bool(getattr(current_user, "is_admin", False))


def _sanitize_todo_input(name, suggestion):
    name = (name or "").strip()[:60]
    suggestion = (suggestion or "").strip()[:1000]

    # Preserve the original public todo character allowlist.
    name = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', name)
    suggestion = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', suggestion)

    name = re.sub(r'\s+', ' ', name).strip()[:60]
    suggestion = re.sub(r'\s+', ' ', suggestion).strip()[:1000]
    return name, suggestion


# Set once the legacy bootstrap has been shown to be unnecessary in this
# process, so /todo stops issuing the probe query on every single GET. Not a
# cache of the todo list itself -- only of "the one-time import is behind us",
# which cannot become false again.
_LEGACY_TODOS_IMPORTED = False


def _import_legacy_todos_if_needed():
    global _LEGACY_TODOS_IMPORTED
    from app.models import TodoSuggestion

    if _LEGACY_TODOS_IMPORTED:
        return

    if TodoSuggestion.query.first():
        _LEGACY_TODOS_IMPORTED = True
        return

    todo_file = os.path.join(current_app.root_path, 'static', 'todos.txt')
    if not os.path.exists(todo_file):
        return

    legacy_entries = []
    try:
        with open(todo_file, 'r', encoding='utf-8', errors='replace') as f:
            legacy_lines = list(deque((line.strip() for line in f), maxlen=200))
    except OSError:
        return

    for line in legacy_lines:
        if not line:
            continue
        if ': ' in line:
            raw_name, raw_suggestion = line.split(': ', 1)
        elif ':' in line:
            raw_name, raw_suggestion = line.split(':', 1)
        else:
            raw_name, raw_suggestion = "Anonymous", line
        name, suggestion = _sanitize_todo_input(raw_name or "Anonymous", raw_suggestion)
        if not suggestion:
            continue
        legacy_entries.append(TodoSuggestion(name=name or "Anonymous", suggestion=suggestion))

    if legacy_entries:
        db.session.add_all(legacy_entries)
        db.session.commit()
    _LEGACY_TODOS_IMPORTED = True


@bp.route('/tree')
def sequence_entry():
    return render_template('sequence_entry.html')


@bp.route('/help')
def help_page():
    return render_template('help.html')


# Alan 8/14/26 - The site had no robots.txt at all, so crawlers had nothing telling
# them which paths are worth fetching. nginx serves this file directly when the
# config in ops/nginx/ is installed; this route is the fallback so it works either
# way, and so it stays correct if the vhost is ever rebuilt.
@bp.route('/robots.txt')
def robots_txt():
    return send_from_directory(
        current_app.static_folder, 'robots.txt', mimetype='text/plain'
    )


# Alan 8/14/26 - The public site is only a couple of dozen pages. Listing them
# explicitly lets search engines index everything without crawling the per-job URL
# space, which is unbounded and holds a request slot per fetch.
SITEMAP_ENDPOINTS = (
    ('journal.home', 'weekly', '1.0'),
    ('journal.about', 'monthly', '0.6'),
    ('journal.current_issue', 'weekly', '0.8'),
    ('journal.archive', 'monthly', '0.6'),
    ('journal.submit_manuscript', 'monthly', '0.6'),
    ('journal.ai_policy', 'monthly', '0.6'),
    ('journal.taxonomy', 'monthly', '0.6'),
    ('journal.reviewers', 'monthly', '0.5'),
    ('journal.resources', 'monthly', '0.5'),
    ('journal.contact', 'monthly', '0.5'),
    ('journal.support', 'monthly', '0.5'),
    ('main.sequence_entry', 'weekly', '0.9'),
    ('main.help_page', 'monthly', '0.7'),
    ('main.whats_new', 'weekly', '0.7'),
    ('main.voucher_labels', 'monthly', '0.5'),
    ('main.todo', 'monthly', '0.4'),
)


@bp.route('/sitemap.xml')
def sitemap_xml():
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for endpoint, changefreq, priority in SITEMAP_ENDPOINTS:
        try:
            loc = url_for(endpoint, _external=True)
        except Exception:
            # An endpoint that has been renamed or removed should not take the
            # whole sitemap down with it.
            current_app.logger.warning("Sitemap: unknown endpoint %r; skipping", endpoint)
            continue
        lines.append(
            f'  <url><loc>{escape(loc)}</loc>'
            f'<changefreq>{changefreq}</changefreq>'
            f'<priority>{priority}</priority></url>'
        )
    lines.append('</urlset>')
    return Response("\n".join(lines) + "\n", mimetype='application/xml')


def _voucher_int(value, default, lo, hi):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _voucher_float(value, default, lo, hi):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, parsed))


def _voucher_page_count(form, labels_per_page):
    if form.get("pages") is not None:
        return _voucher_int(form.get("pages"), 1, 1, 100)
    count = _voucher_int(form.get("count"), 30, 1, 1000)
    return max(1, (count + labels_per_page - 1) // labels_per_page)


# One explicit ceiling on the length of a starting number, mirrored by
# startNumberParts() in voucher_labels.html so the browser preview and the
# generated sheet always agree. Nothing is ever truncated: a run longer than
# this is treated as unusable input and the default is used instead, and the
# page says so. The limit is also a real safety bound -- int() itself refuses a
# string past sys.get_int_max_str_digits() (4300 by default), so an unbounded
# parse here was a 500 waiting for someone to paste a wall of digits.
MAX_VOUCHER_NUMBER_DIGITS = 30


def _voucher_number_parts(form):
    prefix = re.sub(r'[\x00-\x1f\x7f]', '', form.get("prefix", "")).strip()[:32]
    start_raw = (form.get("start_number") or "001").strip()
    # fullmatch, not search: the whole field must be digits, mirroring
    # startNumberParts() in voucher_labels.html. Extracting the first digit run
    # made "ABC123" mean 123 -- a number the user never entered, with the
    # letters silently discarded rather than moved to the prefix field.
    start_match = re.fullmatch(r'\d+', start_raw)
    digits = start_match.group(0) if start_match else None
    if not digits or len(digits) > MAX_VOUCHER_NUMBER_DIGITS:
        return prefix, 1, 3
    # Python ints are exact at any size, so the voucher number is preserved
    # verbatim; only the zero-padding width is capped.
    return prefix, int(digits), max(1, min(12, len(digits)))


def _voucher_format_label(prefix, start_number, number_width, offset):
    return f"{prefix}{str(start_number + offset).zfill(number_width)}"


def _voucher_label_values(form, layout):
    prefix, start_number, number_width = _voucher_number_parts(form)
    labels_per_page = max(1, layout["preset"]["columns"] * layout["preset"]["rows"])
    count = _voucher_page_count(form, labels_per_page) * labels_per_page
    return [_voucher_format_label(prefix, start_number, number_width, i) for i in range(count)]


def _voucher_fit_count(usable_size, label_size, gap_size):
    pitch = label_size + gap_size
    if pitch <= 0:
        return 1
    # Small epsilon so a layout that fits exactly is not rounded down by
    # floating point error in the sheet dimensions.
    return max(1, int((usable_size + gap_size + 1e-6) // pitch))


def _apply_auto_voucher_layout(preset, sample_label, font_size, min_gap_x=0, min_gap_y=0):
    page_width = preset["page_width"]
    page_height = preset["page_height"]
    margin_left = preset["margin_left"]
    margin_top = preset["margin_top"]
    usable_width = max(0.5, page_width - (margin_left * 2))
    usable_height = max(0.5, page_height - (margin_top * 2))
    text_width = max(1, len(sample_label or "000")) * font_size * 0.6 / 72
    text_height = font_size / 72
    label_width = min(usable_width, max(0.35, text_width + 0.18))
    label_height = min(usable_height, max(0.22, text_height + 0.12))
    columns = _voucher_fit_count(usable_width, label_width, min_gap_x)
    rows = _voucher_fit_count(usable_height, label_height, min_gap_y)
    gap_x = (usable_width - (columns * label_width)) / (columns - 1) if columns > 1 else 0
    gap_y = (usable_height - (rows * label_height)) / (rows - 1) if rows > 1 else 0
    preset.update({
        "label_width": label_width,
        "label_height": label_height,
        "columns": columns,
        "rows": rows,
        "gap_x": max(0, gap_x),
        "gap_y": max(0, gap_y),
    })
    return preset


def _voucher_font_choices_for_output(output_format):
    if output_format == "rtf":
        return VOUCHER_RTF_FONT_CHOICES
    return VOUCHER_PDF_FONT_CHOICES


def _voucher_layout_from_form(form, sample_label=None, output_format="pdf"):
    preset_key = form.get("label_size") or "avery_5160"
    preset = dict(VOUCHER_LABEL_PRESETS.get(preset_key, VOUCHER_LABEL_PRESETS["avery_5160"]))
    font_choices = _voucher_font_choices_for_output(output_format)
    default_font_key = "ibm_plex_sans" if output_format == "rtf" else "helvetica"
    font_key = form.get("font_family") or default_font_key
    font_size = _voucher_int(form.get("font_size"), 12, 6, 36)
    min_gap_x = _voucher_float(form.get("spacing_x"), 0.1, 0, 2)
    min_gap_y = _voucher_float(form.get("spacing_y"), 0.1, 0, 2)
    if preset_key == "custom":
        label_width = _voucher_float(form.get("custom_width"), 2.625, 0.5, 8.5)
        label_height = _voucher_float(form.get("custom_height"), 1, 0.25, 5)
        columns = _voucher_int(form.get("custom_columns"), 3, 1, 8)
        rows = _voucher_int(form.get("custom_rows"), 10, 1, 40)
        margin_left = _voucher_float(form.get("custom_margin_left"), 0.25, 0, 4)
        margin_top = _voucher_float(form.get("custom_margin_top"), 0.5, 0, 4)
        gap_x = _voucher_float(form.get("custom_gap_x"), 0.125, 0, 2)
        gap_y = _voucher_float(form.get("custom_gap_y"), 0, 0, 2)
        # Every other branch clamps the grid to what the sheet can hold; the
        # custom branch did not, so a request such as width 8.5 x 2 columns
        # printed labels past the edge of the page while still reporting the
        # full count per page. The custom form has no right/bottom margin field
        # and the renderer lays labels out from margin_left/margin_top to the
        # page edge, so the usable extent subtracts the leading margin only --
        # subtracting it twice would wrongly reject the 3-column default.
        usable_width = max(0.0, 8.5 - margin_left)
        usable_height = max(0.0, 11 - margin_top)
        # Clamp the label itself to the printable extent before fitting the
        # grid. _voucher_fit_count always returns at least one column, so a
        # label wider than the page minus its margin produced a single column
        # that still ran off the sheet -- the grid fit cannot rescue a cell that
        # does not fit on its own. Clamping first also means the stored layout
        # dimensions describe what is actually printed.
        if usable_width > 0:
            label_width = min(label_width, usable_width)
        if usable_height > 0:
            label_height = min(label_height, usable_height)
        columns = min(columns, _voucher_fit_count(usable_width, label_width, gap_x))
        rows = min(rows, _voucher_fit_count(usable_height, label_height, gap_y))
        preset.update({
            "name": "Custom",
            "page_width": 8.5,
            "page_height": 11,
            "label_width": label_width,
            "label_height": label_height,
            "columns": columns,
            "rows": rows,
            "margin_left": margin_left,
            "margin_top": margin_top,
            "gap_x": gap_x,
            "gap_y": gap_y,
        })
    elif preset.get("auto"):
        prefix, start_number, number_width = _voucher_number_parts(form)
        sample = sample_label or _voucher_format_label(prefix, start_number, number_width, 0)
        preset = _apply_auto_voucher_layout(preset, sample, font_size, min_gap_x, min_gap_y)
    else:
        preset["gap_x"] = max(preset["gap_x"], min_gap_x)
        preset["gap_y"] = max(preset["gap_y"], min_gap_y)
        # Sheet margins are not necessarily symmetric, so honour an explicit
        # margin_right/margin_bottom when the preset declares one.
        margin_right = preset.get("margin_right", preset["margin_left"])
        margin_bottom = preset.get("margin_bottom", preset["margin_top"])
        usable_width = max(0.5, preset["page_width"] - preset["margin_left"] - margin_right)
        usable_height = max(0.5, preset["page_height"] - preset["margin_top"] - margin_bottom)
        available_columns = min(
            preset["columns"],
            _voucher_fit_count(usable_width, preset["label_width"], preset["gap_x"]),
        )
        preset["rows"] = min(
            preset["rows"],
            _voucher_fit_count(usable_height, preset["label_height"], preset["gap_y"]),
        )
        preset["columns"] = _voucher_int(form.get("print_columns"), available_columns, 1, available_columns)
    return {
        "preset": preset,
        "font": font_choices.get(font_key, font_choices[default_font_key]),
        "font_size": font_size,
        "include_guides": form.get("include_guides") == "1",
    }


def _voucher_labels_and_layout(values, output_format="pdf"):
    layout = _voucher_layout_from_form(values, output_format=output_format)
    labels = _voucher_label_values(values, layout)
    if not layout["preset"].get("auto"):
        return labels, layout
    for _ in range(4):
        sample_label = max(labels or [""], key=len)
        next_layout = _voucher_layout_from_form(values, sample_label=sample_label, output_format=output_format)
        next_labels = _voucher_label_values(values, next_layout)
        current_preset = layout["preset"]
        next_preset = next_layout["preset"]
        if (
            current_preset["columns"] == next_preset["columns"]
            and current_preset["rows"] == next_preset["rows"]
            and len(labels) == len(next_labels)
        ):
            return next_labels, next_layout
        layout = next_layout
        labels = next_labels
    return labels, layout


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_object(obj_id, body):
    return f"{obj_id} 0 obj\n{body}\nendobj\n".encode("latin-1")


def _build_voucher_pdf(labels, layout):
    preset = layout["preset"]
    font_name = layout["font"]["pdf"]
    font_size = layout["font_size"]
    page_w = preset["page_width"] * 72
    page_h = preset["page_height"] * 72
    label_w = preset["label_width"] * 72
    label_h = preset["label_height"] * 72
    margin_left = preset["margin_left"] * 72
    margin_top = preset["margin_top"] * 72
    gap_x = preset["gap_x"] * 72
    gap_y = preset["gap_y"] * 72
    per_page = max(1, preset["columns"] * preset["rows"])
    pages = [labels[i:i + per_page] for i in range(0, len(labels), per_page)]

    objects = []
    pages_refs = []
    next_obj = 3
    for page_labels in pages:
        content_parts = []
        for idx, label in enumerate(page_labels):
            row = idx // preset["columns"]
            col = idx % preset["columns"]
            x = margin_left + col * (label_w + gap_x)
            y_top = page_h - margin_top - row * (label_h + gap_y)
            y = y_top - (label_h / 2) - (font_size / 3)
            text_width = len(label) * font_size * 0.6
            text_x = x + max(4, (label_w - text_width) / 2)
            if layout["include_guides"]:
                content_parts.append(f"{x:.2f} {y_top - label_h:.2f} {label_w:.2f} {label_h:.2f} re S")
            content_parts.append(f"BT /F1 {font_size} Tf {text_x:.2f} {y:.2f} Td ({_pdf_escape(label)}) Tj ET")
        content = "\n".join(content_parts).encode("latin-1", errors="replace")
        content_id = next_obj
        page_id = next_obj + 1
        next_obj += 2
        objects.append(_pdf_object(content_id, f"<< /Length {len(content)} >>\nstream\n{content.decode('latin-1')}\nendstream"))
        objects.append(_pdf_object(page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.2f} {page_h:.2f}] /Resources << /Font << /F1 1 0 R >> >> /Contents {content_id} 0 R >>"))
        pages_refs.append(f"{page_id} 0 R")

    pdf = BytesIO()
    pdf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    font_object = _pdf_object(1, f"<< /Type /Font /Subtype /Type1 /BaseFont /{font_name} >>")
    pages_object = _pdf_object(2, f"<< /Type /Pages /Kids [{' '.join(pages_refs)}] /Count {len(pages_refs)} >>")
    all_objects = [font_object, pages_object] + objects
    for obj in all_objects:
        offsets.append(pdf.tell())
        pdf.write(obj)
    catalog_id = next_obj
    offsets.append(pdf.tell())
    pdf.write(_pdf_object(catalog_id, "<< /Type /Catalog /Pages 2 0 R >>"))
    xref_start = pdf.tell()
    pdf.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("latin-1"))
    for offset in offsets[1:]:
        pdf.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
    pdf.write(f"trailer\n<< /Size {len(offsets)} /Root {catalog_id} 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("latin-1"))
    return pdf.getvalue()


def _rtf_escape(value):
    """Escape label text for an RTF document body.

    RTF is a 7-bit format: the header declares \\ansi, so a raw UTF-8 byte is
    read as a single Windows-1252 character and an accented voucher prefix
    ("Boleté", "Åland") came out mojibake. Non-ASCII therefore goes out as
    \\uN? escapes, where N is the signed 16-bit UTF-16 code unit and "?" is the
    one-character fallback for readers that do not understand \\u (matching the
    \\uc1 in the header). Characters outside the BMP are emitted as their
    surrogate pair rather than being dropped.
    """
    out = []
    for ch in str(value).replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\\":
            out.append("\\\\")
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif ch == "\n":
            out.append("\\line ")
        elif ch == "\t":
            out.append("\\tab ")
        elif " " <= ch <= "~":
            out.append(ch)
        elif ord(ch) < 0x20:
            # Other control characters have no RTF meaning; drop them.
            continue
        else:
            code = ord(ch)
            if code < 0x10000:
                units = (code,)
            else:
                offset = code - 0x10000
                units = (0xD800 + (offset >> 10), 0xDC00 + (offset & 0x3FF))
            for unit in units:
                # RTF's \uN takes a *signed* 16-bit value.
                out.append(f"\\u{unit - 0x10000 if unit > 0x7FFF else unit}?")
    return "".join(out)


def _build_voucher_rtf(labels, layout):
    preset = layout["preset"]
    font = layout["font"]["rtf"]
    font_half_points = layout["font_size"] * 2
    cell_w = int(preset["label_width"] * 1440)
    cell_h = int(preset["label_height"] * 1440)
    rows = []
    for i in range(0, len(labels), preset["columns"]):
        row_labels = labels[i:i + preset["columns"]]
        row = [f"\\trowd\\trgaph0\\trleft0\\trrh{cell_h}"]
        for col in range(len(row_labels)):
            row.append(f"\\cellx{cell_w * (col + 1)}")
        for label in row_labels:
            row.append(f"\\pard\\intbl\\qc\\f0\\fs{font_half_points} {_rtf_escape(label)}\\cell")
        row.append("\\row")
        rows.append("".join(row))
    # \uc1: every \uN escape written by _rtf_escape is followed by exactly one
    # ASCII fallback character.
    return ("{\\rtf1\\ansi\\ansicpg1252\\uc1\\deff0"
            f"{{\\fonttbl{{\\f0 {font};}}}}"
            "\\margl720\\margr720\\margt720\\margb720\n"
            + "\n".join(rows)
            + "}").encode("utf-8")


@bp.route('/voucher')
@bp.route('/vouchers')
def voucherredirect():
    return redirect(url_for('main.voucher_labels'), code=301)


@bp.route('/voucher-labels', methods=['GET'])
def voucher_labels():
    return render_template(
        'voucher_labels.html',
        presets=VOUCHER_LABEL_PRESETS,
        pdf_fonts=list(VOUCHER_PDF_FONT_CHOICES.items()),
        rtf_fonts=list(VOUCHER_RTF_FONT_CHOICES.items()),
    )


# The label count is already bounded by the form parsers -- pages <= 100 and a
# per-page count that cannot exceed what physically fits on the sheet -- which
# caps a single export at 2,700 labels, ~30 ms of work and ~150 KB. So the
# workload per request is fine; what was missing was a bound on the *number* of
# requests, since this is public, unauthenticated, and answers with a generated
# document. A separate total-label cap would sit above the maximum the form can
# actually produce and would only ever reject valid input.
@bp.route('/voucher-labels/export', methods=['GET', 'POST'])
@limiter.limit("30 per minute; 300 per hour")
def voucher_labels_export():
    values = request.form if request.method == "POST" else request.args
    output_format = (values.get("output_format") or "pdf").lower()
    labels, layout = _voucher_labels_and_layout(values, output_format=output_format)
    if output_format == "rtf":
        data = _build_voucher_rtf(labels, layout)
        return Response(
            data,
            mimetype="application/rtf",
            headers={"Content-Disposition": "attachment; filename=voucher-labels.rtf"},
        )
    data = _build_voucher_pdf(labels, layout)
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=voucher-labels.pdf"},
    )


@bp.route('/test')
def dosage_test():
    try:
        return render_template('test.html')
    except TemplateNotFound:
        abort(404)


# Session key holding an anonymous visitor's last What's New view, as an ISO
# timestamp. Deliberately not a database row: see whats_new() below.
WHATS_NEW_SESSION_KEY = "whats_new_last_viewed"


def read_anonymous_whats_new_view():
    """Last time this browser opened /whats-new, or None. Never raises."""
    try:
        raw = session.get(WHATS_NEW_SESSION_KEY)
        return datetime.fromisoformat(raw) if raw else None
    except (ValueError, TypeError):
        # A cookie carrying junk in this key just means "not seen yet".
        return None


def write_anonymous_whats_new_view(seen_at):
    session[WHATS_NEW_SESSION_KEY] = seen_at.isoformat()


@bp.route('/whats-new')
def whats_new():
    from app.models import WhatsNewEntry, WhatsNewView

    entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()

    now = datetime.utcnow()
    if current_user.is_authenticated:
        # A logged-in reader has an account to hang this off, and wants the
        # badge to agree across their devices, so it stays in the database.
        view_record = WhatsNewView.query.filter_by(user_id=current_user.id).first()
        last_viewed = view_record.last_viewed_at if view_record else None
        if view_record:
            view_record.last_viewed_at = now
        else:
            db.session.add(WhatsNewView(user_id=current_user.id, last_viewed_at=now))
        db.session.commit()
    else:
        # Anonymous readers used to get a row keyed by request.remote_addr: a
        # write to the database on a GET, storing an IP address indefinitely,
        # for no purpose beyond remembering that this browser had seen the page.
        # The session cookie already is per-browser state and is exactly the
        # right size for this.
        last_viewed = read_anonymous_whats_new_view()
        write_anonymous_whats_new_view(now)

    return render_template(
        'whats_new.html',
        entries=entries,
        last_viewed=last_viewed,
        can_edit_whats_new=can_edit_whats_new(),
        edit_mode=False
    )


@bp.route('/whats-new/edit')
def whats_new_edit():
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()
    return render_template(
        'whats_new.html',
        entries=entries,
        last_viewed=None,
        can_edit_whats_new=True,
        edit_mode=True
    )


@bp.route('/whats-new/add', methods=['POST'])
def whats_new_add():
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    category = (request.form.get("category") or "update").strip().lower()

    if category not in {"feature", "fix", "improvement", "update"}:
        category = "update"

    if not title or not body:
        flash("Title and body are required.", "error")
        return redirect(url_for("main.whats_new_edit"))

    entry = WhatsNewEntry(title=title[:255], body=body, category=category)
    db.session.add(entry)
    db.session.commit()
    flash("What's New item added.", "success")
    return redirect(url_for("main.whats_new_edit"))


@bp.route('/whats-new/<int:entry_id>/edit', methods=['POST'])
def whats_new_update(entry_id):
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    entry = WhatsNewEntry.query.get_or_404(entry_id)
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    category = (request.form.get("category") or "update").strip().lower()

    if category not in {"feature", "fix", "improvement", "update"}:
        category = "update"

    if not title or not body:
        flash("Title and body are required.", "error")
        return redirect(url_for("main.whats_new_edit"))

    entry.title = title[:255]
    entry.body = body
    entry.category = category
    db.session.commit()
    flash("What's New item updated.", "success")
    return redirect(url_for("main.whats_new_edit"))


@bp.route('/whats-new/<int:entry_id>/delete', methods=['POST'])
def whats_new_delete(entry_id):
    require_whats_new_editor()

    from app.models import WhatsNewEntry

    entry = WhatsNewEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("What's New item deleted.", "success")
    # startswith("/") alone let `//evil.tld/phish` through: the browser reads
    # that as protocol-relative and leaves the site. Same validation as the
    # login form's ?next=.
    next_url = safe_next_url(request.form.get("next"))
    if next_url:
        return redirect(next_url)
    return redirect(url_for("main.whats_new_edit"))

@bp.route('/job')
def jobredirect():
    return redirect(url_for('user.user_jobs'))

@bp.route('/job/<job_id>')
def job_status(job_id):
    # Reject malformed job_ids early so the template never renders a bogus
    # UUID into the page (defense in depth; Jinja autoescape already covers
    # the HTML contexts, but this avoids wasted API calls and gives a clean
    # 400 instead of a status page that 404s on every backend call).
    if not validate_job_id(job_id):
        abort(400)
    # Initial status check (optional, could just render template)
    from app.workers.queue import get_job_status
    status_info = get_job_status(job_id)

    # Warnings recorded at submission for input that cannot produce an
    # informative tree (two sequences, or a set that is all one sequence). Shown
    # here because the submit page redirects immediately, so a toast there would
    # be gone before it was read.
    from app.models import Job
    job_record = db.session.get(Job, job_id)
    input_warnings = []
    if job_record and isinstance(job_record.metrics, dict):
        input_warnings = job_record.metrics.get("input_warnings") or []

    return render_template('job_status.html', job_id=job_id,
                           status=status_info.get('status', 'unknown'),
                           input_warnings=input_warnings)

from app.config import Config
import json

@bp.route('/job/<job_id>/view')
def job_viewer(job_id):
    # Check access control (View Mode)
    db_job, error_msg, status_code = check_job_access(job_id, mode="view")
    if error_msg:
        # Check specific status codes to provide better UX/privacy
        if status_code in (401, 403):
            # Privacy: don't reveal existence of protected jobs
            abort(404)
        # Default abort for other errors (e.g. 400)
        abort(status_code)

    # Determine View-Only status logic
    # view_only = True if the job has an owner and the current user is NOT that owner.
    # Legacy/Anonymous jobs (user_id=None) remain mutable by public (view_only=False).
    view_only = False
    if db_job and db_job.user_id is not None:
        if not current_user.is_authenticated or current_user.id != db_job.user_id:
            view_only = True

    # Fetch job details for display
    job_dir = Config.JOB_DIR / job_id
    input_info_path = job_dir / "input_info.json"
    
    job_details = {}
    if validate_safe_file_path(input_info_path, job_dir):
        try:
            with open(input_info_path, 'r') as f:
                job_details = json.load(f)
        except (OSError, ValueError) as exc:
            # The viewer still renders with no submitted-parameters panel, but
            # which job lost its metadata used to be unrecoverable from the
            # logs. ValueError covers JSONDecodeError and the UnicodeDecodeError
            # a truncated/binary file raises.
            from app.services.log_context import log_degradation
            log_degradation(
                logger, "job_metadata_unreadable",
                "Tree viewer rendered without the job's submitted parameters",
                job_id=job_id, file="input_info.json",
                exception=type(exc).__name__,
            )

    # input_info.json can carry raw JSON-request values for the boolean
    # settings, and "false" is the one that bites: a non-empty string is truthy
    # in Jinja, so the page reported a setting as on while the worker -- which
    # goes through coerce_bool -- had already run it off.
    from app.services.security_utils import coerce_bool as _coerce_bool
    for _flag in ("mcmc_stop_early", "trim_terminal_overhangs",
                  "enable_bootstrap", "moose_enabled", "early_stopping"):
        if _flag in job_details:
            job_details[_flag] = _coerce_bool(job_details[_flag], default=False)[0]

    mycomap_blast_url = job_details.get("mycomap_blast_url")
    if not mycomap_blast_url and db_job and isinstance(db_job.metrics, dict):
        mycomap_blast_url = db_job.metrics.get("mycomap_blast_url")
    if mycomap_blast_url:
        try:
            from app.services.mycomap_service import validate_mycomap_url
            mycomap_blast_url = str(mycomap_blast_url).strip()
            blast_id = validate_mycomap_url(mycomap_blast_url)
            if blast_id:
                job_details["mycomap_blast_url"] = mycomap_blast_url
            else:
                job_details.pop("mycomap_blast_url", None)
        except Exception:
            job_details.pop("mycomap_blast_url", None)
            
    # Hide the Claude review control entirely when no API key is configured,
    # rather than offering a button that can only ever answer 503.
    from app.services.tree_analysis_service import (
        is_configured as claude_review_enabled,
        resolve_tree_support_context,
    )

    # The viewer used to resolve the tree builder from input_info.json alone
    # while the review preferred the builder's own metadata, so a recomputed job
    # could show one support scale on the badge and describe another in the
    # review. Both read the same resolution now.
    try:
        tree_support_context = resolve_tree_support_context(job_dir)
    except Exception:
        tree_support_context = {
            "tree_method": job_details.get("tree_method", "") or "",
            "alrt_only": False,
        }

    return render_template(
        'job_viewer.html', job_id=job_id, job_details=job_details, view_only=view_only,
        claude_review_enabled=claude_review_enabled(),
        tree_support_context=tree_support_context,
    )

# /health moved to the monitoring blueprint (app/monitoring/routes.py) where
# it does an actual DB + filesystem check. Keeping just one /health avoids
# shadowing it with the trivial {"ok": True} stub that used to live here.


@bp.route("/test/phylotree")
def test_phylotree():
    return render_template("test_phylotree.html")


# ---------------------------------------------------------------------------
# iNaturalist OAuth (site-wide authorized account). Restricted to admin
# emails because there is only one site-wide token. Tokens are stored
# server-side and never echoed back; only generic status is returned.
# ---------------------------------------------------------------------------

INAT_OAUTH_ADMIN_EMAILS = set()


def _require_inat_oauth_admin():
    """Return None if the current user is an iNat OAuth admin, else abort(404).

    404 (not 403) so unauthorized callers cannot tell whether the route
    exists.
    """
    if not current_user.is_authenticated:
        abort(404)
    email = (getattr(current_user, "email", "") or "").strip().lower()
    admins = set(current_app.config.get("INAT_OAUTH_ADMIN_EMAILS")
                 or INAT_OAUTH_ADMIN_EMAILS)
    if email not in admins:
        abort(404)


@bp.route("/tree/oauth/connect")
def inat_oauth_connect():
    from app.services.inaturalist_oauth_service import (
        InatAuthError, authorize_url, new_oauth_state,
    )
    _require_inat_oauth_admin()
    try:
        state = new_oauth_state()
        session["inat_oauth_state"] = state
        return redirect(authorize_url(state))
    except InatAuthError as e:
        flash(f"iNaturalist OAuth not configured: {e}", "error")
        return redirect(url_for("main.sequence_entry"))


@bp.route("/tree/oauth/callback")
def inat_oauth_callback():
    from app.services.inaturalist_oauth_service import (
        InatAuthError, exchange_code_for_token,
    )
    _require_inat_oauth_admin()
    expected_state = session.pop("inat_oauth_state", None)
    state = request.args.get("state")
    code = request.args.get("code")
    if not expected_state or not state or state != expected_state:
        flash("OAuth state mismatch. Please retry.", "error")
        return redirect(url_for("main.sequence_entry"))
    if not code:
        flash(
            "iNaturalist did not return an authorization code.",
            "error",
        )
        return redirect(url_for("main.sequence_entry"))
    try:
        exchange_code_for_token(code)
    except InatAuthError as e:
        # InatAuthError messages are fixed strings carrying at most an upstream
        # status code; the service never puts a response body into one. That is
        # deliberate -- this text is rendered straight into a flash message, and
        # a token endpoint's error body can echo back the request it was given.
        flash(f"iNaturalist authorization failed: {e} "
              "The server log has the details.", "error")
        return redirect(url_for("main.sequence_entry"))
    flash(
        "iNaturalist authorization succeeded. The site can now post "
        "Phylogenetic Tree links back to observations.",
        "success",
    )
    return redirect(url_for("main.sequence_entry"))


@bp.route("/tree/oauth/status")
def inat_oauth_status():
    from app.services.inaturalist_oauth_service import is_authorized
    _require_inat_oauth_admin()
    return jsonify({"authorized": bool(is_authorized())})


@bp.route('/todo', methods=['GET', 'POST'])
# Deliberately NOT csrf-exempt: the POST writes a public suggestion, and the
# exemption let any third-party page submit one on a visitor's behalf. The form
# in todo.html carries the token like every other form in the app.
@limiter.limit("10 per minute")
def todo():
    from app.models import TodoSuggestion
    
    if request.method == 'POST':
        name = request.form.get('name', '')
        suggestion = request.form.get('suggestion', '')
        name, suggestion = _sanitize_todo_input(name, suggestion)

        # Only append if both are non-empty after sanitization
        if name and suggestion:
            db.session.add(TodoSuggestion(name=name, suggestion=suggestion, status='open'))
            db.session.commit()
                
        return redirect(url_for('main.todo'))

    _import_legacy_todos_if_needed()

    todo_admin = is_todo_admin()
    default_status_filter = 'open' if todo_admin else 'all'
    status_filter = (request.args.get('status') or default_status_filter).strip().lower()
    if status_filter not in {'open', 'done', 'all'}:
        status_filter = default_status_filter

    query = TodoSuggestion.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    todos = query.order_by(TodoSuggestion.created_at.desc(), TodoSuggestion.id.desc()).limit(200).all()
    return render_template(
        'todo.html',
        todos=todos,
        status_filter=status_filter,
        is_todo_admin=todo_admin,
    )


@bp.route('/todo/<int:suggestion_id>/status', methods=['POST'])
def todo_status(suggestion_id):
    from app.models import TodoSuggestion

    if not is_todo_admin():
        abort(404)

    next_status = (request.form.get('status') or '').strip().lower()
    if next_status not in {'open', 'done'}:
        abort(400)

    suggestion = TodoSuggestion.query.get_or_404(suggestion_id)
    now = datetime.utcnow()
    suggestion.status = next_status
    suggestion.updated_at = now
    if next_status == 'done':
        suggestion.completed_at = now
        suggestion.completed_by_id = current_user.id
    else:
        suggestion.completed_at = None
        suggestion.completed_by_id = None
    db.session.commit()

    return_status = (request.form.get('return_status') or 'open').strip().lower()
    if return_status not in {'open', 'done', 'all'}:
        return_status = 'open'
    return redirect(url_for('main.todo', status=return_status))


@bp.route('/todo/<int:suggestion_id>/delete', methods=['POST'])
def todo_delete(suggestion_id):
    from app.models import TodoSuggestion

    if not is_todo_admin():
        abort(404)

    suggestion = TodoSuggestion.query.get_or_404(suggestion_id)
    db.session.delete(suggestion)
    db.session.commit()
    flash("ToDo suggestion deleted.", "success")

    return_status = (request.form.get('return_status') or 'open').strip().lower()
    if return_status not in {'open', 'done', 'all'}:
        return_status = 'open'
    return redirect(url_for('main.todo', status=return_status))
