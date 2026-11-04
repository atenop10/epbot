#!/usr/bin/env bash
# Turns on "watch an ad -> time-limited access" (Adsgram Reward block).
# Run from the bot folder:   bash setup-rewarded.sh <adsgram_block_id> [minutes_per_ad]
#   e.g.  bash setup-rewarded.sh 12345 60
# Turn it off again:         bash setup-rewarded.sh off
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ZONE="${1:?Usage: bash setup-rewarded.sh <adsgram_block_id|off> [minutes_per_ad]}"
MINS="${2:-60}"
SERVICE="episodes-bot"

[ -f "$DIR/.env" ] || { echo "No .env in $DIR - run setup.sh first."; exit 1; }
[ -f "$DIR/adpass.py" ] || { echo "adpass.py is missing in $DIR - copy all the new bot files first."; exit 1; }

setenv() {  # setenv KEY VALUE
  [ -n "$(tail -c1 "$DIR/.env")" ] && echo >> "$DIR/.env" || true
  if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >> "$DIR/.env"; fi
}

if [ "$ZONE" = "off" ]; then
  setenv ADSGRAM_BLOCK_ID ""
  setenv AD_REWARD_ZONE ""
  echo "==> Ad-unlock turned OFF"
else
  [[ "$ZONE" =~ ^[0-9]+$ ]] || { echo "Block id must be digits only (a Reward block, e.g. 12345 - not "int-12345")."; exit 1; }
  [[ "$MINS" =~ ^[0-9]+$ ]] || { echo "Minutes must be a number."; exit 1; }
  grep -qE '^STREAM_BASE_URL=.+' "$DIR/.env" || { echo "Streaming player is not set up yet. Run: bash setup-stream.sh play.yourdomain.com"; exit 1; }
  setenv ADSGRAM_BLOCK_ID "$ZONE"
  setenv AD_REWARD_ZONE ""
  setenv AD_PASS_MINUTES "$MINS"
  echo "==> Ad-unlock ON: Adsgram block $ZONE, $MINS min per ad"
  echo "    Everyone except admins and paid (/grant) users must now watch an ad to listen."
fi
sudo systemctl restart "$SERVICE"
sleep 3
sudo journalctl -u "$SERVICE" -n 6 --no-pager -o cat | cut -c1-160
echo
echo "Done. Open the app from Telegram to try it. Admin: /adstats shows how many ads were watched."
