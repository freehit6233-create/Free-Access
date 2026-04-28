import os
import asyncio
import logging
import hashlib
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ─────────────────────── ENV VARIABLES ───────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
VPLINK_KEY   = os.environ["VPLINK_API_KEY"]
CHANNEL_ID   = int(os.environ["CHANNEL_ID"])   # e.g. -100xxxxxxxxxx

# ─────────────────────── LOGGING ─────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────── BOT & DISPATCHER ────────────────────
bot  = Bot(token=BOT_TOKEN)
dp   = Dispatcher(storage=MemoryStorage())

# ─────────────────────── FSM STATES ──────────────────────────
class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_timer     = State()
    waiting_set_key   = State()
    waiting_set_val   = State()

# ─────────────────────── DB HELPERS ──────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id     BIGINT PRIMARY KEY,
                    name        TEXT,
                    username    TEXT,
                    joined_at   TIMESTAMPTZ DEFAULT NOW(),
                    access_until TIMESTAMPTZ,
                    is_banned   BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS verifications (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT,
                    verified_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS videos (
                    id          SERIAL PRIMARY KEY,
                    message_id  BIGINT NOT NULL,
                    added_at    TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                INSERT INTO settings(key, value)
                VALUES ('delete_after_minutes', '10'),
                       ('vplink_url', 'https://vplink.in')
                ON CONFLICT DO NOTHING;
            """)
        conn.commit()
    log.info("DB initialised ✓")

def get_setting(key: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None

def set_setting(key: str, value: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, value))
        conn.commit()

def upsert_user(user_id: int, name: str, username: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users(user_id, name, username)
                VALUES(%s,%s,%s)
                ON CONFLICT(user_id) DO UPDATE
                SET name=EXCLUDED.name, username=EXCLUDED.username
            """, (user_id, name, username))
        conn.commit()

def get_user(user_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
            return cur.fetchone()

def has_access(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or user["is_banned"]:
        return False
    if user["access_until"] and user["access_until"] > datetime.now(timezone.utc):
        return True
    return False

def grant_access(user_id: int, hours: int = 6):
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET access_until=%s WHERE user_id=%s", (until, user_id))
        conn.commit()

def record_verification(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO verifications(user_id) VALUES(%s)", (user_id,))
        conn.commit()

def get_video(index: int):
    """0-based index"""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM videos ORDER BY id LIMIT 1 OFFSET %s", (index,))
            return cur.fetchone()

def total_videos() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM videos")
            return cur.fetchone()[0]

def total_users() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0]

def verified_last_24h() -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM verifications WHERE verified_at>%s", (since,))
            return cur.fetchone()[0]

def all_user_ids():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE is_banned=FALSE")
            return [r[0] for r in cur.fetchall()]

def ban_user(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_banned=TRUE WHERE user_id=%s", (user_id,))
        conn.commit()

def unban_user(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_banned=FALSE WHERE user_id=%s", (user_id,))
        conn.commit()

# ─────────────────────── VPLINK HELPER ───────────────────────
def generate_short_link(original_url: str) -> str:
    """Generate a vplink.in short link via API."""
    try:
        api_url = f"https://vplink.in/api?api={VPLINK_KEY}&url={original_url}&format=text"
        r = requests.get(api_url, timeout=10)
        r.raise_for_status()
        short = r.text.strip()
        if short.startswith("http"):
            return short
    except Exception as e:
        log.error(f"vplink error: {e}")
    return original_url  # fallback: original URL

# ─────────────────────── TOKEN STORE (in-memory) ─────────────
# Maps token -> user_id; real prod should use DB/Redis
pending_tokens: dict[str, int] = {}

def make_token(user_id: int) -> str:
    raw = f"{user_id}-{time.time()}-{BOT_TOKEN}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

# ─────────────────────── KEYBOARDS ───────────────────────────
def kb_get_link():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Get Link", callback_data="get_link")]
    ])

def kb_verify(url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Verify This Link", url=url)],
        [InlineKeyboardButton(text="🔁 I've Verified", callback_data="check_verify")]
    ])

def kb_nav(index: int, total: int):
    row = []
    if index > 0:
        row.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"nav:{index-1}"))
    if index < total - 1:
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"nav:{index+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None

def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Status",    callback_data="admin_status")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⏱ Set Timer",  callback_data="admin_timer")],
        [InlineKeyboardButton(text="⚙️ Settings",  callback_data="admin_settings")],
    ])

# ─────────────────────── AUTO-DELETE HELPER ──────────────────
async def schedule_delete(chat_id: int, msg_id: int, minutes: int):
    await asyncio.sleep(minutes * 60)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass  # already deleted / not found

