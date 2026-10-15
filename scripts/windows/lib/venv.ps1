# Swarm venv component: create a dedicated Cygwin Python venv (separate from the
# repo .venv) and pip install the Swarm package in editable mode. Because
# pydantic-core has no Cygwin wheel, the first install may compile it; the
# Cygwin gcc/rust toolchain installed earlier satisfies that requirement.

function Test-VenvInstalled {
    if (-not (Test-Path -LiteralPath (Join-Path $script:VenvDir 'pyvenv.cfg'))) { return $false }
    $bash = Join-Path $script:CygwinRoot 'bin\bash.exe'
    if (-not (Test-Path -LiteralPath $bash)) { return $false }
    try {
        $probe = To-CygwinPath (Join-Path $script:WindowsScriptsDir 'venv-probe.sh')
        $venv = To-CygwinPath $script:VenvDir
        & $bash $probe $venv *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Install-SwarmVenv {
    Write-Step "Installing Swarm into a dedicated Cygwin venv"

    if (-not (Test-CygwinInstalled)) {
        Fail "Cygwin is required before creating the venv." 40
    }
    if (-not (Test-Path -LiteralPath (Join-Path $script:RepoDir 'pyproject.toml'))) {
        Fail "pyproject.toml not found under repo dir '$($script:RepoDir)'." 41
    }

    $repoCygwin = To-CygwinPath $script:RepoDir
    $venvCygwin = To-CygwinPath $script:VenvDir
    $createVenv = $false
    if (-not (Test-VenvInstalled) -or $script:Force) {
        $createVenv = $true
        if (Test-Path -LiteralPath $script:VenvDir) {
            Remove-Item -LiteralPath $script:VenvDir -Recurse -Force
        }
    }

    # A self-contained bash script receives every path via process env, so no
    # shell interpolation of paths (spaces / apostrophes safe) occurs.
    # NOTE: $venvScript is a local here-string variable; it intentionally does
    # NOT use the `$script:` scope modifier and does not shadow it (PowerShell
    # parses `$script:Name` as a scope-qualified reference, distinct from a
    # variable named `script`). Renamed from `$script` to avoid any confusion.
    $venvScript = @'
#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/bin:/bin:$PATH"
export LANG="${LANG:-C.UTF-8}"
echo "[venv] python: $(python3.12 --version 2>&1)"
if [ "$SWARM_CREATE_VENV" = "1" ]; then
  echo "[venv] creating venv at $SWARM_VENV_CYGWIN"
  # Use Cygwin's packaged maturin rather than trying to bootstrap a Rust build
  # backend from PyPI (there are no Cygwin wheels for it).
  # Cygwin ships pip separately and omits ensurepip's bundled wheels.
  python3.12 -m venv --without-pip --system-site-packages "$SWARM_VENV_CYGWIN"
fi
PY="$SWARM_VENV_CYGWIN/bin/python"
echo "[venv] checking packaged build toolchain"
rustc --version
cargo --version
maturin --version
echo "[venv] checking Cygwin-packaged Python build backends"
"$PY" -c "import pip, setuptools, wheel, hatchling, hatch_fancy_pypi_readme, Cython, maturin"
echo "[venv] building native dependencies (first run compiles pydantic-core)"
# Keep wheels per exact interpreter/platform. Cygwin's maturin emits an
# incorrect platform tag; repair ONLY locally built wheels before installing.
platform=$("$PY" -c 'import sysconfig; print(sysconfig.get_platform())')
wheels="$SWARM_CACHE_CYGWIN/wheels/$platform"
mkdir -p "$wheels"
pip_args=(--no-build-isolation --find-links "$wheels")
if [ -n "${SWARM_WHEELHOUSE_CYGWIN:-}" ]; then
  pip_args+=(--no-index --find-links "$SWARM_WHEELHOUSE_CYGWIN")
fi
# Cygwin's hatchling package omits these pure-Python metadata dependencies.
# Make them available before any source distribution invokes that backend.
"$PY" -m pip install "${pip_args[@]}" tomlkit trove-classifiers
"$PY" -m pip wheel "${pip_args[@]}" --wheel-dir "$wheels" "$SWARM_REPO_CYGWIN" tomlkit trove-classifiers
"$PY" "$SWARM_RETAG_SCRIPT" "$wheels"
"$PY" -m pip install --no-build-isolation --no-index --find-links "$wheels" \
  --force-reinstall --upgrade --upgrade-strategy eager -e "$SWARM_REPO_CYGWIN" tomlkit trove-classifiers
"$PY" -m pip check
echo "[venv] smoke: import full server/reporting stack"
"$PY" -c "import swarm.server, swarm.cli; print('swarm server/CLI imports ok')"
'@

    $tmp = Join-Path $script:CacheDir ('venv-install-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.sh')
    Write-File-LF -Path $tmp -Content $venvScript

    $envVars = @{
        SWARM_REPO_CYGWIN   = $repoCygwin
        SWARM_VENV_CYGWIN   = $venvCygwin
        SWARM_CREATE_VENV   = if ($createVenv) { '1' } else { '0' }
        SWARM_CACHE_CYGWIN  = (To-CygwinPath $script:CacheDir)
        SWARM_RETAG_SCRIPT  = (To-CygwinPath (Join-Path $script:WindowsScriptsDir 'retag-wheels.py'))
    }
    if ($script:Wheelhouse) {
        if (-not (Test-Path -LiteralPath $script:Wheelhouse -PathType Container)) {
            Fail "Wheelhouse directory does not exist: $script:Wheelhouse" 43
        }
        $envVars['SWARM_WHEELHOUSE_CYGWIN'] = To-CygwinPath $script:Wheelhouse
    }
    Write-Info "repo (cygwin):   $repoCygwin"
    Write-Info "venv (cygwin):   $venvCygwin"
    Write-Info "create venv:     $createVenv"
    Write-Info "pip install -e . (first run compiles pydantic-core via rustc/cargo; be patient)"
    Invoke-CygwinScript -ScriptPath $tmp -EnvVars $envVars

    if (-not (Test-VenvInstalled)) {
        Fail "venv python not found at $($script:VenvDir)\bin\python after install" 42
    }
    Write-Ok "Swarm installed in venv: $($script:VenvDir)"
}
