# Launcher component: generate the self-contained, portable launcher bundle in
# the install directory. All generated files are UTF-8 LF. Static runtime files
# are copied in for stable launch paths. The editable Python install still needs
# its original checkout; neither that checkout nor the runtime should be moved.
#
# Generated artifacts:
#   config.json       - human-readable record of all resolved paths
#   env.sh            - sourced by swarm-runtime.sh; trusted, shell-quoted exports
#   bin/pi            - POSIX shim: forwards argv to `python -m swarm.windows_pi "$@"`
#   browser-open.sh   - opens URLs in the Windows default browser via explorer.exe
#   swarm-runtime.sh  - static copy (sources env.sh, execs venv python -m swarm)
#   swarm-launch.ps1  - static copy (argv-safe PowerShell bridge)
#   swarm.cmd         - static copy (Windows entry -> swarm-launch.ps1)
#   swarm             - static copy (POSIX entry -> swarm-runtime.sh)

function New-LauncherBundle {
    Write-Step "Generating launcher bundle"
    # Keep executable shims separate from the private npm installation at pi/.
    $shimDir = Join-Path $script:InstallDir 'bin'
    Ensure-Dir $shimDir
    $readyPath = Join-Path $script:InstallDir 'ready.json'
    if (Test-Path -LiteralPath $readyPath) { Remove-Item -LiteralPath $readyPath -Force }

    if (-not (Test-VenvInstalled)) { Fail "venv required for launcher generation" 60 }
    if (-not (Test-NodeInstalled)) { Fail "node required for launcher generation" 61 }
    if (-not (Test-PiInstalled))  { Fail "pi required for launcher generation" 62 }

    # --- Resolve every path (Windows + Cygwin POSIX forms) ---
    $bashWin = Join-Path $script:CygwinRoot 'bin\bash.exe'
    $nodeExeWin = Join-Path $script:NodeDir 'node.exe'
    $piEntryWin = Join-Path $script:PiDir "node_modules\$($script:PiPackage)\dist\cli.js"
    $venvPyWin = Join-Path $script:VenvDir 'bin\python'
    $venvBinWin = Join-Path $script:VenvDir 'bin'

    # Test-VenvInstalled above checks POSIX symlinks through Cygwin; Windows
    # Test-Path cannot follow the venv's /usr/bin/python3.12 symlink target.
    foreach ($p in @($bashWin, $nodeExeWin, $piEntryWin)) {
        if (-not (Test-Path -LiteralPath $p)) {
            Fail "expected install artifact missing: $p" 63
        }
    }

    $bashCyg       = To-CygwinPath $bashWin
    $nodeExeCyg    = To-CygwinPath $nodeExeWin
    $nodeDirCyg = To-CygwinPath $script:NodeDir
    $piEntryCyg    = To-CygwinPath $piEntryWin
    $venvPyCyg     = To-CygwinPath $venvPyWin
    $venvBinCyg    = To-CygwinPath $venvBinWin
    $installDirCyg = To-CygwinPath $script:InstallDir
    $repoCyg       = To-CygwinPath $script:RepoDir

    # Cygwin HOME == converted USERPROFILE so Pi config/auth at ~/.pi are reused.
    $homeCyg = To-CygwinPath $env:USERPROFILE

    # Windows explorer.exe for browser opening (avoids extra Cygwin packages).
    $sysRoot = $env:SystemRoot
    if (-not $sysRoot) { $sysRoot = "$env:windir" }
    if (-not $sysRoot) { $sysRoot = 'C:\Windows' }
    $explorerWin = Join-Path $sysRoot 'explorer.exe'
    if (-not (Test-Path -LiteralPath $explorerWin)) {
        Fail "explorer.exe not found at '$explorerWin'" 64
    }
    $explorerCyg = To-CygwinPath $explorerWin

    # --- config.json (human-readable record; the launcher reads bashExe +
    # runtimeScriptCygwin; env.sh is the authoritative env source) ---
    $runtimeWin  = Join-Path $script:InstallDir 'swarm-runtime.sh'
    $runtimeCyg  = To-CygwinPath $runtimeWin
    $browserPathCyg = To-CygwinPath (Join-Path $script:InstallDir 'browser-open.sh')
    # Python webbrowser parses BROWSER with shlex; quote the executable path
    # inside the value too, so installation paths containing spaces work.
    $browserCommand = (Bash-SingleQuote $browserPathCyg) + ' %s'

    $config = [ordered]@{
        installDir          = $script:InstallDir
        cygwinRoot          = $script:CygwinRoot
        nodeDir             = $script:NodeDir
        piDir               = $script:PiDir
        venvDir             = $script:VenvDir
        repoDir             = $script:RepoDir
        bashExe             = $bashWin
        bashExeCygwin       = $bashCyg
        nodeExe             = $nodeExeWin
        nodeExeCygwin       = $nodeExeCyg
        piEntry             = $piEntryWin
        piEntryCygwin       = $piEntryCyg
        venvPython          = $venvPyWin
        venvPythonCygwin    = $venvPyCyg
        venvBinCygwin       = $venvBinCyg
        installDirCygwin    = $installDirCyg
        repoDirCygwin       = $repoCyg
        runtimeScript       = $runtimeWin
        runtimeScriptCygwin = $runtimeCyg
        homeCygwin          = $homeCyg
        env                 = [ordered]@{
            SWARM_WINDOWS_NODE      = $nodeExeCyg
            SWARM_WINDOWS_PI_ENTRY  = $piEntryCyg
            HOME                    = $homeCyg
            BROWSER                 = $browserCommand
            SWARM_VENV_PYTHON       = $venvPyCyg
        }
    }
    $configPath = Join-Path $script:InstallDir 'config.json'
    $configJson = $config | ConvertTo-Json -Depth 20
    Write-File-LF -Path $configPath -Content $configJson
    Write-Ok "config.json written"

    # --- env.sh (sourced by swarm-runtime.sh); trusted, single-quoted exports ---
    $browserShCyg = $config.env.BROWSER
    # PATH prefix = venv/bin : install bin : install dir : node dir : /usr/bin ; appended to
    # bash $PATH at source time so every Cygwin shell Swarm spawns has the
    # venv python, the `pi` shim, the private node.exe, and core Cygwin utils.
    $pathPrefix = $venvBinCyg + ':' + (To-CygwinPath $shimDir) + ':' + $installDirCyg + ':' + $nodeDirCyg + ':/usr/bin'
    $pathExport = 'export PATH=' + (Bash-SingleQuote $pathPrefix) + '":$PATH"'
    $envSh = @"
# Generated by Swarm Windows installer. Sourced by swarm-runtime.sh.
# All values are installer-computed and shell-quoted. Do not edit by hand;
# re-run setup-windows.cmd to regenerate after path changes.
export SWARM_WINDOWS_NODE=$(Bash-SingleQuote $nodeExeCyg)
export SWARM_WINDOWS_PI_ENTRY=$(Bash-SingleQuote $piEntryCyg)
export SWARM_VENV_PYTHON=$(Bash-SingleQuote $venvPyCyg)
export HOME=$(Bash-SingleQuote $homeCyg)
export SHELL=/bin/bash
export LANG="`${LANG:-C.UTF-8}"
export BROWSER=$(Bash-SingleQuote $browserShCyg)
# PATH = venv/bin : install bin (pi shim) : install dir : node dir : /usr/bin : existing PATH.
$pathExport
"@
    Write-File-LF -Path (Join-Path $script:InstallDir 'env.sh') -Content $envSh
    Write-Ok "env.sh written"

    # --- pi shim: forwards argv verbatim to the Windows adapter module ---
    $piShim = @"
#!/usr/bin/env bash
# Generated by Swarm Windows installer.
# Shim invoked by Swarm's PiAdapter/tmux as `pi <args>`. Forwards exact argv
# to the `swarm.windows_pi` module, which spawns
#   node `\$SWARM_WINDOWS_PI_ENTRY` <mapped args>
# using the env exported above (see env.sh). No argument interpolation.
set -euo pipefail
exec $(Bash-SingleQuote $venvPyCyg) -m swarm.windows_pi "`$@"
"@
    $piPath = Join-Path $shimDir 'pi'
    Write-File-LF -Path $piPath -Content $piShim
    Write-Ok "pi shim written"

    # --- browser-open.sh: open URL in Windows default browser ---
    $browserSh = @"
#!/usr/bin/env bash
# Generated by Swarm Windows installer. Python webbrowser uses \$BROWSER.
# explorer.exe <url> opens the Windows default browser.
set -euo pipefail
exec $(Bash-SingleQuote $explorerCyg) "`$@"
"@
    Write-File-LF -Path (Join-Path $script:InstallDir 'browser-open.sh') -Content $browserSh
    Write-Ok "browser-open.sh written"

    # --- Copy static runtime files into the install dir (portable bundle) ---
    $srcDir = $script:WindowsScriptsDir
    $static = @('swarm-runtime.sh', 'swarm-launch.ps1', 'swarm.cmd', 'swarm')
    foreach ($name in $static) {
        $src = Join-Path $srcDir $name
        $dst = Join-Path $script:InstallDir $name
        if (-not (Test-Path -LiteralPath $src)) { Fail "static launcher file missing: $src" 65 }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    Write-Ok "static launcher files copied"
    $chmodRunner = Join-Path $script:CacheDir 'swarm-chmod.sh'
    Write-File-LF -Path $chmodRunner -Content @'
#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/bin:/bin:$PATH"
chmod u+x "$SWARM_LAUNCH_DIR/bin/pi" "$SWARM_LAUNCH_DIR/swarm" "$SWARM_LAUNCH_DIR/"*.sh
'@
    Invoke-CygwinScript -ScriptPath $chmodRunner -EnvVars @{ SWARM_LAUNCH_DIR = $installDirCyg }

    # --- Verify the bundle end-to-end via the static runtime (smoke) ---
    Write-Info "smoke: swarm --help via launcher"
    $smokeOut = & $bashWin $runtimeCyg '--help' 2>&1
    $smokeRc = $LASTEXITCODE
    # `swarm --help` must exit 0 (argparse prints help and exits 0). An exit
    # code of 2 indicates a real argparse/CLI error and is a failure, not a
    # warning: fail closed so a broken install is never reported as success.
    if ($smokeRc -ne 0) {
        Fail "swarm --help smoke failed (exit $smokeRc). The venv/runtime did not start cleanly. See $script:LogPath." 66
    }
    # Exercise the real native-Node/Cygwin terminal boundary with no provider
    # calls or access to existing agent sessions. Failed checks abort setup.
    $smokeRunner = Join-Path $script:CacheDir 'swarm-smoke.sh'
    Write-File-LF -Path $smokeRunner -Content @'
#!/usr/bin/env bash
set -euo pipefail
source "$SWARM_ENV_PATH"
exec "$SWARM_VENV_PYTHON" "$SWARM_SMOKE_PATH"
'@
    Invoke-CygwinScript -ScriptPath $smokeRunner -EnvVars @{
        SWARM_ENV_PATH = (To-CygwinPath (Join-Path $script:InstallDir 'env.sh'))
        SWARM_SMOKE_PATH = (To-CygwinPath (Join-Path $script:RepoDir 'scripts\windows\smoke.py'))
    }
    Write-Ok "launcher and native-terminal integration checks passed at $($script:InstallDir)"
    Write-Host ""
    Write-Host "  Use it via:" -ForegroundColor Cyan
    Write-Host "    cmd / Git Bash :  $script:InstallDir\swarm.cmd serve --open" -ForegroundColor White
    Write-Host "    Cygwin / bash  :  $installDirCyg/swarm serve --open" -ForegroundColor White
    Write-Host " repo root       : .\swarm-windows.sh serve --open  (auto-installs if missing)" -ForegroundColor White
}
