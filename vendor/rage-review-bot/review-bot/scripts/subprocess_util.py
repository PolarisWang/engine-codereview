"""Shared subprocess + lark-cli helpers for the review bot.

Consolidates the Windows `creationflags` boilerplate (CREATE_NO_WINDOW and
the detached-process flag combo) and the lark-cli run.js path lookup into
one place. Every subprocess spawn across the bot goes through here so that
tuning a flag or swapping the path is a single-file edit.

Also provides a unified singleton-via-PID-file pattern used by the
listener, daemon, and monitor.
"""

import os
import subprocess
from pathlib import Path


CREATE_NO_WINDOW         = 0x08000000
DETACHED_PROCESS         = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

_WIN = os.name == "nt"


def _add_flags(kwargs, flags):
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | flags


def hidden_run(cmd, **kwargs):
    """subprocess.run with CREATE_NO_WINDOW on Windows; plain elsewhere.

    Accepts the same kwargs as subprocess.run (timeout, capture_output,
    text, encoding, check, stdout, stderr, ...).
    """
    if _WIN:
        _add_flags(kwargs, CREATE_NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def detached_popen(cmd, **kwargs):
    """subprocess.Popen as a fully-detached background process on Windows.

    Used for launching the listener and daemon from a foreground Claude
    Code session so they survive /clear. POSIX: ordinary Popen (double-
    fork is the caller's problem but in practice we only use this on
    Windows).
    """
    if _WIN:
        _add_flags(kwargs,
                   DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)


def lark_cli_path():
    """Absolute path to the lark-cli run.js entry point.

    Resolves via %APPDATA% / $APPDATA. Returned as a string so it plugs
    directly into argv lists (Path objects work too but we keep the
    call-site shape uniform with existing code).
    """
    appdata = os.environ.get("APPDATA", "")
    return str(Path(appdata) / "npm/node_modules/@larksuite/cli/scripts/run.js")


def lark_cli_argv_prefix():
    """argv prefix for invoking lark-cli — preferred over `lark_cli_path`.

    On Windows returns ``[<...>/bin/lark-cli.exe]`` so callers can do
    ``cmd = lark_cli_argv_prefix() + ["im", "+messages-reply", ...]``.
    Going through ``lark-cli.exe`` directly skips the ``node run.js``
    bootstrap, which internally re-execs ``lark-cli.exe`` via
    ``child_process.spawn`` without ``windowsHide: true`` and pops a
    console window every invocation. With ``CREATE_NO_WINDOW`` set on
    our spawn (via ``hidden_run``/``detached_popen``), invoking the
    binary directly keeps the whole tree windowless.

    On POSIX falls back to ``["node", <run.js>]`` since ``lark-cli.exe``
    is Windows-only.
    """
    if _WIN:
        appdata = os.environ.get("APPDATA", "")
        return [str(Path(appdata) / "npm/node_modules/@larksuite/cli/bin/lark-cli.exe")]
    return ["node", lark_cli_path()]


# ---------------------------------------------------------------------------
# Singleton PID-file primitives
# ---------------------------------------------------------------------------
# Used by listener, daemon, and monitor to enforce one-instance-at-a-time.

# ---------------------------------------------------------------------------
# Listener-restart guard
# ---------------------------------------------------------------------------
# A restart of the listener alone leaves the daemon running, and the daemon's
# `_health_check_listener` will happily respawn a listener it finds dead —
# racing the restarter's own launch. `start_listener` has no singleton guard
# (it kills whatever pid it reads and spawns regardless), so the two spawns
# collide and one listener ends up orphaned: alive, absent from the pid file,
# unreachable by any later stop. Sequencing cannot fix this the way it does for
# a multi-component restart, because here the daemon is deliberately NOT being
# restarted — so the restarter posts a short-lived marker and the daemon stands
# down while it is held (DESIGN §1.1.6).
#
# The TTL is the safety valve: a restarter that dies mid-flight must not
# suppress the health check forever, so an expired marker counts as absent.

LISTENER_RESTART_GUARD_NAME = "listener_restart.guard"
# Comfortably longer than kill-confirm (8 s) + launch-confirm (20 s), short
# enough that a crashed restarter costs at most one skipped health check.
LISTENER_RESTART_GUARD_TTL_S = 90.0


def write_guard(path, ttl_s=LISTENER_RESTART_GUARD_TTL_S):
    """Claim the guard until now+ttl. Best-effort; returns True on success."""
    import json
    import time
    try:
        with open(str(path), "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(),
                       "expires_at": time.time() + float(ttl_s)}, handle)
        return True
    except OSError:
        return False


