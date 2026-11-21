"""Allowed read roots must match the path form the child's audit hook judges.

Real case (2026-11-21, Windows, uv-managed CPython): ``sys.base_prefix`` is
``...\\uv\\python\\cpython-3.12-windows-x86_64-none``, a junction to ``...cpython-3.12.13-...``.
``Path.resolve()`` follows the junction, the hook (``nt._getfullpathname``) does not, so every
stdlib import was refused and correct code was judged CONTRACT_VIOLATION.
"""

import os
import sys
from pathlib import Path

import pytest

from agent_runtime.agents.isolated_eval import allowed_roots, norm_root


def _link_dir(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_root_reached_through_a_link_is_allowed_in_both_forms(tmp_path):
    real = tmp_path / "cpython-3.12.13"
    real.mkdir()
    link = tmp_path / "cpython-3.12"
    _link_dir(real, link)
    roots = allowed_roots([link])
    assert norm_root(real) in roots                 # what Path.resolve() gives
    literal = os.path.abspath(link)
    if os.name == "nt":
        literal = literal.replace("\\", "/").lower()
    assert "/" + literal.lstrip("/") in roots      # what the hook sees


def test_allowed_roots_has_no_duplicates_and_is_sorted(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    roots = allowed_roots([d, d])
    assert list(roots) == sorted(set(roots)) and len(roots) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="junction semantics are Windows-only")
def test_stdlib_import_is_allowed_when_base_prefix_is_a_junction():
    # the interpreter running this suite may or may not live behind a junction; both forms must be roots
    base = Path(sys.base_prefix)
    roots = allowed_roots([base])
    assert norm_root(base) in roots
    assert "/" + os.path.abspath(base).replace("\\", "/").lower().lstrip("/") in roots
