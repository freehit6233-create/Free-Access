"""
XVIP Telegram Video Manager Bot
================================
v2 Fixes:
- Connection pooling (ConnectionPool) → much faster DB calls
- All-videos-seen → auto random mode
- Verify success: welcome msg auto-deletes, video appears instantly
- Video counter shows "Video X of Total"
- Clean UI on verify callback
"""

import os
import re
import asyncio
import logging
import random
import time
import requests
import psycopg
import psycopg_pool
from psycopg.rows import dict_row
from datetime import datetime, timedelta, timezone
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
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
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
DATABASE_URL      = os.getenv("DATABASE_URL", "")
VPLINK_API_KEY    = os.getenv("VPLINK_API_KEY", "")

# ─────────────────────────── CONFIG ─────────────────────────────────────────
FREE_VIDEOS_PER_DAY  = 3
DEFAULT_ACCESS_HOURS = 3
VIDEO_DELETE_MINUTES = 10
REPEAT_CHANCE        = 0.05   # 5% random repeat (as per your existing setting)
VPLINK_API_URL       = "https://vplink.in/api"

# ─────────────────────────── CONNECTION POOL ────────────────────────────────
# Pool opens 2 connections at startup, max 10. Reused across all requests.
# This eliminates the per-call connect overhead → much faster responses.
_pool: psycopg_pool.ConnectionPool | None = None

def get_pool() -> psycopg_pool.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg_pool.ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool

def get_db():
    """Returns a pooled connection context manager."""
    return get_pool().connection()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────── DATABASE INIT ──────────────────────────────────

def init_db():
    ddl = """
    CREATE TABLE IF NOT EXISTS videos (
        id         SERIAL PRIMARY KEY,
        file_id    TEXT NOT NULL UNIQUE,
        fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id         BIGINT PRIMARY KEY,
        username        TEXT,
        daily_count     INT DEFAULT 0,
        last_reset_date DATE DEFAULT CURRENT_DATE,
        access_until    TIMESTAMP WITH TIME ZONE,
        current_index   INT DEFAULT 0,
        random_mode     BOOLEAN DEFAULT FALSE,
        joined_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS verifications (
        id         SERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        token      TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        used       BOOLEAN DEFAULT FALSE
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT INTO settings (key, value) VALUES ('access_hours', '3')
    ON CONFLICT (key) DO NOTHING;

    -- Add random_mode column if upgrading from older schema
    ALTER TABLE users ADD COLUMN IF NOT EXISTS random_mode BOOLEAN DEFAULT FALSE;
    """
    try:
        with get_db() as conn:
            conn.execute(ddl)
        logger.info("Database initialised.")
    except Exception as e:
        logger.error(f"DB init error: {e}")
        raise


# ─────────────────────────── DB HELPERS ─────────────────────────────────────

def db_get_user(user_id: int) -> dict | None:
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                return cur.fetchone()
    except Exception as e:
        logger.error(f"db_get_user: {e}")
        return None


def db_upsert_user(user_id: int, username: str | None = None):
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
            """, (user_id, username))
    except Exception as e:
        logger.error(f"db_upsert_user: {e}")


def db_reset_daily_if_needed(user_id: int):
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE users
                SET daily_count = 0, last_reset_date = CURRENT_DATE
                WHERE user_id = %s AND last_reset_date < CURRENT_DATE
            """, (user_id,))
    except Exception as e:
        logger.error(f"db_reset_daily: {e}")


def db_increment_daily(user_id: int):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET daily_count = daily_count + 1 WHERE user_id = %s",
                (user_id,)
            )
    except Exception as e:
        logger.error(f"db_increment_daily: {e}")


def db_set_access(user_id: int, hours: int):
    until = utcnow() + timedelta(hours=hours)
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET access_until = %s WHERE user_id = %s",
                (until, user_id)
            )
    except Exception as e:
        logger.error(f"db_set_access: {e}")


def db_set_random_mode(user_id: int, enabled: bool):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET random_mode = %s WHERE user_id = %s",
                (enabled, user_id)
            )
    except Exception as e:
        logger.error(f"db_set_random_mode: {e}")


def db_get_video_count() -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM videos")
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception as e:
        logger.error(f"db_get_video_count: {e}")
        return 0


def db_get_video_at_index(index: int) -> str | None:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file_id FROM videos ORDER BY id LIMIT 1 OFFSET %s",
                    (index,)
                )
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"db_get_video_at_index: {e}")
        return None


def db_get_random_video_file_id() -> str | None:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT file_id FROM videos ORDER BY RANDOM() LIMIT 1")
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"db_get_random_video: {e}")
        return None


