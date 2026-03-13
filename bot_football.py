"""
Bot de Telegram - Reenvío de goles con panel de control
Adaptado para Render.com + Uptime Robot (keepalive HTTP server incluido)

INSTALACIÓN LOCAL:
    pip install -r requirements.txt

DEPLOY EN RENDER:
    Ver README.md
"""

import re
import logging
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────
#        CARGAR VARIABLES DE ENTORNO
# ─────────────────────────────────────────

load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN")
SUBSCRIBE_LINK = os.getenv("SUBSCRIBE_LINK", "Suscríbete en t.me/iUniversoFootball")
PASSWORD       = os.getenv("PASSWORD", "gfa1234")
PORT           = int(os.getenv("PORT", 8080))   # Render inyecta $PORT automáticamente
CONFIG_FILE    = Path("/tmp/canal_config.json")  # /tmp es escribible en Render

# ─────────────────────────────────────────
#        PERSISTENCIA DE CANALES
# ─────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"source": None, "dest": None}

def save_config(data: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f)

canal_config = load_config()

# ─────────────────────────────────────────
#              LOGGING
# ─────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
#   SERVIDOR FLASK (keepalive para Render)
# ─────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    src = canal_config.get("source") or "no configurado"
    dst = canal_config.get("dest")   or "no configurado"
    return (
        f"<h2>✅ Bot activo</h2>"
        f"<p>Canal origen: <code>{src}</code></p>"
        f"<p>Canal destino: <code>{dst}</code></p>"
    ), 200

@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ─────────────────────────────────────────
#      ESTADOS DE CONVERSACIÓN (menú)
# ─────────────────────────────────────────

(
    STATE_PASSWORD,
    STATE_MENU,
    STATE_SET_SOURCE,
    STATE_SET_DEST,
    STATE_SEND_MSG,
) = range(5)

authenticated_users: set[int] = set()

# ─────────────────────────────────────────
#         LÓGICA DE TRANSFORMACIÓN
# ─────────────────────────────────────────

def transform_message(text: str) -> str | None:
    lines = text.strip().splitlines()

    first_line = lines[0].upper() if lines else ""
    goal_keywords = ["GOL", "GOAL", "GOLO", "⚽"]
    if not any(kw in first_line for kw in goal_keywords):
        return None

    score_line = scorer_line = assist_line = hashtag_line = global_line = None

    for line in lines:
        s = line.strip()

        # Línea del marcador del partido: tiene bandera O empieza con bandera/nombre de equipo
        # La distinguimos de la línea del global (que empieza con 🏆)
        if re.search(r'\d+\s*[xX×]\s*\d+', s) and not s.startswith("🏆") and not s.startswith("#"):
            # Es la línea de marcador real si contiene banderas de países junto a nombres de equipos
            # O simplemente no es una línea de hashtag/trofeo
            score_line = s

        if s.startswith("⚽"):
            scorer_line = s
        if s.startswith("🅰"):
            assist_line = s
        if s.startswith("#"):
            hashtag_line = s

        # Línea con 🏆: puede ser solo hashtag o hashtag + global (ej: #Libertadores 🌎 - 🇨🇴 3x2 🇺🇾)
        if s.startswith("🏆"):
            inner = s.replace("🏆", "").strip()
            # Separar el hashtag del marcador global si existe
            # Formato típico: "#Libertadores 🌎 - 🇨🇴 3x2 🇺🇾"
            global_match = re.search(r'(#\S+.*?)\s*-\s*(.+)', inner)
            if global_match:
                hashtag_part = global_match.group(1).strip()
                global_part  = global_match.group(2).strip()
                # Limpiar banderas del global y normalizar marcador
                global_clean = re.sub(r'[\U0001F1E0-\U0001F1FF]{2}', '', global_part).strip()
                global_clean = re.sub(r'\s{2,}', ' ', global_clean).strip()
                global_clean = re.sub(r'(\d+)\s*[xX×]\s*(\d+)', r'\1-\2', global_clean)
                hashtag_line = hashtag_part
                global_line  = f"(Global: {global_clean})"
            elif inner.startswith("#"):
                hashtag_line = inner

    # Limpiar línea del marcador del partido
    if score_line:
        clean = re.sub(r'[\U0001F1E0-\U0001F1FF]{2}', '', score_line).strip()
        clean = re.sub(r'\s{2,}', ' ', clean).strip()
        clean = re.sub(r'(\d+)\s*[xX×]\s*(\d+)', r'\1-\2', clean)
    else:
        clean = None

    # Construir hashtag final con global si existe
    hashtag_display = None
    if hashtag_line:
        if global_line:
            hashtag_display = f"{hashtag_line} {global_line}"
        else:
            hashtag_display = hashtag_line

    parts = []
    if clean:
        parts.append(f"<b>{clean}</b>")
    parts.append("")
    if scorer_line:
        parts.append(scorer_line)
    if assist_line:
        parts.append(assist_line)
    parts.append("")
    if hashtag_display:
        parts.append(f"<b>{hashtag_display}</b>")
    parts.append("")
    parts.append(f"<i>{SUBSCRIBE_LINK}</i>")

    return "\n".join(parts).strip()

# ─────────────────────────────────────────
#              HELPERS DE MENÚ
# ─────────────────────────────────────────

