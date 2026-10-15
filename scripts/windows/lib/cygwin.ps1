# Cygwin component: download official setup-x86_64.exe over HTTPS, verify its
# Authenticode signature (fail closed), then run a --no-admin quiet install
# into a user-writable root with an explicit trusted HTTPS mirror.

# Default trusted HTTPS mirror (configurable via -Mirror).
$script:DefaultMirror = 'https://mirrors.kernel.org/sourceware/cygwin/'

# Pinned SHA256 of the official Cygwin setup-x86_64.exe bootstrap artifact.
# Verified against the Authenticode-signed artifact fetched from
# https://www.cygwin.com/setup-x86_64.exe (signer: Jon Turney, the Cygwin setup
# maintainer). This is a *bootstrap integrity* pin: it pins exactly one released
# build of setup so a tampered/attacker exe (even one that re-signs) is rejected.
#
# Cygwin periodically re-releases setup-x86_64.exe; when that happens the hash
# changes and this pin must be refreshed. Refresh procedure (documented in
# scripts/windows/README.md):
#   1. Download https://www.cygwin.com/setup-x86_64.exe
#   2. Get-AuthenticodeSignature must report Status=Valid and a Jon Turney signer
#   3. (Get-FileHash -Algorithm SHA256).Hash -> update $script:SetupKnownSha256
#   4. Update the 'fetched' date comment below.
#
# fetched: 2026-10-15 (SHA256 2c9f2fb56e1fb687b5d9680afa8f8b06e6214f0e483096af0eae1946431226c5)
$script:SetupKnownSha256 = '2c9f2fb56e1fb687b5d9680afa8f8b06e6214f0e483096af0eae1946431226c5'

# Python 3.12 packages available in the Cygwin x86_64 setup index, plus the
# build toolchain for
# pydantic-core, which has no Cygwin wheel and must compile from sdist. rustc/
# cargo are the actual compilers; python312-maturin lets pydantic-core build via
# its maturin backend. If a name is unavailable, Cygwin setup exits nonzero and
# we fail closed.
$script:CygwinPackages = @(
    'python312',
    'python312-pip',
    'python312-devel',
    'python312-wheel',
    'python312-setuptools',
    'python312-maturin',
    'python312-cython',
    'python312-hatchling',
    'python312-hatch-fancy-pypi-readme',
    'gcc-g++',
    'make',
    'pkg-config',
    'libffi-devel',
    'libssl-devel',
    'zlib-devel',
    'git',
    'tmux',
    'curl',
    'wget',
    'ca-certificates',
    'rustc',
    'cargo'
)

