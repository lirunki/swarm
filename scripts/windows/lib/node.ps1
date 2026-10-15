# Node LTS component: download the official Windows zip, verify SHA256 against
# nodejs.org SHASUMS256.txt, extract to a private dir. No global PATH change.

# Resolve the latest Node.js LTS version from the official index.
function Resolve-NodeLtsVersion {
    Write-Info "resolving latest Node.js LTS version"
    $index = Fetch-Text 'https://nodejs.org/dist/index.json'
    try {
        $json = $index | ConvertFrom-Json
    } catch {
        Fail "could not parse Node.js index.json: $($_.Exception.Message)" 20
    }
    # Prefer the newest entry whose lts field is a non-false string.
    $lts = $json | Where-Object { $_.lts -and $_.lts -ne $false -and $_.lts -ne '' } |
        Sort-Object -Property { [version]$_.version.Substring(1) } -Descending |
        Select-Object -First 1
    if (-not $lts) { Fail "no Node.js LTS version found in index.json" 21 }
    Write-Ok "latest LTS: $($lts.version) ($($lts.lts))"
    return $lts.version
}

function Test-NodeInstalled {
    $node = Join-Path $script:NodeDir 'node.exe'
    $npm = Join-Path $script:NodeDir 'node_modules\npm\bin\npm-cli.js'
    return ((Test-Path -LiteralPath $node) -and (Test-Path -LiteralPath $npm))
}

function Install-Node {
    param([string]$Version)
    Write-Step "Installing private Node.js LTS"

    if ((Test-NodeInstalled) -and -not $script:Force) {
        Write-Ok "Node already present at $($script:NodeDir); skipping (use -Force to reinstall)"
        return
    }
    if (-not $Version) { $Version = Resolve-NodeLtsVersion }
    Ensure-Dir $script:CacheDir

    $zipName = "node-$Version-win-x64.zip"
    $url = "https://nodejs.org/dist/$Version/$zipName"
    $shasumsUrl = "https://nodejs.org/dist/$Version/SHASUMS256.txt"
    $zipPath = Join-Path $script:CacheDir $zipName
    $shasumsPath = Join-Path $script:CacheDir "SHASUMS256-$Version.txt"

    Write-Info "downloading $url"
    Download-File -Url $url -Destination $zipPath
    Download-File -Url $shasumsUrl -Destination $shasumsPath

    # --- SHA256 verification ---
    Write-Info "verifying SHA256 of $zipName"
    $expected = $null
    foreach ($line in (Get-Content -LiteralPath $shasumsPath)) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*\s*(.+)$') {
            if ($Matches[2] -eq $zipName) { $expected = $Matches[1].ToLower(); break }
        } elseif ($line -match '^\s*([0-9a-fA-F]{64})\s+(.+)$') {
            if ($Matches[2] -eq $zipName) { $expected = $Matches[1].ToLower(); break }
        }
    }
    if (-not $expected) {
        Fail "could not find expected SHA256 for $zipName in $shasumsUrl" 22
    }
    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Fail "Node.js zip SHA256 mismatch.`n      expected: $expected`n      actual:   $actual" 23
    }
    Write-Ok "SHA256 verified"

    # --- Extract ---
    if (Test-Path -LiteralPath $script:NodeDir) {
        Remove-Item -LiteralPath $script:NodeDir -Recurse -Force
    }
    Ensure-Dir $script:NodeDir
    # The zip contains a top-level folder node-<ver>-win-x64; expand to cache then move.
    $expandRoot = Join-Path $script:CacheDir "node-extract-$Version"
    if (Test-Path -LiteralPath $expandRoot) {
        Remove-Item -LiteralPath $expandRoot -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $expandRoot -Force
    $inner = Get-ChildItem -LiteralPath $expandRoot -Directory | Select-Object -First 1
    if (-not $inner) { Fail "Node.js zip did not contain an expected top-level folder" 24 }
    # Copy the *contents* of the top-level folder into NodeDir, not the folder
    # itself. (Copy-Item of the folder into an existing NodeDir would nest it as
    # NodeDir\node-<ver>-win-x64\node.exe, leaving NodeDir\node.exe missing.)
    Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $script:NodeDir -Recurse -Force
    Remove-Item -LiteralPath $expandRoot -Recurse -Force

    if (-not (Test-NodeInstalled)) {
        Fail "Node.js extraction completed but node.exe missing at $($script:NodeDir)" 25
    }
    $nodeVer = & (Join-Path $script:NodeDir 'node.exe') --version
    Write-Ok "Node installed: $nodeVer at $($script:NodeDir)"
}
