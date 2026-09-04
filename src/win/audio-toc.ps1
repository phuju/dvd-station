# Read an audio CD's table of contents via IMAPI2 raw reader. Emits JSON:
#   {"track_count":N,"leadout":L,"tracks":[o1,o2,...]}   (frame offsets, +150)
# Usage: audio-toc.ps1 <drive e.g. D:>
param([Parameter(Mandatory = $true)] [string] $Drive)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_json.ps1")

function Get-Recorder([string]$letter) {
    $master = New-Object -ComObject "IMAPI2.MsftDiscMaster2"
    for ($i = 0; $i -lt $master.Count; $i++) {
        $rec = New-Object -ComObject "IMAPI2.MsftDiscRecorder2"
        $rec.InitializeDiscRecorder($master.Item($i))
        foreach ($p in $rec.VolumePathNames) { if ($p -and $p.TrimEnd('\') -ieq $letter) { return $rec } }
    }
    throw "No optical recorder for $letter"
}

$rec = Get-Recorder $Drive
$raw = New-Object -ComObject "IMAPI2.MsftDiscFormat2RawCD"
$raw.Recorder = $rec
$raw.ClientName = "DiscStation"

$toc = $raw.ReadDiscInformation()   # not always present; fall through to raw TOC
$fmt = New-Object -ComObject "IMAPI2.MsftDiscFormat2Data"
$fmt.Recorder = $rec

# MsftDiscFormat2RawCD.get_TocInformation() -> byte array of the raw TOC (MMC-3).
$bytes = $raw.ReadTocInformation()
# TOC header: [0..1]=data length, [2]=first track, [3]=last track.
$first = $bytes[2]; $last = $bytes[3]
$offsets = @()
$leadout = 0
for ($i = 4; $i + 7 -lt $bytes.Length; $i += 8) {
    $trk = $bytes[$i + 2]
    # LBA is big-endian in bytes [i+4..i+7]
    $lba = ($bytes[$i+4] -shl 24) -bor ($bytes[$i+5] -shl 16) -bor ($bytes[$i+6] -shl 8) -bor $bytes[$i+7]
    if ($trk -eq 0xAA) { $leadout = $lba + 150 }
    elseif ($trk -ge $first -and $trk -le $last) { $offsets += ($lba + 150) }
}
if ($offsets.Count -eq 0) { Write-Error "no audio tracks in TOC"; exit 2 }

Write-Output (ConvertTo-JsonCompat @{ track_count = $offsets.Count; leadout = $leadout; tracks = $offsets })
