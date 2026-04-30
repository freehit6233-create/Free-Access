# 🎬 VVIP Video Telegram Bot

A high-performance Telegram video management bot with free-limit logic, VPLink verification, admin panel, and auto-delete — built with **aiogram 3**, **asyncpg**, and **Neon PostgreSQL**.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🎥 Video Navigation | One video at a time with Prev/Next buttons |
| 🔒 Free Limit | 3 free videos per 24 hours |
| 🔗 VPLink Verification | Get N-hour free access via link verify |
| ⏰ Auto-Delete | Videos delete after 10 min |
| 📢 Broadcast | Admin broadcast with 12h auto-delete |
| 📊 Status | Channel video count + 24h verifications |
| ⏱ Set Timer | Admin can change free hours dynamically |
| 🛡 protect_content | Prevent forwarding/saving of videos |

---

## 🚀 Setup

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/VVIP-VDO-TELE-BOT.git
cd VVIP-VDO-TELE-BOT
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Create `.env` file
```env
BOT_TOKEN=your_bot_token_here
ADMIN_ID=your_telegram_user_id
PRIVATE_CHANNEL_ID=-100xxxxxxxxxx
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
VPLINK_API=https://vplink.in/api?api=YOUR_API_KEY&url=
BOT_USERNAME=your_bot_username
```

### 4. Get your credentials

#### BotFather
1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. `/newbot` → get your `BOT_TOKEN`
3. Set commands:
```
start - Start watching videos
help - Show help
status - Admin: Bot status
settimer - Admin: Set free hours
broadcast - Admin: Broadcast message
ban - Admin: Ban user
unban - Admin: Unban user
```

#### Private Channel
1. Create a private channel
2. Add your bot as **admin** with permission to read messages
3. Get the channel ID (starts with `-100...`)

#### Neon PostgreSQL
1. Go to [neon.tech](https://neon.tech) → Create project
2. Copy the **Connection string** → set as `DATABASE_URL`

#### VPLink API
1. Go to [vplink.in](https://vplink.in) → Register → Get API key
2. `VPLINK_API=https://vplink.in/api?api=YOUR_KEY&url=`

---

## 🚂 Railway Deployment

### Method 1: GitHub (Recommended)
1. Push code to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo
4. Add all environment variables from `.env` in Railway's **Variables** tab
5. Railway auto-deploys on every push ✅

### Method 2: Railway CLI
```bash
npm i -g @railway/cli
railway login
railway init
railway up
```

---

## 🖥️ AWS EC2 Deployment (Alternative)

```bash
# On EC2 instance
git clone https://github.com/YOUR_USERNAME/VVIP-VDO-TELE-BOT.git
cd VVIP-VDO-TELE-BOT
pip install -r requirements.txt
cp .env.example .env   # fill in your values

# systemd service
sudo nano /etc/systemd/system/vvipbot.service
```

```ini
[Unit]
Description=VVIP Video Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/VVIP-VDO-TELE-BOT
ExecStart=/usr/bin/python3 bot.py
EnvironmentFile=/home/ubuntu/VVIP-VDO-TELE-BOT/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable vvipbot
sudo systemctl start vvipbot
sudo journalctl -u vvipbot -f   # logs
```

---

## 📁 Project Structure

```
VVIP-VDO-TELE-BOT/
├── bot.py            # Main bot code
├── requirements.txt  # Python dependencies
├── .env              # Environment variables (gitignored)
├── .env.example      # Template
├── .gitignore
└── README.md
```

---

## 🔧 Admin Commands

| Command | Usage |
|---|---|
| `/status` | Show videos count + verifications |
| `/settimer 6` | Set free access to 6 hours |
| `/broadcast` | Reply to a message to broadcast it |
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |

---

## 🗄️ Database Schema

```sql
users          -- User state, free_used, access_until, video_index
settings       -- Key-value config (free_hours, etc.)
verifications  -- Log of all link verifications
broadcast_msgs -- Track broadcast messages for auto-delete
```

---

## 🔄 Update Workflow (EC2)

```bash
git pull origin main
sudo systemctl restart vvipbot
```

---

## ⚠️ Notes

- Bot must be **admin** in the private channel to copy videos
- `protect_content=True` prevents users from forwarding/saving videos
- VPLink callback uses `https://t.me/{BOT_USERNAME}?start=verify_{user_id}` format
- Videos are cached for 5 minutes to reduce API calls

---

## 📄 License

MIT — Use freely, modify as needed.
