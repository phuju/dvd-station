# Burn a pre-built ISO to the optical drive via IMAPI2. Streams "PROGRESS:<pct>"
# lines to stdout. Usage: burn-image.ps1 <drive e.g. D:> <iso path> [speed]
param(
    [Parameter(Mandatory = $true)] [string] $Drive,
    [Parameter(Mandatory = $true)] [string] $Iso,
    [string] $Speed = ""
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Iso)) { Write-Error "ISO not found: $Iso"; exit 2 }

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
try { $fmt.ForceMediaToBeClosed = $true } catch {}
if ($Speed -and $Speed -match '^\d+') {
    try { $fmt.SetWriteSpeed([int]($Speed -replace '\D',''), $false) } catch {}
}

# Progress: IMAPI2 raises an Update event with sector counts. Not fatal if
# registration fails (seen on some setups: "Cannot register for the
# specified event... does not exist") - the burn itself doesn't need it,
# just no live PROGRESS lines.
try {
    Register-ObjectEvent -InputObject $fmt -EventName "Update" -SourceIdentifier "burn" -Action {
        $s = $EventArgs
        try {
            $done = [double]$s.LastWrittenLba
            $tot  = [double]$s.SectorCount
            if ($tot -gt 0) {
                $pct = [int]([math]::Min(99, $done * 100.0 / $tot))
                Write-Output "PROGRESS:$pct"
            }
        } catch {}
    } | Out-Null
} catch {}

$stream = New-Object -ComObject "ADODB.Stream"
$stream.Type = 1          # binary
$stream.Open()
$stream.LoadFromFile($Iso)

try {
    $fmt.Write($stream)
    Write-Output "PROGRESS:100"
    exit 0
} catch {
    Write-Error ("burn failed: " + $_.Exception.Message)
    exit 1
} finally {
    $stream.Close()
    Unregister-Event -SourceIdentifier "burn" -ErrorAction SilentlyContinue
    try { $rec.EjectMedia() } catch {}
}
