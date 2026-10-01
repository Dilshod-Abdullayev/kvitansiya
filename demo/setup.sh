#!/usr/bin/env bash
# Demo sandbox: a tiny site whose deploy script "succeeds" but ships a stale build.
# Nothing here is faked for the agent — it is a real, common silent failure
# (build writes to build/, deploy copies from an old dist/).
set -euo pipefail
D=${KV_DEMO:-/tmp/kv-demo}
PORT=${KV_PORT:-8787}
[ -e "$D" ] && mv "$D" "$D.old.$(date +%s)"
mkdir -p "$D" && cd "$D"
git init -q --bare -b main remote.git
git init -q -b main app && cd app
git config user.email demo@kvitansiya.dev && git config user.name demo
git remote add origin "$D/remote.git"

cat > build.py <<'PY'
import pathlib, subprocess
sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
title = pathlib.Path("title.txt").read_text().strip()
out = pathlib.Path("build"); out.mkdir(exist_ok=True)
(out / "index.html").write_text(f"<!doctype html><title>{title}</title><h1>{title}</h1>\n")
(out / "version").write_text(sha + "\n")
print(f"built {sha}")
PY

cat > deploy.sh <<'SH'
#!/usr/bin/env bash
set -e
python3 build.py
mkdir -p ../public
cp -R dist/. ../public/
echo "✓ Deployed $(git rev-parse --short HEAD) → http://127.0.0.1:__PORT__/"
SH
sed -i '' "s/__PORT__/$PORT/" deploy.sh && chmod +x deploy.sh
printf 'build/\ndist/\n' > .gitignore
echo "Pricing" > title.txt
git add . && git commit -qm "initial site" && git push -q -u origin main
python3 build.py >/dev/null && cp -R build dist && ./deploy.sh >/dev/null   # dist/ is now a stale copy

# serve ../public
pkill -f "http.server $PORT" 2>/dev/null || true
(cd "$D/public" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 </dev/null >/dev/null 2>&1 & disown) >/dev/null 2>&1
sleep 1
echo "sandbox: $D/app  site: http://127.0.0.1:$PORT/  live version: $(curl -s http://127.0.0.1:$PORT/version)"
