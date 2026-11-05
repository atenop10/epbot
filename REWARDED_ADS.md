# Rewarded ads: free daily listening + watch an ad for bonus time

**How it works (freemium)**

- Everyone can open the app, browse and search with no ad.
- Every user gets **free listening** (default 30 minutes) once every 24 hours. It starts when they first press play. No ad needed.
- When that time is used up, a **"▶ Ad: +60 min"** button (always visible on the home screen) gives **bonus time** after a rewarded ad.
  The ad is a bonus the user chooses, never a requirement for the basic function.
- Paid users (`/grant`) and admins never see time limits or ads.

This follows what Adsgram accepts for rewarded ads: basic functions work without watching ads, and it is clearly shown that an ad gives a bonus.

## Turn it on with Adsgram

1. **Mini App link.** Adsgram needs your app's Telegram link and web address, so register the Mini App in @BotFather:
   `/newapp` -> your bot -> web app URL `https://play.yourdomain.com/app` -> short name `app`. The link becomes
   `https://t.me/<your_bot>/app`. (`/myapps` in BotFather shows both values later.)
2. **Adsgram account.** partner.adsgram.ai -> *Create -> Ad platform*. Fill in the Telegram direct link, the Web app URL
   (`https://play.yourdomain.com/app`, exactly) and the Bot ID (the number before the `:` in your bot token).
3. **Ad unit.** *Create -> Ad unit* -> block type **Reward** -> your platform. Leave *Reward URL* empty. Press **Start** to send
   the platform and block to moderation (see the checklist below first).
4. **Block id.** Open the block -> *Show code* -> *Copy BlockID* (digits only).
5. **On the server** (new/changed files: `adpass.py`, `setup-rewarded.sh`, `bot.py`, `stream.py`, `miniapp.py`, `miniapp.html`):
   ```
   bash setup-rewarded.sh adsgram 12345 60 30
   ```
   (block id, bonus minutes per ad, free minutes per day). It writes `.env` and restarts the bot.
6. Test with a Telegram account that is not an admin and has no `/grant`.

Switch back to Monetag: `bash setup-rewarded.sh monetag 12190381 60 30`. Turn everything off: `bash setup-rewarded.sh off`.

## Adsgram moderation checklist (what the reviewer checks)

| Their rule | What to make sure of |
|---|---|
| Ad placement must be specified and findable in the app | Home screen top bar has the **"▶ Ad: +60 min"** button; the **"Need more time?"** screen has **"▶ Watch ad"**. Say both in the application. |
| Basic functions must work without ads | Browsing and 30 free minutes a day work with no ad. The reviewer must be able to reach them: if you use force-join channels, temporarily turn them off during review (the reviewer cannot pass a channel check). |
| Clear sign that watching gives a bonus | The button says "+60 min" and the screen says "bonus". |
| TMA link and bot must work during moderation | Bot service running (`systemctl status episodes-bot`), `https://t.me/<bot>/app` opens the app. |
| Web app URL / Bot ID must be correct | Exactly the URL from `/myapps`, Bot ID = number before `:` in the token. |
| Rewards must not be excessive; payout proof | The reward is listening time, not money or crypto, so no payouts exist. A moderate bonus (30-60 min) is safest. |
| Not a template, real functionality | A real catalog with episodes must be present when they look. |
| Audience size must match activity | State your real user numbers. |

Decisions are at Adsgram's discretion; this setup matches the model they describe as allowed but cannot guarantee approval.

**Text you can paste into the application / to @adsgram_supportbot:**

> Telegram Mini App with an audio library. Basic functions (browsing, search and 30 minutes of free listening every day) work
> without any ads. Ad placement: (1) the home screen has a bar at the top with a button "▶ Ad: +60 min"; (2) when the free
> listening time is used up, the screen "Need more time?" shows the button "▶ Watch ad: +60 min". Tapping either button shows one
> Adsgram Reward video; after it is watched to the end the user gets 60 minutes of extra listening time as a bonus. The user can
> close the screen and keep using the app. The reward is listening time only - no money or crypto. Open
> https://t.me/<your_bot>/app, press the bar's button to see the placement.

