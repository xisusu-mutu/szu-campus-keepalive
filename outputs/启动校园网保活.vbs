Option Explicit
Dim shell, fso, folder, launcher, command, result, i, argument
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(folder, "campus_launcher.ps1")
If Not fso.FileExists(launcher) Then
    MsgBox "campus_launcher.ps1 is missing. Extract the complete Windows package.", 16, "Campus Keepalive"
    WScript.Quit 1
End If
command = Quote(shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"))
command = command & " -NoLogo -NoProfile -ExecutionPolicy Bypass -File " & Quote(launcher)
i = 0
Do While i < WScript.Arguments.Count
    argument = WScript.Arguments(i)
    If argument = "--autostart" Then
        command = command & " -Autostart"
    ElseIf argument = "--python" And i + 1 < WScript.Arguments.Count Then
        i = i + 1
        command = command & " -PythonExecutable " & Quote(WScript.Arguments(i))
    End If
    i = i + 1
Loop
shell.CurrentDirectory = folder
On Error Resume Next
result = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    MsgBox "Cannot run the launcher: " & Err.Description & vbCrLf & "Use the diagnostic BAT file.", 16, "Campus Keepalive"
    WScript.Quit 1
End If
On Error GoTo 0
If result <> 0 Then
    MsgBox "Cannot start the application. Run the diagnostic BAT or read data\launcher.log." & vbCrLf & "Python 3.10+ with Tcl/Tk is required; Anaconda is not required.", 16, "Campus Keepalive"
End If
WScript.Quit result

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
