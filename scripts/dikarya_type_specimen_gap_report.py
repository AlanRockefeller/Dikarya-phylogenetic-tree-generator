#!/usr/bin/env python3
"""
Report accessions in Dikarya jobs that look like type specimens but are not on
MycoMap's type-specimen list, with the evidence for each, as CSV.

Evidence, strongest first (the `confidence` column):

  high    GenBank's /type_material source qualifier, quoted in evidence_text.
  medium  NCBI's DEFINITION line says "from TYPE material" (RefSeq's marker),
          or the NCBI description stored with a MycoMap BLAST hit says so.
  low     Only the tip label names a type; the NCBI record carries no type
          annotation. Worth checking against the literature, not proven.

Every suspect is re-fetched from NCBI so the evidence is current. RefSeq
records (NR_...) are NCBI copies of an ordinary GenBank record; the source
accession is read from the record's COMMENT ("identical to"/"derived from"),
and `source_accession_in_mycomap_list` says whether MycoMap already lists the
specimen under that accession. On 2026-09-24 that was true for 3,185 of the
3,701 rows, so filter on it before treating a row as missing from the list.

Only accessions that occur in job inputs are considered -- this is not an
audit of all of GenBank. Reads the MycoMap snapshot and GenBank cache that
scripts/dikarya_refresh_type_specimens.py maintains; run that first if they
are stale. Takes a few minutes (one NCBI request per 100 suspects).

    .venv/bin/python scripts/dikarya_type_specimen_gap_report.py
    .venv/bin/python scripts/dikarya_type_specimen_gap_report.py --output /path/report.csv
"""
import argparse
import collections
import csv
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Config  # noqa: E402
from app.services import type_specimen_service as tss  # noqa: E402
from app.services.blast_service import _fetch_genbank_xml_batch  # noqa: E402
from app.services.sequence_dedup_service import record_accession  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "type_specimens_not_in_mycomap.csv"
NCBI_BATCH = 100
TYPE_WORD_RE = re.compile(
    r"\b(holotype|isotype|epitype|lectotype|neotype|paratype|syntype|ex-type|"
    r"TYPE material|type strain)\b",
    re.IGNORECASE,
)
# "The reference sequence is identical to JQ003648." / "...was derived from ..."
REFSEQ_SOURCE_RE = re.compile(r"(?:identical to|derived from)\s+([A-Z]{1,6}\d{5,11}(?:\.\d+)?)", re.IGNORECASE)
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
FIELDS = [
    "accession", "organism", "suspected_status", "confidence", "evidence_source",
    "evidence_text", "voucher", "refseq_source_accession",
    "source_accession_in_mycomap_list", "dikarya_jobs_containing",
    "example_tip_label", "ncbi_url",
]


def scan_jobs(mycomap):
    """Which unlisted accessions occur in which jobs, and what the jobs say about them."""
    jobs_for = collections.defaultdict(set)
    example = {}
    label_claims = set()
    description_claims = {}
    for input_info in Config.JOB_DIR.glob("*/input_info.json"):
        job_dir = input_info.parent
        by_name = {}
        for item in tss.job_sequence_metadata(job_dir):
            if isinstance(item, dict):
                for key in ("fasta_header", "name", "display_label"):
                    if item.get(key):
                        by_name.setdefault(str(item[key]).strip(), item)
        try:
            headers = list(tss.job_headers(job_dir))
        except OSError:
            continue
        for header in headers:
            metadata = by_name.get(header) or {}
            acc = tss.accession_root(record_accession(header, metadata))
            if not acc or acc in mycomap:
                continue
            jobs_for[acc].add(job_dir.name)
            example.setdefault(acc, header)
            if TYPE_WORD_RE.search(header):
                label_claims.add(acc)
            description = str(metadata.get("raw_ncbi_description") or "")
            if tss.FROM_TYPE_MATERIAL_RE.search(description):
                description_claims[acc] = description
    return jobs_for, example, label_claims, description_claims


