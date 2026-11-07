' Bot Manager 客户端启动器：用隐藏窗口拉起 PowerShell 图形界面
Option Explicit
Dim fso, shell, baseDir, scriptPath, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(baseDir, "BotManagerClient.ps1")

If Not fso.FileExists(scriptPath) Then
    MsgBox "找不到 BotManagerClient.ps1，请把程序解压到完整目录后运行。", 16, "Bot Manager 客户端"
    WScript.Quit 1
End If

command = "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """"
shell.Run command, 0, False
