"""
VVIP Video Telegram Bot
Author: SYNAX
Features: Video navigation, free limit, VPLink verification, admin panel, auto-delete
"""

import asyncio
import logging
import os
import aiohttp
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import asyncpg

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN          = os.getenv("BOT_TOKEN")
ADMIN_ID           = int(os.getenv("ADMIN_ID", "0"))
PRIVATE_CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID", "0"))
DATABASE_URL       = os.getenv("DATABASE_URL")
VPLINK_API         = os.getenv("VPLINK_API")          # base URL of VPLink API
BOT_USERNAME       = os.getenv("BOT_USERNAME", "")    # without @

DEFAULT_FREE_HOURS = 3
FREE_VIDEO_LIMIT   = 3
AUTO_DELETE_SEC    = 600   # 10 minutes
HELP_DELETE_SEC    = 10
BROADCAST_DELETE_H = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ── DB pool (global) ──────────────────────────────────────────────────────────
pool: asyncpg.Pool = None

async def get_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return pool

# ── DB helpers ────────────────────────────────────────────────────────────────
async def init_db():
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      BIGINT PRIMARY KEY,
            username     TEXT,
            full_name    TEXT,
            video_index  INT  DEFAULT 0,
            free_used    INT  DEFAULT 0,
            access_until TIMESTAMPTZ,
            last_reset   TIMESTAMPTZ DEFAULT NOW(),
            joined_at    TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS verifications (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT,
            verified_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS broadcast_msgs (
            id          SERIAL PRIMARY KEY,
            chat_id     BIGINT,
            message_id  BIGINT,
            delete_at   TIMESTAMPTZ
        );
        """)
        # seed default free_hours setting
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('free_hours', $1)
            ON CONFLICT (key) DO NOTHING
        """, str(DEFAULT_FREE_HOURS))


async def upsert_user(user_id: int, username: str, full_name: str):
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET username=$2, full_name=$3
        """, user_id, username or "", full_name)


async def get_user(user_id: int) -> asyncpg.Record | None:
    p = await get_pool()
    async with p.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    p = await get_pool()
    async with p.acquire() as conn:
        sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kwargs))
        vals = list(kwargs.values())
        await conn.execute(
            f"UPDATE users SET {sets} WHERE user_id=$1",
            user_id, *vals
        )


async def get_setting(key: str) -> str:
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else None


async def set_setting(key: str, value: str):
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value=$2
        """, key, value)


async def log_verification(user_id: int):
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO verifications (user_id) VALUES ($1)", user_id
        )


async def verifications_last_24h() -> int:
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM verifications WHERE verified_at > NOW() - INTERVAL '24 hours'"
        )
        return row["cnt"]


async def all_user_ids() -> list[int]:
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]


async def save_broadcast_msg(chat_id: int, message_id: int, delete_at: datetime):
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute(
            "INSERT INTO broadcast_msgs (chat_id, message_id, delete_at) VALUES ($1, $2, $3)",
            chat_id, message_id, delete_at
        )

# ── Bot / Dispatcher ──────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ── Utility ───────────────────────────────────────────────────────────────────
async def get_channel_videos() -> list[int]:
    """Return list of message IDs that contain video in the private channel."""
    msgs = []
    try:
        async for msg in bot.iter_messages(PRIVATE_CHANNEL_ID, limit=200):
            if msg.video or msg.document:
                msgs.append(msg.message_id)
    except Exception:
        # fallback: get latest 200 messages and filter
        pass
    return sorted(msgs)


# cache videos to avoid repeated API calls
_video_cache: list[int] = []
_cache_time: datetime   = None

async def cached_videos() -> list[int]:
    global _video_cache, _cache_time
    now = datetime.now(timezone.utc)
    if not _video_cache or (_cache_time and (now - _cache_time).seconds > 300):
        _video_cache = await get_channel_videos()
        _cache_time  = now
    return _video_cache


