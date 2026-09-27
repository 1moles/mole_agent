<#
.SYNOPSIS
  把 Mole 便携版加入当前用户的 PATH，之后在任意目录的 PowerShell 里输入 mole 即可启动。不需要管理员权限。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot.TrimEnd("\")

# 直接读写注册表里的用户 PATH，保留 %USERPROFILE% 这类写法（.NET 的 SetEnvironmentVariable 会把它们展开成固定路径）
$envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
$raw = [string]$envKey.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$entries = @($raw.Split(";") | Where-Object { $_.Trim() -ne "" })
$others = @($entries | Where-Object {
    -not [string]::Equals([Environment]::ExpandEnvironmentVariables($_.Trim()).TrimEnd("\"), $dir, [StringComparison]::OrdinalIgnoreCase)
})

function Save-UserPath([string[]]$Items) {
    $envKey.SetValue("Path", ($Items -join ";"), [Microsoft.Win32.RegistryValueKind]::ExpandString)
    # 通知系统环境变量变了，之后新开的终端才能看到（借 .NET 设置一个临时变量来广播）
    [Environment]::SetEnvironmentVariable("MOLE_INSTALL_REFRESH", "1", "User")
    [Environment]::SetEnvironmentVariable("MOLE_INSTALL_REFRESH", $null, "User")
}

if ($Uninstall) {
    if ($others.Count -lt $entries.Count) {
        Save-UserPath $others
        Write-Host "已从 PATH 移除 $dir"
    } else {
        Write-Host "PATH 里没有 $dir"
    }
    Write-Host "删掉这个目录就卸载完了。对话历史、审计日志等在 $env:USERPROFILE\.mole-agent，不需要可以一起删掉。"
    return
}

if (-not (Test-Path (Join-Path $dir "python\python.exe"))) {
    throw "$dir 下没有 python\python.exe，请把 install.ps1 放在解压出来的 mole-windows-x64 目录里运行"
}

# 浏览器下载的 zip 解压出来的文件带「来自网络」的标记，运行 mole.exe 时可能被 SmartScreen 拦下
Write-Host "去掉下载标记…"
Get-ChildItem -LiteralPath $dir -Recurse -File | Unblock-File

if ($others.Count -eq $entries.Count) {
    Save-UserPath (@($entries) + $dir)
    Write-Host "已把 $dir 加入当前用户的 PATH"
} else {
    Write-Host "PATH 里已经有 $dir"
}

$envFile = Join-Path $dir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $dir ".env.example") $envFile
    Write-Host "已生成 $envFile"
}

Write-Host ""
Write-Host "接下来：" -ForegroundColor Green
Write-Host "  1. notepad `"$envFile`"   填上你要用的供应商的 API key（供应商在同目录的 models.toml 里）"
Write-Host "  2. 重新打开 PowerShell，执行 mole --check，确认模型能用"
Write-Host "  3. cd 到你的项目目录，执行 mole"
