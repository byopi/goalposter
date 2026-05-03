import os
import re
import json
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from telegram import (
    Bot, Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from video import process_video

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN     = os.environ["TOKEN"]
ADMIN_ID  = int(os.environ["ADMIN_ID"])   # tu Telegram user ID
SUBREDDIT = os.environ.get("SUBREDDIT", "soccer")
PORT      = int(os.environ.get("PORT", 8080))

CHANNELS_FILE = "/tmp/channels.json"

# ── Conversation states ───────────────────────────────────────────────────────
(
    STATE_MENU,
    STATE_CHOOSE_CHANNEL,
    STATE_TYPING_MESSAGE,
    STATE_ADD_CHANNEL,
    STATE_REMOVE_CHANNEL,
    STATE_CHOOSE_MIRROR_CHANNEL,
) = range(6)

# ── Reddit constants ──────────────────────────────────────────────────────────
VIDEO_DOMAINS = (
    "v.redd.it", "youtube.com", "youtu.be",
    "streamable.com", "streamja.com", "clippituser.tv",
    "medal.tv", "clips.twitch.tv", "gfycat.com",
    "streamain.com", "dubz.link", "dubz.co",
    "streamin.one", "streamin.me", "streamff.link", "streamin.link",
)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

REDDIT_HEADERS = {"User-Agent": "telegram-mirror-bot/1.0"}

seen_posts: set = set()

# ── Lógica de Parafraseo (Traducción) ─────────────────────────────────────────

def paraphrase_title(title: str) -> str:
    """Traduce términos de fútbol y limpia el formato del título"""
    # Diccionario de traducciones comunes
    subs = {
        r'\bvs\b': 'vs.',
        r'\bGoal\b': 'Gol',
        r'\bGolo\b': 'Gol',
        r'\bGreat Goal\b': 'GOLAZO',
        r'\bPenalty\b': 'Penalti',
        r'\bUruguay\b': 'Uruguay',
        r'\bGermany\b': 'Alemania',
        r'\bSpain\b': 'España',
        r'\bFrance\b': 'Francia',
        r'\bBrazil\b': 'Brasil',
        r'\bNetherlands\b': 'Países Bajos',
        r'\bItaly\b': 'Italia',
        r'\bEngland\b': 'Inglaterra',
        r'\bPortugal\b': 'Portugal',
        r'\bBelgium\b': 'Bélgica',
        r'\[(\d+)\s*-\s*(\d+)\]': r'(\1-\2)', # Cambia [1 - 0] por (1-0)
        r'(\d+)\s*[xX×]\s*(\d+)': r'\1-\2',    # Cambia 1x0 o 1x0 por 1-0
    }
    
    new_title = title
    for pattern, replacement in subs.items():
        new_title = re.sub(pattern, replacement, new_title, flags=re.IGNORECASE)
    
    # Eliminar texto entre corchetes innecesario (como nombres de proveedores)
    new_title = re.sub(r'\[.*?\]', '', new_title).strip()
    
    return new_title

# ── Channel storage ───────────────────────────────────────────────────────────

def load_channels() -> dict:
    try:
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"channels": {}, "mirror_channel": None}


def save_channels(data: dict):
    with open(CHANNELS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Health-check server ───────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


# ── Admin guard ───────────────────────────────────────────────────────────────

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.effective_message.reply_text("⛔ No autorizado.")
            return ConversationHandler.END
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Menu helpers ──────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Enviar mensaje a canal", callback_data="send_msg")],
        [InlineKeyboardButton("➕ Agregar canal",          callback_data="add_channel")],
        [InlineKeyboardButton("➖ Eliminar canal",         callback_data="remove_channel")],
        [InlineKeyboardButton("🔄 Canal del mirror",       callback_data="mirror_channel")],
        [InlineKeyboardButton("📋 Ver canales",            callback_data="list_channels")],
    ])


async def show_main_menu(target, context):
    text = "🎛 *Panel de control*\n\nElige una opción:"
    kb   = main_menu_keyboard()
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ── /menu command ─────────────────────────────────────────────────────────────

@admin_only
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update.message, context)
    return STATE_MENU


