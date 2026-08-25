' Hidden launcher for stop_review_bot.ps1.
'
' Task Scheduler invokes this via wscript.exe, which has no console.
' WshShell.Run with WindowStyle=0 spawns powershell.exe fully hidden —
' no console flash, no taskbar entry. Mirrors the run_poll.vbs pattern
' used by the bot's daemon launcher.
'
' Args are quoted so paths with spaces survive the round-trip. The
' powershell exit code is preserved via WaitOnReturn=True so the
' Task Scheduler "Last Run Result" still reflects success/failure.

Option Explicit

Dim shell, scriptDir, ps1, cmd, exitCode
Set shell = CreateObject("WScript.Shell")

' Resolve the .ps1 next to this .vbs.
scriptDir = CreateObject("Scripting.FileSystemObject") _
              .GetParentFolderName(WScript.ScriptFullName)
ps1 = scriptDir & "\stop_review_bot.ps1"

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass " _
    & "-WindowStyle Hidden -File """ & ps1 & """"

' WindowStyle=0 (hidden), WaitOnReturn=True (return powershell's exit code).
exitCode = shell.Run(cmd, 0, True)

WScript.Quit exitCode