def fetch_ncbi(accessions):
    """Current NCBI evidence for each accession, keyed by accession root."""
    records = {}
    for start in range(0, len(accessions), NCBI_BATCH):
        for document in _fetch_genbank_xml_batch(accessions[start:start + NCBI_BATCH]):
            try:
                root = ET.fromstring(re.sub(r'\sxmlns="[^"]+"', "", document, count=1))
            except ET.ParseError:
                continue
            for seq in root.findall(".//GBSeq"):
                acc = tss.accession_root(seq.findtext("GBSeq_primary-accession", ""))
                quals = {}
                for feature in seq.findall(".//GBFeature"):
                    if feature.findtext("GBFeature_key") == "source":
                        for qual in feature.findall(".//GBQualifier"):
                            quals[qual.findtext("GBQualifier_name", "")] = " ".join(
                                (qual.findtext("GBQualifier_value", "") or "").split())
                source = REFSEQ_SOURCE_RE.search(seq.findtext("GBSeq_comment", "") or "")
                records[acc] = {
                    "organism": seq.findtext("GBSeq_organism", ""),
                    "definition": " ".join((seq.findtext("GBSeq_definition", "") or "").split()),
                    "type_material": quals.get("type_material", ""),
                    "voucher": (quals.get("specimen_voucher") or quals.get("culture_collection")
                                or quals.get("strain") or quals.get("isolate") or ""),
                    "source_acc": tss.accession_root(source.group(1)) if source else "",
                }
        print(f"NCBI: {min(start + NCBI_BATCH, len(accessions))}/{len(accessions)} fetched",
              file=sys.stderr, flush=True)
    return records


def classify(acc, record, label_claims, description_claims, example):
    """(status, confidence, evidence_source, evidence_text), or None if not a suspect."""
    if record and tss.classify_type_material(record["type_material"]):
        return (tss.classify_type_material(record["type_material"]), "high",
                "GenBank /type_material qualifier", record["type_material"])
    if record and tss.FROM_TYPE_MATERIAL_RE.search(record["definition"]):
        return ("type", "medium", "NCBI definition line says 'from TYPE material'",
                record["definition"])
    if acc in description_claims:
        return ("type", "medium",
                "NCBI description stored with the MycoMap BLAST hit says 'from TYPE material' "
                "(record could not be re-fetched)", description_claims[acc])
    if acc in label_claims:
        match = TYPE_WORD_RE.search(example.get(acc, ""))
        word = match.group(1).lower() if match else "type"
        status = "type" if word in ("type material", "type strain") else word
        source = ("Tip label mentions a type word, but the NCBI record has no type annotation"
                  if record else "Tip label mentions a type word; NCBI record could not be fetched")
        return status, "low", source, example.get(acc, "")
    return None  # e.g. the cache said type but NCBI no longer does


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"CSV path to write (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    mycomap = tss.mycomap_index()
    if not mycomap:
        parser.error("no MycoMap snapshot; run scripts/dikarya_refresh_type_specimens.py first")

    jobs_for, example, label_claims, description_claims = scan_jobs(mycomap)
    cached = {acc for acc, entry in tss.genbank_index().items()
              if entry.get("status") and acc not in mycomap}
    suspects = sorted(cached | set(description_claims) | label_claims)
    print(f"{len(suspects)} suspects: {len(cached)} from the GenBank cache, "
          f"{len(description_claims)} from stored NCBI descriptions, {len(label_claims)} from labels",
          file=sys.stderr)

    ncbi = fetch_ncbi(suspects)
    rows = []
    for acc in suspects:
        record = ncbi.get(acc)
        verdict = classify(acc, record, label_claims, description_claims, example)
        if verdict is None:
            continue
        status, confidence, evidence_source, evidence_text = verdict
        source_acc = (record or {}).get("source_acc", "")
        rows.append({
            "accession": acc,
            "organism": (record or {}).get("organism", ""),
            "suspected_status": status,
            "confidence": confidence,
            "evidence_source": evidence_source,
            "evidence_text": evidence_text,
            "voucher": (record or {}).get("voucher", ""),
            "refseq_source_accession": source_acc,
            "source_accession_in_mycomap_list": ("yes" if source_acc in mycomap else "no") if source_acc else "",
            "dikarya_jobs_containing": len(jobs_for.get(acc, ())),
            "example_tip_label": example.get(acc, ""),
            "ncbi_url": f"https://www.ncbi.nlm.nih.gov/nuccore/{acc}",
        })

    rows.sort(key=lambda r: (CONFIDENCE_ORDER[r["confidence"]], -r["dikarya_jobs_containing"], r["accession"]))
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    listed_elsewhere = sum(1 for r in rows if r["source_accession_in_mycomap_list"] == "yes")
    by_confidence = collections.Counter(r["confidence"] for r in rows)
    print(f"Wrote {len(rows)} rows to {args.output}: "
          + ", ".join(f"{by_confidence[c]} {c}" for c in CONFIDENCE_ORDER)
          + f"; {listed_elsewhere} are RefSeq copies of a record MycoMap already lists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
