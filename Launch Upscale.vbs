Option Explicit
Dim shell, files, folder, command, argument
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File " & Quote(folder & "\launch.ps1")
For Each argument In WScript.Arguments
    command = command & " " & Quote(argument)
Next
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Upscale could not start: " & Err.Description & vbCrLf & "Run Setup.ps1 from this folder to check the installation.", vbCritical, "Upscale"
End If

Function Quote(value)
    Quote = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
