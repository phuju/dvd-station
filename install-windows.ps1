$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$App = if ($env:DISCSTATION_APP_DIR) { $env:DISCSTATION_APP_DIR } else { Join-Path $env:LOCALAPPDATA "DiscStation\app" }
$Venv = if ($env:DISCSTATION_VENV_DIR) { $env:DISCSTATION_VENV_DIR } else { Join-Path $env:LOCALAPPDATA "DiscStation\venv" }

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Install Python 3 from https://www.python.org/downloads/windows/ first."
}

New-Item -ItemType Directory -Force -Path $App, $Venv | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "src\*") $App
py -3 -m venv $Venv
& (Join-Path $Venv "Scripts\python.exe") -m pip install --upgrade pip
$Py = Join-Path $Venv "Scripts\python.exe"
$Req = Join-Path $Root "requirements.txt"
if (Test-Path $Req) {
    & $Py -m pip install -r $Req
} else {
    & $Py -m pip install pyserial mutagen requests yt-dlp
}
# Optional metadata deps — best effort, never fatal.
try { & $Py -m pip install musicbrainzngs tmdbsimple } catch {
    Write-Host "Optional metadata deps skipped (host still works)."
}

Write-Host "DiscStation host files installed at $App"
Write-Host "The web/control workflow is available; optical burning requires a Windows IMAPI backend or compatible burning tools."
Write-Host "Run: $Venv\Scripts\python.exe $App\discstation.py"
