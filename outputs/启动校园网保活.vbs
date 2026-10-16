Option Explicit
Dim shell, fso, folder, pythonw, script, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(folder, "campus_keepalive.py")
If Not fso.FileExists(script) Then
    MsgBox "campus_keepalive.py is missing. Keep all application files together.", 16, "Campus Keepalive"
    WScript.Quit 1
End If
If Not fso.FileExists(fso.BuildPath(folder, "campus_core.py")) Then
    MsgBox "campus_core.py is missing. Keep all Python files together.", 16, "Campus Keepalive"
    WScript.Quit 1
End If
If Not fso.FileExists(fso.BuildPath(folder, "campus_tray.py")) Then
    MsgBox "campus_tray.py is missing. Keep all Python files together.", 16, "Campus Keepalive"
    WScript.Quit 1
End If
pythonw = "D:\Anaconda\pythonw.exe"
If fso.FileExists(pythonw) Then
    command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & script & Chr(34)
Else
    command = "pyw -3 " & Chr(34) & script & Chr(34)
End If
shell.CurrentDirectory = folder
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Cannot launch Python. Use the diagnostic BAT file in this folder.", 16, "Campus Keepalive"
End If
