Set shell = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe"
If Not CreateObject("Scripting.FileSystemObject").FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If
shell.CurrentDirectory = root
arguments = ""
For Each argument In WScript.Arguments
    arguments = arguments & " " & Chr(34) & argument & Chr(34)
Next
shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & root & "\gui_app.py" & Chr(34) & arguments, 0, False