def db_save_video(file_id: str):
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO videos (file_id) VALUES (%s)
                ON CONFLICT (file_id) DO NOTHING
            """, (file_id,))
        logger.info(f"Video saved: {file_id[:25]}...")
    except Exception as e:
        logger.error(f"db_save_video: {e}")


def db_set_user_index(user_id: int, index: int):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET current_index = %s WHERE user_id = %s",
                (index, user_id)
            )
    except Exception as e:
        logger.error(f"db_set_user_index: {e}")


def db_save_verification_token(user_id: int, token: str):
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM verifications WHERE user_id = %s", (user_id,))
            conn.execute(
                "INSERT INTO verifications (user_id, token) VALUES (%s, %s)",
                (user_id, token)
            )
    except Exception as e:
        logger.error(f"db_save_token: {e}")


def db_check_and_use_token(user_id: int, token: str) -> bool:
    """
    Validates token — must belong to user_id, be unused, created within 60 min.
    """
    try:
        cutoff = utcnow() - timedelta(minutes=60)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM verifications
                    WHERE user_id    = %s
                      AND token      = %s
                      AND used       = FALSE
                      AND created_at > %s
                """, (user_id, token, cutoff))
                row = cur.fetchone()
            if row:
                conn.execute(
                    "UPDATE verifications SET used = TRUE WHERE id = %s",
                    (row[0],)
                )
        logger.info(f"Token check uid={user_id} token={token} valid={bool(row)}")
        return bool(row)
    except Exception as e:
        logger.error(f"db_check_token: {e}")
        return False


def db_get_access_hours() -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key = 'access_hours'")
                row = cur.fetchone()
        return int(row[0]) if row else DEFAULT_ACCESS_HOURS
    except Exception as e:
        logger.error(f"db_get_access_hours: {e}")
        return DEFAULT_ACCESS_HOURS


def db_set_access_hours(hours: int):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE settings SET value = %s WHERE key = 'access_hours'",
                (str(hours),)
            )
    except Exception as e:
        logger.error(f"db_set_access_hours: {e}")


def db_get_recent_verification_count(hours: int = 24) -> int:
    try:
        cutoff = utcnow() - timedelta(hours=hours)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM verifications WHERE used=TRUE AND created_at > %s",
                    (cutoff,)
                )
                row = cur.fetchone()
                return row[0] if row else 0
    except Exception as e:
        logger.error(f"db_get_recent_verifications: {e}")
        return 0


def db_get_all_user_ids() -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM users")
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"db_get_all_user_ids: {e}")
        return []


# ─────────────────────────── ACCESS CHECKS ──────────────────────────────────

def user_has_active_access(user: dict) -> bool:
    au = user.get("access_until")
    if not au:
        return False
    if au.tzinfo is None:
        au = au.replace(tzinfo=timezone.utc)
    return au > utcnow()


def user_within_daily_limit(user: dict) -> bool:
    return (user.get("daily_count") or 0) < FREE_VIDEOS_PER_DAY


# ─────────────────────────── VPLINK INTEGRATION ─────────────────────────────

def generate_vplink(long_url: str) -> str | None:
    try:
        resp = requests.get(
            VPLINK_API_URL,
            params={"api": VPLINK_API_KEY, "url": long_url},
            timeout=10
        )
        data = resp.json()
        if data.get("status") == "success":
            return (data.get("shortenedUrl")
                    or data.get("short_url")
                    or data.get("shorten_url"))
        logger.warning(f"VPLink response: {data}")
        return None
    except Exception as e:
        logger.error(f"VPLink API: {e}")
        return None


def create_verification_link(bot_username: str, user_id: int) -> str | None:
    token     = str(int(time.time()))
    db_save_verification_token(user_id, token)
    deep_link = f"https://t.me/{bot_username}?start=verify_{user_id}_{token}"
    logger.info(f"Deep link: {deep_link}")
    return generate_vplink(deep_link)


# ─────────────────────────── VIDEO SENDER ───────────────────────────────────

