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
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
CHANNEL_ID   = int(os.environ["CHANNEL_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
VP_TOKEN     = os.environ["VP_LINK_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "RNDAccess_bot").lstrip("@")

FREE_VIDEOS       = 3
ACCESS_HOURS      = 3
FREE_RESET_HOURS  = 24
AUTO_DELETE_VIDEO = 600   # 10 min
AUTO_DELETE_CMD   = 10
BROADCAST_TTL     = 43200
RANDOM_INJECT_PCT = 0.02

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_videos (
    id         SERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL UNIQUE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS users (
    user_id          BIGINT PRIMARY KEY,
    current_index    INT         NOT NULL DEFAULT 0,
    free_start_ts    TIMESTAMPTZ,
    access_until     TIMESTAMPTZ,
    last_verify_msg  BIGINT,
    last_video_msg   BIGINT,
    last_nav_msg     BIGINT,
    is_banned        BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS verifications (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS broadcast_msgs (
    id          SERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    delete_at   TIMESTAMPTZ NOT NULL
);
"""

pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, ssl="require", min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        for m in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_start_ts TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS access_until TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_verify_msg BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_video_msg BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_nav_msg BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS current_index INT NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            try:
                await conn.execute(m)
            except Exception:
                pass
    logger.info("DB pool ready")

async def get_user(conn, user_id):
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if row is None:
        await conn.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return row

async def get_video_ids():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT message_id FROM channel_videos ORDER BY id ASC")
    return [r["message_id"] for r in rows]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

def now_utc():
    return datetime.now(timezone.utc)

def make_token(user_id):
    # Use HMAC-style: hash of "uid:secret" — no underscores in hex output so split is safe
    return hashlib.sha256(f"{user_id}:{VP_TOKEN}".encode()).hexdigest()[:32]

def make_verify_url(user_id):
    token   = make_token(user_id)
    # payload format: verify-USERID-TOKEN  (dash separator, not underscore)
    payload = f"verify-{user_id}-{token}"
    dest    = f"https://t.me/{BOT_USERNAME}?start={payload}"
    encoded = urllib.parse.quote(dest, safe="")
    return f"https://vplink.in/api?api={VP_TOKEN}&url={encoded}&alias=v{user_id}"

def nav_kb(index):
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Previous", callback_data=f"nav:prev:{index}")
    b.button(text="Next ➡️",     callback_data=f"nav:next:{index}")
    b.adjust(2)
    return b.as_markup()

def verify_kb(user_id):
    b = InlineKeyboardBuilder()
    b.button(text="🔗 Get Link", url=make_verify_url(user_id))
    return b.as_markup()

async def silent_delete(chat_id, msg_id):
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

async def delete_after(chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    await silent_delete(chat_id, msg_id)

async def has_access(conn, user_id):
    row = await get_user(conn, user_id)
    if row["is_banned"]:
        return False
    if row["access_until"] and row["access_until"] > now_utc():
        return True
    if row["free_start_ts"]:
        elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
        if elapsed < FREE_RESET_HOURS * 3600 and row["current_index"] < FREE_VIDEOS:
            return True
    elif row["current_index"] < FREE_VIDEOS:
        return True
    return False

async def show_gate(user_id, conn):
    """Delete old gate message (anti-spam) and send fresh verification gate."""
    row = await get_user(conn, user_id)
    await silent_delete(user_id, row["last_verify_msg"])
    sent = await bot.send_message(
        user_id,
        "🔒 *Access Required*\n\nVerify this link to get *3 hours free access*:",
        parse_mode="Markdown",
        reply_markup=verify_kb(user_id),
    )
    await conn.execute("UPDATE users SET last_verify_msg=$1 WHERE user_id=$2", sent.message_id, user_id)

async def delete_prev_video(user_id, conn):
    """Delete user's previously sent video + nav message."""
    row = await get_user(conn, user_id)
    await silent_delete(user_id, row["last_video_msg"])
    await silent_delete(user_id, row["last_nav_msg"])

async def send_video(user_id, index, video_ids, conn):
    """Send video with nav buttons attached directly. Deletes previous video first."""
    if not video_ids:
        return

    # Delete previous video + nav
    await delete_prev_video(user_id, conn)

    # Pick message_id (2% random inject)
    if len(video_ids) > 1 and random.random() < RANDOM_INJECT_PCT:
        msg_id = random.choice(video_ids)
    else:
        msg_id = video_ids[index % len(video_ids)]

    # Send video
    try:
        vid = await bot.copy_message(
            chat_id=user_id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id,
            protect_content=True,
        )
    except TelegramBadRequest as e:
        logger.warning(f"copy_message uid={user_id} mid={msg_id}: {e}")
        return

    # Send nav buttons as separate message (no counter text)
    nav = await bot.send_message(user_id, "‌", reply_markup=nav_kb(index))  # zero-width non-joiner as placeholder

    # Save message IDs for future deletion
    await conn.execute(
        "UPDATE users SET last_video_msg=$1, last_nav_msg=$2 WHERE user_id=$3",
        vid.message_id, nav.message_id, user_id,
    )

    # Schedule auto-delete after 10 min
    asyncio.create_task(delete_after(user_id, vid.message_id, AUTO_DELETE_VIDEO))
    asyncio.create_task(delete_after(user_id, nav.message_id, AUTO_DELETE_VIDEO))


# ── Channel post auto-indexer ─────────────────────────────────────────────────
@dp.channel_post()
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return
    is_video = bool(message.video) or bool(
        message.document and message.document.mime_type
        and "video" in message.document.mime_type
    )
    if not is_video:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO channel_videos(message_id) VALUES($1) ON CONFLICT DO NOTHING",
            message.message_id,
        )
    logger.info(f"Auto-indexed video: msg_id={message.message_id}")


# ── /index (admin) ────────────────────────────────────────────────────────────
@dp.message(Command("index"))
async def cmd_index(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()[1:]
    if not parts:
        sent = await message.answer(
            "ℹ️ Usage: `/index 101 102 103`\nSpace-separated channel message IDs.",
            parse_mode="Markdown"
        )
        asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
        try: await message.delete()
        except Exception: pass
        return
    added = 0
    async with pool.acquire() as conn:
        for p in parts:
            try:
                await conn.execute(
                    "INSERT INTO channel_videos(message_id) VALUES($1) ON CONFLICT DO NOTHING", int(p)
                )
                added += 1
            except Exception:
                pass
    total = len(await get_video_ids())
    sent = await message.answer(f"✅ Added {added}. Total videos: {total}")
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 20))
    try: await message.delete()
    except Exception: pass


# ── /start ────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    parts   = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""

    async with pool.acquire() as conn:
        row = await get_user(conn, user_id)

        # ── Verify deep-link: format is  verify-USERID-TOKEN ─────────────
        if payload.startswith("verify-"):
            # Split with maxsplit=2 so token (hex, no dashes) stays intact
            segs = payload.split("-", 2)
            if len(segs) == 3 and segs[0] == "verify":
                _, uid_str, tok = segs
                try:
                    uid_int = int(uid_str)
                except ValueError:
                    uid_int = -1
                if uid_int == user_id and tok == make_token(user_id):
                    expiry = now_utc() + timedelta(hours=ACCESS_HOURS)
                    await conn.execute(
                        "UPDATE users SET access_until=$1, last_verify_msg=NULL WHERE user_id=$2",
                        expiry, user_id,
                    )
                    await conn.execute("INSERT INTO verifications(user_id) VALUES($1)", user_id)
                    # Delete old gate message
                    await silent_delete(user_id, row["last_verify_msg"])
                    # Confirm access
                    s = await message.answer(
                        f"✅ *Access granted!* You have *{ACCESS_HOURS} hours* free. 🎬",
                        parse_mode="Markdown",
                    )
                    asyncio.create_task(delete_after(user_id, s.message_id, AUTO_DELETE_CMD))
                    # Notify admin
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"✅ *New Verification*\n👤 {message.from_user.full_name}\n🆔 `{user_id}`",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    # Show current video
                    video_ids = await get_video_ids()
                    if video_ids:
                        idx = row["current_index"]
                        await send_video(user_id, idx, video_ids, conn)
                    return
                else:
                    await message.answer("❌ Invalid or expired verification link.")
                    return

        # ── Normal /start ─────────────────────────────────────────────────
        video_ids = await get_video_ids()
        if not video_ids:
            await message.answer("⚠️ No videos available yet.")
            return

        # Init free window
        if row["free_start_ts"] is None:
            await conn.execute("UPDATE users SET free_start_ts=$1 WHERE user_id=$2", now_utc(), user_id)
            row = await get_user(conn, user_id)

        # 24h reset
        if row["free_start_ts"]:
            elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
            no_acc  = not row["access_until"] or row["access_until"] <= now_utc()
            if elapsed >= FREE_RESET_HOURS * 3600 and no_acc:
                await conn.execute(
                    "UPDATE users SET free_start_ts=$1, current_index=0 WHERE user_id=$2",
                    now_utc(), user_id,
                )
                row = await get_user(conn, user_id)

        idx    = row["current_index"]
        access = await has_access(conn, user_id)

        if not access:
            await show_gate(user_id, conn)
            try: await message.delete()
            except Exception: pass
            return

        await send_video(user_id, idx, video_ids, conn)

    try: await message.delete()
    except Exception: pass


# ── Navigation callbacks ──────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _, direction, idx_str = callback.data.split(":")
    idx = int(idx_str)

    async with pool.acquire() as conn:
        row       = await get_user(conn, user_id)
        video_ids = await get_video_ids()

        if not video_ids:
            await callback.answer("No videos available.", show_alert=True)
            return

        new_idx = (idx + 1) % len(video_ids) if direction == "next" else (idx - 1) % len(video_ids)

        # 24h reset
        if row["free_start_ts"]:
            elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
            no_acc  = not row["access_until"] or row["access_until"] <= now_utc()
            if elapsed >= FREE_RESET_HOURS * 3600 and no_acc:
                await conn.execute(
                    "UPDATE users SET free_start_ts=$1, current_index=0 WHERE user_id=$2",
                    now_utc(), user_id,
                )
                new_idx = 0
                row = await get_user(conn, user_id)

        # Gate check — after free 3, require verification
        access = await has_access(conn, user_id)
        if not access and new_idx >= FREE_VIDEOS:
            await callback.answer()
            # Delete old nav message (video already sent above it)
            await silent_delete(user_id, row["last_nav_msg"])
            await show_gate(user_id, conn)
            return

        # Save new index
        await conn.execute("UPDATE users SET current_index=$1 WHERE user_id=$2", new_idx, user_id)

        await callback.answer()
        await send_video(user_id, new_idx, video_ids, conn)


# ── /help ─────────────────────────────────────────────────────────────────────
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    sent = await message.answer(
        "📖 *Help*\n\n/start — Watch videos\n/help — This message\n\n"
        "_3 videos free per 24h. Verify link for 3h full access._",
        parse_mode="Markdown",
    )
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
    try: await message.delete()
    except Exception: pass


# ── /status (admin) ───────────────────────────────────────────────────────────
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        v = await conn.fetchval(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= $1",
            now_utc() - timedelta(hours=24),
        )
        u = await conn.fetchval("SELECT COUNT(*) FROM users")
    vids = await get_video_ids()
    sent = await message.answer(
        f"📊 *Status*\n\n✅ Verifications (24h): `{v}`\n👥 Users: `{u}`\n📹 Videos: `{len(vids)}`",
        parse_mode="Markdown",
    )
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
    try: await message.delete()
    except Exception: pass


# ── /reset (admin) ────────────────────────────────────────────────────────────
@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET access_until=NULL, free_start_ts=NULL, current_index=0")
    sent = await message.answer("♻️ All users reset.")
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, AUTO_DELETE_CMD))
    try: await message.delete()
    except Exception: pass


# ── /broadcast (admin) ────────────────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    reply = message.reply_to_message
    if not reply:
        sent = await message.answer("↩️ Reply to a message with /broadcast.")
        asyncio.create_task(delete_after(message.chat.id, sent.message_id, AUTO_DELETE_CMD))
        try: await message.delete()
        except Exception: pass
        return

    async with pool.acquire() as conn:
        uids = [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users WHERE is_banned=FALSE")]

    delete_at = now_utc() + timedelta(seconds=BROADCAST_TTL)
    ok = fail = 0
    async with pool.acquire() as conn:
        for uid in uids:
            try:
                sm = await reply.copy_to(uid)
                await conn.execute(
                    "INSERT INTO broadcast_msgs(chat_id,message_id,delete_at) VALUES($1,$2,$3)",
                    uid, sm.message_id, delete_at,
                )
                ok += 1
            except TelegramForbiddenError:
                fail += 1
            except Exception as e:
                logger.warning(f"broadcast uid={uid}: {e}")
                fail += 1
            await asyncio.sleep(0.05)

    sent = await message.answer(f"📣 Done! ✅{ok} ❌{fail}")
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
    try: await message.delete()
    except Exception: pass


# ── Background tasks ──────────────────────────────────────────────────────────
async def task_expire_access():
    while True:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id FROM users WHERE access_until IS NOT NULL AND access_until <= $1",
                    now_utc(),
                )
                for row in rows:
                    uid = row["user_id"]
                    try:
                        await bot.send_message(uid,
                            "⏰ *Access expired.* Use /start and verify again! 🔗",
                            parse_mode="Markdown")
                    except Exception:
                        pass
                    await conn.execute("UPDATE users SET access_until=NULL WHERE user_id=$1", uid)
        except Exception as e:
            logger.error(f"expire task: {e}")
        await asyncio.sleep(60)

async def task_delete_broadcasts():
    while True:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id,chat_id,message_id FROM broadcast_msgs WHERE delete_at <= $1", now_utc()
                )
                for row in rows:
                    await silent_delete(row["chat_id"], row["message_id"])
                    await conn.execute("DELETE FROM broadcast_msgs WHERE id=$1", row["id"])
        except Exception as e:
            logger.error(f"broadcast cleanup: {e}")
        await asyncio.sleep(60)


# ── Startup / shutdown ────────────────────────────────────────────────────────
async def on_startup():
    await init_db()
    asyncio.create_task(task_expire_access())
    asyncio.create_task(task_delete_broadcasts())
    total = len(await get_video_ids())
    logger.info(f"Bot started — {total} videos in DB")
    if total == 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ *0 videos indexed!*\n\n"
                "Channel mein bot ko Admin banao (Read Messages permission).\n"
                "Existing videos ke liye:\n`/index 101 102 103 ...`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

async def on_shutdown():
    if pool:
        await pool.close()

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