def build_main_menu() -> InlineKeyboardMarkup:
    src = canal_config.get("source") or "❌ No configurado"
    dst = canal_config.get("dest")   or "❌ No configurado"
    keyboard = [
        [InlineKeyboardButton(f"📥 Canal Origen: {src}",  callback_data="set_source")],
        [InlineKeyboardButton(f"📤 Canal Destino: {dst}", callback_data="set_dest")],
        [InlineKeyboardButton("✉️  Enviar mensaje manual",  callback_data="send_msg")],
        [InlineKeyboardButton("🔄 Recargar menú",           callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    text   = "🎛 *Panel de control*\n\nSelecciona una opción:"
    markup = build_main_menu()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

# ─────────────────────────────────────────
#          FLUJO: /start + contraseña
# ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id in authenticated_users:
        await show_menu(update, context)
        return STATE_MENU
    await update.message.reply_text(
        "🔐 *Bot protegido*\n\nIntroduce la contraseña para continuar:",
        parse_mode="Markdown"
    )
    return STATE_PASSWORD

async def check_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text == PASSWORD:
        authenticated_users.add(user_id)
        await update.message.reply_text("✅ Acceso concedido.")
        await show_menu(update, context)
        return STATE_MENU
    await update.message.reply_text("❌ Contraseña incorrecta. Inténtalo de nuevo:")
    return STATE_PASSWORD

# ─────────────────────────────────────────
#          FLUJO: BOTONES DEL MENÚ
# ─────────────────────────────────────────

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if user_id not in authenticated_users:
        await query.edit_message_text("🔐 Sesión expirada. Usa /start para volver a entrar.")
        return STATE_PASSWORD

    data = query.data

    if data == "set_source":
        await query.edit_message_text(
            "📥 *Canal Origen*\n\nEnvía el ID numérico del canal privado.\n"
            "Ejemplo: `-1001234567890`\n\n"
            "_(Añade @userinfobot al canal y escribe /id para obtenerlo)_",
            parse_mode="Markdown"
        )
        return STATE_SET_SOURCE

    elif data == "set_dest":
        await query.edit_message_text(
            "📤 *Canal Destino*\n\nEnvía el `@username` o ID numérico del canal.\n"
            "Ejemplo: `@iUniversoFootball` o `-1009876543210`",
            parse_mode="Markdown"
        )
        return STATE_SET_DEST

    elif data == "send_msg":
        await query.edit_message_text(
            "✉️ *Enviar mensaje manual*\n\n"
            "Escribe el mensaje que quieres publicar en el canal destino.\n\n"
            "_(Soporta texto normal y emojis)_",
            parse_mode="Markdown"
        )
        return STATE_SEND_MSG

    elif data == "refresh":
        await show_menu(update, context, edit=True)
        return STATE_MENU

    return STATE_MENU

async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.lstrip("-").isdigit():
        await update.message.reply_text(
            "⚠️ El ID debe ser un número negativo. Ej: `-1001234567890`\nInténtalo de nuevo:"
        )
        return STATE_SET_SOURCE
    canal_config["source"] = int(text)
    save_config(canal_config)
    await update.message.reply_text(f"✅ Canal origen guardado: `{text}`", parse_mode="Markdown")
    await show_menu(update, context)
    return STATE_MENU

async def set_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    canal_config["dest"] = int(text) if text.lstrip("-").isdigit() else text
    save_config(canal_config)
    await update.message.reply_text(f"✅ Canal destino guardado: `{text}`", parse_mode="Markdown")
    await show_menu(update, context)
    return STATE_MENU

async def send_manual_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dest = canal_config.get("dest")
    if not dest:
        await update.message.reply_text("⚠️ No hay canal destino configurado.")
        await show_menu(update, context)
        return STATE_MENU
    text = update.message.text.strip()
    try:
        await context.bot.send_message(chat_id=dest, text=text)
        await update.message.reply_text("✅ Mensaje enviado correctamente.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error al enviar: {e}")
    await show_menu(update, context)
    return STATE_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operación cancelada.")
    await show_menu(update, context)
    return STATE_MENU

# ─────────────────────────────────────────
#      HANDLER: POSTS DEL CANAL ORIGEN
# ─────────────────────────────────────────

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    source  = canal_config.get("source")
    dest    = canal_config.get("dest")

    if not source or not dest:
        return
    if not message or message.chat.id != source:
        return

    # message.text = mensajes solo texto
    # message.caption = mensajes con video/foto adjunto
    original_text = message.text or message.caption or ""
    if not original_text.strip():
        return

    transformed = transform_message(original_text)
    if transformed is None:
        logger.info(f"[IGNORADO] {original_text[:60]}…")
        return

    logger.info(f"[ENVIANDO]\n{transformed}\n{'─'*40}")

    if message.video:
        await context.bot.send_video(
            chat_id=dest,
            video=message.video.file_id,
            caption=transformed,
            parse_mode="HTML"
        )
    elif message.animation:
        await context.bot.send_animation(
            chat_id=dest,
            animation=message.animation.file_id,
            caption=transformed,
            parse_mode="HTML"
        )
    elif message.photo:
        await context.bot.send_photo(
            chat_id=dest,
            photo=message.photo[-1].file_id,
            caption=transformed,
            parse_mode="HTML"
        )
    else:
        await context.bot.send_message(
            chat_id=dest,
            text=transformed,
            parse_mode="HTML"
        )

# ─────────────────────────────────────────
#                  MAIN
# ─────────────────────────────────────────

def main():
    logger.info("Iniciando bot…")

    # Arrancar Flask en hilo separado (keepalive para Render + Uptime Robot)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Servidor keepalive activo en puerto {PORT}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            STATE_PASSWORD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)],
            STATE_MENU:       [CallbackQueryHandler(menu_callback)],
            STATE_SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source)],
            STATE_SET_DEST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, set_dest)],
            STATE_SEND_MSG:   [MessageHandler(filters.TEXT & ~filters.COMMAND, send_manual_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

    logger.info("Esperando mensajes (Ctrl+C para detener)")
    app.run_polling(allowed_updates=["message", "channel_post", "callback_query"])


if __name__ == "__main__":
    main()
