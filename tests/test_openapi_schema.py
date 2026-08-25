import unittest

from app.api_v1.openapi import _schemas


class OpenAPISchemaTests(unittest.TestCase):
    def test_tree_model_has_conditional_documentation_and_no_default(self):
        tree_model = _schemas()["CreateJobRequest"]["properties"]["tree_model"]

        self.assertNotIn("default", tree_model)
        self.assertIn("tree_method=iqtree", tree_model["description"])
        self.assertIn("ModelFinder", tree_model["description"])
        self.assertIn("DEFAULT_ML_MODEL", tree_model["description"])

    def test_nullable_job_booleans_document_missing_and_unrecognized_values(self):
        params = _schemas()["Job"]["properties"]["params"]["properties"]
        for field in ("trim_terminal_overhangs", "fix_orientation"):
            with self.subTest(field=field):
                description = params[field]["description"]
                self.assertIn("not recorded", description)
                self.assertIn("not a recognized boolean", description)


if __name__ == "__main__":
    unittest.main()
