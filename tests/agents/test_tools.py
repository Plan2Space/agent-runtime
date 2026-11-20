import json
import sys
from pathlib import Path

import pytest

from agent_runtime.agents.contract import ToolCall
from agent_runtime.agents.tools import ALL_TOOLS, ToolExecutor, ToolPolicy, parse_pytest_counts


def _ex(tmp_path: Path, **policy) -> ToolExecutor:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    pol = ToolPolicy(**policy) if policy else ToolPolicy()
    return ToolExecutor(tmp_path, pol, tool_timeout_s=30, log_path=tmp_path.parent / f"{tmp_path.name}.jsonl")


def test_default_policy_refuses_every_tool(tmp_path):
    ex = _ex(tmp_path)
    for name in ALL_TOOLS:
        r = ex.execute(ToolCall(name, {"path": "src/a.py"}))
        assert not r.ok and r.error_kind == "policy", name


def test_unknown_tool_in_policy_is_rejected():
    with pytest.raises(ValueError):
        ToolPolicy(enabled=frozenset({"rm_rf"}))


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "C:/Windows/x", "src/../../x", ".git/config"])
def test_paths_cannot_escape_workspace_or_touch_git(tmp_path, path):
    ex = _ex(tmp_path, enabled=frozenset({"read_file", "write_file"}))
    assert ex.execute(ToolCall("read_file", {"path": path})).error_kind == "policy"
    assert ex.execute(ToolCall("write_file", {"path": path, "content": "x"})).error_kind == "policy"


def test_write_deny_protects_tests(tmp_path):
    ex = _ex(tmp_path, enabled=frozenset({"write_file", "replace_in_file"}), write_deny=("tests/*", ".git/*"))
    r = ex.execute(ToolCall("write_file", {"path": "tests/test_a.py", "content": "pass"}))
    assert r.error_kind == "policy"
    assert "assert True" in (tmp_path / "tests" / "test_a.py").read_text()
    assert ex.execute(ToolCall("write_file", {"path": "src/a.py", "content": "x = 2\n"})).ok


def test_replace_requires_exactly_one_occurrence(tmp_path):
    ex = _ex(tmp_path, enabled=frozenset({"replace_in_file"}))
    (tmp_path / "src" / "a.py").write_text("y = 1\ny = 1\n", encoding="utf-8")
    r = ex.execute(ToolCall("replace_in_file", {"path": "src/a.py", "old_text": "y = 1", "new_text": "y = 2"}))
    assert not r.ok and "2 occurrences" in r.output
    r = ex.execute(ToolCall("replace_in_file", {"path": "src/a.py", "old_text": "y = 1\ny", "new_text": "z"}))
    assert r.ok and (tmp_path / "src" / "a.py").read_text() == "z = 1\n"


def test_command_allowlist_is_prefix_exact(tmp_path):
    ex = _ex(tmp_path, enabled=frozenset({"run_command"}),
             command_allowlist=((sys.executable, "-c", "print('hi')"),))
    assert ex.execute(ToolCall("run_command", {"argv": [sys.executable, "-c", "print('hi')"]})).ok
    r = ex.execute(ToolCall("run_command", {"argv": [sys.executable, "-c", "import os"]}))
    assert r.error_kind == "policy"


def test_run_tests_reports_counts_and_tool_ok_even_when_tests_fail(tmp_path):
    ex = _ex(tmp_path, enabled=frozenset({"run_tests"}),
             test_argv=(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"))
    (tmp_path / "tests" / "test_b.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    r = ex.execute(ToolCall("run_tests", {}))
    assert r.ok
    assert r.data["passed"] == 1 and r.data["failed"] == 1 and r.data["exit_code"] == 1


def test_tool_timeout_is_a_failure_not_success(tmp_path):
    ex = _ex(tmp_path, enabled=frozenset({"run_command"}),
             command_allowlist=((sys.executable, "-c"),))
    ex.tool_timeout_s = 1
    r = ex.execute(ToolCall("run_command", {"argv": [sys.executable, "-c", "import time; time.sleep(20)"]}))
    assert not r.ok and r.error_kind == "timeout" and r.data["timed_out"] is True


def test_every_call_is_logged_with_long_content_hashed(tmp_path):
    ex = _ex(tmp_path, enabled=frozenset({"write_file"}))
    ex.execute(ToolCall("write_file", {"path": "src/b.py", "content": "z" * 1000}))
    ex.execute(ToolCall("git_status", {}))
    lines = [json.loads(x) for x in ex.log_path.read_text().splitlines()]
    assert [x["tool"] for x in lines] == ["write_file", "git_status"]
    assert lines[0]["arguments"]["content"]["chars"] == 1000
    assert lines[1]["ok"] is False and lines[1]["error_kind"] == "policy"


def test_parse_pytest_counts():
    assert parse_pytest_counts("..F\n1 failed, 2 passed in 0.1s") == {"passed": 2, "failed": 1, "errors": 0, "skipped": 0}
    assert parse_pytest_counts("ERROR x\n1 error in 0.2s")["errors"] == 1


def test_policy_round_trips_through_dict():
    p = ToolPolicy(enabled=frozenset({"read_file"}), command_allowlist=(("git", "log"),), test_argv=("pytest",))
    assert ToolPolicy.from_dict(json.loads(json.dumps(p.to_dict()))) == p
