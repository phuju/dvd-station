# Burn an audio CD (Red Book) from a folder of 16-bit / 44.1 kHz stereo WAV
# files via IMAPI2 Track-At-Once. Streams "PROGRESS:<pct>".
# Usage: burn-audio.ps1 <drive e.g. D:> <wav folder> [speed]
param(
    [Parameter(Mandatory = $true)] [string] $Drive,
    [Parameter(Mandatory = $true)] [string] $WavDir,
    [string] $Speed = ""
)

$ErrorActionPreference = "Stop"
$wavs = @(Get-ChildItem -LiteralPath $WavDir -Filter *.wav | Sort-Object Name)
if ($wavs.Count -eq 0) { Write-Error "no WAV files in $WavDir"; exit 2 }

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
$fmt = New-Object -ComObject "IMAPI2.MsftDiscFormat2TrackAtOnce"
if (-not $fmt.IsRecorderSupported($rec)) { Write-Error "recorder not supported"; exit 3 }
$fmt.Recorder = $rec
$fmt.ClientName = "DiscStation"
try { $fmt.NumberOfExistingTracks } catch {}
if ($Speed -and $Speed -match '^\d+') {
    try { $fmt.SetWriteSpeed([int]($Speed -replace '\D',''), $false) } catch {}
}

$prepared = @()
foreach ($w in $wavs) {
    # IMAPI2 wants raw 44100/16/2 PCM. Strip the 44-byte WAV header.
    $bytes = [System.IO.File]::ReadAllBytes($w.FullName)
    $offset = 44
    $idx = -1
    for ($i = 12; $i -lt [Math]::Min($bytes.Length - 8, 4096); $i++) {
        if ($bytes[$i] -eq 0x64 -and $bytes[$i+1] -eq 0x61 -and $bytes[$i+2] -eq 0x74 -and $bytes[$i+3] -eq 0x61) {
            $offset = $i + 8; break
        }
    }
    $raw = New-Object byte[] ($bytes.Length - $offset)
    [Array]::Copy($bytes, $offset, $raw, 0, $raw.Length)
    $prepared += ,@{ name = $w.Name; data = $raw }
}

$total = $prepared.Count
$done = 0
try {
    foreach ($t in $prepared) {
        $stream = New-Object -ComObject "ADODB.Stream"
        $stream.Type = 1; $stream.Open()
        $stream.Write($t.data)
        $stream.Position = 0
        $fmt.AddAudioTrack($stream)
        $stream.Close()
        $done++
        Write-Output ("PROGRESS:" + [int]([math]::Min(99, $done * 100.0 / $total)))
    }
    $fmt.Close()
    $fmt.Recorder.EjectMedia()
    Write-Output "PROGRESS:100"
    exit 0
} catch {
    Write-Error ("audio burn failed: " + $_.Exception.Message)
    exit 1
}
