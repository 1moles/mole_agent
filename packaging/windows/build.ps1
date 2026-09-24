<#
.SYNOPSIS
  打 Mole 的 Windows 便携包：解压即用，使用者的电脑上不需要装 Python。

.DESCRIPTION
  在一台能上网的 Windows（10/11，x64）上运行，打包机本身也不需要装 Python：

    powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1

  做的事：
    1. 从 nuget.org 下载官方的 Python 完整版（nuget 包 python，就是 python.org 的同一份构建，
       解压即可用、不写注册表），放进 python\
    2. 用它的 pip 安装 pyproject.toml 里列的依赖（openjiuwen 固定为测试过的版本）
    3. 复制 mole_agent 源码和配置样例，写入 site-packages\mole_root.pth 让 Python 找到源码
    4. 用 Windows 自带的 csc.exe 编译启动器 mole.exe（另有 mole.cmd 备用）
    5. 冒烟测试（import、mole --version、mole --list-models），打成 zip

  产物：dist\mole-windows-x64\（目录）和 dist\mole-windows-x64-<版本>.zip

.PARAMETER PythonVersion
  打进包里的 Python 版本（nuget 包 python 的版本号），需在 3.11–3.13 之间。

.PARAMETER OpenjiuwenVersion
  openjiuwen 的版本。默认是测试用例跑通的版本；传空字符串表示按 pyproject.toml 装最新版。

.PARAMETER IndexUrl
  PyPI 镜像地址（公司内网镜像等），例如 https://mirrors.example.com/pypi/simple

.PARAMETER PythonPackage
  已经下载好的 nuget 包文件（.nupkg 或 .zip）。打包机访问不了 nuget.org 时用它。

.PARAMETER SkipZip
  只生成目录，不打 zip（调试打包过程时用）。
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.13.7",
    [string]$OpenjiuwenVersion = "0.1.18.post1",
    [string]$IndexUrl = "",
    [string]$PythonPackage = "",
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # PowerShell 5.1 显示下载进度条会让下载慢很多
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DistDir = Join-Path $RepoRoot "dist"
$Out = Join-Path $DistDir "mole-windows-x64"
$CacheDir = Join-Path $RepoRoot "build\windows-cache"
$Py = Join-Path $Out "python\python.exe"

function Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Native {
    # 运行外部程序；退出码不是 0 就中止打包。
    # 局部改成 Continue：PowerShell 5.1 在 Stop 模式下可能把外部程序写到 stderr 的警告当成错误中止
    param([string]$FilePath, [string[]]$Arguments, [string]$What)
    $ErrorActionPreference = "Continue"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What 失败（退出码 $LASTEXITCODE）"
    }
}

function Write-TextFile([string]$Path, [string]$Content, [switch]$Bom) {
    # PowerShell 5.1 的 Set-Content -Encoding UTF8 总是带 BOM，这里自己控制
    $encoding = New-Object System.Text.UTF8Encoding($Bom.IsPresent)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

# ---------------------------------------------------------------------------
Step "检查环境"
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "需要 64 位 Windows"
}
$pyproject = Get-Content (Join-Path $RepoRoot "pyproject.toml") -Raw -Encoding UTF8
if (-not ($pyproject -match '(?m)^version\s*=\s*"([^"]+)"')) {
    throw "pyproject.toml 里没有找到 version"
}
$MoleVersion = $Matches[1]
if ($Out.Length -gt 90) {
    Write-Warning "输出目录路径较长（$($Out.Length) 个字符），依赖里层级深的文件可能超出 Windows 260 字符的路径限制；出错时把仓库放到短一点的路径下再打包"
}
Write-Host "mole-agent $MoleVersion，Python $PythonVersion，openjiuwen $(if ($OpenjiuwenVersion) { $OpenjiuwenVersion } else { '最新' })"

if (Test-Path $Out) {
    Remove-Item $Out -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Out, $CacheDir | Out-Null

# ---------------------------------------------------------------------------
Step "准备 Python $PythonVersion"
if ($PythonPackage) {
    $nupkg = (Resolve-Path $PythonPackage).Path
} else {
    $nupkg = Join-Path $CacheDir "python.$PythonVersion.zip"   # Expand-Archive 只认 .zip 扩展名
    if (-not (Test-Path $nupkg)) {
        $url = "https://www.nuget.org/api/v2/package/python/$PythonVersion"
        Write-Host "下载 $url"
        try {
            Invoke-WebRequest -Uri $url -OutFile "$nupkg.part" -UseBasicParsing
        } catch {
            throw "下载 Python 失败：$($_.Exception.Message)`n可以换一个版本（-PythonVersion），或者手动下载后用 -PythonPackage 指定文件"
        }
        Move-Item "$nupkg.part" $nupkg -Force
    }
}
if ([IO.Path]::GetExtension($nupkg) -ne ".zip") {
    $copy = Join-Path $CacheDir "python-package.zip"
    Copy-Item $nupkg $copy -Force
    $nupkg = $copy
}
$unpacked = Join-Path $CacheDir "python-unpacked"
if (Test-Path $unpacked) {
    Remove-Item $unpacked -Recurse -Force
}
Expand-Archive -Path $nupkg -DestinationPath $unpacked -Force
$tools = Join-Path $unpacked "tools"
if (-not (Test-Path (Join-Path $tools "python.exe"))) {
    throw "$nupkg 不是 nuget 的 python 包（里面没有 tools\python.exe）"
}
Copy-Item $tools (Join-Path $Out "python") -Recurse
Invoke-Native $Py @("-c", "import sys; print(sys.version)") "运行打包用的 Python"

# ---------------------------------------------------------------------------
Step "安装依赖"
$pipArgs = @("--disable-pip-version-check", "--no-warn-script-location", "--no-cache-dir")
if ($IndexUrl) {
    $pipArgs += @("--index-url", $IndexUrl)
}
$ErrorActionPreference = "Continue"
& $Py -m pip --version *> $null
$hasPip = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $hasPip) {
    Invoke-Native $Py @("-m", "ensurepip", "--default-pip") "安装 pip"
}
Invoke-Native $Py (@("-m", "pip", "install", "--upgrade", "pip") + $pipArgs) "升级 pip"

