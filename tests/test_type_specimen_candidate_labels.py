"""Type-candidate scans recognize generic labels without accepting negations."""

import importlib.util
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


gap = _load("dikarya_type_specimen_gap_report", "dikarya_type_specimen_gap_report.py")
refresh = _load("dikarya_refresh_type_specimens_candidates", "dikarya_refresh_type_specimens.py")


def _is_label_candidate(module, label):
    return bool(module.TYPE_WORD_RE.search(label) and not module.NEGATED_TYPE_RE.search(label))


def test_bare_parenthesized_type_is_a_candidate():
    label = "OL850876 Inosperma sp. FYG2020525(Type) China"
    assert _is_label_candidate(gap, label)
    assert _is_label_candidate(refresh, label)


def test_negated_type_is_not_a_candidate():
    label = "AB123456 Example fungus (non-type specimen)"
    assert not _is_label_candidate(gap, label)
    assert not _is_label_candidate(refresh, label)


def test_classification_quotes_the_label_that_made_the_claim():
    claim = "OL850876 Inosperma sp. FYG2020525(Type) China"
    verdict = gap.classify(
        "OL850876",
        {"definition": "Inosperma sp.", "type_material": ""},
        {"OL850876": claim},
        {},
        {},
        {"OL850876": "OL850876 Inosperma sp."},
    )
    assert verdict == (
        "type",
        "low",
        "Tip label mentions a type word, but the NCBI record has no type annotation",
        claim,
    )


def test_exact_type_voucher_match_is_candidate_evidence():
    index = gap.type_voucher_index(
        {"KJ416269": {
            "status": "holotype",
            "organism": "Gymnopus barbipes",
            "voucher": "TENN 67855",
        }},
        {},
    )
    assert index[("GYMNOPUSBARBIPES", "TENN67855")][0]["accession"] == "KJ416269"
    assert gap._normalized_voucher("TENN:67855 (holotype)") == "TENN67855"
    assert gap._normalized_voucher("s.n.") == ""

    verdict = gap.classify(
        "KJ416265",
        {"definition": "Gymnopus barbipes", "type_material": ""},
        {},
        {},
        {"KJ416265": (
            "Exact organism and voucher match to marked type accession(s)",
            "Analyzed organism Gymnopus barbipes; target voucher TENN 67855; "
            "matches KJ416269 (holotype, MycoMap, voucher TENN 67855)",
        )},
        {"KJ416265": "KJ416265 Gymnopus barbipes TENN 67855"},
    )
    assert verdict[0:3] == (
        "type", "low", "Exact organism and voucher match to marked type accession(s)")
