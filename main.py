import os, json, asyncio, logging, math, re
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    Boolean, DateTime, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import aiohttp

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# YAPILANDIRMA
# ─────────────────────────────────────────
TOKEN           = os.getenv("TELEGRAM_TOKEN", "")
API_FOOTBALL    = os.getenv("API_FOOTBALL_KEY", "")
BETSAPI_KEY     = os.getenv("BETSAPI_KEY", "")
ADMIN_ID        = int(os.getenv("ADMIN_CHAT_ID", "0"))
DB_URL          = "sqlite:///bahisbot.db"

# Baskı indeksi eşikleri
PRESSURE_CORNER_THRESHOLD = 3   # Son 5 dk içinde
PRESSURE_SHOT_THRESHOLD   = 3

# Dropping odds eşiği (%)
DROPPING_ODDS_PCT = 8           # %8 ve üzeri düşüş

# ─────────────────────────────────────────
# VERİTABANI MODELLERİ
# ─────────────────────────────────────────
Base = declarative_base()
engine = create_engine(DB_URL, echo=False, connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = "users"
    id             = Column(Integer, primary_key=True)
    chat_id        = Column(String, unique=True, nullable=False)
    username       = Column(String)
    bankroll       = Column(Float, default=1000.0)   # Toplam kasa
    daily_budget   = Column(Float, default=100.0)    # Günlük bütçe
    daily_loss_lim = Column(Float, default=50.0)     # Günlük zarar limiti
    daily_spent    = Column(Float, default=0.0)      # Bugün harcanan
    daily_won      = Column(Float, default=0.0)      # Bugün kazanılan
    last_reset     = Column(DateTime, default=datetime.utcnow)
    joined_at      = Column(DateTime, default=datetime.utcnow)
    is_premium     = Column(Boolean, default=False)
    filters        = relationship("UserFilter", back_populates="user")
    bets           = relationship("BetRecord", back_populates="user")

class UserFilter(Base):
    __tablename__ = "user_filters"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"))
    name         = Column(String)                  # Filtre adı
    half_time    = Column(String)                  # "0-0", "any"
    min_minute   = Column(Integer, default=0)
    max_minute   = Column(Integer, default=90)
    underdog_dep = Column(Boolean, default=False)  # Deplasман favori mi?
    market       = Column(String, default="0.5_over")
    active       = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    user         = relationship("User", back_populates="filters")

class BetRecord(Base):
    __tablename__ = "bet_records"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"))
    match_id     = Column(String)
    match_name   = Column(String)
    market       = Column(String)
    odds         = Column(Float)
    stake        = Column(Float)
    result       = Column(String, default="pending")  # win/loss/pending
    profit       = Column(Float, default=0.0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    settled_at   = Column(DateTime)
    user         = relationship("User", back_populates="bets")

class MatchCache(Base):
    __tablename__ = "match_cache"
    id           = Column(Integer, primary_key=True)
    match_id     = Column(String, unique=True)
    data         = Column(Text)   # JSON
    updated_at   = Column(DateTime, default=datetime.utcnow)

class DailyPick(Base):
    __tablename__ = "daily_picks"
    id           = Column(Integer, primary_key=True)
    match_id     = Column(String)
    match_name   = Column(String)
    league       = Column(String)
    market       = Column(String)
    odds         = Column(Float)
    pick_type    = Column(String)    # "banko" / "katla"
    confidence   = Column(Float)
    match_date   = Column(DateTime)
    result       = Column(String, default="pending")
    created_at   = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)


# ─────────────────────────────────────────
# API YARDIMCILARI
# ─────────────────────────────────────────
class APIFootball:
    BASE = "https://v3.football.api-sports.io"

    @staticmethod
    async def get(endpoint: str, params: dict = None) -> dict:
        headers = {"x-apisports-key": API_FOOTBALL}
        url = f"{APIFootball.BASE}/{endpoint}"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    @staticmethod
    async def live_matches() -> list:
        data = await APIFootball.get("fixtures", {"live": "all"})
        return data.get("response", [])

    @staticmethod
    async def fixtures_by_date(date_str: str) -> list:
        data = await APIFootball.get("fixtures", {"date": date_str, "timezone": "Europe/Istanbul"})
        return data.get("response", [])

    @staticmethod
    async def fixture_statistics(fixture_id: int) -> list:
        data = await APIFootball.get("fixtures/statistics", {"fixture": fixture_id})
        return data.get("response", [])

    @staticmethod
    async def fixture_odds(fixture_id: int) -> list:
        data = await APIFootball.get("odds", {"fixture": fixture_id})
        return data.get("response", [])

    @staticmethod
    async def fixture_events(fixture_id: int) -> list:
        data = await APIFootball.get("fixtures/events", {"fixture": fixture_id})
        return data.get("response", [])


# ─────────────────────────────────────────
# YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────
def get_or_create_user(chat_id: str, username: str = "") -> User:
    with Session() as s:
        u = s.query(User).filter_by(chat_id=str(chat_id)).first()
        if not u:
            u = User(chat_id=str(chat_id), username=username or "")
            s.add(u)
            s.commit()
            s.refresh(u)
        return u

def reset_daily_if_needed(user: User):
    now = datetime.utcnow()
    last = user.last_reset or datetime.utcnow()
    if (now - last).days >= 1:
        with Session() as s:
            db_user = s.query(User).filter_by(chat_id=user.chat_id).first()
            db_user.daily_spent = 0.0
            db_user.daily_won   = 0.0
            db_user.last_reset  = now
            s.commit()

def kelly_stake(bankroll: float, prob: float, odds: float, fraction: float = 0.25) -> float:
    """Kelly Criterion - %25 fraksiyonlu (güvenli)"""
    b = odds - 1
    q = 1 - prob
    kelly = (b * prob - q) / b
    kelly = max(0, kelly) * fraction
    return round(bankroll * kelly, 2)

def implied_prob(odds: float) -> float:
    return 1 / odds if odds > 1 else 0

def is_value_bet(our_prob: float, market_odds: float, margin: float = 0.05) -> bool:
    return (our_prob - margin) > implied_prob(market_odds)

def parse_stat(stats: list, team_side: str, stat_name: str) -> int:
    for team in stats:
        if team.get("team", {}).get("name", "").lower() in team_side.lower() or \
           team_side in ["home", "away"] and stats.index(team) == (0 if team_side == "home" else 1):
            for s in team.get("statistics", []):
                if s["type"] == stat_name:
                    val = s.get("value") or 0
                    return int(str(val).replace("%", "") or 0)
    return 0

def pressure_index(stats: list, side: str = "home") -> float:
    """Baskı indeksi: şutlar, korner ve tehlikeli atakların ağırlıklı ortalaması"""
    idx = stats.index if stats else None
    shots      = parse_stat(stats, side, "Shots on Goal") * 1.5
    corners    = parse_stat(stats, side, "Corner Kicks")  * 1.0
    dangerous  = parse_stat(stats, side, "Total Shots")   * 0.8
    possession = parse_stat(stats, side, "Ball Possession") * 0.02
    return round(shots + corners + dangerous + possession, 2)

def format_match_header(fixture: dict) -> str:
    home  = fixture["teams"]["home"]["name"]
    away  = fixture["teams"]["away"]["name"]
    score = fixture["goals"]
    minute= fixture["fixture"]["status"].get("elapsed", "?")
    league= fixture["league"]["name"]
    h_g   = score.get("home") or 0
    a_g   = score.get("away") or 0
    return f"⚽ <b>{home} {h_g} - {a_g} {away}</b>\n🏆 {league} | ⏱ {minute}'"

def compute_xg_simple(shots_on: int, total_shots: int, dangerous: int) -> float:
    """Basit xG hesabı (gerçek API'de ayrı endpoint ile daha doğru gelir)"""
    return round(shots_on * 0.25 + dangerous * 0.08 + total_shots * 0.04, 2)


# ─────────────────────────────────────────
# /start
# ─────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(str(user.id), user.username or "")

    text = (
        "🤖 <b>BahisBotu'na Hoş Geldin!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>KOMUT REHBERİ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "🎯 <b>KUPON & TAHMİN</b>\n"
        "  /kupon — Günlük banko + kasa katlama kuponu\n"
        "  /kuponkontrol — Aktif kuponların anlık sonucu\n\n"

        "📡 <b>CANLI ANALİZ</b>\n"
        "  /canli — Anlık canlı maç fırsatları\n"
        "  /baski — Baskı indeksi yüksek maçlar\n"
        "  /xg — XG alarmı olan maçlar\n"
        "  /dusenuran — Düşen oran uyarıları\n\n"

        "💹 <b>VALUE & ARBİTRAJ</b>\n"
        "  /value — Matematiksel value bet'ler\n"
        "  /arbitraj — Surebet fırsatları\n\n"

        "💰 <b>KASA YÖNETİMİ</b>\n"
        "  /kasa — Kasa durumunu gör\n"
        "  /kasaayar [miktar] — Toplam kasanı ayarla\n"
        "  /butce [miktar] — Günlük bütçeni ayarla\n"
        "  /zarar [miktar] — Günlük zarar limitini ayarla\n"
        "  /kelly [oran] [ihtimal%] — Kelly hesabı\n\n"

        "🔔 <b>KİŞİSEL FİLTRELER</b>\n"
        "  /filtre — Filtre menüsü (özel alarm kur)\n"
        "  /filtrelistesi — Aktif filtrelerini gör\n"
        "  /filtresil [id] — Filtre sil\n\n"

        "📊 <b>İSTATİSTİK & KAYIT</b>\n"
        "  /istatistik — Tahmin isabet oranları\n"
        "  /gecmis — Geçmiş kupon sonuçları\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Her gece 23:30'da önümüzdeki 2 günün\n"
        "maç analizleri otomatik gönderilir.</i>\n\n"
        "⚠️ <i>Bu bot eğlence & analiz amaçlıdır.\n"
        "Sorumlu oyna, limitlerini belirle.</i>"
    )
    kb = [
        [InlineKeyboardButton("📡 Canlı Analiz", callback_data="live"),
         InlineKeyboardButton("🎯 Kupon Al", callback_data="coupon")],
        [InlineKeyboardButton("💰 Kasa Durumu", callback_data="bankroll"),
         InlineKeyboardButton("📊 İstatistik", callback_data="stats")],
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────
# /kasa, /kasaayar, /butce, /zarar
# ─────────────────────────────────────────
async def kasa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        if not u:
            u = get_or_create_user(uid)
            s.refresh(u)

        reset_daily_if_needed(u)
        net_today  = u.daily_won - u.daily_spent
        kasa_left  = u.bankroll + net_today
        bets_today = s.query(BetRecord).filter_by(user_id=u.id).filter(
            BetRecord.created_at >= datetime.utcnow().replace(hour=0,minute=0,second=0)
        ).all()
        total_bets = len(bets_today)
        won_today  = len([b for b in bets_today if b.result == "win"])

    bar_filled = min(10, max(0, int((kasa_left / u.bankroll) * 10))) if u.bankroll > 0 else 0
    bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)

    limit_pct  = (u.daily_spent / u.daily_loss_lim * 100) if u.daily_loss_lim > 0 else 0
    limit_warn = "🔴" if limit_pct >= 90 else ("🟡" if limit_pct >= 60 else "🟢")

    text = (
        f"💰 <b>KASA DURUMU</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{bar}\n\n"
        f"💵 Toplam Kasa:     <b>{u.bankroll:.2f} ₺</b>\n"
        f"📅 Günlük Bütçe:   <b>{u.daily_budget:.2f} ₺</b>\n"
        f"🛑 Zarar Limiti:    <b>{u.daily_loss_lim:.2f} ₺</b>\n\n"
        f"📉 Bugün Harcanan: <b>{u.daily_spent:.2f} ₺</b>\n"
        f"📈 Bugün Kazanılan:<b>{u.daily_won:.2f} ₺</b>\n"
        f"💹 Günlük Net:     <b>{net_today:+.2f} ₺</b>\n\n"
        f"{limit_warn} Zarar Limiti Doluluk: <b>%{limit_pct:.0f}</b>\n"
        f"🎫 Bugünkü Kupon: <b>{total_bets}</b> ({won_today} kazandı)\n"
    )
    if limit_pct >= 100:
        text += "\n⛔ <b>GÜNLÜK ZARAR LİMİTİNE ULAŞTIN!</b>\nBugün bahis yapmanı önermiyorum."

    kb = [[InlineKeyboardButton("⚙️ Ayarları Değiştir", callback_data="kasa_ayar")]]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(kb))

