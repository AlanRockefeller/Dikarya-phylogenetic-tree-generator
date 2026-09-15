"""Which direction check MAFFT runs, and what Dikarya says about its answer.

Two separate things live here because they are two halves of one change:

* `--adjustdirectionaccurately` compares every sequence against every other
  before the alignment starts. `--adjustdirection` decides the same question
  from a 6-mer count. On barcode-length fungal input they agree, and the cheap
  one is a large fraction of the wall clock back, so the normal alignment path
  now takes the cheap one by default -- with `accurate` kept as a
  one-environment-variable rollback and `off` as an operator kill switch.

  The direction-only pre-pass MUSCLE, Clustal Omega and IQ-TREE's --align-only
  depend on is deliberately NOT included: there MAFFT's answer is the only
  direction signal there is, and the measurement behind the new default was
  taken on the normal path.

* ORIENT-vs-MAFFT disagreement used to be `reversed_count > orient_uncertain`,
  which compares two totals and therefore does not answer the question it was
  asked. One flip against two uncertain records reads as "agreement" whether or
  not the flipped record was one of the two. MAFFT names the records it
  reversed, via its `_R_` headers, so the answer is a set subtraction.
"""

import logging
import unittest
from unittest import mock

from app.config import Config, choice_env
from app.services import alignment_service


class DirectionModeResolutionTests(unittest.TestCase):
    def _config(self, mode):
        config = mock.Mock(spec_set=["MAFFT_DIRECTION_MODE"])
        config.MAFFT_DIRECTION_MODE = mode
        return config

    def test_the_shipped_default_is_the_fast_check(self):
        self.assertEqual(Config.MAFFT_DIRECTION_MODE, "fast")
        self.assertEqual(
            alignment_service.mafft_direction_flag(Config), "--adjustdirection"
        )

    def test_fast_selects_adjustdirection(self):
        self.assertEqual(
            alignment_service.mafft_direction_flag(self._config("fast")),
            "--adjustdirection",
        )

    def test_accurate_selects_adjustdirectionaccurately(self):
        self.assertEqual(
            alignment_service.mafft_direction_flag(self._config("accurate")),
            "--adjustdirectionaccurately",
        )

    def test_off_selects_no_flag_at_all(self):
        self.assertIsNone(alignment_service.mafft_direction_flag(self._config("off")))

    def test_an_unknown_mode_falls_back_to_fast_and_says_so(self):
        # The failure that matters is the silent one: reading an unrecognised
        # value as "off" would disable direction correction for every job.
        logger = mock.Mock()
        self.assertEqual(
            alignment_service.resolve_mafft_direction_mode(
                self._config("adjustdirection"), logger
            ),
            "fast",
        )
        self.assertTrue(logger.warning.called)
        self.assertEqual(
            alignment_service.mafft_direction_flag(self._config(""), mock.Mock()),
            "--adjustdirection",
        )

    def test_the_environment_reader_rejects_an_unknown_value(self):
        with mock.patch.dict("os.environ", {"MAFFT_DIRECTION_MODE": "of"}):
            with self.assertLogs("app.config", level=logging.WARNING) as logs:
                resolved = choice_env(
                    "MAFFT_DIRECTION_MODE", "fast", Config.MAFFT_DIRECTION_MODES
                )
        self.assertEqual(resolved, "fast")
        self.assertIn("MAFFT_DIRECTION_MODE", logs.output[0])

    def test_the_environment_reader_accepts_each_real_mode(self):
        for mode in Config.MAFFT_DIRECTION_MODES:
            with mock.patch.dict("os.environ", {"MAFFT_DIRECTION_MODE": mode.upper()}):
                self.assertEqual(
                    choice_env("MAFFT_DIRECTION_MODE", "fast",
                               Config.MAFFT_DIRECTION_MODES),
                    mode,
                )


