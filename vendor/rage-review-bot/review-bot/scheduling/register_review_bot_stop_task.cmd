@echo off
REM Double-click this to register the ClaudeReviewBotStop scheduled task.
REM Runs at 01:00 Mon-Sat and kills listener/daemon/monitor via pure PowerShell.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_review_bot_stop_task.ps1"
