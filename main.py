import asyncio
import logging
import os
import sqlite3
import aiosqlite
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from cachetools import TTLCache
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- SETUP ---
load_dotenv()
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Cache (Hız ve Maliyet Tasarrufu)
incele_cache = TTLCache(maxsize=100, ttl=900) # 15 dk

# --- VERİTABANI YÖNETİMİ ---
async def init_db():
    async with aiosqlite.connect("bahis_bot.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS kuponlar (id INTEGER PRIMARY KEY, icerik TEXT, durum TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS istatistik (kazanma INTEGER, kaybetme INTEGER)")
        # Başlangıç değerleri
        cursor = await db.execute("SELECT count(*) FROM istatistik")
        if (await cursor.fetchone())[0] == 0:
            await db.execute("INSERT INTO istatistik VALUES (0, 0)")
        await db.commit()

# --- ANALİZ MOTORU (GEMINI) ---
async def ai_analiz_yap(takim_adi):
    prompt = f"""
    Sen profesyonel bir bahis uzmanısın. {takim_adi} hakkında derin bir analiz yap.
    1. Sakatlıklar ve Kadro durumu (Tahmini)
    2. Zemin ve Hava durumu etkisi
    3. Stratejik 'Trick' (Örn: İlk yarı golü, korner baskısı vb.)
    Analizi 3 madde halinde, emojili ve profesyonel bir dille yaz.
    """
    response = model.generate_content(prompt)
    return response.text

# --- KOMUTLAR ---
@dp.message(Command("start", "yardim"))
async def cmd_start(message: types.Message):
    await message.answer("🤖 <b>Bahis Analiz Botuna Hoş Geldin!</b>\n\n"
                         "📋 <b>Komutlar:</b>\n"
                         "/incele [Takım] - Uzman Analiz\n"
                         "/kupon - Günlük Analizli Kupon\n"
                         "/kuponkontrol - Anlık Durum\n"
                         "/istatistik - ROI Raporu")

@dp.message(Command("incele"))
async def cmd_incele(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return await message.answer("⚠️ Lütfen takım adı girin: /incele Fenerbahçe")
    
    takim = args[1]
    if takim in incele_cache:
        await message.answer(f"⚡ (Önbellekten):\n{incele_cache[takim]}")
    else:
        analiz = await ai_analiz_yap(takim)
        incele_cache[takim] = analiz
        await message.answer(analiz, parse_mode=ParseMode.HTML)

@dp.message(Command("kupon"))
async def cmd_kupon(message: types.Message):
    # Basit bir simülasyon
    kupon_text = "🎯 <b>Günün Uzman Kuponu:</b>\n1. Arsenal - Chelsea: MS 1\n2. Real Madrid - Barca: 2.5 Üst"
    async with aiosqlite.connect("bahis_bot.db") as db:
        await db.execute("INSERT INTO kuponlar (icerik, durum) VALUES (?, ?)", (kupon_text, "Bekliyor"))
        await db.commit()
    await message.answer(kupon_text, parse_mode=ParseMode.HTML)

@dp.message(Command("kuponkontrol"))
async def cmd_kontrol(message: types.Message):
    async with aiosqlite.connect("bahis_bot.db") as db:
        cursor = await db.execute("SELECT icerik FROM kuponlar ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()
    if row: await message.answer(f"📡 <b>Son Durum:</b>\n{row[0]}\n(Canlı takip aktif...)")
    else: await message.answer("⚠️ Aktif kupon yok.")

@dp.message(Command("istatistik"))
async def cmd_istatistik(message: types.Message):
    async with aiosqlite.connect("bahis_bot.db") as db:
        cursor = await db.execute("SELECT * FROM istatistik")
        data = await cursor.fetchone()
    await message.answer(f"📊 <b>Başarı İstatistiği</b>\n✅ Kazanan: {data[0]}\n❌ Kaybeden: {data[1]}")

# --- ARKA PLAN (PUSH BİLDİRİM) ---
async def push_bildirim():
    # Burası otomatik çalışır
    try:
        # Örnek: Eğer aktif bir kanal ID'n varsa buraya yaz
        # await bot.send_message(CHAT_ID, "⚠️ GOL ALARMI: Arsenal maçı baskı kuruyor!")
        pass
    except Exception as e:
        logging.error(f"Push hatası: {e}")

async def main():
    await init_db()
    scheduler.add_job(push_bildirim, 'interval', minutes=5)
    scheduler.start()
    print("Bot aktif...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
