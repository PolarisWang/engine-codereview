' run_poll.vbs — Launch poll_dispatch.py as an event-driven daemon (no console window).
' Called via: wscript "run_poll.vbs"
' --watch mode: scans cfg/events/ every 5s, runs dispatcher only when events arrive
' or merge-tracker periodic check is due (120s). Writes trigger file when work exists.
' WScript.Shell.Run with window style 0 = hidden, False = don't wait (detached).
Dim WshShell, scriptDir, pollScript
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pollScript = fso.BuildPath(scriptDir, "poll_dispatch.py")
WshShell.Run "pythonw """ & pollScript & """ --watch", 0, False
