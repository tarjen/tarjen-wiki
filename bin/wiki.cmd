@echo off
REM bin/wiki.cmd - Wiki CLI wrapper for Windows (cmd.exe / PowerShell)
REM
REM 安装:
REM   .\bootstrap.sh  (或 .\bootstrap.cmd 如果是 Git Bash)
REM
REM 使用 (cmd.exe):
REM   wiki doctor
REM   wiki list
REM
REM 使用 (PowerShell):
REM   .\bin\wiki.cmd doctor
REM   或先 alias: Set-Alias wiki $PWD\bin\wiki.cmd

setlocal

REM 找脚本所在目录 (Windows 路径用 \)
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%.") do set "SCRIPT_DIR=%%~fI"

REM repo 根 = script 目录的上一级
for %%I in ("%SCRIPT_DIR%\..") do set "REPO_ROOT=%%~fI"

REM venv python (Windows venv 路径是 .venv\Scripts\python.exe)
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] 找不到 venv python: %PY%
    echo         跑 bootstrap.sh 装 venv (Git Bash) 或 .\bootstrap.cmd
    exit /b 1
)

cd /d "%REPO_ROOT%"
"%PY%" -m tools.cli_main %*
endlocal