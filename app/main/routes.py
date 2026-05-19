from flask import render_template, redirect, url_for, abort, request, current_app, flash, Response
from flask_login import current_user
from app.main import bp
from app.services.security_utils import validate_safe_file_path, validate_job_id
from app.services.access_control import check_job_access
from app.extensions import csrf, limiter, db
from io import BytesIO
import os
import re
from collections import deque
from datetime import datetime

WHATS_NEW_EDITOR_EMAIL = (os.environ.get("WHATS_NEW_EDITOR_EMAIL") or "").strip().lower()
TODO_ADMIN_DEFAULT_EMAILS = set()

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
    return (
        current_user.is_authenticated
        and (current_user.email or "").strip().lower() == WHATS_NEW_EDITOR_EMAIL
    )


def require_whats_new_editor():
    if not can_edit_whats_new():
        abort(404)


def is_todo_admin():
    if not current_user.is_authenticated:
        return False
    email = (getattr(current_user, "email", "") or "").strip().lower()
    raw_admins = os.environ.get("TODO_ADMIN_EMAILS")
    if raw_admins:
        admin_emails = {
            item.strip().lower()
            for item in raw_admins.split(",")
            if item.strip()
        }
    else:
        admin_emails = TODO_ADMIN_DEFAULT_EMAILS
    return email in admin_emails


def _sanitize_todo_input(name, suggestion):
    name = (name or "").strip()[:60]
    suggestion = (suggestion or "").strip()[:1000]

    # Preserve the original public todo character allowlist.
    name = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', name)
    suggestion = re.sub(r'[^a-zA-Z0-9 ./,:!?\'\-áéíóúüÁÉÍÓÚÜñÑ]', '', suggestion)

    name = re.sub(r'\s+', ' ', name).strip()[:60]
    suggestion = re.sub(r'\s+', ' ', suggestion).strip()[:1000]
    return name, suggestion


def _import_legacy_todos_if_needed():
    from app.models import TodoSuggestion

    if TodoSuggestion.query.first():
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


@bp.route('/tree')
def sequence_entry():
    return render_template('sequence_entry.html')


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


def _voucher_number_parts(form):
    prefix = re.sub(r'[\x00-\x1f\x7f]', '', form.get("prefix", "")).strip()[:32]
    start_raw = (form.get("start_number") or "001").strip()
    start_match = re.search(r'\d+', start_raw)
    start_number = int(start_match.group(0)) if start_match else 1
    number_width = max(1, min(12, len(start_match.group(0)) if start_match else 3))
    return prefix, start_number, number_width


def _voucher_format_label(prefix, start_number, number_width, offset):
    return f"{prefix}{str(start_number + offset).zfill(number_width)}"


def _voucher_label_values(form, layout):
    prefix, start_number, number_width = _voucher_number_parts(form)
    labels_per_page = max(1, layout["preset"]["columns"] * layout["preset"]["rows"])
    count = _voucher_page_count(form, labels_per_page) * labels_per_page
    return [_voucher_format_label(prefix, start_number, number_width, i) for i in range(count)]


