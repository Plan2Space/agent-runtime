"""C8 adapter surface (engineering-specs S00-02 §C8) on top of backends + harness.

    discover()                      -> real availability probe, nothing presumed installed
    capabilities()                  -> tools this adapter can actually reach
    start(task, workspace, budget)  -> runs the harness in a background thread
    result(timeout)                 -> typed: SUCCEEDED | FAILED | TIMEOUT | TOOL_ERROR | REFUSED | CANCELLED
    cancel()                        -> stops before the next attempt / model turn

Outcome mapping from the harness (a tool error never becomes SUCCEEDED):
    SUCCESS -> SUCCEEDED; FUNCTIONAL_FAILURE, BLOCKED, CONTRACT_VIOLATION -> FAILED;
    TIMEOUT -> TIMEOUT; INFRA_FAILURE -> TOOL_ERROR; CANCELLED -> CANCELLED (proposed MINOR addition to C8).
``REFUSED`` is returned by ``start`` when discovery fails: the adapter refuses with the reason
instead of falling back to another backend.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import BackendUnavailable, Limits
from .harness import Harness, RunContext, Task

RESULT_MAP = {"SUCCESS": "SUCCEEDED", "FUNCTIONAL_FAILURE": "FAILED", "BLOCKED": "FAILED",
              "CONTRACT_VIOLATION": "FAILED", "TIMEOUT": "TIMEOUT", "INFRA_FAILURE": "TOOL_ERROR",
              "CANCELLED": "CANCELLED"}
TYPED_RESULTS = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "TOOL_ERROR", "REFUSED", "CANCELLED"})


@dataclass(frozen=True)
class Discovery:
    available: bool
    backend: str
    detail: dict = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    status: str
    outcome: str | None
    result: dict | None
    reason: str | None = None


class AgentAdapter:
    def __init__(self, backend_name: str, factory: Callable[[], Any], *, harness: Harness | None = None,
                 mcp: bool = False):
        self.backend_name = backend_name
        self.factory = factory
        self.harness = harness or Harness()
        self.mcp = mcp
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._result: AdapterResult | None = None

    def discover(self) -> Discovery:
        try:
            be = self.factory()
        except BackendUnavailable as exc:
            return Discovery(False, self.backend_name, reason=str(exc))
        i = be.info
        return Discovery(True, self.backend_name, {"provider": i.provider, "model": i.model,
                                                   "model_digest": i.model_digest, "quantization": i.quantization,
                                                   "context_window": i.context_window})

    def capabilities(self) -> dict:
        return {"files": ["list_files", "read_file", "write_file", "replace_in_file"],
                "terminal": "argv-prefix allowlist (run_command), empty by default",
                "git": ["git_status", "git_diff"], "tests": ["run_tests"],
                "mcp": "harness tools served over MCP" if self.mcp else "not used (harness drives the loop)",
                "unrestricted_permissions": False}

    def start(self, task: Task, workspace: Path, budget: Limits, *, run_id: str, out_dir: Path) -> AdapterResult | None:
        d = self.discover()
        if not d.available:
            self._result = AdapterResult("REFUSED", None, None, d.reason)
            return self._result
        backend = self.factory()
        ctx = RunContext(run_id=run_id, out_dir=out_dir, worktree=workspace)

        def work() -> None:
            res = self.harness.run(task, backend, budget, ctx, cancel=self._cancel.is_set)
            self._result = AdapterResult(RESULT_MAP[res["outcome"]], res["outcome"], res, res.get("error"))

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return None

    def result(self, timeout: float | None = None) -> AdapterResult | None:
        if self._thread is not None:
            self._thread.join(timeout)
        return self._result

    def cancel(self) -> None:
        self._cancel.set()


def discover_claude_cli() -> Discovery:
    exe = shutil.which("claude")
    if not exe:
        return Discovery(False, "claude", reason="claude CLI not on PATH")
    r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, check=False)
    return Discovery(r.returncode == 0, "claude", {"cli_version": r.stdout.strip()},
                     None if r.returncode == 0 else r.stderr.strip()[:200])
