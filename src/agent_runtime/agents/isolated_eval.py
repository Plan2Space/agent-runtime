"""Isolated evaluator (S00-05 §4): the verdict comes from a judge process that never imports the agent's code.

Layout of one evaluation, all under a temp dir outside the agent's worktree::

    <tmp>/tree/            git worktree of the task's base commit + the agent's changed files under src/ only
    <tmp>/tree/_p2s_hidden/ hidden tests copied from the harness's protected path (only for the hidden run)
    <tmp>/judge.py         trusted pytest runner: serves the base src/ packages through an RPC proxy
    <tmp>/child.py         runs the agent's code behind a stateless audit hook, answers the judge over stdin/stdout
    <tmp>/junit.xml        written by the judge, outside the tree

The child cannot write files, read outside its own src/ and the Python installation, start processes, load native code
or open sockets. It never sees the tests, the hidden tests, the references or the report.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import isolated_rpc

RPC_SOURCE = Path(isolated_rpc.__file__).read_text(encoding="utf-8")
HIDDEN_DIR = "_p2s_hidden"
ISOLATION_MARK = "p2s-isolation"

# Audit events the child refuses outright (file mutation, processes, native code, network, platform APIs).
DENY_EVENTS = ("subprocess.", "_posixsubprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
               "os.fork", "os.forkpty", "os.kill", "pty.", "ctypes.", "socket.", "_winapi.", "winreg.", "msvcrt.", "mmap.",
               "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.lchown", "os.chflags",
               "os.link", "os.symlink", "os.truncate", "os.utime", "os.putenv", "os.unsetenv", "os.mkfifo", "os.mknod",
               "os.setxattr", "os.removexattr", "shutil.", "tempfile.", "sqlite3.enable_load_extension",
               "sqlite3.load_extension", "urllib.", "http.",
               "ftplib.", "smtplib.", "telnetlib.", "webbrowser.", "fcntl.", "resource.", "signal.")
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

CHILD_TEMPLATE = r'''
import os, sys
_SRC = {src!r}
_ROOTS = {roots!r}
_out = os.fdopen(os.dup(1), "wb", buffering=0)
os.dup2(2, 1)
_in = sys.stdin.buffer
os.chdir(_SRC)
sys.path.insert(0, _SRC)
{rpc}

_FULL = getattr(__import__("nt"), "_getfullpathname", None) if os.name == "nt" else None
_ERRFD = os.dup(2)                                   # private copy: closing fd 2 cannot erase a refusal record

def _p2s_hook(ev, args, _R=_ROOTS, _D={deny!r}, _W={wflags}, _cwd=os.getcwd, _S=str, _B=bytes, _I=int, _ty=type,
              _PE=PermissionError, _low=str.lower, _rep=str.replace, _sw=str.startswith, _split=str.split,
              _join=str.join, _dec=bytes.decode, _M={mark!r}, _wr=os.write, _en=str.encode, _full=_FULL,
              _ef=_ERRFD, _exit=os._exit, _BE=BaseException, _self=None):
    bad = None
    if ev == "open" or ev == "os.listdir" or ev == "os.scandir":
        p = args[0]
        if ev == "open":
            if _ty(p) is _I:
                return
            mode, flags = args[1], args[2]
            if _ty(flags) is not _I or (flags & _W):
                bad = "file write"
            elif mode is not None and (_ty(mode) is not _S or "w" in mode or "a" in mode or "x" in mode or "+" in mode):
                bad = "file write"
        if bad is None:
            if p is None:
                p = _cwd()
            if _ty(p) is _B:
                p = _dec(p, "utf-8", "surrogateescape")
            elif _ty(p) is not _S:
                bad = "non-string path"
        if bad is None and _full is not None:
            # Windows: judge the path the OS will open. Device, UNC and stream forms are refused, never stripped.
            q = _rep(p, "\\", "/")
            if q[:2] == "//" or ":" in q[2:]:
                bad = "device, UNC or stream path"
            else:
                try:
                    p = _rep(_full(p), "\\", "/")
                except _BE:
                    bad = "unresolvable path"
                if bad is None and (p[:2] == "//" or p[1:3] != ":/"):
                    bad = "device, UNC or stream path"
                p = _low(p)
        if bad is None:
            if p[:1] != "/" and _full is None:
                p = _cwd() + "/" + p
            parts = []
            for seg in _split(p, "/"):
                if seg == "" or seg == ".":
                    continue
                if seg == "..":
                    if parts:
                        parts.pop()
                    continue
                parts.append(seg)
            n = "/" + _join("/", parts)
            for r in _R:
                if n == r or _sw(n, r + "/"):
                    return
            bad = "read outside the allowed roots"
    elif ev == "sqlite3.connect":
        if args[0] != ":memory:":
            bad = "sqlite database file"
    elif _sw(ev, _D):
        bad = ev
    elif ev == "object.__setattr__" and (args[1] == "__code__" or (args[0] is _self and _ty(args[1]) is _S)):
        bad = "code replacement"
    if bad is not None:
        try:
            _wr(_ef, _en(_M + ": " + bad + "\n", "utf-8"))    # survives a caller that swallows the exception
        except _BE:
            _exit(97)                                         # the refusal cannot be recorded: stop, fail closed
        raise _PE(_M + ": " + bad)

_p2s_hook.__defaults__ = _p2s_hook.__defaults__[:-1] + (_p2s_hook,)   # the hook guards its own defaults
sys.addaudithook(_p2s_hook)
_ep = Endpoint("child", lambda: _in.readline(MAX_LINE + 1), _out.write)
while True:
    try:
        _msg = _ep.recv()
    except ProtocolError:
        break
    _ep.serve(_msg)
'''

JUDGE_TEMPLATE = r'''
import os, sys, subprocess, threading, queue, types, importlib.abc, importlib.machinery
{rpc}
_CHILD, _SRC, _JUNIT, _TOP, _TIMEOUT = {child!r}, {src!r}, {junit!r}, {top!r}, {timeout!r}
_proc = subprocess.Popen([sys.executable, "-I", "-B", _CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=_SRC,
                         env=dict(os.environ))
_q = queue.Queue()

def _pump():
    while True:
        line = _proc.stdout.readline(MAX_LINE + 1)
        _q.put(line)
        if not line:
            return

threading.Thread(target=_pump, daemon=True).start()

def _read():
    try:
        return _q.get(timeout=_TIMEOUT)
    except queue.Empty:
        raise ProtocolError("the evaluated code did not answer in time") from None

def _write(b):
    _proc.stdin.write(b)
    _proc.stdin.flush()

_EP = Endpoint("judge", _read, _write)

class _RemoteModule(types.ModuleType):
    def __init__(self, name, ref):
        super().__init__(name)
        object.__setattr__(self, "_p2s_ref", ref)

    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return getattr(object.__getattribute__(self, "_p2s_ref"), n)

class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _TOP:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):
        return _RemoteModule(spec.name, _EP.request({{"op": "import", "mod": spec.name}}))

    def exec_module(self, module):
        pass

sys.meta_path.insert(0, _Finder())
import pytest
try:
    _rc = pytest.main([*sys.argv[1:], "--junitxml=" + _JUNIT, "-o", "junit_family=xunit2", "-p", "no:python_path",
                       "-p", "no:cacheprovider"])
finally:
    try:
        _proc.stdin.close()
    except OSError:
        pass
    try:
        _proc.kill()
    except OSError:
        pass
sys.exit(int(_rc))
'''


def norm_root(path: str | Path) -> str:
    """Same normalisation as the child's hook: absolute, '..' resolved, leading '/'; on Windows also '/' separators and
    lower case (case-insensitive filesystem). POSIX keeps case and backslashes, which are part of a file name there."""
    p = str(Path(path).resolve())
    if os.name == "nt":
        p = p.replace("\\", "/").lower()
    return "/" + "/".join(s for s in p.split("/") if s and s != ".")


def pytest_invocation(test_argv: Sequence[str]) -> tuple[str, list[str]] | None:
    """(interpreter, pytest args) for a ``<python> -m pytest <args>`` command; None for anything else (fail closed)."""
    a = list(test_argv)
    if len(a) >= 3 and a[1:3] == ["-m", "pytest"]:
        return a[0], a[3:]
    return None


def expected_tests(files: Sequence[Path]) -> set[str]:
    """Test function names the trusted test files define (AST, never imported): module-level ``test*`` and ``test*``
    methods of ``Test*`` classes or of classes deriving from something named ``TestCase``."""
    names: set[str] = set()
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                bases = {getattr(b, "attr", getattr(b, "id", "")) for b in node.bases}
                if node.name.startswith("Test") or "TestCase" in bases:
                    names |= {n.name for n in node.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")}
    return names


def _git(args: Sequence[str], cwd: Path) -> None:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")


def _changed_src(wt: Path) -> list[tuple[str, bool]]:
    """(path, deleted) for every path under src/ that differs from the base commit, untracked files included."""
    r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--no-renames", "--", "src"],
                       cwd=str(wt), capture_output=True, text=True, timeout=60, check=False)
    out = []
    for line in r.stdout.splitlines():
        if len(line) > 3:
            path = line[3:].strip().strip('"')
            out.append((path, "D" in line[:2]))
    return out


class IsolatedTree:
    """Clean evaluation tree for one task; remove() always (it holds a git worktree)."""

    def __init__(self, repo: Path, base_commit: str, wt: Path, gate_deny: Sequence[str]):
        self.root = Path(tempfile.mkdtemp(prefix="p2s-eval-"))
        self.repo = repo
        self.tree = self.root / "tree"
        _git(["worktree", "add", "-q", "--detach", str(self.tree), base_commit], repo)
        src = self.tree / "src"
        self.top = sorted(p.stem if p.is_file() else p.name for p in src.iterdir()
                          if (p.is_dir() and (p / "__init__.py").is_file()) or (p.is_file() and p.suffix == ".py")) \
            if src.is_dir() else []
        self.copied: list[str] = []
        for rel, deleted in _changed_src(wt):
            low = rel.lower()
            if any(fnmatch.fnmatchcase(low, g.lower()) for g in gate_deny):
                continue
            dst, origin = self.tree / rel, wt / rel
            if deleted:
                dst.unlink(missing_ok=True)
            elif origin.is_file() and not origin.is_symlink():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, dst)
                self.copied.append(rel)

    def scripts(self, junit: Path, timeout_s: float) -> Path:
        src = str((self.tree / "src").resolve())
        roots = tuple(sorted({norm_root(src), norm_root(sys.prefix), norm_root(sys.base_prefix),
                              norm_root(sys.exec_prefix)}))
        child = self.root / "child.py"
        child.write_text(CHILD_TEMPLATE.format(src=src, roots=roots, rpc=RPC_SOURCE, deny=DENY_EVENTS,
                                               wflags=WRITE_FLAGS, mark=ISOLATION_MARK), encoding="utf-8")
        judge = self.root / "judge.py"
        judge.write_text(JUDGE_TEMPLATE.format(rpc=RPC_SOURCE, child=str(child), src=src, junit=str(junit),
                                               top=tuple(self.top), timeout=float(timeout_s)), encoding="utf-8")
        return judge

    def remove(self) -> None:
        subprocess.run(["git", "worktree", "remove", "--force", str(self.tree)], cwd=str(self.repo),
                       capture_output=True, timeout=60, check=False)
        subprocess.run(["git", "worktree", "prune"], cwd=str(self.repo), capture_output=True, timeout=60, check=False)
        shutil.rmtree(self.root, ignore_errors=True)
