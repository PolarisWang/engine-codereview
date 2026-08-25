# Pure-shell /review-bot stop. No Claude required.
#
# Reads listener.pid, daemon.pid, monitor.pid from the bot's cfg directory,
# taskkills each, removes the pid/lock/cron files, and posts a farewell
# message to the Lark group via lark-cli.
#
# Two callers:
#   * launch_review_bot.ps1, with -Silent, as the first half of the daily
#     10:30 restart (the nightly stop task is retired — see DESIGN §1.1.1).
#   * by hand, or a re-registered stop task, to actually put the bot to bed.
#
# Takes ~2-5 seconds.

param(
    # Skip the "已停止" farewell. Used by the restart path, where a farewell
    # immediately followed by the greeting is noise — the bot never stopped
    # from the group's point of view.
    [switch]$Silent
)

$ErrorActionPreference = 'Continue'  # don't abort the whole run on one failure

$cfg     = 'E:\rage_review\.claude\skills\review-bot\cfg'
$chatId  = 'oc_cd167b52a521bf1df392b2ccf342f728'
$larkCli = Join-Path $env:APPDATA 'npm\node_modules\@larksuite\cli\scripts\run.js'

# --- 1. Kill the long-lived processes by COMMAND-LINE SCAN, not pid files. ---
# History: this step used to taskkill four recorded PIDs and then delete the pid
# files unconditionally. Both halves failed on this machine. taskkill (like
# tasklist / Get-CimInstance) dies under desktop-heap exhaustion — "The paging
# file is too small for this operation to complete" — and the error was piped to
# Out-Null, so the script deleted the pid file of a process it had NOT killed.
# That made the survivor permanently unreachable: the pid file was its only
# record. The orphaned daemon then respawned a listener on every health check,
# which is how ~70 listeners accumulated between 07-24 and 07-31.
#
# stop_bot.py delegates the same scan to subprocess_util.iter_processes; this
# script is standalone (no Claude, no venv), so it inlines the equivalent via
# .NET Process, which reads from the same kernel structures taskkill cannot.
# See DESIGN §1.1.2.
#
# Kill order is load-bearing: DAEMON FIRST. The daemon runs the listener
# health-check that respawns a dead listener, so killing the listener first
# lets the daemon resurrect it under a new PID.