async def send_video_to_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_id: str,
    index: int,
    total: int,
    random_mode: bool = False,
    prev_message_id: int | None = None,
) -> int | None:
    """
    Sends video with navigation buttons.
    - random_mode=True  → shows 🔀 Next Random button only
    - random_mode=False → shows ⬅ Prev / Next ➡ as needed
    Caption: "Video X of Total"  (or "🔀 Random Mode" when all seen)
    """
    if random_mode:
        nav    = [InlineKeyboardButton("🔀 Next Random", callback_data="nav_random")]
        caption = f"🎬 Video {index + 1} of {total}\n🔀 All videos seen — Random mode!"
    else:
        nav = []
        if index > 0:
            nav.append(InlineKeyboardButton("⬅ Previous", callback_data=f"nav_prev_{index}"))
        if index < total - 1:
            nav.append(InlineKeyboardButton("Next ➡", callback_data=f"nav_next_{index}"))
        caption = f"🎬 Video {index + 1} of {total}"

    markup = InlineKeyboardMarkup([nav]) if nav else None

    # Delete previous video message for clean UI
    if prev_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_message_id)
        except TelegramError:
            pass

    try:
        msg = await context.bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=caption,
            reply_markup=markup,
            protect_content=True,
        )
        context.application.create_task(
            auto_delete_message(context, chat_id, msg.message_id, VIDEO_DELETE_MINUTES * 60)
        )
        return msg.message_id
    except TelegramError as e:
        logger.error(f"send_video: {e}")
        return None


async def auto_delete_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay_seconds: int,
):
    await asyncio.sleep(delay_seconds)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Auto-deleted {message_id}")
    except TelegramError as e:
        logger.warning(f"Auto-delete failed {message_id}: {e}")


# ─────────────────────────── SHOW VIDEO HELPER ──────────────────────────────

async def show_video(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    index: int | None = None,
    prev_message_id: int | None = None,
    force_random: bool = False,
):
    db_reset_daily_if_needed(user_id)
    db_user = db_get_user(user_id)
    if not db_user:
        db_upsert_user(user_id)
        db_user = db_get_user(user_id)

    total = db_get_video_count()
    if total == 0:
        await context.bot.send_message(chat_id, "📭 No videos yet. Check back later!")
        return

    # Determine random mode
    current_random_mode = bool(db_user.get("random_mode", False))

    if force_random or current_random_mode:
        # Stay in random mode permanently once all videos seen
        file_id = db_get_random_video_file_id()
        if index is None:
            index = db_user.get("current_index") or 0
        if not current_random_mode:
            db_set_random_mode(user_id, True)
        random_mode = True
    else:
        if index is None:
            index = db_user.get("current_index") or 0
        index = max(0, min(index, total - 1))

        # Check if user has seen all videos → switch to random mode
        if index >= total - 1 and not (random.random() < REPEAT_CHANCE):
            # They're at the last video, next press will trigger random
            random_mode = False
        else:
            random_mode = False

        file_id = (db_get_random_video_file_id()
                   if random.random() < REPEAT_CHANCE
                   else db_get_video_at_index(index))

    if not file_id:
        await context.bot.send_message(chat_id, "⚠️ Could not load video. Try again.")
        return

    db_set_user_index(user_id, index)
    db_increment_daily(user_id)
    await send_video_to_user(
        context, chat_id, file_id, index, total,
        random_mode=random_mode,
        prev_message_id=prev_message_id
    )


# ─────────────────────────── /start HANDLER ─────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    args    = context.args or []

    db_upsert_user(user.id, user.username)
    db_reset_daily_if_needed(user.id)

    # ── Deep-link verification: start=verify_USERID_TOKEN ──
    if args and args[0].startswith("verify_"):
        raw   = args[0]
        parts = raw.split("_", 2)
        logger.info(f"Verify: raw={raw!r} parts={parts} caller={user.id}")

        if len(parts) == 3:
            _, uid_str, token = parts
            try:
                uid = int(uid_str)
            except ValueError:
                uid = -1

            if uid != user.id:
                await update.message.reply_text(
                    "❌ This link belongs to a different account.\n"
                    "Click Next ➡ to get your own link."
                )
                return

            if db_check_and_use_token(user.id, token):
                hours = db_get_access_hours()
                db_set_access(user.id, hours)

                # Send a temporary success message, then delete it after 3s
                # Then immediately show the video for a clean UX
                success_msg = await update.message.reply_text(
                    f"✅ Access unlocked for {hours} hours!\n"
                    "🎉 Enjoy unlimited videos!"
                )
                # Delete the /start command message too (if possible)
                try:
                    await update.message.delete()
                except TelegramError:
                    pass

                # Show video immediately
                await show_video(context, user.id, chat_id, index=0)

                # Auto-delete success message after 4 seconds
                context.application.create_task(
                    auto_delete_message(context, chat_id, success_msg.message_id, 4)
                )
            else:
                await update.message.reply_text(
                    "❌ Verification failed or link expired.\n\n"
                    "Reasons:\n"
                    "• Link older than 60 minutes\n"
                    "• Link already used\n\n"
                    "Click Next ➡ on any video to get a fresh link."
                )
        else:
            await update.message.reply_text("❌ Invalid verification link.")
        return

    # ── Normal /start ──
    welcome_msg = await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        f"🎬 {FREE_VIDEOS_PER_DAY} free videos/day\n"
        "Use ⬅ / ➡ to browse\n\n"
        "🚀 Loading first video..."
    )
    await show_video(context, user.id, chat_id, index=0)

    # Auto-delete welcome message after 5 seconds for clean UI
    context.application.create_task(
        auto_delete_message(context, chat_id, welcome_msg.message_id, 5)
    )


