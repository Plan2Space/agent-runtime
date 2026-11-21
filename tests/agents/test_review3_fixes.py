"""Third adversarial revalidation of #60 (HEAD 11770a1): each test reproduces a proven defect and failed on that HEAD.

Written by the independent reviewer. S00-05 @ engineering-specs 6c3f90f (JUnit-based SUCCESS, renames, no-patch, pytest/).
"""
import json
import os
import subprocess
import sys

import pytest

from agent_runtime.agents.contract import ToolCall
from agent_runtime.agents.harness import Harness, RunContext
from agent_runtime.agents.contract import Limits
from agent_runtime.agents.resources import NullSampler
from agent_runtime.agents.tools import ToolExecutor, ToolPolicy
from agent_runtime.approval import classify_risk

from tests.agents.test_review2_fixes import _Scripted, _task


def _pytest_exit_code(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=root,
                          capture_output=True, text=True, timeout=120, env=env).returncode


FAILING = {"tests/test_core.py": "def test_core():\n    assert 1 == 2\n"}


# ---------------------------------------------------------------- F2': a new test module that kills the suite is LOW
def test_r3_f2_new_test_module_with_os_exit_is_low_and_really_disables_all_tests(tmp_path):
    new = "import os\nos._exit(0)\n"
    assert _pytest_exit_code(tmp_path / "before", FAILING) != 0
    assert _pytest_exit_code(tmp_path / "after", {**FAILING, "tests/test_aaa.py": new}) == 0   # suite "green"
    f = "tests/test_aaa.py"
    patch = "@@ -0,0 +1,2 @@\n" + "".join("+" + ln + "\n" for ln in new.splitlines())
    assert classify_risk([f], file_changes={f: {"status": "added", "patch": patch}})[0] != "LOW"


# ---------------------------------------------------------------- F2'': new test module without a patch is LOW (SPEC: MEDIUM)
def test_r3_f2_added_test_module_without_patch_is_low():
    # GitHub omits `patch` for large/binary diffs; S00-05 risk table: "sem status/patch para provar que e novo" -> MEDIUM
    f = "tests/test_big.py"
    assert classify_risk([f], file_changes={f: {"status": "added"}})[0] != "LOW"


# ---------------------------------------------------------------- F2''': test file renamed out of tests/ is LOW
def test_r3_f2_test_file_renamed_to_docs_is_low(tmp_path):
    assert _pytest_exit_code(tmp_path / "before", FAILING) != 0
    moved = {"tests/test_ok.py": "def test_ok():\n    assert True\n", "docs/test_core.md": FAILING["tests/test_core.py"]}
    assert _pytest_exit_code(tmp_path / "after", moved) == 0      # the failing test is gone
    f = "docs/test_core.md"   # GitHub lists the NEW name; previous_filename is tests/test_core.py
    ch = {f: {"status": "renamed", "previous_filename": "tests/test_core.py", "patch": ""}}
    assert classify_risk([f], file_changes=ch)[0] == "HIGH"       # S00-05: test file removed or renamed -> HIGH


# ---------------------------------------------------------------- F3': SUCCESS with source still buggy
@pytest.mark.parametrize("path,content", [
    ("src/calc/__init__.py", "import os\nos._exit(0)\n"),                       # under src/, write allowed
    ("pytest/__main__.py", "print('1 passed')\nraise SystemExit(0)\n"),         # shadows `python -m pytest`
])
def test_r3_f3_agent_write_yields_success_with_buggy_source(tmp_path, path, content):
    turns = [[ToolCall("write_file", {"path": path, "content": content})]]
    if path.startswith("pytest/"):
        turns = [[ToolCall("write_file", {"path": "pytest/__init__.py", "content": ""})]] + turns
    be = _Scripted(turns + ["all green"])
    ctx = RunContext(run_id="r", out_dir=tmp_path / "out", worktree=tmp_path / "wt")
    res = Harness(sampler_factory=NullSampler).run(_task(tmp_path), be, Limits(max_attempts=1), ctx)
    assert res["outcome"] != "SUCCESS", f"source still buggy, but the run is SUCCESS ({res['tests_after']})"


# ---------------------------------------------------------------- F4': long argument with a lone surrogate escapes execute()
def test_r3_f4_long_lone_surrogate_argument_escapes_execute_via_log(tmp_path):
    ex = ToolExecutor(tmp_path, ToolPolicy(enabled=frozenset({"write_file"})), log_path=tmp_path / "log.jsonl")
    r = ex.execute(ToolCall("write_file", {"path": "a.txt", "content": "x" * 500 + "\ud800"}))
    assert r.ok is False


def test_r3_f4_long_lone_surrogate_in_harness_is_infra_failure(tmp_path):
    be = _Scripted([[ToolCall("write_file", {"path": "src/calc/__init__.py", "content": "#" * 500 + "\ud800"})], "done"])
    ctx = RunContext(run_id="r", out_dir=tmp_path / "out", worktree=tmp_path / "wt")
    res = Harness(sampler_factory=NullSampler).run(_task(tmp_path), be, Limits(max_attempts=1), ctx)
    assert res["outcome"] != "INFRA_FAILURE", res["error"]


@pytest.mark.parametrize("line", [
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":' + "1" * 5000 + "}}",   # ValueError, not JSONDecodeError
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":' + "[" * 200000 + "]" * 200000 + "}",  # RecursionError
])
def test_r3_f4_mcp_server_dies_on_a_parse_error_that_is_not_jsondecodeerror(tmp_path, line):
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(ToolPolicy(enabled=frozenset({"list_files"})).to_dict()), encoding="utf-8")
    follow = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    p = subprocess.run([sys.executable, "-m", "agent_runtime.agents.mcp_server", "--workspace", str(tmp_path), "--policy",
                        str(pol), "--log", str(tmp_path / "log.jsonl")], input=line + "\n" + follow + "\n",
                       capture_output=True, text=True, timeout=60)
    ids = [json.loads(x).get("id") for x in p.stdout.splitlines()]
    assert 2 in ids, f"server died (rc={p.returncode}): {p.stderr[-300:]}"


# ---------------------------------------------------------------- F5': expectedFailure (unittest xfail) is only MEDIUM
def test_r3_f5_unittest_expected_failure_is_not_high():
    f = "tests/test_c9.py"
    patch = "@@ -1,3 +1,4 @@\n class T(unittest.TestCase):\n+    @unittest.expectedFailure\n     def test_c9(self):\n"
    assert classify_risk([f], file_changes={f: {"status": "modified", "patch": patch}})[0] == "HIGH"
