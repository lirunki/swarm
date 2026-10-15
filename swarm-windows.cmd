@echo off
setlocal
if not defined SWARM_WINDOWS_INSTALL_DIR set "SWARM_WINDOWS_INSTALL_DIR=%LOCALAPPDATA%\Swarm"
if not exist "%SWARM_WINDOWS_INSTALL_DIR%\ready.json" (
    call "%~dp0setup-windows.cmd" -InstallDir "%SWARM_WINDOWS_INSTALL_DIR%"
    if errorlevel 1 exit /b 1
)
if "%~1"=="" (
    call "%SWARM_WINDOWS_INSTALL_DIR%\swarm.cmd" serve --open
) else (
    call "%SWARM_WINDOWS_INSTALL_DIR%\swarm.cmd" %*
)
exit /b %errorlevel%
