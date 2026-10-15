# Shared helpers for the Swarm Windows installer. Dot-sourced by install.ps1
# and every component module. Relies on $script:* state set by install.ps1.

# --- Install-wide state (set by install.ps1 before dot-sourcing components) ---
# $script:InstallDir, $script:CacheDir, $script:LogDir
# $script:CygwinRoot, $script:NodeDir, $script:PiDir, $script:VenvDir
# $script:RepoDir, $script:Mirror, $script:PiPackage
# $script:Force, $script:Update, $script:Step

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    $script:Step = $Message
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
    Write-Host "[swarm:windows] $Message" -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
}

function Write-Ok {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  [ok]   $Message" -ForegroundColor Green
}

function Write-Info {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  [info] $Message" -ForegroundColor Gray
}

function Write-Warn {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  [warn] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([Parameter(Mandatory)][string]$Message)
    [Console]::Error.WriteLine("  [error] $Message")
}

# Hard failure: print to console + log (transcript captures console) and exit.
function Fail {
    param(
        [Parameter(Mandatory)][string]$Message,
        [int]$Code = 1
    )
    Write-Err $Message
    Write-Err "Failing step: $script:Step"
    if ($script:LogPath) { Write-Err "See log: $script:LogPath" }
    exit $Code
}

# Ensure a directory exists (user-writable target; no admin needed).
function Ensure-Dir {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Assert-HttpsResponse {
    param($Response)
    # Windows PowerShell uses HttpWebResponse; PowerShell 7 uses HttpResponseMessage.
    $uri = $Response.BaseResponse.ResponseUri
    if (-not $uri) { $uri = $Response.BaseResponse.RequestMessage.RequestUri }
    if (-not $uri -or $uri.Scheme -ne 'https') { Fail 'Refusing a download redirected away from HTTPS.' }
}

# Robust HTTPS download with retry. Fail-closed on TLS errors.
function Download-File {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Destination
    )
    # TLS 1.2 works on all supported Windows PowerShell/.NET versions.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (([Uri]$Url).Scheme -ne 'https') { Fail 'Downloads must use HTTPS.' }
    $attempts = 0
    $max = 3
    while ($true) {
        $attempts++
        try {
            # -UseBasicParsing avoids the IE engine dependency.
            $response = Invoke-WebRequest -Uri $Url -OutFile $Destination -PassThru -UseBasicParsing -TimeoutSec 120 -ErrorAction Stop
            Assert-HttpsResponse $response
            return
        } catch {
            if ($attempts -ge $max) {
                Fail "download failed after $max attempts: $Url`n      $($_.Exception.Message)"
            }
            Write-Warn "download attempt $attempts failed; retrying in 5s: $($_.Exception.Message)"
            Start-Sleep -Seconds 5
        }
    }
}

# Read a remote text resource as a string (TLS pinned to 1.2/1.3).
function Fetch-Text {
    param([Parameter(Mandatory)][string]$Url)
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (([Uri]$Url).Scheme -ne 'https') { Fail 'Downloads must use HTTPS.' }
    $attempts = 0
    while ($true) {
        $attempts++
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 120 -ErrorAction Stop
            Assert-HttpsResponse $response
            return $response.Content
        } catch {
            if ($attempts -ge 3) { Fail "fetch failed: $Url`n      $($_.Exception.Message)" }
            Start-Sleep -Seconds 5
        }
    }
}

# Convert a Windows path to a Cygwin POSIX path via the installed cygpath.exe.
# Requires Cygwin to already be installed (only used post-Cygwin steps).
function To-CygwinPath {
    param([Parameter(Mandatory)][string]$WindowsPath)
    $cygpath = Join-Path $script:CygwinRoot 'bin\cygpath.exe'
    if (-not (Test-Path -LiteralPath $cygpath)) {
        Fail "cygpath.exe not found at '$cygpath'; Cygwin install incomplete."
    }
    $result = & $cygpath -u $WindowsPath
    if ($LASTEXITCODE -ne 0) { Fail "cygpath failed for '$WindowsPath'" }
    return $result.Trim()
}

# Run a Cygwin bash login script from a temp file. Avoids -c quoting pitfalls
# for paths containing spaces or apostrophes: the script reads paths from env.
function Invoke-CygwinScript {
    param(
        [Parameter(Mandatory)][string]$ScriptPath,
        [hashtable]$EnvVars = @{}
    )
    $bash = Join-Path $script:CygwinRoot 'bin\bash.exe'
    if (-not (Test-Path -LiteralPath $bash)) { Fail "bash.exe not found at '$bash'" }
    # Forward as process environment so the script can reference $VAR without
    # any shell interpolation of path strings.
    $previous = @{}
    foreach ($k in $EnvVars.Keys) {
        $previous[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        Set-Item -Path "Env:$k" -Value $EnvVars[$k]
    }
    try {
        & $bash --noprofile --norc "$ScriptPath"
        $rc = $LASTEXITCODE
    } finally {
        foreach ($k in $EnvVars.Keys) {
            [Environment]::SetEnvironmentVariable($k, $previous[$k], 'Process')
        }
    }
    if ($rc -ne 0) { Fail "Cygwin script failed (exit $rc): $ScriptPath" }
}

# Single-quote a string for safe embedding into bash. Used only for trusted,
# installer-generated values written into env.sh (never for user argv).
function Bash-SingleQuote {
    param([Parameter(Mandatory)][string]$Value)
    # '...' with embedded ' split as '\'' — standard safe idiom.
    return "'" + ($Value -replace "'", "'\''") + "'"
}

# Write all files as UTF-8 with LF line endings (no BOM).
function Write-File-LF {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Content
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    # Normalize CRLF -> LF so generated launchers stay LF on Windows.
    $lf = $Content -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($Path, $lf, $utf8NoBom)
}
