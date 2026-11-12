"""Launch the bundled batch audit on a disposable offline Windows qualification host."""

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from time import monotonic, sleep

RELEASE = b"\x01"
LOG_LIMIT = 1024 * 1024
JOB_BASIC_ACCOUNTING = 1
JOB_EXTENDED_LIMITS = 9
KILL_ON_JOB_CLOSE = 0x2000
PROCESS_ASSIGN_RIGHTS = 0x0101


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", ctypes.c_uint32), ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32), ("terminated_processes", ctypes.c_uint32),
    ]


class WindowsJob:
    def __init__(self):
        if sys.platform != "win32":
            raise ValueError("Batch host qualification requires Windows.")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, number, pointer = wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p

        def bind(name, result, *arguments):
            api = getattr(kernel, name)
            api.restype, api.argtypes = result, arguments
            return api

        self.create = bind("CreateJobObjectW", handle, pointer, wintypes.LPCWSTR)
        self.close = bind("CloseHandle", wintypes.BOOL, handle)
        self.limits = bind("SetInformationJobObject", wintypes.BOOL, handle, ctypes.c_int, pointer, number)
        self.query = bind("QueryInformationJobObject", wintypes.BOOL, handle, ctypes.c_int, pointer, number, pointer)
        self.open_process = bind("OpenProcess", handle, number, wintypes.BOOL, number)
        self.assign = bind("AssignProcessToJobObject", wintypes.BOOL, handle, handle)
        self.stop = bind("TerminateJobObject", wintypes.BOOL, handle, wintypes.UINT)

    @staticmethod
    def check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    @contextmanager
    def owned_handle(self, value):
        value = self.check(value)
        try:
            yield value
        finally:
            self.check(self.close(value))

    @contextmanager
    def session(self):
        limits = ExtendedLimits()
        limits.basic.flags = KILL_ON_JOB_CLOSE
        with self.owned_handle(self.create(None, None)) as self.handle:
            self.check(self.limits(self.handle, JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)))
            yield self

    def contain(self, process):
        with self.owned_handle(self.open_process(PROCESS_ASSIGN_RIGHTS, False, process.pid)) as handle:
            # The bootstrap has not received permission to start a child.
            if process.poll() is not None:
                raise ValueError("The waiting batch bootstrap exited before assignment.")
            self.check(self.assign(self.handle, handle))

    def active(self):
        accounting = Accounting()
        self.check(self.query(self.handle, JOB_BASIC_ACCOUNTING, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return accounting.active_processes

    def terminate(self):
        self.check(self.stop(self.handle, 1))


def bootstrap(command):
    if sys.stdin.buffer.read(1) != RELEASE:
        return 2
    return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode


def run_contained(command, directory, timeout, *, job=None):
    job = WindowsJob() if job is None else job
    overflow, drained = Event(), Event()

    def drain(pipe):
        total = 0
        try:
            while data := pipe.read1(16384):
                total += len(data)
                if total > LOG_LIMIT:
                    overflow.set()
                    return
            drained.set()
        except OSError:
            overflow.set()

    with job.session():
        process = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--bootstrap", *command],
            cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        reader = Thread(target=drain, args=(process.stdout,), daemon=True)
        assigned = False
        try:
            job.contain(process)
            assigned = True
            reader.start()
            process.stdin.write(RELEASE)
            process.stdin.close()
            deadline = monotonic() + timeout
            while process.poll() is None or job.active() or not drained.is_set():
                if overflow.is_set():
                    raise ValueError("Batch host exceeded its output limit.")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ValueError("Batch host timed out.")
                overflow.wait(min(0.05, remaining))
            if process.returncode != 0:
                raise ValueError("Batch host launcher failed.")
        finally:
            try:
                if not process.stdin.closed:
                    process.stdin.close()
                if assigned and job.active():
                    job.terminate()
                    deadline = monotonic() + 5
                    while job.active():
                        if monotonic() >= deadline:
                            raise ValueError("Batch host exit could not be verified.")
                        sleep(0.05)
                process.wait(timeout=5)
            finally:
                if reader.ident is not None:
                    reader.join(timeout=5)
                if not reader.is_alive():
                    process.stdout.close()


def qualification_command(executable, workspace):
    from batch_model_audit import checked_path, digest, read_json, regular_file
    from read_batch_models import AUDITS, MAX_JSON

    executable = checked_path(executable)
    regular_file(executable)
    if executable.name.casefold() != "pyrevit.exe":
        raise ValueError("Select an installed pyRevit CLI executable by absolute path.")
    work = checked_path(workspace)
    manifest = read_json(work / "host-manifest.json")
    if manifest["revit_year"] != "2026" or os.path.lexists(work / "host-receipt.json"):
        raise ValueError("Use a fresh, staged Revit 2026 batch workspace.")
    scripts = Path(__file__).resolve().parent
    sources = (scripts / AUDITS[0], scripts.parents[1] / "am-param-qa" / "scripts" / AUDITS[1],
               scripts / "read_batch_models.py")
    pinned = manifest["audit_sha256"]
    if set(pinned) != {source.name for source in sources} or not all(
        digest(work / source.name, max_file=MAX_JSON) == pinned[source.name]
        == digest(source, max_file=MAX_JSON) for source in sources
    ):
        raise ValueError("Staged audit source does not match the bundled worker.")
    return [str(executable), "run", str(work / "read_batch_models.py"), "--revit=2026"], work, manifest


def main(argv=None):
    from batch_model_audit import read_receipt

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyrevit", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--timeout", type=int, default=600, choices=range(1, 1801), metavar="SECONDS")
    parser.add_argument("--offline-fixture-host", required=True, action="store_true")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "win32":
            raise ValueError("Batch host qualification requires Windows.")
        if not Path(args.pyrevit).is_absolute() or not Path(args.workspace).is_absolute():
            raise ValueError("Use absolute local paths for the executable and staged workspace.")
        command, workspace, manifest = qualification_command(args.pyrevit, args.workspace)
        (workspace / "qualification-attempt").open("xb").close()
        run_contained(command, workspace, args.timeout)
        read_receipt(workspace, manifest, manifest["models"])
        print("The batch host exited. Collect and verify its private receipt from the coordinator.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, subprocess.TimeoutExpired) as error:
        print("Batch qualification failed. Keep the private workspace. " + type(error).__name__
              + (": " + str(error) if type(error) is ValueError else "."), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if sys.argv[1:2] == ["--bootstrap"]:
        sys.exit(bootstrap(sys.argv[2:]))
    sys.exit(main())
