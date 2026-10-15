#Requires -Version 5.1
<#
.SYNOPSIS
  Automated Windows installer for Swarm: user-local Cygwin (POSIX Python 3.12 +
  tmux 3.2), private Node LTS, private Pi (npm --ignore-scripts), a dedicated
  Cygwin venv for Swarm, Pi shellPath configuration, and a portable launcher.

.DESCRIPTION
  Goal: run Swarm on Windows WITHOUT WSL and WITHOUT admin rights / global PATH
  changes / Git Bash installation modifications. Everything is user-local under
  %LOCALAPPDATA%\Swarm by default.

  Bootstrap from cmd or Git Bash via the root setup-windows.cmd / setup-windows.sh,
  which invoke this script through PowerShell.

  Fail-closed safety: the official Cygwin setup-x86_64.exe is downloaded over
  HTTPS and its Authenticode signature is verified before execution. The Node
  zip SHA256 is verified against nodejs.org SHASUMS256.txt.

.PARAMETER InstallDir
  Root install directory. Default: $env:LOCALAPPDATA\Swarm.

.PARAMETER Mirror
  Trusted HTTPS Cygwin mirror. Default: https://mirrors.kernel.org/sourceware/cygwin/

.PARAMETER NodeVersion
  Node.js version to install (e.g. v22.11.0). Default: latest LTS (auto-resolved).

.PARAMETER PiPackage
  npm package for Pi. Default: @earendil-works/pi-coding-agent

.PARAMETER RepoDir
  Swarm repo root (must contain pyproject.toml). Default: two levels above this
  script's directory (scripts/windows -> repo root).

.PARAMETER Force
  Reinstall Cygwin and Node even if present.

.PARAMETER Update
  Refresh Pi and the Swarm venv without reinstalling Cygwin or Node.

.PARAMETER SkipCygwin / SkipNode / SkipPi / SkipVenv / SkipSettings / SkipLauncher
  Granular skips for re-running individual stages.

.EXAMPLE
  setup-windows.cmd
  setup-windows.cmd -Update
  setup-windows.cmd -InstallDir D:\Swarm -Mirror https://mirrors.kernel.org/sourceware/cygwin/
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$Mirror,
    [ValidatePattern('^(v[0-9]+\.[0-9]+\.[0-9]+)?$')]
    [string]$NodeVersion,
    [string]$PiPackage = '@earendil-works/pi-coding-agent',
    [string]$RepoDir,
    [string]$Wheelhouse,
    [switch]$Force,
    [switch]$Update,
    [switch]$SkipCygwin,
    [switch]$SkipNode,
    [switch]$SkipPi,
    [switch]$SkipVenv,
    [switch]$SkipSettings,
    [switch]$SkipLauncher,
    [ValidatePattern('^([a-fA-F0-9]{64})?$')]
    [string]$SetupSha256
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # speeds up Invoke-WebRequest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

