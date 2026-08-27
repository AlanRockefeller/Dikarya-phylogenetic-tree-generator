"""Voucher Sync: read specimen voucher IDs out of iNaturalist observation
photos and reconcile them with an observation field.

Ported from the standalone desktop tool
(https://github.com/bthorson1029/inat-voucher-sync). This module is the pure
pipeline -- iNaturalist client, QR decoding, OCR fallback, per-observation
decision logic, CSV export -- with no Flask, no Tk and no token handling.
Orchestration lives in ``app/workers/voucher_sync_tasks.py`` and the HTTP
surface in ``app/api/voucher_sync_routes.py``.

Every heavy import (cv2, numpy, rapidocr_onnxruntime) is deferred into the
function that needs it so the web process never loads OpenCV; only the RQ
worker pays that cost.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
API = "https://api.inaturalist.org/v1"
WEB = "https://www.inaturalist.org"
USER_AGENT = "Dikarya Voucher Sync 1.0 (+https://dikarya.us/voucher-sync)"
REQUEST_TIMEOUT = 30

# The observation field to write vouchers into. "Personal voucher number"
# (ID 1907) is a public iNaturalist field.
DEFAULT_FIELD_NAME = "Personal voucher number"
DEFAULT_FIELD_ID = 1907

# A 2-4 character alphanumeric prefix that must contain at least one letter,
# a hyphen, and a 3-5 digit suffix, e.g. "BT-001", "AB12-34567". The lookahead
# rejects a purely numeric prefix like "12-3456" (OCR noise or a date).
DEFAULT_VOUCHER_RE = r"\b(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,4}-\d{3,5}\b"

# Reads of /observations are paged; pause between pages to stay inside the
# same budget inaturalist_service uses (RATE_LIMIT_DELAY = 1.0).
READ_PAUSE = 1.0
PER_PAGE = 200

# Cap on how much text a voucher regex is run against. Presets are linear,
# but "Custom" patterns come from users, so bound the input instead of
# trying to prove the pattern safe.
MAX_MATCH_TEXT = 500
MAX_CUSTOM_REGEX_LEN = 200

VOUCHER_FORMATS: List[Tuple[str, Optional[str]]] = [
    ("Prefix-Number", DEFAULT_VOUCHER_RE),                    # BT-001, AB12-34567
    ("Numbers only", r"\b\d{3,6}\b"),                         # 00421, 123456
    # 4-10 chars containing at least one letter and one digit.
    ("Alphanumeric",
     r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10}\b"),
    ("Custom", None),
]
DEFAULT_VOUCHER_FORMAT = VOUCHER_FORMATS[0][0]
VOUCHER_FORMAT_EXAMPLES = {
    "Prefix-Number": "BT-001, AB12-34567",
    "Numbers only": "00421, 123456",
    "Alphanumeric": "AB12, 4F9X, X7Y9Z2",
    "Custom": "your own regular expression",
}

UPDATE = "update"
SKIP = "skip"
FLAG = "flag"

CSV_COLUMNS = ["observation_id", "url", "taxon", "upload_date", "detected_voucher",
               "field_state", "current_value", "action", "reason", "raw_qr", "raw_ocr"]


class VoucherSyncError(Exception):
    """User-facing failure from the sync pipeline (message is safe to show)."""


# ---------------------------------------------------------------------------
# iNaturalist API client
# ---------------------------------------------------------------------------
class INatClient:
    """Thin ``requests`` wrapper. ``jwt`` is the short-lived API token from
    /users/api_token; it is sent as a Bearer header on authenticated calls."""

    def __init__(self, jwt: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept": "application/json"})
        self.jwt = jwt

    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt}"} if self.jwt else {}

    def verify_token(self) -> Optional[Dict[str, Any]]:
        """Return ``{"login", "id"}`` for the token's user, or None on 401."""
        r = self.session.get(f"{API}/users/me", headers=self._auth(),
                             timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            return None
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        return {"login": results[0].get("login"), "id": results[0].get("id")}

    def fetch_observations(self, user_login: str, created_d1: Optional[str] = None,
                           created_d2: Optional[str] = None,
                           max_observations: Optional[int] = None) -> Iterable[Dict[str, Any]]:
        """Page a user's observations, oldest first, optionally within a date range."""
        page = 1
        fetched = 0
        total = None
        while True:
            params: Dict[str, Any] = {
                "user_login": user_login,
                "per_page": PER_PAGE,
                "page": page,
                "order_by": "created_at",
                "order": "asc",
            }
            if created_d1:
                params["created_d1"] = created_d1
            if created_d2:
                params["created_d2"] = created_d2
            r = self.session.get(f"{API}/observations", params=params,
                                 timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            if total is None:
                total = payload.get("total_results", 0)
            results = payload.get("results", [])
            if not results:
                break
            for obs in results:
                yield obs
                fetched += 1
                if max_observations and fetched >= max_observations:
                    return
            if fetched >= total or page * PER_PAGE >= total:
                break
            page += 1
            time.sleep(READ_PAUSE)

    def fetch_observation(self, observation_id: int) -> Optional[Dict[str, Any]]:
        """Read a single observation by id."""
        r = self.session.get(f"{API}/observations/{int(observation_id)}",
                             timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def fetch_observations_by_id(self, observation_ids) -> Dict[int, Dict[str, Any]]:
        """Re-read observations by id, keyed by id.

        Used to revalidate apply targets. iNaturalist accepts a comma-separated
        `id` filter, so a whole apply set costs a handful of requests rather
        than one per row.
        """
        out: Dict[int, Dict[str, Any]] = {}
        ids = [int(i) for i in observation_ids]
        for start in range(0, len(ids), PER_PAGE):
            chunk = ids[start:start + PER_PAGE]
            r = self.session.get(f"{API}/observations",
                                 params={"id": ",".join(str(i) for i in chunk),
                                         "per_page": PER_PAGE},
                                 timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            for obs in r.json().get("results", []) or []:
                if obs.get("id") is not None:
                    out[int(obs["id"])] = obs
            if start + PER_PAGE < len(ids):
                time.sleep(READ_PAUSE)
        return out

    def create_ofv(self, observation_id: int, field_id: int, value: str) -> Dict[str, Any]:
        """Create an observation field value."""
        body = {"observation_field_value": {
            "observation_id": observation_id,
            "observation_field_id": field_id,
            "value": value,
        }}
        r = self.session.post(f"{API}/observation_field_values", json=body,
                              headers=self._auth(), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def update_ofv(self, ofv_id: int, observation_id: int, field_id: int,
                   value: str) -> Dict[str, Any]:
        """Replace an existing observation field value."""
        body = {"observation_field_value": {
            "observation_id": observation_id,
            "observation_field_id": field_id,
            "value": value,
        }}
        r = self.session.put(f"{API}/observation_field_values/{int(ofv_id)}",
                             json=body, headers=self._auth(), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def download_image(self, url: str) -> bytes:
        """Fetch photo bytes from iNaturalist's CDN."""
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    def search_observation_fields(self, query: str) -> List[Dict[str, Any]]:
        """Search observation fields by name; no auth required."""
        r = self.session.get(f"{WEB}/observation_fields.json",
                             params={"q": query}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        fields = []
        for f in r.json() or []:
            fid = f.get("id")
            name = f.get("name")
            if fid and name:
                fields.append({"id": fid, "name": name,
                               "datatype": f.get("datatype", "")})
        return fields


# ---------------------------------------------------------------------------
# Photo selection
# ---------------------------------------------------------------------------
def last_photo_url(obs: Dict[str, Any], size: str = "original") -> Optional[str]:
    """URL of the observation's last photo at the requested size, or None."""
    ophotos = obs.get("observation_photos") or []
    if not ophotos:
        return None
    ophotos = sorted(ophotos, key=lambda p: p.get("position") or 0)
    photo = ophotos[-1].get("photo") or {}
    url = photo.get("url")
    return url.replace("square", size) if url else None


# ---------------------------------------------------------------------------
# QR decoding
# ---------------------------------------------------------------------------
def _image_variants(img):
    import cv2
    yield img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    yield gray
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu
    yield cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)


def load_image(image_bytes: bytes):
    """Decode raw image bytes to a BGR ndarray. Returns (img, error)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, "cv2_not_installed"
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, "image_decode_failed"
    return img, None


def _get_candidates(img, cache: Dict[str, Any]):
    """Ranked label-region crops for ``img``, computed at most once per
    observation and shared between the QR second pass and OCR."""
    if "candidates" not in cache:
        cache["candidates"] = _label_candidates(img)
    return cache["candidates"]


def decode_qr(img, cache: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Read a QR code from `img`, trying whole-frame variants then label crops."""
    import cv2

    detector = cv2.QRCodeDetector()
    for variant in _image_variants(img):
        try:
            ok, decoded, _, _ = detector.detectAndDecodeMulti(variant)
        except cv2.error:
            ok, decoded = False, []
        if ok:
            for text in decoded:
                if text:
                    return text, None
        try:
            text, _, _ = detector.detectAndDecode(variant)
        except cv2.error:
            text = ""
        if text:
            return text, None

    # Second attempt on the deskewed, upscaled label crops: OpenCV often
    # locates a QR in the full frame but fails to decode it at that scale.
    try:
        for crop in _get_candidates(img, cache):
            for variant in (crop,
                            cv2.threshold(crop, 0, 255,
                                          cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
                try:
                    ok, decoded, _, _ = detector.detectAndDecodeMulti(variant)
                except cv2.error:
                    ok, decoded = False, []
                if ok:
                    for text in decoded:
                        if text:
                            return text, None
                try:
                    text, _, _ = detector.detectAndDecode(variant)
                except cv2.error:
                    text = ""
                if text:
                    return text, None
    except Exception:
        pass

    return None, "no_qr_detected"


def extract_voucher(text: Optional[str], voucher_re) -> Optional[str]:
    """First substring of `text` matching the voucher pattern, upper-cased."""
    if text is None:
        return None
    m = voucher_re.search(text[:MAX_MATCH_TEXT])
    return m.group(0).upper() if m else None


# ---------------------------------------------------------------------------
# Label detection + OCR fallback (RapidOCR / ONNX runtime)
# ---------------------------------------------------------------------------
def _order_points(pts):
    """Order four box corners as top-left, top-right, bottom-right, bottom-left."""
    import numpy as np
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _label_candidates(img, max_candidates: int = 4):
    """Find candidate voucher-label regions, ranked by how label-like each is
    (rectangularity and ~2.5:1 aspect), across several brightness thresholds.
    Returns upscaled, deskewed grayscale crops (possibly empty)."""
    import cv2
    import numpy as np

    h, w = img.shape[:2]
    img_area = h * w
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    ksize = max(5, min(w, h) // 80)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))

    scored = []
    for p in (94, 92, 90, 88, 86, 83, 80):
        thresh_val = int(np.percentile(gray, p))
        _, bright = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY)
        closed = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if not (img_area * 0.002 <= area <= img_area * 0.25):
                continue
            rect = cv2.minAreaRect(cnt)
            rw, rh = rect[1]
            if not (rw and rh):
                continue
            aspect = max(rw, rh) / min(rw, rh)
            if not (1.4 <= aspect <= 4.5):
                continue
            rect_area = rw * rh
            rectangularity = area / rect_area if rect_area else 0
            aspect_score = 1.0 - min(abs(aspect - 2.5) / 2.5, 1.0)
            score = rectangularity * 0.7 + aspect_score * 0.3
            scored.append((score, rect))

    scored.sort(key=lambda s: -s[0])
    chosen, seen_centers = [], []
    for score, rect in scored:
        cx, cy = rect[0]
        if any(abs(cx - sx) < 60 and abs(cy - sy) < 60 for sx, sy in seen_centers):
            continue
        seen_centers.append((cx, cy))
        chosen.append(rect)
        if len(chosen) >= max_candidates:
            break

    crops = []
    for rect in chosen:
        box = cv2.boxPoints(rect).astype(np.float32)
        src = _order_points(box)
        rw = int(max(rect[1]))
        rh = int(min(rect[1]))
        if rw < 1 or rh < 1:
            continue
        dst = np.array([[0, 0], [rw - 1, 0], [rw - 1, rh - 1], [0, rh - 1]],
                       dtype=np.float32)
        M = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(img, M, (rw, rh))
        scale = max(3.0, 500 / rh)
        crop = cv2.resize(warped, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        crops.append(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))

    return crops


_RAPIDOCR = None
_RAPIDOCR_LOCK = threading.Lock()


def _rapidocr_engine():
    """Lazily construct the RapidOCR engine once per process. Guarded by a
    lock because the scan pool runs several workers concurrently."""
    global _RAPIDOCR
    if _RAPIDOCR is None:
        with _RAPIDOCR_LOCK:
            if _RAPIDOCR is None:
                from rapidocr_onnxruntime import RapidOCR
                _RAPIDOCR = RapidOCR()
    return _RAPIDOCR


def ocr_engine_available() -> bool:
    """Whether the OCR engine is installed in this process."""
    import importlib.util
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def ocr_fallback(img, cache: Dict[str, Any], voucher_re):
    """Read the voucher from ``img`` when QR decoding failed.

    Runs the recognizer over the ranked, deskewed label crops first, then the
    full frame. Returns (voucher_id, raw_ocr_text, error_string)."""
    try:
        engine = _rapidocr_engine()
    except ImportError:
        return None, None, "rapidocr_not_installed"
    except Exception as exc:  # pragma: no cover - engine construction
        return None, None, f"rapidocr_init_failed: {exc}"

    import cv2

    def _scan(image):
        try:
            result, _ = engine(image)
        except Exception:
            return None, None
        if not result:
            return None, None
        texts = [seg[1] for seg in result if len(seg) >= 2 and seg[1]]
        for text in texts:
            voucher = extract_voucher(text, voucher_re)
            if voucher:
                return voucher, text
        joined = " ".join(texts)
        voucher = extract_voucher(joined, voucher_re)
        if voucher:
            return voucher, joined
        return None, (texts[-1] if texts else None)

    last_raw = None
    for crop in _get_candidates(img, cache):
        voucher, raw = _scan(cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR))
        last_raw = raw or last_raw
        if voucher:
            return voucher, last_raw, None

    voucher, raw = _scan(img)
    last_raw = raw or last_raw
    if voucher:
        return voucher, last_raw, None

    return None, last_raw, "ocr_no_match"


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------
def existing_ofv(obs: Dict[str, Any], field_id: int):
    """The observation's current (value, id) for `field_id`, or (None, None)."""
    for ofv in obs.get("ofvs") or []:
        if ofv.get("field_id") == field_id:
            return ofv.get("value"), ofv.get("id")
    return None, None


def taxon_label(obs: Dict[str, Any]) -> str:
    """Display name for the observation's taxon."""
    taxon = obs.get("taxon") or {}
    name = taxon.get("name") or "Unknown"
    common = taxon.get("preferred_common_name")
    return f"{name} ({common})" if common else name


def upload_date(obs: Dict[str, Any]) -> str:
    """ISO date the observation was uploaded to iNaturalist."""
    details = obs.get("created_at_details") or {}
    return details.get("date") or (obs.get("created_at") or "")[:10]


def _base_row(obs: Dict[str, Any]) -> Dict[str, Any]:
    obs_id = obs.get("id")
    return {
        "observation_id": obs_id,
        "url": f"{WEB}/observations/{obs_id}",
        "taxon": taxon_label(obs),
        "upload_date": upload_date(obs),
        "detected_voucher": None,
        "current_value": None,
        "field_state": "empty",
        "action": SKIP,
        "reason": "",
        "ofv_id": None,
        "raw_qr": None,
        "raw_ocr": None,
    }


# ---------------------------------------------------------------------------
# Row builder (the per-observation decision)
# ---------------------------------------------------------------------------
def build_row(client: INatClient, obs: Dict[str, Any], field_id: int, voucher_re,
              allow_overwrite: bool, use_ocr: bool = False) -> Dict[str, Any]:
    """Decide what should happen to one observation.

    Downloads the last photo, decodes a voucher from it (QR, then OCR when
    enabled), and compares that with the observation field's current value.
    Returns the row dict the review queue and the apply step both work from;
    `action` is one of UPDATE, SKIP or FLAG and `reason` says why.
    """
    row = _base_row(obs)

    current_value, ofv_id = existing_ofv(obs, field_id)
    row["current_value"] = current_value
    row["ofv_id"] = ofv_id
    row["field_state"] = "populated" if current_value else "empty"

    photo_url = last_photo_url(obs)
    if not photo_url:
        row["action"], row["reason"] = SKIP, "no_photos"
        return row

    try:
        image_bytes = client.download_image(photo_url)
    except requests.RequestException as exc:
        row["action"], row["reason"] = FLAG, f"photo_download_failed: {exc}"
        return row

    img, dec_err = load_image(image_bytes)
    if dec_err:
        row["action"], row["reason"] = FLAG, dec_err
        return row

    cache: Dict[str, Any] = {}
    text, qr_err = decode_qr(img, cache)

    if qr_err:
        if use_ocr:
            voucher, raw_ocr, ocr_err = ocr_fallback(img, cache, voucher_re)
            row["raw_ocr"] = raw_ocr
            if voucher:
                row["detected_voucher"] = voucher
                if not current_value:
                    row["action"], row["reason"] = UPDATE, "ocr_fallback"
                elif current_value.strip().upper() == voucher.upper():
                    row["action"], row["reason"] = SKIP, "already_correct"
                elif allow_overwrite:
                    row["action"], row["reason"] = UPDATE, "ocr_fallback_overwrite"
                else:
                    row["action"], row["reason"] = FLAG, "ocr_value_conflict"
            else:
                row["action"] = FLAG
                row["reason"] = ocr_err or qr_err
        else:
            row["action"], row["reason"] = FLAG, qr_err
        return row

    row["raw_qr"] = text
    voucher = extract_voucher(text, voucher_re)
    if not voucher:
        row["action"], row["reason"] = FLAG, "unexpected_qr_data"
        return row
    row["detected_voucher"] = voucher

    if not current_value:
        row["action"], row["reason"] = UPDATE, "field_empty"
    elif current_value.strip().upper() == voucher.upper():
        row["action"], row["reason"] = SKIP, "already_correct"
    elif allow_overwrite:
        row["action"], row["reason"] = UPDATE, "overwrite_existing"
    else:
        row["action"], row["reason"] = FLAG, "value_conflict"
    return row


def summarize_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Count rows by action, plus how many came from OCR."""
    summary = {"update": 0, "skip": 0, "flag": 0, "ocr": 0, "total": 0}
    for r in rows:
        summary["total"] += 1
        summary[r.get("action", SKIP)] = summary.get(r.get("action", SKIP), 0) + 1
        if "ocr" in (r.get("reason") or ""):
            summary["ocr"] += 1
    return summary


# ---------------------------------------------------------------------------
# Orchestration (transliterated from the desktop preview/apply workers)
# ---------------------------------------------------------------------------
def scan_observations(client: INatClient, obs_list: List[Dict[str, Any]], *,
                      field_id: int, voucher_re, allow_overwrite: bool,
                      use_ocr: bool, workers: int = 4,
                      on_row: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
                      should_cancel: Optional[Callable[[], bool]] = None):
    """Scan every observation concurrently and return ``(rows, cancelled)``.

    Photo downloads are CDN fetches, not the rate-limited API, so several run
    at once. Rows are stored by original index so the result stays in
    observation order. On cancel, not-yet-started work is dropped and whatever
    finished is returned."""
    total = len(obs_list)
    rows: List[Optional[Dict[str, Any]]] = [None] * total
    done = 0
    cancelled = False
    should_cancel = should_cancel or (lambda: False)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    future_to_idx = {
        pool.submit(build_row, client, obs, field_id, voucher_re,
                    allow_overwrite, use_ocr): idx
        for idx, obs in enumerate(obs_list)
    }
    try:
        for fut in as_completed(future_to_idx):
            # Record the finished row *before* honouring a cancel: this future
            # has already done its download and decode, and dropping it would
            # throw that work away and leave a gap in the queue.
            idx = future_to_idx[fut]
            obs = obs_list[idx]
            try:
                row = fut.result()
            except Exception as exc:
                row = _base_row(obs)
                row["action"] = FLAG
                row["reason"] = f"scan_error: {type(exc).__name__}"
                logger.warning("voucher sync scan_error obs=%s error=%s",
                               obs.get("id"), exc)
            rows[idx] = row
            done += 1
            if on_row:
                on_row(done, total, row)
            if should_cancel():
                cancelled = True
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return [r for r in rows if r is not None], cancelled


def apply_rows(client: INatClient, rows: List[Dict[str, Any]], *, field_id: int,
               allow_overwrite: bool, pause: float = 1.0,
               on_result: Optional[Callable[[int, int, Dict[str, Any], Optional[str]], None]] = None):
    """Write each UPDATE row to iNaturalist serially. Mutates rows in place
    (``action`` -> skip, ``reason`` -> applied) on success. Returns
    ``(applied, failed)``."""
    total = len(rows)
    applied = failed = 0
    for i, r in enumerate(rows, 1):
        obs_id = r["observation_id"]
        voucher = r["detected_voucher"]
        error = None
        try:
            if r.get("ofv_id") and allow_overwrite:
                client.update_ofv(r["ofv_id"], obs_id, field_id, voucher)
            else:
                client.create_ofv(obs_id, field_id, voucher)
            applied += 1
            r["action"] = SKIP
            r["reason"] = "applied"
            r["current_value"] = voucher
            r["field_state"] = "populated"
        except requests.RequestException as exc:
            failed += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            error = f"HTTP {status}" if status else type(exc).__name__
            r["reason"] = f"apply_failed: {error}"
        if on_result:
            on_result(i, total, r, error)
        if i < total:
            time.sleep(pause)
    return applied, failed


# ---------------------------------------------------------------------------
# Validation + export
# ---------------------------------------------------------------------------
def _parse_iso_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"'{value}' is not a valid date (use YYYY-MM-DD).")
    return date.fromisoformat(value)


# Probe lengths are deliberately small. Catastrophic backtracking is
# exponential in the length of the run, so it is already unmistakable at 20
# characters (~10^6 steps) while a linear pattern still finishes in
# microseconds. Probing at MAX_MATCH_TEXT instead would mean *running* the
# pathological match we are trying to detect, which is precisely the hang this
# guard exists to prevent.
_REDOS_PROBE_LENGTHS = (12, 16, 20)
_REDOS_BUDGET_SECONDS = 0.03


def _pattern_is_slow(compiled) -> bool:
    """True if `compiled` backtracks pathologically on adversarial input.

    A user-supplied pattern is matched by the RQ worker, which also runs
    alignment and tree jobs, so a pattern like ``(a+)+b`` would not merely hurt
    its own scan. Python's `re` has no match timeout, so the pattern is probed
    here -- in the web process, before the run is saved -- against short strings
    that end in a character forcing the match to fail and unwind.
    """
    import time as _time
    for n in _REDOS_PROBE_LENGTHS:
        probe = "a" * n + "!"
        start = _time.perf_counter()
        try:
            compiled.search(probe)
        except (RecursionError, MemoryError, OverflowError):
            return True
        if (_time.perf_counter() - start) > _REDOS_BUDGET_SECONDS:
            return True
    return False


def validate_scan_params(data: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize a scan request. Returns ``(params, None)`` or ``(None, error)``."""
    data = data or {}
    fmt = str(data.get("format") or DEFAULT_VOUCHER_FORMAT)
    presets = dict(VOUCHER_FORMATS)
    if fmt not in presets:
        return None, "Unknown voucher format."
    if fmt == "Custom":
        pattern = str(data.get("regex") or "").strip()
        if not pattern:
            return None, "Enter a regular expression for the Custom format."
        if len(pattern) > MAX_CUSTOM_REGEX_LEN:
            return None, f"Custom pattern is too long (max {MAX_CUSTOM_REGEX_LEN} characters)."
    else:
        pattern = presets[fmt]
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return None, f"Voucher pattern error: {exc}"
    if fmt == "Custom":
        slow = _pattern_is_slow(compiled)
        if slow:
            return None, ("That pattern is too slow to run safely (it backtracks "
                          "excessively). Simplify it - avoid nested repeats such "
                          "as (a+)+.")

    try:
        field_id = int(data.get("field_id") or DEFAULT_FIELD_ID)
    except (TypeError, ValueError):
        return None, "Field ID must be a whole number."
    if field_id <= 0:
        return None, "Field ID must be a positive number."
    field_name = str(data.get("field_name") or "").strip()[:120]

    try:
        d1 = _parse_iso_date(data.get("date_start"))
        d2 = _parse_iso_date(data.get("date_end"))
    except ValueError as exc:
        return None, str(exc)
    if d1 and not d2:
        d2 = d1
    if d2 and not d1:
        d1 = d2
    if not d1:
        return None, "Enter an upload date or date range."
    if d1 > d2:
        return None, "The start date must be on or before the end date."

    params = {
        "format": fmt,
        "regex": pattern,
        "field_id": field_id,
        "field_name": field_name,
        "date_start": d1.isoformat(),
        "date_end": d2.isoformat(),
        "allow_overwrite": bool(data.get("allow_overwrite")),
        "use_ocr": bool(data.get("use_ocr", True)),
    }
    return params, None


# Excel, LibreOffice and Sheets treat a cell opening with any of these as a
# formula. raw_qr/raw_ocr are decoded straight from a photo, so an attacker who
# can get a label in front of the camera controls that text.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    """Stop a decoded cell value being run as a spreadsheet formula."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def rows_to_csv_text(rows: Iterable[Dict[str, Any]]) -> str:
    """Render scan rows as CSV text, with formula-injection neutralised."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: _csv_safe(v) for k, v in r.items()})
    return buf.getvalue()