# Command lines must come from a direct PEB read. Get-CimInstance /
# Get-WmiObject / tasklist all fail here with "The paging file is too small",
# and Process.Path is empty for most processes and never contains the .py
# script name anyway - so a python.exe daemon is indistinguishable from any
# other python.exe without the real command line. Verified 2026-07-31: CIM was
# failing at the moment of writing, which is exactly when a stop must work.
if (-not ('BotProc' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class BotProc
{
    [StructLayout(LayoutKind.Sequential)]
    struct UnicodeString { public ushort Length; public ushort Max; public IntPtr Buffer; }

    [StructLayout(LayoutKind.Sequential)]
    struct ProcessBasicInformation
    {
        public IntPtr Reserved1, PebBaseAddress, R2a, R2b, UniqueProcessId, Reserved3;
    }

    [DllImport("kernel32.dll")]
    static extern IntPtr OpenProcess(int access, bool inherit, int pid);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")]
    static extern bool ReadProcessMemory(IntPtr h, IntPtr addr, byte[] buf, int size, out IntPtr read);
    [DllImport("ntdll.dll")]
    static extern int NtQueryInformationProcess(IntPtr h, int cls, ref ProcessBasicInformation info, int len, IntPtr ret);

    const int QUERY_INFORMATION_AND_VM_READ = 0x0410;
    const int PEB_PROCESS_PARAMETERS = 0x20;   // 64-bit PEB
    const int PARAMS_COMMAND_LINE  = 0x70;     // 64-bit RTL_USER_PROCESS_PARAMETERS

    public static string GetCommandLine(int pid)
    {
        IntPtr h = OpenProcess(QUERY_INFORMATION_AND_VM_READ, false, pid);
        if (h == IntPtr.Zero) return null;
        try
        {
            var info = new ProcessBasicInformation();
            if (NtQueryInformationProcess(h, 0, ref info, Marshal.SizeOf(info), IntPtr.Zero) != 0)
                return null;

            IntPtr read;
            byte[] ptr = new byte[IntPtr.Size];
            if (!ReadProcessMemory(h, (IntPtr)((long)info.PebBaseAddress + PEB_PROCESS_PARAMETERS), ptr, ptr.Length, out read))
                return null;
            long parameters = BitConverter.ToInt64(ptr, 0);

            byte[] us = new byte[Marshal.SizeOf(typeof(UnicodeString))];
            if (!ReadProcessMemory(h, (IntPtr)(parameters + PARAMS_COMMAND_LINE), us, us.Length, out read))
                return null;
            ushort length = BitConverter.ToUInt16(us, 0);
            long buffer = BitConverter.ToInt64(us, 8);
            if (length == 0) return null;

            byte[] raw = new byte[length];
            if (!ReadProcessMemory(h, (IntPtr)buffer, raw, length, out read))
                return null;
            return System.Text.Encoding.Unicode.GetString(raw);
        }
        finally { CloseHandle(h); }
    }
}
'@
}

function Get-BotProcesses {
    # Returns Role/Pid/CommandLine for every live bot process, found by
    # command line rather than by pid file - so orphans from earlier
    # stop/start cycles are reachable, which pid files can never make them.
    $out = @()
    foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
        if ($p.ProcessName -notin @('python', 'pythonw', 'node', 'lark-cli')) { continue }
        $cmd = $null
        try { $cmd = [BotProc]::GetCommandLine($p.Id) } catch { }
        if (-not $cmd) { continue }
        $role = $null
        if ($cmd -match 'poll_dispatch\.py')         { $role = 'daemon'   }
        elseif ($cmd -match 'monitor_dispatch\.py')  { $role = 'monitor'  }
        elseif ($cmd -match 'lark-cli' -and $cmd -match '\+subscribe') { $role = 'listener' }
        if ($role) {
            $out += [pscustomobject]@{ Role = $role; Pid = $p.Id; CommandLine = $cmd }
        }
    }
    return $out
}

$order = @('daemon', 'listener', 'monitor')
$killedCount = 0

for ($pass = 1; $pass -le 5; $pass++) {
    $live = Get-BotProcesses
    if (-not $live) { break }
    foreach ($role in $order) {
        foreach ($proc in ($live | Where-Object { $_.Role -eq $role })) {
            Write-Host "Killing $role PID $($proc.Pid)..."
            try {
                # Stop-Process uses TerminateProcess directly — no console helper,
                # so it survives the desktop-heap failure that breaks taskkill.
                Stop-Process -Id $proc.Pid -Force -ErrorAction Stop
                $killedCount++
            } catch {
                Write-Host "  failed: $($_.Exception.Message)"
            }
        }
    }
    Start-Sleep -Milliseconds 800
}

# --- 1b. Kill EVERY registered claude.exe session, image-guarded. ------------
# Sources, both consulted: cfg\sessions.json (written by resolve_start.py on
# every /review-bot start, scheduled or by hand) and the legacy cfg\session.pid
# (written by launch_review_bot.ps1 only).
#
# History: this step used to read session.pid alone. That file records the
# SCHEDULED session and nothing else, so a hand-started session — how the bot
# returns after every incident recovery — was invisible here and outlived every
# later restart. Sessions 5ce29e73 (started 2026-08-04 02:01, still running
# after three 08:00 restarts) and 096edf7d (2026-08-06 15:04) both did, each
# leaving a second parent session racing the new one over monitor.pid.
# See DESIGN §1.1.5.
#
# Note: run this from inside a bot session's own shell and it will kill that
# session — correct for a stop, but it aborts the rest of this script, so
# prefer the scheduled path or a plain terminal.
$sessionPids = @()

