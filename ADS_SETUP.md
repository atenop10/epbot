# Ads on the player page - setup

The ad shows on the **web player** (the page the "Play" button opens), under the Prev / Next links.
It does not show for episodes sent as Telegram audio files.

## One-time setup (Ubuntu server)

1. Copy the whole folder to the server and run the normal setup:
   ```
   bash setup.sh
   ```
2. Turn on the streaming player (needs a domain pointing to your server):
   ```
   bash setup-stream.sh play.yourdomain.com
   ```
3. Turn on the ad (your 320x50 banner is already in `ad.html`):
   ```
   bash setup-ads.sh
   ```
   Use `bash setup-ads.sh 280` instead for a 300x250 banner.
4. In the bot, request an R2 episode and tap **Play**. The banner appears under the player buttons.

## Changing or removing the ad later

From Telegram, as admin:

- `/setad` - shows whether an ad is ON or OFF
- `/setad <paste the full ad code>` - sets a new ad (applies to the next player page opened, no restart)
- `/setad off` - removes the ad

Or edit `ad.html` in the bot folder directly (no restart needed; it is re-read when the file changes).

## Ads not showing? Use /adtest

Send `/adtest` to the bot (admin). It checks the player, the ad file, the box height and your storage mode, then
gives a **test link**. Open it on your phone: under the ad box a line tells you what the ad network did:

- "FAILED to load ... bauval.org" - the ad script is blocked (ad blocker, DNS/ISP filter) or the ad domain is down.
- "Ad script loaded but drew nothing" - the network is not serving your site (not approved yet, no fill).
- "Ad network drew N element(s)" - it works; an empty box then just means no ad was available right now.

Most common reasons ads never appear:
1. Episodes are stored in the **Telegram channel** (`STORAGE_CHANNEL_ID` set). Those are sent as files, not through
   the player, so there is no ad. Only R2 episodes open the web player.
2. Streaming player not enabled (`STREAM_BASE_URL` / `STREAM_SECRET` empty) or the bot was not restarted.
3. `ad.html` missing or empty in the bot folder (the "Advertisement" label does not appear at all then).
4. Your ad network account has not approved the player domain yet.
5. Ad blocker / Telegram in-app browser. Test in Chrome without extensions.

## Settings (.env)

| Setting | Meaning |
|---|---|
| `STREAM_BASE_URL` / `STREAM_SECRET` | Must be set (done by setup-stream.sh), otherwise there is no player to show ads on |
| `STREAM_AD_HEIGHT` | Height of the ad box in pixels. 60 for 320x50, 280 for 300x250 |
| `STREAM_AD_FILE` | Optional. File that holds the ad code (default `ad.html`) |

## Notes

- The ad runs in a sandboxed iframe, so ad scripts cannot read the player, the cookies or the audio.
- Ad code longer than about 4000 characters cannot be sent through `/setad`; put it in `ad.html` instead.
- Some ad networks only serve ads when the site domain is approved in their dashboard. If the box stays empty,
  add your player domain (the one in `STREAM_BASE_URL`) to your ad network account.
- Ad blockers in the listener's browser will hide the ad.

## New in this version: mass delete (admin)

- `/delete <story id> 45` - one episode
- `/delete <story id> 10-25` - a range
- `/delete <story id> 3 7 10-15` - several
- `/delete <story id> all` - the whole story
- `/renumber <story id>` - close gaps after deleting

Every delete asks for confirmation first and cannot be undone.

## New in this version: uploads are numbered in sending order

Send many audio files at once (after `/addto <story id>`) and they are saved **one at a time in the order you sent
them**, so they become Ep n, n+1, n+2... exactly in that order. Telegram message order decides, not which file
finishes uploading first. If a file fails, it does not take an episode number, so there are no gaps.
Tip: for a big batch, send the files in the order you want, then wait until every file shows "Added ... as Ep N".
