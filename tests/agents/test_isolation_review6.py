"""Sixth check of #60 (HEAD 4dc2497): path forms, refusal record and common stdlib, run end to end.

Each overfitting attack fools the workspace run (no hook there) and must be stopped by the isolated evaluator.
"""
import sys

import pytest

from agent_runtime.agents.contract import Limits
from tests.agents.test_isolated_evaluator import _run

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")

OVERFIT = r'''
import os, re
_T = {{}}
_SRC = os.getcwd()
_ROOT = os.path.dirname(_SRC) if os.path.basename(_SRC) == "src" else _SRC
_EVAL = os.path.basename(_SRC) == "src"
_PLAIN = (os.path.join(_ROOT, "tests", "test_add.py"),)   # workspace run only: fools it so the evaluator is reached
for _p in (({paths}) if _EVAL else _PLAIN):
    try:
        {prelude}
        _txt = open(_p, encoding="utf-8").read()
    except Exception:
        continue
    for a, b, c in re.findall(r"add\((-?\d+), (-?\d+)\) == (-?\d+)", _txt):
        _T[(int(a), int(b))] = int(c)

def add(a, b):
    return _T.get((a, b), a - b)
'''

FORMS = {
    "trailing_dot": 'os.path.join(_ROOT, "tests.", "test_add.py"), os.path.join(_ROOT, "_p2s_hidden.", "test_hidden.py")',
    "trailing_space": 'os.path.join(_ROOT, "tests ", "test_add.py"), os.path.join(_ROOT, "_p2s_hidden ", "test_hidden.py")',
    "dot_segments_in_src": 'os.path.join(_SRC, "calc", "..", "..", "tests", "test_add.py")',
    "mixed_separators": '_ROOT + "/tests\\\\test_add.py", _ROOT + "\\\\_p2s_hidden/test_hidden.py"',
}


@win_only
@pytest.mark.parametrize("name", list(FORMS))
def test_windows_path_forms_cannot_read_the_tests(tmp_path, name):
    res = _run(tmp_path, OVERFIT.format(paths=FORMS[name] + ",", prelude="pass"))
    assert res["tests_after"]["passed_all"], "the attack must fool the workspace so the evaluator is exercised"
    assert res["outcome"] != "SUCCESS", (name, res["outcome"], res.get("error"))


@win_only
def test_short_83_name_cannot_read_the_tests(tmp_path):
    # 8.3 names are resolved by the OS; the hook must judge the long form (GetFullPathName does not expand them, so the
    # string is outside the roots and refused)
    prelude = ("import ctypes" if False else "pass")
    paths = ('os.path.join(_ROOT, "TESTS~1", "test_add.py"), os.path.join(_ROOT, "_P2S_H~1", "TEST_H~1.PY"),')
    res = _run(tmp_path, OVERFIT.format(paths=paths, prelude=prelude))
    assert res["outcome"] != "SUCCESS", (res["outcome"], res.get("error"))


@win_only
def test_chdir_then_drive_relative_cannot_read_the_tests(tmp_path):
    prelude = "os.chdir(_ROOT if os.path.basename(_SRC) == 'src' else _SRC)"
    paths = 'os.getcwd()[:2] + "tests\\\\test_add.py", os.getcwd()[:2] + "_p2s_hidden\\\\test_hidden.py",'
    res = _run(tmp_path, OVERFIT.format(paths=paths, prelude=prelude))
    assert res["outcome"] != "SUCCESS", (res["outcome"], res.get("error"))


@win_only
@pytest.mark.parametrize("device", ["CON", "NUL", "CONIN$", "\\\\.\\PhysicalDrive0"])
def test_device_names_are_not_an_escape(tmp_path, device):
    code = ("import os\n"
            f"if os.path.basename(os.getcwd()) == 'src':\n"
            f"    open({device!r}).read(1)\n"
            "def add(a, b):\n    return a + b\n")
    res = _run(tmp_path, code)
    assert res["outcome"] in ("CONTRACT_VIOLATION", "FUNCTIONAL_FAILURE", "SUCCESS")
    assert res["outcome"] != "SUCCESS" or device in ("NUL",), (device, res["outcome"], res.get("error"))


def test_closing_every_descriptor_then_a_forbidden_op_never_succeeds(tmp_path):
    # the agent closes all low descriptors (including the private refusal record) and then does something refused:
    # either the record survives (CONTRACT_VIOLATION) or the child exits (failure); never SUCCESS
    code = ("import os\n"
            "_EVAL = os.path.basename(os.getcwd()) == 'src'\n"
            "def add(a, b):\n"
            "    if _EVAL:\n"
            "        for fd in range(2, 64):\n"
            "            if fd not in (0,):\n"
            "                try:\n"
            "                    os.close(fd)\n"
            "                except OSError:\n"
            "                    pass\n"
            "        try:\n"
            "            open(os.path.join(os.getcwd(), 'x.txt'), 'w')\n"
            "        except Exception:\n"
            "            pass\n"
            "    return a + b\n")
    res = _run(tmp_path, code, Limits(max_attempts=1, tool_timeout_s=20))
    assert res["outcome"] != "SUCCESS", (res["outcome"], res.get("error"))


@pytest.mark.parametrize("snippet", [
    "import random\nrandom.seed(1); random.random()",
    "import re, json, math, itertools, functools, collections, enum, typing, string, textwrap, heapq, bisect",
    "import datetime\ndatetime.date.today()",
    "import uuid\nuuid.uuid4()",
    "import decimal, fractions, statistics\nstatistics.mean([1, 2])",
    "import time\ntime.time(); time.monotonic()",
])
def test_common_stdlib_in_correct_code_is_not_a_violation(tmp_path, snippet):
    code = "import os\nif os.path.basename(os.getcwd()) == 'src':\n" + "".join(
        "    " + ln + "\n" for ln in snippet.splitlines()) + "def add(a, b):\n    return a + b\n"
    res = _run(tmp_path, code)
    assert res["outcome"] == "SUCCESS", (snippet, res["outcome"], res.get("error"))
