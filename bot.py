"""
Advanced Telegram Video Bot
- Private channel se auto-fetch
- 3 free videos, phir VP link verification (3 hours access)
- protect_content=True, no download/forward
- Admin: /status, /reset, /broadcast (12hr auto-delete)
- APScheduler for timed tasks
- Neon PostgreSQL via psycopg2
"""

import os
import asyncio
import logging
import random
import hashlib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError

# ─────────────────────────────────────────────
# ENV & LOGGING
# ─────────────────────────────────────────────
load_dotenv()

BOT_TOKEN    = os.getenv("BOT_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID   = int(os.getenv("CHANNEL_ID", "0"))   # e.g. -1001234567890
DATABASE_URL = os.getenv("DATABASE_URL")            # Neon postgres://...
VP_API_KEY   = os.getenv("VP_API_KEY", "")          # VP link shortener key

FREE_LIMIT      = 3          # Free videos per session
ACCESS_HOURS    = 3          # Hours access after verification
BROADCAST_HOURS = 12         # Hours before broadcast auto-delete

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────
def get_conn():
    """Neon PostgreSQL connection (sslmode=require automatic in Neon URL)."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """Create tables if not exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id          SERIAL PRIMARY KEY,
                    file_id     TEXT UNIQUE NOT NULL,
                    message_id  BIGINT,
                    added_at    TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS users (
                    user_id         BIGINT PRIMARY KEY,
                    videos_watched  INT DEFAULT 0,
                    access_until    TIMESTAMPTZ,
                    verify_token    TEXT,
                    token_created   TIMESTAMPTZ,
                    last_index      INT DEFAULT 0,
                    seen_all        BOOLEAN DEFAULT FALSE
                );

                CREATE TABLE IF NOT EXISTS verifications (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT,
                    verified_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS broadcasts (
                    id              SERIAL PRIMARY KEY,
                    chat_id         BIGINT,
                    message_id      BIGINT,
                    delete_at       TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                );
            """)
        conn.commit()
    logger.info("Database initialised.")


# ── Video DB ──────────────────────────────────

def db_save_video(file_id: str, message_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO videos (file_id, message_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (file_id, message_id),
            )
        conn.commit()


def db_get_videos() -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM videos ORDER BY id")
            return cur.fetchall()


def db_video_count() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM videos")
            return cur.fetchone()[0]


# ── User DB ───────────────────────────────────

def db_get_user(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()


def db_upsert_user(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
        conn.commit()


def db_update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k} = %s" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {sets} WHERE user_id = %s", vals)
        conn.commit()


def db_reset_all_access():
    """Admin /reset: revoke all verified access."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET access_until = NULL, videos_watched = 0")
        conn.commit()


# ── Verification DB ───────────────────────────

def db_log_verification(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO verifications (user_id) VALUES (%s)", (user_id,))
        conn.commit()


def db_verifications_24h() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM verifications WHERE verified_at > NOW() - INTERVAL '24 hours'"
            )
            return cur.fetchone()[0]


# ── Broadcast DB ─────────────────────────────

def db_save_broadcast(chat_id: int, message_id: int, delete_at: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO broadcasts (chat_id, message_id, delete_at) VALUES (%s, %s, %s)",
                (chat_id, message_id, delete_at),
            )
        conn.commit()


def db_due_broadcasts() -> list:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM broadcasts WHERE delete_at <= NOW()"
            )
            return cur.fetchall()