$sessionsFile = Join-Path $cfg 'sessions.json'
if (Test-Path $sessionsFile) {
    try {
        $data = Get-Content -Raw $sessionsFile | ConvertFrom-Json
        foreach ($entry in @($data.sessions)) {
            if ($entry.pid) { $sessionPids += [int]$entry.pid }
        }
    } catch {
        Write-Host "sessions.json unreadable ($($_.Exception.Message)) — falling back to session.pid."
    }
}

$sessionPidFile = Join-Path $cfg 'session.pid'
if (Test-Path $sessionPidFile) {
    $legacy = (Get-Content -Raw $sessionPidFile).Trim()
    if ($legacy -match '^\d+$') { $sessionPids += [int]$legacy }
}

$sessionSurvivors = @()
foreach ($sessionPid in ($sessionPids | Sort-Object -Unique)) {
    $proc = Get-Process -Id $sessionPid -ErrorAction SilentlyContinue
    if (-not $proc) { continue }                      # already gone
    if ($proc.ProcessName -ne 'claude') {
        # A recorded PID that now belongs to something else: the session died
        # and Windows reused its number. Dropping the record is the whole fix.
        Write-Host "session PID $sessionPid is '$($proc.ProcessName)' — skipping (PID reused)."
        continue
    }
    Write-Host "Killing session PID $sessionPid..."
    try {
        Stop-Process -Id $sessionPid -Force -ErrorAction Stop
    } catch {
        Write-Host "  failed: $($_.Exception.Message)"
        $sessionSurvivors += $sessionPid
    }
}

# Keep only records we could NOT kill — a survivor's PID is its only handle,
# so clearing it is what made the old orphans permanently unreachable.
Remove-Item $sessionPidFile -Force -ErrorAction SilentlyContinue
$kept = @(foreach ($p in $sessionSurvivors) {
    [pscustomobject]@{ pid = $p; registered_at = ''; source = 'kill_failed' }
})
$json = [pscustomobject]@{
    sessions   = $kept
    updated_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
} | ConvertTo-Json -Depth 4
Set-Content -Path $sessionsFile -Value $json -Encoding UTF8

# Verify, then clear only the pid files whose process is confirmed dead.
$survivors = Get-BotProcesses
foreach ($name in @('daemon.pid', 'listener.pid', 'monitor.pid')) {
    $path = Join-Path $cfg $name
    if (-not (Test-Path $path)) { continue }
    $recorded = (Get-Content -Raw $path).Trim()
    if ($recorded -and (Get-Process -Id $recorded -ErrorAction SilentlyContinue)) { continue }
    Remove-Item $path -Force -ErrorAction SilentlyContinue
}

# --- 2. Clear stale locks, session-scoped cron id, and scratch _tmp/. --------
Get-ChildItem -Path (Join-Path $cfg 'topics') -Filter '*.lock' -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $cfg 'stop_cron.id') -Force -ErrorAction SilentlyContinue

# _tmp/ sits alongside cfg/ under the skill dir — scratch files dropped by
# topic agents (diffs, review vars, etc.). Safe to wipe on stop; agents
# always write fresh on next start.
$tmpDir = Join-Path (Split-Path $cfg -Parent) '_tmp'
if (Test-Path $tmpDir) {
    Get-ChildItem -Path $tmpDir -File -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# --- 3. Post farewell to the Lark group. -------------------------------------
# Only claim the bot stopped if it actually stopped. Posting the farewell while
# a daemon survives is how the group was told "已停止" on a night the bot kept
# reviewing (2026-07-31).
if ($survivors) {
    Write-Host "[FAIL] $($survivors.Count) process(es) survived:"
    foreach ($s in $survivors) {
        Write-Host "  $($s.Role) PID $($s.Pid): $($s.CommandLine)"
    }
    Write-Host "Pid files for survivors were left in place so a later stop can still find them."
    exit 1
}

if ((-not $Silent) -and (Test-Path $larkCli)) {
    & node $larkCli im +messages-send `
        --as bot `
        --chat-id $chatId `
        --msg-type text `
        --text "🤖 Review Bot 已停止。" 2>&1 | Out-Null
}

Write-Host "[OK] review-bot stopped at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (killed $killedCount process(es))"
exit 0
