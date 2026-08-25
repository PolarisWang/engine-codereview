# launch_review_bot.ps1 — fire-and-forget launcher for the daily review-bot session.
#
# Launched (hidden) by launch_review_bot.vbs from the ClaudeReviewBot scheduled task.
# Stops yesterday's session (stop_review_bot.ps1 -Silent), then starts claude.exe with
# "/review-bot start" and records its PID to cfg\session.pid so a later stop can close
# exactly THAT window — and no other claude.exe you have open — then exits immediately.
#
# The stop half used to be its own 02:00 task. It moved here so the bot keeps running
# overnight (a 3am push or merge is handled instead of waiting for 10:30) and the session
# is recycled at the start of the day instead — the context reset is the point of the
# restart, not the downtime. See DESIGN §1.1.1.
#
# Why fire-and-forget: when the scheduled task launched claude.exe directly, the task
# instance stayed in the Running state for as long as the claude window was open. With
# MultipleInstances=IgnoreNew that made the next day's 10:30 trigger get silently dropped
# whenever a window lingered. By starting claude detached and returning at once, the task
# instance completes in ~1s, so a lingering window can never block the next launch.

$ErrorActionPreference = 'Stop'

$exe = 'C:\Users\muhan.liu\.local\bin\claude.exe'
$cwd = 'E:\rage_review'
$cfg = 'E:\rage_review\.claude\skills\review-bot\cfg'

if (-not (Test-Path $exe)) {
    # No console to show this in (we run hidden) — drop a breadcrumb next to the pid file.
    $errPath = Join-Path $cfg 'session.launch.err'
    Set-Content -Path $errPath -Value "claude.exe not found at $exe" -Encoding ASCII
    exit 1
}

# Stop yesterday's bot first: kill the daemon (before the listener, so its health-check
# can't resurrect it), the listener, the monitor, and the previous claude.exe session
# recorded in session.pid. Runs in-process — the whole stop takes ~2-5s, so the task
# instance still completes quickly enough that a lingering window can't block tomorrow's
# trigger (the fire-and-forget property above is preserved).
#
# -Silent: no "已停止" farewell, because /review-bot start posts its greeting seconds
# later. Failures are non-fatal — a stop that partially fails must not cost us the
# start, and the singleton pid-file guards mean a survivor is a no-op rather than a
# duplicate. $ErrorActionPreference is restored afterwards.
#
# The log APPENDS. It used to be written with Out-File (overwrite), so each
# restart erased the previous one's record and the only question it could
# answer — "did yesterday's session actually die?" — needed the whole history.
$restartLog = Join-Path $cfg 'session.restart.log'
$stopScript = Join-Path $PSScriptRoot 'stop_review_bot.ps1'
if (Test-Path $stopScript) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Add-Content -Path $restartLog -Encoding UTF8 `
                -Value "===== restart $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="
    try {
        & $stopScript -Silent *>&1 | Add-Content -Path $restartLog -Encoding UTF8
    } catch {
        Add-Content -Path $restartLog -Encoding UTF8 `
                    -Value "stop failed: $($_.Exception.Message)"
    }
    $ErrorActionPreference = $prevEap
}

# Start claude detached, in its own (visible) console window, and capture its PID.
#
# -ArgumentList MUST be a single pre-quoted string, NOT an array. Windows PowerShell 5.1
# (which Task Scheduler runs) builds the child command line by joining an array with plain
# spaces and adds NO quoting, so '/review-bot start' as an array element reaches claude.exe
# as two separate argv tokens — claude takes '/review-bot' as the slash command and silently
# drops the 'start' positional, leaving parse_args.py with an empty command. Passing one
# string with the prompt wrapped in embedded double quotes keeps "/review-bot start" intact
# as a single argv token.
$proc = Start-Process -FilePath $exe `
    -ArgumentList '--permission-mode bypassPermissions "/review-bot start"' `
    -WorkingDirectory $cwd `
    -PassThru

# Record the session PID so stop_review_bot.ps1 can surgically kill this exact
# window. This is the belt to resolve_start.py's braces: the session registers
# itself in cfg\sessions.json a few seconds from now (and that is the record
# that also covers hand-started sessions), but if the start aborts before
# reaching that step, this file is all tomorrow's stop has to go on.
$pidPath = Join-Path $cfg 'session.pid'
Set-Content -Path $pidPath -Value ([string]$proc.Id) -Encoding ASCII -NoNewline
Add-Content -Path $restartLog -Encoding UTF8 `
            -Value "[OK] started session PID $($proc.Id) at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
