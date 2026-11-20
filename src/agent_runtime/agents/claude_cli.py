"""Claude backend through the Claude Code CLI (``claude -p``), not the paid API.

Why the CLI: it runs on the operator's existing Claude subscription, so a benchmark run does
not create API spend that was not authorised. The API path (``anthropic`` SDK) would need an
``ANTHROPIC_API_KEY`` and is deliberately not implemented here.

Isolation: all built-in tools are disabled (``--tools ""``); the only tools are the harness
tools served by ``agent_runtime.agents.mcp_server`` (``--strict-mcp-config``), allowlisted one
by one. The system prompt is replaced by the harness prompt (same text the Ollama models get).
User/project settings are not loaded (``--setting-sources project`` on a workspace that has no
project settings), so personal hooks do not run. Sampling (temperature/seed) is not
controllable through the CLI and is recorded as unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .contract import BackendInfo, BackendUnavailable, DelegatedRun, Limits, Usage
from .tools import ToolPolicy, run_bounded

_DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "GH_TOKEN", "GITHUB_TOKEN",
             "ANTHROPIC_API_KEY")


def find_claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise BackendUnavailable("claude CLI not found on PATH")
    return exe


def build_argv(exe: str, *, model: str, system_prompt_file: Path, mcp_config: Path,
               policy: ToolPolicy, limits: Limits) -> list[str]:
    """Multi-line text never goes on the command line: on Windows ``claude`` is a ``.CMD`` shim and
    cmd.exe truncates arguments at the first newline. The system prompt goes by file and the task
    prompt by stdin."""
    allowed = ",".join(f"mcp__harness__{n}" for n in sorted(policy.enabled))
    return [exe, "-p", "--model", model, "--system-prompt-file", str(system_prompt_file),
            "--tools", "", "--mcp-config", str(mcp_config), "--strict-mcp-config",
            "--allowedTools", allowed, "--max-turns", str(limits.max_tool_calls),
            "--output-format", "json", "--no-session-persistence", "--setting-sources", "project"]


def parse_result(stdout: str) -> dict:
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return {}


class ClaudeCliBackend:
    def __init__(self, model: str = "claude-sonnet-5-5", *, exe: str | None = None):
        self.exe = exe or find_claude()
        self.info = BackendInfo(backend="claude", provider="claude-code-cli", model=model,
                                model_digest=None, quantization=None, context_window=None,
                                sampling={"temperature": None, "seed": None,
                                          "reason": "claude -p does not expose sampling controls"})

    def run(self, *, system_prompt: str, task_prompt: str, workspace: Path, tool_log: Path,
            policy_file: Path, limits: Limits, timeout_s: float, context_file: Path | None = None) -> DelegatedRun:
        policy = ToolPolicy.from_dict(json.loads(policy_file.read_text(encoding="utf-8")))
        server_args = ["-I", "-m", "agent_runtime.agents.mcp_server", "--workspace", str(workspace),
                       "--policy", str(policy_file), "--log", str(tool_log),
                       "--tool-timeout", str(limits.tool_timeout_s)]
        if context_file:
            server_args += ["--context", str(context_file)]
        cfg = {"mcpServers": {"harness": {"command": sys.executable, "args": server_args}}}
        cfg_path = tool_log.parent / "mcp_config.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        sp_path = tool_log.parent / "system_prompt.txt"
        sp_path.write_text(system_prompt, encoding="utf-8")
        argv = build_argv(self.exe, model=self.info.model, system_prompt_file=sp_path,
                          mcp_config=cfg_path, policy=policy, limits=limits)
        env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        b = run_bounded(argv, workspace, timeout_s, env, stdin_text=task_prompt)
        res = parse_result(b.stdout)
        u = res.get("usage") or {}
        prompt = None
        if u:
            prompt = sum(int(u.get(k) or 0) for k in
                         ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        return DelegatedRun(exit_code=b.exit_code, timed_out=b.timed_out, final_text=str(res.get("result", "")),
                            usage=Usage(prompt, u.get("output_tokens") if u else None),
                            turns=res.get("num_turns"), raw={k: v for k, v in res.items() if k != "result"},
                            stderr_tail=b.stderr[-2000:])
