#requires -Version 5.1
<#
    Bot Manager 客户端
    ------------------
    用 SSH 私钥连上服务器，在本机开一条指向 Bot Manager 面板的回环隧道，
    再用浏览器查看面板。配置与密码都保存在本机，密码类字段用 DPAPI 加密。

    用法：
      双击 BotManagerClient.exe（或 run-client.bat）
      无界面自检：powershell -STA -File BotManagerClient.ps1 -SelfTest
#>
param(
    [switch]$SelfTest,
    [switch]$Diagnose,
    [string]$DumpLayout,
    [string]$ResetTarget
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# 控制台输出统一成 UTF-8，避免 -Diagnose 输出在 cmd/管道里变乱码
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }

Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

$Script:AppDir       = Join-Path $env:LOCALAPPDATA 'BotManagerClient'
$Script:SettingsPath = Join-Path $Script:AppDir 'settings.json'
$Script:PidPath      = Join-Path $Script:AppDir 'tunnel.pid'
$Script:LogPath      = Join-Path $Script:AppDir 'ssh.log'
$Script:PanelPort    = 17321
# 面板里「新窗口打开」三个入口用的端口，与 /api/config 的默认值保持一致；
# 这些链接是在本机浏览器里打开的，所以必须由隧道映射到本机同名端口。
$Script:ServiceForwards = @(
    @{ Port = 6185; Label = 'AstrBot' },
    @{ Port = 5099; Label = 'SnowLuma' },
    @{ Port = 6081; Label = 'noVNC' }
)
$Script:ClientVersion = '2.2.4'
# 服务端密码重置脚本（随配套服务一起发布）
$Script:ResetScript = '/opt/bot-manager/current/reset-service-password.py'
$Script:ServiceTargets = @(
    @{ Key = 'astrbot'; Label = 'AstrBot'; Max = 128; Note = '账号固定 astrbot' },
    @{ Key = 'snowluma'; Label = 'SnowLuma'; Max = 128; Note = '账号 admin，只重启 bridge' },
    @{ Key = 'vnc'; Label = 'noVNC / VNC'; Max = 8; Note = 'VNC 限 8 位' }
)
$Script:ForwardNotes  = @()
$Script:TunnelOwned   = $false
$Script:TunnelPid     = 0
$Script:SessionKey   = ''
$Script:Form         = $null
$Script:LogBox       = $null
$Script:StatusLabel  = $null

$Script:SshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
if (-not (Test-Path -LiteralPath $Script:SshExe)) { $Script:SshExe = 'ssh.exe' }
$Script:SshKeygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
$Script:ClientLog = Join-Path $Script:AppDir 'client.log'

function Write-ClientLog([string]$Message) {
    # 双击 exe 时没有控制台，启动失败只能靠这个日志排查
    try {
        if (-not (Test-Path -LiteralPath $Script:AppDir)) {
            New-Item -ItemType Directory -Path $Script:AppDir -Force | Out-Null
        }
        $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
        [IO.File]::AppendAllText($Script:ClientLog, $line + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false)))
    } catch { }
}

trap {
    $detail = ('未处理异常：{0}{1}{2}' -f $_.Exception.Message, [Environment]::NewLine, $_.ScriptStackTrace)
    Write-ClientLog $detail
    try {
        [void][Windows.Forms.MessageBox]::Show(
            ("客户端启动失败，详情见：`r`n{0}`r`n`r`n{1}" -f $Script:ClientLog, $_.Exception.Message),
            'Bot Manager 客户端', 'OK', 'Error')
    } catch { }
    exit 1
}

Write-ClientLog ('启动 v{0}：SelfTest={1} Diagnose={2} DumpLayout={3}' -f $Script:ClientVersion, $SelfTest, $Diagnose, [string]$DumpLayout)

function Initialize-AppDir {
    if (-not (Test-Path -LiteralPath $Script:AppDir)) {
        New-Item -ItemType Directory -Path $Script:AppDir -Force | Out-Null
    }
}

function Get-DefaultSettings {
    return [ordered]@{
        ServerHost             = ''
        ServerPort             = 22
        ServerUser             = 'root'
        KeyPath                = ''
        KeyPassphraseProtected = ''
        PanelUser              = 'admin'
        PanelPasswordProtected = ''
        LocalPort              = 17321
        OpenBrowser            = $true
    }
}

