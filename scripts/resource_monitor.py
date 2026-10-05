"""Lightweight, read-only local telemetry; unavailable counters remain null."""
from __future__ import annotations

import csv
import io
import math
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from process_control import process_members


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
            [executable, "--query-gpu=index,utilization.gpu,memory.used,memory.total,power.draw,uuid",
             "--format=csv,noheader,nounits", "--id=" + ",".join(str(value) for value in gpu_ids)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode:
            return [], "unavailable"
        records = []
        for row in csv.reader(io.StringIO(completed.stdout)):
            if len(row) not in {4, 6}:
                continue
            index = int(row[0].strip())
            if index not in gpu_ids:
                continue
            def number(value):
                try:
                    result = float(value.strip())
                    return result if math.isfinite(result) and result >= 0 else None
                except ValueError:
                    return None
            records.append(dict(index=index, utilization_percent=number(row[1]), memory_used_mib=number(row[2]),
                                memory_total_mib=number(row[3]), power_watts=number(row[4]) if len(row) == 6 else None,
                                uuid=row[5].strip() if len(row) == 6 else None))
        complete = {record["index"] for record in records} == set(gpu_ids) and all(
            record[field] is not None for record in records
            for field in ("utilization_percent", "memory_used_mib", "memory_total_mib")
        )
        complete = complete and all(record["memory_total_mib"] > 0 and record["utilization_percent"] <= 100 for record in records)
        return records, ("available" if complete else "partially_unavailable") if records else "unavailable"
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return [], "unavailable"


def gpu_ownership(gpus, pid):
    members = process_members(pid)
    executable = shutil.which("nvidia-smi")
    if members is None or not executable or any(not gpu.get("uuid") for gpu in gpus):
        return [], "unknown", []
    try:
        completed = subprocess.run([executable, "--query-compute-apps=gpu_uuid,pid,used_memory",
                                    "--format=csv,noheader,nounits"], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=3, shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if completed.returncode:
            return [], "unknown", members
        indices = {gpu["uuid"]: gpu["index"] for gpu in gpus}
        processes = []
        for row in csv.reader(io.StringIO(completed.stdout)):
            if not row:
                continue
            if len(row) != 3:
                return [], "unknown", members
            if row[0].strip() not in indices:
                continue
            owner_pid = int(row[1].strip())
            try:
                memory = float(row[2].strip())
                if not math.isfinite(memory) or memory < 0:
                    memory = None
            except ValueError:
                memory = None
            processes.append(dict(index=indices[row[0].strip()], pid=owner_pid,
                                  memory_used_mib=memory, owned=owner_pid in members))
        return processes, "available", members
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], "unknown", members


class GPUWindow:
    """Time-weighted, complete windows; consecutive counts advance once per window."""
    def __init__(self, ids, seconds=300, threshold=30, consecutive=3, max_gap=75):
        self.ids = tuple(ids)
        self.seconds, self.threshold, self.required, self.max_gap = seconds, threshold, consecutive, max_gap
        self.samples = deque()
        self.last_check = None
        self.bad = 0

    def update(self, sample, now=None):
        now = time.monotonic() if now is None else now
        devices = {gpu["index"]: gpu.get("utilization_percent") for gpu in sample.get("gpus", [])}
        complete = sample.get("gpu_status") == "available" and set(devices) == set(self.ids) and bool(self.ids)
        complete = complete and all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100 for value in devices.values())
        if not complete or (self.samples and now-self.samples[-1][0] > self.max_gap):
            self.samples.clear()
            self.bad, self.last_check = 0, None
        if complete:
            self.samples.append((now, devices, sample.get("gpu_ownership_status"), sample.get("project_gpu_ids", [])))
        start = now-self.seconds
        while len(self.samples) > 2 and self.samples[1][0] <= start:
            self.samples.popleft()
        mature = len(self.samples) >= 3 and self.samples[0][0] <= start
        means = {str(index): None for index in self.ids}
        raw_means = {}
        owned = sorted(set(sample.get("project_gpu_ids", [])))
        ownership_known = sample.get("gpu_ownership_status") == "available"
        under_load = mature and all(item[2] == "available" and set(self.ids).issubset(item[3]) for item in self.samples)
        if mature:
            for index in self.ids:
                integral = 0.0
                for left, right in zip(self.samples, list(self.samples)[1:]):
                    a, b = max(start, left[0]), right[0]
                    if b > a:
                        # Piecewise constant sampled device utilization.
                        integral += (b-a)*left[1][index]
                raw_means[str(index)] = integral/self.seconds
                means[str(index)] = round(raw_means[str(index)], 2)
            if self.last_check is None or now-self.last_check >= self.seconds:
                low = under_load and any(value < self.threshold for value in means.values())
                self.bad = self.bad+1 if low else 0
                self.last_check = now
        if not under_load:
            self.bad = 0
        average = round(sum(raw_means.values())/len(raw_means), 2) if mature else None
        return dict(window_seconds=self.seconds, window_mature=mature, sample_count=len(self.samples),
                    average_gpu_utilization_percent=average, per_gpu_average_percent=means,
                    project_gpu_ids=owned, ownership_status="available" if ownership_known else "unknown",
                    workload_present=under_load, low_utilization_threshold_percent=self.threshold,
                    consecutive_bad_windows=self.bad, required_bad_windows=self.required,
                    alert_ready=under_load and self.bad >= self.required,
                    current_gpu_status=sample.get("gpu_status", "unknown"),
                    missing_fields=([] if mature else ["mature_window_average"])
                    + (["gpu_ownership"] if self.ids and not ownership_known else [])
                    + (["complete_gpu_samples"] if self.ids and not complete else []))


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
        host = None
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
        except (OSError, ValueError, AttributeError, IndexError):
            pass
        try:
            result["disk_free_bytes"] = shutil.disk_usage(self.root).free
        except OSError:
            pass
        result["gpus"], result["gpu_status"] = gpu_counters(self.gpu_ids)
        result["gpu_device_ids"] = list(self.gpu_ids)
        processes, ownership, members = gpu_ownership(result["gpus"], pid) if self.gpu_ids else ([], "not_requested", [])
        result.update(gpu_processes=processes, gpu_ownership_status=ownership, managed_process_ids=members,
                      project_gpu_ids=sorted({item["index"] for item in processes if item["owned"]}))
        missing = [name for name in ("memory_total_bytes", "memory_available_bytes", "process_cpu_seconds",
                                    "process_rss_bytes", "disk_free_bytes") if result.get(name) is None]
        if host is None:
            missing.append("host_cpu_counters")
        result["unavailable_counters"] = missing
        result["counter_status"] = "partially_unavailable" if missing else "available"
        return result


