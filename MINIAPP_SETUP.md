# Telegram Mini App - setup

The Mini App is a full app inside Telegram: search series, browse episodes, play with a real player
(resume, speed, sleep timer, auto-next, lock-screen controls). It runs on the same server and domain as the
streaming player (play.yourdomain.com), so there is nothing new to install.

## Install / upgrade (5 minutes)

1. Copy these files into the bot folder on the VPS (replace the old ones):
   `bot.py`, `stream.py`, `miniapp.py`, `miniapp.html`
2. Restart: `sudo systemctl restart episodes-bot`
3. Check the logs: `sudo journalctl -u episodes-bot -n 20 --no-pager`
   (no red errors; "Streaming player listening ..." line is there)
4. Check the page is served: `curl -sI https://play.yourdomain.com/app | head -3` should print `HTTP/2 200`
5. In Telegram open your bot and send `/start` - there is now a **🎧 Open app** button. Also `/app` sends it again,
   and a menu button "🎧 Open app" appears next to the message box (close and reopen the chat if you don't see it).

The streaming player must already be set up (`STREAM_BASE_URL` and `STREAM_SECRET` in `.env`, done by `setup-stream.sh`).

## Optional: nicer link / bot profile button (BotFather)

`/newapp` in @BotFather -> pick your bot -> URL `https://play.yourdomain.com/app` -> short name e.g. `app`.
Then `https://t.me/<your_bot>/app` opens it directly, and you can set it as the bot's main app (Launch button on the
bot profile) with `/mybots` -> your bot -> Bot Settings -> Configure Mini App.

## What users can do

- Search (all words must match), Latest / A-Z tabs, "Continue" card for the last episode they played.
- Open a series, tap an episode number to play it, or type an episode number and press Play.
- Player: -15 / +15, previous / next, speed, sleep timer, resume where they stopped, auto-play next episode.
- Episodes stored in a **Telegram channel** (not R2) cannot be streamed. They show a small ✉ and tapping asks to send
  the episode to the user's chat (same file, protection and auto-delete as before).
- "Request this series" button when a search finds nothing (same as /request, admins get notified).
- Your existing rules still apply: paid-only mode (`REQUIRE_SUBSCRIPTION`), force-join channels, admins skip checks.
  Plays are counted in /stats like before.

## Ads in the Mini App

- **Usage statistics (`/stats`, `/user`):** see `STATS.md`.
- **Rewarded ads (free daily listening, ad = bonus time):** see `REWARDED_ADS.md` (`bash setup-rewarded.sh adsgram <block id>`).

- Anything you saved with `/sethead` (your Monetag zone script) is loaded on the Mini App page at top level - the
  same script that already works on the home page. That is the best place for OnClick / Vignette / Push formats.
- If Monetag gives you a zone made for **Telegram Mini Apps**, paste its script tag with `/sethead` instead.
  (Send both tags in one message if you want both.)
- A banner set with `/setad` is also shown in the app (series page and full player), in the sandboxed box.
  Use it only for normal banner zones; remove with `/setad off` if you don't want an empty box.
- Do not click your own ads.

## Settings (.env) - all optional

| Setting | Meaning |
|---|---|
| `MINIAPP_REQUIRE_COOKIE` | Default `true`: the audio link only works in the browser that opened the app, so a copied link is useless. Set `false` only if audio won't play in Telegram Web (browser version). |

## Troubleshooting

- "Please open this app from Telegram": the page was opened in a normal browser. Use the Open app button in the bot.
- "Your session expired": the app was left open for 2+ days. Close and reopen it.
- Audio won't play on Telegram Web only: set `MINIAPP_REQUIRE_COOKIE=false` in `.env` and restart.
- Series list empty: stories need a name (`/name <story id> <title>`) and at least one episode.
- Nothing changed after copying files: you forgot `sudo systemctl restart episodes-bot`.
- Audio may pause when Telegram is minimised or the phone locks, on some phones. That is a Telegram limitation.