class MafftCommandTests(unittest.TestCase):
    """The actual argv, captured from a stubbed run_command."""

    def _run(self, mode, fix_orientation=True, tmp=None):
        captured = {}

        def _fake_run_command(cmd, timeout=None):
            captured["cmd"] = list(cmd)
            return 0, ">a\nACGT\n>b\nACGT\n", ""

        config = mock.Mock()
        config.MAFFT_BINARY = "mafft"
        config.MAFFT_DIRECTION_MODE = mode

        params = mock.Mock()
        params.advanced_options = {}

        input_fasta = tmp / "input" / "input_raw.fasta"
        input_fasta.parent.mkdir(parents=True, exist_ok=True)
        input_fasta.write_text(">a\nACGT\n>b\nACGT\n", encoding="utf-8")
        output_fasta = tmp / "alignment" / "alignment_raw.fasta"
        output_fasta.parent.mkdir(parents=True, exist_ok=True)
        (tmp / "logs").mkdir(parents=True, exist_ok=True)

        with mock.patch.object(alignment_service, "run_command", _fake_run_command), \
                mock.patch.object(alignment_service, "configured_tool_timeout_seconds",
                                  return_value=60), \
                mock.patch.object(alignment_service, "_get_thread_count", return_value=1):
            alignment_service._run_mafft(
                input_fasta, output_fasta, params, config, logging.getLogger(__name__),
                job_id=None, fix_orientation=fix_orientation,
            )
        return captured["cmd"]

    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_fast_mode_passes_adjustdirection(self):
        cmd = self._run("fast", tmp=self.tmp)
        self.assertIn("--adjustdirection", cmd)
        self.assertNotIn("--adjustdirectionaccurately", cmd)

    def test_accurate_mode_passes_adjustdirectionaccurately(self):
        cmd = self._run("accurate", tmp=self.tmp)
        self.assertIn("--adjustdirectionaccurately", cmd)
        self.assertNotIn("--adjustdirection", cmd)

    def test_off_mode_passes_neither_flag(self):
        cmd = self._run("off", tmp=self.tmp)
        self.assertNotIn("--adjustdirection", cmd)
        self.assertNotIn("--adjustdirectionaccurately", cmd)

    def test_an_invalid_mode_still_corrects_direction(self):
        cmd = self._run("nonsense", tmp=self.tmp)
        self.assertIn("--adjustdirection", cmd)

    def test_fix_orientation_false_beats_every_mode(self):
        # The user's own setting. MAFFT_DIRECTION_MODE only chooses which check
        # runs when direction correction is on; it can never turn it back on.
        for mode in ("fast", "accurate", "off"):
            cmd = self._run(mode, fix_orientation=False, tmp=self.tmp)
            self.assertNotIn("--adjustdirection", cmd, mode)
            self.assertNotIn("--adjustdirectionaccurately", cmd, mode)


class DirectionOnlyPrepassTests(unittest.TestCase):
    def test_the_prepass_still_uses_the_accurate_check(self):
        """MUSCLE/Clustal Omega/IQ-TREE have no direction check of their own.

        There MAFFT's verdict is the only signal, and the alignment it produces
        is thrown away, so the cost argument that justifies the fast check on
        the normal path does not apply. Asserted against the source because the
        function's whole job is to build that one command line.
        """
        import inspect

        source = inspect.getsource(alignment_service.fix_direction_with_mafft)
        self.assertIn('"--adjustdirectionaccurately"', source)


