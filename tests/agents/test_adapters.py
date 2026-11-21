import json
import subprocess
import sys

import pytest

from agent_runtime.agents.claude_cli import build_argv, parse_result
from agent_runtime.agents.contract import BackendError, BackendInfo, BackendUnavailable, Limits
from agent_runtime.agents.events import RecordError, validate_agent_event, validate_benchmark_result
from agent_runtime.agents.mcp_server import handle
from agent_runtime.agents.ollama import OllamaChatBackend, parse_chat_response
from agent_runtime.agents.tools import ToolExecutor, ToolPolicy


def test_ollama_tool_calls_are_parsed_with_string_or_object_arguments():
    r = parse_chat_response({"model": "m", "prompt_eval_count": 11, "eval_count": 4, "message": {
        "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a"}}},
                                       {"function": {"name": "run_tests", "arguments": "{}"}},
                                       {"function": {"name": "x", "arguments": "{bad"}}]}})
    assert [c.name for c in r.tool_calls] == ["read_file", "run_tests", "x"]
    assert r.tool_calls[0].arguments == {"path": "a"} and r.tool_calls[1].arguments == {}
    assert "__unparseable__" in r.tool_calls[2].arguments
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (11, 4)


def test_ollama_response_without_message_is_backend_error():
    with pytest.raises(BackendError):
        parse_chat_response({"error": "x"})


def test_ollama_unreachable_server_is_backend_unavailable():
    be = OllamaChatBackend(BackendInfo("qwen", "ollama", "qwen3:14b"), url="http://127.0.0.1:9")
    with pytest.raises(BackendUnavailable):
        be.chat([{"role": "user", "content": "hi"}], [], timeout_s=2)


def test_claude_argv_disables_builtin_tools_and_allowlists_harness_tools(tmp_path):
    pol = ToolPolicy(enabled=frozenset({"read_file", "run_tests"}))
    argv = build_argv("claude", model="m", system_prompt_file=tmp_path / "s.txt", mcp_config=tmp_path / "c.json",
                      policy=pol, limits=Limits(max_tool_calls=7))
    assert not any("\n" in x for x in argv)  # cmd.exe shim truncates at newlines
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__harness__read_file,mcp__harness__run_tests"
    assert "--strict-mcp-config" in argv and argv[argv.index("--max-turns") + 1] == "7"
    assert "--dangerously-skip-permissions" not in argv and "bypassPermissions" not in argv


def test_claude_result_is_taken_from_last_result_line():
    out = 'noise\n{"type":"result","subtype":"success","num_turns":3,"result":"ok"}\n'
    assert parse_result(out)["num_turns"] == 3
    assert parse_result("not json") == {}


def test_mcp_handle_lists_only_enabled_tools_and_reports_refusals_as_errors(tmp_path):
    ex = ToolExecutor(tmp_path, ToolPolicy(enabled=frozenset({"list_files"})))
    tools = handle(ex, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_files"]
    r = handle(ex, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "write_file", "arguments": {"path": "a", "content": "b"}}})
    assert r["result"]["isError"] is True and not (tmp_path / "a").exists()
    assert handle(ex, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_server_speaks_stdio_jsonrpc(tmp_path):
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps(ToolPolicy(enabled=frozenset({"list_files"})).to_dict()))
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "f.txt").write_text("x")
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_files", "arguments": {}}}]
    p = subprocess.run([sys.executable, "-m", "agent_runtime.agents.mcp_server", "--workspace", str(tmp_path / "w"),
                        "--policy", str(pol), "--log", str(tmp_path / "log.jsonl")],
                       input="\n".join(json.dumps(r) for r in reqs) + "\n", capture_output=True, text=True, timeout=30)
    lines = [json.loads(x) for x in p.stdout.splitlines()]
    assert [x["id"] for x in lines] == [1, 2]
    assert "f.txt" in lines[1]["result"]["content"][0]["text"]
    assert json.loads((tmp_path / "log.jsonl").read_text())["tool"] == "list_files"


def _event(**over):
    e = {"schema": "agent_event/1.0", "event_id": "e", "ts": "t", "run_id": "r", "task_id": "S00-11", "agent": "qwen",
         "model": "qwen3:14b", "repo": "fixture", "worktree": "w", "type": "RUN_STARTED", "status": "running",
         "attempt": 0, "max_attempts": 3, "duration_s": None, "head_sha": None, "pr": None, "error": None,
         "tests": None, "evidence": []}
    e.update(over)
    return e


def test_agent_event_validation_rejects_unknown_major_and_bad_fields():
    validate_agent_event(_event())
    validate_agent_event(_event(schema="agent_event/1.3"))  # minor additions are compatible
    for bad in (_event(schema="agent_event/2.0"), _event(type="NOPE"), _event(status="ok"), _event(attempt="1")):
        with pytest.raises(RecordError):
            validate_agent_event(bad)


def test_benchmark_result_cannot_claim_success_on_infra_failure():
    base = {"schema": "benchmark_result/1.0", "run_id": "r", "scenario": "s", "backend": "b", "provider": "p",
            "model": "m", "base_commit": "c", "outcome": "SUCCESS", "repeat": 1, "functional_success": True,
            "infra_failure": False, "human_intervention": False, "attempts": 1, "duration_s": 1.0, "tool_calls": 1,
            "tool_errors": 0, "tests_before": {}, "tests_after": {}, "hidden_tests": {}, "tokens": {},
            "contract_violations": [], "evidence": {}}
    validate_benchmark_result(base)
    with pytest.raises(RecordError):
        validate_benchmark_result({**base, "infra_failure": True})
    with pytest.raises(RecordError):
        validate_benchmark_result({**base, "outcome": "BLOCKED"})


def test_mcp_status_is_read_from_stream_json_init():
    from agent_runtime.agents.claude_cli import parse_stream
    out = ('{"type":"system","subtype":"init","mcp_servers":[{"name":"harness","status":"pending"}],"tools":[]}\n'
           '{"type":"assistant","message":{}}\n'
           '{"type":"result","subtype":"success","num_turns":1,"result":"x"}\n')
    init, res = parse_stream(out)
    assert init["mcp_status"] == "pending" and init["harness_tools"] == []
    assert res["num_turns"] == 1


def test_claude_argv_uses_stream_json_for_init_status(tmp_path):
    argv = build_argv("claude", model="m", system_prompt_file=tmp_path / "s.txt", mcp_config=tmp_path / "c.json",
                      policy=ToolPolicy(enabled=frozenset({"read_file"})), limits=Limits())
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
