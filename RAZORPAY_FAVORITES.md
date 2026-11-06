# Paid passes (Razorpay) and favorites with new-episode alerts

## 1. Paid passes: Rs 15 for 7 days or Rs 49 for 30 days, no ads

### What users get
- `/subscribe` (or the "⭐ Ad-free" button in the app) first asks which plan: **Weekly Rs 15 / 7 days** or **Monthly Rs 49 / 30 days**. Then it shows a Razorpay payment page (UPI, cards, net banking).
- After paying, access starts within seconds: no ads and no time limit, for the days of the chosen plan. Paying again adds the days on top of what is left.
- Each payment is one-time, not an auto-renewing subscription. The user pays again when it ends.
- Admin gets a message "💰 Rs 15 received from <id>" (or Rs 49) for every payment. `/subs` lists paid users (same list as `/grant`).

### Set it up (about 10 minutes)
1. Razorpay Dashboard -> Account & Settings -> API keys -> generate a key. You get a Key ID and a Key Secret. (Use Test mode first if you want to try it.)
2. Razorpay Dashboard -> Account & Settings -> Webhooks -> Add new webhook:
   - URL: `https://play.audiosfm.online/razorpay/webhook`  (your STREAM_BASE_URL + `/razorpay/webhook`)
   - Secret: make up a long random text and keep it
   - Active events: tick **payment_link.paid** only
3. On the VPS, in the bot folder:
   ```
   bash setup-pay.sh <key_id> <key_secret> <webhook_secret> 15:7,49:30
   ```
   The last part is the plans as `rupees:days`, comma separated. To change prices later, run the same command again with new plans (for example `19:7,59:30`).
4. Test with a normal Telegram account: send `/subscribe`, pay, and check that "✅ Payment received" arrives.
   Test mode: use the Razorpay test UPI/card details from their docs. Switch to Live keys with the same command when ready.

If the webhook is not set up, payments still work: the app checks automatically every few seconds while the pay screen is open, and the "✅ I've paid" button checks on demand. The webhook is the reliable backup (for example if the user closes the app right after paying).

Turn it off: `bash setup-pay.sh off`.

### Notes
- Money goes to your Razorpay account; Razorpay charges its own fee per payment, so check what Rs 15 leaves after fees (Razorpay's fee is a percentage plus GST, so the smaller plan keeps the least).
- Refunds and invoices are done in the Razorpay dashboard. To cancel someone's access use `/revoke <id>`.
- The paid pass needs the streaming player/Mini App (`STREAM_BASE_URL`), because the payment webhook comes to it.

## 2. Favorites with new-episode alerts
- Open a series in the app and tap "☆ Add to favorites". There is also a "⭐ Favorites" tab on the home screen, and `/favs` in the bot.
- When you add an episode (upload to the bot, `/addto`, or `/sync` finds new files in R2) every follower gets a message in the bot chat with a "▶ Listen now" button that opens that series in the app.
- Several uploads within `NOTIFY_DELAY_SECONDS` (default 90) become one message ("Episodes 5-7 are out").
- Each alert has "🔕 Stop alerts for this series". If a user blocks the bot, their favorites are removed.
- A series without a name gets no alerts. Name it with `/name <id> <title>` first.
- Users who opened the app through a link, without ever pressing Start in the bot, may not be reachable by the bot; the app asks for permission when they add a favorite.