def guard_active(path):
    """True if the guard exists and has not expired.

    An unreadable or malformed marker is treated as ABSENT, not as held: the
    failure mode of "held forever" is a listener that never gets supervised,
    which is strictly worse than one redundant respawn.
    """
    import json
    import time
    try:
        with open(str(path), "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    try:
        return float(data.get("expires_at") or 0) > time.time()
    except (TypeError, ValueError):
        return False


def clear_guard(path):
    """Release the guard. Never raises — the TTL is the real backstop."""
    try:
        os.remove(str(path))
    except OSError:
        pass


def _k32_with_openprocess():
    """kernel32 handle with `OpenProcess` fully declared.

    ctypes defaults a foreign function's restype to `c_int`, which TRUNCATES a
    64-bit HANDLE. Windows currently guarantees handle values fit in 32 bits so
    nothing breaks today, but these handles are opened and closed continuously
    inside a long-lived daemon, and `iter_process_tree` already declares its
    own restype — leaving the other four call sites implicit is the kind of
    inconsistency that reads as "the declared one was special".
    """
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def is_process_alive(pid):
    """Check if a process with the given PID is running.

    On Windows this queries the kernel directly via ``OpenProcess`` +
    ``GetExitCodeProcess`` rather than shelling out to ``tasklist.exe``.
    ``tasklist`` (and every other console helper: ``taskkill``, WMI) fails
    with ``ERROR_COMMITMENT_LIMIT`` ("The paging file is too small…") once
    the interactive desktop heap is exhausted — a condition the bot itself
    can create when a listener respawn loop piles up zombie processes. The
    old ``tasklist`` implementation returned ``False`` on that error, so the
    daemon's health-check saw a healthy listener as dead every cycle and
    respawned it, feeding the very runaway that broke ``tasklist`` in the
    first place. The Win32 API needs no child process and keeps working
    under desktop-heap exhaustion, breaking that feedback loop.
    """
    if pid is None:
        return False
    try:
        if _WIN:
            import ctypes
            from ctypes import wintypes
            k32 = _k32_with_openprocess()
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            handle = k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                # Access-denied means the process exists but is not queryable
                # by us (still alive); any other error means it is gone.
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def kill_process(pid):
    """Kill a process by PID. Silently succeeds if already dead.

    Uses ``OpenProcess`` + ``TerminateProcess`` on Windows rather than
    ``taskkill.exe`` for the same reason ``is_process_alive`` avoids
    ``tasklist`` — the console helper fails under desktop-heap exhaustion,
    exactly when a runaway most needs to be torn down. The direct Win32
    call spawns no child process and keeps working.
    """
    if pid is None:
        return
    try:
        if _WIN:
            k32 = _k32_with_openprocess()
            PROCESS_TERMINATE = 0x0001
            handle = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
            if handle:
                try:
                    k32.TerminateProcess(handle, 1)
                finally:
                    k32.CloseHandle(handle)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def process_start_time(pid):
    """Unix timestamp when the process started, or None if unknown.

    Used by `restart_bot.py` to answer "is this process older than the code it
    loaded?" — a long-lived daemon imports its Python once at startup, so an
    edit newer than its start time is code it will never run (DESIGN §1.1.6).

    Windows: `GetProcessTimes`, converted from FILETIME (100 ns units since
    1601-01-01 UTC). POSIX: `ps -o lstart=` is fragile to parse, so we read
    `/proc/<pid>/stat` field 22 against the boot time instead.
    """
    if pid is None:
        return None
    try:
        if _WIN:
            import ctypes
            from ctypes import wintypes
            k32 = _k32_with_openprocess()
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not k32.GetProcessTimes(handle, ctypes.byref(created),
                                           ctypes.byref(exited),
                                           ctypes.byref(kernel),
                                           ctypes.byref(user)):
                    return None
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                # FILETIME epoch (1601) to Unix epoch (1970) = 11644473600 s.
                return ticks / 10000000.0 - 11644473600.0
            finally:
                k32.CloseHandle(handle)
        with open("/proc/%d/stat" % int(pid), "r") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
        clock = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat", "r") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1]) + float(fields[19]) / clock
        return None
    except Exception:  # noqa: BLE001 — best effort, caller degrades
        return None


def iter_process_tree():
    """Yield ``(pid, parent_pid, image_name)`` for every process.

    Windows: ``CreateToolhelp32Snapshot``, which returns pid, parent pid, and
    image name in one kernel call — the parent link is not available from the
    ``EnumProcesses`` + PEB scan below, and ``wmic`` / CIM are unusable here
    (see ``iter_processes``). POSIX: ``ps -eo pid=,ppid=,comm=``.

    Raises ``OSError`` if the snapshot itself fails, so a caller can tell
    "scan broke" from "no matches" — the same contract as ``iter_processes``.
    """
    if not _WIN:
        result = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="],
                                capture_output=True, text=True, timeout=25)
        if result.returncode != 0:
            raise OSError("ps failed: %s" % (result.stderr or "").strip())
        for line in (result.stdout or "").splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                yield int(parts[0]), int(parts[1]), parts[2].strip()
        return

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _ProcessEntry32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise OSError("CreateToolhelp32Snapshot failed: %d"
                      % ctypes.get_last_error())
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return
        while True:
            yield (int(entry.th32ProcessID),
                   int(entry.th32ParentProcessID),
                   entry.szExeFile)
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)