def _apply_auto_voucher_layout(preset, sample_label, font_size):
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
    columns = max(1, int(usable_width // label_width))
    rows = max(1, int(usable_height // label_height))
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
    if preset_key == "custom":
        label_width = _voucher_float(form.get("custom_width"), 2.625, 0.5, 8.5)
        label_height = _voucher_float(form.get("custom_height"), 1, 0.25, 5)
        columns = _voucher_int(form.get("custom_columns"), 3, 1, 8)
        rows = _voucher_int(form.get("custom_rows"), 10, 1, 40)
        preset.update({
            "name": "Custom",
            "page_width": 8.5,
            "page_height": 11,
            "label_width": label_width,
            "label_height": label_height,
            "columns": columns,
            "rows": rows,
            "margin_left": _voucher_float(form.get("custom_margin_left"), 0.25, 0, 4),
            "margin_top": _voucher_float(form.get("custom_margin_top"), 0.5, 0, 4),
            "gap_x": _voucher_float(form.get("custom_gap_x"), 0.125, 0, 2),
            "gap_y": _voucher_float(form.get("custom_gap_y"), 0, 0, 2),
        })
    elif preset.get("auto"):
        prefix, start_number, number_width = _voucher_number_parts(form)
        sample = sample_label or _voucher_format_label(prefix, start_number, number_width, 0)
        preset = _apply_auto_voucher_layout(preset, sample, font_size)
    else:
        available_columns = preset["columns"]
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
    text = str(value).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\n", "\\line ")


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
    return ("{\\rtf1\\ansi\\deff0"
            f"{{\\fonttbl{{\\f0 {font};}}}}"
            "\\margl720\\margr720\\margt720\\margb720\n"
            + "\n".join(rows)
            + "}").encode("utf-8")


@bp.route('/voucher')
@bp.route('/vouchers')
def voucher_redirect():
    return redirect(url_for('main.voucher_labels'), code=301)


@bp.route('/voucher-labels', methods=['GET'])
def voucher_labels():
    return render_template(
        'voucher_labels.html',
        presets=VOUCHER_LABEL_PRESETS,
        pdf_fonts=list(VOUCHER_PDF_FONT_CHOICES.items()),
        rtf_fonts=list(VOUCHER_RTF_FONT_CHOICES.items()),
    )


@bp.route('/voucher-labels/export', methods=['GET', 'POST'])
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
    return render_template('test.html')


@bp.route('/whats-new')
def whats_new():
    from app.models import WhatsNewEntry, WhatsNewView

    entries = WhatsNewEntry.query.order_by(WhatsNewEntry.published_at.desc()).all()

    last_viewed = None
    if current_user.is_authenticated:
        view_record = WhatsNewView.query.filter_by(user_id=current_user.id).first()
    else:
        view_record = WhatsNewView.query.filter_by(ip_address=request.remote_addr).first()

    if view_record:
        last_viewed = view_record.last_viewed_at

    now = datetime.utcnow()
    if view_record:
        view_record.last_viewed_at = now
    else:
        if current_user.is_authenticated:
            view_record = WhatsNewView(user_id=current_user.id, last_viewed_at=now)
        else:
            view_record = WhatsNewView(ip_address=request.remote_addr, last_viewed_at=now)
        db.session.add(view_record)
    db.session.commit()

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
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("main.whats_new_edit"))

@bp.route('/job')
def job_redirect():
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
    return render_template('job_status.html', job_id=job_id, status=status_info.get('status', 'unknown'))

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
        except Exception:
            pass

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
            
    return render_template('job_viewer.html', job_id=job_id, job_details=job_details, view_only=view_only)

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
    from flask import session, redirect as _redirect
    from app.services.inaturalist_oauth_service import (
        InatAuthError, authorize_url, new_oauth_state,
    )
    _require_inat_oauth_admin()
    try:
        state = new_oauth_state()
        session["inat_oauth_state"] = state
        return _redirect(authorize_url(state))
    except InatAuthError as e:
        flash(f"iNaturalist OAuth not configured: {e}", "error")
        return _redirect(url_for("main.sequence_entry"))


@bp.route("/tree/oauth/callback")
def inat_oauth_callback():
    from flask import session, redirect as _redirect
    from app.services.inaturalist_oauth_service import (
        InatAuthError, exchange_code_for_token,
    )
    _require_inat_oauth_admin()
    expected_state = session.pop("inat_oauth_state", None)
    state = request.args.get("state")
    code = request.args.get("code")
    if not expected_state or not state or state != expected_state:
        flash("OAuth state mismatch — please retry.", "error")
        return _redirect(url_for("main.sequence_entry"))
    if not code:
        flash(
            "iNaturalist did not return an authorization code.",
            "error",
        )
        return _redirect(url_for("main.sequence_entry"))
    try:
        exchange_code_for_token(code)
    except InatAuthError as e:
        flash(f"iNaturalist authorization failed: {e}", "error")
        return _redirect(url_for("main.sequence_entry"))
    flash(
        "iNaturalist authorization succeeded. The site can now post "
        "Phylogenetic Tree links back to observations.",
        "success",
    )
    return _redirect(url_for("main.sequence_entry"))


@bp.route("/tree/oauth/status")
def inat_oauth_status():
    from flask import jsonify as _jsonify
    from app.services.inaturalist_oauth_service import is_authorized
    _require_inat_oauth_admin()
    return _jsonify({"authorized": bool(is_authorized())})


@bp.route('/todo', methods=['GET', 'POST'])
@csrf.exempt
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
