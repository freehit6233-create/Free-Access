# 🎬 VVIP VDO TELE BOT — Advanced Video Access Bot

A Telegram bot that fetches videos from a private channel, enforces a free-trial + link-verification access system, and auto-manages content delivery.

---

## ✨ Features

| Feature | Detail |
|---|---|
| Free trial | First 3 videos free per 24-hour window |
| Link gate | VP-shortened one-time link → 3 hours full access |
| Persistence | Resumes from last watched video across sessions |
| Auto-delete | Videos → 10 min, command msgs → 10 sec |
| Protect content | No download / no forward |
| Random inject | 2% chance of a random channel video between navigation |
| Admin panel | `/status`, `/reset`, `/broadcast` |
| Broadcast TTL | Auto-deletes broadcast messages after 12 hours |
| Access expiry | Users notified when 3-hour window closes |

---

## 🗂 File Structure

```
.
├── bot.py            # All bot logic
├── requirements.txt  # Python dependencies
└── README.md
```

---

## ⚙️ Environment Variables

Set these in your Railway service **Variables** tab (or `.env` for local dev):

| Variable | Description | Example |
|---|---|---|
| `BOT_TOKEN` | BotFather token | `123456:ABC...` |
| `ADMIN_ID` | Your Telegram numeric ID | `987654321` |
| `CHANNEL_ID` | Private channel numeric ID | `-1001234567890` |
| `DATABASE_URL` | Neon PostgreSQL connection string | `postgresql://user:pass@host/db` |
| `VP_LINK_TOKEN` | VP Link API key | `f978712f218b482b5b66d00bb570e97a49bd4d08` |
| `BOT_USERNAME` | Bot username without `@` | `RNDAccess_bot` |

---

## 🚀 Deployment — Railway.app

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### Step 2 — Create Railway Project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repository.

### Step 3 — Add Environment Variables

In Railway dashboard → your service → **Variables**, add all 6 env vars listed above.

### Step 4 — Set Start Command

Railway auto-detects Python. If it doesn't pick up the start command, add in **Settings → Deploy**:

```
python bot.py
```

### Step 5 — Deploy

Click **Deploy** (or push a new commit). Check logs — you should see:

```
DB pool ready
Bot started
```

---

## 🗄 Database Setup — Neon Console

1. Create a project at [neon.tech](https://neon.tech).
2. Copy the **connection string** (psql format, SSL required) → paste as `DATABASE_URL`.
3. The bot runs `CREATE TABLE IF NOT EXISTS …` on startup — no manual migration needed.

---

## 📡 Channel Setup

1. Create a **private Telegram channel**.
2. Add the bot as an **Administrator** with permission to **read messages**.
3. Get the channel ID (forward a message to @userinfobot, or use `@username_to_id_bot`).
4. Set `CHANNEL_ID` to the numeric ID (usually starts with `-100…`).

> ⚠️ The bot scans messages 1–5000 on first start to build its video index. This can take a minute or two. The index refreshes every 30 minutes automatically.

---

## 🤖 Bot Commands

### User Commands

| Command | Action |
|---|---|
| `/start` | Start watching / resume |
| `/help` | Show help message |

### Admin Commands

| Command | Action |
|---|---|
| `/status` | Verifications last 24h + total videos |
| `/reset` | Reset all users' verification & access |
| `/broadcast` | Reply to any message → broadcasts to all users |

---

## 🔑 VP Link Flow

```
User presses "Get Link"
        │
        ▼
vplink.in shortens destination URL
        │
        ▼
User completes ad/verification on VP Link
        │
        ▼
Redirects to: t.me/RNDAccess_bot?start=verify_<uid>_<token>
        │
        ▼
Bot validates token → grants 3-hour access
        │
        ▼
Admin receives notification
```

---

## 🧩 Access Logic

```
/start or nav button pressed
        │
        ├── index < 3 AND within 24h window? ──► show video (free)
        │
        ├── active access_until in future?   ──► show video
        │
        └── else ──► show verify gate (deletes old gate first)

After 3 hours → access_until expires → user notified
After 24 hours → free window resets → 3 more free videos from last position
```

---

## 🛡 Security Notes

- Token is HMAC-SHA256 of `user_id:VP_TOKEN` — cannot be forged without the secret.
- `protect_content=True` on all video sends → Telegram blocks forward & download.
- Old verification gate message is deleted before showing a new one (anti-spam).

---

## 📦 Local Development

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Create .env and export variables
export BOT_TOKEN=...
export ADMIN_ID=...
export CHANNEL_ID=...
export DATABASE_URL=...
export VP_LINK_TOKEN=...
export BOT_USERNAME=RNDAccess_bot

python bot.py
```
