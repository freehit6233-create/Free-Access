# 🎬 VVIP VDO TELE BOT

Advanced Telegram Video Bot with 6-hour free access system, admin panel, vplink verification, and auto-delete.

---

## 📁 Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot logic |
| `requirements.txt` | Python dependencies |
| `README.md` | This file |

---

## ⚙️ Environment Variables

Set these in Railway → Project → Variables:

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `ADMIN_ID` | Your Telegram numeric user ID |
| `DATABASE_URL` | Neon Console PostgreSQL connection string |
| `VPLINK_API_KEY` | API key from vplink.in dashboard |
| `CHANNEL_ID` | Private channel ID (e.g. `-100xxxxxxxxxx`) |

---

## 🚀 Railway Deployment

1. Push all files to a GitHub repo.
2. Open [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Add all 5 environment variables above.
4. Railway auto-detects Python. If not, add a `Procfile`:

```
worker: python bot.py
```

5. Deploy. Check logs for `Bot starting...` and `DB initialised ✓`.

---

## 🗄️ Neon PostgreSQL Setup

1. Go to [neon.tech](https://neon.tech) → Create project.
2. Copy the **Connection String** (starts with `postgresql://...`).
3. Paste it as `DATABASE_URL` in Railway variables.
4. Tables are created **automatically** on first bot start — no manual SQL needed.

---

## 🔗 Adding Videos

Videos are fetched from your **private Telegram channel** (`CHANNEL_ID`).

**Steps:**
1. Post a video to your private channel.
2. Note the message ID (forward to [@userinfobot](https://t.me/userinfobot) or check URL).
3. In your bot chat (as admin), send:
   ```
   /addvideo <message_id>
   ```
   Example: `/addvideo 42`

Repeat for each video. They'll be delivered in order (by ID).

> ⚠️ Bot must be added as **admin** to the private channel.

---

## 🤖 Admin Commands

| Command | Description |
|---------|-------------|
| `/panel` | Open admin control panel |
| `/status` | Bot statistics |
| `/addvideo <msg_id>` | Add a video from private channel |
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |

**Admin Panel buttons:**
- 📊 **Status** — Users, verifications, videos count
- 📢 **Broadcast** — Send message to all users
- ⏱ **Set Timer** — Change auto-delete time (default: 10 min)
- ⚙️ **Settings** — Change bot settings via `key=value`

---

## 🔑 Available Settings (via /panel → Settings)

| Key | Default | Description |
|-----|---------|-------------|
| `delete_after_minutes` | `10` | Auto-delete timer for videos |
| `vplink_url` | `https://vplink.in` | vplink base URL |
| `access_hours` | `6` | Free access duration (logic uses this) |

---

## 🔄 User Flow

```
/start
  └─► Welcome + "Get Link" button
        └─► vplink.in short URL generated
              └─► User completes verification
                    └─► Deep-link returns to bot (/start verify_TOKEN)
                          └─► 6-hour access granted
                                └─► First video sent automatically
                                      └─► Previous / Next navigation
                                            └─► Video auto-deletes after N minutes
```

---

## 🛠 Local Development

```bash
pip install -r requirements.txt

export BOT_TOKEN=...
export ADMIN_ID=...
export DATABASE_URL=...
export VPLINK_API_KEY=...
export CHANNEL_ID=...

python bot.py
```

---

## ❓ Troubleshooting

| Problem | Fix |
|---------|-----|
| `copy_message` fails | Make sure bot is admin in private channel |
| Videos not showing | Check `CHANNEL_ID` format (must be `-100xxxxxxx`) |
| DB errors | Check `DATABASE_URL` has `sslmode=require` |
| vplink returns original URL | Check `VPLINK_API_KEY` is correct |
| Bot not starting on Railway | Add `Procfile` with `worker: python bot.py` |
