"""BLAST result parsing must honour the requested hit limit.

`_submit_blast_request` sends the caller's `max_sequences` to NCBI as
HITLIST_SIZE, but the parse used to slice its output to a hard-coded 50. A
caller asking for 200 reference sequences silently got 50 -- a change in taxon
sampling, not merely a shorter list.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import blast_service


def _hit(accession, sciname):
    return {"description": [{"accession": accession, "sciname": sciname}]}


def _json_payload(count):
    return json.dumps({
        "BlastOutput2": [{
            "report": {"results": {"search": {
                "hits": [_hit(f"MK{i:06d}", f"Species {i}") for i in range(count)],
            }}},
        }],
    })


def _response(text, content_type="application/json"):
    return SimpleNamespace(
        text=text,
        content=text.encode("utf-8"),
        headers={"content-type": content_type},
        raise_for_status=lambda: None,
    )


class MaxSequencesPropagationTests(unittest.TestCase):
    def _fetch(self, payload, **kwargs):
        with patch.object(blast_service, "_ncbi_request",
                          return_value=_response(payload)):
            return blast_service._fetch_blast_results("RID123", **kwargs)

    def test_default_is_still_fifty(self):
        result = self._fetch(_json_payload(120))
        self.assertEqual(len(result["accessions"]), 50)
        self.assertEqual(len(result["hit_details"]), 50)

    def test_limit_above_fifty_is_honoured(self):
        result = self._fetch(_json_payload(300), max_sequences=200)
        self.assertEqual(len(result["accessions"]), 200)
        self.assertEqual(len(result["hit_details"]), 200)

    def test_limit_below_fifty_is_honoured(self):
        result = self._fetch(_json_payload(120), max_sequences=10)
        self.assertEqual(len(result["accessions"]), 10)
        self.assertEqual(len(result["hit_details"]), 10)

    def test_fewer_hits_than_the_limit_are_all_returned(self):
        result = self._fetch(_json_payload(7), max_sequences=200)
        self.assertEqual(len(result["accessions"]), 7)

    def test_accessions_and_hit_details_stay_aligned(self):
        result = self._fetch(_json_payload(300), max_sequences=120)
        self.assertEqual(
            result["accessions"],
            [h["accession"] for h in result["hit_details"]],
        )

    def test_text_fallback_parse_uses_the_same_limit(self):
        text = " ".join(f"MK{i:06d}" for i in range(300))
        result = self._fetch(text, max_sequences=200)
        self.assertEqual(len(result["accessions"]), 200)

    def test_text_fallback_parse_defaults_to_fifty(self):
        text = " ".join(f"MK{i:06d}" for i in range(300))
        result = self._fetch(text)
        self.assertEqual(len(result["accessions"]), 50)

    def test_nonsense_limits_fall_back_to_the_default(self):
        for bad in (None, 0, -5, "not a number"):
            with self.subTest(limit=bad):
                result = self._fetch(_json_payload(120), max_sequences=bad)
                self.assertEqual(len(result["accessions"]), 50)


class SubmissionAndParseAgreeTests(unittest.TestCase):
    def test_blast_from_sequence_passes_its_limit_to_the_parser(self):
        seen = {}

        def _fake_fetch(rid, max_sequences=blast_service.DEFAULT_MAX_SEQUENCES):
            seen["rid"] = rid
            seen["max_sequences"] = max_sequences
            return {"accessions": [], "hit_details": []}

        with (
            patch.object(blast_service, "_check_cache", return_value=None),
            patch.object(blast_service, "_submit_blast_request",
                         return_value=("RID1", 10)),
            patch.object(blast_service, "_poll_blast"),
            patch.object(blast_service, "_fetch_blast_results", _fake_fetch),
            patch.object(blast_service, "fetch_fasta_for_accessions",
                         return_value=""),
            patch.object(blast_service, "_save_cache", return_value={}),
        ):
            blast_service.blast_from_sequence(">a\nACGT", object(),
                                              max_sequences=175)
        self.assertEqual(seen["max_sequences"], 175)

    def test_blast_from_accessions_passes_its_limit_to_the_parser(self):
        seen = {}

        def _fake_fetch(rid, max_sequences=blast_service.DEFAULT_MAX_SEQUENCES):
            seen["max_sequences"] = max_sequences
            return {"accessions": [], "hit_details": []}

        with (
            patch.object(blast_service, "_check_cache", return_value=None),
            patch.object(blast_service, "_submit_blast_request",
                         return_value=("RID1", 10)),
            patch.object(blast_service, "_poll_blast"),
            patch.object(blast_service, "_fetch_blast_results", _fake_fetch),
            patch.object(blast_service, "fetch_fasta_for_accessions",
                         return_value=""),
            patch.object(blast_service, "_save_cache", return_value={}),
        ):
            blast_service.blast_from_accessions(["MK564475"], object(),
                                                max_sequences=175)
        self.assertEqual(seen["max_sequences"], 175)


class NoFixedTempFileTests(unittest.TestCase):
    """A malformed NCBI response must not be dumped to a shared /tmp path.

    One predictable filename meant concurrent jobs overwrote each other's dump
    and put upstream response data outside the normal logging controls.
    """

    def test_source_has_no_fixed_tmp_debug_path(self):
        source = open(blast_service.__file__.replace(".pyc", ".py")).read()
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("/tmp/blast_debug_response.json", code)
        self.assertNotIn("write_text(json.dumps(data", code)

    def test_missing_blastoutput2_returns_empty_and_writes_nothing(self):
        import pathlib
        written = []
        original = pathlib.Path.write_text

        def _spy(self, *args, **kwargs):
            written.append(str(self))
            return original(self, *args, **kwargs)

        payload = json.dumps({"unexpected": {"shape": True}})
        with (
            patch.object(blast_service, "_ncbi_request",
                         return_value=_response(payload)),
            patch.object(pathlib.Path, "write_text", _spy),
        ):
            result = blast_service._fetch_blast_results("RID123")
        self.assertEqual(result, {"accessions": [], "hit_details": []})
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
