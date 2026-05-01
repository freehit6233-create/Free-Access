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


def _as_dicts(cursor) -> list:
    if not cursor.description:
        return []
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _as_dict(cursor) -> dict | None:
    if not cursor.description:
        return None
    cols = [d[0] for d in cursor.description]
    row  = cursor.fetchone()
    return dict(zip(cols, row)) if row else None


def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
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
                user_id        BIGINT PRIMARY KEY,
                videos_watched INT DEFAULT 0,
                access_until   TIMESTAMPTZ,
                verify_token   TEXT,
                token_created  TIMESTAMPTZ,
                last_index     INT DEFAULT 0,
                seen_all       BOOLEAN DEFAULT FALSE
            )
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
            InlineKeyboardButton("⬅️ Previous", callback_data=f"nav_{(index - 1) % total}"),
            InlineKeyboardButton("Next ➡️",     callback_data=f"nav_{(index + 1) % total}"),
        ])

    try:
        await context.bot.send_video(
            chat_id=user_id,
            video=video["file_id"],
            caption=f"🎬 Video {index + 1} / {total}",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            protect_content=True,
        )
        db_update_user(user_id, last_index=index)
    except TelegramError as e:
        logger.error(f"send_video error: {e}")
        await context.bot.send_message(user_id, "❌ Video load nahi ho saki. Dobara try karein.")


async def send_verification_message(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    token = generate_token(user_id)
    db_update_user(user_id, verify_token=token, token_created=datetime.now(timezone.utc))
    vp_url = await build_vp_link(token)

    text = (
        "🔒 *Free limit khatam ho gayi!*\n\n"
        "✅ Neeche diye link ko verify karo aur *3 ghante ki free access* pao.\n\n"
        "📌 *Steps:*\n"
        "1️⃣ 'Get Link' button dabao\n"
        "2️⃣ Jo page khule uspe ad close karke wait karo\n"
        "3️⃣ Verify hone ke baad wapas bot pe aao\n\n"
        "⏳ Access milne ke baad *3 ghante* valid rahega."
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
            )
            db_log_verification(user_id)
            await update.message.reply_text(
                f"✅ *Verification successful!*\n\n"
                f"🎉 Tumhare paas *{ACCESS_HOURS} ghante* ki free access hai. Videos enjoy karo! 🎬",
                parse_mode="Markdown",
            )
            user = db_get_user(user_id)
            await send_video_to_user(context, user_id, user.get("last_index", 0), videos)
        else:
            await update.message.reply_text("❌ Link invalid ya expired. Dobara /start karo.")
        return

    # Normal start
    if not videos:
        await update.message.reply_text(
            "👋 *Bot mein aapka swagat hai!*\n\nAbhi koi video nahi hai. Jaldi aayengi! 🎬",
            parse_mode="Markdown",
        )
        return

    watched = user.get("videos_watched") or 0
    if has_valid_access(user) or watched < FREE_LIMIT:
        await send_video_to_user(context, user_id, user.get("last_index", 0), videos)
    else:
        await send_verification_message(context, user_id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Bot Usage Guide — @RNDAccess\\_bot*\n\n"
        "▶️ /start — Bot shuru karo\n"
        "❓ /help  — Yeh guide\n\n"
        "📌 *Features:*\n"
        "• Pehli *3 videos* bilkul free\n"
        "• Uske baad VP link verify karo → *3 ghante ki access*\n"
        "• ⬅️ Previous / Next ➡️ se navigate karo\n"
        "• Videos forward ya download nahi ho sakti *(protected)*\n"
        "• Saari videos dekh lo to random videos chalti hain\n\n"
        "🔒 Access expire hone par dobara verify karna hoga.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"✅ Last 24h verifications: *{db_verifications_24h()}*\n"
        f"🎬 Total videos in DB: *{db_video_count()}*",
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db_reset_all_access()
    await update.message.reply_text("♻️ Sabhi users ka access reset ho gaya. Sabko fir verify karna hoga.")


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

    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1].file_id
        for uid in all_users:
            try:
                sent_msg = await context.bot.send_photo(chat_id=uid, photo=photo,
                                                        caption=caption or None, parse_mode="Markdown")
                db_save_broadcast(uid, sent_msg.message_id, delete_at)
                sent += 1
            except TelegramError:
                fail += 1
    else:
        text = caption or message.text.replace("/broadcast", "").strip()
        if not text:
            await message.reply_text("❌ Usage:\n• `/broadcast message`\n• Photo reply ke saath `/broadcast caption`",
                                     parse_mode="Markdown")
            return
        for uid in all_users:
            try:
                sent_msg = await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
                db_save_broadcast(uid, sent_msg.message_id, delete_at)
                sent += 1
            except TelegramError:
                fail += 1

    await message.reply_text(
        f"📢 *Broadcast bheja!*\n✅ Sent: *{sent}*\n❌ Failed: *{fail}*\n"
        f"🗑️ Auto-delete: *{BROADCAST_HOURS} ghante baad*",
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

    # Random mode after all videos seen
    seen_all = user.get("seen_all") or False
    if not seen_all and requested_index >= len(videos):
        db_update_user(user_id, seen_all=True)
        seen_all = True
    if seen_all:
        requested_index = random.randint(0, len(videos) - 1)

    await send_video_to_user(context, user_id, requested_index, videos)


# ─────────────────────────────────────────────
# CHANNEL POST — Auto-fetch
# ─────────────────────────────────────────────

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or message.chat.id != CHANNEL_ID:
        return
    if message.video:
        db_save_video(message.video.file_id, message.message_id)
        logger.info(f"Video saved from channel: {message.video.file_id}")


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

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_delete_broadcasts, "interval", minutes=10, args=[app])
    scheduler.start()

    logger.info(f"@{BOT_USERNAME} starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
