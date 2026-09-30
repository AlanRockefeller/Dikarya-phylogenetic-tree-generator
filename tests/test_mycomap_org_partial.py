"""A partial NCBI fetch must remain visibly incomplete."""

from unittest.mock import patch

from app.services import mycomap_org_service as org


_HIT = (b"<Hit><Hit_id>1</Hit_id><Hit_def>Example fungus</Hit_def>"
        b"<Hit_accession>{accession}</Hit_accession><Hit_len>4</Hit_len>"
        b"<Hit_hsps><Hsp><Hsp_score>4</Hsp_score><Hsp_identity>4</Hsp_identity>"
        b"<Hsp_align-len>4</Hsp_align-len><Hsp_query-from>1</Hsp_query-from>"
        b"<Hsp_query-to>4</Hsp_query-to><Hsp_hit-from>1</Hsp_hit-from>"
        b"<Hsp_hit-to>4</Hsp_hit-to><Hsp_hseq>ACGT</Hsp_hseq></Hsp>"
        b"</Hit_hsps></Hit>")
XML = (b"<BlastOutput><BlastOutput_query-len>4</BlastOutput_query-len>"
       b"<BlastOutput_iterations><Iteration><Iteration_hits>"
       + _HIT.replace(b"{accession}", b"PX215422")
       + _HIT.replace(b"{accession}", b"PX215423")
       + b"</Iteration_hits></Iteration></BlastOutput_iterations></BlastOutput>")


class _NCBIAnswer(dict):
    """Support both the current and older _ncbi_sequences return contracts."""

    def __iter__(self):
        yield dict(self)
        yield set()


def test_one_missing_full_record_flags_ncbi_as_incomplete():
    status = {"ncbi": {"status": "complete", "has_results": True}}
    with patch.object(org, "_json", return_value=status), \
         patch.object(org, "_read", return_value=XML), \
         patch.object(org, "_ncbi_sequences",
                      return_value=_NCBIAnswer({"PX215422": "ACGT"})):
        result = org.fetch_results("123", include_local=False)

    assert result["ncbi_count"] == 1
    assert result["failed_sources"] == ["ncbi"]
    assert "full NCBI sequences for 1" in result["errors"][0]
