import os
import logging
import asyncio
import re
import json
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, filters, ContextTypes
)
from telegram.constants import ParseMode, ChatMemberStatus
import yt_dlp
import anthropic

# ─── تنظیمات اولیه ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_IDS       = list(map(int, os.environ.get("ADMIN_IDS", "").split(","))) if os.environ.get("ADMIN_IDS") else []

# ─── دیتابیس ساده (در حافظه) ──────────────────────────────────────
warn_count: dict[int, int]              = defaultdict(int)      # user_id → تعداد وارن
muted_users: dict[int, datetime]        = {}                     # user_id → زمان آنمیوت
banned_users: set[int]                  = set()
spam_tracker: dict[int, list[datetime]] = defaultdict(list)     # user_id → لیست زمان‌های پیام

# کلمات فیلتر‌شده (قابل تنظیم)
FILTERED_WORDS: list[str] = [
    "اسپم", "تبلیغ", "لینک_ممنوع",
    "spam", "advertisement", "xxx"
]

# تنظیمات ضد اسپم
SPAM_MAX_MESSAGES = 5    # حداکثر پیام
SPAM_WINDOW_SECS  = 10   # در این ثانیه
WARN_LIMIT        = 3    # بعد از ۳ وارن → بن

# ─── هلپرهای مشترک ────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def get_chat_admins(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    admins = await context.bot.get_chat_administrators(chat_id)
    return [a.user.id for a in admins]

async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    admins = await get_chat_admins(update.effective_chat.id, context)
    if update.effective_user.id not in admins and not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ فقط ادمین‌ها می‌تونن این دستور رو بزنن.")
        return False
    return True

def mention(user) -> str:
    name = user.full_name or user.username or str(user.id)
    return f'<a href="tg://user?id={user.id}">{name}</a>'

