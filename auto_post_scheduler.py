"""
Standalone Telegram Auto Post Scheduler
---------------------------------------
Install:
    pip install pyTelegramBotAPI APScheduler

Set environment variables before running:
    BOT_TOKEN=your_new_bot_token
    ADMIN_ID=your_numeric_telegram_user_id

Run:
    python auto_post_scheduler.py

Important:
- Create a separate bot with @BotFather, or use a token you have rotated and secured.
- Add the bot as an administrator in channels where it should post.
- In groups, add the bot and allow it to send messages.
- Keep this script running for scheduled posts to fire.
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
DB_FILE = "auto_post_scheduler.db"
TIMEZONE = os.getenv("TIMEZONE", "Asia/Dhaka")

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN environment variable.")
if not ADMIN_ID_RAW.isdigit():
    raise SystemExit("Missing/invalid ADMIN_ID environment variable.")
ADMIN_ID = int(ADMIN_ID_RAW)

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Dhaka")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("auto-post-scheduler")

bot = telebot.TeleBot(BOT_TOKEN)
scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
user_state = {}
pending_content = {}  # admin_id -> (source_chat_id, source_message_id, description)


def db():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                added_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                target_mode TEXT NOT NULL,
                target_ids TEXT NOT NULL,
                run_at TEXT NOT NULL,
                repeat_mode TEXT NOT NULL DEFAULT 'once',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_result TEXT DEFAULT ''
            )
        """)


def is_admin(user_id):
    return user_id == ADMIN_ID


def get_targets():
    with db() as conn:
        return conn.execute("SELECT * FROM targets ORDER BY title").fetchall()


def add_target(chat):
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat.id)
    username = getattr(chat, "username", None)
    with db() as conn:
        conn.execute(
            """INSERT INTO targets(chat_id,title,username,added_at)
               VALUES(?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username""",
            (chat.id, title, username, datetime.now(LOCAL_TZ).isoformat())
        )


def get_post(post_id):
    with db() as conn:
        return conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()


def post_targets(row):
    if row["target_mode"] == "all":
        return [int(t["chat_id"]) for t in get_targets()]
    try:
        import json
        return [int(x) for x in json.loads(row["target_ids"])]
    except Exception:
        return []


def deliver_post(post_id):
    row = get_post(post_id)
    if not row or not row["active"]:
        return
    targets = post_targets(row)
    if not targets:
        result = "No targets selected/found."
        with db() as conn:
            conn.execute("UPDATE posts SET last_result=? WHERE id=?", (result, post_id))
        return

    successes, failures = 0, []
    for chat_id in targets:
        try:
            bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(row["source_chat_id"]),
                message_id=int(row["source_message_id"])
            )
            successes += 1
        except Exception as exc:
            failures.append(f"{chat_id}: {str(exc)[:120]}")
            logger.warning("Post %s failed for %s: %s", post_id, chat_id, exc)

    result = f"Success: {successes}/{len(targets)}"
    if failures:
        result += " | Errors: " + " ; ".join(failures[:5])
    with db() as conn:
        conn.execute("UPDATE posts SET last_result=? WHERE id=?", (result, post_id))

    try:
        bot.send_message(ADMIN_ID, f"📊 Post #{post_id} completed\n{result}")
    except Exception:
        pass

    if row["repeat_mode"] == "once":
        with db() as conn:
            conn.execute("UPDATE posts SET active=0 WHERE id=?", (post_id,))


def schedule_row(row):
    if not row["active"]:
        return
    job_id = f"post_{row['id']}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    run_at = datetime.fromisoformat(row["run_at"])
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=LOCAL_TZ)
    now = datetime.now(LOCAL_TZ)

    if row["repeat_mode"] == "once":
        if run_at <= now:
            # One-time posts missed while the script was offline are not sent automatically.
            with db() as conn:
                conn.execute("UPDATE posts SET active=0, last_result=? WHERE id=?",
                             ("Skipped: scheduled time passed while bot was offline.", row["id"]))
            return
        scheduler.add_job(deliver_post, DateTrigger(run_date=run_at), args=[row["id"]], id=job_id,
                          replace_existing=True, misfire_grace_time=3600)
    elif row["repeat_mode"] == "hourly":
        first = run_at if run_at > now else now + timedelta(minutes=1)
        scheduler.add_job(deliver_post, IntervalTrigger(hours=1, start_date=first, timezone=LOCAL_TZ),
                          args=[row["id"]], id=job_id, replace_existing=True, misfire_grace_time=3600)
    elif row["repeat_mode"] == "daily":
        scheduler.add_job(deliver_post, IntervalTrigger(days=1, start_date=run_at, timezone=LOCAL_TZ),
                          args=[row["id"]], id=job_id, replace_existing=True, misfire_grace_time=3600)


