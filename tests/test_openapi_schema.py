import unittest

from flask import Flask

from app.api_v1.openapi import _schemas, build_spec
from app.api_v1.routes import LIMITS
from app.services import inat_finder_service as finder


class OpenAPISchemaTests(unittest.TestCase):
    def test_inaturalist_finder_is_fully_documented(self):
        app = Flask(__name__)
        with app.test_request_context(base_url="https://dikarya.us"):
            operation = build_spec()["paths"]["/tools/inaturalist-finder"]["post"]

        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        # Only the observation number is required now: an automatic search may be
        # run with any combination of clues, including none at all.
        self.assertEqual(schema["required"], ["observation"])
        self.assertEqual(schema["properties"]["digits_off"]["maximum"], 3)
        self.assertEqual(operation["security"], [{"bearerAuth": ["tools:read"]}])
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"]
            ["properties"]["data"]["$ref"],
            "#/components/schemas/InaturalistFinderAnyResult",
        )
        # The runtime propagates InatTreeError's default status=400 (an
        # unparseable observation number or URL reaches the handler that way),
        # so the document has to advertise it alongside the 422 it also returns.
        self.assertIn("400", operation["responses"])
        self.assertEqual(
            operation["responses"]["400"]["content"]["application/json"]["schema"]["$ref"],
            operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"],
        )

    def test_inaturalist_finder_documents_the_deadline_504(self):
        """`InatDeadlineExceeded` carries status 504 and the route propagates it.

        Deadline exhaustion before a resumable search position exists - clue
        resolution, for instance - reaches the caller as a 504, so the document
        has to advertise it with the ordinary error body.
        """
        app = Flask(__name__)
        with app.test_request_context(base_url="https://dikarya.us"):
            operation = build_spec()["paths"]["/tools/inaturalist-finder"]["post"]

        self.assertIn("504", operation["responses"])
        self.assertEqual(
            operation["responses"]["504"]["content"]["application/json"]["schema"]["$ref"],
            operation["responses"]["502"]["content"]["application/json"]["schema"]["$ref"],
        )
        self.assertIn("deadline", operation["responses"]["504"]["description"].lower())
        # And the prose says when a caller should expect it, rather than
        # promising a cursor for every exhausted limit.
        self.assertIn("504", operation["description"])

    def test_inaturalist_finder_documents_the_automatic_search(self):
        """The clue fields, the resume contract, and both result shapes."""
        schemas = _schemas()
        schema = None
        app = Flask(__name__)
        with app.test_request_context(base_url="https://dikarya.us"):
            operation = build_spec()["paths"]["/tools/inaturalist-finder"]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        properties = schema["properties"]

        # Every clue is documented, optional, and not part of a one-of-five choice.
        for clue in ("genus", "family", "taxon", "user", "project"):
            with self.subTest(clue=clue):
                self.assertIn(clue, properties)
                self.assertNotIn(clue, schema["required"])
        # The single-criterion shape is still documented rather than removed.
        self.assertIn("mode", properties)
        self.assertIn("term", properties)

        # Bounded auto is only usable if resuming is documented alongside it.
        self.assertIn("resume", properties)
        self.assertIn("confirm", properties)
        for word in ("resume", "needs_confirmation", "5,000", "10,000"):
            self.assertIn(word, operation["description"], word)

        # Both result shapes exist and the union points at them.
        union = schemas["InaturalistFinderAnyResult"]["oneOf"]
        self.assertIn({"$ref": "#/components/schemas/InaturalistFinderAutoResult"}, union)
        self.assertIn({"$ref": "#/components/schemas/InaturalistFinderResult"}, union)

        auto = schemas["InaturalistFinderAutoResult"]["properties"]
        for field in ("status", "complete", "unusable_clues", "original", "resume", "next_stage"):
            with self.subTest(field=field):
                self.assertIn(field, auto)
        self.assertIn("needs_confirmation", auto["status"]["enum"])
        self.assertIn("incomplete", auto["status"]["enum"])

        # `unknown` must be documented as its own verdict, not folded into "no".
        score = schemas["InaturalistFinderScore"]
        self.assertIn("unknown", score["properties"])
        self.assertIn("never", score["description"])

    def test_the_documented_stop_reasons_include_every_one_the_finder_emits(self):
        """A stop reason the document does not list is a lie to an integrator.

        The deadline added a sixth reason, and the enum is the only place that
        would not have failed loudly when it drifted.
        """
        import re
        from pathlib import Path

        source = Path(finder.__file__).read_text(encoding="utf-8")
        emitted = set(re.findall(r'return finish\(\s*"[a-z_]+",\s*"([a-z_]+)"', source))
        # `stop_reason` also carries the value assigned before a break.
        emitted.update(re.findall(r'stop_reason = "([a-z_]+)"', source))

        schemas = _schemas()
        documented = set(
            schemas["InaturalistFinderAutoResult"]["properties"]["stop_reason"]["enum"]
        )
        self.assertTrue(
            emitted <= documented,
            "undocumented finder stop reasons: {}".format(sorted(emitted - documented)),
        )

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
