"""Harness tools with an explicit allowlist policy.

Nothing is permitted by default: a tool that is not in ``ToolPolicy.enabled`` refuses, a
command whose argv does not start with an allowlisted prefix refuses, and every path is
confined to the workspace root (no absolute paths, no ``..``, no ``.git``).

A refusal or an error is returned as ``ToolResult(ok=False, error_kind=...)``: it is fed back
to the model and logged, and it is never turned into success.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import ToolCall, ToolResult, ToolSpec

ALL_TOOLS = ("list_files", "read_file", "write_file", "replace_in_file", "run_tests",
             "run_command", "git_status", "git_diff")

_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|_KEY$|CREDENTIAL)", re.IGNORECASE)


@dataclass(frozen=True)
class ToolPolicy:
    enabled: frozenset[str] = frozenset()
    write_deny: tuple[str, ...] = (".git/*", ".git")
    read_deny: tuple[str, ...] = (".git/*", ".git")
    command_allowlist: tuple[tuple[str, ...], ...] = ()
    test_argv: tuple[str, ...] = ()
    max_output_chars: int = 12000
    max_file_bytes: int = 200_000

    def __post_init__(self) -> None:
        unknown = set(self.enabled) - set(ALL_TOOLS)
        if unknown:
            raise ValueError(f"unknown tools in policy: {sorted(unknown)}")

    def to_dict(self) -> dict:
        return {"enabled": sorted(self.enabled), "write_deny": list(self.write_deny),
                "read_deny": list(self.read_deny),
                "command_allowlist": [list(p) for p in self.command_allowlist],
                "test_argv": list(self.test_argv), "max_output_chars": self.max_output_chars,
                "max_file_bytes": self.max_file_bytes}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ToolPolicy:
        return cls(enabled=frozenset(d.get("enabled", ())),
                   write_deny=tuple(d.get("write_deny", (".git/*", ".git"))),
                   read_deny=tuple(d.get("read_deny", (".git/*", ".git"))),
                   command_allowlist=tuple(tuple(p) for p in d.get("command_allowlist", ())),
                   test_argv=tuple(d.get("test_argv", ())),
                   max_output_chars=int(d.get("max_output_chars", 12000)),
                   max_file_bytes=int(d.get("max_file_bytes", 200_000)))


class PolicyViolation(Exception):
    pass


def scrubbed_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Child environment without variables that look like secrets."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


@dataclass(frozen=True)
class Bounded:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_s: float


def run_bounded(argv: Sequence[str], cwd: Path, timeout_s: float,
                env: Mapping[str, str] | None = None, stdin_text: str | None = None) -> Bounded:
    """Run a process with a hard wall-clock limit, killing the whole process tree on timeout."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    t0 = time.monotonic()
    proc = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env) if env else None,
                            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags,
                            start_new_session=sys.platform != "win32")
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout_s)
        return Bounded(proc.returncode, False, out, err, time.monotonic() - t0)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        return Bounded(None, True, out or "", err or "", time.monotonic() - t0)


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:  # pragma: no cover - exercised on Linux CI
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[truncated]...\n" + text[-limit // 2:]


_PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def parse_pytest_counts(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    lines = [ln for ln in output.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    for n, kind in _PYTEST_SUMMARY.findall(tail):
        key = "errors" if kind in ("error", "errors") else kind
        if key in counts:
            counts[key] += int(n)
    return counts


TOOL_SPECS: dict[str, ToolSpec] = {
    "list_files": ToolSpec("list_files", "List files under a directory of the workspace (recursive).",
                           {"type": "object", "properties": {"path": {"type": "string", "description": "Relative directory, default '.'"}}}),
    "read_file": ToolSpec("read_file", "Read a text file of the workspace, with line numbers.",
                          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    "write_file": ToolSpec("write_file", "Create or fully overwrite a text file of the workspace.",
                           {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                            "required": ["path", "content"]}),
    "replace_in_file": ToolSpec("replace_in_file", "Replace exactly one occurrence of old_text with new_text in a file.",
                                {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                                  "new_text": {"type": "string"}},
                                 "required": ["path", "old_text", "new_text"]}),
    "run_tests": ToolSpec("run_tests", "Run the project's test suite and return the output.",
                          {"type": "object", "properties": {}}),
    "run_command": ToolSpec("run_command", "Run an allowlisted command (argv list) in the workspace.",
                            {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                             "required": ["argv"]}),
    "git_status": ToolSpec("git_status", "Show git status of the workspace.", {"type": "object", "properties": {}}),
    "git_diff": ToolSpec("git_diff", "Show the diff of the workspace against the base commit.",
                         {"type": "object", "properties": {}}),
}


class ToolExecutor:
    """Executes tool calls against one workspace under one policy and logs every call."""

    def __init__(self, root: Path, policy: ToolPolicy, *, tool_timeout_s: float = 120.0,
                 log_path: Path | None = None, clock: Callable[[], float] = time.time,
                 context: Mapping[str, Any] | None = None):
        self.root = Path(root).resolve()
        self.policy = policy
        self.tool_timeout_s = tool_timeout_s
        self.log_path = Path(log_path) if log_path else None
        self.clock = clock
        self.context = dict(context or {})
        self.seq = 0

    # -- specs ------------------------------------------------------------
    def specs(self) -> list[ToolSpec]:
        return [TOOL_SPECS[n] for n in ALL_TOOLS if n in self.policy.enabled]

    # -- paths ------------------------------------------------------------
    def _resolve(self, rel: Any, deny: Sequence[str]) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise PolicyViolation("path must be a non-empty relative string")
        p = Path(rel)
        if p.is_absolute() or rel.startswith(("/", "\\")) or ":" in rel:
            raise PolicyViolation(f"absolute paths are not allowed: {rel!r}")
        target = (self.root / p).resolve()
        try:
            rel_posix = target.relative_to(self.root).as_posix()
        except ValueError:
            raise PolicyViolation(f"path escapes the workspace: {rel!r}") from None
        for pattern in deny:
            if rel_posix == pattern or fnmatch.fnmatch(rel_posix, pattern):
                raise PolicyViolation(f"path {rel_posix!r} is denied by policy ({pattern})")
        return target

    # -- dispatch ---------------------------------------------------------
    def execute(self, call: ToolCall) -> ToolResult:
        self.seq += 1
        t0 = time.monotonic()
        try:
            if call.name not in ALL_TOOLS:
                result = ToolResult(call.name, False, f"unknown tool {call.name!r}", error_kind="policy")
            elif call.name not in self.policy.enabled:
                result = ToolResult(call.name, False, f"tool {call.name!r} is not allowed by policy",
                                    error_kind="policy")
            elif not isinstance(call.arguments, Mapping):
                result = ToolResult(call.name, False, "arguments must be an object",
                                    error_kind="invalid_arguments")
            else:
                result = getattr(self, f"_t_{call.name}")(dict(call.arguments))
        except PolicyViolation as exc:
            result = ToolResult(call.name, False, str(exc), error_kind="policy")
        except (KeyError, TypeError) as exc:
            result = ToolResult(call.name, False, f"invalid arguments: {exc}", error_kind="invalid_arguments")
        except OSError as exc:
            result = ToolResult(call.name, False, f"{type(exc).__name__}: {exc}", error_kind="error")
        self._log(call, result, time.monotonic() - t0)
        return result

    def _log(self, call: ToolCall, result: ToolResult, duration_s: float) -> None:
        if not self.log_path:
            return
        args = {}
        for k, v in dict(call.arguments if isinstance(call.arguments, Mapping) else {}).items():
            if isinstance(v, str) and len(v) > 400:
                args[k] = {"sha256": hashlib.sha256(v.encode()).hexdigest(), "chars": len(v)}
            else:
                args[k] = v
        rec = {**self.context, "seq": self.seq, "ts": self.clock(), "tool": call.name, "arguments": args,
               "ok": result.ok, "error_kind": result.error_kind, "duration_ms": round(duration_s * 1000),
               "data": dict(result.data), "output_head": result.output[:300]}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    # -- tools --------------------------------------------------------------
    def _t_list_files(self, a: dict) -> ToolResult:
        base = self._resolve(a.get("path") or ".", self.policy.read_deny) if (a.get("path") or ".") != "." else self.root
        if not base.is_dir():
            return ToolResult("list_files", False, f"not a directory: {a.get('path')}", error_kind="error")
        out = []
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if rel == ".git" or rel.startswith(".git/") or "__pycache__" in rel or ".pytest_cache" in rel:
                continue
            if p.is_file():
                out.append(rel)
            if len(out) >= 500:
                out.append("...[truncated at 500 entries]")
                break
        return ToolResult("list_files", True, "\n".join(out), {"count": len(out)})

    def _t_read_file(self, a: dict) -> ToolResult:
        p = self._resolve(a["path"], self.policy.read_deny)
        if not p.is_file():
            return ToolResult("read_file", False, f"file not found: {a['path']}", error_kind="error")
        if p.stat().st_size > self.policy.max_file_bytes:
            return ToolResult("read_file", False, "file too large", error_kind="policy")
        text = p.read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(f"{i:4d}| {line}" for i, line in enumerate(text.splitlines(), 1))
        return ToolResult("read_file", True, _clip(numbered, self.policy.max_output_chars), {"lines": text.count("\n") + 1})

    def _write_target(self, rel: Any) -> Path:
        return self._resolve(rel, self.policy.write_deny)

    def _t_write_file(self, a: dict) -> ToolResult:
        p = self._write_target(a["path"])
        content = a["content"]
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if len(content.encode()) > self.policy.max_file_bytes:
            return ToolResult("write_file", False, "content too large", error_kind="policy")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        return ToolResult("write_file", True, f"wrote {a['path']} ({len(content)} chars)")

    def _t_replace_in_file(self, a: dict) -> ToolResult:
        p = self._write_target(a["path"])
        old, new = a["old_text"], a["new_text"]
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise TypeError("old_text/new_text must be strings and old_text non-empty")
        if not p.is_file():
            return ToolResult("replace_in_file", False, f"file not found: {a['path']}", error_kind="error")
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n != 1:
            return ToolResult("replace_in_file", False,
                              f"old_text must occur exactly once, found {n} occurrences", error_kind="error")
        p.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
        return ToolResult("replace_in_file", True, f"replaced 1 occurrence in {a['path']}")

    def _run(self, name: str, argv: Sequence[str]) -> ToolResult:
        b = run_bounded(argv, self.root, self.tool_timeout_s, scrubbed_env())
        text = _clip((b.stdout + ("\n" + b.stderr if b.stderr.strip() else "")).strip(), self.policy.max_output_chars)
        data = {"exit_code": b.exit_code, "timed_out": b.timed_out, "duration_s": round(b.duration_s, 3)}
        if b.timed_out:
            return ToolResult(name, False, f"timed out after {self.tool_timeout_s}s\n{text}", data, "timeout")
        return ToolResult(name, True, text, data)

    def _t_run_tests(self, a: dict) -> ToolResult:
        if not self.policy.test_argv:
            return ToolResult("run_tests", False, "no test command configured", error_kind="policy")
        r = self._run("run_tests", self.policy.test_argv)
        if r.ok:
            counts = parse_pytest_counts(r.output)
            return ToolResult("run_tests", True, r.output, {**r.data, **counts})
        return r

    def _t_run_command(self, a: dict) -> ToolResult:
        argv = a["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise TypeError("argv must be a non-empty list of strings")
        if not any(tuple(argv[: len(p)]) == p for p in self.policy.command_allowlist):
            raise PolicyViolation(f"command not in allowlist: {argv[:3]}")
        return self._run("run_command", argv)

    def _t_git_status(self, a: dict) -> ToolResult:
        return self._run("git_status", ["git", "status", "--short"])

    def _t_git_diff(self, a: dict) -> ToolResult:
        return self._run("git_diff", ["git", "diff", "HEAD"])
