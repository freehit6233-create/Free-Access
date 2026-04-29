"""
XVIP Telegram Video Manager Bot
================================
Features: VPLink API, Neon PostgreSQL, Daily Free Limit, Auto-Delete, Admin Commands
"""

import os
import asyncio
import logging
import random
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─────────────────────────── LOGGING ────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────── ENV VARS ───────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
DATABASE_URL     = os.getenv("DATABASE_URL", "")
VPLINK_API_KEY   = os.getenv("VPLINK_API_KEY", "")

# ─────────────────────────── CONFIG ─────────────────────────────────────────
FREE_VIDEOS_PER_DAY  = 3          # daily free video limit
DEFAULT_ACCESS_HOURS = 3          # default paid/verified access duration (hours)
VIDEO_DELETE_MINUTES = 10         # auto-delete sent videos after N minutes
REPEAT_CHANCE        = 0.02       # 2% chance to show a random repeat video
VPLINK_API_URL       = "https://vplink.in/api"   # VPLink API endpoint

# ─────────────────────────── DATABASE ───────────────────────────────────────

def get_db():
    """Return a new psycopg2 connection."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """Create tables if they don't exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS videos (
        id          SERIAL PRIMARY KEY,
        file_id     TEXT NOT NULL UNIQUE,
        fetched_at  TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id         BIGINT PRIMARY KEY,
        username        TEXT,
        daily_count     INT DEFAULT 0,
        last_reset_date DATE DEFAULT CURRENT_DATE,
        access_until    TIMESTAMP,
        current_index   INT DEFAULT 0,
        joined_at       TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS verifications (
        id          SERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL,
        token       TEXT NOT NULL UNIQUE,
        created_at  TIMESTAMP DEFAULT NOW(),
        used        BOOLEAN DEFAULT FALSE
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT INTO settings (key, value)
    VALUES ('access_hours', '3')
    ON CONFLICT (key) DO NOTHING;
    """
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        conn.close()
        logger.info("Database initialised successfully.")
    except Exception as e:
        logger.error(f"DB init error: {e}")
        raise


# ─────────────────────────── DB HELPERS ─────────────────────────────────────

def db_get_user(user_id: int) -> dict | None:
    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"db_get_user error: {e}")
        return None


def db_upsert_user(user_id: int, username: str | None = None):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """, (user_id, username))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_upsert_user error: {e}")


def db_reset_daily_if_needed(user_id: int):
    """Reset daily count if it's a new day."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET daily_count = 0,
                    last_reset_date = CURRENT_DATE
                WHERE user_id = %s AND last_reset_date < CURRENT_DATE
            """, (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_reset_daily error: {e}")


def db_increment_daily(user_id: int):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users SET daily_count = daily_count + 1 WHERE user_id = %s
            """, (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_increment_daily error: {e}")


def db_set_access(user_id: int, hours: int):
    until = datetime.utcnow() + timedelta(hours=hours)
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users SET access_until = %s WHERE user_id = %s
            """, (until, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_set_access error: {e}")


def db_get_video_count() -> int:
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM videos")
            count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.error(f"db_get_video_count error: {e}")
        return 0


def db_get_video_at_index(index: int) -> str | None:
    """Return file_id at given 0-based index (ordered by id)."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_id FROM videos ORDER BY id LIMIT 1 OFFSET %s",
                (index,)
            )
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"db_get_video_at_index error: {e}")
        return None


def db_get_random_video_file_id() -> str | None:
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT file_id FROM videos ORDER BY RANDOM() LIMIT 1")
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"db_get_random_video error: {e}")
        return None


def db_save_video(file_id: str):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO videos (file_id)
                VALUES (%s)
                ON CONFLICT (file_id) DO NOTHING
            """, (file_id,))
        conn.commit()
        conn.close()
        logger.info(f"Saved video: {file_id[:20]}...")
    except Exception as e:
        logger.error(f"db_save_video error: {e}")


def db_set_user_index(user_id: int, index: int):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET current_index = %s WHERE user_id = %s",
                (index, user_id)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_set_user_index error: {e}")


def db_save_verification_token(user_id: int, token: str):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # Invalidate old tokens for this user
            cur.execute("DELETE FROM verifications WHERE user_id = %s", (user_id,))
            cur.execute("""
                INSERT INTO verifications (user_id, token)
                VALUES (%s, %s)
            """, (user_id, token))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_save_verification_token error: {e}")


def db_check_and_use_token(user_id: int, token: str) -> bool:
    """Returns True if token is valid and marks it used."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM verifications
                WHERE user_id = %s AND token = %s AND used = FALSE
                  AND created_at > NOW() - INTERVAL '30 minutes'
            """, (user_id, token))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE verifications SET used = TRUE WHERE id = %s",
                    (row[0],)
                )
                conn.commit()
        conn.close()
        return bool(row)
    except Exception as e:
        logger.error(f"db_check_token error: {e}")
        return False


