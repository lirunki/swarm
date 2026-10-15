# Pi settings component: configure the existing native Pi settings.json so Pi
# uses the Cygwin bash as its shell, WITHOUT overwriting any other settings,
# trust, or auth. A timestamped backup is always created before any change.

# Returns the desired shellPath (Windows path with forward slashes, as Pi docs
# require for JSON).
function Get-DesiredShellPath {
    $bash = Join-Path $script:CygwinRoot 'bin\bash.exe'
    return ($bash -replace '\\', '/')
}

function Configure-PiSettings {
    Write-Step "Configuring Pi shellPath (Cygwin bash) in settings.json"

    if (-not (Test-CygwinInstalled)) {
        Fail "Cygwin must be installed before configuring Pi settings." 50
    }
    $desired = Get-DesiredShellPath
    $piAgentDir = Join-Path $env:USERPROFILE '.pi\agent'
    if ($env:PI_CODING_AGENT_DIR) {
        $configured = $env:PI_CODING_AGENT_DIR
        if ($configured.StartsWith('~')) {
            $configured = Join-Path $env:USERPROFILE $configured.Substring(1).TrimStart('/', '\')
        }
        $piAgentDir = & (Join-Path $script:CygwinRoot 'bin\cygpath.exe') -aw -- $configured
        if ($LASTEXITCODE -ne 0) { Fail 'Could not resolve PI_CODING_AGENT_DIR' 52 }
        $piAgentDir = $piAgentDir.Trim()
    }
    Ensure-Dir $piAgentDir
    $settings = Join-Path $piAgentDir 'settings.json'

    # Load existing settings (or start from empty object). Preserve everything.
    $json = $null
    if (Test-Path -LiteralPath $settings) {
        $raw = Get-Content -LiteralPath $settings -Raw -Encoding UTF8
        if ($raw.Trim().Length -eq 0) { $raw = '{}' }
        try {
            $json = $raw | ConvertFrom-Json
        } catch {
            Fail "existing Pi settings.json is not valid JSON: $($_.Exception.Message)`n      Refusing to modify it; back it up manually and re-run." 51
        }
    } else {
        Write-Info "no existing settings.json; creating a new one with shellPath only"
        # ConvertFrom-Json returns PSCustomObject; treat uniformly below.
        $json = '{}' | ConvertFrom-Json
    }

    # Detect current shellPath (may be absent).
    $current = $null
    if ($json.PSObject.Properties.Name -contains 'shellPath') {
        $current = $json.shellPath
    }

    if ($current -eq $desired) {
        Write-Ok "shellPath already correct ($desired); no change"
        return
    }

    # --- Backup before modifying ---
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fffffff'
    $backup = $null
    if (Test-Path -LiteralPath $settings) {
        $backup = "$settings.bak-$stamp-$([guid]::NewGuid().ToString('N'))"
        Copy-Item -LiteralPath $settings -Destination $backup
        Write-Info "backed up existing settings.json to:`n      $backup"
    }

    # Set ONLY shellPath. All other properties are preserved as-is.
    if ($json.PSObject.Properties.Name -contains 'shellPath') {
        $json.shellPath = $desired
    } else {
        $json | Add-Member -NotePropertyName shellPath -NotePropertyValue $desired
    }

    # Serialize with indentation. -Depth 100 keeps nested objects intact.
    $out = $json | ConvertTo-Json -Depth 100
    $temporary = "$settings.swarm-$([guid]::NewGuid().ToString('N')).tmp"
    try {
        Write-File-LF -Path $temporary -Content $out
        Move-Item -LiteralPath $temporary -Destination $settings -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }

    Write-Ok "shellPath set to $desired"
    Write-Warn "Pi settings.json was modified (shellPath only). Other keys are preserved."
    if ($backup) { Write-Info "backup: $backup" }
    Write-Info "If Pi is running, restart it so the new shellPath takes effect."
}