# ─────────────────────── SEND VIDEO HELPER ───────────────────
async def send_video_to_user(user_id: int, index: int = 0):
    video = get_video(index)
    total = total_videos()
    if not video:
        await bot.send_message(user_id, "⚠️ No videos available right now.")
        return

    minutes = int(get_setting("delete_after_minutes") or 10)

    try:
        fwd = await bot.copy_message(
            chat_id=user_id,
            from_chat_id=CHANNEL_ID,
            message_id=video["message_id"],
            reply_markup=kb_nav(index, total),
            protect_content=False,
        )
        # Schedule auto-delete
        asyncio.create_task(schedule_delete(user_id, fwd.message_id, minutes))
    except Exception as e:
        log.error(f"copy_message error: {e}")
        await bot.send_message(user_id, "⚠️ Could not fetch video. Try again later.")

# ─────────────────────── /start ──────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    upsert_user(user.id, user.full_name, user.username or "")

    if has_access(user.id):
        await message.answer(
            f"👋 Welcome back, <b>{user.full_name}</b>!\nYou already have active access.",
            parse_mode="HTML"
        )
        await send_video_to_user(user.id, 0)
        return

    await message.answer(
        f"🎬 <b>Welcome, {user.full_name}!</b>\n\n"
        "Get <b>FREE 6-hour access</b> to exclusive videos.\n"
        "Tap below to get your access link 👇",
        parse_mode="HTML",
        reply_markup=kb_get_link()
    )

# ─────────────────────── Get Link button ─────────────────────
@dp.callback_query(F.data == "get_link")
async def cb_get_link(call: CallbackQuery):
    user = call.from_user
    token = make_token(user.id)
    pending_tokens[token] = user.id

    # Deep-link back to bot after verification
    bot_info = await bot.get_me()
    return_url = f"https://t.me/{bot_info.username}?start=verify_{token}"
    short_url  = generate_short_link(return_url)

    await call.message.edit_caption(
        caption="🔒 <b>Verify This Link</b>\n\n"
                "1️⃣ Tap the button below\n"
                "2️⃣ Complete the short-link verification\n"
                "3️⃣ Come back and tap <b>I've Verified</b>",
        parse_mode="HTML",
        reply_markup=kb_verify(short_url)
    ) if call.message.caption else None

    # If it was a text message (not caption)
    try:
        await call.message.edit_text(
            "🔒 <b>Verify This Link</b>\n\n"
            "1️⃣ Tap the button below\n"
            "2️⃣ Complete the short-link verification\n"
            "3️⃣ Come back and tap <b>I've Verified</b>",
            parse_mode="HTML",
            reply_markup=kb_verify(short_url)
        )
    except Exception:
        pass  # was caption-only message

    await call.answer()

