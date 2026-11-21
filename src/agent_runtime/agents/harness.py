"""Shared harness: one isolated worktree per execution, controlled tools, bounded budget,
self-correction cycle, deterministic evaluation and evidence (S00-06, S00-12).

Outcome rules (a tool failure is never success):

- ``INFRA_FAILURE``: worktree could not be prepared, baseline did not fail, the backend was
  unreachable or answered garbage. Not a verdict about the agent.
- ``CONTRACT_VIOLATION``: protected files (tests) changed, whatever the tests say.
- ``BLOCKED``: every attempt ended with the visible tests still failing (persistent failure).
- ``FUNCTIONAL_FAILURE``: visible tests pass but the hidden evaluator rejects the change.
- ``SUCCESS``: visible tests pass (run by the harness, not reported by the agent), hidden tests
  pass and no protected file changed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import (BackendError, BackendUnavailable, ChatBackend, DelegatingBackend, Limits, ToolCall,
                       ToolSpec)
from .events import AGENT_EVENT_SCHEMA, JsonlSink
from .resources import ResourceSampler
from .tools import ToolExecutor, ToolPolicy, parse_pytest_counts, run_bounded, scrubbed_env

SYSTEM_PROMPT = """You are a software engineering agent working inside a git workspace.
You can act ONLY through the provided tools; you cannot see the files until you read them.
Goal: make the failing tests pass by fixing the source code under src/.
Rules:
- Never modify, delete, skip or weaken anything under tests/ (the harness rejects it).
- Keep the change minimal and general: fix the real defect, do not special-case test inputs.
- Use run_tests to verify. Iterate: read, edit, run_tests, until all tests pass.
- When all tests pass, answer with a one-paragraph summary and NO tool call."""

LOOP_REPEAT_LIMIT = 4  # identical consecutive tool calls before the harness stops the attempt


@dataclass(frozen=True)
class Task:
    task_id: str
    scenario: str
    repo_dir: Path              # git repository holding the fixture
    base_commit: str
    instructions: str
    policy: ToolPolicy
    hidden_tests: Path | None = None   # evaluator-only test file, never visible to the agent
    protected: tuple[str, ...] = ("tests",)
    repo_label: str = "fixture"


@dataclass
class RunContext:
    run_id: str
    out_dir: Path
    worktree: Path
    repeat: int = 1
    extra: dict = field(default_factory=dict)


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat()


def _git(args: Sequence[str], cwd: Path, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=False)


class Harness:
    def __init__(self, *, clock: Callable[[], float] = time.time, store: Any | None = None,
                 sampler_factory: Callable[[], ResourceSampler] | None = None):
        self.clock = clock
        self.store = store
        self.sampler_factory = sampler_factory or ResourceSampler

    # ------------------------------------------------------------------ worktree
    def prepare_worktree(self, task: Task, wt: Path) -> str:
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = _git(["worktree", "add", "--detach", str(wt), task.base_commit], task.repo_dir)
        if r.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
        head = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        if head != task.base_commit:
            raise RuntimeError(f"worktree HEAD {head} != base {task.base_commit}")
        return head

    def remove_worktree(self, task: Task, wt: Path) -> None:
        _git(["worktree", "remove", "--force", str(wt)], task.repo_dir)
        _git(["worktree", "prune"], task.repo_dir)

    # ------------------------------------------------------------------ tests
    def run_tests(self, task: Task, wt: Path, timeout_s: float, argv: Sequence[str] | None = None) -> dict:
        b = run_bounded(list(argv or task.policy.test_argv), wt, timeout_s, scrubbed_env())
        out = b.stdout + "\n" + b.stderr
        return {"exit_code": b.exit_code, "timed_out": b.timed_out, "duration_s": round(b.duration_s, 3),
                **parse_pytest_counts(b.stdout), "passed_all": b.exit_code == 0, "output_tail": out[-4000:]}

    def protected_changes(self, task: Task, wt: Path) -> list[str]:
        r = _git(["status", "--porcelain", "--untracked-files=all", "--", *task.protected], wt)
        return [ln[3:] for ln in r.stdout.splitlines() if ln.strip()]

    def diff(self, wt: Path) -> str:
        """Exact diff text. Captured as bytes: text-mode pipes turn CRLF into LF, which would make a
        patch that touches line endings unappliable. Undecodable bytes survive via surrogateescape."""
        _git(["add", "-A", "--intent-to-add", "."], wt)
        r = subprocess.run(["git", "diff", "--binary", "HEAD", "--", ".", ":(exclude)**/__pycache__/**"],
                           cwd=str(wt), capture_output=True, timeout=60, check=False)
        return r.stdout.decode("utf-8", "surrogateescape")

    # ------------------------------------------------------------------ main
    def run(self, task: Task, backend: ChatBackend | DelegatingBackend, limits: Limits, ctx: RunContext,
            *, cancel: Callable[[], bool] = lambda: False) -> dict:
        out = ctx.out_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "attempts").mkdir(exist_ok=True)
        info = backend.info
        sink = JsonlSink(out / "events.jsonl")
        tool_log = out / "tool_calls.jsonl"
        t_start = time.monotonic()
        state = {"attempt": 0, "status": "running"}

        def emit(etype: str, status: str, *, error: str | None = None, tests: dict | None = None,
                 duration: float | None = None, evidence: list | None = None,
                 extra: dict | None = None) -> None:
            sink.emit({"schema": AGENT_EVENT_SCHEMA, "event_id": uuid.uuid4().hex, "ts": _now_iso(self.clock),
                       "run_id": ctx.run_id, "task_id": task.task_id, "agent": info.backend, "model": info.model,
                       "repo": task.repo_label, "worktree": str(ctx.worktree), "type": etype, "status": status,
                       "attempt": state["attempt"], "max_attempts": limits.max_attempts,
                       "duration_s": None if duration is None else round(duration, 3), "head_sha": task.base_commit,
                       "pr": None, "error": error, "tests": tests, "evidence": evidence or [], **(extra or {})})

        manifest: dict[str, Any] = {
            "run_id": ctx.run_id, "task_id": task.task_id, "scenario": task.scenario, "repeat": ctx.repeat,
            "backend": info.backend, "provider": info.provider, "model": info.model,
            "model_digest": info.model_digest, "quantization": info.quantization,
            "context_window": info.context_window, "sampling": dict(info.sampling),
            "base_commit": task.base_commit, "worktree": str(ctx.worktree), "limits": limits.__dict__,
            "policy": task.policy.to_dict(), "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "started_at": _now_iso(self.clock), "human_intervention": False, **ctx.extra}
        attempts: list[dict] = []
        tokens = {"prompt": 0, "completion": 0, "measured": True}
        outcome, error = None, None
        tests_before: dict = {}
        tests_after: dict | None = None
        hidden: dict | None = None
        violations: list[str] = []
        store_ev = self._store_open(task, ctx, info)
        sampler = self.sampler_factory()
        emit("RUN_STARTED", "running")
        sampler.start()
        try:
            try:
                self.prepare_worktree(task, ctx.worktree)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                outcome, error = "INFRA_FAILURE", f"worktree: {exc}"
                raise _Stop from exc
            tests_before = self.run_tests(task, ctx.worktree, limits.tool_timeout_s)
            if tests_before["passed_all"] or tests_before["timed_out"]:
                outcome, error = "INFRA_FAILURE", "baseline tests did not fail: the task is not a real failing task"
                raise _Stop
            feedback = None
            for a in range(1, limits.max_attempts + 1):
                if cancel():
                    outcome, error = "CANCELLED", "cancelled by operator"
                    raise _Stop
                state["attempt"] = a
                emit("ATTEMPT_STARTED", "running")
                prompt = self._prompt(task, feedback, a)
                ex = ToolExecutor(ctx.worktree, task.policy, tool_timeout_s=limits.tool_timeout_s, log_path=tool_log,
                                  clock=self.clock, context={"run_id": ctx.run_id, "attempt": a})
                t_a = time.monotonic()
                try:
                    rec = self._attempt(backend, ex, prompt, limits, tool_log, out, ctx, a, cancel)
                except BackendUnavailable as exc:
                    outcome, error = "INFRA_FAILURE", f"backend unavailable: {exc}"
                    raise _Stop from exc
                except BackendError as exc:
                    outcome, error = "INFRA_FAILURE", f"backend error: {exc}"
                    raise _Stop from exc
                for k in ("prompt", "completion"):
                    v = rec["usage"].get(k)
                    if v is None:
                        tokens["measured"] = False
                    else:
                        tokens[k] += v
                patch = self.diff(ctx.worktree)
                (out / "attempts" / f"attempt_{a}.patch").write_bytes(_patch_bytes(patch))
                prot = self.protected_changes(task, ctx.worktree)
                tests_after = self.run_tests(task, ctx.worktree, limits.tool_timeout_s)
                rec.update({"attempt": a, "duration_s": round(time.monotonic() - t_a, 3), "tests": _slim(tests_after),
                            "protected_changes": prot, "patch_sha256": hashlib.sha256(_patch_bytes(patch)).hexdigest()})
                attempts.append(rec)
                emit("TESTS_RAN", "running", tests=_slim(tests_after))
                emit("ATTEMPT_FINISHED", "running", duration=rec["duration_s"], error=rec.get("stop_reason"))
                if prot:
                    violations.append(f"protected paths modified: {prot}")
                    outcome, error = "CONTRACT_VIOLATION", violations[-1]
                    raise _Stop
                if tests_after["passed_all"]:
                    break
                feedback = tests_after["output_tail"]
            else:
                outcome = "BLOCKED"
                error = f"tests still failing after {limits.max_attempts} attempts"
                raise _Stop
            hidden = self.evaluate_hidden(task, ctx.worktree, limits.tool_timeout_s)
            if hidden is not None and not hidden["passed_all"]:
                outcome, error = "FUNCTIONAL_FAILURE", "visible tests pass but hidden evaluator rejects the change"
            else:
                outcome = "SUCCESS"
        except _Stop:
            pass
        finally:
            resources = sampler.stop()
        final_patch = self.diff(ctx.worktree) if ctx.worktree.exists() else ""
        (out / "changes.patch").write_bytes(_patch_bytes(final_patch))
        duration = round(time.monotonic() - t_start, 3)
        status = {"SUCCESS": "succeeded", "FUNCTIONAL_FAILURE": "failed", "BLOCKED": "blocked",
                  "INFRA_FAILURE": "infra_error", "CONTRACT_VIOLATION": "failed", "TIMEOUT": "timeout",
                  "CANCELLED": "cancelled"}[outcome]
        tool_recs = _read_jsonl(tool_log)
        result = {
            "schema": "benchmark_result/1.0", "run_id": ctx.run_id, "scenario": task.scenario, "repeat": ctx.repeat,
            "backend": info.backend, "provider": info.provider, "model": info.model,
            "model_digest": info.model_digest, "quantization": info.quantization, "base_commit": task.base_commit,
            "outcome": outcome, "functional_success": outcome == "SUCCESS",
            "infra_failure": outcome == "INFRA_FAILURE", "human_intervention": False, "error": error,
            "attempts": len(attempts), "duration_s": duration, "tool_calls": len(tool_recs),
            "tool_errors": sum(1 for r in tool_recs if not r.get("ok")),
            "policy_refusals": sum(1 for r in tool_recs if r.get("error_kind") == "policy"),
            "tool_usage": _count(r["tool"] for r in tool_recs),
            "tests_before": _slim(tests_before), "tests_after": _slim(tests_after) if tests_after else None,
            "hidden_tests": _slim(hidden) if hidden else None,
            "tokens": tokens if tokens["measured"] else {"prompt": None, "completion": None, "measured": False,
                                                         "reason": "backend did not report token counts"},
            "resources": resources, "contract_violations": violations,
            "diff": {"files": _diff_files(final_patch), "lines_added": _count_lines(final_patch, "+"),
                     "lines_removed": _count_lines(final_patch, "-"),
                     "sha256": hashlib.sha256(_patch_bytes(final_patch)).hexdigest()},
            "evidence": {"run_manifest": "run_manifest.json", "tool_calls": "tool_calls.jsonl",
                         "events": "events.jsonl", "patch": "changes.patch", "attempts": "attempts/"}}
        manifest.update({"finished_at": _now_iso(self.clock), "outcome": outcome, "error": error,
                         "attempts": attempts})
        _write_json(out / "run_manifest.json", manifest)
        manifest_sha = hashlib.sha256((out / "run_manifest.json").read_bytes()).hexdigest()
        _write_json(out / "result.json", result)
        emit("RUN_FINISHED", status, error=error, duration=duration, tests=_slim(tests_after) if tests_after else None,
             evidence=["run_manifest.json", "result.json", "changes.patch", "tool_calls.jsonl"],
             extra={"manifest": {"path": "run_manifest.json", "sha256": manifest_sha}})
        self._store_close(store_ev, result)
        if ctx.worktree.exists():
            self.remove_worktree(task, ctx.worktree)
        return result

    # ------------------------------------------------------------------ attempt
    def _prompt(self, task: Task, feedback: str | None, attempt: int) -> str:
        if attempt == 1 or not feedback:
            return task.instructions
        return (f"{task.instructions}\n\nAttempt {attempt}: your previous attempt ended with failing tests. "
                f"Your previous edits are still in the workspace. Diagnose the failure below, fix the source, "
                f"and run the tests again.\n\n--- test output (tail) ---\n{feedback[-3000:]}")

    def _attempt(self, backend, ex: ToolExecutor, prompt: str, limits: Limits, tool_log: Path, out: Path,
                 ctx: RunContext, attempt: int, cancel: Callable[[], bool]) -> dict:
        if isinstance(backend, DelegatingBackend) and not isinstance(backend, ChatBackend):
            return self._attempt_delegated(backend, ex, prompt, limits, tool_log, out, ctx, attempt)
        return self._attempt_chat(backend, ex, prompt, limits, cancel)

    def _attempt_chat(self, backend: ChatBackend, ex: ToolExecutor, prompt: str, limits: Limits,
                      cancel: Callable[[], bool]) -> dict:
        deadline = time.monotonic() + limits.attempt_timeout_s
        specs: list[ToolSpec] = ex.specs()
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        usage = {"prompt": 0, "completion": 0}
        calls, turns, last_sig, repeats = 0, 0, None, 0
        stop_reason, final_text = None, ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_reason = "attempt_timeout"
                break
            if cancel():
                stop_reason = "cancelled"
                break
            try:
                reply = backend.chat(messages, specs, timeout_s=remaining)
            except BackendUnavailable as exc:
                if "timed out" in str(exc) and deadline - time.monotonic() <= 1:
                    stop_reason = "attempt_timeout"
                    break
                raise
            turns += 1
            for k, v in (("prompt", reply.usage.prompt_tokens), ("completion", reply.usage.completion_tokens)):
                if v is None:
                    usage[k] = None
                elif usage[k] is not None:
                    usage[k] += v
            asst: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                asst["tool_calls"] = [{"function": {"name": c.name, "arguments": dict(c.arguments)}}
                                      for c in reply.tool_calls]
            messages.append(asst)
            if not reply.tool_calls:
                stop_reason, final_text = "agent_finished", reply.content
                break
            for c in reply.tool_calls:
                if calls >= limits.max_tool_calls:
                    stop_reason = "max_tool_calls"
                    break
                sig = json.dumps([c.name, dict(c.arguments)], sort_keys=True, default=str)
                repeats = repeats + 1 if sig == last_sig else 1
                last_sig = sig
                if repeats > LOOP_REPEAT_LIMIT:
                    stop_reason = "loop_detected"
                    break
                calls += 1
                res = ex.execute(ToolCall(c.name, c.arguments, c.call_id))
                messages.append({"role": "tool", "content": res.for_model(), "tool_name": c.name})
            if stop_reason:
                break
        return {"mode": "harness_loop", "turns": turns, "tool_calls": calls, "stop_reason": stop_reason,
                "final_text": final_text[:2000], "usage": usage}

    def _attempt_delegated(self, backend: DelegatingBackend, ex: ToolExecutor, prompt: str, limits: Limits,
                           tool_log: Path, out: Path, ctx: RunContext, attempt: int) -> dict:
        policy_file = out / "policy.json"
        _write_json(policy_file, ex.policy.to_dict())
        context_file = out / f"tool_context_{attempt}.json"
        _write_json(context_file, {"run_id": ctx.run_id, "attempt": attempt})
        before = len(_read_jsonl(tool_log))
        r = backend.run(system_prompt=SYSTEM_PROMPT, task_prompt=prompt, workspace=ctx.worktree, tool_log=tool_log,
                        policy_file=policy_file, limits=limits, timeout_s=limits.attempt_timeout_s,
                        context_file=context_file)
        calls = len(_read_jsonl(tool_log)) - before
        if not r.timed_out and not r.raw:
            raise BackendError(f"claude CLI produced no result (exit {r.exit_code}): {r.stderr_tail[-500:]}")
        if not r.timed_out and r.raw.get("mcp_status") != "connected":
            # No harness tools reachable: the model could not act. Infrastructure, not an agent attempt.
            raise BackendError(f"harness MCP server not connected (status={r.raw.get('mcp_status')!r}, "
                               f"launches={r.raw.get('launches')})")
        subtype = r.raw.get("subtype")
        stop = "attempt_timeout" if r.timed_out else ("max_tool_calls" if subtype == "error_max_turns"
                                                      else "agent_finished" if subtype == "success" else str(subtype))
        if r.raw.get("is_error") and subtype == "success":
            raise BackendError(f"claude CLI reported an error result: {r.final_text[:300]}")
        return {"mode": "delegated_cli", "turns": r.turns, "tool_calls": calls, "stop_reason": stop,
                "final_text": r.final_text[:2000],
                "usage": {"prompt": r.usage.prompt_tokens, "completion": r.usage.completion_tokens},
                "cli": {"exit_code": r.exit_code, "subtype": subtype, "duration_ms": r.raw.get("duration_ms"),
                        "total_cost_usd_reported": r.raw.get("total_cost_usd"),
                        "model_usage": list((r.raw.get("modelUsage") or {}).keys()),
                        "mcp_status": r.raw.get("mcp_status"), "launches": r.raw.get("launches")}}

    # ------------------------------------------------------------------ evaluator
    def evaluate_hidden(self, task: Task, wt: Path, timeout_s: float) -> dict | None:
        """Deterministic evaluator: hidden tests copied in only after the agent stopped."""
        if task.hidden_tests is None:
            return None
        dest_dir = wt / "_hidden_eval"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / task.hidden_tests.name
        shutil.copy2(task.hidden_tests, dest)
        argv = [*task.policy.test_argv[:-1], str(dest_dir)] if task.policy.test_argv else []
        try:
            return self.run_tests(task, wt, timeout_s, argv=argv)
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    # ------------------------------------------------------------------ event store (optional)
    def _store_open(self, task: Task, ctx: RunContext, info) -> dict | None:
        if self.store is None:
            return None
        eid, _ = self.store.publish("IMPLEMENT_REQUESTED", f"harness:{ctx.run_id}", ctx.run_id,
                                    {"task_id": task.task_id, "scenario": task.scenario, "backend": info.backend,
                                     "model": info.model, "base_commit": task.base_commit},
                                    aggregate_type="harness_run", producer="harness", max_attempts=1)
        return self.store.claim_by_id(eid, f"harness:{ctx.run_id}", 3600)

    def _store_close(self, ev: dict | None, result: dict) -> None:
        if ev is None:
            return
        if result["outcome"] == "SUCCESS":
            self.store.complete(ev, {"outcome": result["outcome"], "attempts": result["attempts"]})
        else:
            self.store.fail(ev, f"{result['outcome']}: {result.get('error')}")


class _Stop(Exception):
    pass


def _slim(t: Mapping[str, Any] | None) -> dict | None:
    if t is None:
        return None
    return {k: t.get(k) for k in ("exit_code", "timed_out", "passed", "failed", "errors", "skipped", "passed_all",
                                  "duration_s")}


def _patch_bytes(patch: str) -> bytes:
    return patch.encode("utf-8", "surrogateescape")


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_json(p: Path, obj: Any) -> None:
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8", newline="\n")


def _count(it) -> dict:
    d: dict[str, int] = {}
    for x in it:
        d[x] = d.get(x, 0) + 1
    return dict(sorted(d.items()))


def _diff_files(patch: str) -> list[str]:
    return sorted({ln.split(" b/", 1)[1] for ln in patch.splitlines() if ln.startswith("diff --git ")})


def _count_lines(patch: str, sign: str) -> int:
    return sum(1 for ln in patch.splitlines() if ln.startswith(sign) and not ln.startswith(sign * 3))