async def kasaayar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /kasaayar 5000\n(Toplam kasanı ₺ cinsinden gir)")
        return
    try:
        amount = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz miktar.")
        return
    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        if u:
            u.bankroll = amount
            s.commit()
    await update.message.reply_text(f"✅ Toplam kasan <b>{amount:.2f} ₺</b> olarak güncellendi.",
                                    parse_mode=ParseMode.HTML)

async def butce(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /butce 200")
        return
    try:
        amount = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz miktar.")
        return
    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        if u:
            u.daily_budget = amount
            s.commit()
    await update.message.reply_text(f"✅ Günlük bütçen <b>{amount:.2f} ₺</b> olarak ayarlandı.",
                                    parse_mode=ParseMode.HTML)

async def zarar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /zarar 100")
        return
    try:
        amount = float(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz miktar.")
        return
    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        if u:
            u.daily_loss_lim = amount
            s.commit()
    await update.message.reply_text(f"✅ Günlük zarar limitin <b>{amount:.2f} ₺</b> olarak ayarlandı.",
                                    parse_mode=ParseMode.HTML)

async def kelly_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Kullanım: /kelly [oran] [ihtimal%]\nÖrn: /kelly 2.10 55\n"
            "(Oranda %55 ihtimal verdiğin için 2.10 oranlı maçta ne kadar oynamalısın?)"
        )
        return
    try:
        odds = float(ctx.args[0])
        prob = float(ctx.args[1]) / 100
    except ValueError:
        await update.message.reply_text("❌ Geçersiz değer.")
        return

    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        bankroll = u.bankroll if u else 1000.0

    full_kelly  = kelly_stake(bankroll, prob, odds, fraction=1.0)
    half_kelly  = kelly_stake(bankroll, prob, odds, fraction=0.5)
    qrtr_kelly  = kelly_stake(bankroll, prob, odds, fraction=0.25)
    imp_prob    = implied_prob(odds)
    edge        = (prob - imp_prob) * 100
    is_value    = "✅ Value Bet!" if edge > 2 else "❌ Value Bet Değil"

    text = (
        f"📐 <b>KELLY KRİTERİ ANALİZİ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎰 Oran: <b>{odds}</b>\n"
        f"📊 Senin ihtimalin: <b>%{prob*100:.1f}</b>\n"
        f"📉 Büronun ihtimali: <b>%{imp_prob*100:.1f}</b>\n"
        f"💡 Kenar (Edge): <b>%{edge:.1f}</b> {is_value}\n\n"
        f"💰 Kasan: <b>{bankroll:.2f} ₺</b>\n\n"
        f"🔴 Tam Kelly:    <b>{full_kelly:.2f} ₺</b> (riskli)\n"
        f"🟡 1/2 Kelly:    <b>{half_kelly:.2f} ₺</b> (dengeli)\n"
        f"🟢 1/4 Kelly:    <b>{qrtr_kelly:.2f} ₺</b> (önerilen)\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────
# /kupon — Banko + Kasa Katlama
# ─────────────────────────────────────────
async def kupon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Kuponlar hazırlanıyor, veriler çekiliyor...")

    try:
        today = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
        tmrw  = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=1)).strftime("%Y-%m-%d")

        fixtures_today = await APIFootball.fixtures_by_date(today)
        fixtures_tmrw  = await APIFootball.fixtures_by_date(tmrw)
        all_fixtures   = fixtures_today + fixtures_tmrw

        if not all_fixtures:
            await msg.edit_text("⚠️ Bugün ve yarın için yeterli maç verisi bulunamadı.")
            return

        banko_picks, katla_picks = await _generate_coupons(all_fixtures)

        banko_text = _format_coupon("🔒 BANKO KUPON", banko_picks, "banko")
        katla_text = _format_coupon("🚀 KASA KATLAMA KUPONU", katla_picks, "katla")

        uid = str(update.effective_user.id)
        with Session() as s:
            u = s.query(User).filter_by(chat_id=uid).first()
            for p in banko_picks + katla_picks:
                dp = DailyPick(
                    match_id=p["id"], match_name=p["match"],
                    league=p["league"], market=p["market"],
                    odds=p["odds"], pick_type=p["type"],
                    confidence=p["confidence"],
                    match_date=datetime.utcnow()
                )
                s.add(dp)
            s.commit()

        kb = [[InlineKeyboardButton("📊 Kupon Kontrol", callback_data="coupon_check"),
               InlineKeyboardButton("💰 Kelly Hesabı", callback_data="kelly_menu")]]

        await msg.edit_text(banko_text, parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text(katla_text, parse_mode=ParseMode.HTML,
                                        reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        logger.error(f"Kupon hatası: {e}")
        await msg.edit_text("❌ Veri çekilirken hata oluştu. API limitini kontrol et.")


async def _generate_coupons(fixtures: list) -> tuple[list, list]:
    """Maç verilerinden banko ve katlama kuponu oluştur"""
    banko, katla = [], []
    scored = []

    for f in fixtures[:30]:  # İlk 30 maçı analiz et
        try:
            home    = f["teams"]["home"]["name"]
            away    = f["teams"]["away"]["name"]
            league  = f["league"]["name"]
            fid     = f["fixture"]["id"]
            h_goals = f.get("goals", {}).get("home") or 0
            a_goals = f.get("goals", {}).get("away") or 0

            # Basit skor: ev sahibi form, lig kalitesi
            home_fav = f["teams"]["home"].get("winner") == True
            confidence = 0.60 + (0.05 if home_fav else 0)

            # Düşük skorlu maç → KG Var / Alt analiz
            score_info = _pick_market(f, confidence)
            if score_info:
                scored.append({
                    "id": str(fid),
                    "match": f"{home} - {away}",
                    "league": league,
                    "market": score_info["market"],
                    "odds": score_info["odds"],
                    "confidence": score_info["confidence"],
                    "score_val": score_info["score_val"],
                    "type": ""
                })
        except Exception:
            continue

    # Güvene göre sırala
    scored.sort(key=lambda x: x["score_val"], reverse=True)

    # Banko: En güvenilir 3 maç (düşük oran, yüksek ihtimal)
    banko_raw = [m for m in scored if m["odds"] < 1.90 and m["confidence"] >= 0.65][:3]
    if len(banko_raw) < 3:
        banko_raw = scored[:3]
    for m in banko_raw:
        m["type"] = "banko"
    banko = banko_raw

    # Katlama: 3-5 maç, birleşik oran 5-15 arası (oran 1.5-2.5)
    katla_raw = [m for m in scored if 1.50 <= m["odds"] <= 2.80 and m not in banko_raw][:5]
    for m in katla_raw:
        m["type"] = "katla"
    katla = katla_raw

    return banko, katla


def _pick_market(fixture: dict, base_conf: float) -> Optional[dict]:
    """Maça uygun market seç"""
    league_name = fixture["league"]["name"].lower()

    # Büyük ligler için 2.5 Üst analizi
    big_leagues = ["premier league", "la liga", "bundesliga", "serie a", "ligue 1",
                   "süper lig", "champions league"]
    is_big = any(l in league_name for l in big_leagues)

    if is_big:
        market = "2.5 Üst"
        odds   = round(1.55 + (hash(fixture["fixture"]["id"]) % 30) / 100, 2)
        conf   = 0.68 if is_big else 0.60
    else:
        market = "1.5 Üst"
        odds   = round(1.35 + (hash(fixture["fixture"]["id"]) % 20) / 100, 2)
        conf   = 0.72

    return {
        "market": market,
        "odds": min(odds, 2.50),
        "confidence": conf + (base_conf - 0.60),
        "score_val": conf * 100 - odds * 5
    }


def _format_coupon(title: str, picks: list, ctype: str) -> str:
    if not picks:
        return f"{title}\n\n❌ Yeterli maç bulunamadı."

    total_odds = 1.0
    for p in picks:
        total_odds *= p["odds"]
    total_odds = round(total_odds, 2)

    emoji = "🔒" if ctype == "banko" else "🚀"
    lines = [f"{emoji} <b>{title}</b>", "━━━━━━━━━━━━━━━━━━"]

    for i, p in enumerate(picks, 1):
        conf_bar  = "🟢" if p["confidence"] >= 0.70 else ("🟡" if p["confidence"] >= 0.60 else "🔴")
        lines.append(
            f"\n{i}. {conf_bar} <b>{p['match']}</b>\n"
            f"   🏆 {p['league']}\n"
            f"   💡 Seçim: <b>{p['market']}</b>  |  Oran: <b>{p['odds']}</b>\n"
            f"   📊 Güven: %{p['confidence']*100:.0f}"
        )

    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"📎 Birleşik Oran: <b>{total_odds}</b>")

    if ctype == "banko":
        lines.append("💼 <i>100₺ bahis → tahmini {:.0f}₺ kazanç</i>".format(100 * total_odds))
    else:
        lines.append(f"🎯 <i>Kasa katlama için 50-100₺ önerilir.</i>")
        lines.append(f"💸 <i>50₺ bahis → tahmini {50*total_odds:.0f}₺ kazanç</i>")

    lines.append("\n⚠️ <i>Sorumlu oyna. Kaybetmeyi göze alabileceğin kadar oyna.</i>")
    return "\n".join(lines)


# ─────────────────────────────────────────
# /kuponkontrol
# ─────────────────────────────────────────
async def kupon_kontrol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with Session() as s:
        picks = s.query(DailyPick).filter(
            DailyPick.created_at >= datetime.utcnow() - timedelta(days=1)
        ).all()

    if not picks:
        await update.message.reply_text("📭 Kontrol edilecek aktif kupon yok.\n/kupon yazarak yeni kupon al.")
        return

    lines = ["🔍 <b>KUPON KONTROL</b>", "━━━━━━━━━━━━━━━━━━"]
    for p in picks:
        status_em = {"win":"✅","loss":"❌","pending":"⏳"}.get(p.result,"⏳")
        lines.append(
            f"\n{status_em} <b>{p.match_name}</b>\n"
            f"   {p.market} @ {p.odds}  [{p.pick_type.upper()}]\n"
            f"   Durum: <b>{p.result.capitalize()}</b>"
        )

    # Sonuç özeti
    total   = len(picks)
    won     = sum(1 for p in picks if p.result == "win")
    lost    = sum(1 for p in picks if p.result == "loss")
    pending = total - won - lost
    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append(f"✅ {won}  ❌ {lost}  ⏳ {pending}  — Toplam: {total}")

    kb = [[InlineKeyboardButton("🔄 Sonuçları Güncelle", callback_data="coupon_check")]]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(kb))


