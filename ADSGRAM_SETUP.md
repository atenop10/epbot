# Adsgram setup (rewarded ads in the Mini App)

The bot already runs as a Telegram Mini App (`/app`). Adsgram only needs a block id; no new app is required.

## 1. Adsgram account
1. Register at https://partner.adsgram.ai
2. Create an **Ad platform**: App name, Telegram direct link (`https://t.me/<bot>/app`), Web app url (`https://play.yourdomain.com/app`),
   Bot ID (the number before the colon in your bot token). BotFather `/myapps` shows the link and URL.
   The URL must match exactly where the Mini App is served, otherwise Adsgram shows a "wrong referrer" error.
3. Create an **Ad unit**: type **Reward** (not Interstitial - an interstitial also "completes" when the user closes it early).
4. Leave **Reward URL** empty (Adsgram recommends it only for apps with 50k+ daily users).
5. Press Start to send the platform and unit for moderation. Real ads need approval. For testing, create a **Test platform** with
   your dev app's links and bot id.
6. Open the unit -> `<> Show code` -> **Copy BlockID** (digits).

## 2. Turn it on
Copy all files to the server, then in the bot folder:
```
bash setup-rewarded.sh 12345 60
```
(block id, minutes per ad). It writes `ADSGRAM_BLOCK_ID` and `AD_PASS_MINUTES` to `.env` and restarts the bot.
Turn off: `bash setup-rewarded.sh off`.

## 3. Test
Open the app from a Telegram account that is not an admin and has no `/grant`. Tap **Watch ad & unlock**.
Admins and paid users never see the gate. `/adstats` shows counts.

## What changed compared with Monetag
- SDK: `https://sad.adsgram.ai/js/sad.min.js`, `window.Adsgram.init({blockId}).show()`.
- Watched to the end -> server gives the pass. Closed early -> no pass. No ad / SDK blocked -> short fallback pass (as before).
- Adsgram has no automatic in-app ads, so `AD_INAPP` is gone.
- Server-side token checks and the daily limit are unchanged (`adpass.py`).

## Troubleshooting
- Wrong referrer error: platform "Web app url" differs from the real URL, or you are testing a live block from a dev URL.
- Nothing shows / always the 5-minute fallback: unit not approved yet, no fill for that user, or `sad.adsgram.ai` is blocked.
  Check `/adstats` (fallback count). Debug only in a test build: `Adsgram.init({blockId, debug:true})`; remove before release.
- "Too long session": the user must restart the app to get ads (Adsgram rule).
- Do not click your own ads.
