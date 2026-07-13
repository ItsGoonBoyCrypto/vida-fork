#!/usr/bin/env bash
# One-shot installer for the RH L2 scanner on a fresh Ubuntu/Debian VPS
# (Hetzner console or SSH, run as root).
#
# Usage (paste as ONE command):
#   curl -fsSL <raw-url>/bootstrap.sh | \
#     TELEGRAM_BOT_TOKEN='...' TELEGRAM_ALERT_CHAT_ID='-1004299219898' bash
#
# It is idempotent — safe to re-run to update. Env vars:
#   TELEGRAM_BOT_TOKEN      (required for alerts/digest; omit to configure later)
#   TELEGRAM_ALERT_CHAT_ID  (your channel id, e.g. -1004299219898)
#   MODE                    paper (default) | run
#   BRANCH                  git branch (default: claude/rh-l2-memecoin-scanner-7sj2qa)
set -euo pipefail

REPO="https://github.com/ItsGoonBoyCrypto/vida-fork"
BRANCH="${BRANCH:-claude/rh-l2-memecoin-scanner-7sj2qa}"
DIR="/opt/rhl2-scanner"
MODE="${MODE:-paper}"

echo "==> [1/6] Installing system deps (git, python3-venv)…"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null
fi

echo "==> [2/6] Fetching code ($BRANCH)…"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --depth 1 origin "$BRANCH" -q
  git -C "$DIR" checkout -qB "$BRANCH" "origin/$BRANCH"
else
  git clone --depth 1 -b "$BRANCH" "$REPO" "$DIR" -q
fi
cd "$DIR"

echo "==> [3/6] Python venv + dependencies…"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r rhl2_scanner/requirements.txt

echo "==> [4/6] Config (Robinhood Chain) + secrets…"
[ -f rhl2_scanner/config/config.yaml ] || \
  cp rhl2_scanner/config/robinhood.example.yaml rhl2_scanner/config/config.yaml
{
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"; fi
  if [ -n "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then echo "TELEGRAM_ALERT_CHAT_ID=${TELEGRAM_ALERT_CHAT_ID}"; fi
} > rhl2_scanner/.env
chmod 600 rhl2_scanner/.env

echo "==> [5/6] Telegram check…"
./.venv/bin/python -m rhl2_scanner tg-test --config rhl2_scanner/config/config.yaml || \
  echo "   (tg-test could not confirm — check token/chat id or that the bot is a channel admin)"

echo "==> [6/6] Installing systemd service (mode: $MODE)…"
sed "s/ paper / ${MODE} /" rhl2_scanner/deploy/rhl2-scanner.service \
  > /etc/systemd/system/rhl2-scanner.service
systemctl daemon-reload
systemctl enable --now rhl2-scanner

echo
echo "✅ Done. The scanner is running on Robinhood Chain (mode: $MODE)."
echo "   Live logs:   journalctl -u rhl2-scanner -f"
echo "   Calibration: cd $DIR && ./.venv/bin/python -m rhl2_scanner paper-report --config rhl2_scanner/config/config.yaml"
echo "   Go live:     MODE=run re-run this script (or edit the systemd unit), then: systemctl restart rhl2-scanner"
