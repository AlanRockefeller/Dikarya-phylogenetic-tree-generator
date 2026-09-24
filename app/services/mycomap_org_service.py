"""Read public MycoMap.org BLAST results without following user supplied URLs."""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from app.services.api_diagnostics import diagnostic_urlopen
from app.services.fasta_utils import is_genbank_accession, parse_fasta_records

logger = logging.getLogger(__name__)
ORG_API = "https://mycomap.org/api/mycoblast"
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
NCBI_BATCH_SIZE = 50
_JOB_LINK = re.compile(r"(?:/|%2F)(\d+)(?:/?$|[?&#])", re.I)
_LOCAL_XML_ID = re.compile(r"/local_(\d+)_xml\.", re.I)


class OrgResultError(Exception):
    def __init__(self, message, status=502, retryable=True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _read(path, *, limit=MAX_JSON_BYTES, method="GET", body=None,
          authenticated=False, deadline=None):
    """Only fixed API paths are accepted; no URL from a result is fetched."""
    if not re.fullmatch(r"[a-z0-9/?=&,_-]+", path, re.I) or ".." in path:
        raise OrgResultError("Invalid MycoMap API path.", 400)
    headers = {
        "Accept": "application/json" if limit == MAX_JSON_BYTES else "application/xml",
        "User-Agent": "Dikarya-TreeBuilder/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if authenticated:
        key = (os.environ.get("MYCOMAP_ORG_API_KEY") or "").strip()
        if not key:
            raise OrgResultError("MycoMap.org rerun key is not configured.", 502,
                                 retryable=False)
        headers["Authorization"] = f"Bearer {key}"
    base = "https://mycomap.org/api" if path.startswith("mycomap/sequences/batch?") else ORG_API
    request = urllib.request.Request(
        f"{base}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    timeout = 15 if deadline is None else min(15, deadline - time.monotonic())
    if timeout <= 0:
        raise OrgResultError("MycoMap.org request timed out.")
    try:
        with diagnostic_urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise OrgResultError(
            "MycoMap.org has no matching BLAST result." if exc.code == 404
            else "MycoMap.org could not complete the BLAST request.",
            404 if exc.code == 404 else 502,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OrgResultError("MycoMap.org is temporarily unavailable.") from exc
    if len(data) > limit:
        raise OrgResultError("MycoMap.org returned an oversized result.")
    return data


def _json(path, *, deadline=None):
    try:
        value = json.loads(_read(path, deadline=deadline))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrgResultError("MycoMap.org returned invalid status data.") from exc
    if not isinstance(value, dict):
        raise OrgResultError("MycoMap.org returned invalid status data.")
    return value


def resolve_result_id(reference):
    """Resolve an org observation/sequence URL to a BLAST job ID."""
    if reference["kind"] == "mycoblast":
        return reference["result_id"]
    sequence_id = reference["sequence_id"]
    metadata = _json(f"sequence-metadata/{sequence_id}")
    observation_id = reference["observation_id"]
    linked_ids = {
        str(metadata.get("inatObservationId") or ""),
        str(metadata.get("moObservationId") or ""),
    }
    if observation_id not in linked_ids:
        raise OrgResultError("That MycoMap sequence does not belong to the linked observation.", 404)
    link = str((metadata.get("blastMetadata") or {}).get("MM_Blast_Link") or "")
    match = _JOB_LINK.search(link)
    if not match:
        match = _LOCAL_XML_ID.search(str(metadata.get("localBlastXml") or ""))
    if not match:
        raise OrgResultError("MycoMap has not published BLAST results for this sequence yet.", 409)
    return match.group(1)


def status(result_id):
    return _json(result_id)


def rerun_pending(details):
    """Check requested rerun sources against the saved pre-rerun XML dates."""
    sources = list(details.get("org_wait_sources") or [])
    if not sources:
        return False
    record = status(str(details["result_id"]))
    before = details.get("org_before_dates") or {}
    for source in sources:
        current = record.get(source) or {}
        state = current.get("status")
        if state in {"queued", "pending", "running", "processing"}:
            return True
        if state in {"failed", "error"} or not current.get("has_results"):
            if source == "ncbi":
                raise OrgResultError("MycoMap.org NCBI rerun finished without results.",
                                     retryable=False)
            details.setdefault("warnings", []).append(
                "MycoMap.org local rerun did not finish; using any saved local results."
            )
            details["local_status"] = "failed"
            continue
        old_date = before.get(source)
        if old_date and current.get("xml_date") == old_date:
            return True
        details[f"{source}_status"] = "available" if source == "ncbi" else "completed"
    details["org_wait_sources"] = []
    return False


def rerun(result_id, result_type, limit):
    if not str(result_id).isdigit() or result_type not in {"local", "ncbi"}:
        raise OrgResultError("Invalid MycoMap rerun request.", 400)
    try:
        result = json.loads(_read(
            "rerun", method="POST",
            body={"id": int(result_id), "type": result_type, "limit": int(limit)},
            authenticated=True,
        ))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrgResultError("MycoMap.org returned an invalid rerun response.") from exc
    if not isinstance(result, dict):
        raise OrgResultError("MycoMap.org returned an invalid rerun response.")
    return result


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _percent(numerator, denominator):
    return round(100 * numerator / denominator, 2) if denominator else None


def _location(metadata):
    value = metadata.get("location") or ""
    if isinstance(value, str) and value.startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if isinstance(value, dict):
        return " ".join(str(value.get(key) or "").strip() for key in ("city", "region", "country")).strip()
    return str(value).strip()


def _parse_xml(data, source):
    # BLAST XML declares an external DTD. ElementTree never loads it; reject
    # entities as an additional guard before parsing upstream content.
    if b"<!ENTITY" in data.upper():
        raise OrgResultError("MycoMap.org returned unsafe BLAST XML.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OrgResultError("MycoMap.org returned invalid BLAST XML.") from exc
    query_length = _number(root.findtext("BlastOutput_query-len"))
    hits = []
    for hit in root.findall(".//Hit"):
        hsps = hit.findall("./Hit_hsps/Hsp")
        if not hsps:
            continue
        hsp = max(hsps, key=lambda item: _number(item.findtext("Hsp_score")))
        subject_length = _number(hit.findtext("Hit_len"))
        aligned = _number(hsp.findtext("Hsp_align-len"))
        hit_from = _number(hsp.findtext("Hsp_hit-from"))
        hit_to = _number(hsp.findtext("Hsp_hit-to"))
        sequence = re.sub(r"[^ACGTNRYSWKMBDHV]", "", (hsp.findtext("Hsp_hseq") or "").upper())
        if not sequence:
            continue
        hits.append({
            "id": (hit.findtext("Hit_id") or "").strip(),
            "accession": (hit.findtext("Hit_accession") or "").strip(),
            "description": (hit.findtext("Hit_def") or "").strip(),
            "subject_length": subject_length,
            "sequence": sequence,
            "identity": _percent(_number(hsp.findtext("Hsp_identity")), aligned),
            "query_cover": _percent(abs(_number(hsp.findtext("Hsp_query-to")) - _number(hsp.findtext("Hsp_query-from"))) + 1, query_length),
            "subject_cover": _percent(abs(hit_to - hit_from) + 1, subject_length),
            "source": source,
        })
    return hits


def _local_metadata(ids, *, deadline=None):
    """Fetch full local sequences and names in batches; XML remains fallback."""
    output = {}
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        query = ",".join(batch)
        try:
            payload = _json(f"mycomap/sequences/batch?ids={query}", deadline=deadline)
        except OrgResultError:
            logger.warning("MycoMap.org local metadata was unavailable", exc_info=True)
            continue
        for item in payload.get("sequences") or []:
            if isinstance(item, dict) and str(item.get("id") or "") in batch:
                output[str(item["id"])] = item
    return output


def _ncbi_sequences(accessions, *, deadline=None):
    """Fetch complete NCBI records in batches, keyed by the requested accession."""
    from app.services.blast_service import NCBI_EFETCH_URL, _ncbi_request

    requested = list(dict.fromkeys(
        accession.upper() for accession in accessions if is_genbank_accession(accession)
    ))
    sequences = {}
    for offset in range(0, len(requested), NCBI_BATCH_SIZE):
        batch = requested[offset:offset + NCBI_BATCH_SIZE]
        remaining = deadline - time.monotonic() if deadline is not None else 30
        if remaining <= 0:
            break
        try:
            response = _ncbi_request(
                "POST", NCBI_EFETCH_URL, max_retries=1,
                data={"db": "nuccore", "id": ",".join(batch),
                      "rettype": "fasta", "retmode": "text"},
                timeout=(min(5, remaining), min(20, remaining)), stream=True,
            )
            try:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_XML_BYTES:
                        raise ValueError("NCBI FASTA batch is too large")
                    chunks.append(chunk)
                fasta = b"".join(chunks).decode("utf-8")
            finally:
                response.close()
        except Exception:
            logger.warning("NCBI full sequence fetch failed for %s accessions",
                           len(batch), exc_info=True)
            continue

        returned = {}
        for header, raw_sequence in parse_fasta_records(fasta):
            token = header.split(None, 1)[0].upper() if header else ""
            for part in token.split("|"):
                if is_genbank_accession(part):
                    sequence = re.sub(r"[^ACGTNRYSWKMBDHV]", "", raw_sequence.upper())
                    if sequence:
                        returned[part] = sequence
        for accession in batch:
            # A versioned BLAST hit must match that version. Bare accessions may
            # match the version NCBI puts in the FASTA header.
            sequence = returned.get(accession)
            if sequence is None and "." not in accession:
                sequence = next((value for key, value in returned.items()
                                 if key.split(".")[0] == accession), None)
            if sequence:
                sequences[accession] = sequence
    return sequences


def fetch_results(result_id, *, include_ncbi=True, include_local=True,
                  time_budget=None):
    """Return normalized hits and source state for the shared import pipeline."""
    deadline = time.monotonic() + time_budget if time_budget is not None else None
    record = _json(result_id, deadline=deadline)
    result = {"sequences": [], "ncbi_count": 0, "local_count": 0,
              "errors": [], "failed_sources": [], "pending_sources": [],
              "ncbi_queue_position": None, "metrics_by_key": {}}
    for source in ("ncbi", "local"):
        if not (include_ncbi if source == "ncbi" else include_local):
            continue
        source_status = record.get(source) or {}
        if source == "ncbi":
            result["ncbi_queue_position"] = source_status.get("queue_position")
        if source_status.get("status") in {"queued", "pending", "running", "processing"}:
            result["pending_sources"].append(source)
            continue
        if not source_status.get("has_results"):
            if source_status.get("status") in {"failed", "error"}:
                result["failed_sources"].append(source)
                result["errors"].append(f"{source} BLAST failed on MycoMap.org")
            continue
        try:
            data = _read(f"{result_id}/{source}", limit=MAX_XML_BYTES,
                         deadline=deadline)
            hits = _parse_xml(data, source)
        except OrgResultError as exc:
            result["failed_sources"].append(source)
            result["errors"].append(str(exc))
            continue
        if source == "local":
            metadata = _local_metadata([h["id"] for h in hits if h["id"].isdigit()],
                                       deadline=deadline)
        else:
            metadata = {}
            ncbi_sequences = _ncbi_sequences([h["accession"] for h in hits],
                                             deadline=deadline)
            # Hit_len identifies the sequence whose BLAST metrics we received.
            # A changed or incomplete NCBI record must not inherit those metrics.
            for hit in hits:
                sequence = ncbi_sequences.get(hit["accession"].upper())
                if sequence and (not hit["subject_length"] or
                                 len(sequence) == hit["subject_length"]):
                    hit["full_sequence"] = sequence
            missing = sum("full_sequence" not in hit for hit in hits)
            if missing:
                result["errors"].append(
                    f"Could not fetch full NCBI sequences for {missing} BLAST hits"
                )
                if missing == len(hits):
                    result["failed_sources"].append(source)
        for hit in hits:
            item = metadata.get(hit["id"], {})
            accession = hit["accession"] if source == "ncbi" else ""
            if source == "ncbi" and "full_sequence" not in hit:
                continue
            taxon = str(item.get("scientificName") or "").strip()
            observation_token = (
                f"iNat{item['inatObservationId']}" if item.get("inatObservationId")
                else f"MO #{item['moObservationId']}" if item.get("moObservationId")
                else ""
            )
            name = (f"{accession} {hit['description']}" if accession else
                    f"{hit['id']} {observation_token} {taxon or hit['description']}").strip()
            if source == "ncbi":
                sequence = hit["full_sequence"]
            else:
                sequence = (re.sub(r"[^ACGTNRYSWKMBDHV]", "",
                                   str(item.get("sequence") or "").upper())
                            or hit["sequence"])
            result["sequences"].append({
                "name": name, "sequence": sequence, "source": "mycomap",
                "hit_source": source, "accession": accession, "taxon": taxon,
                "location": _location(item), "identity": hit["identity"],
                "query_cover": hit["query_cover"], "subject_cover": hit["subject_cover"],
                "is_contaminant": any("contaminant" in str(flag).lower()
                                      for flag in (item.get("flags") or [])),
                "blast_metrics_available": True,
            })
        result[f"{source}_count"] = sum(
            sequence["hit_source"] == source for sequence in result["sequences"]
        )
    return result
