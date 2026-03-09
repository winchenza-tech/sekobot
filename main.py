import asyncio
import nest_asyncio
import os
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# --- 1. WEB SUNUCUSU (7/24 Çalışma İçin) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot 7/24 Görev Başında! (Sadece Sticker Kontrolü)"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# --- 2. AYARLAR ---
nest_asyncio.apply()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
AUTHORIZED_GROUP_ID = -1002241271415

# --- 3. YASAKLI STICKER SİLİCİ ---
async def delete_forbidden_stickers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sadece yetkili grupta çalış
    if update.effective_chat.id != AUTHORIZED_GROUP_ID: 
        return
    if not update.message or not update.message.sticker: 
        return
    
    set_name = update.message.sticker.set_name
    banned_packs = ["Dickss", "Trbanl", "FapPornVulgarKissLoveNsfwXXX"]
    
    # İstisna Kullanıcı
    BEYPAZARI_ID = 8561696979
    if update.effective_user.id == BEYPAZARI_ID and set_name == "Trbanl":
        return

    # Sticker yasaklı listedeyse sil
    if set_name in banned_packs:
        try: 
            await update.message.delete()
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Bu sticker yasaklı. Sildim gitti."
            )
        except Exception as e: 
            print(f"Sticker silinemedi: {e}")

# --- 4. ANA ÇALIŞTIRICI ---
async def main():
    keep_alive() # Web sunucusunu başlat
    
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Sadece Sticker filtresini ekliyoruz
    application.add_handler(MessageHandler(filters.Sticker.ALL, delete_forbidden_stickers))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    print("Bot aktif. Sadece sticker kontrolü devrede.")
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Kritik Hata: {e}")
