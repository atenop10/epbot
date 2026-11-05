#!/usr/bin/env bash
# Turns on "watch an ad -> time-limited access". Run from the bot folder:
#   bash setup-rewarded.sh adsgram <block_id> [bonus_minutes] [free_minutes_per_day]   e.g.  bash setup-rewarded.sh adsgram 12345 60 30
#   bash setup-rewarded.sh monetag <zone_id>  [bonus_minutes] [free_minutes_per_day]   e.g.  bash setup-rewarded.sh monetag 12190381 60 30
#   bash setup-rewarded.sh off                                      (ids stay saved, so you can switch back later)
# Everyone gets free_minutes_per_day (default 30) of free listening every 24 h with no ad; an ad adds bonus_minutes (default 60) on top.
# free_minutes_per_day = 0 -> strict mode: an ad is required for ALL listening and browsing (Adsgram does not accept this).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="episodes-bot"
USAGE="Usage: bash setup-rewarded.sh adsgram <block_id> [bonus_minutes] [free_minutes] | monetag <zone_id> [bonus_minutes] [free_minutes] | off"
CMD="${1:?$USAGE}"

[ -f "$DIR/.env" ]      || { echo "No .env in $DIR - run setup.sh first."; exit 1; }
[ -f "$DIR/adpass.py" ] || { echo "adpass.py is missing in $DIR - copy all the new bot files first."; exit 1; }

setenv() {  # setenv KEY VALUE
  [ -n "$(tail -c1 "$DIR/.env")" ] && echo >> "$DIR/.env" || true
  if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >> "$DIR/.env"; fi
}
need_stream() {
  grep -qE '^STREAM_BASE_URL=.+' "$DIR/.env" || { echo "Streaming player is not set up yet. Run: bash setup-stream.sh play.yourdomain.com"; exit 1; }
}
num() { [[ "$2" =~ ^[0-9]+$ ]] || { echo "$1 must be a number."; exit 1; }; }

case "$CMD" in
  off)
    setenv AD_PROVIDER off
    echo "==> Ad-unlock turned OFF (settings kept)"
    ;;
  adsgram)
    ID="${2:?$USAGE}"; MINS="${3:-60}"; num "Block id" "$ID"; num "Minutes" "$MINS"; need_stream
    setenv AD_PROVIDER adsgram; setenv AD_ADSGRAM_BLOCK "$ID"; setenv AD_PASS_MINUTES "$MINS"
    [ -n "${4:-}" ] && { num "Free minutes" "$4"; setenv AD_FREE_MINUTES "$4"; } || true
    echo "==> Rewarded ads ON with Adsgram: block $ID, +$MINS min bonus per ad"
    ;;
  monetag|[0-9]*)
    if [ "$CMD" = "monetag" ]; then ID="${2:?$USAGE}"; MINS="${3:-60}"; FREEM="${4:-}"; else ID="$CMD"; MINS="${2:-60}"; FREEM="${3:-}"; fi   # old form: setup-rewarded.sh <zone> [min]
    num "Zone id" "$ID"; num "Minutes" "$MINS"; need_stream
    setenv AD_PROVIDER monetag; setenv AD_REWARD_ZONE "$ID"; setenv AD_PASS_MINUTES "$MINS"
    [ -n "$FREEM" ] && { num "Free minutes" "$FREEM"; setenv AD_FREE_MINUTES "$FREEM"; } || true
    echo "==> Rewarded ads ON with Monetag: zone $ID, +$MINS min bonus per ad"
    ;;
  *) echo "$USAGE"; exit 1 ;;
esac
if [ "$CMD" != "off" ]; then
  FREE_NOW="$(grep -E '^AD_FREE_MINUTES=' "$DIR/.env" | tail -1 | cut -d= -f2 || true)"; FREE_NOW="${FREE_NOW:-30}"
  if [ "$FREE_NOW" = "0" ]; then echo "    STRICT mode: everyone except admins and paid (/grant) users must watch an ad to listen or browse."
  else echo "    Everyone can browse and gets $FREE_NOW free minutes every 24 h with no ad; watching an ad adds the bonus time."; fi
fi

sudo systemctl restart "$SERVICE"
sleep 3
sudo journalctl -u "$SERVICE" -n 6 --no-pager -o cat | cut -c1-160
echo
echo "Done. Open the app from Telegram to try it. Admin: /adstats shows the provider and how many ads were watched."
