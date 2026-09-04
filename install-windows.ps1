# DiscStation host installer for Windows 10/11 (and best-effort on 7 SP1).
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1
# Sets up: Python + venv, optical CLI tools (winget), self-signed cert,
# a per-user auto-start Scheduled Task, and inbound firewall rules for 8080/8081.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest's progress bar breaks over SSH/non-interactive hosts

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base   = if ($env:DISCSTATION_CONFIG_DIR) { $env:DISCSTATION_CONFIG_DIR } else { Join-Path $env:APPDATA "DiscStation" }
$App    = if ($env:DISCSTATION_APP_DIR)    { $env:DISCSTATION_APP_DIR }    else { Join-Path $Base "app" }
$Venv   = if ($env:DISCSTATION_VENV_DIR)   { $env:DISCSTATION_VENV_DIR }   else { Join-Path $Base "venv" }
$IsWin7 = [Environment]::OSVersion.Version.Major -eq 6
$winget = (Get-Command winget -ErrorAction SilentlyContinue)

function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Winget-Install($id) {
    if (-not $winget) { return $false }
    try { winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent | Out-Null; return $true }
    catch { Write-Host "  winget $id failed (skipping)"; return $false }
}

# --- 1. Python -------------------------------------------------------------------
if (-not (Have "python") -and -not (Have "py")) {
    Write-Host "Installing Python..."
    if ($winget -and -not $IsWin7) {
        Winget-Install "Python.Python.3.12" | Out-Null
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
    } else {
        $pyver = if ($IsWin7) { "3.8.10" } else { "3.12.6" }
        $url = "https://www.python.org/ftp/python/$pyver/python-$pyver-amd64.exe"
        $exe = Join-Path $env:TEMP "python-$pyver-amd64.exe"
        Invoke-WebRequest $url -OutFile $exe -UseBasicParsing
        Start-Process $exe -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1","Include_test=0" -Wait
        $env:Path = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$($pyver.Substring(0,4) -replace '\.','')") + ";" + $env:Path + ";" +
                    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$($pyver.Substring(0,4) -replace '\.','')\Scripts")
    }
}
if (Have "python") { $PyExe = "python"; $PyArgs = @() }
elseif (Have "py") { $PyExe = "py"; $PyArgs = @("-3") }
else { throw "Python install failed. Install Python 3 manually and re-run." }