def restore_jobs():
    with db() as conn:
        rows = conn.execute("SELECT * FROM posts WHERE active=1").fetchall()
    for row in rows:
        try:
            schedule_row(row)
        except Exception as exc:
            logger.exception("Could not restore post #%s: %s", row["id"], exc)


def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ New Scheduled Post", "👥 Targets")
    kb.row("📋 My Schedules", "⏸ Pause Schedule")
    kb.row("▶️ Resume Schedule", "❌ Cancel")
    return kb


def admin_only(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ এই বটটি শুধু অ্যাডমিন ব্যবহার করতে পারবেন।")
        return False
    return True


@bot.message_handler(commands=["start", "help"])
def start(message):
    if not admin_only(message):
        return
    bot.send_message(
        message.chat.id,
        "🤖 <b>Auto Post Scheduler</b>\n\n"
        "• গ্রুপে বট যোগ করে, সেই গ্রুপে /addchat পাঠিয়ে রেজিস্টার করুন।\n"
        "• চ্যানেলের জন্য বটকে Admin করুন, তারপর ব্যক্তিগত চ্যাটে /addtarget @channelusername দিন।\n"
        "• New Scheduled Post থেকে পোস্ট, টার্গেট ও সময় বেছে নিন।\n\n"
        "সময় লিখবেন: <code>YYYY-MM-DD HH:MM</code>\n"
        f"টাইমজোন: <code>{TIMEZONE}</code>\n\n"
        "নোট: পোস্ট পাঠাতে বটের অনুমতি এবং স্ক্রিপ্ট চালু থাকা দরকার।",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


@bot.message_handler(commands=["addchat"])
def addchat(message):
    if not admin_only(message):
        return
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "গ্রুপের ভেতরে /addchat পাঠান।")
        return
    try:
        member = bot.get_chat_member(message.chat.id, ADMIN_ID)
        if member.status not in ("administrator", "creator", "member"):
            bot.reply_to(message, "অ্যাডমিন যাচাই করা যায়নি।")
            return
        add_target(message.chat)
        bot.reply_to(message, f"✅ গ্রুপ যুক্ত হয়েছে: {message.chat.title}\nID: {message.chat.id}")
    except Exception as exc:
        bot.reply_to(message, f"❌ গ্রুপ যোগ করা যায়নি: {str(exc)[:200]}")


@bot.message_handler(commands=["addtarget"])
def addtarget(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        bot.reply_to(message, "ব্যবহার: /addtarget @channelusername")
        return
    target_ref = parts[1].strip()
    try:
        chat = bot.get_chat(target_ref)
        try:
            member = bot.get_chat_member(chat.id, bot.get_me().id)
            if member.status not in ("administrator", "creator"):
                bot.reply_to(message, "❌ চ্যানেলে বটকে Admin করুন, তারপর আবার চেষ্টা করুন।")
                return
        except Exception:
            bot.reply_to(message, "❌ বটের চ্যানেল Admin অনুমতি যাচাই করা যায়নি।")
            return
        add_target(chat)
        bot.reply_to(message, f"✅ চ্যানেল/চ্যাট যুক্ত হয়েছে: {chat.title or target_ref}\nID: {chat.id}")
    except Exception as exc:
        bot.reply_to(message, f"❌ চ্যানেল যোগ করা যায়নি: {str(exc)[:200]}")


@bot.message_handler(func=lambda m: m.chat.type == "private" and (m.text or "") == "👥 Targets")
def show_targets(message):
    if not admin_only(message):
        return
    targets = get_targets()
    if not targets:
        bot.send_message(message.chat.id, "এখনো কোনো গ্রুপ/চ্যানেল যোগ করা হয়নি।")
        return
    lines = ["👥 <b>Registered targets</b>"]
    for t in targets:
        username = f" (@{t['username']})" if t["username"] else ""
        lines.append(f"• {t['title']}{username}\n  <code>{t['chat_id']}</code>")
    lines.append("\nটার্গেট সরাতে: /removetarget CHAT_ID")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=["removetarget"])
def remove_target(message):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        bot.reply_to(message, "ব্যবহার: /removetarget CHAT_ID")
        return
    try:
        chat_id = int(parts[1])
        with db() as conn:
            cur = conn.execute("DELETE FROM targets WHERE chat_id=?", (chat_id,))
        bot.reply_to(message, "✅ Target removed." if cur.rowcount else "Target পাওয়া যায়নি।")
    except ValueError:
        bot.reply_to(message, "CHAT_ID একটি সংখ্যায় দিন।")


@bot.message_handler(func=lambda m: m.chat.type == "private" and (m.text or "") == "➕ New Scheduled Post")
def new_post(message):
    if not admin_only(message):
        return
    if not get_targets():
        bot.send_message(message.chat.id, "আগে অন্তত একটি গ্রুপ/চ্যানেল যোগ করুন।")
        return
    user_state[ADMIN_ID] = "waiting_content"
    bot.send_message(
        message.chat.id,
        "যে পোস্টটি পাঠাতে চান সেটি এখন পাঠান। টেক্সট, ছবি, ভিডিও বা ক্যাপশনযুক্ত মিডিয়া পাঠাতে পারবেন।\n"
        "পোস্টটি ফরওয়ার্ড না করে সরাসরি পাঠান। বাতিল করতে Cancel চাপুন।"
    )


@bot.message_handler(func=lambda m: m.chat.type == "private" and (m.text or "") == "❌ Cancel")
def cancel(message):
    if not admin_only(message):
        return
    user_state.pop(ADMIN_ID, None)
    pending_content.pop(ADMIN_ID, None)
    bot.send_message(message.chat.id, "বাতিল করা হয়েছে।", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.chat.type == "private" and user_state.get(ADMIN_ID) == "waiting_content",
                     content_types=["text", "photo", "video", "document", "animation", "audio", "voice", "video_note"])
def receive_content(message):
    if not admin_only(message):
        return
    pending_content[ADMIN_ID] = (message.chat.id, message.message_id, (message.text or message.caption or "Media post")[:100])
    user_state[ADMIN_ID] = "waiting_targets"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("🌐 All Targets", "🎯 Choose Targets")
    kb.row("❌ Cancel")
    bot.send_message(message.chat.id, "পোস্ট কোথায় পাঠাবেন?", reply_markup=kb)


@bot.message_handler(func=lambda m: m.chat.type == "private" and user_state.get(ADMIN_ID) == "waiting_targets")
def receive_targets_choice(message):
    if not admin_only(message):
        return
    if message.text == "🌐 All Targets":
        mode = "all"
        target_ids = "[]"
    elif message.text == "🎯 Choose Targets":
        targets = get_targets()
        lines = ["নির্দিষ্ট টার্গেট বেছে নিতে তাদের ID কমা দিয়ে লিখুন:"]
        lines.extend([f"{t['title']}: {t['chat_id']}" for t in targets])
        user_state[ADMIN_ID] = "waiting_target_ids"
        bot.send_message(message.chat.id, "\n".join(lines))
        return
    else:
        bot.send_message(message.chat.id, "🌐 All Targets অথবা 🎯 Choose Targets বেছে নিন।")
        return
    pending_content[ADMIN_ID] = (*pending_content[ADMIN_ID], mode, target_ids)
    user_state[ADMIN_ID] = "waiting_time"
    bot.send_message(message.chat.id, "পোস্টের সময় লিখুন:\nYYYY-MM-DD HH:MM\nউদাহরণ: 2026-12-25 09:30")


@bot.message_handler(func=lambda m: m.chat.type == "private" and user_state.get(ADMIN_ID) == "waiting_target_ids")
def receive_target_ids(message):
    if not admin_only(message):
        return
    try:
        ids = [int(x.strip()) for x in message.text.split(",") if x.strip()]
        known = {int(t["chat_id"]) for t in get_targets()}
        if not ids or any(x not in known for x in ids):
            raise ValueError("Unknown target ID")
        import json
        pending_content[ADMIN_ID] = (*pending_content[ADMIN_ID], "selected", json.dumps(ids))
        user_state[ADMIN_ID] = "waiting_time"
        bot.send_message(message.chat.id, "পোস্টের সময় লিখুন:\nYYYY-MM-DD HH:MM\nউদাহরণ: 2026-12-25 09:30")
    except Exception:
        bot.send_message(message.chat.id, "ভুল ID। Registered target-এর ID কমা দিয়ে আবার লিখুন।")


@bot.message_handler(func=lambda m: m.chat.type == "private" and user_state.get(ADMIN_ID) == "waiting_time")
def receive_time(message):
    if not admin_only(message):
        return
    try:
        run_at = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        if run_at <= datetime.now(LOCAL_TZ):
            bot.send_message(message.chat.id, "সময় ভবিষ্যতের হতে হবে। আবার YYYY-MM-DD HH:MM ফরম্যাটে লিখুন।")
            return
        user_state[ADMIN_ID] = "waiting_repeat"
        pending_content[ADMIN_ID] = (*pending_content[ADMIN_ID], run_at.isoformat())
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.row("🔂 Once", "🕐 Hourly", "📆 Daily")
        kb.row("❌ Cancel")
        bot.send_message(message.chat.id, "পোস্ট কতবার চলবে?", reply_markup=kb)
    except ValueError:
        bot.send_message(message.chat.id, "সময়টি ঠিক নয়। ফরম্যাট: YYYY-MM-DD HH:MM")


@bot.message_handler(func=lambda m: m.chat.type == "private" and user_state.get(ADMIN_ID) == "waiting_repeat")
def receive_repeat(message):
    if not admin_only(message):
        return
    mapping = {"🔂 Once": "once", "🕐 Hourly": "hourly", "📆 Daily": "daily"}
    repeat_mode = mapping.get(message.text)
    if not repeat_mode:
        bot.send_message(message.chat.id, "Once, Hourly অথবা Daily বেছে নিন।")
        return
    import json
    source_chat_id, source_message_id, description, mode, target_ids, run_at = pending_content[ADMIN_ID]
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO posts(source_chat_id,source_message_id,description,target_mode,target_ids,run_at,
                                 repeat_mode,active,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (source_chat_id, source_message_id, description, mode, target_ids, run_at,
             repeat_mode, 1, datetime.now(LOCAL_TZ).isoformat())
        )
        post_id = cur.lastrowid
    row = get_post(post_id)
    schedule_row(row)
    user_state.pop(ADMIN_ID, None)
    pending_content.pop(ADMIN_ID, None)
    bot.send_message(
        message.chat.id,
        f"✅ Schedule তৈরি হয়েছে!\nPost ID: {post_id}\n"
        f"সময়: {run_at}\nRepeat: {repeat_mode}\nTargets: {'সব' if mode == 'all' else target_ids}",
        reply_markup=main_keyboard()
    )


@bot.message_handler(func=lambda m: m.chat.type == "private" and (m.text or "") == "📋 My Schedules")
def list_schedules(message):
    if not admin_only(message):
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        bot.send_message(message.chat.id, "এখনো কোনো schedule নেই।", reply_markup=main_keyboard())
        return
    lines = ["📋 <b>Schedules</b>"]
    for r in rows:
        status = "Active" if r["active"] else "Inactive"
        lines.append(f"#{r['id']} — {status} — {r['repeat_mode']}\n"
                     f"সময়: {r['run_at']}\nশেষ ফলাফল: {r['last_result'] or 'এখনো রান করেনি'}")
    bot.send_message(message.chat.id, "\n\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


def set_schedule_active(message, active):
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "ব্যবহার: /pause 123 অথবা /resume 123")
        return
    post_id = int(parts[1])
    row = get_post(post_id)
    if not row:
        bot.reply_to(message, "Schedule পাওয়া যায়নি।")
        return
    with db() as conn:
        conn.execute("UPDATE posts SET active=? WHERE id=?", (1 if active else 0, post_id))
    job_id = f"post_{post_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    if active:
        row = get_post(post_id)
        try:
            schedule_row(row)
            bot.reply_to(message, f"▶️ Schedule #{post_id} চালু করা হয়েছে।")
        except Exception as exc:
            bot.reply_to(message, f"Schedule চালু করা যায়নি: {str(exc)[:150]}")
    else:
        bot.reply_to(message, f"⏸ Schedule #{post_id} বন্ধ করা হয়েছে।")


@bot.message_handler(commands=["pause"])
def pause_cmd(message):
    set_schedule_active(message, False)


@bot.message_handler(commands=["resume"])
def resume_cmd(message):
    set_schedule_active(message, True)


@bot.message_handler(func=lambda m: m.chat.type == "private" and (m.text or "") == "⏸ Pause Schedule")
def pause_help(message):
    if admin_only(message):
        bot.send_message(message.chat.id, "বন্ধ করতে লিখুন: /pause POST_ID")


@bot.message_handler(func=lambda m: m.chat.type == "private" and (m.text or "") == "▶️ Resume Schedule")
def resume_help(message):
    if admin_only(message):
        bot.send_message(message.chat.id, "চালু করতে লিখুন: /resume POST_ID")


def main():
    init_db()
    scheduler.start()
    restore_jobs()
    logger.info("Auto Post Scheduler started.")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