function Test-CygwinInstalled {
    $bash = Join-Path $script:CygwinRoot 'bin\bash.exe'
    if (-not (Test-Path -LiteralPath $bash)) { return $false }
    try {
        $probe = To-CygwinPath (Join-Path $script:WindowsScriptsDir 'cygwin-probe.sh')
        & $bash $probe *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Install-Cygwin {
    Write-Step "Installing user-local Cygwin (no admin)"
    Ensure-Dir $script:CacheDir
    Ensure-Dir $script:CygwinRoot

    if ((Test-CygwinInstalled) -and -not $script:Force) {
        Write-Ok "Cygwin already present at $($script:CygwinRoot); skipping (use -Force to reinstall)"
        return
    }
    if ((Test-CygwinInstalled) -and $script:Force) {
        Write-Warn "-Force: reinstalling Cygwin over existing root (packages re-fetched)"
    }

    $setupUrl = 'https://www.cygwin.com/setup-x86_64.exe'
    $setupExe = Join-Path $script:CacheDir 'setup-x86_64.exe'

    Write-Info "downloading official Cygwin setup: $setupUrl"
    Download-File -Url $setupUrl -Destination $setupExe

    # --- Pinned SHA256 bootstrap integrity check (fail closed) ---
    $expectedHash = $script:SetupSha256
    if (-not $expectedHash) { $expectedHash = $script:SetupKnownSha256 }
    $expectedHash = $expectedHash.ToLower()
    Write-Info "verifying SHA256 pin of setup-x86_64.exe (expected $expectedHash)"
    $actualHash = (Get-FileHash -LiteralPath $setupExe -Algorithm SHA256).Hash.ToLower()
    if ($actualHash -ne $expectedHash) {
        Fail "Cygwin setup SHA256 mismatch. The official installer may have changed; update Swarm and retry. Do not run an unverified installer. expected: $expectedHash actual: $actualHash" 13
    }
    Write-Ok "SHA256 pin verified"

    # --- Authenticode verification (fail closed) ---
    # Official setup-x86_64.exe is Authenticode-signed by Jon Turney (the Cygwin
    # setup maintainer) with a Sectigo code-signing cert, timestamp-countersigned
    # so it stays Valid after cert expiry. A GPG detached .sig also exists from
    # the same maintainer key 56405CF6FCC81574682A5D561A698DE9E2E56300, but gpg is
    # not guaranteed at bootstrap, so Authenticode (always available on Windows)
    # is the practical gate; the pinned SHA256 above is defense-in-depth.
    Write-Info "verifying Authenticode signature of setup-x86_64.exe"
    $sig = Get-AuthenticodeSignature -FilePath $setupExe
    if ($sig.Status -ne 'Valid') {
        Fail "Cygwin setup signature is '$($sig.Status)' (expected Valid). Refusing to run unsigned/tampered setup." 10
    }
    $subj = $sig.SignerCertificate.Subject
    # Cygwin setup is signed by its maintainer Jon Turney; require that CN.
    if ($subj -notmatch '(^|,\s*)CN=Jon Turney(,|$)') {
        Fail "Cygwin setup signer does not match expected publisher (Jon Turney). signer subject: $subj" 11
    }
    Write-Ok "signature valid; signer: $subj"

    # --- Build package list string (comma-separated, no spaces) ---
    $pkgList = ($script:CygwinPackages -join ',')

    # --- Quiet --no-admin install to the user-writable root ---
    # --no-admin             : never request elevation; fail if root needs admin
    # --quiet-mode           : non-interactive
    # --no-desktop --no-startmenu : do not create shortcuts
    # --site                 : explicit trusted HTTPS mirror
    # --local-package-dir    : cache so re-runs are idempotent and offline-friendly
    $args = @(
        '--no-admin',
        '--quiet-mode',
        '--no-desktop',
        '--no-startmenu',
        '--root', $script:CygwinRoot,
        '--site', $script:Mirror,
        '--local-package-dir', $script:CacheDir,
        '--packages', $pkgList
    )

    Write-Info "mirror: $($script:Mirror)"
    Write-Info "root:   $($script:CygwinRoot)"
    Write-Info "packages: $pkgList"
    Write-Info "running Cygwin setup (this may take several minutes)..."

    $logFile = Join-Path $script:LogDir ('cygwin-setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Write-Info "setup log: $logFile"

    # setup-x86_64.exe is a Windows GUI/console hybrid; capture stdout/stderr.
    & $setupExe @args *>&1 | Tee-Object -FilePath $logFile
    $rc = $LASTEXITCODE

    # Fail closed on any nonzero exit. Cygwin setup returns 0 on a clean
    # install; a nonzero code means a package was unavailable, a download
    # failed, or the root was not writable. Do not pretend partial success.
    if ($rc -ne 0) {
        Fail "Cygwin setup exited with code $rc. See $logFile. Common causes: a package name is unavailable at the mirror (try -Mirror https://mirrors.kernel.org/sourceware/cygwin/), a download failed, or the root needs admin. Use the matching -Skip* flag to re-run a stage." 12
    }
    if (-not (Test-CygwinInstalled)) {
        Fail "Cygwin setup returned success but the Python/tmux/build-tool prerequisite probe failed. Inspect the package list and $logFile; installation is incomplete." 12
    }
    Write-Ok "Cygwin installed: $($script:CygwinRoot)"
}
