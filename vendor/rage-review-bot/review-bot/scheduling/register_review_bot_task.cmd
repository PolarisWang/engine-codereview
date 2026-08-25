@echo off
REM Double-click this to register the ClaudeReviewBot scheduled task.
REM Runs PowerShell with -ExecutionPolicy Bypass so the .ps1 executes
REM without tripping the default "scripts disabled" policy.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_review_bot_task.ps1"
