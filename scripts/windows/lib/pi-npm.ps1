# Pi (npm) component: install the private Pi coding agent package with
# --ignore-scripts (no postinstall lifecycle). Uses the private Node/npm only;
# nothing is added to the global PATH or the Git Bash installation.

function Test-PiInstalled {
    $entry = Join-Path $script:PiDir "node_modules\$($script:PiPackage)\dist\cli.js"
    if (-not (Test-Path -LiteralPath $entry)) { return $false }
    try {
        & (Join-Path $script:NodeDir 'node.exe') $entry --version *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Install-PiNpm {
    Write-Step "Installing Pi ($($script:PiPackage)) via npm --ignore-scripts"

    if (-not (Test-NodeInstalled)) {
        Fail "Node is required before installing Pi; run the Node step first." 30
    }
    $nodeExe = Join-Path $script:NodeDir 'node.exe'
    # npm ships inside the Node zip as node_modules/npm; invoke via node.
    $npmCli = Join-Path $script:NodeDir 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $npmCli)) {
        Fail "npm-cli.js not found at '$npmCli'; Node install incomplete." 31
    }

    if ((Test-PiInstalled) -and -not $script:Update -and -not $script:Force) {
        Write-Ok "Pi already present; skipping (use -Update to refresh)"
        return
    }
    if (Test-PiInstalled) {
        Write-Info "refreshing Pi (--Update/--Force)"
    }

    Ensure-Dir $script:PiDir

    # Prepend private Node dir to PATH for this process only (npm needs node).
    $oldPath = $env:PATH
    $env:PATH = "$($script:NodeDir);$oldPath"
    try {
        $npmArgs = @(
            $npmCli,
            '--prefix', $script:PiDir,
            '--ignore-scripts',
            'install', $script:PiPackage
        )
        Write-Info "npm --prefix $($script:PiDir) --ignore-scripts install $($script:PiPackage)"
        & $nodeExe @npmArgs
        $rc = $LASTEXITCODE
    } finally {
        $env:PATH = $oldPath
    }
    if ($rc -ne 0) { Fail "npm install exited $rc for $($script:PiPackage)" 32 }
    if (-not (Test-PiInstalled)) {
        Fail "npm reported success but dist/cli.js is missing under $($script:PiDir)" 33
    }
    Write-Ok "Pi installed at $($script:PiDir)"
}
