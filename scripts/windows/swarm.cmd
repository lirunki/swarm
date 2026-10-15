@echo off
REM Swarm Windows launcher (static).
REM Forwards all arguments to the PowerShell argv-safe bridge, which calls the
REM static Cygwin bash runtime. No argument interpolation occurs here.
setlocal enableextensions
set "DIR=%~dp0"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%DIR%swarm-launch.ps1" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