# --- Resolve paths / defaults ---
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA 'Swarm' }
if (-not $Mirror)     { $Mirror = 'https://mirrors.kernel.org/sourceware/cygwin/' }
if (-not $RepoDir)    { $RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
if ($InstallDir -notmatch '^[A-Za-z]:[\\/]' -or $RepoDir -notmatch '^[A-Za-z]:[\\/]') {
    throw 'InstallDir and RepoDir must be absolute Windows paths on a local drive (for example C:/Users/you/Swarm). UNC paths are not supported.'
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$RepoDir = [IO.Path]::GetFullPath($RepoDir)
$mirrorUri = [Uri]$Mirror
if (-not $mirrorUri.IsAbsoluteUri -or $mirrorUri.Scheme -ne 'https') {
    throw 'The Cygwin mirror must use HTTPS.'
}
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Swarm requires 64-bit Windows.' }

# Expose to component modules via $script: scope (common.ps1 functions rely on these).
$script:InstallDir   = $InstallDir
$script:CacheDir     = Join-Path $InstallDir 'cache'
$script:LogDir       = Join-Path $InstallDir 'logs'
$script:CygwinRoot   = Join-Path $InstallDir 'cygwin'
$script:NodeDir      = Join-Path $InstallDir 'node'
$script:PiDir        = Join-Path $InstallDir 'pi'
$script:VenvDir      = Join-Path $InstallDir 'venv'
$script:RepoDir      = $RepoDir
$script:Wheelhouse   = $Wheelhouse
$script:Mirror       = $Mirror
$script:NodeVersion  = $NodeVersion
$script:PiPackage    = $PiPackage
$script:Force        = $Force
$script:Update       = $Update
$script:WindowsScriptsDir = $PSScriptRoot
$script:Step         = 'init'
$script:LogPath      = $null
$script:SetupSha256  = $SetupSha256

# --- Dot-source component modules ---
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\cygwin.ps1')
. (Join-Path $PSScriptRoot 'lib\node.ps1')
. (Join-Path $PSScriptRoot 'lib\pi-npm.ps1')
. (Join-Path $PSScriptRoot 'lib\venv.ps1')
. (Join-Path $PSScriptRoot 'lib\settings.ps1')
. (Join-Path $PSScriptRoot 'lib\launcher.ps1')

# --- Prepare install + log dirs, start transcript ---
Ensure-Dir $InstallDir
Ensure-Dir $CacheDir
Ensure-Dir $LogDir
Ensure-Dir (Join-Path $LogDir 'cygwin')

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$script:LogPath = Join-Path $LogDir "install-$stamp.log"
try {
    Start-Transcript -LiteralPath $script:LogPath -Force | Out-Null
} catch {
    # Start-Transcript can fail in some non-host shells; continue without it.
    Write-Warn "transcript could not be started; console output only."
}

try {
    Write-Step "Swarm for Windows installer"
    Write-Info "install dir: $InstallDir"
    Write-Info "repo dir:    $RepoDir"
    Write-Info "mirror:      $Mirror"
    Write-Info "pi package:  $PiPackage"
    Write-Info "force: $Force  update: $Update"
    if (-not (Test-Path -LiteralPath (Join-Path $RepoDir 'pyproject.toml'))) {
        Fail "pyproject.toml not found in repo dir '$RepoDir'" 2
    }

    if (-not $SkipCygwin) { Install-Cygwin }
    if (-not $SkipNode)   { Install-Node -Version $NodeVersion }
    if ((Test-CygwinInstalled) -and (Test-NodeInstalled)) {
        Write-Step 'Checking native Node terminal support inside tmux'
        $bash = Join-Path $script:CygwinRoot 'bin\bash.exe'
        & $bash (To-CygwinPath (Join-Path $PSScriptRoot 'probe-runtime.sh')) `
            (To-CygwinPath (Join-Path $PSScriptRoot 'probe.py')) `
            (To-CygwinPath (Join-Path $script:NodeDir 'node.exe'))
        if ($LASTEXITCODE -ne 0) { Fail 'Native Node terminal check failed; refusing to install a non-interactive Pi runtime.' 14 }
    }
    if (-not $SkipPi)     { Install-PiNpm }
    if (-not $SkipVenv)   { Install-SwarmVenv }
    if (-not $SkipLauncher) { New-LauncherBundle }
    # Only change the user's Pi shell after the provider-free runtime checks
    # pass. Trust/auth/model settings are never changed.
    if (-not $SkipSettings) { Configure-PiSettings }
    if (-not $SkipLauncher -and -not $SkipSettings) {
        Write-File-LF -Path (Join-Path $InstallDir 'ready.json') -Content (
            @{ version = 1; verifiedAt = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json
        )
    }

    Write-Step "Done"
    Write-Ok "Selected setup stages completed at $InstallDir"
    if ($SkipLauncher -or $SkipSettings) {
        Write-Warn "Launcher validation or shell configuration was skipped; no ready marker was written."
    }
    Write-Host ""
    Write-Host "  Launch:" -ForegroundColor Cyan
    Write-Host "    $InstallDir\swarm.cmd serve --open" -ForegroundColor White
    Write-Host "    (Git Bash)  $InstallDir\swarm serve --open" -ForegroundColor White
    Write-Host "  Logs:    $LogDir" -ForegroundColor Gray
    Write-Host "  Config:  $InstallDir\config.json" -ForegroundColor Gray
}
catch {
    Write-Err "unhandled error: $($_.Exception.Message)"
    Write-Err "at: $($_.InvocationInfo.PositionMessage)"
    Write-Err "log: $script:LogPath"
    exit 1
}
finally {
    try { Stop-Transcript } catch {}
}

exit 0