def nav_keyboard(index: int, total: int, show_get_link=False) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    if index > 0:
        row.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"nav:{index-1}"))
    if index < total - 1:
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"nav:{index+1}"))
    if row:
        buttons.append(row)
    if show_get_link:
        buttons.append([InlineKeyboardButton(text="🔗 Get Link", callback_data="get_link")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_video_to_user(user_id: int, index: int, bot_instance: Bot):
    videos = await cached_videos()
    if not videos or index >= len(videos):
        await bot_instance.send_message(user_id, "⚠️ No videos found in channel.")
        return

    msg_id = videos[index]
    total  = len(videos)
    kb     = nav_keyboard(index, total)

    try:
        sent = await bot_instance.copy_message(
            chat_id=user_id,
            from_chat_id=PRIVATE_CHANNEL_ID,
            message_id=msg_id,
            protect_content=True,
            reply_markup=kb
        )
        # schedule auto-delete after 10 min
        asyncio.create_task(auto_delete(user_id, sent.message_id, AUTO_DELETE_SEC))
    except Exception as e:
        log.error(f"send_video error: {e}")
        await bot_instance.send_message(user_id, f"⚠️ Could not load video #{index+1}.")


async def auto_delete(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def check_and_reset_free(user: asyncpg.Record) -> asyncpg.Record:
    """If 24h passed since last_reset, reset free_used counter."""
    now = datetime.now(timezone.utc)
    last_reset = user["last_reset"]
    if last_reset and (now - last_reset) >= timedelta(hours=24):
        await update_user(user["user_id"], free_used=0, last_reset=now)
        # re-fetch
        return await get_user(user["user_id"])
    return user


async def user_has_active_access(user: asyncpg.Record) -> bool:
    au = user["access_until"]
    if au and au > datetime.now(timezone.utc):
        return True
    return False


async def generate_vplink(user_id: int) -> str:
    """Call VPLink API to generate a short verification link."""
    callback_url = f"https://t.me/{BOT_USERNAME}?start=verify_{user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{VPLINK_API}/api",
                params={"api": VPLINK_API.split("api=")[-1] if "api=" in VPLINK_API else "",
                        "url": callback_url},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                data = await resp.json()
                return data.get("shortenedUrl") or data.get("short_url") or callback_url
    except Exception as e:
        log.warning(f"VPLink error: {e}")
        return callback_url  # fallback to direct link

# ── /start ────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    await upsert_user(user.id, user.username, user.full_name)

    # handle verify callback: /start verify_<user_id>
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("verify_"):
        try:
            vid = int(args[1].split("_")[1])
        except Exception:
            vid = None
        if vid == user.id:
            await handle_verification(message, user.id)
            return

    db_user = await get_user(user.id)
    db_user = await check_and_reset_free(db_user)

    # delete any old 'Get Link' / start messages (send fresh)
    welcome = (
        f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
        f"🎬 You have <b>{FREE_VIDEO_LIMIT - db_user['free_used']}</b> free videos remaining.\n"
        f"Tap below to start watching!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Watch Videos", callback_data=f"nav:{db_user['video_index']}")]
    ])
    await message.answer(welcome, reply_markup=kb)


async def handle_verification(message: Message, user_id: int):
    db_user = await get_user(user_id)
    free_hours = int(await get_setting("free_hours") or DEFAULT_FREE_HOURS)
    access_until = datetime.now(timezone.utc) + timedelta(hours=free_hours)

    await update_user(user_id, access_until=access_until)
    await log_verification(user_id)

    name = message.from_user.full_name
    conf = await message.answer(
        f"✅ <b>Hello {name}, your {free_hours} hour access is active!</b>\n"
        f"⏰ Access expires at: <code>{access_until.strftime('%H:%M UTC')}</code>"
    )

    # notify admin
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>New Verification!</b>\n"
            f"👤 Name: <a href='tg://user?id={user_id}'>{name}</a>\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"⏰ Access until: {access_until.strftime('%Y-%m-%d %H:%M UTC')}"
        )
    except Exception:
        pass

    # send next video immediately
    await asyncio.sleep(1)
    await send_video_to_user(user_id, db_user["video_index"], bot)

    # schedule access expiry notification
    asyncio.create_task(notify_access_expired(user_id, free_hours * 3600))


async def notify_access_expired(user_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.send_message(
            user_id,
            "⏰ <b>Your free access has expired.</b>\n"
            "You'll get 3 free videos again in 24 hours, or verify a new link for more access!"
        )
    except Exception:
        pass

# ── Navigation Callback ───────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("nav:"))
async def nav_callback(call: CallbackQuery):
    user_id = call.from_user.id
    index   = int(call.data.split(":")[1])

    db_user = await get_user(user_id)
    if not db_user:
        await upsert_user(user_id, call.from_user.username, call.from_user.full_name)
        db_user = await get_user(user_id)

    db_user = await check_and_reset_free(db_user)

    has_access = await user_has_active_access(db_user)
    free_used  = db_user["free_used"]

    # check if free limit exceeded
    if not has_access and free_used >= FREE_VIDEO_LIMIT:
        free_hours = int(await get_setting("free_hours") or DEFAULT_FREE_HOURS)
        link = await generate_vplink(user_id)
        kb   = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Get Link", url=link)]
        ])
        await call.message.answer(
            f"🔒 <b>Free limit reached!</b>\n\n"
            f"Verify this link to get free access for <b>{free_hours} hours</b>:",
            reply_markup=kb
        )
        await call.answer()
        return

    # update index and free_used counter
    new_free = free_used + 1 if not has_access else free_used
    await update_user(user_id, video_index=index, free_used=new_free)

    await call.answer()
    await send_video_to_user(user_id, index, bot)


