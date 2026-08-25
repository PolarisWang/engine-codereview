' launch_review_bot.vbs — run launch_review_bot.ps1 hidden so no PowerShell console flashes.
' Called via: wscript //nologo "launch_review_bot.vbs"  (from the ClaudeReviewBot task)
' WshShell.Run with window style 0 = hidden launcher, False = don't wait. The ps1 then
' Start-Process's claude.exe in its OWN visible console window (same pattern as run_poll.vbs).
Dim WshShell, fso, scriptDir, ps1
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(scriptDir, "launch_review_bot.ps1")
WshShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """", 0, False
