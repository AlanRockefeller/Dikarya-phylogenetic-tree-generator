
import sys
import unittest
from pathlib import Path
import shutil
import tempfile
import logging

# Ensure app is in path if needed, though usually not for simple unit tests
# For this environment, we might need to adjust path
sys.path.append("/var/www/dikarya")

from app.services.fasta_utils import sanitize_fasta_headers, restore_tree_names

class TestFastaSanitization(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.input_fasta = self.test_dir / "test_input.fasta"
        self.output_fasta = self.test_dir / "test_sanitized.fasta"
        self.tree_file = self.test_dir / "test_tree.newick"
        
        # Create a FASTA with problematic names
        content = """>Seq 1 with spaces
ACGT
>Seq'2' with quotes
ACGT
>Seq:3 with colons
ACGT
>Seq(4) with parens
ACGT
"""
        self.input_fasta.write_text(content)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_sanitize_and_restore(self):
        # 1. Sanitize
        mapping = sanitize_fasta_headers(self.input_fasta, self.output_fasta)
        
        # Verify mapping keys are safe
        print("Mapping:", mapping)
        for safe_id in mapping.keys():
            self.assertTrue(safe_id.startswith("SEQ"), f"ID {safe_id} should start with SEQ")
            self.assertTrue(" " not in safe_id, "ID should not contain spaces")
            
        # Verify sanitized file content
        clean_content = self.output_fasta.read_text()
        print("Sanitized content:\n", clean_content)
        self.assertNotIn("Seq 1 with spaces", clean_content)
        self.assertIn("SEQ000001", clean_content)

        # 2. Simulate Tree Output with safe IDs
        # (RAxML output would look like ((SEQ1:0.1, SEQ2:0.1):0.1, ...)
        tree_content = "(SEQ000001:0.1, (SEQ000002:0.1, SEQ000003:0.2):0.5, SEQ000004:0.1);"
        self.tree_file.write_text(tree_content)
        
        # 3. Restore
        restore_tree_names(self.tree_file, mapping)
        
        # 4. Verify Restored content
        restored_content = self.tree_file.read_text()
        print("Restored content:\n", restored_content)
        
        # Check that original names are back (quoted if necessary)
        self.assertIn("'Seq 1 with spaces'", restored_content)
        self.assertIn("'Seq''2'' with quotes'", restored_content)
        self.assertIn("'Seq:3 with colons'", restored_content)
        self.assertIn("'Seq(4) with parens'", restored_content)
        
        # Check that safe IDs are gone
        self.assertNotIn("SEQ000001", restored_content)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    unittest.main()