# 依赖清单取自 pyproject.toml（和 pip install -e . 装的完全一样），openjiuwen 再单独固定版本
$requirements = Join-Path $CacheDir "requirements.txt"
$readDeps = "import sys, tomllib; deps = tomllib.load(open(sys.argv[1], 'rb'))['project']['dependencies']; open(sys.argv[2], 'w', encoding='utf-8').write('\n'.join(deps) + '\n')"
Invoke-Native $Py @("-c", $readDeps, (Join-Path $RepoRoot "pyproject.toml"), $requirements) "读取依赖清单"
$installArgs = @("-m", "pip", "install", "-r", $requirements) + $pipArgs
if ($OpenjiuwenVersion) {
    $installArgs += "openjiuwen==$OpenjiuwenVersion"
}
Invoke-Native $Py $installArgs "安装依赖"

# python\Scripts 里的 pip.exe 等写死了打包机上的路径，挪到别的电脑就不能用，Mole 也用不到
$scripts = Join-Path $Out "python\Scripts"
if (Test-Path $scripts) {
    Remove-Item $scripts -Recurse -Force
}

# ---------------------------------------------------------------------------
Step "复制 Mole"
Copy-Item (Join-Path $RepoRoot "mole_agent") $Out -Recurse
Get-ChildItem (Join-Path $Out "mole_agent") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# 包根目录（mole.exe 所在目录）就是 Mole 的「仓库根目录」：.env 和 models.toml 放在这里即可生效
$modelsToml = Join-Path $RepoRoot "models.toml"
if (-not (Test-Path $modelsToml)) {
    $modelsToml = Join-Path $RepoRoot "models.example.toml"
}
Copy-Item $modelsToml (Join-Path $Out "models.toml")
Copy-Item (Join-Path $RepoRoot "models.example.toml") $Out
Copy-Item (Join-Path $RepoRoot ".env.example") $Out
New-Item -ItemType Directory -Force -Path (Join-Path $Out "docs") | Out-Null
Copy-Item (Join-Path $RepoRoot "docs\mole-manual.html") (Join-Path $Out "docs")
if (Test-Path (Join-Path $RepoRoot "examples")) {
    Copy-Item (Join-Path $RepoRoot "examples") $Out -Recurse
}
Copy-Item (Join-Path $PSScriptRoot "install.ps1") $Out
Copy-Item (Join-Path $PSScriptRoot "README-Windows.txt") $Out

# mole_agent 在 python 目录的上一级：site-packages 里放一个 .pth，写相对路径（site-packages → Lib → python → 包根目录），
# 整个目录挪到哪里都能找到
$sitePackages = Join-Path $Out "python\Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
Write-TextFile (Join-Path $sitePackages "mole_root.pth") "..\..\..`r`n"
Invoke-Native $Py @("-m", "compileall", "-q", (Join-Path $Out "mole_agent")) "预编译 mole_agent"

# ---------------------------------------------------------------------------
Step "生成启动器"
Write-TextFile (Join-Path $Out "mole.cmd") "@echo off`r`n`"%~dp0python\python.exe`" -I -X utf8 -m mole_agent %*`r`n"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (Test-Path $csc) {
    Invoke-Native $csc @("/nologo", "/target:exe", "/platform:anycpu", "/optimize+", "/codepage:65001",
        "/out:$(Join-Path $Out 'mole.exe')", (Join-Path $PSScriptRoot "launcher.cs")) "编译 mole.exe"
} else {
    Write-Warning "没有找到 .NET Framework 4 的 csc.exe，不生成 mole.exe；可以用 mole.cmd 启动"
}

# ---------------------------------------------------------------------------
Step "冒烟测试"
$smokeHome = Join-Path $CacheDir "smoke-home"
if (Test-Path $smokeHome) {
    Remove-Item $smokeHome -Recurse -Force
}
$savedHome = $env:MOLE_HOME
$env:MOLE_HOME = $smokeHome   # 不碰打包机上真实的 ~/.mole-agent
Push-Location $env:TEMP         # 在别的目录运行，确认用的是包里的 mole_agent
try {
    Invoke-Native $Py @("-I", "-X", "utf8", "-c",
        "import mole_agent.cli, mole_agent.agent, openjiuwen, sys; print('import ok,', sys.executable)") "导入 mole_agent 和 openjiuwen"
    $launcher = Join-Path $Out "mole.exe"
    if (-not (Test-Path $launcher)) {
        $launcher = Join-Path $Out "mole.cmd"
    }
    Invoke-Native $launcher @("--version") "mole --version"
    Invoke-Native $launcher @("--list-models") "mole --list-models"
} finally {
    Pop-Location
    $env:MOLE_HOME = $savedHome
}

# ---------------------------------------------------------------------------
if (-not $SkipZip) {
    Step "打 zip"
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = Join-Path $DistDir "mole-windows-x64-$MoleVersion.zip"
    if (Test-Path $zip) {
        Remove-Item $zip -Force
    }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($Out, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    $sizeMb = [Math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "已生成 $zip（$sizeMb MB）" -ForegroundColor Green
}
Write-Host "目录：$Out" -ForegroundColor Green