# ─────────────────────── Deep-link verify via /start ─────────
@dp.message(Command("start"))
async def cmd_start_verify(message: types.Message):
    """Handles /start verify_TOKEN deep links"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].startswith("verify_"):
        return  # handled by first /start handler (registered first, so this won't run)

    token = args[1][len("verify_"):]
    user  = message.from_user

    if token not in pending_tokens or pending_tokens[token] != user.id:
        await message.answer("⚠️ Invalid or expired link. Please start over with /start")
        return

    del pending_tokens[token]
    grant_access(user.id, hours=6)
    record_verification(user.id)

    # Notify admin
    asyncio.create_task(notify_admin_verification(user))

    await message.answer(
        "✅ <b>Access Granted!</b>\n\n"
        "🎉 You now have <b>6 hours</b> of free access.\n"
        "Enjoy the videos! 🎬",
        parse_mode="HTML"
    )
    await send_video_to_user(user.id, 0)

async def notify_admin_verification(user: types.User):
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>New Verification!</b>\n\n"
            f"👤 Name: <b>{user.full_name}</b>\n"
            f"🆔 User ID: <code>{user.id}</code>\n"
            f"🕐 Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"Admin notify error: {e}")

# ─────────────────────── I've Verified button ────────────────
@dp.callback_query(F.data == "check_verify")
async def cb_check_verify(call: CallbackQuery):
    user = call.from_user
    if has_access(user.id):
        await call.message.edit_text(
            "✅ <b>You're already verified!</b> Sending your first video...",
            parse_mode="HTML"
        )
        await send_video_to_user(user.id, 0)
    else:
        await call.answer("❌ Not verified yet. Please complete the link first.", show_alert=True)

# ─────────────────────── Nav buttons ─────────────────────────
@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(call: CallbackQuery):
    user  = call.from_user
    if not has_access(user.id):
        await call.answer("⛔ Your access has expired. Send /start to get a new link.", show_alert=True)
        return

    index = int(call.data.split(":")[1])
    video = get_video(index)
    total = total_videos()

    if not video:
        await call.answer("No more videos.", show_alert=True)
        return

    minutes = int(get_setting("delete_after_minutes") or 10)

    try:
        # Delete old message, send new one (edit_media workaround for copy_message)
        await call.message.delete()
        fwd = await bot.copy_message(
            chat_id=user.id,
            from_chat_id=CHANNEL_ID,
            message_id=video["message_id"],
            reply_markup=kb_nav(index, total),
            protect_content=False,
        )
        asyncio.create_task(schedule_delete(user.id, fwd.message_id, minutes))
    except Exception as e:
        log.error(f"nav error: {e}")
        await call.answer("Error loading video.", show_alert=True)

# ─────────────────────── ADMIN /panel ────────────────────────
@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔧 <b>Admin Panel</b>", parse_mode="HTML", reply_markup=kb_admin_panel())

# ─────────────────────── ADMIN /status ───────────────────────
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    v24 = verified_last_24h()
    tv  = total_videos()
    tu  = total_users()
    delete_min = get_setting("delete_after_minutes")
    await message.answer(
        f"📊 <b>Bot Status</b>\n\n"
        f"🧑‍💻 Total Users   : <b>{tu}</b>\n"
        f"✅ Verified (24h) : <b>{v24}</b>\n"
        f"🎬 Total Videos  : <b>{tv}</b>\n"
        f"⏱ Auto-delete    : <b>{delete_min} min</b>",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_status")
async def cb_admin_status(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Unauthorized")
    await cmd_status(call.message)
    await call.answer()

# ─────────────────────── ADMIN Broadcast ─────────────────────
@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Unauthorized")
    await call.message.answer("📢 Send the message you want to broadcast to all users:")
    await state.set_state(AdminStates.waiting_broadcast)
    await call.answer()

@dp.message(AdminStates.waiting_broadcast)
async def do_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    ids    = all_user_ids()
    sent   = 0
    failed = 0
    for uid in ids:
        try:
            await message.copy_to(uid)
            sent += 1
            await asyncio.sleep(0.05)  # flood control
        except Exception:
            failed += 1
    await message.answer(f"✅ Broadcast done!\n📤 Sent: {sent}\n❌ Failed: {failed}")

# ─────────────────────── ADMIN Set Timer ─────────────────────
@dp.callback_query(F.data == "admin_timer")
async def cb_admin_timer(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Unauthorized")
    current = get_setting("delete_after_minutes")
    await call.message.answer(f"⏱ Current delete timer: <b>{current} min</b>\nSend new value (in minutes):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_timer)
    await call.answer()

@dp.message(AdminStates.waiting_timer)
async def do_set_timer(message: types.Message, state: FSMContext):
    await state.clear()
    val = message.text.strip()
    if not val.isdigit() or int(val) < 1:
        await message.answer("❌ Invalid value. Enter a positive number.")
        return
    set_setting("delete_after_minutes", val)
    await message.answer(f"✅ Auto-delete timer set to <b>{val} minutes</b>.", parse_mode="HTML")

# ─────────────────────── ADMIN Settings ──────────────────────
@dp.callback_query(F.data == "admin_settings")
async def cb_admin_settings(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Unauthorized")
    await call.message.answer(
        "⚙️ <b>Settings</b>\n\nSend: <code>key=value</code>\n\n"
        "Available keys:\n"
        "• <code>delete_after_minutes</code>\n"
        "• <code>vplink_url</code>\n"
        "• <code>access_hours</code>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_set_key)
    await call.answer()

@dp.message(AdminStates.waiting_set_key)
async def do_set_setting(message: types.Message, state: FSMContext):
    await state.clear()
    text = message.text.strip()
    if "=" not in text:
        await message.answer("❌ Format: key=value")
        return
    key, value = text.split("=", 1)
    set_setting(key.strip(), value.strip())
    await message.answer(f"✅ Set <code>{key.strip()}</code> = <code>{value.strip()}</code>", parse_mode="HTML")

# ─────────────────────── ADMIN /addvideo ─────────────────────
@dp.message(Command("addvideo"))
async def cmd_addvideo(message: types.Message):
    """Usage: forward a video from the private channel, then reply with /addvideo <message_id>"""
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Usage: /addvideo <channel_message_id>")
        return
    msg_id = int(args[1])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO videos(message_id) VALUES(%s) ON CONFLICT DO NOTHING", (msg_id,))
        conn.commit()
    await message.answer(f"✅ Video message_id <code>{msg_id}</code> added.", parse_mode="HTML")

# ─────────────────────── ADMIN /ban & /unban ─────────────────
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Usage: /ban <user_id>")
        return
    ban_user(int(args[1]))
    await message.answer(f"🚫 User <code>{args[1]}</code> banned.", parse_mode="HTML")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Usage: /unban <user_id>")
        return
    unban_user(int(args[1]))
    await message.answer(f"✅ User <code>{args[1]}</code> unbanned.", parse_mode="HTML")

# ─────────────────────── MAIN ────────────────────────────────
async def main():
    init_db()
    log.info("Bot starting...")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
