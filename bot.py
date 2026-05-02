import os
import asyncio
import logging
import random
import hashlib
import urllib.parse
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Env Vars ─────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
CHANNEL_ID   = int(os.environ["CHANNEL_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
VP_TOKEN     = os.environ["VP_LINK_TOKEN"]          # vplink API key
BOT_USERNAME = os.environ.get("BOT_USERNAME", "RNDAccess_bot").lstrip("@")

FREE_VIDEOS        = 3          # videos free before gate
ACCESS_HOURS       = 3          # hours granted after verify
FREE_RESET_HOURS   = 24         # hours before free-window resets
AUTO_DELETE_VIDEO  = 10 * 60    # 10 min  (seconds)
AUTO_DELETE_CMD    = 10         # 10 sec
BROADCAST_TTL      = 12 * 60 * 60  # 12 h
RANDOM_INJECT_PCT  = 0.02       # 2 % random inject probability

# ── Database ──────────────────────────────────────────────────────────────────
pool: asyncpg.Pool = None   # type: ignore

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          BIGINT PRIMARY KEY,
    current_index    INT     NOT NULL DEFAULT 0,
    free_start_ts    TIMESTAMPTZ,          -- when 3-free window began
    access_until     TIMESTAMPTZ,          -- paid/verified access expiry
    last_verify_msg  BIGINT,               -- message_id of pending verify msg
    is_banned        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS verifications (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broadcast_msgs (
    id          SERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    delete_at   TIMESTAMPTZ NOT NULL
);
"""

async def init_db():
    global pool
    # Neon requires SSL
    pool = await asyncpg.create_pool(DATABASE_URL, ssl="require", min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
    logger.info("DB pool ready")

async def get_user(conn, user_id: int) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if row is None:
        await conn.execute(
            "INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id
        )
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return row

# ── Bot / Dispatcher ──────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_token(user_id: int) -> str:
    """Deterministic per-user token embedded in verify link."""
    raw = f"{user_id}:{VP_TOKEN}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def make_verify_url(user_id: int) -> str:
    """
    Build VP-shortened URL that redirects back to the bot with a start payload.
    """
    token   = make_token(user_id)
    payload = f"verify_{user_id}_{token}"
    dest    = f"https://t.me/{BOT_USERNAME}?start={payload}"
    encoded = urllib.parse.quote(dest, safe="")
    alias   = f"verify{user_id}"
    return (
        f"https://vplink.in/api"
        f"?api={VP_TOKEN}"
        f"&url={encoded}"
        f"&alias={alias}"
    )

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

async def fetch_channel_videos(bot: Bot) -> list[int]:
    """
    Walk the channel and return a list of message IDs that contain video/document.
    Telegram doesn't expose a direct "list all messages" API, so we use
    forward_messages trick with a large range.  In production you should store
    IDs in DB incrementally; here we cache in-memory for simplicity.
    """
    # We'll do a smart probe: start from msg_id=1 and step forward
    # until we hit consecutive misses. Cap at 5000 for speed.
    found = []
    step  = 1
    empty = 0
    MAX_EMPTY = 50   # stop after 50 consecutive misses
    MAX_ID    = 5000

    for mid in range(1, MAX_ID + 1):
        try:
            msgs = await bot.forward_messages(
                chat_id=ADMIN_ID,
                from_chat_id=CHANNEL_ID,
                message_ids=[mid],
            )
            if msgs:
                msg = msgs[0]
                if msg.video or (msg.document and msg.document.mime_type and "video" in msg.document.mime_type):
                    found.append(mid)
                # delete the forwarded probe copy
                await bot.delete_message(ADMIN_ID, msg.message_id)
            empty = 0
        except TelegramBadRequest:
            empty += 1
            if empty >= MAX_EMPTY:
                break
        except Exception as e:
            logger.warning(f"probe error at {mid}: {e}")
            empty += 1
            if empty >= MAX_EMPTY:
                break
        await asyncio.sleep(0.05)

    logger.info(f"Channel scan found {len(found)} video messages")
    return found

# Simple in-memory cache refreshed every 30 min
_video_cache: list[int] = []
_cache_time: datetime   = datetime(2000, 1, 1, tzinfo=timezone.utc)

async def get_video_ids() -> list[int]:
    global _video_cache, _cache_time
    if (now_utc() - _cache_time).total_seconds() > 1800 or not _video_cache:
        _video_cache = await fetch_channel_videos(bot)
        _cache_time  = now_utc()
    return _video_cache

async def has_access(conn, user_id: int) -> bool:
    row = await get_user(conn, user_id)
    if row["is_banned"]:
        return False
    if row["access_until"] and row["access_until"] > now_utc():
        return True
    # Check free window
    if row["free_start_ts"]:
        elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
        if elapsed < FREE_RESET_HOURS * 3600 and row["current_index"] < FREE_VIDEOS:
            return True
    elif row["current_index"] < FREE_VIDEOS:
        return True
    return False

async def send_video_at_index(
    bot: Bot,
    user_id: int,
    index: int,
    video_ids: list[int],
) -> int | None:
    """Copy video from channel to user. Returns sent message_id or None."""
    if not video_ids:
        return None

    # Possibly inject a random video (2 %)
    if len(video_ids) > 1 and random.random() < RANDOM_INJECT_PCT:
        msg_id = random.choice(video_ids)
    else:
        msg_id = video_ids[index % len(video_ids)]

    try:
        sent = await bot.copy_message(
            chat_id=user_id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id,
            protect_content=True,
        )
        return sent.message_id
    except TelegramBadRequest as e:
        logger.warning(f"copy_message failed for uid={user_id} mid={msg_id}: {e}")
        return None

def nav_keyboard(index: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Previous", callback_data=f"nav:prev:{index}")
    builder.button(text="Next ➡️",     callback_data=f"nav:next:{index}")
    builder.adjust(2)
    return builder.as_markup()

def verify_keyboard(user_id: int) -> InlineKeyboardMarkup:
    url = make_verify_url(user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Get Link", url=url)
    builder.adjust(1)
    return builder.as_markup()

async def show_verify_gate(bot: Bot, user_id: int, conn):
    """Delete old verify msg (if any) and send a fresh one."""
    row = await get_user(conn, user_id)
    if row["last_verify_msg"]:
        try:
            await bot.delete_message(user_id, row["last_verify_msg"])
        except Exception:
            pass

    sent = await bot.send_message(
        user_id,
        "🔒 *Access Required*\n\nVerify this link to get *3 hours free access*:",
        parse_mode="Markdown",
        reply_markup=verify_keyboard(user_id),
    )
    await conn.execute(
        "UPDATE users SET last_verify_msg=$1 WHERE user_id=$2",
        sent.message_id, user_id,
    )

async def delete_msg_after(bot: Bot, chat_id: int, msg_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

# ── /start handler ─────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    text    = message.text or ""
    parts   = text.split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""

    async with pool.acquire() as conn:
        row = await get_user(conn, user_id)

        # ── Verify callback via deep-link ──────────────────────────────────
        if payload.startswith("verify_"):
            segs = payload.split("_")
            if len(segs) == 3:
                _, uid_str, tok = segs
                if int(uid_str) == user_id and tok == make_token(user_id):
                    expiry = now_utc() + timedelta(hours=ACCESS_HOURS)
                    await conn.execute(
                        "UPDATE users SET access_until=$1, last_verify_msg=NULL WHERE user_id=$2",
                        expiry, user_id,
                    )
                    await conn.execute(
                        "INSERT INTO verifications(user_id) VALUES($1)", user_id
                    )
                    # Delete old verify message
                    if row["last_verify_msg"]:
                        try:
                            await bot.delete_message(user_id, row["last_verify_msg"])
                        except Exception:
                            pass

                    sent = await message.answer(
                        f"✅ *Access granted!* You have *{ACCESS_HOURS} hours* of free access.\n"
                        f"Enjoy the videos! 🎬",
                        parse_mode="Markdown",
                    )
                    asyncio.create_task(delete_msg_after(bot, user_id, sent.message_id, AUTO_DELETE_CMD))

                    # Notify admin
                    name = message.from_user.full_name
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"✅ *New Verification*\n👤 {name}\n🆔 `{user_id}`",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

                    # Show current video
                    video_ids = await get_video_ids()
                    if video_ids:
                        idx = row["current_index"]
                        vid_msg = await send_video_at_index(bot, user_id, idx, video_ids)
                        if vid_msg:
                            await bot.send_message(
                                user_id,
                                f"Video {idx + 1} / {len(video_ids)}",
                                reply_markup=nav_keyboard(idx),
                            )
                            asyncio.create_task(delete_msg_after(bot, user_id, vid_msg, AUTO_DELETE_VIDEO))
                    return

        # ── Normal /start ──────────────────────────────────────────────────
        video_ids = await get_video_ids()
        if not video_ids:
            await message.answer("⚠️ No videos found in the channel yet.")
            return

        # Initialize free window timestamp on first use
        if row["free_start_ts"] is None:
            await conn.execute(
                "UPDATE users SET free_start_ts=$1 WHERE user_id=$2",
                now_utc(), user_id,
            )

        idx = row["current_index"]
        access = await has_access(conn, user_id)

        if not access:
            await show_verify_gate(bot, user_id, conn)
            return

        vid_msg = await send_video_at_index(bot, user_id, idx, video_ids)
        if vid_msg:
            nav = await message.answer(
                f"Video {idx + 1} / {len(video_ids)}",
                reply_markup=nav_keyboard(idx),
            )
            asyncio.create_task(delete_msg_after(bot, user_id, vid_msg, AUTO_DELETE_VIDEO))
            asyncio.create_task(delete_msg_after(bot, user_id, nav.message_id, AUTO_DELETE_VIDEO))

    # Auto-delete /start command message
    try:
        await message.delete()
    except Exception:
        pass

# ── Navigation callbacks ──────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _, direction, idx_str = callback.data.split(":")
    idx = int(idx_str)

    async with pool.acquire() as conn:
        row = await get_user(conn, user_id)

        # Compute new index
        video_ids = await get_video_ids()
        if not video_ids:
            await callback.answer("No videos available.", show_alert=True)
            return

        if direction == "next":
            new_idx = (idx + 1) % len(video_ids)
        else:
            new_idx = (idx - 1) % len(video_ids)

        # Check 24h reset: if free window expired and no active access, reset index to 0
        if row["free_start_ts"]:
            elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
            if elapsed >= FREE_RESET_HOURS * 3600 and (
                not row["access_until"] or row["access_until"] <= now_utc()
            ):
                # Reset free window
                await conn.execute(
                    "UPDATE users SET free_start_ts=$1, current_index=0 WHERE user_id=$2",
                    now_utc(), user_id,
                )
                new_idx = 0

        # Gate check
        access = await has_access(conn, user_id)
        if not access and new_idx >= FREE_VIDEOS:
            await callback.answer()
            await show_verify_gate(bot, user_id, conn)
            return

        # Save index
        await conn.execute(
            "UPDATE users SET current_index=$1 WHERE user_id=$2", new_idx, user_id
        )

    # Delete previous nav message
    try:
        await callback.message.delete()
    except Exception:
        pass

    vid_msg = await send_video_at_index(bot, user_id, new_idx, video_ids)
    if vid_msg:
        nav = await bot.send_message(
            user_id,
            f"Video {new_idx + 1} / {len(video_ids)}",
            reply_markup=nav_keyboard(new_idx),
        )
        asyncio.create_task(delete_msg_after(bot, user_id, vid_msg, AUTO_DELETE_VIDEO))
        asyncio.create_task(delete_msg_after(bot, user_id, nav.message_id, AUTO_DELETE_VIDEO))

    await callback.answer()

# ── /help ─────────────────────────────────────────────────────────────────────
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 *Help*\n\n"
        "/start — Watch videos\n"
        "/help  — This message\n\n"
        "_First 3 videos are free. Verify a link for 3 hours full access._"
    )
    sent = await message.answer(text, parse_mode="Markdown")
    asyncio.create_task(delete_msg_after(bot, message.chat.id, sent.message_id, 30))
    try:
        await message.delete()
    except Exception:
        pass

# ── /status (admin) ───────────────────────────────────────────────────────────
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    async with pool.acquire() as conn:
        since = now_utc() - timedelta(hours=24)
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= $1", since
        )
    video_ids = await get_video_ids()
    sent = await message.answer(
        f"📊 *Bot Status*\n\n"
        f"✅ Verifications (last 24h): `{count}`\n"
        f"📹 Total Videos: `{len(video_ids)}`",
        parse_mode="Markdown",
    )
    asyncio.create_task(delete_msg_after(bot, message.chat.id, sent.message_id, 30))
    try:
        await message.delete()
    except Exception:
        pass

# ── /reset (admin) ────────────────────────────────────────────────────────────
@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET access_until=NULL, free_start_ts=NULL, current_index=0"
        )
    sent = await message.answer("♻️ All users' verification status has been reset.")
    asyncio.create_task(delete_msg_after(bot, message.chat.id, sent.message_id, AUTO_DELETE_CMD))
    try:
        await message.delete()
    except Exception:
        pass

# ── /broadcast (admin) ───────────────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    reply = message.reply_to_message
    if not reply:
        sent = await message.answer("↩️ Reply to a message with /broadcast to broadcast it.")
        asyncio.create_task(delete_msg_after(bot, message.chat.id, sent.message_id, AUTO_DELETE_CMD))
        try:
            await message.delete()
        except Exception:
            pass
        return

    async with pool.acquire() as conn:
        user_ids = await conn.fetch("SELECT user_id FROM users WHERE is_banned=FALSE")

    delete_at = now_utc() + timedelta(seconds=BROADCAST_TTL)
    success = fail = 0

    async with pool.acquire() as conn:
        for record in user_ids:
            uid = record["user_id"]
            try:
                sent_msg = await reply.copy_to(uid)
                await conn.execute(
                    "INSERT INTO broadcast_msgs(chat_id, message_id, delete_at) VALUES($1,$2,$3)",
                    uid, sent_msg.message_id, delete_at,
                )
                success += 1
            except TelegramForbiddenError:
                fail += 1
            except Exception as e:
                logger.warning(f"broadcast fail uid={uid}: {e}")
                fail += 1
            await asyncio.sleep(0.05)

    sent = await message.answer(
        f"📣 Broadcast done!\n✅ Sent: {success}\n❌ Failed: {fail}"
    )
    asyncio.create_task(delete_msg_after(bot, message.chat.id, sent.message_id, 30))
    try:
        await message.delete()
    except Exception:
        pass

# ── Background tasks ──────────────────────────────────────────────────────────

async def task_expire_access():
    """Notify users when their 3-hour access expires."""
    while True:
        try:
            async with pool.acquire() as conn:
                expired = await conn.fetch(
                    "SELECT user_id FROM users WHERE access_until IS NOT NULL AND access_until <= $1",
                    now_utc(),
                )
                for row in expired:
                    uid = row["user_id"]
                    try:
                        await bot.send_message(
                            uid,
                            "⏰ *Your access has expired.*\n"
                            "Verify again to get another 3 hours free! /start",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    await conn.execute(
                        "UPDATE users SET access_until=NULL WHERE user_id=$1", uid
                    )
        except Exception as e:
            logger.error(f"task_expire_access error: {e}")
        await asyncio.sleep(60)

async def task_delete_broadcasts():
    """Auto-delete broadcast messages after 12 hours."""
    while True:
        try:
            async with pool.acquire() as conn:
                due = await conn.fetch(
                    "SELECT id, chat_id, message_id FROM broadcast_msgs WHERE delete_at <= $1",
                    now_utc(),
                )
                for row in due:
                    try:
                        await bot.delete_message(row["chat_id"], row["message_id"])
                    except Exception:
                        pass
                    await conn.execute("DELETE FROM broadcast_msgs WHERE id=$1", row["id"])
        except Exception as e:
            logger.error(f"task_delete_broadcasts error: {e}")
        await asyncio.sleep(60)

async def task_refresh_cache():
    """Refresh video cache every 30 minutes."""
    while True:
        await asyncio.sleep(1800)
        try:
            await get_video_ids()
        except Exception as e:
            logger.error(f"task_refresh_cache error: {e}")

# ── Startup / shutdown ────────────────────────────────────────────────────────
async def on_startup():
    await init_db()
    asyncio.create_task(task_expire_access())
    asyncio.create_task(task_delete_broadcasts())
    asyncio.create_task(task_refresh_cache())
    logger.info("Bot started")

async def on_shutdown():
    if pool:
        await pool.close()
    logger.info("Bot stopped")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
