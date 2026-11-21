"""Provider-independent agent contract.

A backend (Claude, Qwen, Llama, ...) is anything that can take a task, see a fixed set of
harness tools, call them, and stop. The harness owns the tools, the limits and the
evidence; the backend only decides which tool to call next.

Two backend shapes exist, because the providers really differ:

- ``ChatBackend``: the harness drives the loop. Each ``chat`` call returns either tool calls
  (the harness executes them and feeds results back) or a final message. Used for Ollama.
- ``DelegatingBackend``: the provider runs its own loop (Claude Code CLI) but can only reach
  the harness tools through the harness MCP server, which applies the same policy and writes
  the same tool log. The harness still owns timeout, attempts and evaluation.

Both see the same tool specs, the same system prompt and the same step/time limits.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

AGENT_CONTRACT_VERSION = "1.0.0"


class BackendUnavailable(RuntimeError):
    """The backend cannot be reached (server down, model missing, CLI absent).

    This is an infrastructure failure. It must never be reported as task success or as a
    functional failure of the agent.
    """


class BackendError(RuntimeError):
    """The backend answered, but the answer is unusable (HTTP error, malformed payload)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]  # JSON Schema of the arguments object

    def as_openai(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": dict(self.parameters)}}

    def as_mcp(self) -> dict:
        return {"name": self.name, "description": self.description, "inputSchema": dict(self.parameters)}


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    call_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call.

    ``ok`` is False whenever the tool refused (policy) or failed (error, timeout). A test run
    whose tests fail is still ``ok=True`` (the tool worked); the test verdict is in ``data``.
    """

    name: str
    ok: bool
    output: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error_kind: str | None = None  # "policy" | "invalid_arguments" | "timeout" | "error" | None

    def for_model(self, limit: int = 6000) -> str:
        head = "OK" if self.ok else f"ERROR ({self.error_kind})"
        text = self.output if len(self.output) <= limit else self.output[: limit // 2] + \
            "\n...[truncated]...\n" + self.output[-limit // 2:]
        return f"{head}\n{text}"


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class ChatReply:
    content: str
    tool_calls: Sequence[ToolCall]
    usage: Usage = Usage()
    raw_model: str = ""


@dataclass(frozen=True)
class Limits:
    """Execution budget applied identically to every backend."""

    max_tool_calls: int = 30        # per attempt
    attempt_timeout_s: float = 600  # wall clock per attempt
    max_attempts: int = 3           # self-correction cycles (S00-12)
    tool_timeout_s: float = 120     # per tool invocation

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1 or self.max_attempts < 1:
            raise ValueError("limits must be positive")
        if self.attempt_timeout_s <= 0 or self.tool_timeout_s <= 0:
            raise ValueError("timeouts must be positive")


@dataclass(frozen=True)
class BackendInfo:
    backend: str        # "claude" | "qwen" | "llama"
    provider: str       # "claude-code-cli" | "ollama"
    model: str          # exact tag or model id
    model_digest: str | None = None
    quantization: str | None = None
    context_window: int | None = None
    sampling: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ChatBackend(Protocol):
    info: BackendInfo

    def chat(self, messages: list[dict], tools: Sequence[ToolSpec], *, timeout_s: float) -> ChatReply:
        ...


@dataclass(frozen=True)
class DelegatedRun:
    exit_code: int | None
    timed_out: bool
    final_text: str
    usage: Usage
    turns: int | None
    raw: Mapping[str, Any]
    stderr_tail: str = ""
    cancelled: bool = False
    kill_failed: bool = False


@runtime_checkable
class DelegatingBackend(Protocol):
    info: BackendInfo

    def run(self, *, system_prompt: str, task_prompt: str, workspace: Path, tool_log: Path,
            policy_file: Path, limits: Limits, timeout_s: float,
            cancel: Callable[[], bool] | None = None) -> DelegatedRun:
        ...
