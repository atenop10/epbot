#!/usr/bin/env bash
# One-command setup for the AUDIOS FM episodes bot on Ubuntu.
# Usage:  cd episodes-bot && bash setup.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"

echo "== 1/4 Installing Python =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "== 2/4 Installing bot libraries =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "== 3/4 Settings =="
if [ -f .env ]; then
  echo ".env already exists - keeping it (delete it and rerun to change)."
else
  read -rp "Bot token from @BotFather: " BOT_TOKEN
  read -rp "Your Telegram user ID (admin): " ADMIN_IDS
  read -rp "R2 endpoint (https://<account-id>.r2.cloudflarestorage.com): " R2_ENDPOINT
  read -rp "R2 bucket name [audiosfm]: " R2_BUCKET
  R2_BUCKET="${R2_BUCKET:-audiosfm}"
  read -rp "R2 access key ID: " R2_ACCESS_KEY_ID
  read -rsp "R2 secret access key (hidden): " R2_SECRET_ACCESS_KEY; echo
  cat > .env <<EOT
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
R2_ENDPOINT=$R2_ENDPOINT
R2_BUCKET=$R2_BUCKET
R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
REQUIRE_SUBSCRIPTION=false
CONTACT_TEXT=To subscribe, contact the admin.
DELETE_AFTER_SECONDS=5400
MAX_RANGE=30
EOT
  chmod 600 .env
fi

echo "== 4/4 Starting the bot (auto-start on reboot) =="
sudo tee /etc/systemd/system/episodes-bot.service >/dev/null <<EOT
[Unit]
Description=AUDIOS FM Episodes Telegram Bot
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOT
sudo systemctl daemon-reload
sudo systemctl enable --now episodes-bot
sleep 3
sudo systemctl --no-pager status episodes-bot | head -n 12

echo
echo "Done. Useful commands:"
echo "  sudo journalctl -u episodes-bot -f     # live logs"
echo "  sudo systemctl restart episodes-bot    # after editing .env or bot.py"
echo "  sudo systemctl stop episodes-bot       # stop the bot"
