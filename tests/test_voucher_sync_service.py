"""Voucher Sync pipeline tests (pure functions; no Flask, no OpenCV).

The module is loaded with importlib so the test does not import the ``app``
package (which pulls in Flask). cv2/numpy are lazy-imported inside the
functions that need them, and those paths are patched out here.
"""
import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = REPO_ROOT / "app" / "services" / "voucher_sync_service.py"
    spec = importlib.util.spec_from_file_location("voucher_sync_service_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vs = _load()


def _obs(obs_id=1, *, photos=True, ofv_value=None, ofv_id=None, field_id=1907):
    obs = {
        "id": obs_id,
        "taxon": {"name": "Amanita muscaria", "preferred_common_name": "Fly Agaric"},
        "created_at_details": {"date": "2026-08-01"},
        "ofvs": [],
        "observation_photos": [],
    }
    if photos:
        obs["observation_photos"] = [
            {"position": 1, "photo": {"url": "https://cdn/1/square.jpg"}},
            {"position": 0, "photo": {"url": "https://cdn/0/square.jpg"}},
        ]
    if ofv_value is not None:
        obs["ofvs"] = [{"field_id": field_id, "value": ofv_value, "id": ofv_id or 55}]
    return obs


class _StubClient:
    def __init__(self, image=b"img", fail=False):
        self.image = image
        self.fail = fail
        self.created = []
        self.updated = []

    def download_image(self, url):
        if self.fail:
            raise requests.ConnectionError("boom")
        return self.image

    def create_ofv(self, observation_id, field_id, value):
        self.created.append((observation_id, field_id, value))
        return {}

    def update_ofv(self, ofv_id, observation_id, field_id, value):
        self.updated.append((ofv_id, observation_id, field_id, value))
        return {}


class VoucherFormatTests(unittest.TestCase):
    def test_presets_match_their_examples(self):
        for name, pattern in vs.VOUCHER_FORMATS:
            if pattern is None:
                continue
            rx = re.compile(pattern, re.IGNORECASE)
            for example in vs.VOUCHER_FORMAT_EXAMPLES[name].split(", "):
                self.assertIsNotNone(rx.search(example), f"{name} should match {example}")

    def test_prefix_number_rejects_numeric_prefix(self):
        rx = re.compile(vs.DEFAULT_VOUCHER_RE, re.IGNORECASE)
        self.assertIsNone(rx.search("12-3456"))
        self.assertEqual(vs.extract_voucher("label bt-001 here", rx), "BT-001")

    def test_extract_voucher_bounds_input_length(self):
        rx = re.compile(vs.DEFAULT_VOUCHER_RE, re.IGNORECASE)
        text = "x" * (vs.MAX_MATCH_TEXT + 10) + " BT-001"
        self.assertIsNone(vs.extract_voucher(text, rx))
        self.assertIsNone(vs.extract_voucher(None, rx))


class PhotoAndObservationHelperTests(unittest.TestCase):
    def test_last_photo_url_orders_by_position_and_swaps_size(self):
        self.assertEqual(vs.last_photo_url(_obs()), "https://cdn/1/original.jpg")
        self.assertEqual(vs.last_photo_url(_obs(), size="large"), "https://cdn/1/large.jpg")
        self.assertIsNone(vs.last_photo_url(_obs(photos=False)))

    def test_existing_ofv(self):
        self.assertEqual(vs.existing_ofv(_obs(ofv_value="BT-001", ofv_id=9), 1907), ("BT-001", 9))
        self.assertEqual(vs.existing_ofv(_obs(), 1907), (None, None))

    def test_taxon_label_and_upload_date(self):
        self.assertEqual(vs.taxon_label(_obs()), "Amanita muscaria (Fly Agaric)")
        self.assertEqual(vs.taxon_label({"taxon": {"name": "Russula"}}), "Russula")
        self.assertEqual(vs.taxon_label({}), "Unknown")
        self.assertEqual(vs.upload_date(_obs()), "2026-08-01")
        self.assertEqual(vs.upload_date({"created_at": "2026-07-04T10:00:00Z"}), "2026-07-04")


class BuildRowDecisionTests(unittest.TestCase):
    """The decision matrix, with image loading and decoding patched out."""

    def setUp(self):
        self.rx = re.compile(vs.DEFAULT_VOUCHER_RE, re.IGNORECASE)
        self.client = _StubClient()
        self.load = patch.object(vs, "load_image", return_value=("IMG", None))
        self.load.start()
        self.addCleanup(self.load.stop)

    def _row(self, obs, qr=("BT-001", None), ocr=(None, None, "ocr_no_match"),
             allow_overwrite=False, use_ocr=False):
        with patch.object(vs, "decode_qr", return_value=qr), \
             patch.object(vs, "ocr_fallback", return_value=ocr):
            return vs.build_row(self.client, obs, 1907, self.rx, allow_overwrite, use_ocr)

    def test_no_photos_skips(self):
        row = self._row(_obs(photos=False))
        self.assertEqual((row["action"], row["reason"]), (vs.SKIP, "no_photos"))

    def test_download_failure_flags(self):
        self.client.fail = True
        row = self._row(_obs())
        self.assertEqual(row["action"], vs.FLAG)
        self.assertTrue(row["reason"].startswith("photo_download_failed"))

    def test_field_empty_updates(self):
        row = self._row(_obs())
        self.assertEqual((row["action"], row["reason"]), (vs.UPDATE, "field_empty"))
        self.assertEqual(row["detected_voucher"], "BT-001")
        self.assertEqual(row["raw_qr"], "BT-001")
        self.assertEqual(row["field_state"], "empty")

    def test_already_correct_skips_case_insensitively(self):
        row = self._row(_obs(ofv_value="bt-001"))
        self.assertEqual((row["action"], row["reason"]), (vs.SKIP, "already_correct"))
        self.assertEqual(row["field_state"], "populated")

    def test_conflict_flags_unless_overwrite(self):
        row = self._row(_obs(ofv_value="BT-999", ofv_id=7))
        self.assertEqual((row["action"], row["reason"]), (vs.FLAG, "value_conflict"))
        row = self._row(_obs(ofv_value="BT-999", ofv_id=7), allow_overwrite=True)
        self.assertEqual((row["action"], row["reason"]), (vs.UPDATE, "overwrite_existing"))
        self.assertEqual(row["ofv_id"], 7)

    def test_unexpected_qr_data_flags(self):
        row = self._row(_obs(), qr=("https://example.org/not-a-voucher", None))
        self.assertEqual((row["action"], row["reason"]), (vs.FLAG, "unexpected_qr_data"))

    def test_qr_failure_without_ocr_flags(self):
        row = self._row(_obs(), qr=(None, "no_qr_detected"))
        self.assertEqual((row["action"], row["reason"]), (vs.FLAG, "no_qr_detected"))

    def test_ocr_fallback_paths(self):
        qr = (None, "no_qr_detected")
        row = self._row(_obs(), qr=qr, ocr=("BT-002", "raw text", None), use_ocr=True)
        self.assertEqual((row["action"], row["reason"]), (vs.UPDATE, "ocr_fallback"))
        self.assertEqual(row["raw_ocr"], "raw text")

        row = self._row(_obs(ofv_value="BT-002"), qr=qr, ocr=("BT-002", "t", None), use_ocr=True)
        self.assertEqual((row["action"], row["reason"]), (vs.SKIP, "already_correct"))

        row = self._row(_obs(ofv_value="BT-003"), qr=qr, ocr=("BT-002", "t", None), use_ocr=True)
        self.assertEqual((row["action"], row["reason"]), (vs.FLAG, "ocr_value_conflict"))

        row = self._row(_obs(ofv_value="BT-003"), qr=qr, ocr=("BT-002", "t", None),
                        use_ocr=True, allow_overwrite=True)
        self.assertEqual((row["action"], row["reason"]), (vs.UPDATE, "ocr_fallback_overwrite"))

        row = self._row(_obs(), qr=qr, ocr=(None, "junk", "ocr_no_match"), use_ocr=True)
        self.assertEqual((row["action"], row["reason"]), (vs.FLAG, "ocr_no_match"))


class OrchestrationTests(unittest.TestCase):
    def test_scan_observations_keeps_order_and_flags_errors(self):
        obs_list = [_obs(1), _obs(2), _obs(3)]

        def fake_build_row(client, obs, field_id, rx, allow_overwrite, use_ocr):
            if obs["id"] == 2:
                raise RuntimeError("decoder exploded")
            row = vs._base_row(obs)
            row["action"] = vs.UPDATE
            return row

        seen = []
        with patch.object(vs, "build_row", side_effect=fake_build_row):
            rows, cancelled = vs.scan_observations(
                _StubClient(), obs_list, field_id=1907, voucher_re=re.compile("x"),
                allow_overwrite=False, use_ocr=False, workers=2,
                on_row=lambda done, total, row: seen.append((done, total)))
        self.assertFalse(cancelled)
        self.assertEqual([r["observation_id"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[1]["action"], vs.FLAG)
        self.assertTrue(rows[1]["reason"].startswith("scan_error"))
        self.assertEqual(sorted(seen), [(1, 3), (2, 3), (3, 3)])

    def test_scan_observations_cancel_returns_partial(self):
        obs_list = [_obs(i) for i in range(1, 6)]
        calls = {"n": 0}

        def should_cancel():
            calls["n"] += 1
            return calls["n"] >= 2

        with patch.object(vs, "build_row", side_effect=lambda c, o, *a: vs._base_row(o)):
            rows, cancelled = vs.scan_observations(
                _StubClient(), obs_list, field_id=1907, voucher_re=re.compile("x"),
                allow_overwrite=False, use_ocr=False, workers=1,
                should_cancel=should_cancel)
        self.assertTrue(cancelled)
        self.assertLess(len(rows), 5)

    def test_apply_rows_create_vs_update_and_failure(self):
        client = _StubClient()
        rows = [
            {"observation_id": 1, "detected_voucher": "BT-001", "ofv_id": None,
             "current_value": None, "action": vs.UPDATE, "reason": "field_empty"},
            {"observation_id": 2, "detected_voucher": "BT-002", "ofv_id": 77,
             "current_value": "OLD", "action": vs.UPDATE, "reason": "overwrite_existing"},
            {"observation_id": 3, "detected_voucher": "BT-003", "ofv_id": None,
             "current_value": None, "action": vs.UPDATE, "reason": "field_empty"},
        ]
        original_create = client.create_ofv

        def flaky_create(observation_id, field_id, value):
            if observation_id == 3:
                raise requests.ConnectionError("down")
            return original_create(observation_id, field_id, value)

        client.create_ofv = flaky_create
        with patch.object(vs.time, "sleep"):
            applied, failed = vs.apply_rows(client, rows, field_id=1907,
                                            allow_overwrite=True, pause=0)
        self.assertEqual((applied, failed), (2, 1))
        self.assertEqual(client.created, [(1, 1907, "BT-001")])
        self.assertEqual(client.updated, [(77, 2, 1907, "BT-002")])
        self.assertEqual((rows[0]["action"], rows[0]["reason"], rows[0]["current_value"]),
                         (vs.SKIP, "applied", "BT-001"))
        self.assertTrue(rows[2]["reason"].startswith("apply_failed"))
        self.assertEqual(rows[2]["action"], vs.UPDATE)

    def test_summarize_rows(self):
        rows = [{"action": "update", "reason": "field_empty"},
                {"action": "update", "reason": "ocr_fallback"},
                {"action": "skip", "reason": "already_correct"},
                {"action": "flag", "reason": "no_qr_detected"}]
        self.assertEqual(vs.summarize_rows(rows),
                         {"update": 2, "skip": 1, "flag": 1, "ocr": 1, "total": 4})


class ValidationAndExportTests(unittest.TestCase):
    def test_valid_single_date_fills_range(self):
        params, err = vs.validate_scan_params({"date_start": "2026-08-01", "field_id": "1907"})
        self.assertIsNone(err)
        self.assertEqual((params["date_start"], params["date_end"]), ("2026-08-01", "2026-08-01"))
        self.assertEqual(params["regex"], vs.DEFAULT_VOUCHER_RE)
        self.assertTrue(params["use_ocr"])
        self.assertFalse(params["allow_overwrite"])

    def test_rejections(self):
        cases = [
            ({"date_start": "2026-08-01", "format": "Nope"}, "Unknown voucher format"),
            ({"date_start": "2026-08-01", "format": "Custom", "regex": ""}, "Enter a regular expression"),
            ({"date_start": "2026-08-01", "format": "Custom", "regex": "(" }, "pattern error"),
            ({"date_start": "2026-08-01", "format": "Custom", "regex": "a" * 201}, "too long"),
            ({"date_start": "2026-08-01", "field_id": "abc"}, "whole number"),
            ({"date_start": "2026-08-01", "field_id": "-4"}, "positive"),
            ({"date_start": "01/08/2026"}, "not a valid date"),
            ({}, "Enter an upload date"),
            ({"date_start": "2026-08-02", "date_end": "2026-08-01"}, "on or before"),
        ]
        for data, fragment in cases:
            params, err = vs.validate_scan_params(data)
            self.assertIsNone(params, data)
            self.assertIn(fragment, err, data)

    def test_custom_regex_accepted(self):
        params, err = vs.validate_scan_params(
            {"date_start": "2026-08-01", "format": "Custom", "regex": r"\bBT-\d{3}\b"})
        self.assertIsNone(err)
        self.assertEqual(params["regex"], r"\bBT-\d{3}\b")

    def test_rows_to_csv_text_columns(self):
        text = vs.rows_to_csv_text([{"observation_id": 1, "url": "u", "taxon": "t",
                                     "upload_date": "d", "detected_voucher": "BT-001",
                                     "field_state": "empty", "current_value": None,
                                     "action": "update", "reason": "field_empty",
                                     "raw_qr": "BT-001", "raw_ocr": None, "ofv_id": 3}])
        lines = text.splitlines()
        self.assertEqual(lines[0], ",".join(vs.CSV_COLUMNS))
        self.assertEqual(lines[1], "1,u,t,d,BT-001,empty,,update,field_empty,BT-001,")
        self.assertNotIn("ofv_id", text)


if __name__ == "__main__":
    unittest.main()
