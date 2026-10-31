#!/usr/bin/env bash
set -euo pipefail
# Runta runs this as root with umask 077; the agent runs as an unprivileged user and must read the engine.
umask 022

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl git gnupg python3 xz-utils
command -v google-chrome >/dev/null || { echo 'The Runta clean image must include Google Chrome.' >&2; exit 1; }
if ! node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22 || a===22 && b>=19 ? 0 : 1)' 2>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
git init /opt/parallax
git -C /opt/parallax remote add origin https://github.com/ParallaxPro-AI/ParallaxEngine.git
git -C /opt/parallax fetch --depth 1 origin "$ENGINE_REF"
git -C /opt/parallax checkout --detach FETCH_HEAD
cd /opt/parallax
# Use the pnpm the engine pins; npm instead of corepack, which Node 25+ no longer ships.
npm install -g "pnpm@$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
pnpm install --frozen-lockfile
cat > /usr/local/bin/parallax <<'SH'
#!/bin/sh
exec node /opt/parallax/packages/cli/bin/parallax.mjs "$@"
SH
chmod +x /usr/local/bin/parallax
# pxr created /workspace as root before this script ran; hand it to the agent's user.
AGENT_USER="${AGENT_USER:-runta}"
chmod 755 /workspace
if id "$AGENT_USER" >/dev/null 2>&1; then
  chown "$AGENT_USER:$AGENT_USER" /workspace
  # Fail the base now rather than leave the agent unable to read the skill, run parallax or write the game.
  su "$AGENT_USER" -s /bin/sh -c 'cat /opt/parallax/skills/parallax-game/SKILL.md >/dev/null && parallax new --list >/dev/null && touch /workspace/.pxr-write && rm /workspace/.pxr-write'
fi
python3 - <<'PY'
import json, subprocess, sys
from pathlib import Path
def output(args):
    return subprocess.check_output(args, text=True).strip()
Path('/opt/pxr/versions.json').write_text(json.dumps({
    'engine': output(['git', '-C', '/opt/parallax', 'rev-parse', 'HEAD']),
    'node': output(['node', '--version']), 'python': sys.version.split()[0],
}))
PY
