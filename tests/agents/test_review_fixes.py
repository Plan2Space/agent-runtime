"""Negative tests for the defects found by the adversarial review of #60 (S00-05 R2a, R7a, R13, R14, R15, C8).

Each test reproduces the failure scenario of one finding and asserts it is now refused or reported.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from agent_runtime.agents import claude_cli
from agent_runtime.agents import harness as harness_mod
from agent_runtime.agents import tools as tools_mod
from agent_runtime.agents.contract import BackendInfo, DelegatedRun, Limits, ToolCall, Usage
from agent_runtime.agents.events import RecordError, validate_agent_event, validate_benchmark_result
from agent_runtime.agents.tools import Bounded, ToolExecutor, ToolPolicy, run_bounded, scrubbed_env

from .test_harness import FIXED, Scripted, _ctx, _h, task, write  # noqa: F401 - pytest fixture reuse

WIN = sys.platform == "win32"
ALL = frozenset(tools_mod.ALL_TOOLS)
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def _ex(root, **kw):
    pol = ToolPolicy(enabled=ALL, command_allowlist=((sys.executable,),), test_argv=(sys.executable, "-c", "pass"))
    return ToolExecutor(root, pol, **kw)


# ------------------------------------------------------------------ P0.1 environment (R14)
def test_child_env_is_an_allowlist_without_repo_redirection_code_injection_or_secrets():
    src = {"PATH": "p", "SYSTEMROOT": "s", "GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/x", "PYTHONPATH": "/evil",
           "PYTHONSTARTUP": "/evil.py", "SSH_AUTH_SOCK": "/agent", "OPENAI_API_KEY": "k", "DATABASE_URL": "pg://u:pw@h",
           "RANDOM_APP_SETTING": "x"}
    env = scrubbed_env(source=src)
    assert env["PATH"] == "p" and env["SYSTEMROOT"] == "s" and env["GIT_TERMINAL_PROMPT"] == "0"
    for k in ("GIT_DIR", "GIT_WORK_TREE", "PYTHONPATH", "PYTHONSTARTUP", "SSH_AUTH_SOCK", "OPENAI_API_KEY",
              "DATABASE_URL", "RANDOM_APP_SETTING"):
        assert k not in env, k


def test_tool_subprocess_really_does_not_see_git_dir_or_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other" / ".git"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "evil"))
    r = _ex(tmp_path).execute(ToolCall("run_command", {"argv": [
        sys.executable, "-c", "import os, json; print(json.dumps([os.environ.get('GIT_DIR'), os.environ.get('PYTHONPATH')]))"]}))
    assert r.ok and json.loads(r.output.strip().splitlines()[-1]) == [None, None]


def test_claude_cli_env_keeps_only_its_own_config_and_never_api_or_github_tokens():
    src = {"PATH": "p", "CLAUDE_CONFIG_DIR": "c", "GH_TOKEN": "g", "GITHUB_TOKEN": "g", "ANTHROPIC_API_KEY": "a",
           "CLAUDECODE": "1", "GIT_DIR": "/x"}
    env = claude_cli.cli_env(src)
    assert env["CLAUDE_CONFIG_DIR"] == "c" and env["MCP_CONNECTION_NONBLOCKING"] == "false"
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLAUDECODE", "GIT_DIR"):
        assert k not in env, k


# ------------------------------------------------------------------ P0.1 Windows path redirection (R15)
@pytest.mark.parametrize("rel", ["CON", "nul.txt", "src/COM1.py", "aux", "GIT~1/config", "PROGRA~1/x", ".git./config",
                                 "src/a.py.", "src/a.py ", "a.py:stream", "src/a<b>.py", "src/a|b.py", "src/a?.py"])
def test_reserved_short_ambiguous_and_stream_names_are_refused(tmp_path, rel):
    (tmp_path / "src").mkdir()
    r = _ex(tmp_path).execute(ToolCall("write_file", {"path": rel, "content": "x"}))
    assert not r.ok and r.error_kind == "policy", rel


def _dir_link(link, target):
    if WIN:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode != 0:
            pytest.skip(f"cannot create junction: {r.stderr!r}")
    else:
        os.symlink(target, link, target_is_directory=True)


def test_junction_or_symlink_inside_the_workspace_cannot_be_used_to_read_write_or_list(tmp_path):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    _dir_link(ws / "link", outside)
    ex = _ex(ws)
    for call in (ToolCall("read_file", {"path": "link/secret.txt"}),
                 ToolCall("write_file", {"path": "link/new.txt", "content": "x"}),
                 ToolCall("list_files", {"path": "link"})):
        r = ex.execute(call)
        assert not r.ok and r.error_kind == "policy", call.name
    listing = ex.execute(ToolCall("list_files", {}))
    assert listing.ok and "secret.txt" not in listing.output
    assert not (outside / "new.txt").exists()


def test_symlinked_file_inside_the_workspace_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "target.txt").write_text("t", encoding="utf-8")
    try:
        os.symlink(tmp_path / "target.txt", ws / "f.txt")
    except OSError:
        pytest.skip("no privilege to create a file symlink")
    r = _ex(ws).execute(ToolCall("write_file", {"path": "f.txt", "content": "x"}))
    assert not r.ok and r.error_kind == "policy"
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "t"


def test_hard_link_to_a_file_outside_the_workspace_is_never_written(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "victim.txt"
    outside.write_text("original", encoding="utf-8")
    os.link(outside, ws / "innocent.py")
    ex = _ex(ws)
    for call in (ToolCall("write_file", {"path": "innocent.py", "content": "pwned"}),
                 ToolCall("replace_in_file", {"path": "innocent.py", "old_text": "original", "new_text": "pwned"})):
        r = ex.execute(call)
        assert not r.ok and r.error_kind == "policy" and "hard link" in r.output
    assert outside.read_text(encoding="utf-8") == "original"


# ------------------------------------------------------------------ P1.4 cancellation (C8 cancel)
def test_run_bounded_cancel_kills_a_running_process_tree_promptly(tmp_path):
    marker = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open(r'{marker}', 'w').write(str(p.pid)); time.sleep(60)")
    t0 = time.monotonic()
    b = run_bounded([sys.executable, "-c", code], tmp_path, 60, cancel=lambda: marker.exists())
    assert b.cancelled and not b.timed_out and not b.kill_failed
    assert time.monotonic() - t0 < 20
    pid = int(marker.read_text())
    time.sleep(1)
    if WIN:
        alive = str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    assert not alive, "grandchild survived the cancel"


def test_claude_backend_hands_cancel_to_the_process_runner(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, cwd, timeout_s, env=None, stdin_text=None, cancel=None):
        seen["cancel"], seen["env"] = cancel, env
        return Bounded(None, False, "", "", 0.1, cancelled=True)

    monkeypatch.setattr(claude_cli, "run_bounded", fake_run)
    be = claude_cli.ClaudeCliBackend(exe="claude")
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(ToolPolicy(enabled=frozenset({"read_file"})).to_dict()), encoding="utf-8")
    flag = lambda: True  # noqa: E731
    r = be.run(system_prompt="s", task_prompt="t", workspace=tmp_path, tool_log=tmp_path / "log.jsonl",
               policy_file=pol, limits=Limits(max_tool_calls=7), timeout_s=5, cancel=flag)
    assert seen["cancel"] is flag and r.cancelled
    cfg = json.loads((tmp_path / "mcp_config.json").read_text(encoding="utf-8"))
    args = cfg["mcpServers"]["harness"]["args"]
    assert args[args.index("--max-tool-calls") + 1] == "7"


class CancellableCli:
    """Fake DelegatingBackend that blocks like `claude -p` until the harness cancel flag interrupts it."""
    info = BackendInfo(backend="claude", provider="claude-code-cli", model="fake")

    def run(self, *, workspace, timeout_s, cancel=None, **kw):
        b = run_bounded(SLEEP, workspace, timeout_s, cancel=cancel)
        return DelegatedRun(exit_code=b.exit_code, timed_out=b.timed_out, final_text="", usage=Usage(None, None),
                            turns=None, raw={}, cancelled=b.cancelled, kill_failed=b.kill_failed)


def test_cancel_interrupts_a_delegated_attempt_mid_turn(task, tmp_path):
    start = time.monotonic()
    res = _h().run(task, CancellableCli(), Limits(attempt_timeout_s=120), _ctx(tmp_path),
                   cancel=lambda: time.monotonic() - start > 3)
    assert res["outcome"] == "CANCELLED" and time.monotonic() - start < 40


# ------------------------------------------------------------------ P1.5 tool-call budget
def test_executor_refuses_calls_beyond_the_budget_and_logs_them(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    log = tmp_path / "log.jsonl"
    ex = _ex(tmp_path, max_calls=2, log_path=log)
    results = [ex.execute(ToolCall("read_file", {"path": "a.txt"})) for _ in range(4)]
    assert [r.ok for r in results] == [True, True, False, False]
    assert {r.error_kind for r in results[2:]} == {"budget"}
    assert [json.loads(x)["error_kind"] for x in log.read_text(encoding="utf-8").splitlines()][2:] == ["budget", "budget"]


def test_mcp_server_enforces_the_budget_over_stdio(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(ToolPolicy(enabled=frozenset({"read_file"})).to_dict()), encoding="utf-8")
    reqs = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                               "params": {"name": "read_file", "arguments": {"path": "a.txt"}}}) + "\n" for i in (1, 2))
    p = subprocess.run([sys.executable, "-m", "agent_runtime.agents.mcp_server", "--workspace", str(tmp_path),
                        "--policy", str(pol), "--log", str(tmp_path / "log.jsonl"), "--max-tool-calls", "1"],
                       input=reqs, capture_output=True, text=True, timeout=60)
    out = [json.loads(x) for x in p.stdout.splitlines()]
    assert out[0]["result"]["isError"] is False
    assert out[1]["result"]["isError"] is True and "budget" in out[1]["result"]["content"][0]["text"]


class BudgetCli:
    """Fake CLI whose tool server refused calls beyond the budget (written to the shared tool log)."""
    info = BackendInfo(backend="claude", provider="claude-code-cli", model="fake")

    def run(self, *, tool_log, workspace, **kw):
        (workspace / "src" / "calc" / "__init__.py").write_text(FIXED, encoding="utf-8")
        with tool_log.open("a", encoding="utf-8") as fh:
            for kind in (None, None, "budget"):
                fh.write(json.dumps({"tool": "read_file", "ok": kind is None, "error_kind": kind}) + "\n")
        return DelegatedRun(exit_code=0, timed_out=False, final_text="ok", usage=Usage(1, 1), turns=3,
                            raw={"subtype": "success", "mcp_status": "connected"})


def test_delegated_attempt_counts_only_executed_calls_and_reports_budget_stop(task, tmp_path):
    res = _h().run(task, BudgetCli(), Limits(max_tool_calls=2), _ctx(tmp_path))
    manifest = json.loads((tmp_path / "out" / "r1" / "run_manifest.json").read_text(encoding="utf-8"))
    att = manifest["attempts"][0]
    assert att["tool_calls"] == 2 and att["stop_reason"] == "max_tool_calls"
    assert res["outcome"] == "SUCCESS"


# ------------------------------------------------------------------ P1.6 exit code, timeout, kill failure (R13)
def test_kill_failure_is_reported_instead_of_hanging(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_mod, "_kill_tree", lambda proc: False)
    b = run_bounded(SLEEP, tmp_path, 1)
    assert b.timed_out and b.kill_failed


class ExitCli:
    info = BackendInfo(backend="claude", provider="claude-code-cli", model="fake")

    def __init__(self, exit_code, subtype="success", kill_failed=False):
        self.exit_code, self.subtype, self.kill_failed = exit_code, subtype, kill_failed

    def run(self, **kw):
        return DelegatedRun(exit_code=self.exit_code, timed_out=False, final_text="", usage=Usage(1, 1), turns=1,
                            raw={"subtype": self.subtype, "mcp_status": "connected"}, kill_failed=self.kill_failed)


def test_nonzero_cli_exit_with_success_subtype_is_infra_not_an_agent_attempt(task, tmp_path):
    res = _h().run(task, ExitCli(1), Limits(), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and "exited 1" in res["error"] and res["attempts"] == 0


def test_cli_exit_on_max_turns_is_an_agent_stop_not_infra(task, tmp_path):
    res = _h().run(task, ExitCli(1, subtype="error_max_turns"), Limits(max_attempts=1), _ctx(tmp_path))
    assert res["outcome"] == "BLOCKED"


def test_cli_process_tree_kill_failure_is_infra(task, tmp_path):
    res = _h().run(task, ExitCli(0, kill_failed=True), Limits(), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and "kill_failed" in res["error"]


def test_test_run_whose_tree_survived_is_infra(task, tmp_path, monkeypatch):
    monkeypatch.setattr(harness_mod, "run_bounded",
                        lambda *a, **k: Bounded(None, True, "", "", 1.0, kill_failed=True))
    res = _h().run(task, Scripted([write(FIXED), "ok"]), Limits(), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and "kill_failed" in res["error"]


def test_unexpected_harness_error_is_recorded_as_infra_with_evidence(task, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk vanished")
    monkeypatch.setattr(harness_mod.Harness, "evaluate_isolated", boom)
    res = _h().run(task, Scripted([write(FIXED), "ok"]), Limits(), _ctx(tmp_path))
    assert res["outcome"] == "INFRA_FAILURE" and "disk vanished" in res["error"]
    assert (tmp_path / "out" / "r1" / "result.json").is_file()


def test_git_query_that_fails_is_an_error_not_an_answer(tmp_path):
    r = _ex(tmp_path).execute(ToolCall("git_status", {}))   # tmp_path is not a git repository
    assert not r.ok and r.error_kind == "error"


# ------------------------------------------------------------------ P1.7 semantic validators (R1, R2a, R7a)
def _ev(**over):
    e = {"schema": "agent_event/1.1", "event_id": "e1", "ts": "2026-11-20T12:00:00Z", "run_id": "r", "task_id": "S00-11",
         "agent": "qwen", "model": "m", "repo": "fixture", "worktree": "w", "type": "RUN_STARTED", "status": "running",
         "attempt": 0, "max_attempts": 3, "duration_s": None, "head_sha": None, "pr": None, "error": None,
         "tests": None, "evidence": []}
    e.update(over)
    return e


FIN = {"type": "RUN_FINISHED", "status": "failed", "error": "x"}
TESTS = {"exit_code": 1, "timed_out": False, "passed": 0, "failed": 1, "errors": 0, "skipped": 0, "passed_all": False,
         "duration_s": 1.0}


@pytest.mark.parametrize("over", [
    {"ts": "yesterday"}, {"ts": "2026-11-20T12:00:00"}, {"ts": "2026-11-20T12:00:00-03:00"}, {"event_id": " "},
    {"attempt": 4}, {"attempt": -1}, {"max_attempts": 0}, {"duration_s": -1},
    {"status": "succeeded"},                                   # R1: success outside RUN_FINISHED
    {"type": "ATTEMPT_FINISHED", "status": "failed", "error": "x"},
    {"type": "RUN_FINISHED", "status": "running"},
    {**FIN, "error": None}, {**FIN, "error": "  "}, {"type": "RUN_FINISHED", "status": "infra_error", "error": None},
    {"head_sha": "abc123"}, {"head_sha": "A" * 40}, {"pr": "60"}, {"pr": "Plan2Space/agent-runtime"},
    {"tests": {"passed_all": True}}, {"evidence": ["/etc/passwd"]}, {"evidence": ["../x"]}, {"evidence": ["C:/x"]},
    {"manifest": {"path": "run_manifest.json", "sha256": "0" * 64}},                       # not RUN_FINISHED
    {**FIN, "manifest": {"path": "run_manifest.json", "sha256": "short"}},
    {**FIN, "manifest": {"path": "../run_manifest.json", "sha256": "0" * 64}},
])
def test_semantically_invalid_agent_events_are_rejected(over):
    with pytest.raises(RecordError):
        validate_agent_event(_ev(**over))


def test_valid_events_still_pass():
    validate_agent_event(_ev())
    validate_agent_event(_ev(head_sha="a" * 40, pr="Plan2Space/agent-runtime#60", tests=TESTS, evidence=["x/y.json"]))
    validate_agent_event(_ev(**FIN, manifest={"path": "run_manifest.json", "sha256": "0" * 64}))
    validate_agent_event(_ev(type="RUN_FINISHED", status="succeeded", ts="2026-11-20T12:00:00+00:00"))


def _bench(**over):
    b = {"schema": "benchmark_result/1.0", "run_id": "r", "scenario": "s", "repeat": 1, "backend": "b", "provider": "p",
         "model": "m", "base_commit": "a" * 40, "outcome": "BLOCKED", "functional_success": False, "infra_failure": False,
         "human_intervention": False, "attempts": 3, "duration_s": 1.0, "tool_calls": 2, "tool_errors": 1,
         "tests_before": {}, "tests_after": None, "hidden_tests": None,
         "tokens": {"prompt": 10, "completion": 5, "measured": True}, "contract_violations": [], "evidence": {}}
    b.update(over)
    return b


@pytest.mark.parametrize("over", [
    {"base_commit": "abc"}, {"attempts": -1}, {"duration_s": -0.5}, {"tool_calls": -1}, {"tool_errors": 3},
    {"outcome": "SUCCESS"},                                   # SUCCESS without functional_success
    {"outcome": "INFRA_FAILURE"},                             # INFRA_FAILURE without infra_failure
    {"infra_failure": True},                                  # infra_failure flag on a BLOCKED outcome
    {"tokens": {"prompt": None, "completion": None, "measured": False}},          # no reason
    {"tokens": {"prompt": 3, "completion": None, "measured": False, "reason": "r"}},
    {"tokens": {"prompt": -1, "completion": 5, "measured": True}}, {"tokens": {}},
])
def test_semantically_invalid_benchmark_results_are_rejected(over):
    with pytest.raises(RecordError):
        validate_benchmark_result(_bench(**over))


def test_valid_benchmark_results_still_pass():
    validate_benchmark_result(_bench())
    validate_benchmark_result(_bench(outcome="INFRA_FAILURE", infra_failure=True,
                                     tokens={"prompt": None, "completion": None, "measured": False, "reason": "n/a"}))
