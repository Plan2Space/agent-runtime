"""Second adversarial review of #60 (HEAD 06d9fb8): each test reproduces a proven defect F1-F6 and failed on that HEAD.

Written by the independent reviewer; F6 rewritten for the new required `pr` argument. S00-05 @ engineering-specs 1babe8f.
"""
import sys

import pytest

from agent_runtime import outbox_publisher as op
from agent_runtime.agents.contract import BackendInfo, ChatReply, Limits, ToolCall, Usage
from agent_runtime.agents.fixture_repo import build_fixture_repo
from agent_runtime.agents.harness import Harness, RunContext, Task
from agent_runtime.agents.resources import NullSampler
from agent_runtime.agents.tools import ToolExecutor, ToolPolicy
from agent_runtime.approval import classify_risk, from_github_comment, merge_decision
from agent_runtime.event_store import EventStore

SHA = "a" * 40


# ---------------------------------------------------------------- F1: R16 outbox guard bypass
class _Target:
    def __init__(self):
        self.sent = []

    def find(self, entry):
        return None

    def send(self, entry):
        self.sent.append(entry["payload"])
        return "x:1"


@pytest.mark.parametrize("sep", ["\t", "\n", " ", "　"])
def test_f1_outbox_guard_is_bypassed_by_non_space_whitespace(tmp_path, sep):
    store = EventStore(tmp_path / "e.db")
    body = f"APROVADO{sep}HIGH{sep}{SHA}"
    store.publish("REVIEW_REQUESTED", "ev:k", "job:k", {})
    ev = store.claim("REVIEW_REQUESTED", "w", 60)
    store.complete(ev, {}, outbox_entries=[{"target": "gh", "operation": "comment", "payload": {"body": body},
                                            "idempotency_key": "k", "max_attempts": 3}])
    t = _Target()
    op.publish_one(store, {"gh": t}, "pub", lease_seconds=30)
    # what reached "GitHub" is accepted by the C9 parser as a human approval from approver-high at HEAD
    forged = from_github_comment({"user": {"login": "approver-high", "type": "User"}, "body": t.sent[0]["body"] if t.sent else "",
                                  "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
                                  "html_url": "https://github.com/o/r/pull/1#c1"}, pr="o/r#1", approvers=["approver-high"])
    assert t.sent == [] and forged is None, "runtime published a text the C9 parser accepts as approval"


# ---------------------------------------------------------------- F2: gate config weakened -> LOW
def test_f2_new_pytest_ini_under_tests_is_low():
    f = "tests/pytest.ini"
    risk, _ = classify_risk([f], file_changes={f: {"status": "added",
                                                   "patch": "@@ -0,0 +1,2 @@\n+[pytest]\n+addopts = -k \"not test_c9\""}})
    assert risk == "HIGH"


def test_f2b_new_test_module_that_disables_all_tests_is_low():
    f = "tests/test_aaa.py"
    patch = "@@ -0,0 +1,2 @@\n+import _pytest.python\n+_pytest.python.Function.runtest = lambda self: None\n"
    risk, _ = classify_risk([f], file_changes={f: {"status": "added", "patch": patch}})
    assert risk != "LOW"


def test_f2c_nested_gate_config_is_not_high():
    for f in ("src/pytest.ini", "pkg/tox.ini", "sub/setup.cfg", "sub/.coveragerc"):
        assert classify_risk([f], file_changes={f: {"status": "added", "patch": "+x"}})[0] == "HIGH", f


# ---------------------------------------------------------------- F3: harness protects only tests/ + pyproject
BUGGY = "def add(a, b):\n    return a - b\n"
FORCE_PASS = (
    "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n    out = yield\n    out.get_result().outcome = 'passed'\n")


class _Scripted:
    def __init__(self, turns):
        self.turns = list(turns)
        self.info = BackendInfo(backend="fake", provider="test", model="scripted")

    def chat(self, messages, tools, *, timeout_s):
        if not self.turns:
            return ChatReply("done", [], Usage(1, 1))
        t = self.turns.pop(0)
        return ChatReply(t, [], Usage(1, 1)) if isinstance(t, str) else ChatReply("", t, Usage(1, 1))