def find_ancestor_pid(image_name, start_pid=None, max_depth=12):
    """Return the PID of the nearest ancestor running ``image_name``.

    Used by the start flow to learn which ``claude.exe`` owns this session:
    the skill's scripts run as grandchildren of that process (claude → shell →
    python), and the PID is the only handle a later stop has on the session.
    Matching is case-insensitive and extension-insensitive (``claude`` matches
    ``claude.exe``).

    Returns None when no such ancestor exists (script run standalone), when
    the chain is longer than ``max_depth``, or when the scan fails — every
    caller treats a missing session PID as "don't record", never as an error
    worth aborting the start for.
    """
    target = image_name.lower().rsplit(".exe", 1)[0]
    try:
        tree = {pid: (ppid, name) for pid, ppid, name in iter_process_tree()}
    except (OSError, Exception):  # noqa: BLE001 — best-effort by contract
        return None

    pid = os.getpid() if start_pid is None else int(start_pid)
    seen = set()
    for _ in range(max_depth):
        entry = tree.get(pid)
        if not entry:
            return None
        ppid, _name = entry
        # PID 0/4 terminate the walk; a self-referential ppid would loop.
        if not ppid or ppid in seen:
            return None
        seen.add(ppid)
        parent = tree.get(ppid)
        if not parent:
            return None
        if parent[1].lower().rsplit(".exe", 1)[0] == target:
            return ppid
        pid = ppid
    return None


