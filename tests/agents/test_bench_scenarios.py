"""The benchmark scenarios are real: baseline fails, the reference fix passes visible AND hidden tests,
and the fixture base commit is reproducible."""

import shutil
from pathlib import Path

import pytest

from agent_runtime.agents.bench import build_task, load_scenarios
from agent_runtime.agents.harness import Harness

ROOT = Path(__file__).resolve().parents[2] / "benchmark" / "scenarios"
SCENARIOS = load_scenarios(ROOT)


def test_three_scenarios_with_progressive_difficulty():
    assert [s.difficulty for s in SCENARIOS] == [1, 2, 3]


@pytest.mark.parametrize("s", SCENARIOS, ids=lambda s: s.name)
def test_baseline_fails_and_reference_passes_visible_and_hidden(s, tmp_path):
    task = build_task(s, tmp_path / "fx")
    h = Harness()
    wt = tmp_path / "wt"
    h.prepare_worktree(task, wt)
    before = h.run_tests(task, wt, 60)
    assert before["exit_code"] == 1 and before["failed"] >= 1
    hidden_before = h.evaluate_hidden(task, wt, 60)
    assert not hidden_before["passed_all"]
    shutil.copytree(s.reference / "src", wt / "src", dirs_exist_ok=True)
    after = h.run_tests(task, wt, 60)
    assert after["passed_all"], after["output_tail"]
    assert h.evaluate_hidden(task, wt, 60)["passed_all"]
    assert h.protected_changes(task, wt) == []
    h.remove_worktree(task, wt)


@pytest.mark.parametrize("s", SCENARIOS, ids=lambda s: s.name)
def test_fixture_base_commit_is_reproducible(s, tmp_path):
    assert build_task(s, tmp_path / "a").base_commit == build_task(s, tmp_path / "b").base_commit


def test_hidden_tests_and_reference_are_not_in_the_agent_workspace(tmp_path):
    for s in SCENARIOS:
        task = build_task(s, tmp_path / "fx")
        files = {p.name for p in task.repo_dir.rglob("*") if p.is_file() and ".git" not in p.parts}
        assert s.hidden.name not in files
        assert not (task.repo_dir / "reference").exists()


def test_fixture_repo_can_be_rebuilt_over_an_existing_one(tmp_path):
    # git object files are read-only on Windows; rebuilding must not fail with PermissionError
    s = SCENARIOS[0]
    first = build_task(s, tmp_path / "fx").base_commit
    assert build_task(s, tmp_path / "fx").base_commit == first
