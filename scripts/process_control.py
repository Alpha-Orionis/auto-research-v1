"""Own a workload's descendants, not just its original process ID."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from pathlib import Path


def process_members(pid):
    """Best-effort process-group/ancestry inventory; unknown remains None."""
    if os.name == "nt":
        from ctypes import wintypes
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", ctypes.c_long),
                        ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            return None
        parents = {}
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                parents[entry.pid] = entry.parent
                ok = kernel.Process32NextW(handle, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(handle)
    elif Path("/proc").is_dir():
        parents = {}
        members = set()
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                values = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if values[0] not in {"Z", "X"}:
                    parents[int(path.name)] = int(values[1])
                    if int(values[2]) == pid:
                        members.add(int(path.name))
            except (OSError, ValueError, IndexError):
                continue
        # Group membership also survives the original parent's exit.
        if members:
            return sorted(members)
    else:
        return None
    members = {pid} if pid in parents else set()
    while True:
        expanded = members | {child for child, parent in parents.items() if parent in members}
        if expanded == members:
            return sorted(members)
        members = expanded


class WindowsJob:
    """Assign a suspended child before any descendant can escape containment."""
    def __init__(self):
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Limits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IO)] + [
                (name, ctypes.c_size_t) for name in ("process_memory", "job_memory", "peak_process", "peak_job")]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]
        self.accounting_type = Accounting
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign_and_resume(self, child):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes the initial thread handle; resume the suspended process
        # through the native API after successful Job Object assignment.
        native = ctypes.WinDLL("ntdll")
        native.NtResumeProcess.argtypes = [ctypes.c_void_p]
        native.NtResumeProcess.restype = ctypes.c_long
        if native.NtResumeProcess(int(child._handle)) != 0:
            raise OSError("Cannot resume the contained process.")

    def active(self):
        counters = self.accounting_type()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.active > 0

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 125):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Workload:
    def __init__(self, argv, **options):
        self.job = WindowsJob() if os.name == "nt" else None
        self.child = None
        try:
            self.child = subprocess.Popen(
                argv, shell=False, close_fds=True, start_new_session=os.name != "nt",
                creationflags=(subprocess.CREATE_NO_WINDOW | 4) if os.name == "nt" else 0,
                **options,
            )
            if self.job:
                self.job.assign_and_resume(self.child)
        except BaseException:
            if self.child:
                self.child.kill()
                self.child.wait(timeout=5)
            if self.job:
                self.job.close()
            raise

    def active(self):
        if self.job:
            return self.job.active()
        members = process_members(self.child.pid)
        if members is not None:
            return bool(members)
        try:
            os.killpg(self.child.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop(self):
        if self.job:
            self.job.terminate()
        else:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while self.active() and time.monotonic() < deadline:
            self.child.poll()
            time.sleep(0.02)
        if self.active() and not self.job:
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while self.active() and time.monotonic() < deadline:
            self.child.poll()
            time.sleep(0.02)
        self.child.wait(timeout=5)
        if self.active():
            raise OSError("Workload descendants are still alive; containment must remain held.")

    def close(self):
        if self.job:
            self.job.close()
