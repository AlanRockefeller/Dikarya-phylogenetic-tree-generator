"""Suite-wide fixtures.

Alan 9/7/26 - The only thing here is archive isolation, and it is here rather
than in a single test module because the leak was never in the diagnostics
tests: any test that exercises an upstream API path -- a synthetic 429, a
timeout, an `invalid_client` fixture, the `old-tree` fixture in
test_inaturalist_tree_service.py -- goes through record_api_failure(), which
writes to app.services.api_diagnostics.ARCHIVE_DIR. That default is the
production directory, so a test run deposited 61 fixture archives into
var/logs/api-responses/<today>/ next to the genuine, request-correlated ones an
operator is meant to read. Only tests/test_api_diagnostics.py had ever
redirected it, and only for its own cases.
"""
import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_ncbi_pacing():
    from app.services import ncbi_pacing, mycomap_org_service
    import uuid
    run_id = uuid.uuid4().hex
    original = ncbi_pacing.KEY_PREFIX
    auth_prefix = mycomap_org_service.SEARCH_AUTH_KEY_PREFIX
    curation_pause_key = mycomap_org_service.CURATION_PAUSE_KEY
    ncbi_pacing.KEY_PREFIX = f"dikarya:test:ncbi:{run_id}:"
    mycomap_org_service.SEARCH_AUTH_KEY_PREFIX = f"dikarya:test:mycomap-org:submit-auth:{run_id}:"
    # A mocked 429 must never pause the live site's curation lookups.
    mycomap_org_service.CURATION_PAUSE_KEY = f"dikarya:test:mycomap-org:curation-paused:{run_id}"
    curation_key_refused = mycomap_org_service.CURATION_KEY_REFUSED_KEY
    mycomap_org_service.CURATION_KEY_REFUSED_KEY = f"dikarya:test:mycomap-org:curation-key-refused:{run_id}"
    curation_exempt_state = mycomap_org_service.CURATION_EXEMPT_STATE_KEY
    mycomap_org_service.CURATION_EXEMPT_STATE_KEY = f"dikarya:test:mycomap-org:curation-exempt-state:{run_id}"
    # Nor pause the live tree viewer's MycoMap record refreshes.
    refresh_pause_prefix = mycomap_org_service.REFRESH_PAUSE_KEY_PREFIX
    mycomap_org_service.REFRESH_PAUSE_KEY_PREFIX = f"dikarya:test:mycomap-org:refresh-paused:{run_id}:"
    try:
        yield
    finally:
        mycomap_org_service.REFRESH_PAUSE_KEY_PREFIX = refresh_pause_prefix
        ncbi_pacing.KEY_PREFIX = original
        mycomap_org_service.SEARCH_AUTH_KEY_PREFIX = auth_prefix
        mycomap_org_service.CURATION_PAUSE_KEY = curation_pause_key
        mycomap_org_service.CURATION_KEY_REFUSED_KEY = curation_key_refused
        mycomap_org_service.CURATION_EXEMPT_STATE_KEY = curation_exempt_state


@pytest.fixture(autouse=True, scope="session")
def _isolate_fasta_diagnostic_archives(tmp_path_factory):
    from app.services import fasta_input_service

    original = fasta_input_service.ARCHIVE_DIR
    fasta_input_service.ARCHIVE_DIR = tmp_path_factory.mktemp("fasta-rejections")
    try:
        yield
    finally:
        fasta_input_service.ARCHIVE_DIR = original


@pytest.fixture(autouse=True, scope="session")
def _isolate_api_diagnostic_archives(tmp_path_factory):
    """Point failed-API-response archives at a temporary directory for the run.

    Session-scoped and autouse so no test can opt out by omission. Individual
    tests may still monkeypatch ARCHIVE_DIR to their own tmp_path; monkeypatch
    restores it to this temporary directory afterwards, never to production.
    """
    from app.services import api_diagnostics

    original = api_diagnostics.ARCHIVE_DIR
    api_diagnostics.ARCHIVE_DIR = tmp_path_factory.mktemp("api-responses")
    try:
        yield api_diagnostics.ARCHIVE_DIR
    finally:
        api_diagnostics.ARCHIVE_DIR = original


@pytest.fixture(autouse=True, scope="session")
def _isolate_inat_redis_keys():
    """Keep tests off the production iNaturalist pacing, cooldown and caches.

    Alan 9/23/26 - Those live in the same Redis the site uses. A synthetic 429
    in a test started a real ten-minute cooldown that would have paused every
    production bulk job, and each paced test took a real slot from live
    traffic. Everything is moved under a test prefix for the run instead.
    """
    from app.services import inaturalist_tree_service as svc

    names = ("_PACING_KEY", "_BULK_PACING_KEY", "_COOLDOWN_KEY", "_LOOKUP_CACHE_KEY")
    original = {name: getattr(svc, name) for name in names}
    for name, value in original.items():
        setattr(svc, name, value.replace("dikarya:inat:", "dikarya:test:inat:", 1))
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(svc, name, value)


@pytest.fixture(autouse=True, scope="session")
def _isolate_type_specimen_cache(tmp_path_factory):
    """Keep GenBank /type_material answers from tests out of the real cache.

    Alan 9/24/26 - _parse_genbank_xml() records every type-bearing record it
    parses into type_specimen_service.DATA_DIR, which defaults to the shared
    cache/type_specimens directory the live site reads.
    """
    from app.services import type_specimen_service

    original = type_specimen_service.DATA_DIR
    bundled_original = type_specimen_service.BUNDLED_MYCOMAP_PATH
    type_specimen_service.DATA_DIR = tmp_path_factory.mktemp("type-specimens")
    type_specimen_service.BUNDLED_MYCOMAP_PATH = type_specimen_service.DATA_DIR / "bundled.json"
    try:
        yield type_specimen_service.DATA_DIR
    finally:
        type_specimen_service.DATA_DIR = original
        type_specimen_service.BUNDLED_MYCOMAP_PATH = bundled_original


@pytest.fixture(autouse=True, scope="session")
def _no_mycomap_curation_from_genbank_fetches():
    """Keep every GenBank fetch in the suite off mycomap.org.

    fetch_fasta_for_accessions() asks MycoMap.org for curated names; tests mock
    NCBI and would otherwise make that call for real. A test of the override
    monkeypatches blast_service._mycomap_curated_names itself.
    """
    from app.services import blast_service

    original = blast_service._mycomap_curated_names
    blast_service._mycomap_curated_names = lambda accessions: {}
    try:
        yield
    finally:
        blast_service._mycomap_curated_names = original
