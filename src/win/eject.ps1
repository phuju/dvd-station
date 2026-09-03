# Eject or close the optical tray via IMAPI2, with a Shell.Application fallback.
# Usage: eject.ps1 <drive letter e.g. D:> [close]
param([string]$Drive = "", [switch]$Close)

$ErrorActionPreference = "Stop"
$ok = $false

try {
    $master = New-Object -ComObject "IMAPI2.MsftDiscMaster2"
    for ($i = 0; $i -lt $master.Count; $i++) {
        $rec = New-Object -ComObject "IMAPI2.MsftDiscRecorder2"
        $rec.InitializeDiscRecorder($master.Item($i))
        $match = -not $Drive
        foreach ($p in $rec.VolumePathNames) { if ($p -and $Drive -and $p.TrimEnd('\') -ieq $Drive) { $match = $true } }
        if (-not $match) { continue }
        if ($Close) { $rec.CloseTray() } else { $rec.EjectMedia() }
        $ok = $true
        break
    }
} catch {}

if (-not $ok -and -not $Close -and $Drive) {
    try {
        $sh = New-Object -ComObject "Shell.Application"
        $sh.Namespace(17).ParseName($Drive).InvokeVerb("Eject")
        $ok = $true
    } catch {}
}

if ($ok) { exit 0 } else { Write-Error "eject failed"; exit 1 }
