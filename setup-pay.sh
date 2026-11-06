#!/usr/bin/env bash
# Turns on the paid passes (Razorpay): two plans, Rs 15 for 7 days and Rs 49 for 30 days by default. Run from the bot folder:
#   bash setup-pay.sh <key_id> <key_secret> <webhook_secret> [plans]
#   e.g.  bash setup-pay.sh rzp_live_xxxx yyyy mywebhooksecret 15:7,49:30
#   plans = rupees:days pairs, comma separated (default 15:7,49:30). One plan only: 29:30
#   bash setup-pay.sh off        (keys stay saved)
# The webhook secret is the one you type when you create the webhook in the Razorpay dashboard (see RAZORPAY_FAVORITES.md).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="episodes-bot"
CMD="${1:?Usage: bash setup-pay.sh <key_id> <key_secret> <webhook_secret> [plans] | off}"
[ -f "$DIR/.env" ] || { echo "No .env in $DIR - run setup.sh first."; exit 1; }
[ -f "$DIR/pay.py" ] || { echo "pay.py is missing in $DIR - copy all the new bot files first."; exit 1; }
setenv() {
  [ -n "$(tail -c1 "$DIR/.env")" ] && echo >> "$DIR/.env" || true
  if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >> "$DIR/.env"; fi
}
if [ "$CMD" = "off" ]; then
  setenv RAZORPAY_KEY_ID ""
  echo "==> Paid pass turned OFF"
else
  SECRET="${2:?Usage: bash setup-pay.sh <key_id> <key_secret> <webhook_secret> [plans]}"
  HOOK="${3:-}"
  PLANS="${4:-15:7,49:30}"
  [[ "$PLANS" =~ ^[0-9]+:[0-9]+(,[0-9]+:[0-9]+)*$ ]] || { echo "Plans must look like 15:7,49:30 (rupees:days, comma separated)."; exit 1; }
  grep -qE '^STREAM_BASE_URL=.+' "$DIR/.env" || { echo "Streaming player is not set up yet. Run: bash setup-stream.sh play.yourdomain.com"; exit 1; }
  setenv RAZORPAY_KEY_ID "$CMD"
  setenv RAZORPAY_KEY_SECRET "$SECRET"
  setenv RAZORPAY_WEBHOOK_SECRET "$HOOK"
  setenv SUB_PLANS "$PLANS"
  echo "==> Paid passes ON (rupees:days): $PLANS"
  [ -z "$HOOK" ] && echo "    No webhook secret given: payments are confirmed only when the user taps 'I've paid' (or the app checks)."
fi
sudo systemctl restart "$SERVICE"
echo "==> Bot restarted."
