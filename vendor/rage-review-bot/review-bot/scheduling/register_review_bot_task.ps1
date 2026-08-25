# Registers a per-user Windows Scheduled Task that restarts the review bot
# every day at 08:00 local time.
#
# No admin privileges required. Task runs only when the user is logged in.
#
# This is a RESTART, not just a start: launch_review_bot.ps1 runs
# stop_review_bot.ps1 -Silent first, then starts the new session. The bot runs
# around the clock; 08:00 is only where the parent session's context gets
# recycled, chosen because a code review is very unlikely to be in flight then.
# The separate ClaudeReviewBotStop task is disabled. See DESIGN §1.1.1.
#
# To verify after running:
#   Get-ScheduledTask -TaskName 'ClaudeReviewBot' | Get-ScheduledTaskInfo
# To remove:
#   Unregister-ScheduledTask -TaskName 'ClaudeReviewBot' -Confirm:$false

$ErrorActionPreference = 'Stop'

$exe = 'C:\Users\muhan.liu\.local\bin\claude.exe'
$cwd = 'E:\rage_review'
$vbs = Join-Path $PSScriptRoot 'launch_review_bot.vbs'

if (-not (Test-Path $exe)) {
    Write-Host "ERROR: claude.exe not found at $exe" -ForegroundColor Red
    Write-Host "Edit launch_review_bot.ps1 to point at the correct path, then rerun." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $vbs)) {
    Write-Host "ERROR: launch_review_bot.vbs not found next to this script" -ForegroundColor Red
    exit 1
}

# NOTE: registration goes through schtasks.exe, not Register-ScheduledTask, and
# there is no Get-/Unregister-ScheduledTask pre-check. Every *-ScheduledTask
# cmdlet is CIM-backed, and CIM on this machine intermittently dies with "The
# paging file is too small for this operation to complete" (desktop-heap
# exhaustion — actual memory is fine; the same failure breaks tasklist/taskkill,
# see DESIGN §1.1.2). Worse, that error did not stop this script: it printed
# "[OK] ... registered" while the task still carried its old trigger. schtasks
# talks to the Task Scheduler service directly, and /f makes it idempotent.

# Launch via wscript.exe + VBS wrapper (launch_review_bot.vbs -> hidden powershell ->
# launch_review_bot.ps1). The launcher Start-Process's claude.exe detached, records its
# PID to cfg\session.pid, and returns immediately — so the task instance completes in ~1s
# instead of staying Running for as long as the claude window is open. That, plus
# MultipleInstancesPolicy=StopExisting below, means a lingering window can no longer block
# the next day's trigger.
#
# Registered from raw XML rather than New-ScheduledTaskSettingsSet because that cmdlet's
# enum only accepts Parallel/Queue/IgnoreNew — it cannot express StopExisting at all.
$sid   = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$start = (Get-Date -Hour 8 -Minute 0 -Second 0).ToString('yyyy-MM-ddTHH:mm:ss')
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Restart the review bot (silent stop + /review-bot start) - daily 08:00 (hidden launcher, records session.pid)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>StopExisting</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//nologo "$vbs"</Arguments>
      <WorkingDirectory>$cwd</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

# schtasks /create /xml requires the file to actually be UTF-16 (the encoding the
# XML declaration claims). PS 5.1's 'Unicode' is UTF-16LE with BOM — correct here.
$xmlPath = Join-Path $env:TEMP 'ClaudeReviewBot.task.xml'
Set-Content -Path $xmlPath -Value $xml -Encoding Unicode

$out = & schtasks.exe /create /tn 'ClaudeReviewBot' /xml $xmlPath /f 2>&1
$rc  = $LASTEXITCODE
Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue

if ($rc -ne 0) {
    Write-Host ""
    Write-Host "[FAIL] schtasks exited $rc — the task was NOT registered:" -ForegroundColor Red
    $out | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Press any key to close this window..."
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    exit 1
}

# Read the trigger back rather than trusting the exit code — this is the step
# that previously reported success against an unchanged task.
Write-Host ""
Write-Host "[OK] Scheduled task 'ClaudeReviewBot' registered." -ForegroundColor Green
Write-Host "     Verified from Task Scheduler:" -ForegroundColor Green
& schtasks.exe /query /tn 'ClaudeReviewBot' /fo list /v |
    Select-String -Pattern 'Schedule Type|Start Time|Days|Next Run Time' |
    ForEach-Object { Write-Host "       $($_.Line.Trim())" -ForegroundColor Green }
Write-Host "     Fires: daily 08:00 (Asia/Shanghai, while you are logged in)" -ForegroundColor Green
Write-Host "     Action: wscript.exe `"$vbs`" (-> hidden powershell -> silent stop, then claude.exe /review-bot start)" -ForegroundColor Green
Write-Host "     PID recorded to cfg\session.pid so tomorrow's restart closes this exact window." -ForegroundColor Green
Write-Host ""
Write-Host "Press any key to close this window..."
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