# ─────────────────────────── NAVIGATION CALLBACKS ───────────────────────────

async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    data    = query.data

    await query.answer()

    db_reset_daily_if_needed(user_id)
    db_user = db_get_user(user_id)
    if not db_user:
        db_upsert_user(user_id)
        db_user = db_get_user(user_id)

    # ── Random mode button ──
    if data == "nav_random":
        if not user_has_active_access(db_user) and not user_within_daily_limit(db_user):
            await _send_access_gate(context, chat_id, user_id)
            return
        await show_video(
            context, user_id, chat_id,
            prev_message_id=query.message.message_id,
            force_random=True,
        )
        return

    # ── Normal prev/next ──
    parts     = data.split("_")   # ['nav', 'next'/'prev', 'INDEX']
    direction = parts[1]
    cur_index = int(parts[2])

    new_index = cur_index + 1 if direction == "next" else cur_index - 1
    total     = db_get_video_count()
    new_index = max(0, min(new_index, total - 1))

    # Access gate (Next only)
    if direction == "next":
        if not user_has_active_access(db_user) and not user_within_daily_limit(db_user):
            await _send_access_gate(context, chat_id, user_id)
            return

    # If going next and already at last video → switch to random mode
    if direction == "next" and cur_index >= total - 1:
        db_set_random_mode(user_id, True)
        await show_video(
            context, user_id, chat_id,
            index=cur_index,
            prev_message_id=query.message.message_id,
            force_random=True,
        )
        return

    await show_video(
        context, user_id, chat_id,
        index=new_index,
        prev_message_id=query.message.message_id,
    )


async def _send_access_gate(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
):
    """Sends the VPLink access gate message."""
    bot_me    = await context.bot.get_me()
    short_url = create_verification_link(bot_me.username, user_id)

    if short_url:
        kb = [[InlineKeyboardButton("🔗 Get Free Access", url=short_url)]]
        await context.bot.send_message(
            chat_id,
            "🔒 Daily limit reached!\n\n"
            "✨ Verify below to unlock unlimited access.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        await context.bot.send_message(
            chat_id,
            "⚠️ Could not generate access link. Try again shortly."
        )


# ─────────────────────────── SOURCE CHANNEL WATCHER ─────────────────────────

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if not message or message.chat_id != SOURCE_CHANNEL_ID:
        return
    if message.video:
        db_save_video(message.video.file_id)
    elif (message.document
          and message.document.mime_type
          and "video" in message.document.mime_type):
        db_save_video(message.document.file_id)


# ─────────────────────────── ADMIN: /status ─────────────────────────────────

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    v24  = db_get_recent_verification_count(24)
    vids = db_get_video_count()
    hrs  = db_get_access_hours()
    await update.message.reply_text(
        f"📊 Bot Status\n\n"
        f"🎬 Total videos: {vids}\n"
        f"✅ Verifications (24h): {v24}\n"
        f"⏱ Access timer: {hrs} hours\n"
        f"🎲 Repeat chance: {int(REPEAT_CHANCE * 100)}%\n"
        f"🗑 Auto-delete: {VIDEO_DELETE_MINUTES} mins"
    )


# ─────────────────────────── ADMIN: /users ──────────────────────────────────

async def users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    try:
        cutoff = utcnow() - timedelta(hours=24)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                total = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM users WHERE joined_at > %s", (cutoff,))
                new24 = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM users WHERE access_until > NOW()")
                active = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM users WHERE random_mode = TRUE")
                rand_users = cur.fetchone()[0]
    except Exception as e:
        await update.message.reply_text(f"❌ DB error: {e}")
        return
    await update.message.reply_text(
        f"👥 User Stats\n\n"
        f"Total users: {total}\n"
        f"New (24h): {new24}\n"
        f"Active access now: {active}\n"
        f"In random mode: {rand_users}"
    )


# ─────────────────────────── ADMIN: /broadcast ──────────────────────────────