function Protect-Secret([string]$Plain) {
    if ([string]::IsNullOrEmpty($Plain)) { return '' }
    $bytes = [Text.Encoding]::UTF8.GetBytes($Plain)
    $encrypted = [Security.Cryptography.ProtectedData]::Protect(
        $bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    return [Convert]::ToBase64String($encrypted)
}

function Unprotect-Secret([string]$Cipher) {
    if ([string]::IsNullOrEmpty($Cipher)) { return '' }
    try {
        $bytes = [Convert]::FromBase64String($Cipher)
        $plain = [Security.Cryptography.ProtectedData]::Unprotect(
            $bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [Text.Encoding]::UTF8.GetString($plain)
    } catch {
        return ''
    }
}

function Read-Settings {
    $settings = Get-DefaultSettings
    if (Test-Path -LiteralPath $Script:SettingsPath) {
        try {
            $raw = Get-Content -LiteralPath $Script:SettingsPath -Raw -Encoding UTF8
            $saved = $raw | ConvertFrom-Json
            foreach ($name in @($settings.Keys)) {
                $property = $saved.PSObject.Properties[$name]
                if ($null -ne $property -and $null -ne $property.Value) {
                    $settings[$name] = $property.Value
                }
            }
        } catch {
            # 配置损坏时退回默认值，保证客户端总能起来
        }
    }
    return $settings
}

function Write-Settings($Settings) {
    Initialize-AppDir
    $json = $Settings | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($Script:SettingsPath, $json, (New-Object Text.UTF8Encoding($false)))
}

function Write-Log([string]$Message) {
    $line = ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message)
    if ($null -ne $Script:LogBox) {
        $Script:LogBox.AppendText($line + [Environment]::NewLine)
    }
}

function Set-Status([string]$Message) {
    if ($null -ne $Script:StatusLabel) {
        $Script:StatusLabel.Text = $Message
    }
}

function Invoke-SshWithInput([string[]]$Arguments, [string]$InputText) {
    # 密码走 stdin，不要放进命令行参数（远端 ps 能看到 argv）
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Script:SshExe
    $info.Arguments = (Join-Arguments $Arguments)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    $errorTask = $process.StandardError.ReadToEndAsync()
    try {
        $process.StandardInput.Write($InputText)
        $process.StandardInput.Close()
    } catch { }
    $output = $process.StandardOutput.ReadToEnd()
    $errors = $errorTask.Result
    $process.WaitForExit()
    $code = $process.ExitCode
    $process.Dispose()
    return @{ Code = $code; Text = (($output + $errors).Trim()) }
}

function Show-PasswordDialog([string]$Title, [string]$Hint, [int]$MaxLength) {
    $dialog = New-Object Windows.Forms.Form
    $dialog.Text = $Title
    $dialog.ClientSize = New-Object Drawing.Size(420, 190)
    $dialog.StartPosition = 'CenterParent'
    $dialog.FormBorderStyle = 'FixedDialog'
    $dialog.MaximizeBox = $false
    $dialog.MinimizeBox = $false

    $labelHint = New-Object Windows.Forms.Label
    $labelHint.Text = $Hint
    $labelHint.Location = New-Object Drawing.Point(14, 12)
    $labelHint.Size = New-Object Drawing.Size(392, 34)
    $dialog.Controls.Add($labelHint)

    $labelNew = New-Object Windows.Forms.Label
    $labelNew.Text = '新密码'
    $labelNew.Location = New-Object Drawing.Point(14, 52)
    $labelNew.Size = New-Object Drawing.Size(60, 20)
    $dialog.Controls.Add($labelNew)
    $boxNew = New-Object Windows.Forms.TextBox
    $boxNew.Location = New-Object Drawing.Point(80, 49)
    $boxNew.Size = New-Object Drawing.Size(320, 24)
    $boxNew.UseSystemPasswordChar = $true
    $boxNew.MaxLength = $MaxLength
    $dialog.Controls.Add($boxNew)

    $labelAgain = New-Object Windows.Forms.Label
    $labelAgain.Text = '再输一次'
    $labelAgain.Location = New-Object Drawing.Point(14, 88)
    $labelAgain.Size = New-Object Drawing.Size(60, 20)
    $dialog.Controls.Add($labelAgain)
    $boxAgain = New-Object Windows.Forms.TextBox
    $boxAgain.Location = New-Object Drawing.Point(80, 85)
    $boxAgain.Size = New-Object Drawing.Size(320, 24)
    $boxAgain.UseSystemPasswordChar = $true
    $boxAgain.MaxLength = $MaxLength
    $dialog.Controls.Add($boxAgain)

    $labelTip = New-Object Windows.Forms.Label
    $labelTip.Text = ('至少 8 位，不能含空格' + $(if ($MaxLength -le 8) { '；该服务上限 8 位' } else { '' }))
    $labelTip.Location = New-Object Drawing.Point(80, 112)
    $labelTip.Size = New-Object Drawing.Size(320, 20)
    $labelTip.ForeColor = [Drawing.Color]::FromArgb(110, 110, 110)
    $dialog.Controls.Add($labelTip)

    $okButton = New-Object Windows.Forms.Button
    $okButton.Text = '确定'
    $okButton.Location = New-Object Drawing.Point(238, 146)
    $okButton.Size = New-Object Drawing.Size(76, 28)
    $okButton.DialogResult = 'OK'
    $dialog.Controls.Add($okButton)

    $cancelButton = New-Object Windows.Forms.Button
    $cancelButton.Text = '取消'
    $cancelButton.Location = New-Object Drawing.Point(324, 146)
    $cancelButton.Size = New-Object Drawing.Size(76, 28)
    $cancelButton.DialogResult = 'Cancel'
    $dialog.Controls.Add($cancelButton)

    $dialog.AcceptButton = $okButton
    $dialog.CancelButton = $cancelButton

    $answer = $null
    while ($true) {
        $result = $dialog.ShowDialog($Script:Form)
        if ($result -ne 'OK') { break }
        $value = $boxNew.Text
        if ($value.Length -lt 8) {
            [Windows.Forms.MessageBox]::Show('密码至少 8 位。', '密码太短', 'OK', 'Warning') | Out-Null
            continue
        }
        if ($value.Length -gt $MaxLength) {
            [Windows.Forms.MessageBox]::Show(('该服务密码最多 {0} 位。' -f $MaxLength), '密码太长', 'OK', 'Warning') | Out-Null
            continue
        }
        if ($value -match '\s') {
            [Windows.Forms.MessageBox]::Show('密码不能包含空格等空白字符。', '格式不支持', 'OK', 'Warning') | Out-Null
            continue
        }
        if ($value -ne $boxAgain.Text) {
            [Windows.Forms.MessageBox]::Show('两次输入不一致。', '确认失败', 'OK', 'Warning') | Out-Null
            continue
        }
        $answer = $value
        break
    }
    $dialog.Dispose()
    return $answer
}

function Invoke-ResetRemote($Settings, $Target, [string]$Password) {
    # 通过 SSH 调用服务器上的重置脚本；密码走 stdin，返回 @{Ok, Message, Detail}
    $identity = Resolve-IdentityFile $Settings
    $remote = 'python3 {0} --target {1}' -f $Script:ResetScript, $Target.Key
    $arguments = @(Get-SshArgs $Settings $identity)
    $arguments += @(('{0}@{1}' -f $Settings.ServerUser, $Settings.ServerHost), $remote)
    $result = Invoke-SshWithInput $arguments ($Password + "`n")

    $payload = $null
    foreach ($line in ($result.Text -split "`r?`n")) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith('{') -and $trimmed.EndsWith('}')) {
            try { $payload = $trimmed | ConvertFrom-Json } catch { }
        }
    }
    if ($null -eq $payload) {
        $detail = $result.Text
        if (-not $detail) { $detail = ('ssh 退出码 {0}' -f $result.Code) }
        return @{ Ok = $false; Message = '没有拿到服务端结果'; Detail = $detail }
    }
    return @{
        Ok = [bool]$payload.ok
        Message = [string]$payload.message
        Detail = [string]$payload.detail
    }
}

function Reset-ServicePassword($Target) {
    $settings = Read-FormSettings
    if (-not (Assert-Inputs $settings)) { return }

    $hint = ('重置「{0}」的登录密码。{1}。' -f $Target.Label, $Target.Note)
    $password = Show-PasswordDialog ('重置 {0} 密码' -f $Target.Label) $hint $Target.Max
    if ([string]::IsNullOrEmpty($password)) {
        Write-Log ('已取消重置 {0}。' -f $Target.Label)
        return
    }

    $Script:Form.Cursor = [Windows.Forms.Cursors]::WaitCursor
    Set-Status ('正在重置 {0} 密码…' -f $Target.Label)
    try {
        $outcome = Invoke-ResetRemote $settings $Target $password
        Write-Log ('[{0}] {1}' -f $Target.Label, $outcome.Message)
        if ($outcome.Detail) { Write-Log ('  ' + $outcome.Detail) }
        if ($outcome.Ok) {
            Set-Status ('{0} 密码已重置' -f $Target.Label)
            $body = $outcome.Message
            if ($outcome.Detail) { $body += "`r`n`r`n" + $outcome.Detail }
            $body += "`r`n`r`n新密码就是你刚输入的那个，请到对应页面用新密码登录。"
            [Windows.Forms.MessageBox]::Show($body, ('{0} 密码已重置' -f $Target.Label), 'OK', 'Information') | Out-Null
        } else {
            Set-Status ('{0} 重置失败' -f $Target.Label)
            $body = "服务端返回失败：`r`n`r`n" + $outcome.Message
            if ($outcome.Detail) { $body += "`r`n`r`n" + $outcome.Detail }
            [Windows.Forms.MessageBox]::Show($body, '重置失败', 'OK', 'Warning') | Out-Null
        }
    } catch {
        Write-Log ('重置 {0} 出错：{1}' -f $Target.Label, $_.Exception.Message)
        Set-Status ('{0} 重置异常' -f $Target.Label)
        [Windows.Forms.MessageBox]::Show($_.Exception.Message, '重置出错', 'OK', 'Error') | Out-Null
    } finally {
        $Script:Form.Cursor = [Windows.Forms.Cursors]::Default
    }
}

