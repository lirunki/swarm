#Requires -Version 5.1
# Run AFTER setup. Tests the real Git Bash/cmd launchers and ready marker while
# redirecting Pi configuration to a disposable profile, never the real user.
param([Parameter(Mandatory)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$id = [guid]::NewGuid().ToString('N')
$profile = Join-Path $repo ".browser-cache\profile's path-$id"
$relative = "./.browser-cache/registry with spaces's-$id"
$old = @{}
foreach ($key in @('USERPROFILE', 'PI_CODING_AGENT_DIR', 'SWARM_WINDOWS_INSTALL_DIR', 'SWARM_TEST_REPO', 'SWARM_TEST_RELATIVE_HOME', 'PSModuleAnalysisCachePath')) {
    $old[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
$hostExe = (Get-Process -Id $PID).Path
$installer = Join-Path $repo 'scripts\windows\install.ps1'
$cygpath = Join-Path $InstallDir 'cygwin\bin\cygpath.exe'

function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw "FAIL: $message" }
}

try {
    $agent = Join-Path $profile '.pi\agent'
    New-Item -ItemType Directory -Path $agent -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $agent 'settings.json'), '{"defaultProjectTrust":"ask","thinkingBudgets":{"high":1234}}')
    $env:USERPROFILE = $profile
    $env:PSModuleAnalysisCachePath = Join-Path $profile 'powershell-module-cache'
    Remove-Item Env:PI_CODING_AGENT_DIR -ErrorAction SilentlyContinue
    $env:SWARM_WINDOWS_INSTALL_DIR = $InstallDir
    & $hostExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installer `
        -InstallDir $InstallDir -SkipCygwin -SkipNode -SkipPi -SkipVenv
    Assert-True ($LASTEXITCODE -eq 0) 'runtime smoke/setup failed'
    Assert-True (Test-Path -LiteralPath (Join-Path $InstallDir 'ready.json')) 'validated setup did not create ready marker'
    $settings = Get-Content -LiteralPath (Join-Path $agent 'settings.json') -Raw | ConvertFrom-Json
    Assert-True ($settings.defaultProjectTrust -eq 'ask') 'setup changed project trust'
    Assert-True ($settings.thinkingBudgets.high -eq 1234) 'setup changed another setting'
    Assert-True ($settings.shellPath -eq ((Join-Path $InstallDir 'cygwin\bin\bash.exe') -replace '\\', '/')) 'setup shellPath is incorrect'

    $env:SWARM_TEST_REPO = $repo
    $env:SWARM_TEST_RELATIVE_HOME = $relative
    $gitBash = Join-Path $env:ProgramFiles 'Git\bin\bash.exe'
    Assert-True (Test-Path -LiteralPath $gitBash) 'Git for Windows Bash not found'
    $json = & $gitBash --noprofile --norc (Join-Path $PSScriptRoot 'git-bash-probe.sh')
    Assert-True ($LASTEXITCODE -eq 0) 'Git Bash launcher failed'
    $state = ($json -join "`n") | ConvertFrom-Json
    $repoCygwin = (& $cygpath -u $repo).Trim()
    Assert-True ($state.home -eq ($repoCygwin + '/' + $relative.Substring(2))) 'launcher changed cwd or mangled quoted path'
    Assert-True ($state.health.tmux -and $state.health.pi) 'Git Bash launcher lost runtime PATH'

    $cmdHome = Join-Path $profile "cmd registry's path"
    $json = & (Join-Path $InstallDir 'swarm.cmd') --home $cmdHome doctor
    Assert-True ($LASTEXITCODE -eq 0) 'cmd launcher failed'
    $state = ($json -join "`n") | ConvertFrom-Json
    Assert-True ($state.home -eq (& $cygpath -u $cmdHome).Trim()) 'cmd launcher mangled path with spaces/apostrophe'
    Assert-True ($state.health.tmux -and $state.health.pi) 'cmd launcher lost runtime PATH'
    Write-Host 'PASS: ready marker, isolated shell configuration, actual Git Bash/cmd launchers, cwd and quoted paths'
} finally {
    foreach ($key in $old.Keys) { [Environment]::SetEnvironmentVariable($key, $old[$key], 'Process') }
    # Rebuild the test bundle for the original environment, but do not modify
    # real Pi settings or leave a fixture profile recorded as a ready install.
    & $hostExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installer `
        -InstallDir $InstallDir -SkipCygwin -SkipNode -SkipPi -SkipVenv -SkipSettings
    $restoreCode = $LASTEXITCODE
    if (Test-Path -LiteralPath $profile) { Remove-Item -LiteralPath $profile -Recurse -Force }
    $registry = Join-Path $repo $relative.Substring(2)
    if (Test-Path -LiteralPath $registry) { Remove-Item -LiteralPath $registry -Recurse -Force }
    if ($restoreCode -ne 0) { throw 'Failed to restore the test runtime bundle after fixture checks' }
}
