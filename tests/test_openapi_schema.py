import unittest

from app.api_v1.openapi import _schemas


class OpenAPISchemaTests(unittest.TestCase):
    def test_tree_model_has_conditional_documentation_and_no_default(self):
        tree_model = _schemas()["CreateJobRequest"]["properties"]["tree_model"]

        self.assertNotIn("default", tree_model)
        self.assertIn("tree_method=iqtree", tree_model["description"])
        self.assertIn("ModelFinder", tree_model["description"])
        self.assertIn("DEFAULT_ML_MODEL", tree_model["description"])


if __name__ == "__main__":
    unittest.main()
