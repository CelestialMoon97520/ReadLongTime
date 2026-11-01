Option Explicit
Dim shell, fs, root, cmd
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
cmd = "pythonw.exe " & Chr(34) & fs.BuildPath(root, "launcher.pyw") & Chr(34)
On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
  Err.Clear
  cmd = "pyw.exe -3 " & Chr(34) & fs.BuildPath(root, "launcher.pyw") & Chr(34)
  shell.Run cmd, 0, False
  If Err.Number <> 0 Then MsgBox "Python 3 is required to start the reader.", 16, "Reader"
End If
