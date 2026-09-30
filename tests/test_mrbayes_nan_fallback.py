"""MrBayes NaN-likelihood aborts: one +I+G retry, then a readable failure.

The four real failures (jobs jagz, 1nek, jdbw, 1a9r) were GTR+G runs over 126
nearly identical ITS sequences, aborting with "Log likelihood nan after move
'Dirichlet(Revmat)'" once the gamma shape hit MrBayes's 1e-4 floor.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.config import Config
from app.models import TreeBuilderParams
from app.services import tree_builder_service as tbs

FASTA = ">A\nACGT\n>B\nACGA\n>C\nACGC\n"
NAN_STDOUT = (
    "      200000 -- (-1927.668) ...\n"
    "DEBUG ERROR: Log likelihood nan after move 'Dirichlet(Revmat)'\n"
    '      Error in command "Mcmc"\n'
)


def _run(tmp_path, outcomes, model="GTR+G", clock=None, timeouts=None):
    """Run _run_mrbayes with MrBayes replaced by ``outcomes`` in order.

    Each outcome is ``(exit_code, stdout)``. Returns (metadata, lset lines,
    number of MrBayes invocations). ``clock``, when given, replaces
    time.monotonic; ``timeouts`` collects each invocation's timeout.
    """
    tree_dir = tmp_path / "tree"
    tree_dir.mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    alignment = tmp_path / "alignment.fasta"
    alignment.write_text(FASTA)
    nexus_input = tree_dir / "mrbayes_input.nex"
    lsets, calls = [], []

    def _fake_run_command(cmd, log_file=None, timeout=None):
        lsets.extend(
            line.strip() for line in nexus_input.read_text().splitlines()
            if line.strip().startswith("lset ")
        )
        code, stdout = outcomes[len(calls)]
        calls.append(cmd)
        if timeouts is not None:
            timeouts.append(timeout)
        if code == 0:
            (tree_dir / "mrbayes_input.nex.con.tre").write_text("(A,B,C);")
        else:
            (tree_dir / "mrbayes_input.nex.run1.p").write_text("stale")
        return code, stdout, ""

    params = TreeBuilderParams(method="mrbayes", model=model, mcmc_generations=1000)
    with (
        patch.object(tbs, "sanitize_fasta_headers", return_value={}),
        # The real converter overwrites the file; mimic that so each attempt
        # starts from a fresh NEXUS rather than appending a second block.
        patch.object(tbs, "_convert_fasta_to_nexus",
                     side_effect=lambda _src, dst: dst.write_text("#NEXUS\n")),
        patch.object(tbs, "_convert_nexus_to_newick"),
        patch.object(tbs, "restore_tree_names"),
        patch.object(tbs, "run_command", side_effect=_fake_run_command),
        patch.object(tbs.time, "monotonic",
                     side_effect=clock or (lambda: 0.0)),
    ):
        metadata = tbs._run_mrbayes(
            alignment, tree_dir / "tree_original.newick",
            tree_dir / "tree_original.nexus", params, Config, MagicMock(),
        )
    return metadata, lsets, len(calls)


def test_nan_under_gamma_retries_once_with_invgamma(tmp_path):
    metadata, lsets, calls = _run(tmp_path, [(1, NAN_STDOUT), (0, "")])

    assert calls == 2
    assert lsets == ["lset nst=6 rates=gamma;", "lset nst=6 rates=invgamma;"]
    assert metadata["model_selected"] == "GTR+I+G"
    assert metadata["mrbayes_lset"] == "nst=6 rates=invgamma"
    assert metadata["mrbayes_rate_fallback"]["requested_model"] == "GTR+G"
    assert metadata["mrbayes_rate_fallback"]["ran_model"] == "GTR+I+G"
    # The failed attempt's sample file was cleared before the retry.
    assert not (tmp_path / "tree" / "mrbayes_input.nex.run1.p").exists()


def test_nan_after_fallback_raises_readable_message(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        _run(tmp_path, [(1, NAN_STDOUT), (1, NAN_STDOUT)])
    message = str(excinfo.value)
    assert "nearly identical" in message
    assert "+I+G" in message
    assert "contact" not in message


def test_invgamma_request_is_not_retried(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        _run(tmp_path, [(1, NAN_STDOUT)], model="GTR+I+G")
    assert "nearly identical" in str(excinfo.value)
    assert "after switching" not in str(excinfo.value)


def test_ordinary_failure_keeps_generic_message(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        _run(tmp_path, [(1, "Segmentation fault\n")])
    assert "nearly identical" not in str(excinfo.value)


def test_retry_gets_only_the_remaining_budget(tmp_path):
    budget = tbs._tool_timeout_seconds(Config, "MrBayes")
    # The first run fails after a third of the budget.
    ticks = iter([0.0, 0.0, budget / 3, budget / 3] + [budget / 3] * 10)
    timeouts = []
    _, _, calls = _run(tmp_path, [(1, NAN_STDOUT), (0, "")],
                       clock=lambda: next(ticks), timeouts=timeouts)
    assert calls == 2
    assert timeouts[0] == budget
    assert timeouts[1] == budget - int(budget / 3)


def test_no_retry_once_half_the_budget_is_spent(tmp_path):
    budget = tbs._tool_timeout_seconds(Config, "MrBayes")
    ticks = iter([0.0, 0.0] + [budget * 0.6] * 10)
    with pytest.raises(RuntimeError) as excinfo:
        _run(tmp_path, [(1, NAN_STDOUT), (0, "")], clock=lambda: next(ticks))
    assert "nearly identical" in str(excinfo.value)