# --- 2. Optical CLI tools (winget when available; Win10/11 only; best-effort,
#        strictly time-bounded so a slow/blocked mirror can never hang the install) --
if ($winget -and -not $IsWin7) {
    Write-Host "Installing optical tools via winget (best effort)..."
    foreach ($id in "libburnia.xorriso","mpv.mpv","HandBrake.HandBrake.CLI","Gyan.FFmpeg","yt-dlp.yt-dlp") {
        Winget-Install $id | Out-Null
    }
} elseif (-not $IsWin7) {
    Write-Host "winget unavailable - fetching optical tools directly (best effort, 25s timeout each)..."
    $tools = Join-Path $Base "tools"
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    function Get-Zip($url, $dest) {
        try {
            $zip = Join-Path $env:TEMP ([IO.Path]::GetFileName($url))
            Invoke-WebRequest $url -OutFile $zip -UseBasicParsing -TimeoutSec 25
            Expand-Archive -Path $zip -DestinationPath $dest -Force
            Remove-Item $zip -ErrorAction SilentlyContinue
            return $true
        } catch { Write-Host "  fetch failed: $url"; return $false }
    }
    if (-not (Have "yt-dlp")) {
        try { Invoke-WebRequest "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile (Join-Path $tools "yt-dlp.exe") -UseBasicParsing -TimeoutSec 25 }
        catch { Write-Host "  yt-dlp fetch skipped" }
    }
    if (-not (Have "ffmpeg")) { Get-Zip "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $tools | Out-Null }
    if (-not (Have "mpv")) {
        # The SourceForge "latest" mpv build is a .7z (Expand-Archive can't
        # open it); mpv's own first-party CI release ships plain .zip builds.
        try {
            $mpvRelease = Invoke-RestMethod "https://api.github.com/repos/mpv-player/mpv/releases/tags/git-release" -UseBasicParsing -TimeoutSec 25
            $mpvAsset = $mpvRelease.assets | Where-Object { $_.name -match "x86_64-w64-mingw32\.zip$" } | Select-Object -First 1
            if ($mpvAsset) { Get-Zip $mpvAsset.browser_download_url (Join-Path $tools "mpv") | Out-Null }
        } catch { Write-Host "  mpv fetch skipped" }
    }
    if (-not (Have "HandBrakeCLI")) {
        try {
            $hbRelease = Invoke-RestMethod "https://api.github.com/repos/HandBrake/HandBrake/releases/latest" -UseBasicParsing -TimeoutSec 25
            $hbAsset = $hbRelease.assets | Where-Object { $_.name -match "^HandBrakeCLI-.*-win-x86_64\.zip$" } | Select-Object -First 1
            if ($hbAsset) { Get-Zip $hbAsset.browser_download_url (Join-Path $tools "handbrake") | Out-Null }
        } catch { Write-Host "  HandBrakeCLI fetch skipped" }
    }
    # dvdauthor + spumux (DVD-Video authoring/subtitles) have no winget package;
    # this VideoHelp-hosted plain .zip (no rar/unrar needed) is the only
    # reliable direct-download source found.
    if (-not (Have "dvdauthor")) { Get-Zip "https://download.videohelp.com/gfd/edcounter.php?file=download/dvdauthor_winbin.zip" (Join-Path $tools "dvdauthor") | Out-Null }
    if (Test-Path $tools) {
        $env:Path = $env:Path + ";" + $tools + ";" +
            ((Get-ChildItem $tools -Recurse -Filter "ffmpeg.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).DirectoryName)
    }
    Write-Host "xorriso and HandBrakeCLI have no simple direct-download URL - install manually if you need"
    Write-Host "video-DVD ISO packaging / DVD ripping (see the plan's Windows setup notes)."
} else {
    Write-Host "winget unavailable (Windows 7). ISO + data + audio burn work via IMAPI2."
    Write-Host "For rip/play/video-DVD install manually: xorriso, HandBrakeCLI 1.5.1, ffmpeg, mpv, yt-dlp."
}

# --- 3. App files + venv ------------------------------------------------------
New-Item -ItemType Directory -Force -Path $Base, $App | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "src\*") $App
& $PyExe @PyArgs -m venv $Venv
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --upgrade pip
$Req = Join-Path $Root "requirements.txt"
if (Test-Path $Req) { & $Py -m pip install -r $Req } else { & $Py -m pip install pyserial mutagen requests yt-dlp }
$Opt = Join-Path $Root "requirements-optional.txt"
try { if (Test-Path $Opt) { & $Py -m pip install -r $Opt } else { & $Py -m pip install musicbrainzngs tmdbsimple } }
catch { Write-Host "Optional metadata deps skipped (host still works)." }

# --- 4. Self-signed cert (HTTPS on :8080; :8081 works without it) --------------
$crt = Join-Path $Base "server.crt"; $key = Join-Path $Base "server.key"
if (-not (Test-Path $crt) -or -not (Test-Path $key)) {
    try {
        $c = New-SelfSignedCertificate -DnsName "discstation.local" -CertStoreLocation "Cert:\CurrentUser\My" -NotAfter (Get-Date).AddYears(10)
        $pwd = ConvertTo-SecureString -String "discstation" -Force -AsPlainText
        $pfx = Join-Path $env:TEMP "ds.pfx"
        Export-PfxCertificate -Cert $c -FilePath $pfx -Password $pwd | Out-Null
        & $Py -c "import ssl" 2>$null
        # Convert PFX -> PEM via Python cryptography if present, else leave HTTPS off.
        & $Py -m pip install cryptography 2>$null
        & $Py -c "import sys;from cryptography.hazmat.primitives.serialization import pkcs12,Encoding,PrivateFormat,NoEncryption;d=open(sys.argv[1],'rb').read();k,c,_=pkcs12.load_key_and_certificates(d,b'discstation');open(sys.argv[2],'wb').write(c.public_bytes(Encoding.PEM));open(sys.argv[3],'wb').write(k.private_bytes(Encoding.PEM,PrivateFormat.TraditionalOpenSSL,NoEncryption()))" $pfx $crt $key
        Remove-Item $pfx -ErrorAction SilentlyContinue
    } catch { Write-Host "Cert generation skipped; the host will serve plain HTTP on 8081." }
}

# --- 5. Firewall -------------------------------------------------------------
foreach ($port in 8080, 8081) {
    $name = "DiscStation $port"
    try {
        if (Get-Command New-NetFirewallRule -ErrorAction SilentlyContinue) {
            if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
                New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port | Out-Null
            }
        } else {
            netsh advfirewall firewall add rule name="$name" dir=in action=allow protocol=TCP localport=$port | Out-Null
        }
    } catch {}
}

# --- 6. Auto-start Scheduled Task -----------------------------------------------
$pyw = Join-Path $Venv "Scripts\pythonw.exe"
$target = Join-Path $App "discstation.py"
try {
    if (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue) {
        $action    = New-ScheduledTaskAction -Execute $pyw -Argument "`"$target`"" -WorkingDirectory $App
        $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $set       = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        # Interactive: attaches to the logged-in desktop session, same
        # lifecycle as `systemctl --user`/launchd LaunchAgents on the other
        # two platforms - required so PLAY's mpv window actually renders
        # somewhere visible (S4U runs headlessly with no session to render
        # into, which silently made every mpv window invisible). Trade-off,
        # same one Linux/macOS already accept: won't start until someone is
        # logged into the desktop.
        # RunLevel Limited (standard, non-elevated): nothing here needs admin -
        # IMAPI2 burning, WMI reads, and binding ports >1024 all work as a normal
        # user, and elevation is what put "Administrator" on the flashing console
        # windows this used to spawn.
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName "DiscStation" -Action $action -Trigger $trigger -Settings $set -Principal $principal -Force | Out-Null
        Start-ScheduledTask -TaskName "DiscStation"
    } else {
        schtasks /create /tn "DiscStation" /sc onlogon /f /tr "`"$pyw`" `"$target`"" | Out-Null
        schtasks /run /tn "DiscStation" | Out-Null
    }
} catch { Write-Host "Auto-start task not created: $_" }

$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notmatch '^127\.|^169\.254\.' } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "DiscStation installed at $App"
Write-Host "  Web UI:  http://localhost:8081/   (also http://$ip`:8081 on the LAN)"
Write-Host "  Run 'discstation' any time to open it. Detection/eject/ISO+data+audio burn use IMAPI2 (no extra tools)."
try { node (Join-Path $Root "scripts\open.mjs") } catch {}
