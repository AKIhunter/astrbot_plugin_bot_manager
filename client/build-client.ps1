# 重新打包客户端为单文件 exe（离线，仅用 Windows 自带 IExpress）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File build-client.ps1
param(
    [string]$OutputName = 'BotManagerClient.exe'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stage = Join-Path $root 'stage'
$dist = Join-Path $root 'dist'
$sedPath = Join-Path $env:TEMP 'bot-manager-client.sed'

foreach ($dir in @($stage, $dist)) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

Copy-Item -LiteralPath (Join-Path $root 'BotManagerClient.ps1') -Destination $stage -Force
Copy-Item -LiteralPath (Join-Path $root 'run-client.vbs') -Destination $stage -Force
Copy-Item -LiteralPath (Join-Path $root 'README.txt') -Destination $dist -Force

$target = Join-Path $dist $OutputName
if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$target
FriendlyName=Bot Manager Client
AppLaunched=powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File BotManagerClient.ps1
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles
[Strings]
FILE0=BotManagerClient.ps1
FILE1=run-client.vbs
[SourceFiles]
SourceFiles0=$stage\
[SourceFiles0]
%FILE0%=
%FILE1%=
"@

# IExpress 只认 CRLF + 系统 ANSI 编码的 SED，写成 UTF-8 会静默失败
$sed = $sed -replace "`r`n", "`n" -replace "`n", "`r`n"
[IO.File]::WriteAllText($sedPath, $sed, [Text.Encoding]::Default)

& "$env:WINDIR\System32\iexpress.exe" /N $sedPath | Out-Null
if (-not (Test-Path -LiteralPath $target)) {
    throw 'IExpress 没有产出 exe，检查 SED 的内容与路径。'
}

$file = Get-Item -LiteralPath $target
$hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
"built: {0} ({1:N1} KB)" -f $file.FullName, ($file.Length / 1KB)
"SHA256: $hash"