# ─── خوش‌آمدگویی ──────────────────────────────────────────────────
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📜 قوانین گروه", callback_data=f"rules_{member.id}"),
            InlineKeyboardButton("👋 معرفی خودم", callback_data=f"intro_{member.id}")
        ]])
        text = (
            f"🌟 <b>خوش اومدی {mention(member)}!</b>\n\n"
            f"🏠 به گروه ما خوش اومدی.\n"
            f"📋 لطفاً قوانین رو بخون و باهاشون موافقت کن.\n"
            f"❓ اگه سوالی داری بپرس، اینجا همه کمکت می‌کنن! 💪"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("rules_"):
        await query.message.reply_text(
            "📜 <b>قوانین گروه:</b>\n\n"
            "1️⃣ احترام به همه اعضا\n"
            "2️⃣ تبلیغات ممنوع\n"
            "3️⃣ اسپم ممنوع\n"
            "4️⃣ محتوای نامناسب ممنوع\n"
            "5️⃣ فحاشی ممنوع\n\n"
            "⚠️ نقض قوانین = وارن → بن",
            parse_mode=ParseMode.HTML
        )
    elif data.startswith("intro_"):
        await query.message.reply_text(
            "👋 می‌تونی خودت رو با یه پیام معرفی کنی!\n"
            "📝 اسم، علاقه‌مندی‌ها و اینکه از کجا اومدی رو بنویس."
        )

# ─── مدیریت (بن، اخراج، وارن، آنبن) ──────────────────────────────
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    target = update.message.reply_to_message
    if not target:
        await update.message.reply_text("↩️ روی پیام کسی ریپلای کن.")
        return
    user = target.from_user
    reason = " ".join(context.args) if context.args else "دلیل ذکر نشده"
    await context.bot.ban_chat_member(update.effective_chat.id, user.id)
    banned_users.add(user.id)
    await update.message.reply_text(
        f"🚫 {mention(user)} بن شد.\n📝 دلیل: {reason}",
        parse_mode=ParseMode.HTML
    )

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("⚠️ آی‌دی کاربر رو بده: /unban <user_id>")
        return
    uid = int(context.args[0])
    await context.bot.unban_chat_member(update.effective_chat.id, uid)
    banned_users.discard(uid)
    await update.message.reply_text(f"✅ کاربر {uid} آنبن شد.")

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    target = update.message.reply_to_message
    if not target:
        await update.message.reply_text("↩️ روی پیام کسی ریپلای کن.")
        return
    user = target.from_user
    await context.bot.ban_chat_member(update.effective_chat.id, user.id)
    await context.bot.unban_chat_member(update.effective_chat.id, user.id)  # kick = ban + unban
    await update.message.reply_text(
        f"👢 {mention(user)} از گروه اخراج شد.",
        parse_mode=ParseMode.HTML
    )

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    target = update.message.reply_to_message
    if not target:
        await update.message.reply_text("↩️ روی پیام کسی ریپلای کن.")
        return
    user = target.from_user
    warn_count[user.id] += 1
    wc = warn_count[user.id]
    reason = " ".join(context.args) if context.args else "دلیل ذکر نشده"

    if wc >= WARN_LIMIT:
        await context.bot.ban_chat_member(update.effective_chat.id, user.id)
        await update.message.reply_text(
            f"🚫 {mention(user)} بعد از {wc} وارن بن شد!\n📝 آخرین دلیل: {reason}",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"⚠️ {mention(user)} وارن گرفت! ({wc}/{WARN_LIMIT})\n📝 دلیل: {reason}",
            parse_mode=ParseMode.HTML
        )

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    target = update.message.reply_to_message
    if not target:
        await update.message.reply_text("↩️ روی پیام کسی ریپلای کن.")
        return
    user = target.from_user
    # مدت میوت (پیش‌فرض ۱ ساعت)
    minutes = int(context.args[0]) if context.args else 60
    until = datetime.now() + timedelta(minutes=minutes)
    muted_users[user.id] = until
    await context.bot.restrict_chat_member(
        update.effective_chat.id, user.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until
    )
    await update.message.reply_text(
        f"🔇 {mention(user)} برای {minutes} دقیقه میوت شد.",
        parse_mode=ParseMode.HTML
    )

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    target = update.message.reply_to_message
    if not target:
        await update.message.reply_text("↩️ روی پیام کسی ریپلای کن.")
        return
    user = target.from_user
    muted_users.pop(user.id, None)
    await context.bot.restrict_chat_member(
        update.effective_chat.id, user.id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )
    await update.message.reply_text(
        f"🔊 {mention(user)} آنمیوت شد.",
        parse_mode=ParseMode.HTML
    )

# ─── ضد اسپم + فیلتر کلمات ────────────────────────────────────────
async def filter_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user    = update.effective_user
    chat_id = update.effective_chat.id
    text    = update.message.text.lower()

    # ادمین‌ها فیلتر نمی‌شن
    admins = await get_chat_admins(chat_id, context)
    if user.id in admins or is_admin(user.id):
        await ai_reply(update, context)
        return

    # ── فیلتر کلمات ممنوع ──
    for word in FILTERED_WORDS:
        if word.lower() in text:
            await update.message.delete()
            warn_count[user.id] += 1
            await context.bot.send_message(
                chat_id,
                f"⚠️ {mention(user)} پیامت حذف شد (کلمه ممنوع). وارن: {warn_count[user.id]}/{WARN_LIMIT}",
                parse_mode=ParseMode.HTML
            )
            if warn_count[user.id] >= WARN_LIMIT:
                await context.bot.ban_chat_member(chat_id, user.id)
            return

    # ── ضد اسپم ──
    now = datetime.now()
    spam_tracker[user.id] = [
        t for t in spam_tracker[user.id]
        if (now - t).total_seconds() < SPAM_WINDOW_SECS
    ]
    spam_tracker[user.id].append(now)

    if len(spam_tracker[user.id]) > SPAM_MAX_MESSAGES:
        await update.message.delete()
        await context.bot.restrict_chat_member(
            chat_id, user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=now + timedelta(minutes=5)
        )
        await context.bot.send_message(
            chat_id,
            f"🚨 {mention(user)} به خاطر اسپم ۵ دقیقه میوت شد!",
            parse_mode=ParseMode.HTML
        )
        return

    # ── پاسخ هوش مصنوعی ──
    await ai_reply(update, context)

# ─── هوش مصنوعی ───────────────────────────────────────────────────
async def ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اگه ربات منشن یا ریپلای شد، با Claude پاسخ می‌ده"""
    msg  = update.message
    text = msg.text or ""
    bot_username = (await context.bot.get_me()).username

    mentioned  = f"@{bot_username}" in text
    is_reply   = msg.reply_to_message and msg.reply_to_message.from_user
    bot_replied = is_reply and msg.reply_to_message.from_user.username == bot_username

    if not (mentioned or bot_replied):
        return
    if not ANTHROPIC_KEY:
        return

    clean_text = text.replace(f"@{bot_username}", "").strip()
    if not clean_text:
        return

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 500,
            system     = (
                "تو یه دستیار هوشمند فارسی‌زبان در یه گروه تلگرامی هستی. "
                "پاسخ‌هات کوتاه، مفید و دوستانه باشه. "
                "از ایموجی‌های مناسب استفاده کن."
            ),
            messages   = [{"role": "user", "content": clean_text}]
        )
        reply = response.content[0].text
        await msg.reply_text(reply)
    except Exception as e:
        logger.error(f"AI error: {e}")

# ─── موزیک و مدیا ─────────────────────────────────────────────────
async def play_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🎵 نحوه استفاده:\n/play <نام آهنگ یا لینک یوتیوب>"
        )
        return

    query = " ".join(context.args)
    msg   = await update.message.reply_text(f"🔍 دنبال «{query}» می‌گردم...")

    ydl_opts = {
        "format"         : "bestaudio/best",
        "quiet"          : True,
        "noplaylist"     : True,
        "default_search" : "ytsearch1",
        "outtmpl"        : "/tmp/%(id)s.%(ext)s",
        "postprocessors" : [{
            "key"            : "FFmpegExtractAudio",
            "preferredcodec" : "mp3",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if "entries" in info:
                info = info["entries"][0]
            title    = info.get("title", "ناشناس")
            duration = info.get("duration", 0)
            filepath = f"/tmp/{info['id']}.mp3"

        await msg.edit_text(f"🎶 در حال ارسال: {title}")
        with open(filepath, "rb") as audio:
            await update.message.reply_audio(
                audio,
                title    = title,
                duration = duration,
                caption  = f"🎵 {title}"
            )
        await msg.delete()
        os.remove(filepath)
    except Exception as e:
        logger.error(f"Music error: {e}")
        await msg.edit_text("❌ دانلود موزیک ممکن نشد. لینک یا اسم دیگه‌ای امتحان کن.")

async def ytdl_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📹 نحوه استفاده:\n/video <لینک یوتیوب>")
        return

    url = context.args[0]
    msg = await update.message.reply_text("⬇️ در حال دانلود ویدیو...")

    ydl_opts = {
        "format"   : "best[filesize<50M]",
        "quiet"    : True,
        "outtmpl"  : "/tmp/%(id)s.%(ext)s",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            title    = info.get("title", "ویدیو")
            filepath = f"/tmp/{info['id']}.{info['ext']}"

        await msg.edit_text(f"📤 در حال ارسال: {title}")
        with open(filepath, "rb") as video:
            await update.message.reply_video(video, caption=f"🎬 {title}")
        await msg.delete()
        os.remove(filepath)
    except Exception as e:
        logger.error(f"Video error: {e}")
        await msg.edit_text("❌ دانلود ویدیو ممکن نشد. حجم باید زیر ۵۰ مگ باشه.")

# ─── دستورات عمومی ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 سلام! من ربات مدیریت گروه هستم.\n\n"
        "📋 /help — راهنمای دستورات"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>راهنمای دستورات:</b>\n\n"
        "👮 <b>مدیریت (فقط ادمین):</b>\n"
        "/ban [دلیل] — بن کاربر (ریپلای)\n"
        "/unban &lt;id&gt; — آنبن کاربر\n"
        "/kick — اخراج کاربر (ریپلای)\n"
        "/warn [دلیل] — وارن کاربر (ریپلای)\n"
        "/mute [دقیقه] — میوت کاربر (ریپلای)\n"
        "/unmute — آنمیوت کاربر (ریپلای)\n\n"
        "🎵 <b>موزیک و مدیا:</b>\n"
        "/play &lt;نام یا لینک&gt; — پخش موزیک\n"
        "/video &lt;لینک&gt; — دانلود ویدیو\n\n"
        "🤖 <b>هوش مصنوعی:</b>\n"
        "منشن کن یا روی پیامم ریپلای بزن\n\n"
        "ℹ️ /about — درباره ربات",
        parse_mode=ParseMode.HTML
    )

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>ربات مدیریت گروه</b>\n\n"
        "✅ مدیریت کامل (بن، اخراج، وارن، میوت)\n"
        "✅ ضد اسپم هوشمند\n"
        "✅ فیلتر کلمات ممنوع\n"
        "✅ خوش‌آمدگویی به اعضای جدید\n"
        "✅ پخش موزیک از یوتیوب\n"
        "✅ دانلود ویدیو\n"
        "✅ هوش مصنوعی Claude\n\n"
        "🛠 ساخته‌شده با Python + python-telegram-bot",
        parse_mode=ParseMode.HTML
    )

# ─── راه‌اندازی ───────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # دستورات
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("about",   about))
    app.add_handler(CommandHandler("ban",     ban_user))
    app.add_handler(CommandHandler("unban",   unban_user))
    app.add_handler(CommandHandler("kick",    kick_user))
    app.add_handler(CommandHandler("warn",    warn_user))
    app.add_handler(CommandHandler("mute",    mute_user))
    app.add_handler(CommandHandler("unmute",  unmute_user))
    app.add_handler(CommandHandler("play",    play_music))
    app.add_handler(CommandHandler("video",   ytdl_video))

    # پیام‌های جدید عضو
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # فیلتر پیام‌ها + هوش مصنوعی
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, filter_messages))

    # دکمه‌های اینلاین
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("🤖 ربات شروع به کار کرد!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
      
