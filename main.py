import asyncio
import os
import aiohttp
import aiosqlite
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta

# --- SETUP ---
load_dotenv()
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RAPID_API_KEY = os.getenv("X_RAPIDAPI_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- API CLIENT (2 GÜNLÜK BÜLTEN) ---
async def get_matches_next_two_days():
    """Önümüzdeki 48 saatin maçlarını çeker."""
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    headers = {"x-rapidapi-key": RAPID_API_KEY, "x-rapidapi-host": "api-football-v1.p.rapidapi.com"}
    
    # Bugünden itibaren 2 gün sonrasına kadar olan maçlar
    # Not: API-Football parametrelerinde 'next' gün sayısını belirtebiliriz veya tarih filtreleyebiliriz.
    params = {"next": "20"} 
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as response:
            data = await response.json()
            return data.get("response", [])

# --- AI ANALİZ MOTORU (MİLLİ TAKIM DUYARLI) ---
async def generate_advanced_coupons():
    matches = await get_matches_next_two_days()
    if not matches: return "Önümüzdeki 48 saat için uygun maç verisi bulunamadı."
    
    # İlk 10 maçı özetleyip gönderelim
    context = "\n".join([f"{m['teams']['home']['name']} vs {m['teams']['away']['name']} ({m['league']['name']})" for m in matches[:10]])
    
    prompt = f"""
    Sen profesyonel bir bahis uzmanısın. Aşağıdaki 2 günlük bülteni incele:
    {context}
    
    Analiz kuralların:
    1. Hem Kulüp maçlarını hem de MİLLİ TAKIM maçlarını (Uluslar Ligi, Elemeler vb.) analiz et.
    2. Milli Takım maçlarında oyuncu motivasyonu, turnuva hedefi ve sakatlık riskini vurgula.
    3. Bana 2 kupon ver:
       - KASA KATLAMA: (Güvenli, düşük risk)
       - RİSKLİ/SİSTEM: (Sürpriz, yüksek kazanç)
    4. Her maç için 'Trick' (gerekçe) yaz.
    """
    response = model.generate_content(prompt)
    return response.text

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect("bahis_bot.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS kuponlar (id INTEGER PRIMARY KEY, icerik TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS stats (id INTEGER PRIMARY KEY, win INTEGER, loss INTEGER)")
        await db.execute("INSERT OR IGNORE INTO stats (id, win, loss) VALUES (1, 0, 0)")
        await db.commit()

# --- KOMUTLAR ---
@dp.message(Command("kupon"))
async def cmd_kupon(message: types.Message):
    wait_msg = await message.answer("⚽ 48 saatlik bülten taranıyor ve Milli Takım dinamikleri analiz ediliyor...")
    analiz = await generate_advanced_coupons()
    
    async with aiosqlite.connect("bahis_bot.db") as db:
        await db.execute("INSERT INTO kuponlar (icerik) VALUES (?)", (analiz,))
        await db.commit()
        
    await bot.edit_message_text(f"🔥 <b>2 Günlük Bahis Bülteni</b>\n\n{analiz}", message.chat.id, wait_msg.message_id, parse_mode=ParseMode.HTML)

@dp.message(Command("istatistik"))
async def cmd_istatistik(message: types.Message):
    async with aiosqlite.connect("bahis_bot.db") as db:
        cursor = await db.execute("SELECT win, loss FROM stats WHERE id=1")
        row = await cursor.fetchone()
    win, loss = row if row else (0, 0)
    await message.answer(f"📊 <b>Performans:</b>\n✅ {win} Başarılı\n❌ {loss} Başarısız")

@dp.message(Command("incele"))
async def cmd_incele(message: types.Message):
    takim = message.text.replace("/incele", "").strip()
    if not takim: return await message.answer("Takım adı gir: /incele Milli Takım Adı veya Kulüp Adı")
    
    # API'den spesifik takımı arama (Basitleştirilmiş)
    await message.answer(f"🔍 {takim} hakkında güncel veri ve milli takım performansı inceleniyor...")

# --- PUSH NOTIFICATION ---
async def push_bildirim():
    # Burada gol, kırmızı kart gibi anlık verileri kontrol edebilirsin
    pass

async def main():
    await init_db()
    scheduler.add_job(push_bildirim, 'interval', minutes=5)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
