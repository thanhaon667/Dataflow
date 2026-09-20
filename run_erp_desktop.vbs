' Silent launcher for ERP Desk: starts it with no console window at all, not even
' the brief flash a .bat file shows. Double-click this file, or point a shortcut at it.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
shell.Run """" & root & "\venv\Scripts\pythonw.exe"" -m desktop", 0, False