function Test-PortListening([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(600)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-PortListening([int]$Port, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

# Start-Process 只接受一整条命令行，带空格的路径必须自己加引号
function Quote-Argument([string]$Value) {
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Join-Arguments([string[]]$Arguments) {
    $parts = @()
    foreach ($item in $Arguments) {
        $parts += (Quote-Argument $item)
    }
    return ($parts -join ' ')
}

function Get-SshArgs($Settings, [string]$IdentityPath) {
    $knownHosts = Join-Path $Script:AppDir 'known_hosts'
    $arguments = @(
        '-p', "$($Settings.ServerPort)",
        '-o', 'ConnectTimeout=10',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', ('UserKnownHostsFile={0}' -f $knownHosts),
        '-o', 'BatchMode=yes',
        '-o', 'LogLevel=ERROR'
    )
    if (-not [string]::IsNullOrWhiteSpace($IdentityPath)) {
        $arguments += @('-i', $IdentityPath)
    }
    return $arguments
}

function Resolve-IdentityFile($Settings) {
    # 口令私钥：Windows 上 ssh 没法可靠地走 askpass（提示串会被拼进命令行），
    # 所以用 ssh-keygen 在会话目录解密出一份临时副本，断开或退出时立刻删除。
    $passphrase = Unprotect-Secret $Settings.KeyPassphraseProtected
    if ([string]::IsNullOrEmpty($passphrase)) { return $Settings.KeyPath }
    if (-not [string]::IsNullOrWhiteSpace($Script:SessionKey) -and (Test-Path -LiteralPath $Script:SessionKey)) {
        return $Script:SessionKey
    }
    Initialize-AppDir
    $temporary = Join-Path $Script:AppDir ('session-key-' + [Guid]::NewGuid().ToString('N'))
    Copy-Item -LiteralPath $Settings.KeyPath -Destination $temporary -Force
    try {
        $acl = Get-Acl -LiteralPath $temporary
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            ('{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME), 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $temporary -AclObject $acl
    } catch {
        # 收紧 ACL 失败不阻断使用
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # PowerShell 5.1 不会把空字符串传给原生程序，空口令要写成 ""
        & $Script:SshKeygen -p -f $temporary -P $passphrase -N '""' 2>&1 | Out-Null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        throw '私钥口令不正确，或这把私钥的格式不受支持。'
    }
    $Script:SessionKey = $temporary
    return $temporary
}

function Clear-SessionKey {
    if (-not [string]::IsNullOrWhiteSpace($Script:SessionKey) -and (Test-Path -LiteralPath $Script:SessionKey)) {
        Remove-Item -LiteralPath $Script:SessionKey -Force -ErrorAction SilentlyContinue
    }
    $Script:SessionKey = ''
}

function Invoke-SshCapture([string[]]$Arguments) {
    # 不用 Start-Process 重定向：环境里同时有 HTTP_PROXY / http_proxy 时 PS 5.1 会抛字典重复键
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Script:SshExe
    $info.Arguments = (Join-Arguments $Arguments)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    $errorTask = $process.StandardError.ReadToEndAsync()
    $output = $process.StandardOutput.ReadToEnd()
    $errors = $errorTask.Result
    $process.WaitForExit()
    $code = $process.ExitCode
    $process.Dispose()
    return @{ Code = $code; Text = (($output + $errors).Trim()) }
}

function Test-RemoteConnection($Settings) {
    $target = '{0}@{1}' -f $Settings.ServerUser, $Settings.ServerHost
    $identity = Resolve-IdentityFile $Settings
    $arguments = @(Get-SshArgs $Settings $identity)
    $arguments += @($target, 'echo BOTMGR_OK')
    $result = Invoke-SshCapture $arguments
    $text = $result.Text
    if ($text -match 'BOTMGR_OK') {
        return @{ Ok = $true; Message = '连接成功：这个私钥可以用。' }
    }
    $hint = '连接失败，详见下方 SSH 输出。'
    if ($text -match 'Permission denied') {
        $hint = '连接失败：服务器拒绝了这个私钥。请确认它的公钥已装进服务器 ~/.ssh/authorized_keys。'
    } elseif ($text -match 'incorrect passphrase|Load key|invalid format|error in libcrypto') {
        $hint = '连接失败：私钥解不开。私钥若有口令，请在上方「私钥口令」里填写。'
    } elseif ($text -match 'Connection refused|timed out|No route|Could not resolve') {
        $hint = '连接失败：网络不通。检查服务器地址、端口与本机网络。'
    } elseif ($text -match 'REMOTE HOST IDENTIFICATION HAS CHANGED') {
        $hint = '连接被拒绝：服务器主机密钥变了。确认是本人操作后，删除配置目录里的 known_hosts 再试。'
    }
    if ($text) {
        $hint += [Environment]::NewLine + [Environment]::NewLine + 'SSH 输出：' + [Environment]::NewLine + $text
    }
    return @{ Ok = $false; Message = $hint }
}

function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        return @{ Pid = [int]$conn.OwningProcess; Name = [string]$proc.ProcessName }
    } catch {
        return $null
    }
}

function Start-Tunnel($Settings, [string]$IdentityPath) {
    Initialize-AppDir
    $target = '{0}@{1}' -f $Settings.ServerUser, $Settings.ServerHost
    $arguments = @(Get-SshArgs $Settings $IdentityPath)
    $arguments += @('-N', '-L', ('127.0.0.1:{0}:127.0.0.1:{1}' -f $Settings.LocalPort, $Script:PanelPort))
    $Script:ForwardNotes = @()
    foreach ($item in $Script:ServiceForwards) {
        if ($item.Port -eq $Settings.LocalPort) { continue }
        $owner = Get-PortOwner $item.Port
        if ($owner) {
            # 本机这个端口被别的程序占了：跳过它，别把整条隧道拖垮
            $Script:ForwardNotes += ('{0}（{1}）本机端口被 {2}(PID {3}) 占用，已跳过' -f $item.Label, $item.Port, $owner.Name, $owner.Pid)
            continue
        }
        $arguments += @('-L', ('127.0.0.1:{0}:127.0.0.1:{0}' -f $item.Port))
    }
    $arguments += @(
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-E', $Script:LogPath
    )
    $arguments += @($target)

    $commandLine = Join-Arguments $arguments
    $process = Start-Process -FilePath $Script:SshExe -ArgumentList $commandLine `
        -WindowStyle Hidden -PassThru
    if ($null -ne $process) {
        $Script:TunnelPid = $process.Id
        [IO.File]::WriteAllText($Script:PidPath, "$($process.Id)", (New-Object Text.UTF8Encoding($false)))
    }
    return $process
}

function Stop-Tunnel {
    $candidates = @()
    if ($Script:TunnelPid -gt 0) { $candidates += $Script:TunnelPid }
    if (Test-Path -LiteralPath $Script:PidPath) {
        try {
            $saved = (Get-Content -LiteralPath $Script:PidPath -Raw).Trim()
            if ($saved -match '^\d+$') { $candidates += [int]$saved }
        } catch { }
    }
    $stopped = 0
    foreach ($processId in ($candidates | Select-Object -Unique)) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -ne $process -and $process.ProcessName -like 'ssh*') {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    }
    $Script:TunnelPid = 0
    if (Test-Path -LiteralPath $Script:PidPath) {
        Remove-Item -LiteralPath $Script:PidPath -Force -ErrorAction SilentlyContinue
    }
    Clear-SessionKey
    return $stopped
}

function Get-TunnelLogTail([int]$Lines) {
    if (-not (Test-Path -LiteralPath $Script:LogPath)) { return '' }
    try {
        return ((Get-Content -LiteralPath $Script:LogPath -Tail $Lines -ErrorAction SilentlyContinue) -join [Environment]::NewLine)
    } catch {
        return ''
    }
}

function Test-PanelHealth([int]$Port) {
    try {
        $response = Invoke-WebRequest -Uri ('http://127.0.0.1:{0}/api/health' -f $Port) `
            -UseBasicParsing -TimeoutSec 6
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Show-TextDialog([string]$Title, [string]$Text) {
    $dialog = New-Object Windows.Forms.Form
    $dialog.Text = $Title
    $dialog.Size = New-Object Drawing.Size(640, 320)
    $dialog.StartPosition = 'CenterParent'
    $dialog.MinimizeBox = $false
    $box = New-Object Windows.Forms.TextBox
    $box.Multiline = $true
    $box.ReadOnly = $true
    $box.ScrollBars = 'Vertical'
    $box.Dock = 'Fill'
    $box.Font = New-Object Drawing.Font('Consolas', 9)
    $box.Text = $Text
    $dialog.Controls.Add($box)
    $dialog.ShowDialog($Script:Form) | Out-Null
    $dialog.Dispose()
}

function New-KeyPair($Settings) {
    $dialog = New-Object Windows.Forms.SaveFileDialog
    $dialog.Title = '选择新私钥的保存位置'
    $dialog.Filter = '所有文件 (*.*)|*.*'
    $dialog.FileName = 'id_ed25519'
    $dialog.InitialDirectory = $Script:AppDir
    if ($dialog.ShowDialog($Script:Form) -ne 'OK') {
        $dialog.Dispose()
        return $null
    }
    $target = $dialog.FileName
    $dialog.Dispose()

    if (Test-Path -LiteralPath $target) {
        [Windows.Forms.MessageBox]::Show('该文件已存在，请换个名字或先删除。', '生成密钥', 'OK', 'Warning') | Out-Null
        return $null
    }
    if (-not (Test-Path -LiteralPath $Script:SshKeygen)) {
        [Windows.Forms.MessageBox]::Show('系统里找不到 ssh-keygen.exe。', '生成密钥', 'OK', 'Error') | Out-Null
        return $null
    }
    $passphrase = Unprotect-Secret $Settings.KeyPassphraseProtected
    $keygenPass = $passphrase
    if ([string]::IsNullOrEmpty($keygenPass)) { $keygenPass = '""' }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Script:SshKeygen -t ed25519 -f $target -N $keygenPass -C 'bot-manager-client' 2>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $previous
    }
    if (-not (Test-Path -LiteralPath "$target.pub")) {
        [Windows.Forms.MessageBox]::Show('生成失败，请检查目录是否可写。', '生成密钥', 'OK', 'Error') | Out-Null
        return $null
    }
    $public = [IO.File]::ReadAllText("$target.pub").Trim()
    [Windows.Forms.Clipboard]::SetText($public)
    Write-Log "已生成新密钥：$target（公钥已复制到剪贴板）"
    Show-TextDialog '公钥已生成（已复制到剪贴板）' $public
    return $target
}

function Install-PublicKey($Settings) {
    if ([string]::IsNullOrWhiteSpace($Settings.KeyPath) -or -not (Test-Path -LiteralPath "$($Settings.KeyPath).pub")) {
        [Windows.Forms.MessageBox]::Show('请先选择私钥文件，并且同目录下要有对应的 .pub 公钥。', '安装公钥', 'OK', 'Warning') | Out-Null
        return
    }
    $remote = 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
    $command = 'type "{0}.pub" | ssh -p {1} -o PreferredAuthentications=password -o PubkeyAuthentication=no {2}@{3} "{4}" & echo. & echo 若上面没有报错，公钥已装好，可以关闭本窗口。 & pause' -f `
        $Settings.KeyPath, $Settings.ServerPort, $Settings.ServerUser, $Settings.ServerHost, $remote
    Write-Log '已打开安装公钥窗口：需要输入服务器登录密码。'
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $command | Out-Null
}

function Copy-PanelPassword($Settings) {
    $password = Unprotect-Secret $Settings.PanelPasswordProtected
    if ([string]::IsNullOrEmpty($password)) {
        Write-Log '面板密码为空，无需复制。'
        return
    }
    [Windows.Forms.Clipboard]::SetText($password)
    Write-Log '面板密码已复制到剪贴板，浏览器登录框可直接粘贴（Ctrl+V）。'
}

function Show-TunnelFailure([string]$Reason) {
    $tail = Get-TunnelLogTail 12
    $message = $Reason
    if ($tail) {
        $message += [Environment]::NewLine + [Environment]::NewLine + 'SSH 输出：' + [Environment]::NewLine + $tail
    }
    [Windows.Forms.MessageBox]::Show($message, '连接失败', 'OK', 'Error') | Out-Null
    Set-Status '未连接'
    Write-Log $Reason
    Stop-Tunnel | Out-Null
}

# ---------------------------------------------------------------- 自检
$Script:SelfTestResults = New-Object Collections.ArrayList

function Add-SelfTestResult([string]$Name, [bool]$Ok, [string]$Detail) {
    $Script:SelfTestResults.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail }) | Out-Null
}

function Invoke-SelfTest {
    Initialize-AppDir
    $backup = $null
    if (Test-Path -LiteralPath $Script:SettingsPath) {
        $backup = [IO.File]::ReadAllBytes($Script:SettingsPath)
    }
    try {
        $settings = Get-DefaultSettings
        $settings.ServerHost = 'example.local'
        $settings.ServerPort = 2222
        $settings.PanelPasswordProtected = Protect-Secret 'plain-secret'
        $settings.KeyPassphraseProtected = Protect-Secret 'key-pass'
        Write-Settings $settings
        $loaded = Read-Settings
        Add-SelfTestResult '设置读写往返' ($loaded.ServerHost -eq 'example.local' -and $loaded.ServerPort -eq 2222) ('host={0} port={1}' -f $loaded.ServerHost, $loaded.ServerPort)
        Add-SelfTestResult 'DPAPI 解密（面板密码）' ((Unprotect-Secret $loaded.PanelPasswordProtected) -eq 'plain-secret') 'roundtrip'
        Add-SelfTestResult 'DPAPI 解密（私钥口令）' ((Unprotect-Secret $loaded.KeyPassphraseProtected) -eq 'key-pass') 'roundtrip'
        $disk = [IO.File]::ReadAllText($Script:SettingsPath)
        Add-SelfTestResult '配置不落明文' (($disk -notmatch 'plain-secret') -and ($disk -notmatch 'key-pass')) 'ciphertext only'
        Add-SelfTestResult '空值加密仍为空' ((Protect-Secret '') -eq '') 'empty'
        Add-SelfTestResult '错误密文安全降级' ((Unprotect-Secret 'not-a-cipher') -eq '') 'returns empty'

        $arguments = @('-p', '22', '-o', 'UserKnownHostsFile=C:\path with space\known_hosts', '-i', 'C:\my keys\id.pem', 'root@host')
        $line = Join-Arguments $arguments
        $quoted = $line -match '"UserKnownHostsFile=C:\\path with space\\known_hosts"' -and $line -match '"C:\\my keys\\id.pem"' -and $line -match 'root@host' -and $line -match '-p 22'
        Add-SelfTestResult '参数引号拼接' $quoted $line

        Add-SelfTestResult 'ssh.exe 存在' (Test-Path -LiteralPath $Script:SshExe) $Script:SshExe
        Add-SelfTestResult 'ssh-keygen 存在' (Test-Path -LiteralPath $Script:SshKeygen) $Script:SshKeygen

        $port = 17399
        Add-SelfTestResult '端口检测：空闲' (-not (Test-PortListening -Port $port)) "port $port"
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $port)
        $listener.Start()
        $detected = Wait-PortListening -Port $port -TimeoutSeconds 3
        $listener.Stop()
        Add-SelfTestResult '端口检测：监听中' $detected "port $port"

        $encryptedKey = Join-Path $Script:AppDir 'selftest-key'
        $pubBackup = ''
        foreach ($item in @($encryptedKey, "$encryptedKey.pub")) {
            if ([IO.File]::Exists($item)) { [IO.File]::Delete($item) }
        }
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $Script:SshKeygen -t ed25519 -f $encryptedKey -N 'pass-phrase' -C 'selftest' 2>&1 | Out-Null
            $keygenOk = [IO.File]::Exists($encryptedKey)
            Add-SelfTestResult '生成加密私钥' $keygenOk $encryptedKey

            if ($keygenOk) {
                $keySettings = Get-DefaultSettings
                $keySettings.KeyPath = $encryptedKey
                $keySettings.KeyPassphraseProtected = Protect-Secret 'pass-phrase'
                $resolved = Resolve-IdentityFile $keySettings
                Add-SelfTestResult '口令私钥解密' (([IO.File]::Exists($resolved)) -and ($resolved -ne $encryptedKey)) $resolved
                $derived = (& $Script:SshKeygen -y -f $resolved 2>&1 | Out-String).Trim()
                $published = ([IO.File]::ReadAllText("$encryptedKey.pub")).Trim()
                $derivedParts = ($derived -split ' ')[0..1] -join ' '
                $publishedParts = ($published -split ' ')[0..1] -join ' '
                Add-SelfTestResult '私钥与公钥匹配' ($derivedParts -eq $publishedParts) 'derived vs published'

                Clear-SessionKey
                Add-SelfTestResult '会话私钥已清理' (-not [IO.File]::Exists($resolved)) 'removed'

                $wrongSettings = Get-DefaultSettings
                $wrongSettings.KeyPath = $encryptedKey
                $wrongSettings.KeyPassphraseProtected = Protect-Secret 'wrong-pass'
                $threw = $false
                try {
                    Resolve-IdentityFile $wrongSettings | Out-Null
                } catch {
                    $threw = $true
                }
                Clear-SessionKey
                Add-SelfTestResult '错误口令被拒绝' $threw 'throws'
            }
        } finally {
            $ErrorActionPreference = $previous
            foreach ($item in @($encryptedKey, "$encryptedKey.pub")) {
                if ([IO.File]::Exists($item)) { [IO.File]::Delete($item) }
            }
        }

        $ErrorActionPreference = 'Continue'
        try {
            $version = (& $Script:SshExe -V 2>&1) -join ' '
        } finally {
            $ErrorActionPreference = $previous
        }
        Add-SelfTestResult 'ssh 版本可见' ($version -match 'OpenSSH') $version.Trim()
    } finally {
        if ($null -ne $backup) {
            [IO.File]::WriteAllBytes($Script:SettingsPath, $backup)
        } elseif (Test-Path -LiteralPath $Script:SettingsPath) {
            Remove-Item -LiteralPath $Script:SettingsPath -Force -ErrorAction SilentlyContinue
        }
    }

    $failed = 0
    foreach ($item in $Script:SelfTestResults) {
        $flag = 'OK  '
        if (-not $item.Ok) { $flag = 'FAIL'; $failed++ }
        Write-Host ('[{0}] {1} :: {2}' -f $flag, $item.Name, $item.Detail)
    }
    Write-Host ('自检完成：{0} 项通过，{1} 项失败' -f ($Script:SelfTestResults.Count - $failed), $failed)
    if ($failed -gt 0) { exit 1 }
    exit 0
}

if ($SelfTest) {
    Invoke-SelfTest
}

# 诊断：读现有配置，跑一次「测试连接 + 建隧道 + 探面板」，不进界面
function Write-Diag($Message) {
    # 诊断模式既打印到控制台，也落进 client.log，避免管道编码把中文弄乱
    Write-Host $Message
    Write-ClientLog ([string]$Message)
}

function Invoke-Diagnose {
    Initialize-AppDir
    $settings = Read-Settings
    if ([string]::IsNullOrWhiteSpace($settings.ServerHost)) {
        Write-Diag '配置里没有服务器地址，先开界面填一次并保存。'
        exit 2
    }
    Write-Diag ('服务器：{0}@{1}:{2}' -f $settings.ServerUser, $settings.ServerHost, $settings.ServerPort)
    Write-Diag ('私钥：{0}' -f $settings.KeyPath)
    Write-Diag ('本地端口：{0} → 面板 {1}' -f $settings.LocalPort, $Script:PanelPort)
    $ok = $false
    try {
        $result = Test-RemoteConnection $settings
        Write-Diag ('[连接测试] {0}' -f $result.Message)
        if (-not $result.Ok) { return 1 }

        Stop-Tunnel | Out-Null
        $identity = Resolve-IdentityFile $settings
        $process = Start-Tunnel $settings $identity
        Write-Diag ('[隧道] SSH PID {0}' -f $process.Id)
        if (-not (Wait-PortListening -Port $settings.LocalPort -TimeoutSeconds 20)) {
            Write-Diag '[隧道] 失败：本地端口未监听'
            Write-Diag (Get-TunnelLogTail 15)
            return 1
        }
        Write-Diag '[隧道] 本地端口已监听'
        foreach ($note in $Script:ForwardNotes) { Write-Diag ('[隧道] ' + $note) }
        foreach ($item in $Script:ServiceForwards) {
            $state = '未监听'
            if (Test-PortListening -Port $item.Port) { $state = '已转发' }
            $probe = ''
            if ($state -eq '已转发') {
                try {
                    $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:{0}/' -f $item.Port) `
                        -UseBasicParsing -TimeoutSec 6
                    $probe = 'HTTP {0}' -f $resp.StatusCode
                } catch {
                    $probe = '请求失败（服务未启动？）'
                }
            }
            Write-Diag ('[转发] {0}({1}) -> {2} {3}' -f $item.Label, $item.Port, $state, $probe)
        }
        if (Test-PanelHealth -Port $settings.LocalPort) {
            Write-Diag '[面板] /api/health 200，链路可用'
            $ok = $true
        } else {
            Write-Diag '[面板] 无响应'
            Write-Diag (Get-TunnelLogTail 15)
        }
        return $(if ($ok) { 0 } else { 1 })
    } catch {
        Write-Diag ('[异常] {0}' -f $_.Exception.Message)
        return 1
    } finally {
        Stop-Tunnel | Out-Null
        Clear-SessionKey
        Write-Diag '[隧道] 已停止，会话私钥已清理'
    }
}

