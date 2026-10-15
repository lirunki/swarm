@echo off
REM Bootstrap entry point: cmd / PowerShell console.
REM Invokes the PowerShell installer with all arguments forwarded verbatim.
setlocal enableextensions
set "SCRIPT=%~dp0scripts\windows\install.ps1"
if not exist "%SCRIPT%" (
    echo [swarm] installer not found: %SCRIPT% 1>&2
    exit /b 2
)
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