@router.callback_query(F.data == "get_link")
async def get_link_callback(call: CallbackQuery):
    user_id    = call.from_user.id
    free_hours = int(await get_setting("free_hours") or DEFAULT_FREE_HOURS)
    link = await generate_vplink(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Verify Now", url=link)]
    ])
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer(f"Get {free_hours}h free access by verifying the link!", show_alert=True)

# ── /help ─────────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>Bot Help</b>\n\n"
        "/start — Start watching videos\n"
        "/help — Show this message\n\n"
        "<b>Free users:</b> 3 videos free every 24 hours.\n"
        "Verify a link to unlock more hours!\n\n"
        "<i>This message will auto-delete in 10 seconds.</i>"
    )
    sent = await message.answer(text)
    asyncio.create_task(auto_delete(message.chat.id, sent.message_id, HELP_DELETE_SEC))
    asyncio.create_task(auto_delete(message.chat.id, message.message_id, HELP_DELETE_SEC))

# ── Admin Commands ────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not is_admin(message.from_user.id):
        return
    videos   = await cached_videos()
    verifs   = await verifications_last_24h()
    p        = await get_pool()
    async with p.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
    free_hours  = await get_setting("free_hours")
    await message.answer(
        f"📊 <b>Bot Status</b>\n\n"
        f"🎬 Videos in channel: <b>{len(videos)}</b>\n"
        f"✅ Verifications (24h): <b>{verifs}</b>\n"
        f"👥 Total users: <b>{total_users}</b>\n"
        f"⏱ Free access hours: <b>{free_hours}</b>"
    )


@router.message(Command("settimer"))
async def cmd_set_timer(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Usage: /settimer <hours>\nExample: /settimer 6")
        return
    hours = int(parts[1])
    await set_setting("free_hours", str(hours))
    await message.answer(f"✅ Free access timer set to <b>{hours} hours</b>.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Reply to a message to broadcast it.")
        return

    user_ids   = await all_user_ids()
    delete_at  = datetime.now(timezone.utc) + timedelta(hours=BROADCAST_DELETE_H)
    success, fail = 0, 0

    status_msg = await message.answer(f"📢 Broadcasting to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            sent = await message.reply_to_message.copy_to(uid)
            await save_broadcast_msg(uid, sent.message_id, delete_at)
            success += 1
            await asyncio.sleep(0.05)  # rate limit
        except Exception:
            fail += 1

    await status_msg.edit_text(
        f"✅ Broadcast complete!\n"
        f"Sent: {success} | Failed: {fail}\n"
        f"🗑 Auto-delete in {BROADCAST_DELETE_H} hours."
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: /ban <user_id>")
        return
    uid = int(parts[1])
    await update_user(uid, access_until=datetime(2000, 1, 1, tzinfo=timezone.utc))
    await message.answer(f"🚫 User <code>{uid}</code> banned.")


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: /unban <user_id>")
        return
    uid = int(parts[1])
    await update_user(uid, access_until=None, free_used=0)
    await message.answer(f"✅ User <code>{uid}</code> unbanned.")

# ── Scheduled Tasks ───────────────────────────────────────────────────────────
async def delete_expired_broadcasts():
    """Delete broadcast messages that are past their delete_at time."""
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, chat_id, message_id FROM broadcast_msgs WHERE delete_at <= NOW()"
        )
        for row in rows:
            try:
                await bot.delete_message(row["chat_id"], row["message_id"])
            except Exception:
                pass
        if rows:
            ids = [r["id"] for r in rows]
            await conn.execute("DELETE FROM broadcast_msgs WHERE id=ANY($1)", ids)


async def refresh_video_cache():
    global _video_cache, _cache_time
    _video_cache = await get_channel_videos()
    _cache_time  = datetime.now(timezone.utc)
    log.info(f"Video cache refreshed: {len(_video_cache)} videos")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    log.info("Database initialized.")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(delete_expired_broadcasts, "interval", minutes=10)
    scheduler.add_job(refresh_video_cache,        "interval", minutes=5)
    scheduler.start()

    log.info("Bot starting...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