if ($Diagnose) {
    exit (Invoke-Diagnose)
}

# 无界面重置：新密码从标准输入读一行，输出 JSON（供脚本/自动化使用）
if ($ResetTarget) {
    $target = $Script:ServiceTargets | Where-Object { $_.Key -eq $ResetTarget } | Select-Object -First 1
    if ($null -eq $target) {
        Write-Output (ConvertTo-Json @{ ok = $false; target = $ResetTarget; message = '未知目标' } -Compress)
        exit 2
    }
    $settings = Read-Settings
    $password = [Console]::In.ReadLine()
    if ([string]::IsNullOrEmpty($password)) {
        Write-Output (ConvertTo-Json @{ ok = $false; target = $ResetTarget; message = '密码为空' } -Compress)
        exit 2
    }
    $outcome = Invoke-ResetRemote $settings $target $password
    Write-Output (ConvertTo-Json @{
        ok = $outcome.Ok; target = $ResetTarget
        message = $outcome.Message; detail = $outcome.Detail
    } -Compress)
    if ($outcome.Ok) { exit 0 } else { exit 1 }
}

# ---------------------------------------------------------------- 界面
Initialize-AppDir
Write-ClientLog '界面初始化……'
$Script:Settings = Read-Settings

$Script:Form = New-Object Windows.Forms.Form
$Script:Form.Text = ('Bot Manager 客户端 {0}' -f $Script:ClientVersion)
$Script:Form.ClientSize = New-Object Drawing.Size(628, 592)
$Script:Form.StartPosition = 'CenterScreen'
$Script:Form.FormBorderStyle = 'FixedSingle'
$Script:Form.MaximizeBox = $false
$Script:Form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 9)

