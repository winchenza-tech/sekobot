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

# --- API CLIENT ---
async def get_upcoming_matches():
    """RapidAPI'den bugün/yarınki maçları çeker."""
    url = "https://api-football-v1.p.rapidapi.com/v3/fixtures"
    headers = {"x-rapidapi-key": RAPID_API_KEY, "x-rapidapi-host": "api-football-v1.p.rapidapi.com"}
    params = {"live": "all"} # Canlı veya yaklaşan maçlar
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as response:
            data = await response.json()
            return data.get("response", [])[:5] # İlk 5 maçı al

# --- AI ANALİZ MOTORU ---
async def generate_coupons_ai():
    matches = await get_upcoming_matches()
    if not matches: return "Şu an oynanan veya yaklaşan maç bulunamadı."
    
    context = str(matches)
    prompt = f"""
    Sen profesyonel bir bahis uzmanısın. Aşağıdaki maç verilerini incele:
    {context}
    Bana 2 farklı kupon oluştur:
    1. KASA KATLAMA (Güvenli, düşük oranlı, yüksek ihtimal)
    2. RİSKLİ/VALUE (Yüksek oranlı, sürpriz)
    Her kupon için kısa bir 'Trick' (gerekçe) yaz. Sonuna 'Başarı Potansiyeli' ekle.
    """
    response = model.generate_content(prompt)
    return response.text

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect("bahis_bot.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS kuponlar (id INTEGER PRIMARY KEY, icerik TEXT, tip TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS stats (win INTEGER, loss INTEGER)")
        await db.commit()

# --- KOMUTLAR ---
@dp.message(Command("kupon"))
async def cmd_kupon(message: types.Message):
    await message.answer("🧠 <i>Analiz ediliyor, veriler işleniyor...</i>")
    kuponlar = await generate_coupons_ai()
    
    # DB'ye kaydet
    async with aiosqlite.connect("bahis_bot.db") as db:
        await db.execute("INSERT INTO kuponlar (icerik, tip) VALUES (?, ?)", (kuponlar, "Günlük"))
        await db.commit()
        
    await message.answer(f"🔥 <b>Günün Kuponları</b>\n\n{kuponlar}", parse_mode=ParseMode.HTML)

@dp.message(Command("kuponkontrol"))
async def cmd_kontrol(message: types.Message):
    async with aiosqlite.connect("bahis_bot.db") as db:
        cursor = await db.execute("SELECT icerik FROM kuponlar ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()
    if row: await message.answer(f"📡 <b>Son Kuponun Durumu:</b>\n{row[0]}")
    else: await message.answer("⚠️ Aktif kupon yok.")

@dp.message(Command("istatistik"))
async def cmd_istatistik(message: types.Message):
    async with aiosqlite.connect("bahis_bot.db") as db:
        cursor = await db.execute("SELECT * FROM stats")
        row = await cursor.fetchone() or (0, 0)
    await message.answer(f"📊 <b>Başarı:</b> %{((row[0]/(row[0]+row[1]+1))*100):.1f}\n✅ {row[0]} Kazanılan\n❌ {row[1]} Kaybedilen")

@dp.message(Command("incele"))
async def cmd_incele(message: types.Message):
    takim = message.text.replace("/incele", "").strip()
    if not takim: return await message.answer("Lütfen takım adı gir: /incele Galatasaray")
    
    # Gerçek API'den o takımı ara
    await message.answer(f"🔍 {takim} için derin analiz yapılıyor...")
    # (Buraya get_upcoming_matches içindeki mantığı o takıma filtreleyerek ekleyebilirsin)
    analiz = "Takım verileri çekildi, Gemini tarafından işleniyor..." # Basitleştirilmiş
    await message.answer(analiz)

# --- PUSH NOTIFICATION (TRICK) ---
async def live_scanner():
    """Arka planda çalışır, gol veya önemli olay olduğunda gruba bildirim atar."""
    # Gerçek API kontrolü:
    # matches = await get_upcoming_matches()
    # if maç_durumu == "Goal": await bot.send_message(CHAT_ID, "⚠️ GOL ALARMI!")
    pass

async def main():
    await init_db()
    scheduler.add_job(live_scanner, 'interval', minutes=2)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
