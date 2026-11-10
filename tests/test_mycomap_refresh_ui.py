"""The MycoMap refresh form must make its two independent limits unambiguous."""

from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "templates"
    / "sequence_entry.html"
)


def test_inactive_ncbi_limit_is_disabled_and_explained():
    source = TEMPLATE.read_text(encoding="utf-8")

    ncbi_input = source[source.index('id="mycomap_refresh_ncbi_limit"'):
                        source.index('id="mycomap_refresh_ncbi_limit"') + 180]
    assert "disabled" in ncbi_input
    assert "Local and NCBI hit counts are separate" in source
    assert "updateMycomapRefreshOptions" in source


def test_refresh_request_announces_and_sends_the_local_limit_it_uses():
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "requestedLocalHits" in source
    assert "local_limit: requestedLocalHits" in source
    assert "local BLAST for up to ${requestedLocalHits} hits" in source
