from app.services.blast_service import _build_header
record1 = {
    "accession": "PP959535",
    "version": "PP959535",
    "organism": "Strobilomyces sp.",
    "source_features": {}
}
print(_build_header(record1))

record2 = {
    "accession": "PP959535",
    "version": "PP959535",
    "organism": "",
    "source_features": {}
}
print(_build_header(record2))
