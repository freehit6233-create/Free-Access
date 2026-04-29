# XVIP Telegram Video Manager Bot

Advanced Telegram bot with VPLink verification, Neon PostgreSQL, daily free limits, and auto-delete.

---

## Features

| Feature | Details |
|---|---|
| Auto video fetch | Watches SOURCE_CHANNEL, saves file_id to DB |
| protect_content | All videos — no download, no forward |
| Auto-delete | Videos deleted 10 minutes after sending |
| Daily free limit | 3 videos/day per user |
| VPLink integration | Generates verified short links for access |
| 3-hour access | After verification, unlimited videos for 3 hours |
| 2% repeat | Random repeat of older videos |
| Pagination | Previous / Next buttons, edits message in place |
| Admin commands | /status, /broadcast, /settimer |
| Expiry notification | User notified when 3-hour access expires |

---

## File Structure

```
.
├── bot.py            ← Main bot (all logic)
├── requirements.txt  ← Python dependencies
└── README.md         ← This file
```

---

## Environment Variables

Set these in Railway (or .env locally):

| Variable | Description | Example |
|---|---|---|
| `BOT_TOKEN` | Your BotFather token | `123456:ABC-DEF...` |
| `ADMIN_ID` | Your Telegram numeric user ID | `987654321` |
| `SOURCE_CHANNEL_ID` | Private channel ID (negative number) | `-1001234567890` |
| `DATABASE_URL` | Neon PostgreSQL connection string | `postgresql://user:pass@host/db` |
| `VPLINK_API_KEY` | Your VPLink API key | `abc123xyz` |

---

## Local Setup

```bash
# 1. Clone the repo
git clone https://github.com/your-username/your-bot-repo.git
cd your-bot-repo

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set environment variables
export BOT_TOKEN="your_token"
export ADMIN_ID="your_id"
export SOURCE_CHANNEL_ID="-1001234567890"
export DATABASE_URL="postgresql://..."
export VPLINK_API_KEY="your_vplink_key"

# 5. Run
python bot.py
```

---

## Railway Deployment

### Step 1 — Push code to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Step 2 — Create Railway project
1. Go to [railway.app](https://railway.app) → New Project
2. Select **Deploy from GitHub repo**
3. Choose your repository

### Step 3 — Add environment variables
In Railway dashboard → your service → **Variables** tab, add all 5 variables from the table above.

### Step 4 — Set start command
In **Settings** → **Deploy** → **Start Command**:
```
python bot.py
```

### Step 5 — Deploy
Railway will auto-deploy on every `git push` to `main`.

---

## Neon PostgreSQL Setup

1. Go to [neon.tech](https://neon.tech) → Create project
2. Copy the **Connection string** (it looks like `postgresql://user:pass@host.neon.tech/neondb?sslmode=require`)
3. Set it as `DATABASE_URL` in Railway

The bot creates all tables automatically on first run:
- `videos` — stores file_id of each video
- `users` — tracks daily count, access expiry, current index
- `verifications` — one-time tokens for VPLink verification
- `settings` — configurable values (access_hours)

---

## How the Verification Flow Works

```
User clicks "Next" after 3 free videos
        ↓
Bot calls VPLink API → generates short URL wrapping
    t.me/YourBot?start=verify_USERID_TOKEN
        ↓
User opens short URL → VPLink interstitial page
        ↓
User clicks "Continue" on VPLink page
        ↓
Telegram opens: t.me/YourBot?start=verify_USERID_TOKEN
        ↓
Bot validates token (30-min window, single use)
        ↓
✅ Access granted for 3 hours (configurable)
```

---

## Admin Commands

| Command | Who | Description |
|---|---|---|
| `/start` | Everyone | Start bot, shows first video |
| `/status` | Admin only | Verifications (24h), total videos, timer |
| `/broadcast` | Admin only | Reply to any message → sends to all users |
| `/settimer 6` | Admin only | Change access duration to 6 hours |

### Broadcast usage
1. Send or forward the image/text you want to broadcast
2. Reply to it with `/broadcast`
3. Bot sends it to all registered users

---

## Configuration (in bot.py)

```python
FREE_VIDEOS_PER_DAY  = 3      # daily free limit
DEFAULT_ACCESS_HOURS = 3      # hours of verified access
VIDEO_DELETE_MINUTES = 10     # auto-delete timer
REPEAT_CHANCE        = 0.02   # 2% random repeat chance
```

---

## Source Channel Setup

1. Create a **private Telegram channel**
2. Add your bot as an **Administrator** with "Post Messages" permission
3. Get the channel ID (use [@userinfobot](https://t.me/userinfobot) or forward a message to a bot that shows IDs)
4. Set it as `SOURCE_CHANNEL_ID` (will be a negative number like `-1001234567890`)

Any video posted to this channel will be automatically saved to the database.
