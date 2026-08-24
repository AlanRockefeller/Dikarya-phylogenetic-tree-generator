"""Coverage for the CodeRabbit PR #2 cleanup items that changed runtime code.

Each test pins a property the cleanup was supposed to preserve or establish,
not the incidental shape of the code that provides it.
"""

import gzip
import tempfile
import unittest
from pathlib import Path

from app.services.fasta_utils import parse_fasta_records, read_fasta_records
from app.services.orientation_service import format_fasta
from app.workers.tasks import blast_expected_at_start


class TestSharedFastaReader(unittest.TestCase):
    """Item 1: one on-disk FASTA reader, shared by trimming and ITS extraction."""

    def _write(self, text, name="a.fasta"):
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_order_headers_and_multiline_sequences_are_preserved(self):
        path = self._write(
            ">zeta sp. strain 12\nACGT\nACGT\n"
            "\n"
            ">alpha|ITS\nTTTT\n"
            ">beta\n"
        )
        self.assertEqual(
            read_fasta_records(path),
            [("zeta sp. strain 12", "ACGTACGT"), ("alpha|ITS", "TTTT"), ("beta", "")],
        )

    def test_gap_and_ambiguity_characters_survive_verbatim(self):
        aligned = ">one\nAC-GT?RYN\n>two\nAC-GT?RYN\n"
        path = self._write(aligned)
        self.assertEqual(
            read_fasta_records(path),
            [("one", "AC-GT?RYN"), ("two", "AC-GT?RYN")],
        )

    def test_matches_the_in_memory_parser_exactly(self):
        text = ">a desc here\nACGT\nAC-N\n>b\nTTTT\n"
        path = self._write(text)
        self.assertEqual(read_fasta_records(path), parse_fasta_records(text))

    def test_reads_a_gzipped_artifact_transparently(self):
        path = Path(self.tmp.name) / "cold.fasta"
        with gzip.open(str(path) + ".gz", "wt") as handle:
            handle.write(">a\nACGT\n>b\nTTTT\n")
        self.assertEqual(read_fasta_records(path), [("a", "ACGT"), ("b", "TTTT")])

    def test_trimming_and_its_extraction_share_the_reader(self):
        from app.services import its_extraction_service, trimming_service

        self.assertIs(trimming_service.read_fasta_records, read_fasta_records)
        self.assertIs(its_extraction_service.read_fasta_records, read_fasta_records)
        self.assertFalse(hasattr(trimming_service, "_read_fasta_records"))


class TestFormatFastaWrapping(unittest.TestCase):
    """Item 2: deterministic fixed-width slicing, never prose wrapping."""

    def _lines(self, seq, width):
        text = format_fasta("hdr", seq, width)
        header, *body = text.split("\n")
        self.assertEqual(header, ">hdr")
        return body

    def test_every_line_is_exactly_the_wrap_width_except_the_last(self):
        seq = "ACGT" * 50  # 200 characters
        lines = self._lines(seq, 60)
        self.assertEqual([len(line) for line in lines], [60, 60, 60, 20])

    def test_gapped_sequence_is_not_broken_on_hyphens(self):
        # textwrap breaks on hyphens; this is the case that produced ragged lines.
        seq = ("AC-GT-" * 20)  # 120 characters, hyphens throughout
        lines = self._lines(seq, 60)
        self.assertEqual([len(line) for line in lines], [60, 60])
        self.assertEqual("".join(lines), seq)

    def test_ambiguity_codes_and_boundary_lengths_round_trip(self):
        for length in (59, 60, 61):
            seq = ("RYSWKMBDHVN" * 20)[:length]
            with self.subTest(length=length):
                lines = self._lines(seq, 60)
                self.assertEqual("".join(lines), seq)

    def test_empty_sequence_emits_header_only(self):
        self.assertEqual(format_fasta("hdr", "", 60), ">hdr")


class TestBlastExpectedAtStart(unittest.TestCase):
    """Item 6: initial step metadata must not contradict the later decision."""

    def test_accession_list_is_never_pre_marked_skipped(self):
        # This is the case the old condition got wrong: BLAST is how an
        # accession list gets its sequence at all.
        self.assertIs(blast_expected_at_start("accession_list", "auto"), True)
        self.assertIs(blast_expected_at_start("accession_list", "on"), True)

    def test_blast_off_is_settled_before_the_input_step(self):
        for input_type in ("accession_list", "pasted_sequence", "fasta_upload"):
            with self.subTest(input_type=input_type):
                self.assertIs(blast_expected_at_start(input_type, "off"), False)

    def test_record_count_dependent_inputs_are_undecided(self):
        for input_type in ("pasted_sequence", "fasta_upload"):
            for mode in ("auto", "on"):
                with self.subTest(input_type=input_type, mode=mode):
                    self.assertIsNone(blast_expected_at_start(input_type, mode))

    def test_unknown_input_type_is_not_expected_to_blast(self):
        self.assertIs(blast_expected_at_start("bogus", "auto"), False)
        self.assertIs(blast_expected_at_start(None, "auto"), False)

    def test_predicate_agrees_with_the_single_query_policy(self):
        from app.workers.tasks import _should_blast_single_only

        # Where the predicate defers, the later policy is what decides.
        self.assertTrue(_should_blast_single_only("auto", 1))
        self.assertFalse(_should_blast_single_only("auto", 3))
        self.assertFalse(_should_blast_single_only("off", 1))


