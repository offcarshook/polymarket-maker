#!/usr/bin/env bash
# One-shot PAPER-ONLY setup for the polymaker market-making test on a fresh
# Ubuntu server (Oracle Cloud Always Free, ARM or x86). Safe to re-run.
#
#   git clone https://github.com/offcarshook/polymarket-maker
#   cd polymarket-maker && git checkout claude/magical-ritchie-b6wz2p
#   bash oracle/setup.sh                 # full: ~2000+ markets (Oracle / a spare server)
#   MODE=lite bash oracle/setup.sh       # lite: 300 selected markets, capped CPU/RAM (your PC / WSL)
#
# What it does:
#   * installs git/curl + uv (Python manager)
#   * clones warproxxx/poly-maker at the reviewed commit, applies our fixes (scale + paper fills)
#   * copies our configs + my_tools, creates an EMPTY .env (paper mode needs no key)
#   * scans ~11k markets, picks the eligible ones, starts 5 systemd services
# It NEVER asks for or stores a private key, and the bot service refuses to start
# if PK is ever filled in (paper test only).
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${APP_DIR:-$HOME/poly-maker}"
REVIEWED_COMMIT="4f32103591c9582ccd012bdf10f77d86e5879444"
RUN_USER="$(id -un)"
MODE="${MODE:-full}"
if [ "$MODE" = "lite" ]; then
  PICK_ARGS="--n 300 --min-hours-left 336 --max-spread-ticks 3 --no-games"
  MAX_MB=200; DASH_EVERY=30; CPUQUOTA=50%; MEMMAX=700M
else
  PICK_ARGS="--n 100000 --min-hours-left 48 --max-spread-ticks 3 --no-games"
  MAX_MB=1000; DASH_EVERY=15; CPUQUOTA=200%; MEMMAX=2G
fi

if [ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]; then
  echo "systemd is not running (on WSL: see oracle/WINDOWS_SETUP.md step 2, then 'wsl --shutdown' and reopen)."
  exit 1
fi

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "1/7 system packages"
sudo apt-get update -y -qq
sudo apt-get install -y -qq git curl ca-certificates

say "2/7 uv"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
UV="$(command -v uv)"

say "3/7 bot code (warproxxx/poly-maker @ ${REVIEWED_COMMIT:0:7})"
if [ ! -d "$APP/.git" ]; then
  git clone -q https://github.com/warproxxx/poly-maker "$APP"
fi
cd "$APP"
git checkout -q "$REVIEWED_COMMIT" 2>/dev/null || { git fetch -q origin && git checkout -q "$REVIEWED_COMMIT"; }
if git apply --check "$KIT/patches/polymaker-scale-fixes.patch" 2>/dev/null; then
  git apply "$KIT/patches/polymaker-scale-fixes.patch"
  echo "applied fixes (414 chunking, websocket max_size, in-engine paper fills)"
else
  echo "fixes already applied"
fi

say "4/7 configs, tools, empty .env"
mkdir -p config my_tools web logs journal
cp "$KIT"/config/config.toml "$KIT"/config/strategy.toml config/
cp "$KIT"/my_tools/* my_tools/
chmod +x my_tools/*.sh
if [ ! -f .env ]; then
  printf '# PAPER MODE: keep PK and BROWSER_ADDRESS EMPTY.\nPK=\nBROWSER_ADDRESS=\n' > .env
fi
chmod 600 .env

say "5/7 python deps"
"$UV" sync -q

say "6/7 market scan + pick (takes ~1-3 min)"
"$UV" run python my_tools/scan_tags.py
# shellcheck disable=SC2086
.venv/bin/python -I my_tools/pick_markets.py $PICK_ARGS

say "7/7 systemd services (mode: $MODE)"
for f in "$KIT"/systemd/*; do
  sed -e "s#@APP@#$APP#g" -e "s#@USER@#$RUN_USER#g" -e "s#@UV@#$UV#g" \
      -e "s#@PICK_ARGS@#$PICK_ARGS#g" -e "s#@MAX_MB@#$MAX_MB#g" -e "s#@DASH_EVERY@#$DASH_EVERY#g" \
      -e "s#@CPUQUOTA@#$CPUQUOTA#g" -e "s#@MEMMAX@#$MEMMAX#g" "$f" \
    | sudo tee "/etc/systemd/system/$(basename "$f")" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now polymaker-paper polymaker-markout polymaker-dashboard \
  polymaker-web polymaker-journal-guard polymaker-refresh.timer

sleep 5
systemctl --no-pager --lines=0 status polymaker-paper polymaker-markout polymaker-dashboard \
  polymaker-web polymaker-journal-guard | grep -E '●|Active:' || true

cat <<EOF

✅ Done. Everything runs 24/7 and restarts itself after crashes/reboots.
   Dashboard (private, on the server only): http://127.0.0.1:8080
   Windows/WSL: just open http://localhost:8080 in your browser.
   Oracle: see oracle/ORACLE_SETUP.md, step 6.
   Logs:   journalctl -u polymaker-paper -f
   Stop:   sudo systemctl stop 'polymaker-*'
EOF
