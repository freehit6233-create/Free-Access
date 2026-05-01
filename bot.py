"""
Advanced Telegram Video Bot — @RNDAccess_bot
- pg8000 (pure Python PostgreSQL — no libpq needed, works on Railway)
- Private channel se auto-fetch videos
- 3 free videos → VP Link verification → 3 hours access
- protect_content=True (no download/forward)
- Admin: /status, /reset, /broadcast (12hr auto-delete)
- APScheduler for timed tasks
"""

import os
import logging
import random
import hashlib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import pg8000.dbapi
import aiohttp
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
CHANNEL_ID   = int(os.getenv("CHANNEL_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
VP_API_KEY   = os.getenv("VP_API_KEY", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "RNDAccess_bot")

FREE_LIMIT      = 3
ACCESS_HOURS    = 3
BROADCAST_HOURS = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# DATABASE — pg8000 (pure Python, no libpq)
# ─────────────────────────────────────────────

def get_conn():
    """
    pg8000 pure Python connection.
    No system libraries needed — works on any Railway container.
    Parses Neon DATABASE_URL automatically.
    """
    r = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.dbapi.connect(
        host=r.hostname,
        port=r.port or 5432,
        database=r.path.lstrip("/"),
        user=r.username,
        password=r.password,
        ssl_context=True,
    )


def _col_name(d) -> str:
    """pg8000 column names kabhi kabhi bytes hote hain — safely decode karo."""
    name = d[0]
    return name.decode() if isinstance(name, bytes) else str(name)


def _as_dicts(cursor) -> list:
    if not cursor.description:
        return []
    cols = [_col_name(d) for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _as_dict(cursor) -> dict | None:
    if not cursor.description:
        return None
    cols = [_col_name(d) for d in cursor.description]
    row  = cursor.fetchone()
    return dict(zip(cols, row)) if row else None


def init_db():
    """
    Tables create karo. pg8000 ke saath column names lowercase
    quoted identifiers se define karte hain taaki case-sensitivity issue na ho.
    Agar purani broken table hai (psycopg2 se), usse DROP karke recreate karo.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        # Check karo videos table mein file_id column hai ya nahi
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'videos' AND column_name = 'file_id'
        """)
        has_file_id = cur.fetchone()
        if not has_file_id:
            # Purani broken table drop karo
            logger.warning("videos table missing file_id — recreating all tables.")
            cur.execute("DROP TABLE IF EXISTS broadcasts CASCADE")
            cur.execute("DROP TABLE IF EXISTS verifications CASCADE")
            cur.execute("DROP TABLE IF EXISTS videos CASCADE")
            cur.execute("DROP TABLE IF EXISTS users CASCADE")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id         SERIAL PRIMARY KEY,
                file_id    TEXT UNIQUE NOT NULL,
                message_id BIGINT,
                added_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id             BIGINT PRIMARY KEY,
                videos_watched      INT DEFAULT 0,
                access_until        TIMESTAMPTZ,
                verify_token        TEXT,
                token_created       TIMESTAMPTZ,
                last_index          INT DEFAULT 0,
                seen_all            BOOLEAN DEFAULT FALSE,
                last_video_msg_id   BIGINT,
                last_video_sent_at  TIMESTAMPTZ,
                expiry_notified     BOOLEAN DEFAULT FALSE
            )
        """)
        # Existing DB mein columns add karo agar nahi hain
        cur.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS last_video_msg_id  BIGINT,
            ADD COLUMN IF NOT EXISTS last_video_sent_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS expiry_notified    BOOLEAN DEFAULT FALSE
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                verified_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id         SERIAL PRIMARY KEY,
                chat_id    BIGINT,
                message_id BIGINT,
                delete_at  TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        logger.info("Database initialised.")
    finally:
        conn.close()


# ── Video ─────────────────────────────────────

def db_save_video(file_id: str, message_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO videos (file_id, message_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (file_id, message_id),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_videos() -> list:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM videos ORDER BY id")
        return _as_dicts(cur)
    finally:
        conn.close()


def db_video_count() -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM videos")
        return cur.fetchone()[0]
    finally:
        conn.close()


# ── User ──────────────────────────────────────

def db_get_user(user_id: int) -> dict | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        return _as_dict(cur)
    finally:
        conn.close()


def db_upsert_user(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def db_update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k} = %s" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE users SET {sets} WHERE user_id = %s", vals)
        conn.commit()
    finally:
        conn.close()


def db_reset_all_access():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET access_until = NULL, videos_watched = 0")
        conn.commit()
    finally:
        conn.close()


def db_all_user_ids() -> list:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# ── Verification ──────────────────────────────

def db_log_verification(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO verifications (user_id) VALUES (%s)", (user_id,))
        conn.commit()
    finally:
        conn.close()


def db_verifications_24h() -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM verifications WHERE verified_at > NOW() - INTERVAL '24 hours'"
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


# ── Broadcast ─────────────────────────────────

def db_save_broadcast(chat_id: int, message_id: int, delete_at: datetime):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO broadcasts (chat_id, message_id, delete_at) VALUES (%s, %s, %s)",
            (chat_id, message_id, delete_at),
        )
        conn.commit()
    finally:
        conn.close()


def db_due_broadcasts() -> list:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM broadcasts WHERE delete_at <= NOW()")
        return _as_dicts(cur)
    finally:
        conn.close()


def db_delete_broadcast_record(bid: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM broadcasts WHERE id = %s", (bid,))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────
# VERIFICATION TOKEN & VP LINK
# ─────────────────────────────────────────────

def generate_token(user_id: int) -> str:
    raw = f"{user_id}:{time.time()}:{VP_API_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def build_vp_link(token: str) -> str:
    """
    VPLink API — exact format:
    GET https://vplink.in/api?api=KEY&url=DEST&alias=ALIAS&format=text
    format=text → response is plain short URL string.
    """
    destination  = f"https://t.me/{BOT_USERNAME}?start=verify_{token}"
    encoded_dest = urllib.parse.quote(destination, safe="")
    alias        = f"v{token[:8]}"

    api_url = (
        f"https://vplink.in/api"
        f"?api={VP_API_KEY}"
        f"&url={encoded_dest}"
        f"&alias={alias}"
        f"&format=text"
    )

    try:
        async with aiohttp.ClientSession() as session:
            # Attempt 1: with custom alias
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                text = (await r.text()).strip()
                if text.startswith("http"):
                    logger.info(f"VPLink created: {text}")
                    return text
                logger.warning(f"VPLink alias attempt: {text} — retrying without alias")

            # Attempt 2: without alias (auto-generated)
            retry = (
                f"https://vplink.in/api"
                f"?api={VP_API_KEY}"
                f"&url={encoded_dest}"
                f"&format=text"
            )
            async with session.get(retry, timeout=aiohttp.ClientTimeout(total=10)) as r2:
                text2 = (await r2.text()).strip()
                if text2.startswith("http"):
                    logger.info(f"VPLink created (no alias): {text2}")
                    return text2
                logger.error(f"VPLink both attempts failed: {text2}")

    except Exception as e:
        logger.error(f"VPLink exception: {e}")

    # Fallback: direct Telegram deep-link
    logger.warning("VPLink failed — using direct deep-link.")
    return destination


def has_valid_access(user: dict) -> bool:
    if not user or not user.get("access_until"):
        return False
    now          = datetime.now(timezone.utc)
    access_until = user["access_until"]
    if access_until.tzinfo is None:
        access_until = access_until.replace(tzinfo=timezone.utc)
    return now < access_until


async def delete_last_video(context: ContextTypes.DEFAULT_TYPE, user: dict):
    """User ka pichla video message delete karo agar hai to."""
    msg_id = user.get("last_video_msg_id") if user else None
    if not msg_id:
        return
    try:
        await context.bot.delete_message(chat_id=user["user_id"], message_id=msg_id)
    except TelegramError:
        pass  # Already deleted ya unavailable — ignore
    db_update_user(user["user_id"], last_video_msg_id=None, last_video_sent_at=None)


# ─────────────────────────────────────────────
# VIDEO SENDING
# ─────────────────────────────────────────────

async def send_video_to_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    index: int,
    videos: list,
):
    total = len(videos)
    if total == 0:
        await context.bot.send_message(user_id, "⚠️ Abhi koi video available nahi hai.")
        return

    index = index % total
    video = videos[index]
    buttons = []
    if total > 1:
        buttons.append([
            InlineKeyboardButton("⬅️ Prev", callback_data=f"nav_{(index - 1) % total}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"nav_{(index + 1) % total}"),
        ])

    # Pichla video delete karo
    user = db_get_user(user_id)
    await delete_last_video(context, user)

    try:
        sent = await context.bot.send_video(
            chat_id=user_id,
            video=video["file_id"],
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            protect_content=True,
        )
        db_update_user(
            user_id,
            last_index=index,
            last_video_msg_id=sent.message_id,
            last_video_sent_at=datetime.now(timezone.utc),
        )
    except TelegramError as e:
        logger.error(f"send_video error: {e}")
        await context.bot.send_message(user_id, "❌ Video load nahi hui। Dobara try karo।")


async def send_verification_message(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    token = generate_token(user_id)
    db_update_user(user_id, verify_token=token, token_created=datetime.now(timezone.utc))
    vp_url = await build_vp_link(token)

    text = (
        "🔒 *Free limit khatam!*\n\n"
        "Neeche diye link pe verify karo aur *3 ghante ki access* pao।\n\n"
        "📌 *Steps:*\n"
        "1️⃣ *Get Link* dabao\n"
        "2️⃣ Page pe ad band karo aur thoda wait karo\n"
        "3️⃣ Verify hone ke baad wapas bot pe aao\n\n"
        "⏳ Access *3 ghante* valid rahegi।"
    )
    await context.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Get Link", url=vp_url)]]),
    )


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_upsert_user(user_id)
    user   = db_get_user(user_id)

    # Pichla video/messages delete karo
    await delete_last_video(context, user)
    videos = db_get_videos()
    args   = context.args

    # Deep-link verification
    if args and args[0].startswith("verify_"):
        token_from_link = args[0][len("verify_"):]
        stored_token  = user.get("verify_token")  if user else None
        token_created = user.get("token_created") if user else None

        valid = False
        if stored_token and token_from_link == stored_token and token_created:
            if token_created.tzinfo is None:
                token_created = token_created.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - token_created).total_seconds() < 1800:
                valid = True

        if valid:
            db_update_user(
                user_id,
                access_until=datetime.now(timezone.utc) + timedelta(hours=ACCESS_HOURS),
                verify_token=None,
                token_created=None,
                videos_watched=0,
                expiry_notified=False,
            )
            db_log_verification(user_id)

            # Admin ko notify karo
            tg_user = update.effective_user
            name = tg_user.full_name or tg_user.first_name or "Unknown"
            username_part = f"@{tg_user.username}" if tg_user.username else "—"
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"✅ *New Verification*\n\n"
                        f"👤 *Name:* {name}\n"
                        f"🔗 *Username:* {username_part}\n"
                        f"🆔 *User ID:* `{user_id}`"
                    ),
                    parse_mode="Markdown",
                )
            except TelegramError as e:
                logger.warning(f"Admin notify error: {e}")

            await update.message.reply_text(
                f"✅ *Verified!*\n\n"
                f"🎉 *{ACCESS_HOURS} ghante* ki access mil gayi — enjoy karo! 🎬",
                parse_mode="Markdown",
            )
            user = db_get_user(user_id)
            await send_video_to_user(context, user_id, user.get("last_index", 0), videos)
        else:
            await update.message.reply_text("❌ Link invalid ya expire ho gaya। Dobara /start karo।")
        return

    # Normal start
    if not videos:
        await update.message.reply_text(
            "👋 *Swagat hai!*\n\nAbhi koi video available nahi hai. Jaldi aayengi! 🎬",
            parse_mode="Markdown",
        )
        return

    watched = user.get("videos_watched") or 0
    if has_valid_access(user) or watched < FREE_LIMIT:
        await send_video_to_user(context, user_id, user.get("last_index", 0), videos)
    else:
        await send_verification_message(context, user_id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db_get_user(user_id)
    await delete_last_video(context, user)
    await update.message.reply_text(
        "📖 *Help — @RNDAccess\\_bot*\n\n"
        "▶️ /start — Videos dekhna shuru karo\n"
        "❓ /help  — Yeh guide\n\n"
        "🎬 *Kaise kaam karta hai:*\n"
        "• Pehli *3 videos* free hain\n"
        "• Free limit ke baad ek link verify karo\n"
        "• Verify hone par *3 ghante* ki full access milti hai\n"
        "• ⬅️ Prev / Next ➡️ se navigate karo\n\n"
        "🔒 Access expire hone par dobara verify karo.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"✅ Verifications (24h): *{db_verifications_24h()}*\n"
        f"🎬 Total videos: *{db_video_count()}*",
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db_reset_all_access()
    await update.message.reply_text("♻️ Sabhi users ka access reset ho gaya।")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    message   = update.message
    all_users = db_all_user_ids()
    if not all_users:
        await message.reply_text("Koi user nahi mila.")
        return

    delete_at  = datetime.now(timezone.utc) + timedelta(hours=BROADCAST_HOURS)
    sent, fail = 0, 0
    caption    = " ".join(context.args) if context.args else ""

    if message.reply_to_message:
        # Format preserve karo — copy_message use karo
        src_msg = message.reply_to_message
        for uid in all_users:
            try:
                sent_msg = await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=src_msg.chat_id,
                    message_id=src_msg.message_id,
                )
                db_save_broadcast(uid, sent_msg.message_id, delete_at)
                sent += 1
            except TelegramError:
                fail += 1
    else:
        text = message.text.replace("/broadcast", "").strip()
        if not text:
            await message.reply_text(
                "❌ *Usage:*\n• `/broadcast message`\n• Kisi bhi message ko reply karke `/broadcast`",
                parse_mode="Markdown",
            )
            return
        for uid in all_users:
            try:
                sent_msg = await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
                db_save_broadcast(uid, sent_msg.message_id, delete_at)
                sent += 1
            except TelegramError:
                fail += 1

    await message.reply_text(
        f"📢 *Broadcast Done!*\n✅ Sent: *{sent}*\n❌ Failed: *{fail}*\n"
        f"🗑️ Auto-delete: *{BROADCAST_HOURS}h baad*",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────
# NAVIGATION CALLBACK
# ─────────────────────────────────────────────

async def callback_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db_upsert_user(user_id)
    user   = db_get_user(user_id)
    videos = db_get_videos()

    if not videos:
        await query.message.reply_text("⚠️ Koi video nahi mili.")
        return

    try:
        requested_index = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        return

    watched = user.get("videos_watched") or 0

    if not has_valid_access(user):
        if watched >= FREE_LIMIT:
            await send_verification_message(context, user_id)
            return
        db_update_user(user_id, videos_watched=watched + 1)
        user = db_get_user(user_id)

    total = len(videos)
    seen_all = user.get("seen_all") or False
    last_index = user.get("last_index") or 0

    # Jab Next dabane par loop complete ho (last video se index 0 par aaye)
    if not seen_all and last_index == total - 1 and requested_index == 0:
        db_update_user(user_id, seen_all=True)
        seen_all = True

    if seen_all:
        requested_index = random.randint(0, total - 1)

    await send_video_to_user(context, user_id, requested_index, videos)


# ─────────────────────────────────────────────
# CHANNEL POST — Auto-fetch
# ─────────────────────────────────────────────

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or message.chat.id != CHANNEL_ID:
        return
    if not message.video:
        return

    db_save_video(message.video.file_id, message.message_id)
    logger.info(f"New video saved from channel: {message.video.file_id}")

    # Sabhi active users ko naya video turant bhejo
    videos = db_get_videos()
    new_index = len(videos) - 1  # Latest video
    now = datetime.now(timezone.utc)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM users WHERE access_until IS NOT NULL AND access_until > %s",
            (now,),
        )
        active_users = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    sent_count = 0
    for uid in active_users:
        try:
            await send_video_to_user(context, uid, new_index, videos)
            sent_count += 1
        except TelegramError as e:
            logger.warning(f"New video notify uid={uid}: {e}")

    # seen_all reset karo — naya content hai ab
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET seen_all = FALSE WHERE seen_all = TRUE")
        conn.commit()
    finally:
        conn.close()

    logger.info(f"New video pushed to {sent_count} active user(s).")


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

async def job_delete_broadcasts(app):
    due = db_due_broadcasts()
    for row in due:
        try:
            await app.bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
        except TelegramError as e:
            logger.warning(f"Delete broadcast error: {e}")
        db_delete_broadcast_record(row["id"])
    if due:
        logger.info(f"Deleted {len(due)} expired broadcast(s).")


async def job_auto_delete_videos(app):
    """10 min se purane video messages auto-delete karo."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, last_video_msg_id FROM users "
            "WHERE last_video_msg_id IS NOT NULL AND last_video_sent_at <= %s",
            (cutoff,),
        )
        rows = _as_dicts(cur)
    finally:
        conn.close()

    for row in rows:
        try:
            await app.bot.delete_message(chat_id=row["user_id"], message_id=row["last_video_msg_id"])
        except TelegramError:
            pass
        db_update_user(row["user_id"], last_video_msg_id=None, last_video_sent_at=None)

    if rows:
        logger.info(f"Auto-deleted {len(rows)} expired video(s).")


