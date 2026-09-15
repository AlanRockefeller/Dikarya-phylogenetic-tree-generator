"""Quick Tree accepts barcodes, not genomes -- and never claimed a bootstrap.

Quick Tree is the two-click preset: fixed MAFFT --auto, trimAl, FastTree, no
parameter form. MAFFT --auto's cost grows with sequence LENGTH as well as
count, so one pathological record turns a ten-second job into one that holds
the single worker slot for hours. Measured across the 11,670 job directories on
disk: 1,515,220 submitted records, 400 of them over 10 kb, the longest a 149 kb
complete phage genome.

The cap is deliberately NOT site-wide. A 30 kb mitochondrial region with eight
taxa is a perfectly reasonable RAxML run, so the advanced tree builder is
untouched.

The browser half runs the shipped code out of sequence_entry.html through node,
rather than a Python re-creation of it, so the two checks cannot drift.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.tree_parameter_validation import (
    ADVANCED_SUBMISSION_MODE,
    ADVANCED_TREE_BUILDER_FIELDS,
    QUICK_TREE_MAX_SEQUENCE_BP,
    QUICK_TREE_SUBMISSION_MODE,
    oversized_quick_tree_records,
    quick_tree_length_error,
    submission_is_quick_tree,
)

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "app" / "templates" / "sequence_entry.html"
JS_DIR = Path(__file__).resolve().parent / "js"

# What the two Quick Tree buttons post, minus the sequence payload.
QUICK_TREE_BODY = {
    "input_type": "sequence",
    "notes": "Quick Tree",
    "alignment_method": "mafft",
    "trimming_method": "trimal_gappy",
    "trim_terminal_overhangs": True,
    "tree_method": "fasttree",
    "tree_model": "GTR+G",
    "submission_mode": QUICK_TREE_SUBMISSION_MODE,
}

# The same preset without the marker: a browser still running a cached copy of
# the page from before it existed.
UNMARKED_QUICK_TREE_BODY = {
    key: value for key, value in QUICK_TREE_BODY.items() if key != "submission_mode"
}


def _fasta(*lengths):
    return "".join(
        f">seq{index}\n{'A' * length}\n" for index, length in enumerate(lengths)
    )


class QuickTreeDetectionTests(unittest.TestCase):
    """Which submissions carry the cap, decided by the marker the UI sends.

    Alan 9/14/26 - This used to be "uses FastTree, and sends none of a
    hand-maintained list of advanced fields", which is neither necessary nor
    sufficient. A deliberate FastTree + MUSCLE + no-trimming API request was
    classified as the preset and had its long sequence rejected; a real preset
    submission could opt out by adding `seed: null`. Both are fixed by asking
    the client which form it is, and falling back to matching the preset
    *exactly* when it does not say.
    """

    def test_the_marker_decides(self):
        self.assertTrue(submission_is_quick_tree(QUICK_TREE_BODY))
        self.assertFalse(submission_is_quick_tree(
            dict(QUICK_TREE_BODY, submission_mode=ADVANCED_SUBMISSION_MODE)
        ))

    def test_a_marked_quick_tree_cannot_opt_out_by_adding_a_stray_field(self):
        # The evasion the old fallback allowed: `seed: null` and the cap was
        # gone. The marker is not affected by what else is in the body.
        for field in sorted(ADVANCED_TREE_BUILDER_FIELDS):
            body = dict(QUICK_TREE_BODY)
            body[field] = None
            self.assertTrue(submission_is_quick_tree(body), field)

    def test_an_unrecognised_mode_falls_back_rather_than_failing_open(self):
        """A typo in our own marker must not switch the guardrail off.

        Only "advanced" opts out. Anything else -- a typo, a value from some
        future form -- is treated as "no marker" and settled by the preset
        match, which is the fail-safe direction for a guardrail against
        accidents.
        """
        body = dict(QUICK_TREE_BODY, submission_mode="quicktree")
        self.assertTrue(submission_is_quick_tree(body))

        # And a typo'd advanced marker still opts out on the parameter block.
        advanced = dict(UNMARKED_QUICK_TREE_BODY, submission_mode="advnaced",
                        run_preset="fast_good")
        self.assertFalse(submission_is_quick_tree(advanced))

    def test_only_advanced_opts_out_explicitly(self):
        self.assertFalse(submission_is_quick_tree(
            dict(QUICK_TREE_BODY, submission_mode=ADVANCED_SUBMISSION_MODE)
        ))

    def test_the_marker_is_case_and_whitespace_tolerant(self):
        self.assertTrue(
            submission_is_quick_tree(dict(QUICK_TREE_BODY, submission_mode=" Quick_Tree "))
        )

    # --- the no-marker fallback -------------------------------------------

    def test_an_unmarked_preset_is_still_recognised(self):
        # A cached page from before the marker shipped.
        self.assertTrue(submission_is_quick_tree(UNMARKED_QUICK_TREE_BODY))

    def test_a_custom_fasttree_request_is_not_quick_tree(self):
        """The false positive that motivated this rewrite.

        FastTree with MUSCLE and no trimming is a deliberate choice, not the
        preset, and it must not inherit the preset's 10 kb cap.
        """
        body = dict(UNMARKED_QUICK_TREE_BODY,
                    alignment_method="muscle", trimming_method="none")
        self.assertFalse(submission_is_quick_tree(body))

    def test_each_preset_value_is_required(self):
        for field, other in (
            ("alignment_method", "clustalo"),
            ("trimming_method", "bmge"),
            ("tree_method", "raxml"),
            ("tree_model", "JC"),
        ):
            body = dict(UNMARKED_QUICK_TREE_BODY)
            body[field] = other
            self.assertFalse(submission_is_quick_tree(body), field)

    def test_terminal_overhang_trimming_is_part_of_the_preset(self):
        body = dict(UNMARKED_QUICK_TREE_BODY, trim_terminal_overhangs=False)
        self.assertFalse(submission_is_quick_tree(body))

    def test_the_advanced_form_reproducing_the_preset_is_not_capped(self):
        """The case the marker exists for.

        mafft + trimAl-gappy + FastTree + GTR+G is a reasonable thing to pick by
        hand in the advanced form, and picking it must not silently impose a
        limit the advanced builder has never had. The marker settles it; the
        advanced parameter block settles it for an unmarked request.
        """
        marked = dict(UNMARKED_QUICK_TREE_BODY,
                      submission_mode=ADVANCED_SUBMISSION_MODE, run_preset="fast_good")
        self.assertFalse(submission_is_quick_tree(marked))

        unmarked = dict(UNMARKED_QUICK_TREE_BODY, run_preset="fast_good",
                        enable_bootstrap=True, mcmc_generations=1000000)
        self.assertFalse(submission_is_quick_tree(unmarked))

    def test_another_tree_method_is_never_quick_tree(self):
        for method in ("raxml", "iqtree", "mrbayes", "nj"):
            self.assertFalse(
                submission_is_quick_tree(
                    dict(UNMARKED_QUICK_TREE_BODY, tree_method=method)
                ),
                method,
            )

    def test_a_non_dict_body_is_not_quick_tree(self):
        self.assertFalse(submission_is_quick_tree(None))
        self.assertFalse(submission_is_quick_tree("fasttree"))

    def test_the_cap_is_documented_as_a_guardrail_not_a_boundary(self):
        """A declared advanced submission is not capped, on purpose.

        The advanced builder has always accepted a long locus, so declaring
        advanced mode opens nothing that was not already open. Anyone reading
        this as an abuse boundary would be wrong, and the docstring says so.
        """
        self.assertIn("not an abuse boundary",
                      submission_is_quick_tree.__doc__)


class QuickTreeLengthTests(unittest.TestCase):
    def test_the_limit_is_ten_kilobases(self):
        self.assertEqual(QUICK_TREE_MAX_SEQUENCE_BP, 10_000)

    def test_exactly_ten_thousand_bases_is_accepted(self):
        fasta = _fasta(10_000, 600)
        self.assertEqual(oversized_quick_tree_records(fasta), [])
        self.assertIsNone(quick_tree_length_error(QUICK_TREE_BODY, fasta))

    def test_ten_thousand_and_one_bases_is_rejected(self):
        fasta = _fasta(10_001, 600)
        self.assertEqual(oversized_quick_tree_records(fasta), [("seq0", 10_001)])
        error = quick_tree_length_error(QUICK_TREE_BODY, fasta)
        self.assertIsNotNone(error)
        self.assertIn("10,000 bp", error)
        self.assertIn("advanced tree builder", error)
        self.assertIn("10,001", error)

    def test_whitespace_and_line_wrapping_do_not_count_as_bases(self):
        # Exactly at the limit, wrapped at 60 columns the way every FASTA the
        # queue builds is. A length check over the raw text would see 10,166.
        body = "\n".join(["A" * 60] * 166) + "\n" + "A" * 40
        self.assertEqual(len(body.replace("\n", "")), 10_000)
        self.assertEqual(oversized_quick_tree_records(f">seq0\n{body}\n"), [])

    def test_an_advanced_submission_may_carry_a_long_locus(self):
        # The whole point of scoping the cap: a long marker in the advanced
        # builder is legitimate and must not be rejected by the Quick Tree rule.
        body = dict(UNMARKED_QUICK_TREE_BODY,
                    submission_mode=ADVANCED_SUBMISSION_MODE,
                    tree_method="raxml", run_preset="publication",
                    enable_bootstrap=True, bootstrap_preset="standard")
        self.assertIsNone(quick_tree_length_error(body, _fasta(150_000, 149_000)))

    def test_an_advanced_fasttree_submission_may_carry_a_long_locus(self):
        body = dict(UNMARKED_QUICK_TREE_BODY,
                    submission_mode=ADVANCED_SUBMISSION_MODE,
                    run_preset="fast_good", enable_bootstrap=True)
        self.assertIsNone(quick_tree_length_error(body, _fasta(40_000, 600)))

    def test_a_custom_fasttree_api_request_may_carry_a_long_locus(self):
        """No marker, not the preset: the cap must not reach it.

        This is the false positive the old classifier produced -- FastTree plus
        MUSCLE plus no trimming was read as the preset and rejected.
        """
        body = {"input_type": "sequence", "tree_method": "fasttree",
                "alignment_method": "muscle", "trimming_method": "none"}
        self.assertIsNone(quick_tree_length_error(body, _fasta(40_000, 600)))

    def test_a_marked_quick_tree_is_capped_whatever_else_it_sends(self):
        body = dict(QUICK_TREE_BODY, seed=None, run_preset="fast_good")
        self.assertIsNotNone(quick_tree_length_error(body, _fasta(10_001)))

    def test_the_longest_record_is_named_not_the_first(self):
        error = quick_tree_length_error(QUICK_TREE_BODY, _fasta(12_000, 90_000, 600))
        self.assertIn("2 sequence(s)", error)
        self.assertIn("90,000", error)


class ServerEnforcementTests(unittest.TestCase):
    """create_job must reject before the job row is written.

    The route cannot be exercised here -- it needs Redis for the rate limiter
    and Postgres for the Job row -- so this asserts the wiring: the check runs,
    it runs against the request body rather than the normalized params (which
    fill in the advanced defaults and would make every submission look like the
    preset), and it returns a 400 rather than raising.
    """

    def setUp(self):
        self.source = (REPO / "app" / "api" / "routes.py").read_text(encoding="utf-8")

    def test_create_job_calls_the_check_against_the_raw_body(self):
        self.assertIn(
            'quick_tree_error = quick_tree_length_error(data, job_params.get("sequence", ""))',
            self.source,
        )

    def test_the_rejection_is_a_400_with_the_explanation(self):
        index = self.source.index("quick_tree_error = quick_tree_length_error")
        window = self.source[index:index + 600]
        self.assertIn('"status": "error", "error": quick_tree_error}), 400', window)
        self.assertIn('note_request_failure("quick_tree_sequence_too_long")', window)

    def test_the_check_runs_before_the_job_row_is_created(self):
        self.assertLess(
            self.source.index("quick_tree_error = quick_tree_length_error"),
            self.source.index("job_id = generate_job_id()"),
        )


class BrowserEnforcementTests(unittest.TestCase):
    """The shipped browser check, executed rather than re-implemented."""

    @staticmethod
    def _extracted():
        html = TEMPLATE.read_text(encoding="utf-8")
        start = html.index("    const QUICK_TREE_MAX_SEQUENCE_BP = ")
        end = html.index("    function buildFastaFromQueue() {", start)
        return html[start:end]

    def _run(self, queue):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extracted.js"
            path.write_text(self._extracted(), encoding="utf-8")
            proc = subprocess.run(
                [node, str(JS_DIR / "quick_tree_limit.test.js"), str(path),
                 json.dumps(queue)],
                capture_output=True, text=True,
            )
        if proc.returncode != 0:
            self.fail(f"node harness failed:\n{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    def test_the_browser_constant_matches_the_backend_constant(self):
        match = re.search(
            r"const QUICK_TREE_MAX_SEQUENCE_BP = (\d+);", self._extracted()
        )
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), QUICK_TREE_MAX_SEQUENCE_BP)

    def test_exactly_ten_thousand_bases_passes_in_the_browser(self):
        result = self._run([{"name": "seq0", "sequence": "A" * 10_000},
                            {"name": "seq1", "sequence": "ACGT"}])
        self.assertTrue(result["passes"])
        self.assertEqual(result["status"], [])

    def test_ten_thousand_and_one_bases_is_stopped_in_the_browser(self):
        result = self._run([{"name": "seq0", "sequence": "A" * 10_001},
                            {"name": "seq1", "sequence": "ACGT"}])
        self.assertFalse(result["passes"])
        self.assertEqual(len(result["status"]), 1)
        message, level = result["status"][0]
        self.assertEqual(level, "error")
        self.assertIn("10,000 bp", message)
        self.assertIn("advanced tree builder", message)

    def test_wrapped_sequence_text_is_measured_in_bases(self):
        wrapped = "\n".join(["A" * 60] * 166) + "\n" + "A" * 40
        result = self._run([{"name": "seq0", "sequence": wrapped},
                            {"name": "seq1", "sequence": "ACGT"}])
        self.assertTrue(result["passes"])


class QuickTreeFastTreeSupportTests(unittest.TestCase):
    """FastTree never had a bootstrap, and Quick Tree no longer claims one."""

    def setUp(self):
        self.html = TEMPLATE.read_text(encoding="utf-8")

    def _quick_tree_payloads(self):
        payloads = []
        for match in re.finditer(r"const payload = \{(.*?)\n        \};",
                                 self.html, re.S):
            body = match.group(1)
            if "tree_method: 'fasttree'" in body:
                payloads.append(body)
        return payloads

    def test_both_quick_tree_payloads_exist_and_send_no_bootstrap(self):
        payloads = self._quick_tree_payloads()
        # The queue's "Run Quick Tree" and MycoMap's "One-Click Tree".
        self.assertEqual(len(payloads), 2)
        for body in payloads:
            self.assertNotRegex(body, r"^\s*bootstrap:", )
            self.assertNotIn("bootstrap: 100", body)

    def test_both_quick_tree_payloads_declare_the_preset(self):
        for body in self._quick_tree_payloads():
            self.assertIn("submission_mode: 'quick_tree'", body)

    def test_the_advanced_payload_declares_itself_advanced(self):
        # Without this the advanced form could reproduce the preset's four
        # values by hand and inherit a cap it has never had.
        self.assertIn("submission_mode: 'advanced'", self.html)
        self.assertEqual(self.html.count("submission_mode: 'advanced'"), 1)
        self.assertEqual(self.html.count("submission_mode: 'quick_tree'"), 2)

    def test_the_advanced_form_keeps_its_bootstrap_control(self):
        # Removing it from the preset must not remove it from RAxML/IQ-TREE.
        self.assertIn("bootstrap: parseInt(document.getElementById('bootstrap').value)",
                      self.html)

    def test_fasttree_metadata_still_reports_sh_like_support(self):
        """The real run_tree_builder, with only the executable stubbed out."""
        import logging
        from unittest.mock import patch

        from app.models import TreeBuilderParams
        from app.services import tree_builder_service as tbs

        params = TreeBuilderParams(method="fasttree", model="GTR+G", bootstrap=1000)

        with tempfile.TemporaryDirectory() as tmp:
            newick = Path(tmp) / "tree_original.newick"
            nexus = Path(tmp) / "tree_original.nexus"

            def _fake_fasttree(alignment, out_newick, out_nexus, *args, **kwargs):
                out_newick.write_text("(a:0.1,b:0.1);\n", encoding="utf-8")
                return {}

            with patch.object(tbs, "_run_fasttree", side_effect=_fake_fasttree), \
                    patch.object(tbs, "_discard_scratch_inputs"):
                metadata = tbs.run_tree_builder(
                    Path(tmp) / "alignment_trimmed.fasta", newick, nexus,
                    params, object(), logging.getLogger(__name__),
                )

        self.assertEqual(metadata["support_type"], "sh_like")
        self.assertEqual(metadata["support_resamples"], tbs.FASTTREE_SH_RESAMPLES)
        # Not a bootstrap proportion, and never reported as one.
        self.assertIsNone(metadata["bootstrap"])

    def test_the_fasttree_command_is_unchanged(self):
        import inspect

        from app.services import tree_builder_service as tbs

        source = inspect.getsource(tbs._run_fasttree)
        self.assertIn('"-boot", str(FASTTREE_SH_RESAMPLES)', source)
        # -boot is SH-like local support resampling, not classical bootstrap,
        # and the resample count is fixed rather than read from the submitted
        # bootstrap. The only mention of params.bootstrap is the docstring
        # saying so.
        code = source.split('"""', 2)[-1]
        self.assertNotIn("params.bootstrap", code)
        self.assertIn("params.bootstrap is deliberately unused", source)


class PersistedBootstrapTests(unittest.TestCase):
    def test_create_job_drops_an_unsent_bootstrap_for_fasttree(self):
        source = (REPO / "app" / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn(
            'if tree_method == "fasttree" and "bootstrap" not in data:', source
        )
        index = source.index('if tree_method == "fasttree" and "bootstrap" not in data:')
        self.assertIn('job_params.pop("bootstrap", None)', source[index:index + 900])

    def test_an_explicit_bootstrap_is_still_stored(self):
        # The advanced form sends one; dropping it would change what the job
        # page reports for a deliberate choice.
        source = (REPO / "app" / "api" / "routes.py").read_text(encoding="utf-8")
        index = source.index('if tree_method == "fasttree" and "bootstrap" not in data:')
        self.assertIn('"bootstrap" not in data', source[index:index + 120])


if __name__ == "__main__":
    unittest.main()