def iter_processes():
    """Yield ``(pid, command_line)`` for every readable process.

    Windows: ``EnumProcesses`` + a direct PEB read of
    ``RTL_USER_PROCESS_PARAMETERS.CommandLine``. This deliberately avoids
    ``Get-CimInstance Win32_Process`` / ``tasklist`` / ``wmic`` — every one of
    those spawns a console helper that dies under desktop-heap exhaustion
    ("The paging file is too small for this operation to complete"), which is
    precisely the state a runaway leaves the machine in. A scan that silently
    returns nothing is worse than no scan: callers read it as "nothing is
    running" and declare success. POSIX: ``ps -eo pid=,args=``.

    Raises ``OSError`` if the enumeration itself fails, so callers can tell
    "scan broke" apart from "no matches". Processes that individually refuse
    to open (permissions, exited mid-scan) are skipped, not fatal.
    """
    if not _WIN:
        result = subprocess.run(["ps", "-eo", "pid=,args="],
                                capture_output=True, text=True, timeout=25)
        if result.returncode != 0:
            raise OSError("ps failed: %s" % (result.stderr or "").strip())
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                yield int(parts[0]), parts[1]
        return

    import ctypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort),
                    ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_void_p)]

    class _ProcessBasicInformation(ctypes.Structure):
        _fields_ = [("Reserved1", ctypes.c_void_p),
                    ("PebBaseAddress", ctypes.c_void_p),
                    ("Reserved2", ctypes.c_void_p * 2),
                    ("UniqueProcessId", ctypes.c_void_p),
                    ("Reserved3", ctypes.c_void_p)]

    # Offsets into the 64-bit PEB / RTL_USER_PROCESS_PARAMETERS.
    PEB_PROCESS_PARAMETERS = 0x20
    PARAMS_COMMAND_LINE = 0x70
    PROCESS_QUERY_INFORMATION_AND_VM_READ = 0x0410

    pids = (ctypes.c_uint * 16384)()
    needed = ctypes.c_uint()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids),
                               ctypes.byref(needed)):
        raise OSError("EnumProcesses failed: %d" % ctypes.get_last_error())

    # Same restype/argtypes declaration as `_k32_with_openprocess` — this
    # function needs psapi + ntdll as well, so it builds its own handles.
    from ctypes import wintypes as _wt
    k32.OpenProcess.restype = _wt.HANDLE
    k32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    k32.CloseHandle.restype = _wt.BOOL
    k32.CloseHandle.argtypes = [_wt.HANDLE]

    def _read(handle, address, buffer, size):
        read = ctypes.c_size_t()
        return k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer,
                                     size, ctypes.byref(read))

    for index in range(needed.value // ctypes.sizeof(ctypes.c_uint)):
        pid = pids[index]
        handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION_AND_VM_READ,
                                 False, pid)
        if not handle:
            continue
        try:
            basic = _ProcessBasicInformation()
            if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(basic),
                                               ctypes.sizeof(basic),
                                               None) != 0:
                continue
            params = ctypes.c_void_p()
            if not _read(handle, basic.PebBaseAddress + PEB_PROCESS_PARAMETERS,
                         ctypes.byref(params), ctypes.sizeof(params)):
                continue
            command = _UnicodeString()
            if not _read(handle, params.value + PARAMS_COMMAND_LINE,
                         ctypes.byref(command), ctypes.sizeof(command)):
                continue
            if not command.Length:
                continue
            raw = ctypes.create_string_buffer(command.Length)
            if not _read(handle, command.Buffer, raw, command.Length):
                continue
            yield pid, raw.raw.decode("utf-16-le", errors="replace")
        finally:
            k32.CloseHandle(handle)


def read_pid_file(pid_file):
    """Read a PID from a file. Returns int or None."""
    try:
        with open(pid_file, "r") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def write_pid_file(pid_file, pid=None):
    """Write *pid* (default ``os.getpid()``) to *pid_file*."""
    if pid is None:
        pid = os.getpid()
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as fh:
        fh.write(str(pid))


def release_pid_file(pid_file, pid=None):
    """Remove *pid_file* only if it still belongs to *pid* (default: us)."""
    if pid is None:
        pid = os.getpid()
    try:
        stored = read_pid_file(pid_file)
        if stored == pid:
            os.remove(pid_file)
    except OSError:
        pass
