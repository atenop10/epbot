#!/usr/bin/env bash
# One-command setup for the streaming player (Caddy HTTPS + .env + restart).
# Run from the bot folder:   bash setup-stream.sh play.audiosfm.online
# Optional 2nd argument: bot folder (default: current folder).
set -euo pipefail

DOMAIN="${1:?Usage: bash setup-stream.sh play.yourdomain.com [bot_folder]}"
DIR="$(cd "${2:-$PWD}" && pwd)"
SERVICE="episodes-bot"

[ -f "$DIR/.env" ]      || { echo "No .env in $DIR - run this from the bot folder."; exit 1; }
[ -f "$DIR/stream.py" ] || { echo "stream.py missing in $DIR - copy the new bot files here first."; exit 1; }
[ -x "$DIR/venv/bin/pip" ] || { echo "No venv found in $DIR/venv."; exit 1; }

echo "==> 0/5 Checking DNS"
MYIP="$(curl -4 -fsS https://ifconfig.me 2>/dev/null || true)"
DNSIP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
echo "    This server's IP : ${MYIP:-unknown}"
echo "    $DOMAIN points to: ${DNSIP:-nothing yet}"
if [ -z "$MYIP" ] || [ "$MYIP" != "$DNSIP" ]; then
  echo "    WARNING: the domain does not point to this server yet (or it is proxied)."
  echo "    Add the DNS 'A' record first, otherwise the HTTPS certificate cannot be issued."
  read -r -p "    Continue anyway? [y/N] " ans
  [ "${ans:-n}" = "y" ] || exit 1
fi

echo "==> 1/5 Installing Caddy (HTTPS web server)"
if ! command -v caddy >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y caddy
else
  echo "    Caddy already installed."
fi

echo "==> 2/5 Configuring Caddy for $DOMAIN"
[ -f /etc/caddy/Caddyfile ] && sudo cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8080
}
EOF
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 80/tcp >/dev/null && sudo ufw allow 443/tcp >/dev/null && echo "    ufw: opened ports 80 and 443"
fi
sudo systemctl enable caddy >/dev/null 2>&1 || true
sudo systemctl restart caddy

echo "==> 3/5 Installing Python packages"
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> 4/5 Updating .env"
[ -n "$(tail -c1 "$DIR/.env")" ] && echo >> "$DIR/.env" || true
set_env() {
  if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >> "$DIR/.env"; fi
}
SECRET="$(grep -E '^STREAM_SECRET=.+' "$DIR/.env" | head -1 | cut -d= -f2- || true)"
[ -n "$SECRET" ] || SECRET="$(openssl rand -hex 32)"
set_env STREAM_BASE_URL "https://$DOMAIN"
set_env STREAM_SECRET "$SECRET"
echo "    STREAM_BASE_URL and STREAM_SECRET saved."

echo "==> 5/5 Restarting the bot"
sudo systemctl restart "$SERVICE"
sleep 4
sudo journalctl -u "$SERVICE" -n 8 --no-pager -o cat | cut -c1-160

echo
echo "Done. Test: curl -sI https://$DOMAIN/p/test | head -1   (should print: HTTP/2 403)"
echo "Then request an R2 episode in the bot and tap Play."