async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 Usage: Reply to any message (text/photo/video) with /broadcast"
        )
        return

    source   = update.message.reply_to_message
    user_ids = db_get_all_user_ids()
    sent = failed = 0

    for uid in user_ids:
        try:
            if source.photo:
                await context.bot.send_photo(
                    uid, source.photo[-1].file_id,
                    caption=source.caption or "",
                    parse_mode=ParseMode.HTML
                )
            elif source.video:
                await context.bot.send_video(
                    uid, source.video.file_id,
                    caption=source.caption or "",
                    parse_mode=ParseMode.HTML,
                    protect_content=True
                )
            elif source.text:
                await context.bot.send_message(uid, source.text, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.04)
        except TelegramError:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast done!\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


# ─────────────────────────── ADMIN: /settimer ───────────────────────────────

async def settimer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        hrs = db_get_access_hours()
        await update.message.reply_text(
            f"⏱ Current access timer: {hrs} hours\n\n"
            "Usage: /settimer <hours>\nExample: /settimer 6"
        )
        return
    try:
        hours = int(context.args[0])
        if not (1 <= hours <= 720):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a number between 1 and 720.")
        return
    db_set_access_hours(hours)
    await update.message.reply_text(f"✅ Access timer updated to {hours} hours.")


# ─────────────────────────── /help HANDLER ──────────────────────────────────

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        text = (
            "🛠 Admin Commands\n\n"
            "/status — Bot stats (verifications, videos, timer)\n"
            "/users — User count and active access stats\n"
            "/broadcast — Reply to any message to broadcast it\n"
            "/settimer <hours> — Change verified access duration\n\n"
            "👤 Also available\n"
            "/start — Start / restart video feed\n"
            "/help — This message"
        )
    else:
        text = (
            "ℹ️ How to use this bot\n\n"
            f"1️⃣  /start to begin\n"
            f"2️⃣  {FREE_VIDEOS_PER_DAY} free videos/day\n"
            "3️⃣  ⬅ Prev / Next ➡ to browse\n"
            "4️⃣  After limit → click Get Free Access\n"
            "5️⃣  Verify link → unlock unlimited for 3 hours\n"
            "6️⃣  After all videos seen → 🔀 Random mode\n\n"
            "🔒 Videos are protected — no download/forward."
        )
    await update.message.reply_text(text)


# ─────────────────────────── EXPIRY CHECKER (background) ────────────────────

async def check_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    now          = utcnow()
    window_start = now - timedelta(minutes=5)
    try:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT user_id FROM users
                    WHERE access_until IS NOT NULL
                      AND access_until BETWEEN %s AND %s
                """, (window_start, now))
                expired = cur.fetchall()
    except Exception as e:
        logger.error(f"expiry_job: {e}")
        return

    for row in expired:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "⏰ Your free access has expired!\n\n"
                    "You still get 3 free videos per day.\n"
                    "Click Next ➡ on any video to get a new access link."
                )
            )
        except TelegramError:
            pass


# ─────────────────────────── BOT MENU COMMANDS ──────────────────────────────

async def setup_commands(app: Application):
    user_commands = [
        BotCommand("start", "▶️ Start watching videos"),
        BotCommand("help",  "❓ How to use this bot"),
    ]
    admin_commands = [
        BotCommand("start",     "▶️ Start bot"),
        BotCommand("status",    "📊 Bot stats"),
        BotCommand("users",     "👥 User stats"),
        BotCommand("broadcast", "📢 Broadcast a message"),
        BotCommand("settimer",  "⏱ Set access duration"),
        BotCommand("help",      "❓ Help"),
    ]

    await app.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    try:
        await app.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        )
        logger.info("Admin command menu set.")
    except TelegramError as e:
        logger.warning(f"Admin commands not set (start bot as admin first): {e}")


# ─────────────────────────── MAIN ───────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set!")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_commands)
        .build()
    )

    app.add_handler(CommandHandler("start",     start_handler))
    app.add_handler(CommandHandler("help",      help_handler))
    app.add_handler(CommandHandler("status",    status_handler))
    app.add_handler(CommandHandler("users",     users_handler))
    app.add_handler(CommandHandler("broadcast", broadcast_handler))
    app.add_handler(CommandHandler("settimer",  settimer_handler))

    # Nav callbacks: prev/next AND random button
    app.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav_(next|prev)_\d+$"))
    app.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav_random$"))

    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & (filters.VIDEO | filters.Document.VIDEO),
        channel_post_handler,
    ))

    app.job_queue.run_repeating(check_expiry_job, interval=300, first=60)

    logger.info("Bot started (v2 — pooled connections, random mode, clean verify UI).")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
