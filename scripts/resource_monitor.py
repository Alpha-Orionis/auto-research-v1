"""Lightweight, read-only local telemetry; unavailable counters remain null."""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def windows_counters(pid):
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")
        ]

    class ProcessMemory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in
            ("peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
    kernel.GetSystemTimes.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    def ticks(value):
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    result = {}
    if kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        result.update(memory_total_bytes=memory.total, memory_available_bytes=memory.available)
    idle, system, user = (wintypes.FILETIME() for _ in range(3))
    if kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
        result["host_ticks"] = (ticks(system) + ticks(user), ticks(idle))
    handle = kernel.OpenProcess(0x1010, False, pid)  # query + read this process's counters
    if handle:
        try:
            created, exited, process_kernel, process_user = (wintypes.FILETIME() for _ in range(4))
            if kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in (created, exited, process_kernel, process_user)]):
                result["process_cpu_seconds"] = (ticks(process_kernel) + ticks(process_user)) / 10_000_000
            process_memory = ProcessMemory()
            process_memory.cb = ctypes.sizeof(process_memory)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(process_memory), process_memory.cb):
                result["process_rss_bytes"] = process_memory.rss
        finally:
            kernel.CloseHandle(handle)
    return result


def linux_counters(pid):
    result = {}
    try:
        values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        # guest counters are already included in user/nice counters.
        result["host_ticks"] = (sum(values[:8]), values[3] + values[4])
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            memory[name] = int(value.split()[0]) * 1024
        result.update(memory_total_bytes=memory.get("MemTotal"), memory_available_bytes=memory.get("MemAvailable"))
    except (OSError, ValueError, IndexError):
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        result["process_cpu_seconds"] = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
        result["process_rss_bytes"] = max(0, int(stat[21])) * os.sysconf("SC_PAGE_SIZE")
        counters = {}
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            name, value = line.split(":", 1)
            counters[name] = int(value)
        result.update(process_read_bytes=counters.get("read_bytes"), process_write_bytes=counters.get("write_bytes"))
    except (OSError, ValueError, IndexError):
        pass
    return result


def gpu_counters(gpu_ids):
    if not gpu_ids:
        return [], "not_requested"
    executable = shutil.which("nvidia-smi")
    if not executable:
        return [], "unavailable"
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=index,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits", "--id=" + ",".join(str(value) for value in gpu_ids)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode:
            return [], "unavailable"
        records = []
        for row in csv.reader(io.StringIO(completed.stdout)):
            if len(row) != 4:
                continue
            index = int(row[0].strip())
            if index not in gpu_ids:
                continue
            def number(value):
                try:
                    return float(value.strip())
                except ValueError:
                    return None
            records.append(dict(index=index, utilization_percent=number(row[1]), memory_used_mib=number(row[2]), memory_total_mib=number(row[3])))
        return records, "available" if records else "unavailable"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], "unavailable"


class Sampler:
    def __init__(self, root: Path, gpu_ids=()):
        self.root = root
        self.gpu_ids = tuple(gpu_ids)
        self.previous_host = None
        self.previous_process = None

    def sample(self, pid):
        now = time.monotonic()
        result = dict(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"), pid=pid,
            host_cpu_percent=None, process_cpu_percent=None, process_rss_bytes=None,
            memory_total_bytes=None, memory_available_bytes=None,
            process_read_bytes=None, process_write_bytes=None, disk_free_bytes=None,
        )
        try:
            counters = windows_counters(pid) if os.name == "nt" else linux_counters(pid)
            host = counters.pop("host_ticks", None)
            if host and self.previous_host:
                total = host[0] - self.previous_host[0]
                idle = host[1] - self.previous_host[1]
                if total > 0:
                    result["host_cpu_percent"] = round(max(0, min(100, 100 * (total - idle) / total)), 2)
            self.previous_host = host
            cpu = counters.get("process_cpu_seconds")
            if cpu is not None and self.previous_process and self.previous_process[0] == pid:
                _, old_time, old_cpu = self.previous_process
                if now > old_time:
                    result["process_cpu_percent"] = round(max(0, 100 * (cpu - old_cpu) / (now - old_time)), 2)
            self.previous_process = (pid, now, cpu) if cpu is not None else None
            result.update(counters)
        except (OSError, ValueError, AttributeError):
            result["counter_status"] = "partially_unavailable"
        try:
            result["disk_free_bytes"] = shutil.disk_usage(self.root).free
        except OSError:
            pass
        result["gpus"], result["gpu_status"] = gpu_counters(self.gpu_ids)
        return result