def detect_anomalies(sample, quiet_seconds, quiet_limit):
    """Pure read-only assessment. Unknown metrics do not establish recovery."""
    alerts = {}
    checked = {"counter_unavailable", "gpu_sampling_unavailable"}

    def add(code, message, observed, threshold, hint):
        alerts[code] = dict(code=code, severity="warning", message=message,
                            observed=observed, threshold=threshold, hint=hint)

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

    if number(quiet_seconds):
        checked.add("quiet_output")
    if number(quiet_seconds) and quiet_seconds >= quiet_limit:
        add("quiet_output", "Captured logs have been quiet longer than the configured threshold.",
            {"quiet_seconds": round(quiet_seconds, 1)}, {"quiet_seconds": quiet_limit},
            "Inspect progress and resource evidence; quiet logs alone do not prove a stall.")
    free = sample.get("disk_free_bytes")
    if number(free):
        checked.add("low_disk_space")
        if free < 256 * 1024 * 1024:
            add("low_disk_space", "Free disk space is below 256 MiB.", {"disk_free_bytes": free},
                {"minimum_free_bytes": 256 * 1024 * 1024}, "Inspect the project volume and decide whether to free space or cancel.")
    total, available = sample.get("memory_total_bytes"), sample.get("memory_available_bytes")
    if number(total) and total > 0 and number(available):
        checked.add("low_host_memory")
        if available / total < 0.02:
            add("low_host_memory", "Available host RAM is below two percent.",
                {"memory_total_bytes": total, "memory_available_bytes": available},
                {"minimum_available_fraction": 0.02}, "Inspect concurrent workloads and the approved memory budget.")
    gpus = sample.get("gpus", [])
    complete_gpu_memory = sample.get("gpu_status") == "not_requested"
    if sample.get("gpu_status") == "available" and gpus:
        complete_gpu_memory = all(number(gpu.get("memory_used_mib")) and number(gpu.get("memory_total_mib"))
                                  and gpu["memory_total_mib"] > 0 for gpu in gpus)
    if complete_gpu_memory:
        checked.add("high_device_memory")
    high = [dict(index=gpu.get("index"), memory_used_mib=gpu["memory_used_mib"], memory_total_mib=gpu["memory_total_mib"])
            for gpu in gpus if number(gpu.get("memory_used_mib")) and number(gpu.get("memory_total_mib"))
            and gpu["memory_total_mib"] > 0 and gpu["memory_used_mib"] / gpu["memory_total_mib"] >= 0.95]
    if high:
        add("high_device_memory", "Selected NVIDIA device memory is at least 95 percent full.",
            {"devices": high}, {"maximum_used_fraction": 0.95},
            "Check device workloads; these counters cover the whole device, not this task alone.")
    if sample.get("counter_status") in {"unavailable", "partially_unavailable"}:
        add("counter_unavailable", "Some requested CPU, memory, or disk counters are unavailable.",
            {"unavailable_counters": sample.get("unavailable_counters", [])}, {"expected": "available counters"},
            "Check operating-system counter access; unavailable data is not proof of healthy resources.")
    if sample.get("gpu_status") in {"unavailable", "partially_unavailable"}:
        add("gpu_sampling_unavailable", "Optional NVIDIA sampling is unavailable or incomplete.",
            {"gpu_status": sample["gpu_status"], "device_ids": sample.get("gpu_device_ids", [])},
            {"expected": "all selected devices sampled"}, "Check nvidia-smi and the selected device IDs; the task can continue.")
    window = sample.get("gpu_window", {})
    if window.get("window_mature") and window.get("ownership_status") == "available":
        checked.add("low_gpu_utilization")
    if window.get("alert_ready") is True:
        add("low_gpu_utilization", "Owned GPUs have sustained low window-average utilization under load.",
            window, {"minimum_average_percent": window["low_utilization_threshold_percent"],
                     "consecutive_windows": window["required_bad_windows"]},
            "Inspect the workload, data pipeline and per-GPU averages before choosing a controlled optimization.")
    return alerts, checked
