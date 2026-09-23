# Shared IMAPI2 helpers, dot-sourced by the burn scripts (same pattern as _json.ps1).

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

# Apply an optional "8x"-style write speed; not fatal if the drive refuses it.
function Set-WriteSpeed($fmt, [string]$speed) {
    if ($speed -and $speed -match '^\d+') {
        try { $fmt.SetWriteSpeed([int]($speed -replace '\D',''), $false) } catch {}
    }
}

# A data-disc (IMAPI2.MsftDiscFormat2Data) writer bound to the drive letter.
function New-DataWriter([string]$letter, [string]$speed) {
    $rec = Get-Recorder $letter
    $fmt = New-Object -ComObject "IMAPI2.MsftDiscFormat2Data"
    if (-not $fmt.IsRecorderSupported($rec)) { Write-Error "recorder not supported"; exit 3 }
    $fmt.Recorder = $rec
    $fmt.ClientName = "DiscStation"
    # Without this the disc session never finalizes - the drive reports the
    # disc as still blank afterward even though the data is physically there.
    try { $fmt.ForceMediaToBeClosed = $true } catch {}
    Set-WriteSpeed $fmt $speed
    return $fmt
}

# Write $stream to the disc, streaming "PROGRESS:<pct>" lines, then eject.
# Sets $global:BurnExit (0 ok, 1 failed) for the caller to `exit` with.
function Write-DiscStream($fmt, $stream) {
    # IMAPI2 raises an Update event with sector counts. Not fatal if
    # registration fails (seen on some setups: "Cannot register for the
    # specified event... does not exist") - the burn itself doesn't need it,
    # just no live PROGRESS lines.
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
        $global:BurnExit = 0
    } catch {
        Write-Error ("burn failed: " + $_.Exception.Message)
        $global:BurnExit = 1
    } finally {
        try { $stream.Close() } catch {}
        Unregister-Event -SourceIdentifier "burn" -ErrorAction SilentlyContinue
        try { $fmt.Recorder.EjectMedia() } catch {}
    }
}
