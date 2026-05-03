"""
Bot de Telegram - Reenvío de goles con panel de control
Adaptado para José - Universo Football
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
PORT           = int(os.getenv("PORT", 8080))
CONFIG_FILE    = Path("/tmp/canal_config.json")

# ─────────────────────────────────────────
#        PERSISTENCIA DE CANALES
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
#              LOGGING
# ─────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
#    SERVIDOR FLASK (keepalive para Render)
# ─────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    src = canal_config.get("source") or "no configurado"
    dst = canal_config.get("dest")   or "no configurado"
    return f"<h2>✅ Bot activo</h2><p>Origen ID: {src}</p><p>Destino: {dst}</p>", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ─────────────────────────────────────────
#      ESTADOS DE CONVERSACIÓN (menú)
# ─────────────────────────────────────────

(STATE_PASSWORD, STATE_MENU, STATE_SET_SOURCE, STATE_SET_DEST, STATE_SEND_MSG) = range(5)
authenticated_users: set[int] = set()

# ─────────────────────────────────────────
#         TRADUCCIÓN PORTUGUÉS → ESPAÑOL
# ─────────────────────────────────────────

PT_ES_WORDS = {
    "Uruguai": "Uruguay", "Alemanha": "Alemania", "Holanda": "Países Bajos",
    "Países Baixos": "Países Bajos", "Franca": "Francia", "França": "Francia",
    "Espanha": "España", "Suíça": "Suiza", "Suecia": "Suecia", "Suécia": "Suecia",
    "Noruega": "Noruega", "Dinamarca": "Dinamarca", "Bélgica": "Bélgica",
    "Polónia": "Polonia", "Polonia": "Polonia", "Croácia": "Croacia",
    "Sérvia": "Serbia", "Hungria": "Hungría", "Eslováquia": "Eslovaquia",
    "Eslovénia": "Eslovenia", "Escocia": "Escocia", "Escócia": "Escocia",
    "Gales": "Gales", "Irlanda": "Irlanda", "Turquia": "Turquía",
    "Grécia": "Grecia", "Albânia": "Albania", "Romênia": "Rumanía",
    "Romenia": "Rumanía", "Ucrânia": "Ucrania", "Rússia": "Rusia",
    "Russia": "Rusia", "Áustria": "Austria", "Republica Checa": "República Checa",
    "República Checa": "República Checa", "Marrocos": "Marruecos", "Egipto": "Egipto",
    "Egito": "Egipto", "Argélia": "Argelia", "Argelia": "Argelia", "Nigéria": "Nigeria",
    "Senegal": "Senegal", "Costa do Marfim": "Costa de Marfil", "Camarões": "Camerún",
    "Gana": "Ghana", "Guiné": "Guinea", "Tunísia": "Túnez", "Africa do Sul": "Sudáfrica",
    "África do Sul": "Sudáfrica", "Japão": "Japón", "Coreia do Sul": "Corea del Sur",
    "Coreia do Norte": "Corea del Norte", "China": "China", "Arábia Saudita": "Arabia Saudita",
    "Irão": "Irán", "Iran": "Irán", "Austrália": "Australia", "Nova Zelândia": "Nueva Zelanda",
    "Estados Unidos": "Estados Unidos", "EUA": "EE.UU.", "México": "México",
    "Costa Rica": "Costa Rica", "Panamá": "Panamá", "Honduras": "Honduras",
    "Guatemala": "Guatemala", "Jamaica": "Jamaica", "Trinidad e Tobago": "Trinidad y Tobago",
    "Colômbia": "Colombia", "Bolívia": "Bolivia", "Paraguai": "Paraguay",
    "Venezuela": "Venezuela", "Equador": "Ecuador", "Perú": "Perú", "Peru": "Perú",
    "Chile": "Chile", "Argentina": "Argentina", "Brasil": "Brasil", "Brazil": "Brasil",
    "Golo": "Gol", "Golos": "Goles", "Assistência": "Asistencia", "Penalti": "Penal",
    "Pênalti": "Penal", "Autogolo": "Autogol", "Intervalo": "Descanso", "Jogo": "Partido",
}

PT_ES_HASHTAGS = {
    "#RepescagemUEFA": "#RepescaUEFA",
    "#RepescagemIntercontinental": "#RepescaFIFA",
}

def translate_pt_es(text: str) -> str:
    for pt, es in PT_ES_HASHTAGS.items():
        text = re.sub(re.escape(pt), es, text, flags=re.IGNORECASE)
    for pt, es in PT_ES_WORDS.items():
        text = re.sub(r'(?<![\w#])' + re.escape(pt) + r'(?![\w])', es, text, flags=re.IGNORECASE)
    return text

# ─────────────────────────────────────────
#                HANDLERS PANEL
# ─────────────────────────────────────────

def build_main_menu():
    src = canal_config.get("source") or "❌ No configurado"
    dst = canal_config.get("dest")   or "❌ No configurado"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Origen: {src}", callback_data="set_source")],
        [InlineKeyboardButton(f"📤 Destino: {dst}", callback_data="set_dest")],
        [InlineKeyboardButton("✉️ Manual", callback_data="send_msg")],
        [InlineKeyboardButton("🔄 Recargar", callback_data="refresh")]
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in authenticated_users:
        await update.message.reply_text("🎛 Panel:", reply_markup=build_main_menu())
        return STATE_MENU
    await update.message.reply_text("🔐 Contraseña:")
    return STATE_PASSWORD

async def check_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() == PASSWORD:
        authenticated_users.add(update.effective_user.id)
        await update.message.reply_text("✅ Acceso concedido.", reply_markup=build_main_menu())
        return STATE_MENU
    await update.message.reply_text("❌ Incorrecta.")
    return STATE_PASSWORD

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "set_source":
        await query.edit_message_text("📥 Envía el ID del canal (ej: -100...):")
        return STATE_SET_SOURCE
    elif query.data == "set_dest":
        await query.edit_message_text("📤 Envía el @username o ID destino:")
        return STATE_SET_DEST
    elif query.data == "send_msg":
        await query.edit_message_text("✉️ Escribe el mensaje:")
        return STATE_SEND_MSG
    elif query.data == "refresh":
        await query.edit_message_text("🎛 Panel:", reply_markup=build_main_menu())
    return STATE_MENU

async def set_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    canal_config["source"] = text # Lo guardamos como string para evitar errores de casting
    save_config(canal_config)
    await update.message.reply_text(f"✅ Origen: `{text}`", reply_markup=build_main_menu(), parse_mode="Markdown")
    return STATE_MENU

async def set_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    canal_config["dest"] = text
    save_config(canal_config)
    await update.message.reply_text(f"✅ Destino: `{text}`", reply_markup=build_main_menu(), parse_mode="Markdown")
    return STATE_MENU

async def send_manual_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dest = canal_config.get("dest")
    try:
        await context.bot.send_message(chat_id=dest, text=update.message.text)
        await update.message.reply_text("✅ Enviado.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return STATE_MENU

# ─────────────────────────────────────────
#    HANDLER CRÍTICO: DETECCIÓN DE MENSAJES
# ─────────────────────────────────────────

async def handle_any_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if not message: return

    source = str(canal_config.get("source"))
    dest = canal_config.get("dest")
    
    # DEBUG LOG: Esto te dirá en Render qué ID está viendo el bot realmente
    logger.info(f"DEBUG: Mensaje recibido del Chat ID: {message.chat.id}")

    if str(message.chat.id) != source or not dest:
        return

    original_text = message.text or message.caption or ""
    transformed = transform_message(original_text)
    
    if not transformed: return

    try:
        if message.video:
            await context.bot.send_video(dest, message.video.file_id, caption=transformed, parse_mode="HTML")
        elif message.photo:
            await context.bot.send_photo(dest, message.photo[-1].file_id, caption=transformed, parse_mode="HTML")
        else:
            await context.bot.send_message(dest, transformed, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error reenviando: {e}")

# ─────────────────────────────────────────
#                    MAIN
# ─────────────────────────────────────────

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            STATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, check_password)],
            STATE_MENU: [CallbackQueryHandler(menu_callback)],
            STATE_SET_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source)],
            STATE_SET_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_dest)],
            STATE_SEND_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_manual_message)],
        },
        fallbacks=[CommandHandler("start", cmd_start)]
    )

    app.add_handler(conv)
    
    # IMPORTANTE: Captura TODO (fotos, videos, texto) de canales y grupos
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_any_post))

    logger.info("Bot arrancado. Revisa logs para ver los IDs de entrada.")
    # Update.ALL_TYPES es vital para recibir mensajes de otros bots en canales
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
