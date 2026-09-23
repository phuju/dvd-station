# Build a data-disc filesystem image from a folder (or single file) and burn it
# via IMAPI2 - no external mkisofs needed. Streams "PROGRESS:<pct>".
# Usage: burn-data.ps1 <drive e.g. D:> <source folder-or-file> <label> [speed]
param(
    [Parameter(Mandatory = $true)] [string] $Drive,
    [Parameter(Mandatory = $true)] [string] $Source,
    [Parameter(Mandatory = $true)] [string] $Label,
    [string] $Speed = ""
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Source)) { Write-Error "source not found: $Source"; exit 2 }

. (Join-Path $PSScriptRoot "_imapi.ps1")

$fmt = New-DataWriter $Drive $Speed

# Filesystem image: ISO9660 + Joliet + UDF, sized to the loaded media.
$fsi = New-Object -ComObject "IMAPI2FS.MsftFileSystemImage"
try { $fsi.ChooseImageDefaultsForMediaType($fmt.CurrentPhysicalMediaType) } catch {}
$fsi.FileSystemsToCreate = 7          # ISO9660 | Joliet | UDF
$fsi.VolumeName = ($Label -replace '[^A-Za-z0-9_\- ]', '').Substring(0, [Math]::Min(32, ($Label -replace '[^A-Za-z0-9_\- ]', '').Length))
$fsi.FreeMediaBlocks = -1            # -1 = use the whole disc

$item = Get-Item -LiteralPath $Source
if ($item.PSIsContainer) {
    # AddTree's 2nd arg is IncludeBaseDirectory: $false flattens a folder
    # child into just its contents at the disc root (dropping the folder
    # name entirely) - wrong for VIDEO_TS/AUDIO_TS or any subfolder, which
    # need to keep their own name. $true preserves it as a real subfolder.
    foreach ($child in Get-ChildItem -LiteralPath $Source) { $fsi.Root.AddTree($child.FullName, $true) }
} else {
    $fsi.Root.AddTree($item.FullName, $false)
}

$result = $fsi.CreateResultImage()
$stream = $result.ImageStream

Write-DiscStream $fmt $stream
exit $global:BurnExit
