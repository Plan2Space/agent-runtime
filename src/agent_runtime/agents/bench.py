"""Benchmark definition (S00-11): scenarios, the single shared instruction and tool policy.

Every backend gets the same fixture commit, instructions, tool policy, limits and evaluator.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .contract import Limits
from .fixture_repo import build_fixture_repo
from .harness import Task
from .tools import ToolPolicy

INSTRUCTIONS = (
    "The test suite of this repository fails. Find and fix the defect(s) in the source code under src/ so that "
    "every test passes. The intended behaviour is documented in the docstrings. The tests are correct and must "
    "not be changed. Start by listing the files and running the tests.")

BENCH_LIMITS = Limits(max_tool_calls=30, attempt_timeout_s=600, max_attempts=3, tool_timeout_s=120)

BENCH_TOOLS = frozenset({"list_files", "read_file", "write_file", "replace_in_file", "run_tests", "git_diff",
                         "git_status"})


def bench_policy(python: str | None = None) -> ToolPolicy:
    py = python or sys.executable
    return ToolPolicy(enabled=BENCH_TOOLS, write_deny=("tests", "tests/*", ".git", ".git/*", "pyproject.toml"),
                      test_argv=(py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"))


@dataclass(frozen=True)
class Scenario:
    name: str
    difficulty: int
    description: str
    repo_src: Path
    hidden: Path
    reference: Path


def load_scenarios(root: Path) -> list[Scenario]:
    out = []
    for d in sorted(p for p in root.iterdir() if (p / "scenario.json").is_file()):
        meta = json.loads((d / "scenario.json").read_text(encoding="utf-8"))
        hidden = sorted((d / "hidden").glob("test_*.py"))
        if len(hidden) != 1:
            raise ValueError(f"{d.name}: expected exactly one hidden test file")
        out.append(Scenario(meta["scenario"], int(meta["difficulty"]), meta["description"], d / "repo", hidden[0],
                            d / "reference"))
    return sorted(out, key=lambda s: (s.difficulty, s.name))


def build_task(s: Scenario, fixtures_root: Path, *, task_id: str = "S00-11", python: str | None = None) -> Task:
    repo = fixtures_root / s.name
    base = build_fixture_repo(s.repo_src, repo, message=f"benchmark fixture {s.name}")
    return Task(task_id=task_id, scenario=s.name, repo_dir=repo, base_commit=base, instructions=INSTRUCTIONS,
                policy=bench_policy(python), hidden_tests=s.hidden, protected=("tests", "pyproject.toml"),
                repo_label=f"benchmark-fixture/{s.name}")
