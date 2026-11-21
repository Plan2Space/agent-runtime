"""Best-effort resource sampling during a run: GPU (nvidia-smi), system RAM and CPU (Windows).

Anything that cannot be measured is reported as ``null`` with a reason, never estimated.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading


def _gpu_sample() -> tuple[float, float] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, check=False)
        mem, util = r.stdout.strip().splitlines()[0].split(",")
        return float(mem), float(util)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _MEM(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    def _ram_used_mib() -> float | None:
        m = _MEM()
        m.dwLength = ctypes.sizeof(_MEM)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return (m.ullTotalPhys - m.ullAvailPhys) / 2**20

    def _cpu_times() -> tuple[int, int] | None:
        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return None

        def v(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return v(idle), v(kern) + v(user)
else:  # pragma: no cover - CI is Linux; sampling is informative only
    def _ram_used_mib() -> float | None:
        return None

    def _cpu_times() -> tuple[int, int] | None:
        return None


class ResourceSampler:
    def __init__(self, interval_s: float = 2.0):
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.gpu_mem: list[float] = []
        self.gpu_util: list[float] = []
        self.ram: list[float] = []
        self._cpu0 = None

    def start(self) -> None:
        self._cpu0 = _cpu_times()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            g = _gpu_sample()
            if g:
                self.gpu_mem.append(g[0])
                self.gpu_util.append(g[1])
            r = _ram_used_mib()
            if r is not None:
                self.ram.append(r)
            self._stop.wait(self.interval_s)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        cpu1 = _cpu_times()
        cpu = None
        if self._cpu0 and cpu1 and cpu1[1] > self._cpu0[1]:
            cpu = round(100.0 * (1 - (cpu1[0] - self._cpu0[0]) / (cpu1[1] - self._cpu0[1])), 1)

        def stat(xs):
            return {"max": round(max(xs), 1), "mean": round(sum(xs) / len(xs), 1), "samples": len(xs)} if xs else None
        return {
            "scope": "system-wide (GPU and RAM are shared with the desktop and other processes)",
            "gpu_mem_used_mib": stat(self.gpu_mem) or {"value": None, "reason": "nvidia-smi unavailable"},
            "gpu_util_pct": stat(self.gpu_util) or {"value": None, "reason": "nvidia-smi unavailable"},
            "ram_used_mib": stat(self.ram) or {"value": None, "reason": "RAM probe unavailable on this platform"},
            "cpu_busy_pct_mean": cpu if cpu is not None else {"value": None, "reason": "CPU probe unavailable"},
        }


class NullSampler(ResourceSampler):
    def start(self) -> None:
        pass

    def stop(self) -> dict:
        return {"scope": "disabled", "gpu_mem_used_mib": None, "gpu_util_pct": None, "ram_used_mib": None,
                "cpu_busy_pct_mean": None}
