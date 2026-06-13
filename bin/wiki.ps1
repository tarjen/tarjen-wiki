# bin/wiki.ps1 - Wiki CLI wrapper for PowerShell
#
# 安装 (admin PowerShell):
#   .\bootstrap.sh
#   # 把 $PWD\bin 加到 PATH 或创建 function:
#   function wiki { & "$PWD\bin\wiki.ps1" @args }
#
# 使用:
#   wiki doctor
#   wiki list

$ErrorActionPreference = 'Stop'

# 找脚本所在目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptDir = (Resolve-Path $ScriptDir).Path

# repo 根
$RepoRoot = Split-Path -Parent $ScriptDir
$RepoRoot = (Resolve-Path $RepoRoot).Path

# venv python (Windows: .venv\Scripts\python.exe)
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host "[ERROR] 找不到 venv python: $Python" -ForegroundColor Red
    Write-Host "        跑 bootstrap.sh 装 venv (Git Bash)" -ForegroundColor Red
    exit 1
}

# 切到 repo 根并执行
Push-Location $RepoRoot
try {
    & $Python -m tools.cli_main @args
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode