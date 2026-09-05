# Play a Red Book audio CD via Windows Media Player's COM control - mpv on
# Windows has no libcdio, so cdda:// is "disabled at compile-time" there.
# WMPlayer.OCX only actually plays when hosted in a real window with a
# message pump; a bare `New-Object -ComObject` never leaves playState
# "Ready". Runs as a tiny persistent process, polled by the caller:
#
# Commands: caller writes one command to $CmdFile (PAUSE | STOP | NEXT |
# PREV | VOL:<0-100>); this script deletes it once consumed. A file-based
# channel instead of stdin - a background thread reading Console stdin
# here crashed the whole process outright (a piped stdin under a
# powershell.exe launched via -File from a console-less pythonw.exe
# parent apparently isn't safe to read from a second thread).
# Status (stdout): TRACK:<0-based index> | DONE | ERROR:<message>
#
# Usage: play-audio-cd.ps1 <drive e.g. D:> <command file path>
param(
    [Parameter(Mandatory = $true)] [string] $Drive,
    [Parameter(Mandatory = $true)] [string] $CmdFile
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

# Write-Output through a redirected/piped stdout (SSH, or Python's own
# subprocess.PIPE) buffers until the process exits instead of flushing
# per line - confirmed live (only the final "DONE" ever arrived; every
# "TRACK:" sent during actual playback was stuck in the buffer). Writing
# straight to the console stream and flushing after each line sidesteps
# PowerShell's own output-pipeline buffering.
function Send-Status([string]$msg) {
    [Console]::Out.WriteLine($msg)
    [Console]::Out.Flush()
}

try {
    $form = New-Object System.Windows.Forms.Form
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()

    $wmp = New-Object -ComObject WMPlayer.OCX.7
    $drives = $wmp.cdromCollection
    $target = $null
    for ($i = 0; $i -lt $drives.count; $i++) {
        if ($drives.Item($i).driveSpecifier.TrimEnd('\') -ieq $Drive.TrimEnd('\')) { $target = $drives.Item($i); break }
    }
    if (-not $target) { Send-Status "ERROR:No CD drive $Drive"; exit 2 }

    $wmp.currentPlaylist = $target.playlist
    $wmp.controls.play()
} catch {
    Send-Status ("ERROR:" + $_.Exception.Message)
    exit 1
}

$lastTrack = -1
while ($true) {
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.Application]::DoEvents()

    if ($wmp.playState -eq 8) { Send-Status "DONE"; break }  # wmppsMediaEnded

    # currentItem.playlistIndex comes back empty on this drive/build -
    # confirmed live - but .name reliably reads "Track N" for a CD
    # playlist item, so parse the number out of that instead.
    $track = -1
    try {
        $item = $wmp.controls.currentItem
        if ($item -and $item.name -match 'Track\s+(\d+)') { $track = [int]$Matches[1] - 1 }
    } catch {}
    if ($track -ge 0 -and $track -ne $lastTrack) {
        $lastTrack = $track
        Send-Status "TRACK:$track"
    }

    if (Test-Path -LiteralPath $CmdFile) {
        $line = (Get-Content -LiteralPath $CmdFile -Raw -ErrorAction SilentlyContinue)
        Remove-Item -LiteralPath $CmdFile -ErrorAction SilentlyContinue
        if ($line) {
            $line = $line.Trim()
            switch -Regex ($line) {
                '^PAUSE$' {
                    if ($wmp.playState -eq 3) { $wmp.controls.pause() } else { $wmp.controls.play() }
                }
                '^STOP$' { $wmp.controls.stop(); Send-Status "DONE"; break }
                '^NEXT$' { $wmp.controls.next() }
                '^PREV$' { $wmp.controls.previous() }
                '^VOL:(\d+)$' { $wmp.settings.volume = [Math]::Min(100, [Math]::Max(0, [int]$Matches[1])) }
            }
        }
    }
}

try { $wmp.controls.stop(); $wmp.close() } catch {}
try { $form.Close() } catch {}