# ── Callback: main menu buttons ───────────────────────────────────────────────

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    data     = load_channels()
    channels = data["channels"]

    if query.data == "send_msg":
        if not channels:
            await query.edit_message_text(
                "No hay canales registrados. Agrega uno primero.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back")]])
            )
            return STATE_MENU
        rows = [[InlineKeyboardButton(ch, callback_data=f"ch::{ch}")] for ch in channels]
        rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="back")])
        await query.edit_message_text(
            "📢 *¿A qué canal quieres enviar el mensaje?*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return STATE_CHOOSE_CHANNEL

    if query.data == "add_channel":
        await query.edit_message_text(
            "➕ Escribe el ID o username del canal a agregar.\n\n"
            "Ejemplos: `@mi_canal` o `-1001234567890`\n\n"
            "⚠️ El bot debe ser administrador del canal.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back")]])
        )
        return STATE_ADD_CHANNEL

    if query.data == "remove_channel":
        if not channels:
            await query.edit_message_text(
                "No hay canales registrados.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back")]])
            )
            return STATE_MENU
        rows = [[InlineKeyboardButton(f"🗑 {ch}", callback_data=f"del::{ch}")] for ch in channels]
        rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="back")])
        await query.edit_message_text(
            "➖ *¿Qué canal quieres eliminar?*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return STATE_REMOVE_CHANNEL

    if query.data == "mirror_channel":
        if not channels:
            await query.edit_message_text(
                "No hay canales registrados. Agrega uno primero.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back")]])
            )
            return STATE_MENU
        current = data.get("mirror_channel", "ninguno")
        rows = [[InlineKeyboardButton(
            f"{'✅ ' if ch == current else ''}{ch}", callback_data=f"mirror::{ch}"
        )] for ch in channels]
        rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="back")])
        await query.edit_message_text(
            f"🔄 *Canal del mirror de Reddit*\nActual: `{current}`\n\nElige el canal:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return STATE_CHOOSE_MIRROR_CHANNEL

    if query.data == "list_channels":
        mirror = data.get("mirror_channel", "ninguno")
        lines  = "\n".join(
            f"• `{ch}`{'  🔄 mirror' if ch == mirror else ''}" for ch in channels
        ) if channels else "_No hay canales registrados._"
        await query.edit_message_text(
            f"📋 *Canales registrados:*\n\n{lines}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="back")]])
        )
        return STATE_MENU

    if query.data == "back":
        await show_main_menu(query, context)
        return STATE_MENU


# ── Choose channel → ask for message ─────────────────────────────────────────

async def cb_choose_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        await show_main_menu(query, context)
        return STATE_MENU

    channel = query.data.replace("ch::", "")
    context.user_data["target_channel"] = channel
    await query.edit_message_text(
        f"📝 Escribe el mensaje para *{channel}*:\n_(texto, foto, video o documento)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data="back")]])
    )
    return STATE_TYPING_MESSAGE


# ── Forward message to channel ────────────────────────────────────────────────

async def receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel = context.user_data.get("target_channel")
    if not channel:
        return ConversationHandler.END

    msg = update.message
    try:
        if msg.text:
            await context.bot.send_message(chat_id=channel, text=msg.text)
        elif msg.photo:
            await context.bot.send_photo(chat_id=channel, photo=msg.photo[-1].file_id, caption=msg.caption or "")
        elif msg.video:
            await context.bot.send_video(chat_id=channel, video=msg.video.file_id, caption=msg.caption or "", supports_streaming=True)
        elif msg.document:
            await context.bot.send_document(chat_id=channel, document=msg.document.file_id, caption=msg.caption or "")
        elif msg.animation:
            await context.bot.send_animation(chat_id=channel, animation=msg.animation.file_id, caption=msg.caption or "")
        else:
            await msg.reply_text("❌ Tipo de mensaje no soportado.")
            return STATE_TYPING_MESSAGE

        await msg.reply_text(
            f"✅ Enviado a *{channel}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="back")]])
        )
    except TelegramError as e:
        await msg.reply_text(f"❌ Error: `{e}`\n\nVerifica que el bot sea admin del canal.", parse_mode="Markdown")

    return STATE_MENU


# ── Add channel ───────────────────────────────────────────────────────────────

async def receive_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = update.message.text.strip()
    data       = load_channels()
    try:
        chat = await context.bot.get_chat(channel_id)
        key  = f"@{chat.username}" if chat.username else str(chat.id)
    except TelegramError as e:
        await update.message.reply_text(
            f"❌ No pude acceder al canal `{channel_id}`.\nError: `{e}`\n\n"
            "Verifica que el bot sea administrador.",
            parse_mode="Markdown",
        )
        return STATE_ADD_CHANNEL

    data["channels"][key] = {"name": key}
    if not data.get("mirror_channel"):
        data["mirror_channel"] = key
    save_channels(data)

    await update.message.reply_text(
        f"✅ Canal *{key}* agregado.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="back")]])
    )
    return STATE_MENU


# ── Remove channel ────────────────────────────────────────────────────────────

async def cb_remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        await show_main_menu(query, context)
        return STATE_MENU

    channel = query.data.replace("del::", "")
    data    = load_channels()

    if channel in data["channels"]:
        del data["channels"][channel]
        if data.get("mirror_channel") == channel:
            remaining = list(data["channels"].keys())
            data["mirror_channel"] = remaining[0] if remaining else None
        save_channels(data)

    await query.edit_message_text(
        f"🗑 Canal *{channel}* eliminado.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="back")]])
    )
    return STATE_MENU


# ── Set mirror channel ────────────────────────────────────────────────────────

async def cb_set_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        await show_main_menu(query, context)
        return STATE_MENU

    channel = query.data.replace("mirror::", "")
    data    = load_channels()
    data["mirror_channel"] = channel
    save_channels(data)

    await query.edit_message_text(
        f"✅ Mirror establecido: *{channel}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="back")]])
    )
    return STATE_MENU


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operación cancelada.")
    return ConversationHandler.END


# ── Reddit mirror logic ───────────────────────────────────────────────────────

def fetch_new_posts():
    url = f"https://www.reddit.com/r/{SUBREDDIT}/new.json?limit=10"
    try:
        resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()["data"]["children"]
    except Exception as e:
        logger.error(f"Error fetching Reddit JSON: {e}")
        return []


def is_media_post(post):
    flair   = (post.get("link_flair_text") or "").lower()
    url_low = post.get("url", "").lower().split("?")[0]
    if any(url_low.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return False
    return (
        "media" in flair
        or post.get("is_video", False)
        or any(d in url_low for d in VIDEO_DOMAINS)
    )


def get_mirror_links(post):
    links = []
    try:
        url  = f"https://www.reddit.com{post.get('permalink','')}.json?limit=50"
        resp = requests.get(url, headers=REDDIT_HEADERS, timeout=15)
        resp.raise_for_status()
        for c in resp.json()[1]["data"]["children"]:
            if c["data"].get("author", "").lower() == "automoderator":
                links.extend(re.findall(r"https?://\S+", c["data"].get("body", "")))
    except Exception as e:
        logger.warning(f"Mirror links error: {e}")
    return links


def cleanup(video):
    if not video:
        return
    for key in ("path", "thumb"):
        p = video.get(key)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


async def send_video_to_channel(bot: Bot, channel_id: str, video: dict, caption: str) -> bool:
    thumb_handle = None
    try:
        if video.get("thumb") and os.path.exists(video["thumb"]):
            thumb_handle = open(video["thumb"], "rb")
        with open(video["path"], "rb") as f:
            await bot.send_video(
                chat_id=channel_id, video=f, caption=caption,
                supports_streaming=True, duration=video.get("duration"),
                width=video.get("width"), height=video.get("height"),
                thumbnail=thumb_handle, parse_mode="Markdown",
            )
        return True
    except TelegramError as e:
        logger.error(f"Error sending to {channel_id}: {e}")
        return False
    finally:
        if thumb_handle:
            thumb_handle.close()


async def process_submission(bot: Bot, post: dict):
    data       = load_channels()
    channel_id = data.get("mirror_channel")
    if not channel_id:
        logger.warning("No mirror channel configured — skipping.")
        return

    # AQUI APLICAMOS EL PARAFRASEO AL TITULO
    original_title = post["title"]
    clean_title = paraphrase_title(original_title)
    
    caption = f"**{clean_title}**\n\nSuscríbete a [Universo Football](https://t.me/iuniversofootball)"
    links   = [post["url"]] + get_mirror_links(post)

    for attempt in range(5):
        for link in links:
            video = None
            try:
                video = await asyncio.get_event_loop().run_in_executor(None, process_video, link)
                if video and await send_video_to_channel(bot, channel_id, video, caption):
                    return
            except Exception as e:
                logger.warning(f"[attempt {attempt+1}] {link}: {e}")
            finally:
                cleanup(video)
        if attempt < 4:
            await asyncio.sleep(60)

    logger.warning(f"Gave up on: {original_title}")


async def check_new_posts(bot: Bot):
    logger.info(f"Polling r/{SUBREDDIT}...")
    try:
        posts = await asyncio.get_event_loop().run_in_executor(None, fetch_new_posts)
        for child in posts:
            post = child["data"]
            if post["id"] in seen_posts:
                continue
            seen_posts.add(post["id"])
            if is_media_post(post):
                asyncio.create_task(process_submission(bot, post))
    except Exception as e:
        logger.error(f"check_new_posts error: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info(f"Health-check server on port {PORT}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("menu", cmd_menu)],
        states={
            STATE_MENU:                  [CallbackQueryHandler(cb_menu)],
            STATE_CHOOSE_CHANNEL:        [CallbackQueryHandler(cb_choose_channel)],
            STATE_TYPING_MESSAGE:        [
                MessageHandler(
                    filters.TEXT | filters.PHOTO | filters.VIDEO |
                    filters.Document.ALL | filters.ANIMATION,
                    receive_message,
                ),
                CallbackQueryHandler(cb_menu),
            ],
            STATE_ADD_CHANNEL:           [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_add_channel),
                CallbackQueryHandler(cb_menu),
            ],
            STATE_REMOVE_CHANNEL:        [CallbackQueryHandler(cb_remove_channel)],
            STATE_CHOOSE_MIRROR_CHANNEL: [CallbackQueryHandler(cb_set_mirror)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_message=False,
    )
    app.add_handler(conv)

    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.add_job(check_new_posts, "interval", minutes=1, args=[app.bot])

    async def on_startup(application):
        posts = fetch_new_posts()
        for child in posts:
            seen_posts.add(child["data"]["id"])
        logger.info(f"Skipping {len(seen_posts)} already-seen posts.")
        scheduler.start()

    app.post_init = on_startup

    logger.info("Starting bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