function Add-Label([string]$Text, [int]$X, [int]$Y, [int]$Width) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $Text
    $label.Location = New-Object Drawing.Point($X, $Y)
    $label.Size = New-Object Drawing.Size($Width, 20)
    $Script:Form.Controls.Add($label)
    return $label
}

function Add-TextBox([int]$X, [int]$Y, [int]$Width, [bool]$Password) {
    $box = New-Object Windows.Forms.TextBox
    $box.Location = New-Object Drawing.Point($X, $Y)
    $box.Size = New-Object Drawing.Size($Width, 24)
    if ($Password) { $box.UseSystemPasswordChar = $true }
    $Script:Form.Controls.Add($box)
    return $box
}

function Add-Button([string]$Text, [int]$X, [int]$Y, [int]$Width) {
    $button = New-Object Windows.Forms.Button
    $button.Text = $Text
    $button.Location = New-Object Drawing.Point($X, $Y)
    $button.Size = New-Object Drawing.Size($Width, 30)
    $Script:Form.Controls.Add($button)
    return $button
}

Add-Label '服务器' 12 17 52 | Out-Null
$Script:HostBox = Add-TextBox 70 14 158 $false
Add-Label '端口' 240 17 36 | Out-Null
$Script:PortBox = Add-TextBox 278 14 60 $false

