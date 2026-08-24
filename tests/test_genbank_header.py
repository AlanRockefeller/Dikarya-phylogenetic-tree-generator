
import unittest
from unittest.mock import patch, MagicMock
from app.services.blast_service import _parse_genbank_xml, _build_header, fetch_fasta_for_accessions

class TestGenBankHeader(unittest.TestCase):

    def test_parse_structured_type(self):
        xml_text = """
        <GBSet>
            <GBSeq>
                <GBSeq_primary-accession>KA123456</GBSeq_primary-accession>
                <GBSeq_accession-version>KA123456.1</GBSeq_accession-version>
                <GBSeq_organism>Fungi sp.</GBSeq_organism>
                <GBSeq_sequence>atgc</GBSeq_sequence>
                <GBSeq_feature-table>
                    <GBFeature>
                        <GBFeature_key>source</GBFeature_key>
                        <GBFeature_quals>
                            <GBQualifier>
                                <GBQualifier_name>type_material</GBQualifier_name>
                                <GBQualifier_value>holotype of Fungi sp.</GBQualifier_value>
                            </GBQualifier>
                        </GBFeature_quals>
                    </GBFeature>
                </GBSeq_feature-table>
            </GBSeq>
        </GBSet>
        """
        parsed = _parse_genbank_xml(xml_text)
        record = parsed["by_ver"]["KA123456.1"]
        self.assertEqual(record["type_material"], "holotype of Fungi sp.")
        
        header = _build_header(record)
        # Should strip " of Fungi sp." and NOT have "type_material" prefix
        self.assertIn("holotype", header)
        self.assertNotIn("type_material", header)
        self.assertTrue(header.strip().endswith("holotype"))

    def test_redundant_organism_in_sample_id(self):
        record = {
            "version": "ACC.1",
            "organism": "Myco sp.",
            "source_features": {
                "strain": "Myco sp. 123",
                "specimen_voucher": "Myco sp. VOU-456"
            }
        }
        # specimen_voucher priority
        header = _build_header(record)
        # Should strip redundant organism
        self.assertIn("specimen_voucher VOU-456", header)
        self.assertNotIn("Myco sp. VOU-456", header)
        
        # Test fallback to strain
        del record["source_features"]["specimen_voucher"]
        header = _build_header(record)
        self.assertIn("strain 123", header)
        self.assertNotIn("Myco sp. 123", header)

    def test_redundant_organism_in_extras(self):
        record = {
            "version": "ACC.1",
            "organism": "Psilocybe cyanescens",
            "source_features": {
                "geo_loc_name": "USA Psilocybe cyanescens",
                "country": "Psilocybe cyanescens"
            }
        }
        header = _build_header(record)
        # Should strip redundant organism from extras
        # geo_loc_name "USA Psilocybe cyanescens" -> "USA"
        self.assertIn("USA", header)
        self.assertNotIn("USA Psilocybe cyanescens", header)
        
    def test_reproduction_OQ871811(self):
        # User reported: ">OQ871811.1 Psilocybe zapotecorum ... Ecuador: ... Garcia Moreno Psilocybe zapotecorum"
        record = {
            "version": "OQ871811.1",
            "organism": "Psilocybe zapotecorum",
            "source_features": {
                "isolate": "RLC_625_iNat_122513549",
                "geo_loc_name": "Ecuador: Reserva Los Cedros, Imbabura, Cotacachi, Garcia Moreno Psilocybe zapotecorum"
            }
        }
        header = _build_header(record)
        target = "Ecuador: Reserva Los Cedros, Imbabura, Cotacachi, Garcia Moreno"
        self.assertIn(target, header)
        # Should NOT have the organism at the end
        self.assertNotIn("Garcia Moreno Psilocybe zapotecorum", header)

    def test_xml_parsing_cleans_whitespace(self):
        # Verify that _parse_genbank_xml strips whitespace from valid fields
        xml_text = """
        <GBSet>
            <GBSeq>
                <GBSeq_primary-accession>DIRTY1</GBSeq_primary-accession>
                <GBSeq_organism> Psilocybe zapotecorum \n</GBSeq_organism>
                <GBSeq_definition> \n Definition \n </GBSeq_definition>
            </GBSeq>
        </GBSet>
        """
        parsed = _parse_genbank_xml(xml_text)
        record = parsed["by_acc"]["DIRTY1"]
        
        # Check organism is clean
        self.assertEqual(record["organism"], "Psilocybe zapotecorum")
        # Check definition is clean (used in blob)
        self.assertIn("Definition", record["blob"]) 
        self.assertNotIn("\n Definition", record["blob"])

        # Create record with manual clean organism (simulating post-parse) to ensure stripping works
        record["source_features"] = {
            "geo_loc_name": "Location Psilocybe zapotecorum"
        }
        header = _build_header(record)
        self.assertNotIn("Location Psilocybe zapotecorum", header)

    def test_geo_loc_format(self):
        record = {
            "version": "ACC.1",
            "organism": "Org",
            "source_features": {
                "geo_loc_name": "USA: Maryland",
                "country": "USA",
                "host": "Homo sapiens",
                "collection_date": "2020"
            }
        }
        header = _build_header(record)
        # Should be just value, no key=
        self.assertIn("USA: Maryland", header)
        self.assertNotIn("geo_loc_name=", header)
        # Should exclude host and collection_date
        self.assertNotIn("Homo sapiens", header)
        self.assertNotIn("2020", header)

    def test_keyword_fallback(self):
        xml_text = """
        <GBSet>
            <GBSeq>
                <GBSeq_primary-accession>KA123456</GBSeq_primary-accession>
                <GBSeq_organism>Fungi sp.</GBSeq_organism>
                <GBSeq_sequence>atgc</GBSeq_sequence>
                <GBSeq_comment>This is a Holotype specimen.</GBSeq_comment>
            </GBSeq>
        </GBSet>
        """
        parsed = _parse_genbank_xml(xml_text)
        record = parsed["by_acc"]["KA123456"]
        self.assertIsNone(record["type_material"])
        
        header = _build_header(record)
        self.assertTrue(header.endswith("holotype"))

    def test_sample_id_priority(self):
        # Priority: specimen_voucher > culture_collection > bio_material > isolate > strain
        record = {
            "version": "ACC.1",
            "organism": "Org",
            "source_features": {
                "strain": "Strain123", # low priority
                "specimen_voucher": "VoucherXYZ" # high priority
            }
        }
        header = _build_header(record)
        # The rewritten-string assertion this replaces only ever compared a value
        # against a string its own .replace() had just produced, so it held no
        # matter what _build_header returned.
        self.assertIn("ACC.1 Org specimen_voucher VoucherXYZ", header)
        self.assertNotIn("strain", header)

    def test_sequence_cleanup(self):
        xml_text = """
        <GBSet>
            <GBSeq>
                <GBSeq_primary-accession>A</GBSeq_primary-accession>
                <GBSeq_sequence> a t g c \n - . n </GBSeq_sequence>
            </GBSeq>
        </GBSet>
        """
        parsed = _parse_genbank_xml(xml_text)
        seq = parsed["by_acc"]["A"]["sequence"]
        self.assertEqual(seq, "ATGCN") # Removed spaces, -, ., kept n, uppercased

    @patch("app.services.blast_service._fetch_genbank_xml_batch")
    def test_fetch_fasta_integration(self, mock_fetch):
        # Mock XML response. _fetch_genbank_xml_batch returns a *list* of
        # documents so a batch that had to be retried per accession can keep
        # the records that did resolve.
        mock_fetch.return_value = ["""
        <GBSet>
            <GBSeq>
                <GBSeq_primary-accession>AA123456</GBSeq_primary-accession>
                <GBSeq_accession-version>AA123456.1</GBSeq_accession-version>
                <GBSeq_organism>Org1</GBSeq_organism>
                <GBSeq_sequence>AAAA</GBSeq_sequence>
            </GBSeq>
        </GBSet>
        """]
        
        result = fetch_fasta_for_accessions(["AA123456"])
        self.assertIn(">AA123456.1 Org1", result)
        self.assertIn("\nAAAA", result)

    @patch("app.services.blast_service._fetch_genbank_xml_batch")
    @patch("app.services.blast_service._ncbi_request")
    def test_fallback_missing_sequence(self, mock_ncbi, mock_fetch):
        # XML return has record but NO sequence
        mock_fetch.return_value = ["""
        <GBSet>
            <GBSeq>
                <GBSeq_primary-accession>AA123456</GBSeq_primary-accession>
            </GBSeq>
        </GBSet>
        """]
        # Mock fallback response
        mock_ncbi.return_value.status_code = 200
        mock_ncbi.return_value.text = ">AA123456 Fallback\nTTTT"
        
        result = fetch_fasta_for_accessions(["AA123456"])
        
        # Should call fallback
        self.assertTrue(mock_ncbi.called)
        self.assertIn(">AA123456 Fallback", result)
        self.assertIn("TTTT", result)
        
if __name__ == "__main__":
    unittest.main()
