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
print("smoke ok")
EOF
)
echo "all checks passed"