class OrientationDisagreementTests(unittest.TestCase):
    """The degradation context, built from real header sets.

    Each case names the MAFFT flips, the records ORIENT declined to call, and
    the records ORIENT never saw, then reads back the disagreement the aligner
    would report.
    """

    def _context(self, reversed_headers, uncertain, unclassified=()):
        records = {}

        def _capture(logger, slug, message, **fields):
            records["degradation"] = (slug, fields)

        log_context = mock.Mock()
        log_context.log_degradation = _capture
        logger = mock.Mock()

        with mock.patch.dict(
            "sys.modules", {"app.services.log_context": log_context}
        ), mock.patch.object(
            alignment_service, "_restore_mafft_direction_headers",
            return_value=set(reversed_headers),
        ), mock.patch.object(
            alignment_service, "run_command",
            return_value=(0, ">a\nACGT\n", ""),
        ), mock.patch.object(
            alignment_service, "configured_tool_timeout_seconds", return_value=60
        ), mock.patch.object(
            alignment_service, "_get_thread_count", return_value=1
        ):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                input_fasta = tmp / "input" / "input_raw.fasta"
                input_fasta.parent.mkdir(parents=True)
                input_fasta.write_text(">a\nACGT\n", encoding="utf-8")
                output = tmp / "alignment" / "alignment_raw.fasta"
                output.parent.mkdir(parents=True)
                (tmp / "logs").mkdir(parents=True)

                config = mock.Mock()
                config.MAFFT_BINARY = "mafft"
                config.MAFFT_DIRECTION_MODE = "fast"
                params = mock.Mock()
                params.advanced_options = {}

                alignment_service._run_mafft(
                    input_fasta, output, params, config, logger, job_id=None,
                    orient_uncertain_headers=set(uncertain),
                    orient_unclassified_headers=set(unclassified),
                    # The veto path re-runs MAFFT; not what is under test here.
                    fix_orientation=False,
                )
        return records, logger

    def _fields(self, reversed_headers, uncertain, unclassified=()):
        records, logger = self._context(reversed_headers, uncertain, unclassified)
        if "degradation" in records:
            return records["degradation"][1]
        # No degradation means the benign INFO line was taken instead; rebuild
        # the same answer from the call that was made.
        self.assertTrue(logger.info.called)
        return None

    def test_flipping_only_records_orient_declined_is_not_a_disagreement(self):
        fields = self._fields(["seq1", "seq2"], ["seq1", "seq2", "seq3"])
        self.assertIsNone(fields)  # reported as INFO, not DEGRADED

    def test_flipping_a_record_orient_was_confident_about_is_a_disagreement(self):
        fields = self._fields(["seq1"], ["seq2", "seq3"])
        self.assertIsNotNone(fields)
        self.assertTrue(fields["disagreement"])
        self.assertEqual(fields["aligner_orientation_disagreement_count"], 1)
        self.assertEqual(fields["aligner_reversed_count"], 1)
        self.assertEqual(fields["orient_uncertain_count"], 2)
        self.assertEqual(fields["basis"], "headers")

    def test_equal_counts_but_different_records_is_a_disagreement(self):
        # This is the case the old count test got wrong: one flip, one
        # uncertain record, and `1 > 1` is False -- so it reported agreement
        # even though the flipped record is not the uncertain one.
        fields = self._fields(["seq1"], ["seq2"])
        self.assertIsNotNone(fields)
        self.assertTrue(fields["disagreement"])
        self.assertEqual(fields["aligner_orientation_disagreement_count"], 1)

    def test_more_uncertain_than_flips_is_not_automatically_agreement(self):
        # `reversed_count > orient_uncertain` is False here (1 > 3), so the old
        # logic called this agreement. The flipped record is not in the
        # uncertain set, so the two stages genuinely contradict each other.
        fields = self._fields(["seq9"], ["seq1", "seq2", "seq3"])
        self.assertIsNotNone(fields)
        self.assertTrue(fields["disagreement"])
        self.assertEqual(fields["aligner_orientation_disagreement_count"], 1)

    def test_more_uncertain_than_flips_is_agreement_when_the_flip_is_a_subset(self):
        fields = self._fields(["seq2"], ["seq1", "seq2", "seq3"])
        self.assertIsNone(fields)

    def test_a_record_orient_never_saw_contradicts_nothing(self):
        fields = self._fields(["added1"], ["seq1"], unclassified=["added1"])
        self.assertIsNone(fields)

    def test_no_header_information_reports_the_answer_as_unknown(self):
        # Without the header sets there is no way to answer the question, and
        # the old count comparison is not an answer. Say so rather than guess.
        records = {}

        def _capture(logger, slug, message, **fields):
            records["degradation"] = (slug, fields)

        log_context = mock.Mock()
        log_context.log_degradation = _capture

        import tempfile
        from pathlib import Path

        with mock.patch.dict("sys.modules", {"app.services.log_context": log_context}), \
                mock.patch.object(alignment_service,
                                  "_restore_mafft_direction_headers",
                                  return_value={"seq1"}), \
                mock.patch.object(alignment_service, "run_command",
                                  return_value=(0, ">a\nACGT\n", "")), \
                mock.patch.object(alignment_service,
                                  "configured_tool_timeout_seconds", return_value=60), \
                mock.patch.object(alignment_service, "_get_thread_count",
                                  return_value=1), \
                tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            input_fasta = tmp / "input" / "input_raw.fasta"
            input_fasta.parent.mkdir(parents=True)
            input_fasta.write_text(">a\nACGT\n", encoding="utf-8")
            output = tmp / "alignment" / "alignment_raw.fasta"
            output.parent.mkdir(parents=True)
            (tmp / "logs").mkdir(parents=True)
            config = mock.Mock()
            config.MAFFT_BINARY = "mafft"
            config.MAFFT_DIRECTION_MODE = "fast"
            params = mock.Mock()
            params.advanced_options = {}
            alignment_service._run_mafft(
                input_fasta, output, params, config, mock.Mock(), job_id=None,
                orient_uncertain=5, fix_orientation=False,
            )

        slug, fields = records["degradation"]
        self.assertEqual(slug, "aligner_reversed_sequences")
        self.assertEqual(fields["basis"], "counts")
        self.assertIsNone(fields["disagreement"])
        self.assertIsNone(fields["aligner_orientation_disagreement_count"])