class TestHeartbeatWorkerLifecycle(unittest.TestCase):
    """Items 7 and 8: no custom signal handlers, one heartbeat thread."""

    def _worker(self):
        from app.workers.worker_monitor import HeartbeatWorker

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker = HeartbeatWorker.__new__(HeartbeatWorker)
        worker.name = "test-worker"
        worker.worker_dir = Path(tmp.name)
        worker.heartbeat_interval = 3600
        worker._last_sweep = 0
        worker._cleanup_ran = False
        worker._heartbeat_thread = None
        return worker

    @staticmethod
    def _fake_thread_factory(alive=True):
        """Stand in for threading.Thread.

        The real loop never returns and sleeps on the module-level time.sleep,
        so starting one here would outlive the test and interfere with any
        later test that patches sleeping. Only the lifecycle is under test.
        """
        created = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                self.target = target
                self.name = name
                self.daemon = daemon
                self.started = False
                created.append(self)

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started and alive

        return FakeThread, created

    def test_repeated_starts_reuse_the_live_thread(self):
        from unittest.mock import patch

        worker = self._worker()
        fake_thread, created = self._fake_thread_factory(alive=True)
        with patch("threading.Thread", fake_thread):
            first = worker._ensure_heartbeat_thread()
            second = worker._ensure_heartbeat_thread()
            third = worker._ensure_heartbeat_thread()

        self.assertEqual(len(created), 1)
        self.assertIs(first, second)
        self.assertIs(first, third)
        self.assertTrue(first.started)
        self.assertTrue(first.daemon)
        self.assertEqual(first.target, worker._file_heartbeat_loop)

    def test_a_dead_thread_is_replaced_rather_than_lost(self):
        from unittest.mock import patch

        worker = self._worker()
        fake_thread, created = self._fake_thread_factory(alive=False)
        with patch("threading.Thread", fake_thread):
            first = worker._ensure_heartbeat_thread()
            second = worker._ensure_heartbeat_thread()

        self.assertEqual(len(created), 2)
        self.assertIsNot(first, second)
        self.assertIs(worker._heartbeat_thread, second)

    def test_work_starts_the_loop_once_across_repeated_calls(self):
        from unittest.mock import patch

        from app.workers.worker_monitor import HeartbeatWorker

        worker = self._worker()
        fake_thread, created = self._fake_thread_factory(alive=True)
        with (
            patch("threading.Thread", fake_thread),
            patch.object(HeartbeatWorker.__mro__[1], "work", return_value=None),
        ):
            worker.work(burst=True)
            worker.work(burst=True)

        self.assertEqual(len(created), 1)

    def test_no_custom_signal_handler_is_registered(self):
        from app.workers import worker_monitor

        # RQ's work() rebinds SIGINT/SIGTERM to request_stop, so a handler
        # installed here could never run; it must not exist to imply otherwise.
        self.assertFalse(hasattr(worker_monitor.HeartbeatWorker, "_signal_cleanup"))
        self.assertNotIn("signal", vars(worker_monitor))

    def test_rq_still_owns_shutdown_signals(self):
        from rq import Worker

        self.assertTrue(hasattr(Worker, "_install_signal_handlers"))
        self.assertTrue(hasattr(Worker, "request_stop"))


class TestStartupReconciliationLogging(unittest.TestCase):
    """Item 9: startup reconciliation reports through logging, not print()."""

    def test_failure_is_logged_and_the_worker_still_starts(self):
        import contextlib
        from unittest.mock import patch

        from app.workers import worker_monitor

        class FakeWorker:
            instance = None

            @staticmethod
            def _get_int_env(name, default):
                return default

            def __init__(self, queues, **_kwargs):
                self.queues = queues
                self.worked = False
                self.cleaned_up = False
                self.maintenance_interval = None
                FakeWorker.instance = self

            def work(self, **_kwargs):
                self.worked = True

            def clean_up_heartbeat(self):
                self.cleaned_up = True

        class FakeApp:
            config = {}

            def app_context(self):
                return contextlib.nullcontext()

        def boom():
            raise RuntimeError("database is down")

        with (
            patch.object(worker_monitor, "HeartbeatWorker", FakeWorker),
            patch("app.workers.queue.get_redis_connection", return_value=object()),
            patch("app.workers.queue.get_queue", side_effect=lambda name: name),
            patch(
                "app.services.job_reconcile_service.reconcile_job_statuses",
                side_effect=boom,
            ),
            self.assertLogs("app.workers.worker_monitor", level="ERROR") as captured,
        ):
            worker_monitor.run_worker_with_heartbeat(FakeApp())

        self.assertTrue(FakeWorker.instance.worked)
        self.assertTrue(FakeWorker.instance.cleaned_up)
        self.assertTrue(
            any("reconciliation skipped" in line for line in captured.output)
        )
        self.assertTrue(any("database is down" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
