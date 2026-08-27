import unittest

from app.api_v1.openapi import _schemas
from app.api_v1.routes import LIMITS


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

    def test_mcmc_generations_documents_rejection_not_blind_acceptance(self):
        """The described contract has to match _validate_clamped_int().

        The wording used to promise that "an explicitly supplied value is
        always used as given", which reads as "the server takes whatever I
        send". It does not: outside 1,000..100,000,000, or not an integer, the
        request is refused with 422 rather than clamped to the nearest bound.
        """
        low, high = LIMITS["mcmc_generations"]
        schemas = _schemas()
        for schema_name in ("CreateJobRequest", "RecomputeRequest"):
            with self.subTest(schema=schema_name):
                field = schemas[schema_name]["properties"]["mcmc_generations"]
                description = field["description"]

                # The advertised bounds are the ones validation enforces.
                self.assertEqual(field["minimum"], low)
                self.assertEqual(field["maximum"], high)
                self.assertIn(f"{low:,}", description)
                self.assertIn(f"{high:,}", description)

                self.assertIn("rejected", description)
                self.assertNotIn("always used as given", description)


if __name__ == "__main__":
    unittest.main()
