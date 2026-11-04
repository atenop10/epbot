# Ad-unlock: watch an ad, listen free for a limited time

> This package uses **Adsgram**. Setup steps are in `ADSGRAM_SETUP.md`; the rest of this page still applies.

Users who are not admins and have no paid subscription (`/grant`) see a **"Watch ad & unlock"** screen in the Mini App.
When the Adsgram Reward ad finishes, they can listen for `AD_PASS_MINUTES` minutes (default 60).
A small bar on the home screen shows the time left, with a **+ time** button to watch another ad before it runs out.
Paid users and admins never see any of this.

## Turn it on

1. In Monetag, make sure your Mini App URL (`https://play.yourdomain.com/app`) is added under **Telegram Mini App** and
   the Rewarded Interstitial zone belongs to it.
2. Copy all the files of this package to the server (new files: `adpass.py`, `setup-rewarded.sh`; changed: `bot.py`,
   `stream.py`, `miniapp.py`, `miniapp.html`).
3. Run in the bot folder:
   ```
   bash setup-rewarded.sh 12190381 60
   ```
   (zone id, minutes per ad). It writes `.env` and restarts the bot. No code editing needed.
4. Open the app from a Telegram account that is not an admin and has no `/grant`. You get the unlock screen.
   Admins see no gate - test with a second account.

Turn it off: `bash setup-rewarded.sh off`.

**Heads up:** once the zone is set, *everyone* who is not an admin or paid has to watch an ad, including people who
used the bot freely before.

## What users see

- Open the app -> "Listen free" screen -> **Watch ad & unlock** -> ad plays -> app opens with the time bar.
- When time runs out, audio pauses and the unlock screen returns. Anything they typed in the bot chat gets a message with an
  **Open app** button instead of the old "paid members only" text.
- `/status` in the bot shows how many free minutes are left.
- If the ad network has no ad or the SDK is blocked, the user gets a short fallback pass (`AD_GRACE_MINUTES`, max
  `AD_GRACE_PER_DAY` times a day) instead of being stuck. Set `AD_GRACE_MINUTES=0` to disable.

## Admin

- `/adstats` - ads watched (24 h / 7 days / total), viewers, people on a free pass right now.
- `/grant <id> <days>` still works and removes ads for that person (your paid tier).
- Time stacks: an extra ad adds `AD_PASS_MINUTES` on top of what is left, but never more than a little under two passes at once.

## Settings (.env)

| Setting | Default | Meaning |
|---|---|---|
| `ADSGRAM_BLOCK_ID` | empty (off) | Your Adsgram **Reward** block id (digits only) |
| `AD_PASS_MINUTES` | 60 | Listening time per ad |
| `AD_MAX_PER_DAY` | 12 | Ads that count per user per 24 h |
| `AD_MIN_WATCH_SECONDS` | 5 | "Ad finished" reports faster than this are refused |
| `AD_GRACE_MINUTES` / `AD_GRACE_PER_DAY` | 5 / 2 | Fallback pass when no ad could be shown (0 = never) |
| `AD_SDK_URL` | `https://sad.adsgram.ai/js/sad.min.js` | Change only if Adsgram gives another address |

## How it is protected

The server decides, not the page. Before each ad the app gets a signed one-time token; the pass is only given when the
token comes back valid, unused, not too old and not faster than `AD_MIN_WATCH_SECONDS`, and the user is under the
daily limit. Limits: one token can't be reused, one user can't claim twice at the same moment.

A determined person can still skip the ad by calling the server directly (the check can't see inside Monetag's player).
The daily limit caps how much that can get them. For airtight proof, Monetag's server-to-server postback can confirm each view;
that needs a postback URL set in your Monetag dashboard and can be added later.

## Troubleshooting

- **Nothing changes after copying files:** `sudo systemctl restart episodes-bot`, then check
  `sudo journalctl -u episodes-bot -n 20 --no-pager`.
- **Ad never plays, user always gets the 5-minute fallback:** Monetag has no ad for that user, or an ad blocker / DNS filter
  blocks `libtl.com`. Check `/adstats` ("could not load" count) and that the zone belongs to your Mini App URL in Monetag.
- **"Please open this app from Telegram":** the page was opened in a normal browser.
- **Everyone is locked out and you want to pause the gate:** `bash setup-rewarded.sh off`.
- Do not click your own ads.
