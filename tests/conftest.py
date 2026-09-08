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
