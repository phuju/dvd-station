# Burn a pre-built ISO to the optical drive via IMAPI2. Streams "PROGRESS:<pct>"
# lines to stdout. Usage: burn-image.ps1 <drive e.g. D:> <iso path> [speed]
param(
    [Parameter(Mandatory = $true)] [string] $Drive,
    [Parameter(Mandatory = $true)] [string] $Iso,
    [string] $Speed = ""
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Iso)) { Write-Error "ISO not found: $Iso"; exit 2 }
. (Join-Path $PSScriptRoot "_imapi.ps1")

$fmt = New-DataWriter $Drive $Speed
$stream = New-Object -ComObject "ADODB.Stream"
$stream.Type = 1          # binary
$stream.Open()
$stream.LoadFromFile($Iso)
Write-DiscStream $fmt $stream
exit $global:BurnExit