def db_delete_broadcast_record(bid: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM broadcasts WHERE id = %s", (bid,))
        conn.commit()


# ─────────────────────────────────────────────
# VERIFICATION TOKEN LOGIC
# ─────────────────────────────────────────────
def generate_token(user_id: int) -> str:
    """Create a unique token tied to user_id + current timestamp."""
    raw = f"{user_id}:{time.time()}:{VP_API_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def build_vp_link(token: str) -> str:
    """
    VPLink API se actual short URL banao.
    Exact format: https://vplink.in/api?api=KEY&url=DEST&alias=ALIAS&format=text

    format=text  → response mein sirf plain short URL aata hai (no JSON parsing)
    alias        → token ke pehle 8 chars se unique custom alias
    """
    bot_username = os.getenv("BOT_USERNAME", "RNDAccess_bot")
    # User verify hoke is deep-link pe wapas aayega
    destination  = f"https://t.me/{bot_username}?start=verify_{token}"
    encoded_dest = urllib.parse.quote(destination, safe="")
    alias        = f"v{token[:8]}"   # e.g. "vaBcD1234" — unique per token

    api_url = (
        f"https://vplink.in/api"
        f"?api={VP_API_KEY}"
        f"&url={encoded_dest}"
        f"&alias={alias}"
        f"&format=text"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                text = (await resp.text()).strip()
                # format=text → response sirf short URL hota hai
                # e.g.  https://vplink.in/vaBcD1234
                if text.startswith("http"):
                    logger.info(f"VPLink short URL: {text}")
                    return text
                # Alias already taken? retry without alias
                logger.warning(f"VPLink API returned: {text} — retrying without alias")
                retry_url = (
                    f"https://vplink.in/api"
                    f"?api={VP_API_KEY}"
                    f"&url={encoded_dest}"
                    f"&format=text"
                )
                async with session.get(
                    retry_url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp2:
                    text2 = (await resp2.text()).strip()
                    if text2.startswith("http"):
                        logger.info(f"VPLink short URL (no alias): {text2}")
                        return text2
                    logger.error(f"VPLink retry also failed: {text2}")
    except Exception as e:
        logger.error(f"VPLink API exception: {e}")

    # Fallback: API fail hone par direct destination URL use karo
    logger.warning("VPLink failed — using direct Telegram deep-link as fallback.")
    return destination


def has_valid_access(user: dict) -> bool:
    if not user or not user.get("access_until"):
        return False
    now = datetime.now(timezone.utc)
    access_until = user["access_until"]
    # psycopg2 returns timezone-aware datetime for TIMESTAMPTZ
    if access_until.tzinfo is None:
        access_until = access_until.replace(tzinfo=timezone.utc)
    return now < access_until


# ─────────────────────────────────────────────
# VIDEO SENDING HELPER
# ─────────────────────────────────────────────
async def send_video_to_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    index: int,
    videos: list,
):
    """Send video at `index` with Prev/Next navigation buttons."""
    total = len(videos)
    if total == 0:
        await context.bot.send_message(user_id, "⚠️ Abhi koi video available nahi hai.")
        return

    video = videos[index % total]
    buttons = []

    if total > 1:
        prev_idx = (index - 1) % total
        next_idx = (index + 1) % total
        buttons.append([
            InlineKeyboardButton("⬅️ Previous", callback_data=f"nav_{prev_idx}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"nav_{next_idx}"),
        ])

    markup = InlineKeyboardMarkup(buttons) if buttons else None

    try:
        await context.bot.send_video(
            chat_id=user_id,
            video=video["file_id"],
            caption=f"🎬 Video {index + 1} / {total}",
            reply_markup=markup,
            protect_content=True,   # No download, no forward
        )
        # Update user's last watched index
        db_update_user(user_id, last_index=index)
    except TelegramError as e:
        logger.error(f"Video send error (file_id={video['file_id']}): {e}")
        await context.bot.send_message(user_id, "❌ Video load nahi ho saki. Dobara try karein.")


async def send_verification_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    """Show verification prompt when free limit is reached."""
    token = generate_token(user_id)
    db_update_user(
        user_id,
        verify_token=token,
        token_created=datetime.now(timezone.utc),
    )
    vp_url = await build_vp_link(token)

    text = (
        "🔒 *Free limit khatam ho gayi!*\n\n"
        "✅ Neeche diye link ko verify karo aur *3 ghante ki free access* pao.\n\n"
        "📌 Steps:\n"
        "1️⃣ 'Get Link' button dabao\n"
        "2️⃣ Jo page khule uspe ad close karke wait karo\n"
        "3️⃣ Verify ho jaega, wapas bot pe aao\n\n"
        "⏳ Access milne ke baad 3 ghante valid rahega."
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Get Link", url=vp_url)
    ]])
    await context.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=markup,
    )


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start  OR  /start verify_<token>
    """
    user_id = update.effective_user.id
    db_upsert_user(user_id)
    user = db_get_user(user_id)

    # ── Deep-link verification ──────────────────
    args = context.args  # list of words after /start
    if args and args[0].startswith("verify_"):
        token_from_link = args[0][len("verify_"):]
        stored_token    = user.get("verify_token") if user else None
        token_created   = user.get("token_created") if user else None

        valid = False
        if stored_token and token_from_link == stored_token and token_created:
            # Token must be fresh (< 30 min)
            if token_created.tzinfo is None:
                token_created = token_created.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - token_created).total_seconds()
            if age < 1800:
                valid = True

        if valid:
            access_until = datetime.now(timezone.utc) + timedelta(hours=ACCESS_HOURS)
            db_update_user(
                user_id,
                access_until=access_until,
                verify_token=None,
                token_created=None,
                videos_watched=0,
            )
            db_log_verification(user_id)
            await update.message.reply_text(
                f"✅ *Verification successful!*\n\n"
                f"🎉 Tumhare paas *{ACCESS_HOURS} ghante* ki free access hai.\n"
                f"Ab videos enjoy karo! 🎬",
                parse_mode="Markdown",
            )
            # Show first video
            videos = db_get_videos()
            user = db_get_user(user_id)
            await send_video_to_user(update, context, user_id, user.get("last_index", 0), videos)
            return
        else:
            await update.message.reply_text(
                "❌ Verification link invalid ya expired hai. Dobara try karo.",
            )
            return

    # ── Normal /start ───────────────────────────
    videos = db_get_videos()
    if not videos:
        await update.message.reply_text(
            "👋 *Bot mein aapka swagat hai!*\n\nAbhi koi video available nahi. Jaldi aayengi!",
            parse_mode="Markdown",
        )
        return

    # Check access
    if has_valid_access(user):
        idx = user.get("last_index", 0)
        await send_video_to_user(update, context, user_id, idx, videos)
    elif (user.get("videos_watched") or 0) < FREE_LIMIT:
        idx = user.get("last_index", 0)
        await send_video_to_user(update, context, user_id, idx, videos)
    else:
        await send_verification_message(update, context, user_id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *Bot Usage Guide*\n\n"
        "▶️ /start — Bot shuru karo\n"
        "❓ /help  — Yeh message\n\n"
        "📌 *Features:*\n"
        "• Pehli 3 videos bilkul free\n"
        "• Uske baad link verify karo → 3 ghante ki access\n"
        "• ⬅️ Previous / Next ➡️ buttons se navigate karo\n"
        "• Videos forward ya download nahi ho sakti (protected)\n\n"
        "🔒 Verification ke baad bhi access expire hone par dobara verify karna hoga."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only."""
    if update.effective_user.id != ADMIN_ID:
        return
    verifs = db_verifications_24h()
    vcount = db_video_count()
    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"✅ Last 24h verifications: *{verifs}*\n"
        f"🎬 Total videos in DB: *{vcount}*",
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only — reset all user access."""
    if update.effective_user.id != ADMIN_ID:
        return
    db_reset_all_access()
    await update.message.reply_text("♻️ Sabhi users ka access reset kar diya gaya. Sabko fir verify karna hoga.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin only.
    Usage: Reply to a photo with /broadcast <caption text> <optional link>
    Or: /broadcast <text message>
    Broadcast auto-deletes after BROADCAST_HOURS.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    message = update.message

    # Collect all bot users
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            all_users = [r[0] for r in cur.fetchall()]

    if not all_users:
        await message.reply_text("Koi user nahi mila.")
        return

    delete_at = datetime.now(timezone.utc) + timedelta(hours=BROADCAST_HOURS)
    sent_count = 0
    failed_count = 0

    # Build caption
    caption = " ".join(context.args) if context.args else ""
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1].file_id
        for uid in all_users:
            try:
                sent = await context.bot.send_photo(
                    chat_id=uid,
                    photo=photo,
                    caption=caption or None,
                    parse_mode="Markdown",
                )
                db_save_broadcast(uid, sent.message_id, delete_at)
                sent_count += 1
            except TelegramError:
                failed_count += 1
    else:
        text = caption or (message.text.replace("/broadcast", "").strip())
        if not text:
            await message.reply_text("❌ Koi message ya photo nahi mili. Usage: /broadcast <text> ya photo reply ke saath.")
            return
        for uid in all_users:
            try:
                sent = await context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="Markdown",
                )
                db_save_broadcast(uid, sent.message_id, delete_at)
                sent_count += 1
            except TelegramError:
                failed_count += 1

    await message.reply_text(
        f"📢 Broadcast bheja!\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}\n"
        f"🗑️ Auto-delete: {BROADCAST_HOURS} ghante baad"
    )


# ─────────────────────────────────────────────
# CALLBACK QUERY — Navigation Buttons
# ─────────────────────────────────────────────

async def callback_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db_upsert_user(user_id)
    user = db_get_user(user_id)
    videos = db_get_videos()

    if not videos:
        await query.message.reply_text("⚠️ Koi video nahi mili.")
        return

    data = query.data  # "nav_<index>"
    try:
        requested_index = int(data.split("_")[1])
    except (IndexError, ValueError):
        return

    total = len(videos)
    watched = user.get("videos_watched") or 0

    # ── Access check ─────────────────────────────
    if not has_valid_access(user):
        if watched >= FREE_LIMIT:
            await send_verification_message(update, context, user_id)
            return
        # Increment watched counter
        db_update_user(user_id, videos_watched=watched + 1)
        user = db_get_user(user_id)  # refresh

    # ── Random mode if seen all ───────────────────
    seen_all = user.get("seen_all") or False
    if not seen_all and requested_index >= total:
        db_update_user(user_id, seen_all=True)
        seen_all = True

    if seen_all:
        requested_index = random.randint(0, total - 1)

    await send_video_to_user(update, context, user_id, requested_index, videos)


# ─────────────────────────────────────────────
# CHANNEL POST LISTENER — Auto-fetch videos
# ─────────────────────────────────────────────

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-save videos posted to the private channel."""
    message = update.channel_post
    if not message:
        return
    if message.chat.id != CHANNEL_ID:
        return
    if message.video:
        file_id = message.video.file_id
        db_save_video(file_id, message.message_id)
        logger.info(f"New video saved: {file_id}")


# ─────────────────────────────────────────────
# SCHEDULED TASKS (APScheduler)
# ─────────────────────────────────────────────

async def job_delete_broadcasts(app):
    """Delete expired broadcast messages."""
    due = db_due_broadcasts()
    for row in due:
        try:
            await app.bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
        except TelegramError as e:
            logger.warning(f"Could not delete broadcast msg {row['message_id']}: {e}")
        db_delete_broadcast_record(row["id"])
    if due:
        logger.info(f"Deleted {len(due)} expired broadcast messages.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Handlers ─────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(callback_nav, pattern=r"^nav_\d+$"))

    # Channel posts (bot must be admin of channel)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))

    # ── Scheduler ────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Run every 10 minutes to check for expired broadcasts
    scheduler.add_job(
        job_delete_broadcasts,
        "interval",
        minutes=10,
        args=[app],
        id="delete_broadcasts",
    )
    scheduler.start()

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
