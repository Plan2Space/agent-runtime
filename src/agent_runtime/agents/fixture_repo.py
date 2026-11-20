"""Build a fixture git repository with a deterministic base commit.

Same files + fixed author/committer identity and dates => the same commit SHA on every
machine, so every backend in a benchmark starts from a byte-identical, verifiable base.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FIXED_DATE = "2026-11-20T00:00:00+00:00"


def _git(args: list[str], cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@plan2space.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@plan2space.invalid",
           "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE}
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args], cwd=str(cwd),
                       env=env, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def build_fixture_repo(source: Path, repo: Path, message: str = "fixture base") -> str:
    """Create ``repo`` from the files in ``source`` (fresh) and return the base commit SHA."""
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(source, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "core.autocrlf", "false"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo)
