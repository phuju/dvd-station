# Optical drive + media state for DiscStation. Emits one JSON line.
# Works on PowerShell 2.0 (Win7) and later. Optional arg: a drive letter ("D:")
# to force; otherwise the first optical drive is used.
param([string]$Drive = "")

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_json.ps1")

$out = @{ drive = ""; media_loaded = $false; blank = $false; label = "";
          fs = ""; media_type = ""; rewritable = $false; capacity_bytes = 0 }

try {
    $cd = @(Get-WmiObject Win32_CDROMDrive)
    if ($Drive) { $cd = @($cd | Where-Object { $_.Drive -eq $Drive }) }
    if ($cd.Count -eq 0) { Write-Output (ConvertTo-JsonCompat $out); exit 0 }
    $d = $cd[0]
    $out.drive = $d.Drive
    # NOTE: [bool]"False" is $true in PowerShell (any non-empty string casts
    # truthy) -- compare explicitly instead of casting.
    $out.media_loaded = ($d.MediaLoaded -eq $true) -or ("$($d.MediaLoaded)" -eq "True")
} catch { Write-Output (ConvertTo-JsonCompat $out); exit 0 }

if (-not $out.media_loaded) { Write-Output (ConvertTo-JsonCompat $out); exit 0 }

# Volume label + filesystem (WMI logical disk).
try {
    $ld = Get-WmiObject Win32_LogicalDisk -Filter ("DeviceID='" + $out.drive + "'")
    if ($ld) {
        if ($ld.VolumeName) { $out.label = $ld.VolumeName }
        if ($ld.FileSystem) {
            $fs = $ld.FileSystem.ToLower()
            if ($fs -match "udf") { $out.fs = "udf" }
            elseif ($fs -match "cdfs|iso9660") { $out.fs = "iso9660" }
            else { $out.fs = $fs }
        }
        if ($ld.Size) { $out.capacity_bytes = [int64]$ld.Size }
    }
} catch {}

# IMAPI2: physical media type, blank flag, recordable capacity.
try {
    $master = New-Object -ComObject "IMAPI2.MsftDiscMaster2"
    for ($i = 0; $i -lt $master.Count; $i++) {
        $rec = New-Object -ComObject "IMAPI2.MsftDiscRecorder2"
        $rec.InitializeDiscRecorder($master.Item($i))
        $match = $false
        foreach ($p in $rec.VolumePathNames) { if ($p -and $p.TrimEnd('\') -ieq $out.drive) { $match = $true } }
        if (-not $match) { continue }
        $fmt = New-Object -ComObject "IMAPI2.MsftDiscFormat2Data"
        if (-not $fmt.IsRecorderSupported($rec)) { break }
        $fmt.Recorder = $rec
        $fmt.ClientName = "DiscStation"
        try { $out.blank = ($fmt.MediaHeuristicallyBlank -eq $true) } catch {}
        try { if ($fmt.MediaPhysicallyBlank) { $out.blank = $true } } catch {}
        try { $out.capacity_bytes = [int64]$fmt.TotalSectorsOnMedia * 2048 } catch {}
        $t = 0; try { $t = [int]$fmt.CurrentPhysicalMediaType } catch {}
        # IMAPI_MEDIA_PHYSICAL_TYPE
        $map = @{ 1="cd-rom"; 2="cd-r"; 3="cd-rw"; 4="dvd-rom"; 5="dvd-r"; 6="dvd-ram";
                  7="dvd+r"; 8="dvd+rw"; 9="dvd+r dl"; 10="dvd-r dl"; 12="dvd+rw dl";
                  16="bd-rom"; 17="bd-r"; 18="bd-re" }
        if ($map.ContainsKey($t)) { $out.media_type = $map[$t] }
        if ($out.media_type -match "rw|ram|-re") { $out.rewritable = $true }
        break
    }
} catch {}

# An audio CD has readable media, no filesystem, and (usually) no IMAPI type.
if (-not $out.fs -and -not $out.blank -and ($out.media_type -eq "" -or $out.media_type -match "^cd")) {
    try {
        $ld2 = Get-WmiObject Win32_CDROMDrive -Filter ("Drive='" + $out.drive + "'")
        # Win32_CDROMDrive has no track info; treat "media loaded, no FS, not blank" as audio.
        $out.media_type = "audio_cd"
    } catch {}
}

Write-Output (ConvertTo-JsonCompat $out)
