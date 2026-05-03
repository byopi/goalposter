
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
PORT           = int(os.getenv("PORT", 8080))
CONFIG_FILE    = Path("/tmp/canal_config.json")

# ─────────────────────────────────────────
#        PERSISTENCIA DE CONFIGURACIÓN
# ─────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"source": None, "dest": None}
    return {"source": None, "dest": None}

def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

canal_config = load_config()

# ─────────────────────────────────────────
#              LOGGING & FLASK
# ─────────────────────────────────────────

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
@flask_app.route("/")
def home():
    return f"Bot Activo. Origen: {canal_config.get('source')} | Destino: {canal_config.get('dest')}", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ─────────────────────────────────────────
#         LÓGICA DE TRANSFORMACIÓN
# ─────────────────────────────────────────

def transform_message(text: str) -> str | None:
    if not text: return None
    
    # Traducción rápida de términos clave
    replacements = {"Golo": "Gol", "Golos": "Goles", "Jogo": "Partido", "França": "Francia"}
    for pt, es in replacements.items():
        text = re.sub(re.escape(pt), es, text, flags=re.IGNORECASE)
    
    # Verificación flexible de Goles (según image_0af387.jpg)
    if not any(kw in text.upper() for kw in ["GOL", "⚽"]):
        return None

    # Limpiar líneas de marcador (Ej: 3x1 -> 3-1)
    text = re.sub(r'(\d+)\s*[xX×]\s*(\d+)', r'\1-\2', text)
    
    # Eliminar banderas/emojis de países para dejarlo más limpio si se desea
    # text = re.sub(r'[\U0001F1E0-\U0001F1FF]{2}', '', text)

    return f"{text}\n\n<i>{SUBSCRIBE_LINK}</i>"

# ─────────────────────────────────────────
#          PANEL DE CONTROL (MENÚ)
# ─────────────────────────────────────────

(STATE_PASSWORD, STATE_MENU, STATE_SET_SOURCE, STATE_SET_DEST) = range(4)
auth_users = set()

def get_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Origen: {canal_config.get('source')}", callback_data="src")],
        [InlineKeyboardButton(f"📤 Destino: {canal_config.get('dest')}", callback_data="dst")],
        [InlineKeyboardButton("🔄 Refrescar", callback_data="ref")]
    ])

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id in auth_users:
        await u.message.reply_text("🎛 Panel:", reply_markup=get_kb())
        return STATE_MENU
    await u.message.reply_text("🔐 Contraseña:")
    return STATE_PASSWORD

async def check_pw(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.message.text == PASSWORD:
        auth_users.add(u.effective_user.id)
        await u.message.reply_text("✅ Acceso OK", reply_markup=get_kb())
        return STATE_MENU
    return STATE_PASSWORD

async def menu_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == "src":
        await q.edit_message_text("Envía el ID de Origen:")
        return STATE_SET_SOURCE
    elif q.data == "dst":
        await q.edit_message_text("Envía el @Username o ID Destino:")
        return STATE_SET_DEST
    await q.edit_message_text("🎛 Panel:", reply_markup=get_kb())
    return STATE_MENU

async def save_src(u: Update, c: ContextTypes.DEFAULT_TYPE):
    canal_config["source"] = u.message.text.strip()
    save_config(canal_config)
    await u.message.reply_text("✅ Origen Guardado", reply_markup=get_kb())
    return STATE_MENU

async def save_dst(u: Update, c: ContextTypes.DEFAULT_TYPE):
    canal_config["dest"] = u.message.text.strip()
    save_config(canal_config)
    await u.message.reply_text("✅ Destino Guardado", reply_markup=get_kb())
    return STATE_MENU

# ─────────────────────────────────────────
#    REENVÍO AUTOMÁTICO (FIX MULTIMEDIA)
# ─────────────────────────────────────────

async def forward_logic(u: Update, c: ContextTypes.DEFAULT_TYPE):
    # Detectar el mensaje sin importar si es post de canal o mensaje de grupo
    m = u.channel_post or u.message
    if not m: return

    src = str(canal_config.get("source"))
    dst = canal_config.get("dest")

    # Log para depuración en Render
    logger.info(f"Mensaje de ID: {m.chat.id}")

    if str(m.chat.id) != src or not dst:
        return

    # Extraer texto de cualquier fuente (mensaje normal o pie de foto/video)
    raw_text = m.text or m.caption or ""
    final_text = transform_message(raw_text)

    if not final_text: return

    try:
        # Prioridad a Video -> Foto -> Texto
        if m.video:
            await c.bot.send_video(dst, m.video.file_id, caption=final_text, parse_mode="HTML")
        elif m.photo:
            await c.bot.send_photo(dst, m.photo[-1].file_id, caption=final_text, parse_mode="HTML")
        elif m.animation: # Para los GIFs
            await c.bot.send_animation(dst, m.animation.file_id, caption=final_text, parse_mode="HTML")
        else:
            await c.bot.send_message(dst, final_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error al reenviar: {e}")

# ─────────────────────────────────────────
#                    MAIN
# ─────────────────────────────────────────

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_pw)],
            STATE_MENU: [CallbackQueryHandler(menu_cb)],
            STATE_SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_src)],
            STATE_SET_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_dst)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(conv)
    
    # Escucha TODO (fotos, videos, texto) de canales y grupos
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, forward_logic))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