Add-Label '用户名' 12 50 52 | Out-Null
$Script:UserBox = Add-TextBox 70 47 158 $false

Add-Label '私钥文件' 12 83 60 | Out-Null
$Script:KeyBox = Add-TextBox 70 80 322 $false
$Script:BrowseButton = New-Object Windows.Forms.Button
$Script:BrowseButton.Text = '浏览…'
$Script:BrowseButton.Location = New-Object Drawing.Point(398, 78)
$Script:BrowseButton.Size = New-Object Drawing.Size(88, 26)
$Script:Form.Controls.Add($Script:BrowseButton)
$Script:GenKeyButton = New-Object Windows.Forms.Button
$Script:GenKeyButton.Text = '生成密钥…'
$Script:GenKeyButton.Location = New-Object Drawing.Point(494, 78)
$Script:GenKeyButton.Size = New-Object Drawing.Size(118, 26)
$Script:Form.Controls.Add($Script:GenKeyButton)

Add-Label '私钥口令' 12 116 60 | Out-Null
$Script:KeyPassBox = Add-TextBox 70 113 158 $true
$Script:InstallPubButton = New-Object Windows.Forms.Button
$Script:InstallPubButton.Text = '安装公钥到服务器…'
$Script:InstallPubButton.Location = New-Object Drawing.Point(240, 112)
$Script:InstallPubButton.Size = New-Object Drawing.Size(180, 26)
$Script:Form.Controls.Add($Script:InstallPubButton)

Add-Label '面板账号' 12 149 60 | Out-Null
$Script:PanelUserBox = Add-TextBox 70 146 128 $false
Add-Label '密码' 208 149 36 | Out-Null
$Script:PanelPassBox = Add-TextBox 246 146 148 $true
$Script:CopyPassButton = New-Object Windows.Forms.Button
$Script:CopyPassButton.Text = '复制密码'
$Script:CopyPassButton.Location = New-Object Drawing.Point(402, 145)
$Script:CopyPassButton.Size = New-Object Drawing.Size(88, 26)
$Script:Form.Controls.Add($Script:CopyPassButton)

Add-Label '本地端口' 12 182 60 | Out-Null
$Script:LocalPortBox = Add-TextBox 70 179 60 $false
$Script:BrowserCheck = New-Object Windows.Forms.CheckBox
$Script:BrowserCheck.Text = '连上后自动打开浏览器'
$Script:BrowserCheck.Location = New-Object Drawing.Point(146, 181)
$Script:BrowserCheck.Size = New-Object Drawing.Size(180, 22)
$Script:BrowserCheck.Checked = $true
$Script:Form.Controls.Add($Script:BrowserCheck)

$Script:TestButton = Add-Button '测试连接' 12 218 96
$Script:ConnectButton = Add-Button '连接并打开面板' 118 218 132
$Script:DisconnectButton = Add-Button '断开' 260 218 76
$Script:SaveButton = Add-Button '保存设置' 346 218 96
$Script:OpenButton = Add-Button '打开面板' 452 218 76

$Script:StatusLabel = New-Object Windows.Forms.Label
$Script:StatusLabel.Text = '未连接'
$Script:StatusLabel.Location = New-Object Drawing.Point(12, 258)
$Script:StatusLabel.Size = New-Object Drawing.Size(600, 20)
$Script:StatusLabel.ForeColor = [Drawing.Color]::FromArgb(70, 70, 70)
$Script:Form.Controls.Add($Script:StatusLabel)

$Script:ResetGroup = New-Object Windows.Forms.GroupBox
$Script:ResetGroup.Text = '服务密码重置（通过 SSH 在服务器上执行）'
$Script:ResetGroup.Location = New-Object Drawing.Point(12, 282)
$Script:ResetGroup.Size = New-Object Drawing.Size(604, 92)
$Script:Form.Controls.Add($Script:ResetGroup)

$Script:ResetButtons = @()
for ($i = 0; $i -lt $Script:ServiceTargets.Count; $i++) {
    $target = $Script:ServiceTargets[$i]
    $button = New-Object Windows.Forms.Button
    $button.Text = ('重置 {0} 密码…' -f $target.Label)
    $button.Location = New-Object Drawing.Point((14 + $i * 196), 24)
    $button.Size = New-Object Drawing.Size(184, 28)
    $button.Tag = $target
    $Script:ResetGroup.Controls.Add($button)
    $Script:ResetButtons += $button
}

