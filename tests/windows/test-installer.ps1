#Requires -Version 5.1
# Provider/network-free installer contracts. Run from Windows PowerShell:
# powershell -NoProfile -File tests/windows/test-installer.ps1
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$root = Join-Path ([IO.Path]::GetTempPath()) ("swarm's installer test-" + [guid]::NewGuid().ToString('N'))
$oldProfile = $env:USERPROFILE
$oldPiConfig = $env:PI_CODING_AGENT_DIR
$oldModuleCache = $env:PSModuleAnalysisCachePath

function Assert-True([bool]$condition, [string]$message) {
    if (-not $condition) { throw "FAIL: $message" }
}

try {
    New-Item -ItemType Directory -Path $root | Out-Null
    $script:LogPath = $null
    $script:Step = 'installer contract test'
    $script:CygwinRoot = Join-Path $root 'cygwin'
    . (Join-Path $repo 'scripts\windows\lib\common.ps1')
    . (Join-Path $repo 'scripts\windows\lib\settings.ps1')
    # No real Cygwin/Node installation and no real user config is needed.
    function Test-CygwinInstalled { return $true }
    $env:USERPROFILE = Join-Path $root 'profile'
    $env:PSModuleAnalysisCachePath = Join-Path $root 'powershell-module-cache'
    Remove-Item Env:PI_CODING_AGENT_DIR -ErrorAction SilentlyContinue
    $agent = Join-Path $env:USERPROFILE '.pi\agent'
    Ensure-Dir $agent
    $settings = Join-Path $agent 'settings.json'
    $original = '{"defaultModel":"keep","defaultProjectTrust":"ask","thinkingBudgets":{"high":1234},"packages":["a",{"source":"b","extensions":["keep"]}]}'
    Write-File-LF -Path $settings -Content $original
    Configure-PiSettings
    $actual = Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-True ($actual.defaultModel -eq 'keep') 'model setting changed'
    Assert-True ($actual.defaultProjectTrust -eq 'ask') 'trust setting changed'
    Assert-True ($actual.thinkingBudgets.high -eq 1234) 'nested setting changed'
    Assert-True ($actual.packages[1].extensions[0] -eq 'keep') 'package setting changed'
    Assert-True ($actual.shellPath -eq (Get-DesiredShellPath)) 'shellPath not set'
    $backups = @(Get-ChildItem -LiteralPath $agent -Filter 'settings.json.bak-*')
    Assert-True ($backups.Count -eq 1) 'original settings backup missing'
    Assert-True ((Get-Content -LiteralPath $backups[0].FullName -Raw) -eq $original) 'backup content differs'
    $first = [IO.File]::ReadAllBytes($settings)
    Configure-PiSettings
    Assert-True (@(Get-ChildItem -LiteralPath $agent -Filter 'settings.json.bak-*').Count -eq 1) 'idempotent rerun created another backup'
    Assert-True ([Convert]::ToBase64String($first) -eq [Convert]::ToBase64String([IO.File]::ReadAllBytes($settings))) 'idempotent rerun rewrote settings'
    Assert-True ((Bash-SingleQuote "a'b") -eq "'a'\''b'") 'bash apostrophe escaping failed'
    $utf = Join-Path $root 'utf8.sh'
    Write-File-LF -Path $utf -Content "first`r`nsecond`r`n"
    $bytes = [IO.File]::ReadAllBytes($utf)
    Assert-True ($bytes[0] -eq 102) 'UTF-8 file has BOM'
    Assert-True (-not ($bytes -contains 13)) 'shell script has CR line endings'

    $scripts = @(Get-ChildItem -LiteralPath (Join-Path $repo 'scripts\windows') -Recurse -Filter '*.ps1')
    $scripts += @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1')
    foreach ($file in $scripts) {
        $tokens = $null
        $errors = $null
        [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
        Assert-True ($errors.Count -eq 0) "PowerShell syntax errors in $($file.FullName): $errors"
    }
    # ParseFile alone misses validation metadata rejecting optional defaults.
    # Exercise parameter binding and fresh-root setup without network or installs.
    $dryRoot = Join-Path $root 'binding test'
    $hostExe = (Get-Process -Id $PID).Path
    & $hostExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `
        (Join-Path $repo 'scripts\windows\install.ps1') -InstallDir $dryRoot `
        -SkipCygwin -SkipNode -SkipPi -SkipVenv -SkipSettings -SkipLauncher *> $null
    Assert-True ($LASTEXITCODE -eq 0) 'installer optional defaults or fresh-root binding failed'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $dryRoot 'ready.json'))) 'partial setup marked itself ready'
    Write-Host 'PASS: settings preservation, backup/idempotence, quoting, UTF-8/LF, PowerShell syntax and installer defaults'
} finally {
    $env:USERPROFILE = $oldProfile
    $env:PI_CODING_AGENT_DIR = $oldPiConfig
    $env:PSModuleAnalysisCachePath = $oldModuleCache
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
