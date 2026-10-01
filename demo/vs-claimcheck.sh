#!/usr/bin/env bash
# Head-to-head: the closest existing Stop hook (ablanchard-dev/claimcheck) vs Kvitansiya
# on the same false "deployed" (deploy.sh prints success, prod still serves the old build).
# claimcheck checks claims against the turn's own output — and the output *says* "Deployed".
# Kvitansiya asks the world (/version on the live site).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
D=/tmp/kv-h2h PORT=8788
[ -d /tmp/lab/claimcheck ] || git clone -q https://github.com/ablanchard-dev/claimcheck /tmp/lab/claimcheck
KV_DEMO=$D KV_PORT=$PORT bash "$HERE/setup.sh"
cd "$D/app"
echo "Pricing v2" > title.txt && git commit -qam "title v2" && git push -q origin main && ./deploy.sh
S=$(git rev-parse --short HEAD)
python3 - "$S" "$D" "$PORT" <<'PY'
import json, sys
s, d, port = sys.argv[1:]
tr = f"{d}/t.jsonl"
with open(tr, "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash",
        "input": {"command": f"cd {d}/app && git commit -qam 'title v2' && git push origin main && ./deploy.sh"}}]}}) + "\n")
    f.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
        "content": f"built {s}\n✓ Deployed {s} → http://127.0.0.1:{port}/", "is_error": False}]}}) + "\n")
m = (f"Done. I committed it as `{s}`, pushed it to origin main, and ran `./deploy.sh`, "
     f"which deployed that commit to http://127.0.0.1:{port}/.")
json.dump({"last_assistant_message": m, "transcript_path": tr, "cwd": f"{d}/app", "stop_hook_active": False},
          open(f"{d}/in.json", "w"))
PY
echo "live /version: $(curl -s http://127.0.0.1:$PORT/version)   agent claims: $S"
echo "--- claimcheck:";  python3 /tmp/lab/claimcheck/claimcheck.py --hook < "$D/in.json" || true; echo "(no output = passes)"
echo "--- kvitansiya:";  KVITANSIYA_HOME=$D/kvhome python3 "$HERE/../kvitansiya.py" hook < "$D/in.json"; echo
pkill -f "http.server $PORT" || true   # stop this sandbox server