$Script:ResetNote = New-Object Windows.Forms.Label
$Script:ResetNote.Text = '密码只经 stdin 传给服务器，不写入命令历史；失败会原样返回服务端原因。'
$Script:ResetNote.Location = New-Object Drawing.Point(16, 60)
$Script:ResetNote.Size = New-Object Drawing.Size(576, 20)
$Script:ResetNote.ForeColor = [Drawing.Color]::FromArgb(110, 110, 110)
$Script:ResetGroup.Controls.Add($Script:ResetNote)

$Script:LogBox = New-Object Windows.Forms.TextBox
$Script:LogBox.Multiline = $true
$Script:LogBox.ReadOnly = $true
$Script:LogBox.ScrollBars = 'Vertical'
$Script:LogBox.Location = New-Object Drawing.Point(12, 388)
$Script:LogBox.Size = New-Object Drawing.Size(604, 190)
$Script:LogBox.BackColor = [Drawing.Color]::FromArgb(250, 250, 250)
$Script:LogBox.Font = New-Object Drawing.Font('Consolas', 9)
$Script:Form.Controls.Add($Script:LogBox)

function Read-FormSettings {
    $settings = Get-DefaultSettings
    $settings.ServerHost = $Script:HostBox.Text.Trim()
    $parsed = 0
    if ([int]::TryParse($Script:PortBox.Text.Trim(), [ref]$parsed) -and $parsed -gt 0 -and $parsed -lt 65536) {
        $settings.ServerPort = $parsed
    }
    $settings.ServerUser = $Script:UserBox.Text.Trim()
    $settings.KeyPath = $Script:KeyBox.Text.Trim()
    $settings.KeyPassphraseProtected = Protect-Secret $Script:KeyPassBox.Text
    $settings.PanelUser = $Script:PanelUserBox.Text.Trim()
    $settings.PanelPasswordProtected = Protect-Secret $Script:PanelPassBox.Text
    if ([int]::TryParse($Script:LocalPortBox.Text.Trim(), [ref]$parsed) -and $parsed -gt 0 -and $parsed -lt 65536) {
        $settings.LocalPort = $parsed
    }
    $settings.OpenBrowser = $Script:BrowserCheck.Checked
    return $settings
}

function Set-FormSettings($Settings) {
    $Script:HostBox.Text = [string]$Settings.ServerHost
    $Script:PortBox.Text = "$($Settings.ServerPort)"
    $Script:UserBox.Text = [string]$Settings.ServerUser
    $Script:KeyBox.Text = [string]$Settings.KeyPath
    $Script:KeyPassBox.Text = Unprotect-Secret $Settings.KeyPassphraseProtected
    $Script:PanelUserBox.Text = [string]$Settings.PanelUser
    $Script:PanelPassBox.Text = Unprotect-Secret $Settings.PanelPasswordProtected
    $Script:LocalPortBox.Text = "$($Settings.LocalPort)"
    $Script:BrowserCheck.Checked = [bool]$Settings.OpenBrowser
}

function Assert-Inputs($Settings) {
    if ([string]::IsNullOrWhiteSpace($Settings.ServerHost)) {
        [Windows.Forms.MessageBox]::Show('请填写服务器地址。', '参数不完整', 'OK', 'Warning') | Out-Null
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($Settings.ServerUser)) {
        [Windows.Forms.MessageBox]::Show('请填写登录用户名。', '参数不完整', 'OK', 'Warning') | Out-Null
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($Settings.KeyPath) -or -not (Test-Path -LiteralPath $Settings.KeyPath)) {
        [Windows.Forms.MessageBox]::Show('请选择有效的私钥文件。', '参数不完整', 'OK', 'Warning') | Out-Null
        return $false
    }
    return $true
}

function Open-Panel($Settings) {
    $url = 'http://127.0.0.1:{0}/' -f $Settings.LocalPort
    Start-Process $url | Out-Null
    Copy-PanelPassword $Settings
    Write-Log "已在浏览器打开：$url"
}

$Script:BrowseButton.Add_Click({
    try {
        $dialog = New-Object Windows.Forms.OpenFileDialog
        $dialog.Title = '选择 SSH 私钥文件'
        $dialog.Filter = '所有文件 (*.*)|*.*'
        if (Test-Path -LiteralPath $Script:AppDir) { $dialog.InitialDirectory = $Script:AppDir }
        if ($dialog.ShowDialog($Script:Form) -eq 'OK') {
            $Script:KeyBox.Text = $dialog.FileName
            if (Test-Path -LiteralPath "$($dialog.FileName).pub") {
                Write-Log '同目录发现对应公钥，可直接用「安装公钥到服务器」部署。'
            }
        }
        $dialog.Dispose()
    } catch {
        Write-Log ('选择私钥出错：{0}' -f $_.Exception.Message)
    }
})

$Script:GenKeyButton.Add_Click({
    try {
        $newKey = New-KeyPair (Read-FormSettings)
        if ($newKey) { $Script:KeyBox.Text = $newKey }
    } catch {
        Write-Log ('生成密钥出错：{0}' -f $_.Exception.Message)
    }
})

$Script:InstallPubButton.Add_Click({
    try {
        Install-PublicKey (Read-FormSettings)
    } catch {
        Write-Log ('安装公钥出错：{0}' -f $_.Exception.Message)
    }
})

$Script:CopyPassButton.Add_Click({
    try {
        Copy-PanelPassword (Read-FormSettings)
    } catch {
        Write-Log ('复制密码出错：{0}' -f $_.Exception.Message)
    }
})

$Script:TestButton.Add_Click({
    $Script:Form.Cursor = [Windows.Forms.Cursors]::WaitCursor
    try {
        $settings = Read-FormSettings
        if (-not (Assert-Inputs $settings)) { return }
        Set-Status '正在测试 SSH 连接…'
        $result = Test-RemoteConnection $settings
        Write-Log $result.Message
        if (-not $result.Ok) {
            Set-Status '连接测试失败'
            [Windows.Forms.MessageBox]::Show($result.Message, '测试连接', 'OK', 'Warning') | Out-Null
        } else {
            Set-Status ('连接可用：{0}@{1}:{2}' -f $settings.ServerUser, $settings.ServerHost, $settings.ServerPort)
            Write-Settings $settings
        }
    } catch {
        Write-Log ('测试连接出错：{0}' -f $_.Exception.Message)
        Set-Status '连接测试异常'
    } finally {
        $Script:Form.Cursor = [Windows.Forms.Cursors]::Default
    }
})

