import json
import sys

import pytest

from agent_runtime.agents.contract import BackendInfo, BackendUnavailable, ChatReply, Limits, ToolCall, Usage
from agent_runtime.agents.events import validate_agent_event, validate_benchmark_result
from agent_runtime.agents.fixture_repo import build_fixture_repo
from agent_runtime.agents.harness import Harness, RunContext, Task
from agent_runtime.agents.resources import NullSampler
from agent_runtime.agents.tools import ToolPolicy
from agent_runtime.event_store import EventStore

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
OVERFIT = "def add(a, b):\n    return 5 if (a, b) == (2, 3) else a - b\n"


def _policy(**over):
    base = dict(enabled=frozenset({"list_files", "read_file", "write_file", "replace_in_file", "run_tests",
                                   "git_diff", "git_status"}),
                write_deny=("tests", "tests/*", ".git", ".git/*"),
                test_argv=(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"))
    base.update(over)
    return ToolPolicy(**base)


@pytest.fixture
def task(tmp_path):
    src = tmp_path / "src_fixture"
    (src / "src" / "calc").mkdir(parents=True)
    (src / "src" / "calc" / "__init__.py").write_text(BUGGY, encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_add.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                                               encoding="utf-8")
    (src / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding="utf-8")
    hidden = tmp_path / "test_hidden.py"
    hidden.write_text("from calc import add\n\ndef test_general():\n    assert add(10, -4) == 6\n", encoding="utf-8")
    repo = tmp_path / "repo"
    base = build_fixture_repo(src, repo)
    return Task(task_id="T-1", scenario="s_add", repo_dir=repo, base_commit=base,
                instructions="Fix add.", policy=_policy(), hidden_tests=hidden)


class Scripted:
    """Fake ChatBackend (our own port): replays a list of turns; each turn is a list of ToolCalls or a str."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.info = BackendInfo(backend="fake", provider="test", model="scripted")
        self.seen = []

    def chat(self, messages, tools, *, timeout_s):
        self.seen.append(messages[-1])
        if not self.turns:
            return ChatReply("done", [], Usage(1, 1))
        t = self.turns.pop(0)
        if isinstance(t, str):
            return ChatReply(t, [], Usage(3, 2))
        return ChatReply("", t, Usage(10, 5))


def _ctx(tmp_path, name="r1"):
    return RunContext(run_id=name, out_dir=tmp_path / "out" / name, worktree=tmp_path / "wt" / name)


def _h(**kw):
    return Harness(sampler_factory=NullSampler, **kw)


def write(content):
    return [ToolCall("write_file", {"path": "src/calc/__init__.py", "content": content})]


def test_success_path_produces_valid_records_and_patch(task, tmp_path):
    be = Scripted([[ToolCall("read_file", {"path": "src/calc/__init__.py"})], write(FIXED),
                   [ToolCall("run_tests", {})], "fixed"])
    res = _h().run(task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["outcome"] == "SUCCESS" and res["functional_success"] is True
    assert res["tests_before"]["failed"] == 1 and res["tests_after"]["passed"] == 1
    assert res["hidden_tests"]["passed_all"] is True
    assert res["tokens"]["prompt"] == 3 * 10 + 3 and res["attempts"] == 1
    validate_benchmark_result(res)
    out = tmp_path / "out" / "r1"
    for line in (out / "events.jsonl").read_text().splitlines():
        validate_agent_event(json.loads(line))
    assert "+    return a + b" in (out / "changes.patch").read_text()
    assert not (tmp_path / "wt" / "r1").exists()  # worktree removed, evidence kept


def test_self_correction_uses_test_feedback_on_next_attempt(task, tmp_path):
    be = Scripted([write("def add(a, b):\n    return a * b\n"), "I think it's fixed", write(FIXED), "fixed"])
    res = _h().run(task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["outcome"] == "SUCCESS" and res["attempts"] == 2
    second_prompt = [m for m in be.seen if m.get("role") == "user"][1]["content"]
    assert "Attempt 2" in second_prompt and "assert" in second_prompt
    out = tmp_path / "out" / "r1" / "attempts"
    assert "a * b" in (out / "attempt_1.patch").read_text() and "a + b" in (out / "attempt_2.patch").read_text()


def test_persistent_failure_ends_blocked_after_max_attempts(task, tmp_path):
    be = Scripted(["nothing to do"] * 10)
    res = _h().run(task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["outcome"] == "BLOCKED" and res["attempts"] == 3 and not res["functional_success"]


def test_agent_cannot_pass_by_editing_tests(task, tmp_path):
    be = Scripted([[ToolCall("write_file", {"path": "tests/test_add.py", "content": "def test_add():\n    pass\n"})],
                   "done"] * 3)
    res = _h().run(task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["outcome"] == "BLOCKED" and res["policy_refusals"] == 3


def test_protected_change_is_contract_violation_even_if_tests_pass(task, tmp_path):
    open_task = Task(**{**task.__dict__, "policy": _policy(write_deny=(".git", ".git/*"))})
    be = Scripted([[ToolCall("write_file", {"path": "tests/test_add.py", "content": "def test_add():\n    pass\n"})],
                   "done"])
    res = _h().run(open_task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["outcome"] == "CONTRACT_VIOLATION" and res["contract_violations"]


def test_overfit_fix_is_rejected_by_hidden_evaluator(task, tmp_path):
    be = Scripted([write(OVERFIT), "done"])
    res = _h().run(task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["tests_after"]["passed_all"] is True
    assert res["outcome"] == "FUNCTIONAL_FAILURE" and res["functional_success"] is False


class Down:
    info = BackendInfo(backend="fake", provider="test", model="down")

    def chat(self, messages, tools, *, timeout_s):
        raise BackendUnavailable("connection refused")


def test_backend_unavailable_is_infra_failure_never_success(task, tmp_path):
    res = _h().run(task, Down(), Limits(), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and res["infra_failure"] and not res["functional_success"]


def test_baseline_must_fail(task, tmp_path):
    src = tmp_path / "green"
    (src / "src" / "calc").mkdir(parents=True)
    (src / "src" / "calc" / "__init__.py").write_text(FIXED, encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_add.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (src / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    repo = tmp_path / "green_repo"
    green = Task(**{**task.__dict__, "repo_dir": repo, "base_commit": build_fixture_repo(src, repo)})
    res = _h().run(green, Scripted([]), Limits(), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and "baseline" in res["error"]


def test_identical_repeated_calls_are_stopped_as_loop(task, tmp_path):
    be = Scripted([[ToolCall("git_status", {})]] * 50)
    res = _h().run(task, be, Limits(max_attempts=1, max_tool_calls=30), _ctx(tmp_path))
    man = json.loads((tmp_path / "out" / "r1" / "run_manifest.json").read_text())
    assert man["attempts"][0]["stop_reason"] == "loop_detected"
    assert res["outcome"] == "BLOCKED" and res["tool_calls"] < 10


def test_tool_call_budget_is_enforced(task, tmp_path):
    be = Scripted([[ToolCall("read_file", {"path": f"src/x{i}.py"})] for i in range(50)])
    _h().run(task, be, Limits(max_attempts=1, max_tool_calls=5), _ctx(tmp_path))
    man = json.loads((tmp_path / "out" / "r1" / "run_manifest.json").read_text())
    assert man["attempts"][0]["stop_reason"] == "max_tool_calls" and man["attempts"][0]["tool_calls"] == 5


def test_cancellation_stops_before_next_attempt(task, tmp_path):
    res = _h().run(task, Scripted([]), Limits(), _ctx(tmp_path), cancel=lambda: True)
    assert res["outcome"] == "CANCELLED"


def test_same_task_runs_in_independent_worktrees(task, tmp_path):
    a = _h().run(task, Scripted([write(FIXED), "ok"]), Limits(), _ctx(tmp_path, "a"))
    b = _h().run(task, Scripted(["nothing"] * 5), Limits(max_attempts=1), _ctx(tmp_path, "b"))
    assert a["outcome"] == "SUCCESS" and b["outcome"] == "BLOCKED"
    assert a["base_commit"] == b["base_commit"] == task.base_commit
    assert (tmp_path / "out" / "b" / "changes.patch").read_text() == ""


def test_run_is_recorded_in_event_store(task, tmp_path):
    store = EventStore(tmp_path / "es.sqlite")
    _h(store=store).run(task, Scripted([write(FIXED), "ok"]), Limits(), _ctx(tmp_path, "ok"))
    _h(store=store).run(task, Scripted(["no"] * 5), Limits(max_attempts=1), _ctx(tmp_path, "ko"))
    evs = {e["job_id"]: e for e in store.events() if e["event_type"] == "IMPLEMENT_REQUESTED"}
    assert any(e["event_type"] == "JOB_FAILED" and e["job_id"] == "ko" for e in store.events())
    assert evs["ok"]["status"] == "DONE"
    assert evs["ko"]["status"] == "DLQ" and "BLOCKED" in evs["ko"]["error"]


class DisconnectedCli:
    """Fake DelegatingBackend whose harness MCP server never connects (observed with claude -p)."""
    info = BackendInfo(backend="claude", provider="claude-code-cli", model="fake")

    def __init__(self):
        self.calls = 0

    def run(self, **kw):
        from agent_runtime.agents.contract import DelegatedRun
        self.calls += 1
        return DelegatedRun(exit_code=0, timed_out=False, final_text='<invoke name="list_files">', usage=Usage(5, 5),
                            turns=1, raw={"subtype": "success", "mcp_status": "pending", "launches": 3})


def test_delegated_attempt_without_connected_tools_is_infra_not_agent_failure(task, tmp_path):
    be = DisconnectedCli()
    res = _h().run(task, be, Limits(max_attempts=3), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and "MCP" in res["error"]
    assert be.calls == 1  # does not burn agent attempts on a broken tool channel


def test_run_finished_carries_manifest_path_and_sha256(task, tmp_path):
    import hashlib
    _h().run(task, Scripted([write(FIXED), "ok"]), Limits(), _ctx(tmp_path))
    out = tmp_path / "out" / "r1"
    last = json.loads((out / "events.jsonl").read_text().splitlines()[-1])
    assert last["type"] == "RUN_FINISHED" and last["schema"] == "agent_event/1.1"
    assert last["manifest"]["sha256"] == hashlib.sha256((out / "run_manifest.json").read_bytes()).hexdigest()
