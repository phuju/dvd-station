#!/usr/bin/env bash
# Static sanity checks for every language in the repo. Run before committing.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m py_compile src/*.py
for f in install.sh install-macos.sh scripts/*.sh; do bash -n "$f"; done
for f in scripts/*.mjs scripts/lib/*.mjs src/static/app.js; do node --check "$f"; done
(cd mobile && ./node_modules/.bin/tsc --noEmit --noUnusedLocals)

# Import the host and exercise the pure status/menu logic with canned input.
(cd src && python3 - <<'EOF'
import discstation as d
d._record_web_status("PLAY_MODE:AUDIO_CD")
assert d._status_snapshot()["playing"] is True
d._record_web_status("PROGRESS:42%")
assert d._status_snapshot()["progress"] == 42
d._record_web_status("DONE:ok")
assert d._status_snapshot()["playing"] is False

# Burn-flow helpers: history recording for every exit path, START/SPEED/MODE/CANCEL.
import json, pathlib, tempfile
d.HISTORY_FILE = pathlib.Path(tempfile.mkdtemp()) / "h.jsonl"
with d._burn_history({"title": "t"}):
    pass
try:
    with d._burn_history({"title": "t"}):
        raise RuntimeError("boom")
except RuntimeError:
    pass
with d._burn_history({"title": "t"}, swallow_cancel=True) as h:
    raise d.CancelError("x")
assert h.cancelled
rows = [json.loads(l) for l in open(d.HISTORY_FILE)]
assert [r["success"] for r in rows] == [True, False, False] and rows[1]["error"] == "boom"
ser = d.VirtualSerial()
for line in ("SPEED:6x", "START"):
    ser.push_line(line)
assert d._wait_for_start(ser, "audio burn") == (None, "6x")
for line in ("MODE:BEST", "START"):
    ser.push_line(line)
assert d._wait_for_start(ser, mode="AUTO") == ("BEST", None)
ser.push_line("CANCEL")
assert d._wait_for_start(ser, "x") is None

# Subprocess-output iterators (shared reader thread) + cancel handling.
import subprocess, sys
def spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
assert list(d.discstation_burn.iter_proc_or_cancel(spawn("print('a');print('b')"), ser)) == ["a", "b"]
assert [e for e in d.iter_process_events(spawn("print('x')"), idle_seconds=0.2, ser=ser) if e] == ["x"]
ser.push_line("CANCEL")
try:
    list(d.iter_process_events(spawn("import time;time.sleep(30)"), idle_seconds=0.2, ser=ser))
    raise SystemExit("cancel not raised")
except d.CancelError:
    pass

# Remote OTA state: available only for a real, older release; dev builds never nag.
d._appliance_mode = "hardware"
d._firmware_image = lambda: (None, {"version": "0.2.0", "size": 1, "md5": "x"})
for fw, want in (("0.1.0", "available"), ("dev", "current"), ("0.2.0", "current"), ("0.3.0", "current")):
    d._remote_fw = fw
    assert d._remote_update_state() == want, (fw, d._remote_update_state())
d._remote_fw, d._remote_fw_asked = None, 0.0
assert d._remote_update_state() == "unknown"
d._appliance_mode = "software"
assert d._remote_update_state() == ""
print("smoke ok")
EOF
)
echo "all checks passed"
