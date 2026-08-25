# Registers a per-user Windows Scheduled Task that runs stop_review_bot.ps1
# at 01:00 Tue-Sun. No admin, no Claude required.
#
# Pairs with register_review_bot_task.ps1 (the start side). Together they
# form a durable start/stop loop that survives Ctrl+C, window close, reboots.
#
# Verify:  Get-ScheduledTask -TaskName 'ClaudeReviewBotStop' | Get-ScheduledTaskInfo
# Remove:  Unregister-ScheduledTask -TaskName 'ClaudeReviewBotStop' -Confirm:$false

$ErrorActionPreference = 'Stop'

$script = Join-Path $PSScriptRoot 'stop_review_bot.ps1'
$vbs    = Join-Path $PSScriptRoot 'stop_review_bot.vbs'
if (-not (Test-Path $script)) {
    Write-Host "ERROR: stop_review_bot.ps1 not found next to this script" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $vbs)) {
    Write-Host "ERROR: stop_review_bot.vbs not found next to this script" -ForegroundColor Red
    exit 1
}

# Replace existing registration so reruns are idempotent.
$existing = Get-ScheduledTask -TaskName 'ClaudeReviewBotStop' -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Task 'ClaudeReviewBotStop' already exists — replacing..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName 'ClaudeReviewBotStop' -Confirm:$false
}

# Launch via wscript.exe + VBS wrapper so no console window flashes.
# powershell.exe directly would pop a 1-2 second console because Task
# Scheduler runs in the user's interactive session. The VBS wrapper
# uses WshShell.Run with WindowStyle=0 for a fully hidden launch
# (same pattern as scripts/run_poll.vbs in the bot's daemon launcher).
$action = New-ScheduledTaskAction `
    -Execute 'wscript.exe' `
    -Argument "//nologo `"$vbs`""

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday `
    -At 1:00am

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName    'ClaudeReviewBotStop' `
    -Description 'Kill review-bot listener/daemon/monitor — Tue-Sun 01:00' `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings | Out-Null

Write-Host ""
Write-Host "[OK] Scheduled task 'ClaudeReviewBotStop' registered." -ForegroundColor Green
Write-Host "     Fires: Tue-Sun 01:00 (Asia/Shanghai, while you are logged in)" -ForegroundColor Green
Write-Host "     Action: wscript.exe `"$vbs`" (-> hidden powershell -> $script)" -ForegroundColor Green
Write-Host ""
Write-Host "     StartWhenAvailable=true — if machine was asleep at 01:00," -ForegroundColor Green
Write-Host "     the task fires as soon as it wakes." -ForegroundColor Green
Write-Host ""
Write-Host "Press any key to close this window..."
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