def db_get_access_hours() -> int:
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'access_hours'")
            row = cur.fetchone()
        conn.close()
        return int(row[0]) if row else DEFAULT_ACCESS_HOURS
    except Exception as e:
        logger.error(f"db_get_access_hours error: {e}")
        return DEFAULT_ACCESS_HOURS


def db_set_access_hours(hours: int):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE settings SET value = %s WHERE key = 'access_hours'",
                (str(hours),)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"db_set_access_hours error: {e}")


def db_get_recent_verification_count(hours: int = 24) -> int:
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM verifications
                WHERE used = TRUE
                  AND created_at > NOW() - INTERVAL '%s hours'
            """, (hours,))
            count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.error(f"db_get_recent_verifications error: {e}")
        return 0


# ─────────────────────────── ACCESS CHECKS ──────────────────────────────────

def user_has_active_access(user: dict) -> bool:
    if user.get("access_until") and user["access_until"] > datetime.utcnow():
        return True
    return False


def user_within_daily_limit(user: dict) -> bool:
    return user.get("daily_count", 0) < FREE_VIDEOS_PER_DAY


# ─────────────────────────── VPLINK INTEGRATION ─────────────────────────────

def generate_vplink(long_url: str) -> str | None:
    """Call VPLink API and return short URL."""
    try:
        params = {
            "api": VPLINK_API_KEY,
            "url": long_url,
        }
        resp = requests.get(VPLINK_API_URL, params=params, timeout=10)
        data = resp.json()
        if data.get("status") == "success":
            return data.get("shortenedUrl") or data.get("short_url")
        logger.warning(f"VPLink API response: {data}")
        return None
    except Exception as e:
        logger.error(f"VPLink API error: {e}")
        return None


def create_verification_link(bot_username: str, user_id: int) -> str | None:
    """Generate a deep-link and wrap it in VPLink."""
    token = f"{user_id}_{int(time.time())}"
    db_save_verification_token(user_id, token)

    deep_link = f"https://t.me/{bot_username}?start=verify_{user_id}_{token}"
    short_url = generate_vplink(deep_link)
    return short_url


# ─────────────────────────── VIDEO SENDER ───────────────────────────────────

async def send_video_to_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_id: str,
    index: int,
    total: int,
    message_id: int | None = None,
) -> int | None:
    """Send or edit a video message. Returns new message_id."""
    nav_buttons = []
    if index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅ Previous", callback_data=f"nav_prev_{index}"))
    if index < total - 1:
        nav_buttons.append(InlineKeyboardButton("Next ➡", callback_data=f"nav_next_{index}"))

    markup = InlineKeyboardMarkup([nav_buttons]) if nav_buttons else None
    caption = f"🎬 Video {index + 1} of {total}"

    try:
        if message_id:
            # Delete old message and send new (Telegram can't edit video content)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramError:
                pass

        msg = await context.bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=caption,
            reply_markup=markup,
            protect_content=True,
        )

        # Schedule auto-delete after VIDEO_DELETE_MINUTES
        context.application.create_task(
            auto_delete_message(context, chat_id, msg.message_id, VIDEO_DELETE_MINUTES * 60)
        )
        return msg.message_id

    except TelegramError as e:
        logger.error(f"send_video error: {e}")
        return None


async def auto_delete_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay_seconds: int,
):
    """Wait then delete the message."""
    await asyncio.sleep(delay_seconds)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Auto-deleted message {message_id} in chat {chat_id}")
    except TelegramError as e:
        logger.warning(f"Auto-delete failed for {message_id}: {e}")


# ─────────────────────────── /start HANDLER ─────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    args    = context.args  # deep-link params

    # Register user
    db_upsert_user(user.id, user.username)
    db_reset_daily_if_needed(user.id)

    # ── Deep-link verification: start=verify_USERID_TOKEN ──
    if args and args[0].startswith("verify_"):
        parts = args[0].split("_", 2)
        if len(parts) == 3:
            _, uid_str, token = parts
            try:
                uid = int(uid_str)
            except ValueError:
                uid = -1

            if uid == user.id and db_check_and_use_token(user.id, token):
                access_hours = db_get_access_hours()
                db_set_access(user.id, access_hours)
                await context.bot.answer_callback_query(
                    callback_query_id=None
                )  # won't work here – use send_message instead
                await update.message.reply_text(
                    f"✅ Your free {access_hours}-hour access approved!\n\n"
                    "You can now watch unlimited videos. Enjoy! 🎉",
                )
                # Kick off normal flow
                await show_video(update, context, user.id, chat_id)
                return
            else:
                await update.message.reply_text(
                    "❌ Verification failed or link expired. Please try again."
                )
                return

    # ── Normal /start ──
    db_user = db_get_user(user.id)
    if not db_user:
        db_upsert_user(user.id, user.username)
        db_user = db_get_user(user.id)

    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        f"You get {FREE_VIDEOS_PER_DAY} free videos per day.\n"
        "Use ⬅ Previous / Next ➡ to browse.\n\n"
        "Let's start watching! 🎬"
    )
    await show_video(update, context, user.id, chat_id, index=0)


# ─────────────────────────── SHOW VIDEO HELPER ──────────────────────────────

async def show_video(
    update_or_context,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    index: int | None = None,
    prev_message_id: int | None = None,
):
    db_reset_daily_if_needed(user_id)
    db_user = db_get_user(user_id)

    if index is None:
        index = db_user.get("current_index", 0)

    total = db_get_video_count()
    if total == 0:
        await context.bot.send_message(chat_id, "📭 No videos available yet. Check back later!")
        return

    index = max(0, min(index, total - 1))

    # 2% random repeat logic
    if random.random() < REPEAT_CHANCE:
        file_id = db_get_random_video_file_id()
        caption_note = " (🔁 Repeat)"
    else:
        file_id = db_get_video_at_index(index)
        caption_note = ""

    if not file_id:
        await context.bot.send_message(chat_id, "⚠️ Could not load video. Please try again.")
        return

    db_set_user_index(user_id, index)
    db_increment_daily(user_id)
    await send_video_to_user(
        context, chat_id, file_id, index, total, prev_message_id
    )


# ─────────────────────────── NAVIGATION CALLBACKS ───────────────────────────

async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data    = query.data  # nav_next_INDEX or nav_prev_INDEX

    await query.answer()

    db_reset_daily_if_needed(user_id)
    db_user = db_get_user(user_id)
    if not db_user:
        db_upsert_user(user_id)
        db_user = db_get_user(user_id)

    parts     = data.split("_")
    direction = parts[1]               # "next" or "prev"
    cur_index = int(parts[2])

    new_index = cur_index + 1 if direction == "next" else cur_index - 1
    new_index = max(0, new_index)

    total = db_get_video_count()
    if new_index >= total:
        new_index = total - 1

    # ── Access gate: check daily limit ──
    if direction == "next":
        has_access = user_has_active_access(db_user)
        within_limit = user_within_daily_limit(db_user)

        if not has_access and not within_limit:
            # Show "Get Free Access" gate
            bot_me = await context.bot.get_me()
            short_url = create_verification_link(bot_me.username, user_id)

            if short_url:
                caption = (
                    "🔒 You've used your 3 free videos for today!\n\n"
                    "✨ Get Free Access to watch more!\n"
                    "Verify the link below to get 3 hours of unlimited access."
                )
                keyboard = [[InlineKeyboardButton("🔗 Get Link", url=short_url)]]
                await context.bot.send_message(
                    chat_id,
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await context.bot.send_message(
                    chat_id,
                    "⚠️ Could not generate access link. Please try again later."
                )
            return

    prev_msg_id = query.message.message_id
    await show_video(update, context, user_id, chat_id, index=new_index, prev_message_id=prev_msg_id)


# ─────────────────────────── SOURCE CHANNEL WATCHER ─────────────────────────

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-fetch videos posted to SOURCE_CHANNEL_ID."""
    message = update.channel_post or update.message
    if not message:
        return

    chat_id = message.chat_id
    if chat_id != SOURCE_CHANNEL_ID:
        return

    if message.video:
        file_id = message.video.file_id
        db_save_video(file_id)
        logger.info(f"New video saved from source channel: {file_id[:20]}")

    elif message.document and message.document.mime_type and "video" in message.document.mime_type:
        file_id = message.document.file_id
        db_save_video(file_id)
        logger.info(f"New video doc saved from source channel: {file_id[:20]}")