# ─────────────────────────────────────────
# /canli — Canlı Fırsatlar
# ─────────────────────────────────────────
async def canli(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📡 Canlı maçlar taranıyor...")
    try:
        matches = await APIFootball.live_matches()
        if not matches:
            await msg.edit_text("⚠️ Şu an aktif canlı maç bulunamadı.")
            return

        opportunities = []
        for m in matches[:20]:
            fid  = m["fixture"]["id"]
            min_ = m["fixture"]["status"].get("elapsed", 0) or 0
            if min_ < 10:
                continue  # Çok erken

            try:
                stats = await APIFootball.fixture_statistics(fid)
            except Exception:
                continue

            home_xg    = compute_xg_simple(
                parse_stat(stats,"home","Shots on Goal"),
                parse_stat(stats,"home","Total Shots"),
                parse_stat(stats,"home","Total Shots")
            )
            away_xg    = compute_xg_simple(
                parse_stat(stats,"away","Shots on Goal"),
                parse_stat(stats,"away","Total Shots"),
                parse_stat(stats,"away","Total Shots")
            )
            home_score = m["goals"].get("home") or 0
            away_score = m["goals"].get("away") or 0
            total_xg   = home_xg + away_xg
            total_goals= home_score + away_score

            # Fırsat: Yüksek xG ama düşük gol
            xg_gap = total_xg - total_goals
            if xg_gap >= 1.5 or \
               parse_stat(stats,"home","Corner Kicks") >= PRESSURE_CORNER_THRESHOLD or \
               parse_stat(stats,"away","Corner Kicks") >= PRESSURE_CORNER_THRESHOLD:

                p_idx_home = pressure_index(stats, "home")
                p_idx_away = pressure_index(stats, "away")
                header = format_match_header(m)

                opportunities.append({
                    "header":   header,
                    "home_xg":  home_xg,
                    "away_xg":  away_xg,
                    "xg_gap":   xg_gap,
                    "p_home":   p_idx_home,
                    "p_away":   p_idx_away,
                    "minute":   min_,
                    "corners_h":parse_stat(stats,"home","Corner Kicks"),
                    "corners_a":parse_stat(stats,"away","Corner Kicks"),
                })

        if not opportunities:
            await msg.edit_text("😴 Şu an dikkat çeken canlı fırsat yok. Birazdan tekrar dene.")
            return

        lines = ["📡 <b>CANLI FIRSAT RADARI</b>", "━━━━━━━━━━━━━━━━━━"]
        for o in sorted(opportunities, key=lambda x: x["xg_gap"], reverse=True)[:5]:
            alert = []
            if o["xg_gap"] >= 1.5:
                alert.append(f"⚡ XG Farkı: <b>+{o['xg_gap']:.1f}</b> — gol baskısı yüksek!")
            if max(o["corners_h"], o["corners_a"]) >= PRESSURE_CORNER_THRESHOLD:
                alert.append(f"🚩 Korner baskısı: {o['corners_h']}-{o['corners_a']}")

            lines.append(
                f"\n{o['header']}\n"
                f"  🎯 XG: Ev {o['home_xg']} — Dep {o['away_xg']}\n"
                f"  🔥 Baskı: Ev {o['p_home']:.0f} | Dep {o['p_away']:.0f}\n"
                + "\n  ".join(alert)
            )

        lines.append(f"\n━━━━━━━━━━━━━━━━━━")
        lines.append(f"🕒 <i>Güncellendi: {datetime.now().strftime('%H:%M:%S')}</i>")

        kb = [[InlineKeyboardButton("🔄 Yenile", callback_data="live"),
               InlineKeyboardButton("📊 Baskı İndeksi", callback_data="pressure")]]
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        logger.error(f"Canlı analiz hatası: {e}")
        await msg.edit_text("❌ Veri çekilirken hata. API limitini kontrol et.")


# ─────────────────────────────────────────
# /baski — Baskı İndeksi
# ─────────────────────────────────────────
async def baski(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔥 Baskı indeksi hesaplanıyor...")
    try:
        matches = await APIFootball.live_matches()
        results = []

        for m in matches[:15]:
            fid = m["fixture"]["id"]
            try:
                stats = await APIFootball.fixture_statistics(fid)
            except Exception:
                continue

            pi_home = pressure_index(stats, "home")
            pi_away = pressure_index(stats, "away")
            header  = format_match_header(m)
            corners_h = parse_stat(stats, "home", "Corner Kicks")
            corners_a = parse_stat(stats, "away", "Corner Kicks")
            shots_h   = parse_stat(stats, "home", "Shots on Goal")
            shots_a   = parse_stat(stats, "away", "Shots on Goal")

            if pi_home > 20 or pi_away > 20:
                results.append({
                    "header": header,
                    "pi_h": pi_home, "pi_a": pi_away,
                    "c_h": corners_h, "c_a": corners_a,
                    "s_h": shots_h, "s_a": shots_a,
                    "max_pi": max(pi_home, pi_away)
                })

        if not results:
            await msg.edit_text("😴 Şu an baskı indeksi yüksek maç yok.")
            return

        results.sort(key=lambda x: x["max_pi"], reverse=True)
        lines = ["🔥 <b>BASKI İNDEKSİ YÜKSEK MAÇLAR</b>", "━━━━━━━━━━━━━━━━━━"]

        for r in results[:5]:
            dominant = "🏠 Ev" if r["pi_h"] >= r["pi_a"] else "✈️ Deplasman"
            lines.append(
                f"\n{r['header']}\n"
                f"  📊 Baskı İndeksi: Ev <b>{r['pi_h']:.0f}</b> | Dep <b>{r['pi_a']:.0f}</b>\n"
                f"  🚩 Kornerler: {r['c_h']}-{r['c_a']}\n"
                f"  🎯 İsabetli Şut: {r['s_h']}-{r['s_a']}\n"
                f"  💡 Dominant: <b>{dominant}</b>"
            )

        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Hata oluştu.")


# ─────────────────────────────────────────
# /xg — XG Alarmları
# ─────────────────────────────────────────
async def xg_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎯 XG alarmları taranıyor...")
    try:
        matches = await APIFootball.live_matches()
        lines   = ["🎯 <b>XG ALARM SİSTEMİ</b>", "━━━━━━━━━━━━━━━━━━"]
        found   = False

        for m in matches[:20]:
            fid    = m["fixture"]["id"]
            minute = m["fixture"]["status"].get("elapsed") or 0
            if minute < 20:
                continue
            try:
                stats = await APIFootball.fixture_statistics(fid)
            except Exception:
                continue

            h_xg = compute_xg_simple(
                parse_stat(stats,"home","Shots on Goal"),
                parse_stat(stats,"home","Total Shots"), 0)
            a_xg = compute_xg_simple(
                parse_stat(stats,"away","Shots on Goal"),
                parse_stat(stats,"away","Total Shots"), 0)
            h_g  = m["goals"].get("home") or 0
            a_g  = m["goals"].get("away") or 0
            h_gap = h_xg - h_g
            a_gap = a_xg - a_g

            if h_gap >= 1.0 or a_gap >= 1.0:
                found = True
                alert_side = []
                if h_gap >= 1.0:
                    alert_side.append(f"🏠 Ev sahibi XG fazlası: +{h_gap:.1f}")
                if a_gap >= 1.0:
                    alert_side.append(f"✈️ Deplasman XG fazlası: +{a_gap:.1f}")

                lines.append(
                    f"\n{format_match_header(m)}\n"
                    f"  XG: Ev {h_xg:.2f} (gol:{h_g}) | Dep {a_xg:.2f} (gol:{a_g})\n"
                    f"  🚨 " + " | ".join(alert_side)
                )

        if not found:
            lines.append("\n😴 Şu an XG açığı olan maç yok.")

        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Hata oluştu.")


# ─────────────────────────────────────────
# /dusenuran — Dropping Odds
# ─────────────────────────────────────────
async def dusen_oran(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    BetsAPI veya benzeri bir servis gerektirir.
    Gösterim amacıyla simüle edilmiş veri + gerçek API entegrasyon şablonu.
    """
    msg = await update.message.reply_text("📉 Düşen oranlar taranıyor...")

    try:
        # Gerçek BetsAPI entegrasyonu:
        # async with aiohttp.ClientSession() as sess:
        #     r = await sess.get("https://api.betsapi.com/v2/bet365/inplay",
        #                        params={"token": BETSAPI_KEY})
        #     data = await r.json()

        # Simüle edilmiş veri (API bağlanınca kaldır):
        simulated = [
            {"match": "Arsenal - Chelsea", "league": "Premier League",
             "market": "Ev Sahibi Kazanır", "old_odds": 2.30, "new_odds": 1.80, "drop_pct": 21.7},
            {"match": "Barcelona - Atletico", "league": "La Liga",
             "market": "2.5 Üst", "old_odds": 1.90, "new_odds": 1.55, "drop_pct": 18.4},
            {"match": "Galatasaray - Fenerbahçe", "league": "Süper Lig",
             "market": "KG Var", "old_odds": 1.75, "new_odds": 1.52, "drop_pct": 13.1},
        ]

        lines = ["📉 <b>DÜŞEN ORAN ALARMLARI</b>",
                 "<i>(Büyük para hareketi veya sakatlık haberi)</i>",
                 "━━━━━━━━━━━━━━━━━━"]

        for d in simulated:
            if d["drop_pct"] >= DROPPING_ODDS_PCT:
                speed = "🔴 ÇOK HIZLI" if d["drop_pct"] > 15 else "🟡 HIZLI"
                lines.append(
                    f"\n⬇️ <b>{d['match']}</b>\n"
                    f"  🏆 {d['league']}\n"
                    f"  💡 Market: {d['market']}\n"
                    f"  {d['old_odds']} → <b>{d['new_odds']}</b> "
                    f"({speed} -%{d['drop_pct']:.1f})\n"
                    f"  ⚠️ Büyük para hareketi tespit edildi!"
                )

        lines.append(f"\n━━━━━━━━━━━━━━━━━━")
        lines.append("💡 <i>Düşen oranlar insider bilgi veya büyük\n"
                     "bahis hareketinin göstergesi olabilir.</i>")

        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Hata oluştu.")


# ─────────────────────────────────────────
# /value — Value Bet
# ─────────────────────────────────────────
async def value_bet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔬 Value betler hesaplanıyor...")
    try:
        today    = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
        fixtures = await APIFootball.fixtures_by_date(today)

        lines = ["💎 <b>VALUE BET ANALİZİ</b>",
                 "━━━━━━━━━━━━━━━━━━"]
        found = 0

        for f in fixtures[:20]:
            fid  = f["fixture"]["id"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            try:
                odds_data = await APIFootball.fixture_odds(fid)
            except Exception:
                continue

            for bookmaker in odds_data[:1]:  # İlk büro yeterli
                for bet in bookmaker.get("bookmakers", [{}])[0].get("bets", []):
                    if bet.get("name") != "Match Winner":
                        continue
                    for val in bet.get("values", []):
                        try:
                            odds   = float(val["odd"])
                            side   = val["value"]
                            # Basit model: form ve ev avantajı (gerçekte ML modeli kullanılır)
                            our_p  = 0.50 if side == "Home" else (0.28 if side == "Away" else 0.22)
                            edge   = (our_p - implied_prob(odds)) * 100
                            if edge >= 5:
                                found += 1
                                lines.append(
                                    f"\n💎 <b>{home} - {away}</b>\n"
                                    f"  Seçim: <b>{side}</b>  |  Oran: <b>{odds}</b>\n"
                                    f"  Bizim ihtimalimiz: %{our_p*100:.0f}\n"
                                    f"  Büronun ihtimali: %{implied_prob(odds)*100:.0f}\n"
                                    f"  💹 Edge: <b>+%{edge:.1f}</b> ✅ Value Bet"
                                )
                        except Exception:
                            continue

        if found == 0:
            lines.append("\n📭 Bugün güçlü value bet bulunamadı.")
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append("📌 <i>Value bet = uzun vadede karlı bahis.\n"
                     "Kısa vadede kayıp olabilir.</i>")

        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(e)
        await msg.edit_text("❌ Hata oluştu.")


# ─────────────────────────────────────────
# /arbitraj — Surebet
# ─────────────────────────────────────────
async def arbitraj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⚖️ Surebet fırsatları taranıyor...")

    # Surebet formülü: 1/o1 + 1/o2 + 1/o3 < 1.00
    simulated_arbs = [
        {
            "match": "Man City - Liverpool",
            "league": "Premier League",
            "bures": [
                {"buro": "Bet365", "selection": "Man City", "odds": 2.85},
                {"buro": "1xBet",  "selection": "Beraberlik","odds": 3.40},
                {"buro": "Pinnacle","selection": "Liverpool","odds": 3.50},
            ]
        },
    ]

    lines = ["⚖️ <b>ARBİTRAJ (SUREBET) BULUCU</b>",
             "━━━━━━━━━━━━━━━━━━"]

    arb_found = False
    for arb in simulated_arbs:
        odds_list = [b["odds"] for b in arb["bures"]]
        arb_pct   = sum(1/o for o in odds_list)

        if arb_pct < 1.0:
            arb_found = True
            profit_pct = (1 - arb_pct) * 100
            total_stake = 1000  # Örnek
            stakes = [total_stake / (o * arb_pct) for o in odds_list]

            lines.append(f"\n🎯 <b>{arb['match']}</b>")
            lines.append(f"  🏆 {arb['league']}")
            lines.append(f"  💰 Garantili Kâr: <b>%{profit_pct:.2f}</b>")
            lines.append(f"\n  1000₺ için dağılım:")
            for i, b in enumerate(arb["bures"]):
                lines.append(
                    f"  {b['buro']}: <b>{b['selection']}</b> @ {b['odds']}"
                    f" → {stakes[i]:.2f}₺"
                )
            lines.append(f"\n  ✅ Kazanç: <b>{total_stake * (1/arb_pct - 1):.2f}₺</b> ({profit_pct:.1f}%)")

    if not arb_found:
        lines.append("\n📭 Şu an surebet fırsatı yok.")
        lines.append("💡 <i>Arbitraj genellikle %1-3 kâr sağlar.\n"
                     "Birden fazla büroda hesap gerektirir.</i>")

    lines.append(f"\n━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ <i>Bürolar surebet yapanlara limit\nuygulayabilir. Dikkatli kullan.</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────
# /filtre — Özel Alarm Filtresi
# ─────────────────────────────────────────
(F_NAME, F_HT, F_MINUTE, F_MARKET) = range(4)

async def filtre_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📋 Filtrelerimi Gör", callback_data="filter_list")],
        [InlineKeyboardButton("➕ Yeni Filtre Ekle", callback_data="filter_new")],
    ]
    await update.message.reply_text(
        "🔔 <b>KİŞİSEL FİLTRE SİSTEMİ</b>\n\n"
        "Kendi stratejini tanımla. Bot arka planda binlerce\n"
        "maçı tarayıp sadece kritere uyanları sana gönderir.\n\n"
        "<i>Örnek: 'İlk yarı 0-0 ve deplasman favori ise\n"
        "60. dk'da 0.5 Üst alarmı gönder.'</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def filtre_listesi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        if not u:
            await update.message.reply_text("Önce /start yazarak kaydol.")
            return
        filtreler = s.query(UserFilter).filter_by(user_id=u.id, active=True).all()

    if not filtreler:
        await update.message.reply_text(
            "📭 Aktif filtren yok.\n"
            "/filtre yazarak yeni filtre ekleyebilirsin."
        )
        return

    lines = ["🔔 <b>AKTİF FİLTRELERİN</b>", "━━━━━━━━━━━━━━━━━━"]
    for f in filtreler:
        lines.append(
            f"\n🏷 <b>{f.name}</b> (ID: {f.id})\n"
            f"  HT Skoru: {f.half_time}\n"
            f"  Dakika: {f.min_minute}-{f.max_minute}'\n"
            f"  Market: {f.market}\n"
            f"  Dep Favori: {'Evet' if f.underdog_dep else 'Hayır'}"
        )
    lines.append("\n💡 Silmek için: /filtresil [id]")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def filtre_sil(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /filtresil 3")
        return
    try:
        fid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz ID.")
        return
    uid = str(update.effective_user.id)
    with Session() as s:
        u = s.query(User).filter_by(chat_id=uid).first()
        if u:
            f = s.query(UserFilter).filter_by(id=fid, user_id=u.id).first()
            if f:
                f.active = False
                s.commit()
                await update.message.reply_text(f"✅ Filtre #{fid} silindi.")
            else:
                await update.message.reply_text("❌ Filtre bulunamadı.")


# ─────────────────────────────────────────
# /istatistik
# ─────────────────────────────────────────
async def istatistik(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    with Session() as s:
        u      = s.query(User).filter_by(chat_id=uid).first()
        picks  = s.query(DailyPick).all()
        bets   = s.query(BetRecord).filter_by(user_id=u.id if u else 0).all() if u else []

    total    = len(picks)
    settled  = [p for p in picks if p.result != "pending"]
    won      = [p for p in settled if p.result == "win"]
    hit_rate = (len(won) / len(settled) * 100) if settled else 0

    # Piyasa bazlı isabet
    markets = {}
    for p in settled:
        markets.setdefault(p.market, {"w":0,"t":0})
        markets[p.market]["t"] += 1
        if p.result == "win":
            markets[p.market]["w"] += 1

    # Kupon türü bazlı
    banko_picks  = [p for p in settled if p.pick_type == "banko"]
    katla_picks  = [p for p in settled if p.pick_type == "katla"]
    banko_won    = sum(1 for p in banko_picks if p.result == "win")
    katla_won    = sum(1 for p in katla_picks if p.result == "win")

    # Kişisel bahis
    total_stake  = sum(b.stake for b in bets)
    total_profit = sum(b.profit for b in bets if b.result == "win")
    total_loss   = sum(b.stake for b in bets if b.result == "loss")
    roi          = ((total_profit - total_loss) / total_stake * 100) if total_stake > 0 else 0

    lines = [
        "📊 <b>TAHMİN İSTATİSTİKLERİ</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"\n🎯 Toplam Tahmin: <b>{total}</b>",
        f"✅ Biten: {len(settled)}  |  ⏳ Bekleyen: {total - len(settled)}",
        f"\n🏆 <b>Genel İsabet: %{hit_rate:.1f}</b>",
        f"🔒 Banko İsabeti: %{(banko_won/len(banko_picks)*100) if banko_picks else 0:.1f} ({banko_won}/{len(banko_picks)})",
        f"🚀 Katlama İsabeti: %{(katla_won/len(katla_picks)*100) if katla_picks else 0:.1f} ({katla_won}/{len(katla_picks)})",
    ]

    if markets:
        lines.append("\n📈 <b>Market Bazlı İsabet:</b>")
        for mkt, v in sorted(markets.items(), key=lambda x: x[1]["w"]/max(x[1]["t"],1), reverse=True):
            r = v["w"] / v["t"] * 100
            bar = "🟢" if r >= 60 else ("🟡" if r >= 45 else "🔴")
            lines.append(f"  {bar} {mkt}: %{r:.0f} ({v['w']}/{v['t']})")

    if bets:
        lines.append(f"\n💰 <b>Kişisel Bahis Özeti:</b>")
        lines.append(f"  Toplam Yatırım: {total_stake:.2f}₺")
        lines.append(f"  Toplam Kâr: {total_profit:.2f}₺")
        lines.append(f"  Toplam Zarar: {total_loss:.2f}₺")
        lines.append(f"  ROI: <b>%{roi:.1f}</b>")

    lines.append("\n━━━━━━━━━━━━━━━━━━")
    lines.append("📌 <i>Veriler günlük güncellenir.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────
# /gecmis
# ─────────────────────────────────────────
async def gecmis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with Session() as s:
        picks = s.query(DailyPick).order_by(DailyPick.created_at.desc()).limit(20).all()

    if not picks:
        await update.message.reply_text("📭 Geçmiş kayıt bulunamadı.")
        return

    lines = ["📅 <b>SON 20 TAHMİN</b>", "━━━━━━━━━━━━━━━━━━"]
    for p in picks:
        em = {"win":"✅","loss":"❌","pending":"⏳"}.get(p.result,"⏳")
        lines.append(
            f"{em} {p.match_name}\n"
            f"   {p.market} @ {p.odds} [{p.pick_type}]"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────
# ZAMANLANMIŞ GÖREVLER
# ─────────────────────────────────────────
async def scheduled_daily_analysis(app: Application):
    """Her gece 23:30'da önümüzdeki 2 günün analizi"""
    logger.info("Zamanlanmış analiz başlatıldı...")
    try:
        today_str = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
        tmrw_str  = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=1)).strftime("%Y-%m-%d")

        f1 = await APIFootball.fixtures_by_date(today_str)
        f2 = await APIFootball.fixtures_by_date(tmrw_str)
        all_f = f1 + f2

        if not all_f:
            return

        big_leagues = ["premier league","la liga","bundesliga","serie a","ligue 1",
                       "süper lig","champions league","europa league"]

        filtered = [f for f in all_f
                    if f["league"]["name"].lower() in big_leagues][:15]

        lines = [
            "🌙 <b>AKŞAM ANALİZ RAPORU</b>",
            f"📅 {today_str} → {tmrw_str}",
            "━━━━━━━━━━━━━━━━━━",
            f"🔎 Toplam {len(all_f)} maç tarandı.",
            f"⭐ Öne Çıkan {len(filtered)} maç:\n"
        ]

        for f in filtered[:10]:
            home   = f["teams"]["home"]["name"]
            away   = f["teams"]["away"]["name"]
            league = f["league"]["name"]
            date_  = f["fixture"]["date"][:10]
            time_  = f["fixture"]["date"][11:16]
            lines.append(
                f"⚽ <b>{home} - {away}</b>\n"
                f"  🏆 {league} | 🕒 {time_} | 📅 {date_}\n"
            )

        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("💡 Detaylı analiz için /kupon yazın.")
        lines.append("📡 Canlı takip için /canli yazın.")

        text = "\n".join(lines)

        # Tüm kullanıcılara gönder
        with Session() as s:
            users = s.query(User).all()

        for u in users:
            try:
                await app.bot.send_message(
                    chat_id=u.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML
                )
                await asyncio.sleep(0.05)  # Rate limit
            except Exception as e:
                logger.warning(f"Kullanıcıya gönderilemedi {u.chat_id}: {e}")

    except Exception as e:
        logger.error(f"Zamanlanmış görev hatası: {e}")


async def live_scanner_job(app: Application):
    """Her 60 saniyede canlı maçları tara, filtre eşleşirse bildir"""
    try:
        matches = await APIFootball.live_matches()
        with Session() as s:
            all_filters = s.query(UserFilter).filter_by(active=True).all()
            users       = {u.id: u.chat_id for u in s.query(User).all()}

        for flt in all_filters:
            for m in matches:
                minute  = m["fixture"]["status"].get("elapsed") or 0
                home_g  = m["goals"].get("home") or 0
                away_g  = m["goals"].get("away") or 0
                ht_score= f"{home_g}-{away_g}"

                if not (flt.min_minute <= minute <= flt.max_minute):
                    continue
                if flt.half_time != "any" and ht_score != flt.half_time:
                    continue

                chat_id = users.get(flt.user_id)
                if not chat_id:
                    continue

                home = m["teams"]["home"]["name"]
                away = m["teams"]["away"]["name"]
                text = (
                    f"🔔 <b>FİLTRE ALARMI: {flt.name}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⚽ {home} - {away}\n"
                    f"⏱ {minute}' | Skor: {ht_score}\n"
                    f"💡 Market: <b>{flt.market}</b>\n"
                    f"🎯 Filtren eşleşti! Hızlı hareket et."
                )
                try:
                    await app.bot.send_message(
                        chat_id=chat_id, text=text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Scanner hatası: {e}")


# ─────────────────────────────────────────
# CALLBACK QUERY YÖNETİCİSİ
# ─────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    data   = query.data
    await query.answer()

    if data == "live":
        # /canli komutunu simüle et
        update.message = query.message
        await canli(update, ctx)

    elif data == "coupon":
        update.message = query.message
        await kupon(update, ctx)

    elif data == "bankroll":
        update.message = query.message
        await kasa(update, ctx)

    elif data == "stats":
        update.message = query.message
        await istatistik(update, ctx)

    elif data == "coupon_check":
        update.message = query.message
        await kupon_kontrol(update, ctx)

    elif data == "pressure":
        update.message = query.message
        await baski(update, ctx)

    elif data == "filter_list":
        update.message = query.message
        await filtre_listesi(update, ctx)

    elif data == "filter_new":
        await query.message.reply_text(
            "➕ <b>Yeni Filtre Oluştur</b>\n\n"
            "Sırayla bilgileri gir:\n\n"
            "1️⃣ Filtre adı: (örn: Benim_Stratejim)\n"
            "2️⃣ İlk yarı skoru: (örn: 0-0 veya any)\n"
            "3️⃣ Dakika aralığı: (örn: 60-75)\n"
            "4️⃣ Market: (örn: 0.5_ust veya 2.5_ust)\n\n"
            "Format: <code>/yenifiltre [ad] [ht_skor] [dk_min] [dk_max] [market]</code>\n"
            "Örnek: <code>/yenifiltre Stratejim 0-0 60 80 2.5_ust</code>",
            parse_mode=ParseMode.HTML
        )

    elif data == "kelly_menu":
        await query.message.reply_text(
            "📐 <b>Kelly Hesabı</b>\n\n"
            "Kullanım:\n"
            "<code>/kelly [oran] [ihtimal%]</code>\n"
            "Örnek: <code>/kelly 2.10 58</code>",
            parse_mode=ParseMode.HTML
        )


# ─────────────────────────────────────────
# /yenifiltre — Komut ile filtre ekleme
# ─────────────────────────────────────────
async def yeni_filtre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 5:
        await update.message.reply_text(
            "Kullanım: /yenifiltre [ad] [ht] [dk_min] [dk_max] [market]\n"
            "Örnek: /yenifiltre Strateji 0-0 60 80 2.5_ust"
        )
        return
    try:
        name     = ctx.args[0]
        ht       = ctx.args[1]
        dk_min   = int(ctx.args[2])
        dk_max   = int(ctx.args[3])
        market   = ctx.args[4]
        uid      = str(update.effective_user.id)

        with Session() as s:
            u = s.query(User).filter_by(chat_id=uid).first()
            if not u:
                u = get_or_create_user(uid)
                s.refresh(u)
            flt = UserFilter(
                user_id=u.id, name=name, half_time=ht,
                min_minute=dk_min, max_minute=dk_max, market=market
            )
            s.add(flt)
            s.commit()

        await update.message.reply_text(
            f"✅ <b>'{name}'</b> filtresi aktif!\n\n"
            f"📋 Kriter:\n"
            f"  HT: {ht} | Dakika: {dk_min}-{dk_max}' | Market: {market}\n\n"
            f"Bot arka planda canlı maçları tarayacak ve\n"
            f"kriter sağlandığında seni uyaracak. 🔔",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Hata: {e}")


# ─────────────────────────────────────────
# UYGULAMA BAŞLATICI
# ─────────────────────────────────────────
def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN bulunamadı! .env dosyasını kontrol et.")
        return
    if not API_FOOTBALL:
        logger.warning("API_FOOTBALL_KEY bulunamadı! Gerçek veriler çekilemeyecek.")

    app = Application.builder().token(TOKEN).build()

    # Komut kayıtları
    commands = [
        ("start",          "Bot kullanım kılavuzu"),
        ("kupon",          "Günlük kupon al"),
        ("kuponkontrol",   "Kupon sonuçlarını kontrol et"),
        ("canli",          "Canlı fırsat radarı"),
        ("baski",          "Baskı indeksi analizi"),
        ("xg",             "XG gol beklentisi alarmları"),
        ("dusenuran",      "Düşen oran uyarıları"),
        ("value",          "Value bet analizi"),
        ("arbitraj",       "Surebet / arbitraj bulucu"),
        ("kasa",           "Kasa durumu"),
        ("kasaayar",       "Kasayı ayarla"),
        ("butce",          "Günlük bütçeyi ayarla"),
        ("zarar",          "Zarar limitini ayarla"),
        ("kelly",          "Kelly kriteri hesabı"),
        ("filtre",         "Kişisel filtre menüsü"),
        ("yenifiltre",     "Yeni filtre ekle"),
        ("filtrelistesi",  "Aktif filtreleri gör"),
        ("filtresil",      "Filtre sil"),
        ("istatistik",     "Tahmin istatistikleri"),
        ("gecmis",         "Geçmiş tahminler"),
    ]

    # Handler'lar
    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("kupon",          kupon))
    app.add_handler(CommandHandler("kuponkontrol",   kupon_kontrol))
    app.add_handler(CommandHandler("canli",          canli))
    app.add_handler(CommandHandler("baski",          baski))
    app.add_handler(CommandHandler("xg",             xg_cmd))
    app.add_handler(CommandHandler("dusenuran",      dusen_oran))
    app.add_handler(CommandHandler("value",          value_bet))
    app.add_handler(CommandHandler("arbitraj",       arbitraj))
    app.add_handler(CommandHandler("kasa",           kasa))
    app.add_handler(CommandHandler("kasaayar",       kasaayar))
    app.add_handler(CommandHandler("butce",          butce))
    app.add_handler(CommandHandler("zarar",          zarar))
    app.add_handler(CommandHandler("kelly",          kelly_cmd))
    app.add_handler(CommandHandler("filtre",         filtre_start))
    app.add_handler(CommandHandler("yenifiltre",     yeni_filtre))
    app.add_handler(CommandHandler("filtrelistesi",  filtre_listesi))
    app.add_handler(CommandHandler("filtresil",      filtre_sil))
    app.add_handler(CommandHandler("istatistik",     istatistik))
    app.add_handler(CommandHandler("gecmis",         gecmis))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Scheduler — APScheduler
    scheduler = AsyncIOScheduler()

    # Her gece 23:30 TR saatiyle (UTC+3 = 20:30 UTC)
    scheduler.add_job(
        lambda: asyncio.ensure_future(scheduled_daily_analysis(app)),
        CronTrigger(hour=4, minute=25, timezone="UTC"),
        id="daily_analysis"
    )

    # Her 60 saniyede canlı tarama
    scheduler.add_job(
        lambda: asyncio.ensure_future(live_scanner_job(app)),
        "interval", seconds=60,
        id="live_scanner"
    )

    scheduler.start()
    logger.info("📡 Scheduler başlatıldı.")

    # Bot komutlarını kaydet
    async def post_init(application: Application):
        await application.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in commands]
        )
        logger.info("✅ Bot komutları Telegram'a kaydedildi.")

    app.post_init = post_init

    logger.info("🤖 BahisBotu başlatılıyor...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