async def job_notify_expired_access(app):
    """Access expire ho chuke users ko ek baar notification bhejo."""
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM users "
            "WHERE access_until IS NOT NULL "
            "  AND access_until <= %s "
            "  AND (expiry_notified IS NULL OR expiry_notified = FALSE)",
            (now,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for (uid,) in rows:
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=(
                    "⏰ *Access expire ho gaya!*\n\n"
                    "Dobara videos dekhne ke liye /start karo aur link verify karo। 🔗"
                ),
                parse_mode="Markdown",
            )
        except TelegramError as e:
            logger.warning(f"Expiry notify error uid={uid}: {e}")
        db_update_user(uid, expiry_notified=True)

    if rows:
        logger.info(f"Notified {len(rows)} expired user(s).")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(callback_nav, pattern=r"^nav_\d+$"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))

    async def post_init(application):
        from telegram import BotCommand
        from telegram.constants import BotCommandScopeType

        # Default (sabhi users) — sirf start aur help
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Videos dekhna shuru karo"),
                BotCommand("help",  "Help guide"),
            ]
        )

        # Admin ke liye — sab commands
        await application.bot.set_my_commands(
            [
                BotCommand("start",     "Videos dekhna shuru karo"),
                BotCommand("help",      "Help guide"),
                BotCommand("status",    "Bot stats dekho"),
                BotCommand("broadcast", "Sabko message bhejo"),
                BotCommand("reset",     "Sabka access reset karo"),
            ],
            scope={"type": "chat", "chat_id": ADMIN_ID},
        )

    app.post_init = post_init

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_delete_broadcasts,      "interval", minutes=10, args=[app])
    scheduler.add_job(job_auto_delete_videos,     "interval", minutes=2,  args=[app])
    scheduler.add_job(job_notify_expired_access,  "interval", minutes=5,  args=[app])
    scheduler.start()

    logger.info(f"@{BOT_USERNAME} starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