# ─────────────────────────── ADMIN: /status ─────────────────────────────────

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    verifications_24h = db_get_recent_verification_count(24)
    total_videos      = db_get_video_count()
    access_hours      = db_get_access_hours()

    text = (
        "📊 *Bot Status*\n\n"
        f"🎬 Total videos in DB: `{total_videos}`\n"
        f"✅ Verifications (24h): `{verifications_24h}`\n"
        f"⏱ Current access timer: `{access_hours} hours`\n"
        f"🎲 Repeat chance: `{int(REPEAT_CHANCE * 100)}%`\n"
        f"🗑 Video auto-delete: `{VIDEO_DELETE_MINUTES} mins`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ─────────────────────────── ADMIN: /broadcast ──────────────────────────────

async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    message = update.message

    # Must be a reply to a message (image+caption) OR have text
    if message.reply_to_message:
        source = message.reply_to_message
    else:
        await message.reply_text(
            "Usage: Reply to a message (image+caption or text) with /broadcast"
        )
        return

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            user_ids = [row[0] for row in cur.fetchall()]
        conn.close()
    except Exception as e:
        await message.reply_text(f"❌ DB error: {e}")
        return

    sent = 0
    failed = 0

    for uid in user_ids:
        try:
            if source.photo:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=source.photo[-1].file_id,
                    caption=source.caption or "",
                    parse_mode=ParseMode.HTML,
                )
            elif source.text:
                await context.bot.send_message(
                    chat_id=uid,
                    text=source.text,
                    parse_mode=ParseMode.HTML,
                )
            elif source.video:
                await context.bot.send_video(
                    chat_id=uid,
                    video=source.video.file_id,
                    caption=source.caption or "",
                    parse_mode=ParseMode.HTML,
                    protect_content=True,
                )
            sent += 1
            await asyncio.sleep(0.05)  # Avoid rate limits
        except TelegramError:
            failed += 1

    await message.reply_text(
        f"📢 Broadcast complete!\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


# ─────────────────────────── ADMIN: /settimer ───────────────────────────────

async def settimer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        hours = db_get_access_hours()
        await update.message.reply_text(
            f"Current access timer: {hours} hours\n"
            "Usage: /settimer <hours>\nExample: /settimer 6"
        )
        return

    try:
        hours = int(context.args[0])
        if hours < 1 or hours > 720:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid number between 1 and 720.")
        return

    db_set_access_hours(hours)
    await update.message.reply_text(
        f"✅ Access timer updated to {hours} hours."
    )


# ─────────────────────────── EXPIRY CHECKER (background) ────────────────────

async def check_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 5 minutes. Notifies users whose access just expired."""
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=5)

    try:
        conn = get_db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id FROM users
                WHERE access_until IS NOT NULL
                  AND access_until BETWEEN %s AND %s
            """, (window_start, now))
            expired_users = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"expiry_job DB error: {e}")
        return

    for row in expired_users:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "⏰ Your free access has expired!\n\n"
                    "You still get 3 free videos per day.\n"
                    "Click *Next* on any video to get a new access link. 🔗"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except TelegramError as e:
            logger.warning(f"Could not notify user {row['user_id']}: {e}")


# ─────────────────────────── MAIN ───────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Handlers ──
    app.add_handler(CommandHandler("start",     start_handler))
    app.add_handler(CommandHandler("status",    status_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("settimer",  settimer_handler))

    app.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav_(next|prev)_\d+$"))

    # Source channel watcher
    app.add_handler(MessageHandler(
        filters.Chat(SOURCE_CHANNEL_ID) & (filters.VIDEO | filters.Document.VIDEO),
        channel_post_handler,
    ))

    # Also catch channel_post updates
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & (filters.VIDEO | filters.Document.VIDEO),
        channel_post_handler,
    ))

    # ── Background job: expiry checker every 5 minutes ──
    job_queue = app.job_queue
    job_queue.run_repeating(check_expiry_job, interval=300, first=60)

    logger.info("🤖 Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
