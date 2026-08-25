@echo off
REM Double-click this to unregister the review-bot scheduled tasks
REM (ClaudeReviewBot start trigger + ClaudeReviewBotStop stop trigger).
REM
REM Stops the start task first if it is currently running, then removes
REM both registrations. Missing tasks are skipped silently.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& { foreach ($name in 'ClaudeReviewBot','ClaudeReviewBotStop') { $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue; if (-not $t) { Write-Host ('[skip] {0} not registered' -f $name) -ForegroundColor DarkGray; continue }; if ($t.State -eq 'Running') { Write-Host ('[stop] {0} is running -- stopping...' -f $name) -ForegroundColor Yellow; Stop-ScheduledTask -TaskName $name }; Unregister-ScheduledTask -TaskName $name -Confirm:$false; Write-Host ('[OK]   {0} unregistered' -f $name) -ForegroundColor Green }; Write-Host ''; Write-Host 'Press any key to close this window...'; $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') }"
