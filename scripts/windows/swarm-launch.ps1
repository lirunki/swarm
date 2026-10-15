# Swarm Windows launcher (static).
#
# Bridges exact Windows argv -> Cygwin bash runtime without string evaluation.
# $Remain is a real PowerShell array; splatting @Remain preserves each element
# as a distinct argv entry (spaces, apostrophes, and non-ASCII survive).
#
# Cygwin bash.exe receives: argv[1]=runtime script (POSIX path), argv[2..]=args.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Remain
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $dir 'config.json'

if (-not (Test-Path -LiteralPath $configPath)) {
    [Console]::Error.WriteLine("swarm: config.json not found at '$configPath'.")
    [Console]::Error.WriteLine("swarm: run setup-windows.cmd to install Swarm for Windows.")
    exit 3
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not (Test-Path -LiteralPath $config.bashExe)) {
    [Console]::Error.WriteLine("swarm: Cygwin bash not found at '$($config.bashExe)'.")
    [Console]::Error.WriteLine("swarm: re-run setup-windows.cmd to repair the installation.")
    exit 4
}

# Launch the static bash runtime, forwarding argv as a true array.
& $config.bashExe $config.runtimeScriptCygwin @Remain
exit $LASTEXITCODE
