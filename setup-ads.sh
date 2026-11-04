#!/usr/bin/env bash
# Turns on the ad banner on the streaming player.
# Run from the bot folder:   bash setup-ads.sh [ad_height]      (default 280 = 300x250 banner; use 60 for 320x50)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
H="${1:-280}"
SERVICE="episodes-bot"

[ -f "$DIR/.env" ]    || { echo "No .env in $DIR - run setup.sh first."; exit 1; }
[ -s "$DIR/ad.html" ] || { echo "ad.html is missing or empty in $DIR - paste your ad code into it first."; exit 1; }
grep -qE '^STREAM_BASE_URL=.+' "$DIR/.env" || { echo "Streaming player is not set up yet. Run: bash setup-stream.sh play.yourdomain.com"; exit 1; }

[ -n "$(tail -c1 "$DIR/.env")" ] && echo >> "$DIR/.env" || true
if grep -q '^STREAM_AD_HEIGHT=' "$DIR/.env"; then sed -i "s|^STREAM_AD_HEIGHT=.*|STREAM_AD_HEIGHT=$H|" "$DIR/.env"; else echo "STREAM_AD_HEIGHT=$H" >> "$DIR/.env"; fi
echo "==> STREAM_AD_HEIGHT=$H saved in .env"
echo "==> Ad code in ad.html: $(wc -c < "$DIR/ad.html") bytes"
sudo systemctl restart "$SERVICE"
sleep 3
sudo journalctl -u "$SERVICE" -n 6 --no-pager -o cat | cut -c1-160
echo
echo "Done. Open an episode with the Play button - the ad shows under Prev/Next."
echo "Change it later from the bot with /setad <code>, or remove with /setad off."