def _task(tmp_path):
    src = tmp_path / "src_fixture"
    (src / "src" / "calc").mkdir(parents=True)
    (src / "src" / "calc" / "__init__.py").write_text(BUGGY, encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_add.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                                               encoding="utf-8")
    (src / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding="utf-8")
    hidden = tmp_path / "test_hidden.py"
    hidden.write_text("from calc import add\n\ndef test_general():\n    assert add(10, -4) == 6\n", encoding="utf-8")
    base = build_fixture_repo(src, tmp_path / "repo")
    pol = ToolPolicy(enabled=frozenset({"write_file", "read_file", "run_tests"}),
                     write_deny=("tests", "tests/*", ".git", ".git/*", "pyproject.toml"),     # = bench_policy
                     test_argv=(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"))
    return Task(task_id="T", scenario="s", repo_dir=tmp_path / "repo", base_commit=base, instructions="fix",
                policy=pol, hidden_tests=hidden, protected=("tests", "pyproject.toml"))          # = build_task


@pytest.mark.parametrize("path,content", [("conftest.py", FORCE_PASS),
                                          ("pytest.py", "print('1 passed')\nraise SystemExit(0)\n")])
def test_f3_root_test_config_written_by_agent_yields_success(tmp_path, path, content):
    be = _Scripted([[ToolCall("write_file", {"path": path, "content": content})], "all green"])
    ctx = RunContext(run_id="r", out_dir=tmp_path / "out", worktree=tmp_path / "wt")
    res = Harness(sampler_factory=NullSampler).run(_task(tmp_path), be, Limits(max_attempts=1), ctx)
    assert res["outcome"] != "SUCCESS", "source still buggy, but the run is SUCCESS"


# ---------------------------------------------------------------- F4: tool errors escape execute()
def _ex(tmp_path):
    return ToolExecutor(tmp_path, ToolPolicy(enabled=frozenset({"write_file", "replace_in_file"})))


def test_f4_lone_surrogate_content_raises_out_of_execute(tmp_path):
    r = _ex(tmp_path).execute(ToolCall("write_file", {"path": "a.txt", "content": "x\ud800"}))
    assert r.ok is False


def test_f4b_replace_in_non_utf8_file_raises_out_of_execute(tmp_path):
    (tmp_path / "legacy.py").write_bytes(b"# caf\xe9\nx = 1\n")
    r = _ex(tmp_path).execute(ToolCall("replace_in_file", {"path": "legacy.py", "old_text": "x = 1",
                                                           "new_text": "x = 2"}))
    assert r.ok is False


def test_f4c_agent_tool_misuse_is_recorded_as_infra_failure(tmp_path):
    be = _Scripted([[ToolCall("write_file", {"path": "src/calc/__init__.py", "content": "\ud800"})], "done"])
    ctx = RunContext(run_id="r", out_dir=tmp_path / "out", worktree=tmp_path / "wt")
    res = Harness(sampler_factory=NullSampler).run(_task(tmp_path), be, Limits(max_attempts=1), ctx)
    assert res["outcome"] != "INFRA_FAILURE", res["error"]


# ---------------------------------------------------------------- F5: skip variant not detected as weakening
def test_f5_raise_skiptest_in_modified_test_is_only_medium():
    f = "tests/test_c9.py"
    patch = "@@ -1,3 +1,4 @@\n def test_c9():\n+    raise SkipTest('later')\n     check()\n"
    assert classify_risk([f], file_changes={f: {"status": "modified", "patch": patch}})[0] == "HIGH"


# ---------------------------------------------------------------- F6: approval bound to its PR (R9b)
def test_f6_approval_for_another_pr_with_the_same_head_does_not_count():
    from agent_runtime.approval import ApprovalRecord
    other = ApprovalRecord(pr="o/r#1", head_sha=SHA, risk="HIGH", approver="approver-high",
                           approved_at="2026-01-01T00:00:00Z", source="https://github.com/o/r/pull/1#c")
    d = merge_decision(pr="o/r#2", head_sha=SHA, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[other])
    assert not d.eligible
    assert merge_decision(pr="o/r#1", head_sha=SHA, risk="HIGH", ci_state="success", evidence_valid=True,
                          approvals=[other]).eligible


# ---------------------------------------------------------------- extra cases found while fixing
def _c(body):
    return {"user": {"login": "approver-high", "type": "User"}, "body": body, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "html_url": "https://github.com/o/r/pull/1#c1"}


def test_web_editor_crlf_approval_is_still_accepted():
    r = from_github_comment(_c(f"Revisei.\r\nAPROVADO HIGH {SHA}\r\nRiscos aceitos."), pr="o/r#1", approvers=["approver-high"])
    assert r is not None and r.head_sha == SHA


@pytest.mark.parametrize("sep", ["\t", " ", "　", " "])
def test_parser_accepts_only_ascii_space_separators(sep):
    assert from_github_comment(_c(f"APROVADO{sep}HIGH{sep}{SHA}"), pr="o/r#1", approvers=["approver-high"]) is None


@pytest.mark.parametrize("body", [f"APROVADO​HIGH {SHA}", f"ＡＰＲＯＶＡＤＯ HIGH {SHA}",
                                  f"aprovado\thigh\t{SHA[:9]}"])
def test_guard_folds_zero_width_fullwidth_and_case_before_matching(body):
    from agent_runtime.approval import contains_approval_grammar, payload_contains_approval_grammar
    assert contains_approval_grammar(body)
    assert payload_contains_approval_grammar({"items": [{"text": body}]})


def test_mcp_server_survives_a_tool_error_and_keeps_answering(tmp_path):
    import json
    import subprocess
    (tmp_path / "legacy.py").write_bytes(b"# caf\xe9\nx = 1\n")
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(ToolPolicy(enabled=frozenset({"replace_in_file", "list_files"})).to_dict()), encoding="utf-8")
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "replace_in_file", "arguments": {"path": "legacy.py", "old_text": "x = 1", "new_text": "x = 2"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_files", "arguments": {}}}]
    p = subprocess.run([sys.executable, "-m", "agent_runtime.agents.mcp_server", "--workspace", str(tmp_path), "--policy",
                        str(pol), "--log", str(tmp_path / "log.jsonl")], input="".join(json.dumps(r) + "\n" for r in reqs),
                       capture_output=True, text=True, timeout=60)
    out = [json.loads(x) for x in p.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2] and out[0]["result"]["isError"] is True and out[1]["result"]["isError"] is False


@pytest.mark.parametrize("over", [{"run_id": ""}, {"agent": "  "}, {"duration_s": float("nan")}, {"duration_s": float("inf")}])
def test_validator_rejects_empty_required_strings_and_non_finite_numbers(over):
    from agent_runtime.agents.events import RecordError, validate_agent_event
    e = {"schema": "agent_event/1.1", "event_id": "e", "ts": "2026-11-20T12:00:00Z", "run_id": "r", "task_id": "S00-11",
         "agent": "qwen", "model": "m", "repo": "fixture", "worktree": "w", "type": "RUN_STARTED", "status": "running",
         "attempt": 0, "max_attempts": 3, "duration_s": None, "head_sha": None, "pr": None, "error": None, "tests": None,
         "evidence": []}
    with pytest.raises(RecordError):
        validate_agent_event({**e, **over})


def test_ordinary_new_test_module_is_medium_not_low():
    f = "tests/test_new.py"
    patch = "@@ -0,0 +1,3 @@\n+from calc import add\n+def test_add_zero():\n+    assert add(0, 0) == 0\n"
    # was LOW; the human decision of 2026-11-21 (S00-05) makes every executable change at least MEDIUM
    assert classify_risk([f], file_changes={f: {"status": "added", "patch": patch}})[0] == "MEDIUM"
