# Usage statistics (admin only)

## Commands

- **`/stats`** - users (total, new, active), app opens, episodes played, listeners, time listened, and the top series and
  listeners of the week.
- **`/user <telegram id or @username>`** - one person: first seen / last active, access status (paid, free time left),
  app opens, plays, time listened, ads watched, last 5 plays.
- **`/adstats`** - rewarded ads (see `REWARDED_ADS.md`).
- **`/subs`** - paid users.

## What is counted

| Number | Counted when |
|---|---|
| Users, Active | someone messages the bot **or opens / plays in the Mini App** (the app now counts too) |
| App opens | the Mini App page is opened. Opening it again within 10 minutes is the same visit |
| Plays | a play starts in the app (or an episode is sent in chat) |
| Time listened | the app reports the seconds audio was really playing, about every 45 seconds and when audio pauses or the app is hidden |

Good to know:

- Counting of app opens and listening time starts from the day you install this update; older days are not filled in
  (`/stats` shows the start date). Users who only ever used the app before will appear once they open it again.
- Listening time is per UTC day. "Today" in `/stats` is the current UTC day.
- Listening time is reported by the page, so treat it as a good estimate, not an audit (a closed app can lose up to ~45 seconds).
- Only Telegram id, name, username and usage are stored. If you publish a privacy note for the bot, mention this.

## Your own queries

On the server, in the bot folder (`sudo apt install sqlite3` if needed):

```
# listeners and plays per day, last 14 days
sqlite3 bot.db "SELECT date(ts,'unixepoch') day, COUNT(DISTINCT user_id) listeners, COUNT(*) plays FROM plays GROUP BY day ORDER BY day DESC LIMIT 14;"

# hours listened per day, last 14 days
sqlite3 bot.db "SELECT date(day*86400,'unixepoch') day, ROUND(SUM(secs)/3600.0,1) hours, COUNT(DISTINCT user_id) listeners FROM listen GROUP BY day ORDER BY day DESC LIMIT 14;"

# app opens per day
sqlite3 bot.db "SELECT date(ts,'unixepoch') day, COUNT(*) opens, COUNT(DISTINCT user_id) people FROM appopens GROUP BY day ORDER BY day DESC LIMIT 14;"
```
