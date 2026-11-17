' Starts watchdog.ps1 without showing a console window and passes its exit code on.
' Task Scheduler runs this file with wscript.exe (see install.ps1). A task that started
' PowerShell directly would flash a window on the screen every hour.
Option Explicit
Dim shell, folder, powershell, command
Set shell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
' Windows does not run script files by default. -ExecutionPolicy Bypass allows it for this one
' process only and changes no setting.
command = """" & powershell & """ -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & folder & "\watchdog.ps1"""
WScript.Quit shell.Run(command, 0, True)