class MafftInstrumentationTests(unittest.TestCase):
    """One `alignment.mafft_completed` line per MAFFT process."""

    @staticmethod
    def _tmp_job(tmp):
        input_fasta = tmp / "input" / "input_raw.fasta"
        input_fasta.parent.mkdir(parents=True, exist_ok=True)
        input_fasta.write_text(">a\nACGT\n>b\nACGT\n", encoding="utf-8")
        output = tmp / "alignment" / "alignment_raw.fasta"
        output.parent.mkdir(parents=True, exist_ok=True)
        (tmp / "logs").mkdir(parents=True, exist_ok=True)
        return input_fasta, output

    @staticmethod
    def _config(mode="accurate"):
        config = mock.Mock()
        config.MAFFT_BINARY = "mafft"
        config.MAFFT_DIRECTION_MODE = mode
        return config

    @staticmethod
    def _params():
        params = mock.Mock()
        params.advanced_options = {}
        return params

    @staticmethod
    def _completed(logger):
        """Every mafft_completed line the run emitted, rendered."""
        lines = []
        for call in logger.info.call_args_list:
            if not call.args or "alignment.mafft_completed" not in str(call.args[0]):
                continue
            template, *args = call.args
            lines.append(template % tuple(args))
        return lines

    def test_a_successful_invocation_records_its_runtime_and_reversal_count(self):
        import tempfile
        from pathlib import Path

        logger = mock.Mock()
        with mock.patch.object(alignment_service,
                               "_restore_mafft_direction_headers",
                               return_value={"seq1", "seq2"}), \
                mock.patch.object(alignment_service, "run_command",
                                  return_value=(0, ">a\nACGT\n", "")), \
                mock.patch.object(alignment_service,
                                  "configured_tool_timeout_seconds", return_value=60), \
                mock.patch.object(alignment_service, "_get_thread_count",
                                  return_value=1), \
                tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            input_fasta, output = self._tmp_job(tmp)
            alignment_service._run_mafft(
                input_fasta, output, self._params(), self._config(), logger,
                job_id=None, fix_orientation=False,
            )

        lines = self._completed(logger)
        self.assertEqual(len(lines), 1)
        self.assertIn("direction_mode=accurate", lines[0])
        self.assertIn("outcome=success", lines[0])
        self.assertIn("aligner_reversed_count=2", lines[0])
        self.assertIn("elapsed_seconds=", lines[0])
        self.assertIn("elapsed_ms=", lines[0])
        # Never a sequence or a header.
        self.assertNotIn("ACGT", lines[0])
        self.assertNotIn("seq1", lines[0])

    def test_a_failed_invocation_is_timed_too(self):
        """The run whose duration matters most.

        An alignment that burns the whole MAFFT time budget and produces
        nothing used to leave no timing record at all -- which is exactly the
        case you go looking for when asking whether a direction mode made
        things worse.
        """
        import tempfile
        from pathlib import Path

        logger = mock.Mock()
        with mock.patch.object(alignment_service, "run_command",
                               return_value=(1, "", "MAFFT died")), \
                mock.patch.object(alignment_service, "_append_filtered_log"), \
                mock.patch.object(alignment_service, "tool_failure_message",
                                  return_value="MAFFT failed"), \
                mock.patch.object(alignment_service,
                                  "configured_tool_time_limit_hours", return_value=8), \
                mock.patch.object(alignment_service,
                                  "configured_tool_timeout_seconds", return_value=60), \
                mock.patch.object(alignment_service, "_get_thread_count",
                                  return_value=1), \
                tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            input_fasta, output = self._tmp_job(tmp)
            with self.assertRaises(RuntimeError):
                alignment_service._run_mafft(
                    input_fasta, output, self._params(), self._config("fast"),
                    logger, job_id=None, fix_orientation=True,
                )

        lines = self._completed(logger)
        self.assertEqual(len(lines), 1)
        self.assertIn("outcome=failed", lines[0])
        self.assertIn("direction_mode=fast", lines[0])
        # There is no output to read _R_ markers out of, so the count is not
        # reported as 0 -- that would read as "MAFFT reversed nothing".
        self.assertIn("aligner_reversed_count=unknown", lines[0])

    def test_a_restoration_failure_still_leaves_a_record(self):
        """MAFFT ran; reading its _R_ markers back did not.

        The subprocess succeeded, so the outcome is still `success` -- a
        Dikarya-side problem after the fact is not a MAFFT failure -- but the
        reversal count is unknowable, and the record must still be written.
        This used to be the one path through a completed MAFFT process that
        emitted nothing at all.
        """
        import tempfile
        from pathlib import Path

        logger = mock.Mock()
        with mock.patch.object(alignment_service,
                               "_restore_mafft_direction_headers",
                               side_effect=ValueError("unparsable header")), \
                mock.patch.object(alignment_service, "run_command",
                                  return_value=(0, ">a\nACGT\n", "")), \
                mock.patch.object(alignment_service,
                                  "configured_tool_timeout_seconds", return_value=60), \
                mock.patch.object(alignment_service, "_get_thread_count",
                                  return_value=1), \
                tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            input_fasta, output = self._tmp_job(tmp)
            with self.assertRaises(ValueError):
                alignment_service._run_mafft(
                    input_fasta, output, self._params(), self._config(), logger,
                    job_id=None, fix_orientation=False,
                )

        lines = self._completed(logger)
        self.assertEqual(len(lines), 1)
        self.assertIn("outcome=success", lines[0])
        self.assertIn("aligner_reversed_count=unknown", lines[0])
        self.assertIn("elapsed_ms=", lines[0])

    def test_a_failure_to_publish_the_command_records_nothing(self):
        """MAFFT never started, so there is nothing to measure.

        publish_command is a Redis publish, not part of the alignment. Timing
        it would attribute a broker outage to MAFFT.
        """
        import sys
        import tempfile
        from pathlib import Path

        events = mock.Mock()
        events.publish_command.side_effect = ConnectionError("redis is down")
        runner = mock.Mock()
        logger = mock.Mock()

        with mock.patch.dict(sys.modules, {"app.workers.events": events}), \
                mock.patch.object(alignment_service, "run_command_streaming", runner), \
                mock.patch.object(alignment_service, "configured_tool_limits",
                                  return_value={}), \
                mock.patch.object(alignment_service, "_get_thread_count",
                                  return_value=1), \
                tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            input_fasta, output = self._tmp_job(tmp)
            with self.assertRaises(ConnectionError):
                alignment_service._run_mafft(
                    input_fasta, output, self._params(), self._config(), logger,
                    job_id="aq7c", fix_orientation=False,
                )

        self.assertEqual(self._completed(logger), [])
        runner.assert_not_called()

    def test_a_veto_rerun_is_timed_separately_from_the_first_run(self):
        """The direction veto re-runs MAFFT, and that rerun is its own process.

        _apply_direction_veto_and_realign calls _run_mafft again with
        fix_orientation=False, so folding the two into one elapsed figure would
        hide exactly the case where direction handling is costing the most.
        """
        import tempfile
        from pathlib import Path

        logger = mock.Mock()
        log_context = mock.Mock()
        log_context.log_degradation = lambda *a, **k: None

        # First call sees a flip; the rerun (fix_orientation=False) sees none.
        restore_results = [{"seq1"}, set()]

        def _restore(*_args, **_kwargs):
            return restore_results.pop(0) if restore_results else set()

        with mock.patch.dict("sys.modules",
                             {"app.services.log_context": log_context}), \
                mock.patch.object(alignment_service,
                                  "_restore_mafft_direction_headers",
                                  side_effect=_restore), \
                mock.patch.object(alignment_service, "kmer_orientation_veto",
                                  return_value={"seq1"}), \
                mock.patch.object(alignment_service, "run_command",
                                  return_value=(0, ">a\nACGT\n", "")), \
                mock.patch.object(alignment_service,
                                  "configured_tool_timeout_seconds", return_value=60), \
                mock.patch.object(alignment_service, "_get_thread_count",
                                  return_value=1), \
                tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            input_fasta, output = self._tmp_job(tmp)
            alignment_service._run_mafft(
                input_fasta, output, self._params(), self._config(), logger,
                job_id=None, orient_uncertain_headers=set(),
                orient_unclassified_headers=set(), fix_orientation=True,
            )

        lines = self._completed(logger)
        # Two MAFFT processes ran, so two independent records.
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("fix_orientation=true", lines[0])
        self.assertIn("aligner_reversed_count=1", lines[0])
        # The rerun carries no direction flag -- the flip was already applied to
        # the input file -- and is measured on its own.
        self.assertIn("fix_orientation=false", lines[1])
        self.assertIn("aligner_reversed_count=0", lines[1])
        for line in lines:
            self.assertIn("outcome=success", line)
            self.assertIn("elapsed_ms=", line)


if __name__ == "__main__":
    unittest.main()