$Script:ConnectButton.Add_Click({
    $Script:Form.Cursor = [Windows.Forms.Cursors]::WaitCursor
    try {
        $settings = Read-FormSettings
        if (-not (Assert-Inputs $settings)) { return }
        Write-Settings $settings

        if (Test-PortListening -Port $settings.LocalPort) {
            $owner = Get-PortOwner $settings.LocalPort
            $ownerText = '未知进程'
            if ($owner) { $ownerText = '{0}(PID {1})' -f $owner.Name, $owner.Pid }
            $healthy = Test-PanelHealth -Port $settings.LocalPort
            $answer = [Windows.Forms.MessageBox]::Show(
                ("本机端口 {0} 已被 {1} 占用。`r`n`r`n隧道状态：{2}`r`n`r`n【是】结束它并重新建立本客户端的隧道（推荐）`r`n【否】直接使用已有隧道`r`n【取消】返回，不做任何改动" -f `
                    $settings.LocalPort, $ownerText, $(if ($healthy) { '可用（面板能响应）' } else { '不可用（面板无响应）' })),
                '已有隧道', 'YesNoCancel', 'Question')
            if ($answer -eq 'Cancel') {
                Set-Status '未改动'
                return
            }
            if ($answer -eq 'Yes') {
                if (-not $owner -or $owner.Name -notlike 'ssh*') {
                    [Windows.Forms.MessageBox]::Show(
                        ('占用端口的是 {0}，不是 ssh，结束它可能会影响其他程序。请手动处理或换一个本地端口。' -f $ownerText),
                        '端口被占用', 'OK', 'Warning') | Out-Null
                    return
                }
                Stop-Process -Id $owner.Pid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 600
                Write-Log ('已结束遗留隧道 {0}。' -f $ownerText)
            } elseif ($healthy) {
                $Script:TunnelOwned = $false
                Set-Status ('已连接（沿用已有隧道）：http://127.0.0.1:{0}/' -f $settings.LocalPort)
                Write-Log ('沿用已有隧道（{0}）：断开时不会关闭它。' -f $ownerText)
                if ($settings.OpenBrowser) { Open-Panel $settings }
                return
            }
        }

        Stop-Tunnel | Out-Null
        Set-Status '正在建立隧道…'
        Write-Log ('连接 {0}@{1}:{2}…' -f $settings.ServerUser, $settings.ServerHost, $settings.ServerPort)
        $identity = Resolve-IdentityFile $settings
        $process = Start-Tunnel $settings $identity
        if (-not (Wait-PortListening -Port $settings.LocalPort -TimeoutSeconds 20)) {
            Show-TunnelFailure '隧道没建起来：20 秒内本地端口没有开始监听。'
            return
        }
        if (-not (Test-PanelHealth -Port $settings.LocalPort)) {
            Show-TunnelFailure '隧道已建立，但面板没有响应。请确认服务器上 bot-manager 服务在运行。'
            return
        }
        $Script:TunnelOwned = $true
        Set-Status ('已连接：http://127.0.0.1:{0}/' -f $settings.LocalPort)
        Write-Log ('隧道就绪（SSH PID {0}）。' -f $process.Id)
        foreach ($note in $Script:ForwardNotes) { Write-Log $note }
        if ($settings.OpenBrowser) { Open-Panel $settings }
    } catch {
        Write-Log ('连接出错：{0}' -f $_.Exception.Message)
        Set-Status '连接异常'
        [Windows.Forms.MessageBox]::Show($_.Exception.Message, '连接出错', 'OK', 'Error') | Out-Null
    } finally {
        $Script:Form.Cursor = [Windows.Forms.Cursors]::Default
    }
})

$Script:DisconnectButton.Add_Click({
    try {
        $settings = Read-FormSettings
        $count = Stop-Tunnel
        $Script:TunnelOwned = $false
        if ($count -gt 0) {
            Write-Log ('已断开，停止 {0} 个 SSH 进程。' -f $count)
            Set-Status '已断开'
        } else {
            Write-Log '本客户端没有正在运行的隧道。'
            Set-Status '未连接'
        }
        # 断开后如果端口还在监听，说明有别处启动的隧道（例如旧启动脚本），如实说明
        $owner = Get-PortOwner $settings.LocalPort
        if ($owner) {
            Write-Log ('注意：端口 {0} 仍由 {1}(PID {2}) 监听，这不是本客户端启动的隧道，所以没有被关闭。' -f `
                $settings.LocalPort, $owner.Name, $owner.Pid)
            Set-Status ('仍有外部隧道占用 {0}' -f $settings.LocalPort)
        }
    } catch {
        Write-Log ('断开出错：{0}' -f $_.Exception.Message)
    }
})

$Script:SaveButton.Add_Click({
    try {
        Write-Settings (Read-FormSettings)
        Write-Log '设置已保存（密码类字段用当前 Windows 账户加密）。'
        Set-Status '设置已保存'
    } catch {
        Write-Log ('保存设置出错：{0}' -f $_.Exception.Message)
    }
})

$Script:OpenButton.Add_Click({
    try {
        $settings = Read-FormSettings
        if (-not (Test-PortListening -Port $settings.LocalPort)) {
            Write-Log '隧道未运行，请先点「连接并打开面板」。'
            Set-Status '未连接'
            return
        }
        Open-Panel $settings
    } catch {
        Write-Log ('打开面板出错：{0}' -f $_.Exception.Message)
    }
})

foreach ($resetButton in $Script:ResetButtons) {
    $resetButton.Add_Click({
        try {
            Reset-ServicePassword $this.Tag
        } catch {
            Write-Log ('重置流程出错：{0}' -f $_.Exception.Message)
        }
    })
}

$Script:Form.Add_FormClosing({
    Stop-Tunnel | Out-Null
    Clear-SessionKey
})

$Script:Form.Add_Shown({
    Write-ClientLog '窗口已显示'
    Set-FormSettings $Script:Settings
    Write-Log '客户端已启动：填服务器与私钥 → 测试连接 → 连接并打开面板。'
    if ([string]::IsNullOrWhiteSpace($Script:Settings.ServerHost)) {
        Set-Status '等待配置'
    } else {
        Set-Status ('已载入上次配置：{0}@{1}' -f $Script:Settings.ServerUser, $Script:Settings.ServerHost)
    }
})

# 自检窗体的排版：把窗口挪到屏幕外渲染成图片，并打印控件坐标（无需人工看屏幕）
if ($DumpLayout) {
    $Script:Form.StartPosition = 'Manual'
    $Script:Form.Location = New-Object Drawing.Point(-3200, -3200)
    Set-FormSettings $Script:Settings
    $Script:Form.Show()
    [Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 400
    [Windows.Forms.Application]::DoEvents()

    $report = New-Object Collections.ArrayList
    $report.Add(('ClientSize: {0}x{1}' -f $Script:Form.ClientSize.Width, $Script:Form.ClientSize.Height)) | Out-Null
    foreach ($control in $Script:Form.Controls) {
        $text = $control.Text
        if ($text.Length -gt 24) { $text = $text.Substring(0, 24) + '…' }
        $report.Add(('{0,-12} {1,4},{2,-4} {3,4}x{4,-4} {5}' -f $control.GetType().Name, $control.Left, $control.Top, $control.Width, $control.Height, $text)) | Out-Null
    }
    $bitmap = New-Object Drawing.Bitmap($Script:Form.Width, $Script:Form.Height)
    $Script:Form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $Script:Form.Width, $Script:Form.Height)))
    $bitmap.Save($DumpLayout, [Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    $report.Add(('saved: {0}' -f $DumpLayout)) | Out-Null
    $Script:Form.Close()
    [IO.File]::WriteAllText(($DumpLayout + '.txt'), ($report -join "`r`n"), (New-Object Text.UTF8Encoding($false)))
    exit 0
}

[void]$Script:Form.ShowDialog()
