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

function Get-Recorder([string]$letter) {
    $master = New-Object -ComObject "IMAPI2.MsftDiscMaster2"
    for ($i = 0; $i -lt $master.Count; $i++) {
        $rec = New-Object -ComObject "IMAPI2.MsftDiscRecorder2"
        $rec.InitializeDiscRecorder($master.Item($i))
        foreach ($p in $rec.VolumePathNames) {
            if ($p -and $p.TrimEnd('\') -ieq $letter) { return $rec }
        }
    }
    throw "No optical recorder for $letter"
}

$rec = Get-Recorder $Drive
$fmt = New-Object -ComObject "IMAPI2.MsftDiscFormat2Data"
if (-not $fmt.IsRecorderSupported($rec)) { Write-Error "recorder not supported"; exit 3 }
$fmt.Recorder = $rec
$fmt.ClientName = "DiscStation"
# Without this the disc session never finalizes - the drive reports the
# disc as still blank afterward even though the data is physically there.
try { $fmt.ForceMediaToBeClosed = $true } catch {}
if ($Speed -and $Speed -match '^\d+') {
    try { $fmt.SetWriteSpeed([int]($Speed -replace '\D',''), $false) } catch {}
}

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

# Not fatal if registration fails (seen on some setups: "Cannot register
# for the specified event... does not exist") - the burn itself doesn't
# need it, just no live PROGRESS lines.
try {
    Register-ObjectEvent -InputObject $fmt -EventName "Update" -SourceIdentifier "burn" -Action {
        $s = $EventArgs
        try {
            $done = [double]$s.LastWrittenLba
            $tot  = [double]$s.SectorCount
            if ($tot -gt 0) { Write-Output ("PROGRESS:" + [int]([math]::Min(99, $done * 100.0 / $tot))) }
        } catch {}
    } | Out-Null
} catch {}

try {
    $fmt.Write($stream)
    Write-Output "PROGRESS:100"
    exit 0
} catch {
    Write-Error ("burn failed: " + $_.Exception.Message)
    exit 1
} finally {
    Unregister-Event -SourceIdentifier "burn" -ErrorAction SilentlyContinue
    try { $rec.EjectMedia() } catch {}
}