If Adsgram says they cannot see the ad because the block is not approved yet, ask them to approve for review, or set
`AD_ADSGRAM_DEBUG=on` in `.env` only while they check (test ads), then set it back to `off`.

## What users see

- Open the app -> catalog, plus a bar: "🎁 30 free minutes today - they start when you press play" and a "▶ Ad: +60 min" button.
- Press play -> the free time starts (bar shows time left). When it ends, audio pauses and "Need more time?" appears with the ad
  button, the time until the next free window, and "Not now".
- After an ad: "✅ Bonus added". Bonus time stacks on top, never more than a little under two bonuses at once.
- In the bot chat the same rule applies; the message offers the Open app button. `/status` shows the time left.
- Closing the ad early gives nothing. If the network has no ad or the SDK is blocked, a short fallback bonus is given
  (`AD_GRACE_MINUTES`, max `AD_GRACE_PER_DAY` a day); set `AD_GRACE_MINUTES=0` to turn that off.

## Admin

- `/adstats` - provider, ads watched (24 h / 7 days / total), viewers, free windows started, people listening now, and how often
  the "no ad" fallback was used (a high number means poor ad availability for your users).
- `/grant <id> <days>` removes limits and ads for that person (your paid tier).

## Settings (.env)

| Setting | Default | Meaning |
|---|---|---|
| `AD_PROVIDER` | auto | `adsgram`, `monetag` or `off` |
| `AD_ADSGRAM_BLOCK` | empty | Adsgram Reward block id |
| `AD_REWARD_ZONE` | empty | Monetag Rewarded Interstitial zone id |
| `AD_FREE_MINUTES` | 30 | Free listening per window, no ad. **0 = strict mode** (ad needed for everything; not accepted by Adsgram) |
| `AD_FREE_EVERY_HOURS` | 24 | Hours until the next free window |
| `AD_PASS_MINUTES` | 60 | Bonus minutes per ad |
| `AD_MAX_PER_DAY` | 12 | Ads that count per user per 24 h |
| `AD_MIN_WATCH_SECONDS` | 5 | "Ad finished" reports faster than this are refused |
| `AD_GRACE_MINUTES` / `AD_GRACE_PER_DAY` | 5 / 2 | Fallback bonus when no ad could be shown (0 = never) |
| `AD_ADSGRAM_DEBUG` | off | Adsgram test ads, integration checks only |
| `AD_INAPP` | off | `on` = also show Monetag In-App Interstitial ads to free users (needs `AD_REWARD_ZONE`) |

Existing installs: the new column is added to the database automatically on the first start.

## How it is protected

The server decides, not the page. Before each ad the app gets a signed one-time token; the bonus is only given when the token comes
back valid, unused, not too old and not faster than `AD_MIN_WATCH_SECONDS`, and the user is under the daily limit. A determined
person could still skip the ad by calling the server directly (it cannot see inside the ad network's player); the daily limit caps
that. Adsgram's optional *Reward URL* is meant for apps with 50k+ daily users and its request carries no signature, so it is not used.

## Troubleshooting

- **Nothing changes after copying files:** `sudo systemctl restart episodes-bot`, then `sudo journalctl -u episodes-bot -n 20 --no-pager`.
- **Adsgram "wrong referrer" style error:** the Web app URL / link / Bot ID in your Adsgram platform must match the real app exactly.
- **Ad never plays / many "could not load" in `/adstats`:** the platform or block is not approved yet, no ad is available for that
  user, or an ad blocker / DNS filter blocks `sad.adsgram.ai`.
- **"Please open this app from Telegram":** the page was opened in a normal browser.
- **Pause all of it:** `bash setup-rewarded.sh off`.
- Do not watch or click your own ads - networks ban accounts for it.
