import asyncio
import base64
import html
import io
import itertools
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from pypinyin import Style, lazy_pinyin
from PIL import Image, ImageDraw, ImageFont, ImageOps

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEOAPIFY_API_KEY = os.getenv("GEOAPIFY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "cityquest.sqlite3")
PERSISTENCE_OK = True

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not GEOAPIFY_API_KEY:
    raise RuntimeError("GEOAPIFY_API_KEY is not set")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cityquest")
router = Router()


ACTIVE_DATA_KEYS = (
    "quest",
    "route",
    "duration",
    "city",
    "style",
    "interests",
    "completed",
    "bonuses",
    "photos",
    "photo_versions",
    "travel_card_path",
    "travel_caption",
    "travel_ai_caption",
)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_persistence():
    global PERSISTENCE_OK

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "travel_cards"), exist_ok=True)
        with db_connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_quests (
                    user_id INTEGER PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS completed_quests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT,
                    city TEXT,
                    finished_at TEXT NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0,
                    photos INTEGER NOT NULL DEFAULT 0,
                    data_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_completed_user_finished
                ON completed_quests(user_id, finished_at DESC)
                """
            )
        logger.info("Persistence ready: %s", DB_PATH)
    except Exception:
        PERSISTENCE_OK = False
        logger.exception("Persistence initialization failed; bot will continue without DB")


def active_payload(data):
    return {
        key: data.get(key)
        for key in ACTIVE_DATA_KEYS
        if key in data
    }


def db_save_active(user_id: int, data: dict):
    if not PERSISTENCE_OK or not data.get("quest"):
        return

    payload = active_payload(data)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO active_quests(user_id, data_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                (int(user_id), raw, utc_now_iso()),
            )
    except Exception:
        logger.exception("Failed to save active quest for user %s", user_id)


def db_load_active(user_id: int):
    if not PERSISTENCE_OK:
        return None

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM active_quests WHERE user_id=?",
                (int(user_id),),
            ).fetchone()

        if not row:
            return None
        return json.loads(row["data_json"])
    except Exception:
        logger.exception("Failed to load active quest for user %s", user_id)
        return None


def db_delete_active(user_id: int):
    if not PERSISTENCE_OK:
        return

    try:
        with db_connect() as conn:
            conn.execute(
                "DELETE FROM active_quests WHERE user_id=?",
                (int(user_id),),
            )
    except Exception:
        logger.exception("Failed to delete active quest for user %s", user_id)


def db_archive_completed(user_id: int, data: dict):
    if not PERSISTENCE_OK or not data.get("quest"):
        return

    quest = data["quest"]
    completed = data.get("completed", [])
    bonuses = data.get("bonuses", [])
    photos = data.get("photos", {})
    city = data.get("city") or {}

    xp = earned_xp(quest, completed, bonuses)
    payload = active_payload(data)
    payload["finished_at"] = utc_now_iso()

    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO completed_quests(
                    user_id, title, city, finished_at, xp, photos, data_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    str(quest.get("title") or "CityQuest"),
                    city_display_ru(city),
                    payload["finished_at"],
                    int(xp),
                    int(len(photos)),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.execute(
                "DELETE FROM active_quests WHERE user_id=?",
                (int(user_id),),
            )
    except Exception:
        logger.exception("Failed to archive quest for user %s", user_id)


def db_completed_list(user_id: int, limit: int = 5):
    if not PERSISTENCE_OK:
        return []

    try:
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, city, finished_at, xp, photos
                FROM completed_quests
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(user_id), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to list completed quests for user %s", user_id)
        return []


def db_recent_missions_for_city(user_id: int, city_name: str, limit: int = 12):
    """Small memory used only to tell AI what not to repeat in the same city."""
    if not PERSISTENCE_OK:
        return []

    try:
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json
                FROM completed_quests
                WHERE user_id=? AND city=?
                ORDER BY id DESC
                LIMIT 5
                """,
                (int(user_id), str(city_name)),
            ).fetchall()

        seen = []
        for row in rows:
            try:
                payload = json.loads(row["data_json"])
            except Exception:
                continue

            quest = payload.get("quest") or {}
            for stop in quest.get("stops", []):
                mission = stop.get("mission") or {}
                title = str(mission.get("title") or "").strip()
                body = str(mission.get("text") or "").strip()
                if not title:
                    continue
                memory = title if not body else f"{title}: {short_text(body, 110)}"
                if memory not in seen:
                    seen.append(memory)
                if len(seen) >= limit:
                    return seen

        return seen
    except Exception:
        logger.exception("Failed to load mission memory for %s / %s", user_id, city_name)
        return []


def db_latest_card(user_id: int):
    if not PERSISTENCE_OK:
        return None

    try:
        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json
                FROM completed_quests
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 10
                """,
                (int(user_id),),
            ).fetchall()

        for row in rows:
            try:
                payload = json.loads(row["data_json"])
            except Exception:
                continue

            path = payload.get("travel_card_path")
            if path and os.path.exists(path):
                return path

    except Exception:
        logger.exception("Failed to load latest travel card for user %s", user_id)

    return None


def db_latest_completed_payload(user_id: int):
    if not PERSISTENCE_OK:
        return None, None

    try:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT id, data_json
                FROM completed_quests
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()

        if not row:
            return None, None

        return int(row["id"]), json.loads(row["data_json"])
    except Exception:
        logger.exception("Failed to load latest completed quest for user %s", user_id)
        return None, None


def db_update_completed_payload(record_id: int, payload: dict):
    if not PERSISTENCE_OK or not record_id:
        return False

    try:
        quest = payload.get("quest") or {}
        city = payload.get("city") or {}
        completed = payload.get("completed", [])
        bonuses = payload.get("bonuses", [])
        photos = payload.get("photos", {})
        xp = earned_xp(quest, completed, bonuses) if quest else 0

        with db_connect() as conn:
            conn.execute(
                """
                UPDATE completed_quests
                SET title=?, city=?, xp=?, photos=?, data_json=?
                WHERE id=?
                """,
                (
                    str(quest.get("title") or "CityQuest"),
                    city_display_ru(city),
                    int(xp),
                    int(len(photos)),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    int(record_id),
                ),
            )
        return True
    except Exception:
        logger.exception("Failed to update completed quest %s", record_id)
        return False


async def persist_active_state(user_id: int, state: FSMContext):
    data = await state.get_data()
    if data.get("quest"):
        db_save_active(user_id, data)


async def restore_active_state(user_id: int, state: FSMContext):
    """
    Restore MemoryStorage from SQLite after BotHost restart.
    Returns the restored/current state data.
    """
    data = await state.get_data()
    if data.get("quest"):
        return data

    saved = db_load_active(user_id)
    if not saved or not saved.get("quest"):
        return data

    await state.clear()
    await state.update_data(**saved)
    await state.set_state(QuestForm.quest_active)
    return await state.get_data()


def active_quest_summary(data):
    quest = data.get("quest") or {}
    city = data.get("city") or {}
    completed = data.get("completed", [])
    bonuses = data.get("bonuses", [])
    photos = data.get("photos", {})

    title = str(quest.get("title") or "CityQuest")
    city_name = city_display_ru(city)
    total = len(quest.get("stops", []))
    xp = earned_xp(quest, completed, bonuses) if quest else 0

    return (
        f"🏮 <b>{esc(title)}</b>\n"
        f"📍 {esc(city_name)}\n"
        f"✅ Миссии: <b>{len(completed)}/{total}</b>\n"
        f"⭐ XP: <b>{xp}</b>\n"
        f"📷 Фото: <b>{len(photos)}</b>"
    )


def adventures_keyboard(has_active=False, has_card=False):
    kb = InlineKeyboardBuilder()
    if has_active:
        kb.button(text="▶️ Продолжить активный квест", callback_data="resume_quest")
    if has_card:
        kb.button(text="🖼 Последняя travel-открытка", callback_data="latest_card")
    kb.button(text="🧭 Новый квест", callback_data="new_quest")
    kb.button(text="🏠 Главное меню", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


class QuestForm(StatesGroup):
    waiting_city = State()
    choosing_interests = State()
    choosing_style = State()
    generating = State()
    quest_active = State()
    waiting_photo = State()
    waiting_free_photo = State()
    free_photo_ready = State()
    waiting_custom_impression = State()
    quest_finished = State()


INTERESTS = {
    "history": {"label": "🏯 История", "categories": ["tourism.sights", "heritage", "entertainment.museum"]},
    "tea": {"label": "🍵 Чай", "categories": ["catering.cafe.tea", "catering.cafe"]},
    "food": {"label": "🍜 Еда", "categories": ["catering.restaurant", "catering.fast_food", "catering.food_court", "commercial.marketplace"]},
    "photo": {"label": "📸 Фото", "categories": ["tourism.attraction", "tourism.attraction.viewpoint", "tourism.sights", "leisure.park"]},
    "nature": {"label": "🌿 Природа", "categories": ["leisure.park", "leisure.park.garden", "natural"]},
    "art": {"label": "🎨 Искусство", "categories": ["entertainment.culture", "entertainment.museum", "tourism.attraction.artwork"]},
    "tradition": {"label": "🏮 Традиции", "categories": ["tourism.sights.place_of_worship", "tourism.sights.city_gate", "tourism.sights", "heritage"]},
    "unusual": {"label": "🕵️ Необычное", "categories": ["tourism.attraction", "tourism.sights", "commercial.marketplace"]},
}

REST_CATEGORIES = ["catering.cafe.tea", "catering.cafe"]

FALLBACK_DISCOVERY_SOURCES = [
    ("fallback_history", ["tourism.sights", "heritage", "entertainment.museum", "tourism.sights.place_of_worship"]),
    ("fallback_nature", ["leisure.park", "leisure.park.garden", "natural"]),
    ("fallback_local", ["commercial.marketplace"]),
    ("fallback_food", ["catering.restaurant", "catering.cafe", "catering.fast_food"]),
    ("fallback_culture", ["entertainment.culture", "tourism.attraction.artwork", "tourism.attraction.viewpoint"]),
]

ADAPTIVE_MODE_LABELS = {
    "rich": "🟢 Полный городской квест",
    "compact": "🟡 Компактный квест",
    "explorer": "🟠 Исследовательский квест",
}

STYLE_LABELS = {
    "calm": "😌 Спокойно",
    "explorer": "🔎 Исследователь",
    "adventure": "🔥 Приключение",
}

DURATION_MINUTES = {
    "2 часа": 120,
    "4 часа": 240,
    "6 часов": 360,
    "весь день": 480,
}

MAX_LEG_MINUTES = {
    "2 часа": 25,
    "4 часа": 35,
    "6 часов": 45,
    "весь день": 55,
}

RU_CITY_ALIASES = {
    "пекин": "Beijing",
    "шанхай": "Shanghai",
    "гуанчжоу": "Guangzhou",
    "шэньчжэнь": "Shenzhen",
    "шеньчжень": "Shenzhen",
    "чэнду": "Chengdu",
    "ченду": "Chengdu",
    "сиань": "Xi'an",
    "ханчжоу": "Hangzhou",
    "нанкин": "Nanjing",
    "сучжоу": "Suzhou",
    "ухань": "Wuhan",
    "чунцин": "Chongqing",
    "тяньцзинь": "Tianjin",
    "циндао": "Qingdao",
    "сямэнь": "Xiamen",
    "сямень": "Xiamen",
    "куньмин": "Kunming",
    "далянь": "Dalian",
    "харбин": "Harbin",
    "санья": "Sanya",
    "гуйлинь": "Guilin",
    "лоян": "Luoyang",
    "чжанцзяцзе": "Zhangjiajie",
    "кашгар": "Kashgar",
    "урумчи": "Urumqi",
    "лхаса": "Lhasa",
    "ланчжун": "Langzhong",
    "цзяньшуй": "Jianshui",
    "шэсянь": "Shexian",
    "макао": "Macao",
    "гонконг": "Hong Kong",
}

CHINA_REGION_RU = {
    "anhui": "Аньхой",
    "beijing": "Пекин",
    "chongqing": "Чунцин",
    "fujian": "Фуцзянь",
    "gansu": "Ганьсу",
    "guangdong": "Гуандун",
    "guangxi": "Гуанси",
    "guizhou": "Гуйчжоу",
    "hainan": "Хайнань",
    "hebei": "Хэбэй",
    "heilongjiang": "Хэйлунцзян",
    "henan": "Хэнань",
    "hubei": "Хубэй",
    "hunan": "Хунань",
    "inner mongolia": "Внутренняя Монголия",
    "jiangsu": "Цзянсу",
    "jiangxi": "Цзянси",
    "jilin": "Гирин",
    "liaoning": "Ляонин",
    "ningxia": "Нинся",
    "qinghai": "Цинхай",
    "shaanxi": "Шэньси",
    "shandong": "Шаньдун",
    "shanghai": "Шанхай",
    "shanxi": "Шаньси",
    "sichuan": "Сычуань",
    "tianjin": "Тяньцзинь",
    "tibet": "Тибет",
    "xinjiang": "Синьцзян",
    "yunnan": "Юньнань",
    "zhejiang": "Чжэцзян",
    "hong kong": "Гонконг",
    "macao": "Макао",
    "macau": "Макао",
}

PREFERRED_CITY_RU = {
    "beijing": "Пекин",
    "shanghai": "Шанхай",
    "guangzhou": "Гуанчжоу",
    "shenzhen": "Шэньчжэнь",
    "chengdu": "Чэнду",
    "xi'an": "Сиань",
    "xian": "Сиань",
    "hangzhou": "Ханчжоу",
    "nanjing": "Нанкин",
    "suzhou": "Сучжоу",
    "wuhan": "Ухань",
    "chongqing": "Чунцин",
    "tianjin": "Тяньцзинь",
    "qingdao": "Циндао",
    "xiamen": "Сямэнь",
    "kunming": "Куньмин",
    "dalian": "Далянь",
    "harbin": "Харбин",
    "sanya": "Санья",
    "guilin": "Гуйлинь",
    "luoyang": "Лоян",
    "zhangjiajie": "Чжанцзяцзе",
    "kashgar": "Кашгар",
    "urumqi": "Урумчи",
    "lhasa": "Лхаса",
    "macao": "Макао",
    "macau": "Макао",
    "hong kong": "Гонконг",
    "langzhong": "Ланчжун",
    "jianshui": "Цзяньшуй",
    "shexian": "Шэсянь",
}

EXACT_RU_NAMES = {
    "天府广场": "Площадь Тяньфу",
    "钟楼": "Колокольная башня",
    "西安钟楼": "Колокольная башня Сианя",
    "鼓楼": "Барабанная башня",
    "西安鼓楼": "Барабанная башня Сианя",
    "西安鼓楼博物馆": "Музей Барабанной башни Сианя",
    "西安城墙": "Городская стена Сианя",
    "大雁塔": "Большая пагода диких гусей",
    "小雁塔": "Малая пагода диких гусей",
    "喜茶": "Чайная HEYTEA",
    "星巴克": "Starbucks",
}

FOOD_GLOSSARY = {
    "lao gan ma": "Lao Gan Ma — популярный китайский острый соус/масло с чили",
    "老干妈": "Lao Gan Ma — популярный китайский острый соус/масло с чили",
    "mala": "málà / 麻辣 — остро-пряный вкус с чили и сычуаньским перцем; может слегка «онемлять» рот",
    "麻辣": "málà / 麻辣 — остро-пряный вкус с чили и сычуаньским перцем; может слегка «онемлять» рот",
    "huajiao": "huājiāo / 花椒 — сычуаньский перец с характерным покалывающим эффектом",
    "花椒": "huājiāo / 花椒 — сычуаньский перец с характерным покалывающим эффектом",
    "doubanjiang": "dòubànjiàng / 豆瓣酱 — ферментированная бобово-чили паста, обычно солёная и острая",
    "豆瓣酱": "dòubànjiàng / 豆瓣酱 — ферментированная бобово-чили паста, обычно солёная и острая",
}

PHRASES = {
    "spicy": {
        "title": "🌶 Острое?",
        "hanzi": "这个辣不辣？",
        "pinyin": "Zhège là bu là?",
        "ru": "Чжэгэ ла бу ла?",
        "translation": "Это острое?",
    },
    "meatfish": {
        "title": "🥩 Мясо или рыба?",
        "hanzi": "这是肉还是鱼？",
        "pinyin": "Zhè shì ròu háishi yú?",
        "ru": "Чжэ ши жоу хайши юй?",
        "translation": "Это мясо или рыба?",
    },
    "inside": {
        "title": "🥢 Что внутри?",
        "hanzi": "里面有什么？",
        "pinyin": "Lǐmiàn yǒu shénme?",
        "ru": "Лимьен ёу шэньмэ?",
        "translation": "Что внутри / из чего это?",
    },
    "what": {
        "title": "❓ Что это?",
        "hanzi": "这个是什么？",
        "pinyin": "Zhège shì shénme?",
        "ru": "Чжэгэ ши шэньмэ?",
        "translation": "Что это?",
    },
    "recommend": {
        "title": "🍜 Что рекомендуете?",
        "hanzi": "你推荐什么？",
        "pinyin": "Nǐ tuījiàn shénme?",
        "ru": "Ни туэйцзень шэньмэ?",
        "translation": "Что вы рекомендуете?",
    },
    "smell": {
        "title": "🍵 Можно понюхать?",
        "hanzi": "我可以闻一下吗？",
        "pinyin": "Wǒ kěyǐ wén yíxià ma?",
        "ru": "Во кэ-и вэнь и-ся ма?",
        "translation": "Можно понюхать?",
    },
    "photo": {
        "title": "📸 Сфотографируйте меня",
        "hanzi": "请帮我拍张照片，可以吗？",
        "pinyin": "Qǐng bāng wǒ pāi zhāng zhàopiàn, kěyǐ ma?",
        "ru": "Цин бан во пай чжан чжаопьен, кэ-и ма?",
        "translation": "Можете меня сфотографировать, пожалуйста?",
    },
}


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def short_text(value: str, max_len: int = 40) -> str:
    value = str(value or "").strip()
    return value if len(value) <= max_len else value[:max_len - 1].rstrip() + "…"


def contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(text or "")))


def contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", str(text or "")))


def fmt_minutes(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    return f"{h} ч" if not m else f"{h} ч {m} мин"


def fmt_distance(meters: float) -> str:
    if meters < 1000:
        return f"{max(10, int(round(meters / 10) * 10))} м"
    return f"{meters / 1000:.1f} км"


def place_pinyin(text: str) -> str:
    if not contains_han(text):
        return ""
    parts = lazy_pinyin(text, style=Style.TONE, errors=lambda chars: [chars])
    value = " ".join(p for p in parts if p).strip()
    return value[:1].upper() + value[1:] if value else ""


def city_query_variants(text: str) -> list[str]:
    original = text.strip()
    variants = []

    alias = RU_CITY_ALIASES.get(original.casefold())
    if alias:
        variants.append(alias)

    variants.append(original)

    if contains_han(original):
        stripped = re.sub(r"[市县区]$", "", original)

        # County-level cities in China often have 市 in their official name.
        # Example: 阆中市. Search both the short and official-looking variants.
        if not re.search(r"[市县区]$", original):
            variants.append(original + "市")

        for base in {original, stripped}:
            syllables = [p for p in lazy_pinyin(base, style=Style.NORMAL, errors="ignore") if p]
            if syllables:
                variants.extend([
                    "".join(syllables),
                    " ".join(syllables),
                    "'".join(syllables),
                ])

    out, seen = [], set()
    for value in variants:
        for query in (value, f"{value}, China"):
            key = query.casefold().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(query.strip())
    return out


def city_display_ru(city: dict) -> str:
    """
    Russian UI name for a city. Works for old saved quests too:
    it derives the display name at render time instead of requiring migration.
    """
    if not city:
        return "Китай"

    input_name = str(city.get("input_name") or "").strip()
    canonical = str(
        city.get("city")
        or city.get("name")
        or input_name
        or ""
    ).strip()

    # If user entered a Russian name, preserve it.
    if input_name and contains_cyrillic(input_name):
        return input_name

    # Direct canonical map.
    key = canonical.casefold().strip()
    key_simple = re.sub(r"\s+city$", "", key).strip()
    if key in PREFERRED_CITY_RU:
        return PREFERRED_CITY_RU[key]
    if key_simple in PREFERRED_CITY_RU:
        return PREFERRED_CITY_RU[key_simple]

    # Existing Russian -> English aliases can be inverted for supported cities.
    canonical_norm = re.sub(r"[\s'’`\\-_,.]+", "", key_simple)
    for ru_name, english_name in RU_CITY_ALIASES.items():
        english_norm = re.sub(
            r"[\s'’`\\-_,.]+",
            "",
            str(english_name).casefold(),
        )
        if english_norm == canonical_norm:
            return ru_name[:1].upper() + ru_name[1:]

    # If the original input was Chinese and we have a canonical English form,
    # use that as a safe fallback instead of exposing a lowercase technical value.
    if canonical:
        return canonical

    return input_name or "Китай"


def city_display_secondary(city: dict) -> str:
    """
    Optional original/canonical form shown after the Russian name when useful.
    """
    if not city:
        return ""

    ru = city_display_ru(city)
    canonical = str(city.get("city") or city.get("name") or "").strip()
    original = str(city.get("input_name") or "").strip()

    choices = []
    for value in (canonical, original):
        if value and value.casefold() != ru.casefold() and value not in choices:
            choices.append(value)

    return " · ".join(choices[:2])


def normalize_city_text(value: str) -> str:
    value = str(value or "").casefold().strip()
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"[\s'’`\\-_,.]+", "", value)

    # Administrative suffixes should not make the same city look different.
    for suffix in ("city", "town", "county", "district", "市", "县", "区", "镇"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    return value


def region_ru(value: str) -> str:
    raw = str(value or "").strip()
    return CHINA_REGION_RU.get(raw.casefold(), raw or "регион не указан")


def admin_ru(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    low = raw.casefold()
    if low.endswith(" county"):
        return "уезд " + raw[:-7].strip()
    if low.endswith(" city"):
        return "город " + raw[:-5].strip()
    if low.endswith(" district"):
        return "район " + raw[:-9].strip()
    if low.endswith(" town"):
        return "посёлок " + raw[:-5].strip()
    return raw


def city_target_norms(city_query: str) -> set[str]:
    targets = set()
    original = str(city_query or "").strip()

    if original:
        targets.add(normalize_city_text(original))

    alias = RU_CITY_ALIASES.get(original.casefold())
    if alias:
        targets.add(normalize_city_text(alias))

    if contains_han(original):
        stripped = re.sub(r"[市县区镇]$", "", original)
        if stripped:
            targets.add(normalize_city_text(stripped))
            syllables = [
                p for p in lazy_pinyin(stripped, style=Style.NORMAL, errors="ignore")
                if p
            ]
            if syllables:
                targets.add(normalize_city_text("".join(syllables)))

    return {t for t in targets if t}


def candidate_direct_name_norms(item: dict) -> set[str]:
    fields = [
        item.get("name"),
        item.get("city"),
        item.get("town"),
        item.get("village"),
    ]
    return {normalize_city_text(v) for v in fields if v}


def candidate_matches_requested_name(item: dict, targets: set[str]) -> bool:
    """
    IMPORTANT: match the actual settlement name, not a word hidden somewhere
    in formatted address. This removes results like Puyang/Henan for 阆中.
    """
    names = candidate_direct_name_norms(item)
    if not names:
        return False

    for name in names:
        for target in targets:
            if name == target:
                return True

    return False


def candidate_admin_bonus(item: dict, targets: set[str]) -> float:
    """
    Prefer the real administrative city when the county/city field repeats the
    requested name, e.g. Langzhong City, over a tiny same-named settlement in
    an unrelated county.
    """
    bonus = 0.0
    county_norm = normalize_city_text(item.get("county"))
    city_norm = normalize_city_text(item.get("city"))
    name_norm = normalize_city_text(item.get("name"))

    if city_norm in targets:
        bonus += 2.0
    if name_norm in targets:
        bonus += 1.5
    if county_norm in targets:
        bonus += 3.0

    result_type = (item.get("result_type") or "").casefold()
    if result_type == "city":
        bonus += 1.5
    elif result_type in {"county", "district"}:
        bonus += 0.4

    return bonus


def same_city_nearby(a: dict, b: dict) -> bool:
    """
    Treat duplicate geocoding records for the same named settlement as one city.
    A missing province on one record must not create a fake second choice.
    """
    if normalize_city_text(a.get("city")) != normalize_city_text(b.get("city")):
        return False

    lat1, lon1 = float(a["lat"]), float(a["lon"])
    lat2, lon2 = float(b["lat"]), float(b["lon"])
    dx = (lon1 - lon2) * 111000 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat1 - lat2) * 111000
    distance = math.hypot(dx, dy)

    state_a = normalize_city_text(a.get("state"))
    state_b = normalize_city_text(b.get("state"))

    # If both regions are known and explicitly different, keep them separate.
    if state_a and state_b and state_a != state_b:
        return False

    # Duplicate city/admin records can have slightly different centroids.
    # 35 km safely collapses central/admin representations of a large city
    # without merging same-name cities in different provinces.
    return distance <= 35000


def city_record_richness(city: dict) -> float:
    score = 0.0
    if city.get("state"):
        score += 5.0
    if city.get("county"):
        score += 2.0
    if city.get("city"):
        score += 2.0
    if city.get("name"):
        score += 1.0
    if city.get("place_id"):
        score += 1.0

    result_type = (city.get("result_type") or "").casefold()
    if result_type == "city":
        score += 2.5
    elif result_type in {"county", "district"}:
        score += 0.5

    # Keep original search score as a tie-breaker.
    score += float(city.get("_score") or 0) * 0.05
    return score


def merge_city_records(existing: dict, candidate: dict) -> dict:
    """
    Keep the richer record, but backfill any missing administrative fields
    from the other duplicate.
    """
    if city_record_richness(candidate) > city_record_richness(existing):
        best, other = dict(candidate), existing
    else:
        best, other = dict(existing), candidate

    for field in ("state", "county", "city", "name", "formatted", "place_id"):
        if not best.get(field) and other.get(field):
            best[field] = other[field]

    return best


def city_candidate_label(city: dict) -> str:
    name = city.get("city") or city.get("name") or city.get("formatted") or "Город"
    state = region_ru(city.get("state"))
    county = admin_ru(city.get("county"))

    if county and normalize_city_text(county) != normalize_city_text(name):
        return f"{name} · {state} · {county}"
    return f"{name} · {state}"


def city_choices_keyboard(candidates, indexes=None):
    kb = InlineKeyboardBuilder()

    if indexes is None:
        indexes = list(range(len(candidates)))

    for candidate, original_index in zip(candidates[:5], indexes[:5]):
        label = city_candidate_label(candidate)
        kb.button(
            text=f"{original_index+1}. {short_text(label, 52)}",
            callback_data=f"city_select:{original_index}",
        )

    kb.button(text="⌨️ Ввести город вручную", callback_data="city_manual")
    kb.adjust(1)
    return kb.as_markup()


def main_menu(has_active=False):
    kb = InlineKeyboardBuilder()
    if has_active:
        kb.button(text="▶️ Продолжить квест", callback_data="resume_quest")
    kb.button(text="🧭 Создать квест", callback_data="new_quest")
    kb.button(text="📷 Спросить по фото", callback_data="free_photo")
    kb.button(text="🎒 Мои приключения", callback_data="my_quests")
    kb.button(text="ℹ️ Как это работает", callback_data="about")
    kb.adjust(1)
    return kb.as_markup()


def city_confirmation_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Это он", callback_data="city_confirm")
    kb.button(text="✏️ Другой город", callback_data="city_retry")
    kb.adjust(1)
    return kb.as_markup()


def duration_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ 2 часа", callback_data="duration_2")
    kb.button(text="🌿 4 часа", callback_data="duration_4")
    kb.button(text="🏮 6 часов", callback_data="duration_6")
    kb.button(text="🌅 Весь день", callback_data="duration_day")
    kb.adjust(2)
    return kb.as_markup()


def interests_keyboard(selected=None):
    selected = set(selected or [])
    kb = InlineKeyboardBuilder()
    for key, meta in INTERESTS.items():
        kb.button(
            text=f"{'✅ ' if key in selected else ''}{meta['label']}",
            callback_data=f"interest:{key}",
        )
    kb.adjust(2)
    markup = kb.as_markup()
    if selected:
        markup.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"Продолжить ({len(selected)}/3) →",
                callback_data="interests_continue",
            )
        ])
    return markup


def style_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="😌 Спокойно", callback_data="style:calm")
    kb.button(text="🔎 Исследователь", callback_data="style:explorer")
    kb.button(text="🔥 Приключение", callback_data="style:adventure")
    kb.adjust(1)
    return kb.as_markup()


def clean_category_label(categories):
    cats = categories or []

    def has(prefix):
        return any(c == prefix or c.startswith(prefix + ".") for c in cats)

    if has("catering.cafe.tea"): return "🍵 чайная"
    if has("catering.restaurant"): return "🍜 ресторан"
    if has("catering.food_court"): return "🍽 фуд-корт"
    if has("catering.fast_food"): return "🥡 кафе / стритфуд"
    if has("catering.cafe"): return "☕ кафе"
    if has("commercial.marketplace"): return "🛍 рынок"
    if has("entertainment.museum"): return "🏛 музей"
    if has("tourism.sights.place_of_worship"): return "🛕 храм / религиозное место"
    if has("tourism.sights.city_gate"): return "🏮 исторические ворота"
    if has("tourism.attraction.viewpoint"): return "📸 смотровая точка"
    if has("tourism.attraction.artwork"): return "🎨 арт-объект"
    if has("leisure.park.garden"): return "🌺 сад"
    if has("leisure.park"): return "🌿 парк"
    if has("natural"): return "🌿 природное место"
    if has("entertainment.culture"): return "🎭 культурное место"
    if has("heritage"): return "🏯 историческое место"
    if has("tourism.sights"): return "🏯 достопримечательность"
    if has("tourism.attraction"): return "📍 достопримечательность"
    return "📍 интересное место"


def place_group(place):
    label = (place.get("category_label") or "").lower()
    if "ресторан" in label: return "restaurant"
    if "чайная" in label: return "tea"
    if "кафе" in label or "фуд-корт" in label or "стритфуд" in label: return "cafe"
    if "рынок" in label: return "market"
    if "парк" in label or "сад" in label or "природ" in label: return "park"
    if "музей" in label: return "museum"
    if "храм" in label or "религиоз" in label: return "temple"
    if "истор" in label or "ворота" in label or "достопримеч" in label: return "heritage"
    if "арт" in label or "культур" in label: return "art"
    if "смотров" in label: return "viewpoint"
    return "other"


def is_food_group(group):
    return group in {"restaurant", "tea", "cafe"}


GLOBAL_CHAIN_PATTERNS = [
    "starbucks", "星巴克",
    "mcdonald", "麦当劳",
    "kfc", "肯德基",
    "pizza hut", "必胜客",
    "burger king", "汉堡王",
    "subway",
    "costa",
]


def is_global_chain(place):
    name = str(place.get("name") or "").casefold()
    return any(pattern.casefold() in name for pattern in GLOBAL_CHAIN_PATTERNS)


def prefer_local_places(places):
    """
    Keep chains as fallback for sparse cities, but if we have enough local POIs,
    remove global chains from the candidate pool.
    """
    if not places:
        return places

    local = [p for p in places if not is_global_chain(p)]
    chains = [p for p in places if is_global_chain(p)]

    # If there are enough local candidates and enough category diversity,
    # don't let a familiar global chain displace a more useful local stop.
    if len(local) >= 5 and len({place_group(p) for p in local}) >= 3:
        return local

    # Sparse city: keep chains at the END as emergency fallbacks.
    return local + chains


def is_outdoor_social_place(place):
    return place_group(place) in {"park", "market", "heritage", "temple", "viewpoint", "art", "other"}


def pinyin_without_tones(text: str) -> str:
    if not contains_han(text):
        return ""
    parts = lazy_pinyin(text, style=Style.NORMAL, errors=lambda chars: [chars])
    return " ".join(p for p in parts if p).strip().title()


def translated_suffix_name(original: str) -> str:
    """Safe human label from literal Chinese POI suffixes; no invented proper-name translation."""
    pinyin = pinyin_without_tones(original)

    suffixes = [
        ("牌楼", "мемориальная арка"),
        ("牌坊", "мемориальная арка"),
        ("鼓楼", "Барабанная башня"),
        ("钟楼", "Колокольная башня"),
        ("博物馆", "музей"),
        ("美术馆", "художественный музей"),
        ("纪念馆", "мемориальный музей"),
        ("城墙", "городская стена"),
        ("广场", "площадь"),
        ("公园", "парк"),
        ("花园", "сад"),
        ("寺", "храм"),
        ("庙", "храм"),
        ("塔", "пагода / башня"),
        ("市场", "рынок"),
        ("茶馆", "чайная"),
        ("茶楼", "чайная"),
        ("餐厅", "ресторан"),
        ("饭店", "ресторан"),
        ("街", "улица / квартал"),
    ]

    for suffix, ru_type in suffixes:
        if original.endswith(suffix):
            base = original[:-len(suffix)]
            if suffix in {"鼓楼", "钟楼"} and not base:
                return ru_type
            if pinyin and base:
                base_py = pinyin_without_tones(base)
                if base_py:
                    return f"{ru_type.capitalize()} {base_py}"
            return ru_type.capitalize()

    if "咖啡" in original:
        return f"Кафе {pinyin}" if pinyin else "Кафе"

    return ""


def safe_russian_name(place):
    """
    Never let AI rename a real POI.
    Priority: verified Russian international name -> known mapping -> literal suffix -> safe category.
    """
    original = str(place.get("name") or "").strip()

    verified_ru = str(place.get("verified_name_ru") or "").strip()
    if verified_ru and contains_cyrillic(verified_ru):
        return short_text(verified_ru, 55)

    if original in EXACT_RU_NAMES:
        return EXACT_RU_NAMES[original]

    suffix_name = translated_suffix_name(original)
    if suffix_name:
        return short_text(suffix_name, 55)

    lower = original.casefold()
    if original and not contains_han(original):
        if "muslim street" in lower:
            return "Мусульманская улица / рынок"
        if "market" in lower:
            return f"Рынок · {original}"
        if "museum" in lower:
            return f"Музей · {original}"
        if "temple" in lower:
            return f"Храм · {original}"
        if "park" in lower:
            return f"Парк · {original}"
        if "tea" in lower:
            return f"Чайная · {original}"
        if "restaurant" in lower:
            return f"Ресторан · {original}"
        if "cafe" in lower or "coffee" in lower:
            return f"Кафе · {original}"
        return short_text(original, 55)

    category = re.sub(
        r"^[^\wА-Яа-яЁё]+",
        "",
        place.get("category_label") or "интересное место",
    ).capitalize()
    pinyin = pinyin_without_tones(original)
    return f"{category} · {pinyin}" if pinyin else category


async def fetch_place_details(session, place):
    """Enrich only final route points, so we do not spend requests on all candidates."""
    place_id = place.get("place_id")
    if not place_id:
        return place

    url = "https://api.geoapify.com/v2/place-details"
    params = {
        "id": place_id,
        "features": "details,details.names",
        "lang": "ru",
        "apiKey": GEOAPIFY_API_KEY,
    }

    try:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                logger.warning("Place Details %s HTTP %s", place_id, response.status)
                return place
            data = await response.json()
    except Exception:
        logger.exception("Place Details failed for %s", place_id)
        return place

    details_features = [
        f for f in data.get("features", [])
        if (f.get("properties") or {}).get("feature_type") == "details"
    ]
    if not details_features:
        return place

    props = details_features[0].get("properties") or {}
    enriched = dict(place)

    categories = props.get("categories") or place.get("categories") or []
    if categories:
        enriched["categories"] = categories
        enriched["category_label"] = clean_category_label(categories)

    original_name = str(props.get("name") or "").strip()
    if original_name:
        enriched["name"] = original_name
        enriched["pinyin"] = place_pinyin(original_name)

    international = props.get("name_international") or {}
    if isinstance(international, dict):
        ru_name = international.get("ru")
        if ru_name and contains_cyrillic(str(ru_name)):
            enriched["verified_name_ru"] = str(ru_name).strip()

    name_other = props.get("name_other") or {}
    enriched["details"] = {
        "description": props.get("description"),
        "historic": props.get("historic") or {},
        "building": props.get("building") or {},
        "name_other": name_other if isinstance(name_other, dict) else {},
    }

    return enriched


async def enrich_final_places(places):
    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        enriched = await asyncio.gather(
            *[fetch_place_details(session, place) for place in places]
        )
    return list(enriched)


async def _fetch_city_variant(session, url, query):
    params = {
        "text": query,
        "filter": "countrycode:cn",
        "type": "city",
        "limit": 20,
        "format": "json",
        "lang": "en",
        "apiKey": GEOAPIFY_API_KEY,
    }

    try:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                logger.warning("City geocode %s HTTP %s", query, response.status)
                return query, []
            data = await response.json()
            return query, data.get("results", [])
    except asyncio.TimeoutError:
        logger.warning("City geocode timed out: %s", query)
        return query, []
    except Exception:
        logger.exception("City geocode query failed: %s", query)
        return query, []


async def geocode_city_candidates(city_query):
    """
    v5.3 exact city matching, but query variants run in parallel.
    The whole lookup is bounded so the user never waits for minutes.
    """
    url = "https://api.geoapify.com/v1/geocode/search"
    variants = city_query_variants(city_query)
    targets = city_target_norms(city_query)
    collected = {}

    # Limit the number of variants: exact original/official/pinyin forms are enough.
    variants = variants[:8]

    request_timeout = aiohttp.ClientTimeout(
        total=10,
        connect=5,
        sock_connect=5,
        sock_read=8,
    )

    try:
        async with aiohttp.ClientSession(timeout=request_timeout) as session:
            tasks = [
                asyncio.create_task(_fetch_city_variant(session, url, query))
                for query in variants
            ]

            # Hard cap for the complete multi-query search.
            try:
                query_results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=False),
                    timeout=12,
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                logger.warning("Overall city lookup timeout for %s", city_query)
                query_results = []

    except Exception:
        logger.exception("City lookup session failed for %s", city_query)
        query_results = []

    variant_index = {query: i for i, query in enumerate(variants)}

    for query, results in query_results:
        v_index = variant_index.get(query, 99)

        for item in results:
            if (item.get("country_code") or "").lower() != "cn":
                continue

            result_type = (item.get("result_type") or "").lower()
            if result_type not in {"city", "county", "district"}:
                continue

            lat = item.get("lat")
            lon = item.get("lon")
            if lat is None or lon is None:
                continue

            # Core v5.3 rule: the settlement name itself must match,
            # not merely appear somewhere in the formatted address.
            if not candidate_matches_requested_name(item, targets):
                continue

            place_id = item.get("place_id")
            dedupe_key = place_id or f"{float(lat):.5f}:{float(lon):.5f}"

            rank = item.get("rank") or {}
            confidence_city = float(rank.get("confidence_city_level") or 0)
            confidence = float(rank.get("confidence") or 0)
            popularity = float(rank.get("popularity") or 0)
            specificity_bonus = max(0, 1.2 - v_index * 0.08)

            score = (
                confidence_city * 5.0
                + confidence * 3.0
                + min(popularity, 20.0) * 0.08
                + specificity_bonus
                + candidate_admin_bonus(item, targets)
            )

            candidate = {
                "place_id": place_id,
                "formatted": item.get("formatted") or city_query,
                "name": item.get("name"),
                "city": item.get("city") or item.get("name") or city_query,
                "county": item.get("county"),
                "state": item.get("state"),
                "result_type": result_type,
                "lat": float(lat),
                "lon": float(lon),
                "input_name": city_query,
                "_score": score,
            }

            old = collected.get(dedupe_key)
            if not old or candidate["_score"] > old["_score"]:
                collected[dedupe_key] = candidate

    ranked = sorted(
        collected.values(),
        key=lambda x: x.get("_score", 0),
        reverse=True,
    )

    final = []
    for candidate in ranked:
        merged = False

        for i, existing in enumerate(final):
            if same_city_nearby(candidate, existing):
                final[i] = merge_city_records(existing, candidate)
                merged = True
                break

        if not merged:
            final.append(candidate)

    # Sort AFTER merging so richer records with province/admin data come first.
    final.sort(key=city_record_richness, reverse=True)

    for item in final:
        item.pop("_score", None)

    return final[:5]


async def fetch_places_source(session, city, source_key, categories, limit=20):
    url = "https://api.geoapify.com/v2/places"
    spatial_filter = (
        f"place:{city['place_id']}"
        if city.get("place_id")
        else f"circle:{city['lon']},{city['lat']},15000"
    )

    params = {
        "categories": ",".join(categories),
        "filter": spatial_filter,
        "bias": f"proximity:{city['lon']},{city['lat']}",
        "limit": limit,
        "lang": "zh",
        "apiKey": GEOAPIFY_API_KEY,
    }

    async with session.get(url, params=params) as response:
        if response.status != 200:
            return []
        data = await response.json()

    output = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = (props.get("name") or "").strip()
        if not name or re.match(r"^\d{5,6}(?:\s|$)", name):
            continue
        if props.get("lat") is None or props.get("lon") is None:
            continue

        raw_categories = props.get("categories") or []
        output.append({
            "place_id": props.get("place_id") or "",
            "name": name,
            "pinyin": place_pinyin(name),
            "category_label": clean_category_label(raw_categories),
            "categories": raw_categories,
            "lat": float(props["lat"]),
            "lon": float(props["lon"]),
            "interest_matches": [source_key],
        })
    return output


def merge_place_results(result_sets, source_defs, max_items=32):
    merged = {}
    per_source = {key: [] for key, _ in source_defs}

    for (source_key, _), places in zip(source_defs, result_sets):
        for place in places:
            unique = place.get("place_id") or f"{place['name']}:{place['lat']:.5f}:{place['lon']:.5f}"
            if unique in merged:
                if source_key not in merged[unique]["interest_matches"]:
                    merged[unique]["interest_matches"].append(source_key)
            else:
                merged[unique] = place

            if unique not in per_source[source_key]:
                per_source[source_key].append(unique)

    ordered, seen = [], set()

    # Round-robin prevents one broad category from taking over the pool.
    for pos in range(20):
        for source_key, _ in source_defs:
            arr = per_source[source_key]
            if pos < len(arr) and arr[pos] not in seen:
                seen.add(arr[pos])
                ordered.append(arr[pos])
            if len(ordered) >= max_items:
                break
        if len(ordered) >= max_items:
            break

    for key in merged:
        if key not in seen and len(ordered) < max_items:
            ordered.append(key)

    return [merged[k] for k in ordered]


def pool_stats(places):
    groups = [place_group(p) for p in places]
    return {
        "count": len(places),
        "groups": len(set(groups)),
        "group_names": sorted(set(groups)),
    }


def pool_needs_broadening(places):
    stats = pool_stats(places)
    return stats["count"] < 10 or stats["groups"] < 4


async def search_places(city, interests, duration):
    """
    First search exactly what the user asked for.
    If the map is sparse, broaden discovery with neutral city categories.
    This never means "the city has nothing"; it only compensates for map coverage.
    """
    primary_sources = [(key, INTERESTS[key]["categories"]) for key in interests]

    if duration != "2 часа" and "tea" not in interests and "food" not in interests:
        primary_sources.append(("rest", REST_CATEGORIES))

    timeout = aiohttp.ClientTimeout(total=40)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        primary_results = await asyncio.gather(*[
            fetch_places_source(session, city, key, cats)
            for key, cats in primary_sources
        ])

        primary_places = merge_place_results(primary_results, primary_sources, max_items=28)

        if not pool_needs_broadening(primary_places):
            return prefer_local_places(primary_places)

        # Sparse map: search broad, safe categories in the same city.
        fallback_results = await asyncio.gather(*[
            fetch_places_source(session, city, key, cats, limit=16)
            for key, cats in FALLBACK_DISCOVERY_SOURCES
        ])

    all_sources = primary_sources + FALLBACK_DISCOVERY_SOURCES
    all_results = primary_results + list(fallback_results)
    return prefer_local_places(merge_place_results(all_results, all_sources, max_items=32))


def suggested_mode_from_pool(places):
    stats = pool_stats(places)

    if stats["count"] >= 10 and stats["groups"] >= 4:
        return "rich"
    if stats["count"] >= 3:
        return "compact"
    if stats["count"] >= 1:
        return "explorer"
    return "none"


def pool_mode_message(mode):
    if mode == "rich":
        return (
            "На карте достаточно разных точек — попробую собрать полноценный маршрут."
        )
    if mode == "compact":
        return (
            "На карте меньше подробно размеченных мест, чем в крупных туристических центрах. "
            "Это нормально: если строгий маршрут не соберётся, бот автоматически сделает компактный квест "
            "из лучших подтверждённых точек."
        )
    if mode == "explorer":
        return (
            "На карте сейчас очень мало подробно размеченных мест. "
            "Это не значит, что в городе нечего смотреть: бот включит исследовательский режим "
            "и использует подтверждённые точки как ориентиры, а часть заданий даст по ходу прогулки."
        )
    return (
        "На карте пока не удалось найти подходящие подробно размеченные точки."
    )


def candidate_summary(places):
    counts = {}
    for p in places:
        group = place_group(p)
        counts[group] = counts.get(group, 0) + 1

    order = [
        ("heritage", "🏯 история и достопримечательности"),
        ("temple", "🛕 храмы"),
        ("museum", "🏛 музеи"),
        ("park", "🌿 парки и сады"),
        ("market", "🛍 рынки и улицы"),
        ("tea", "🍵 чайные"),
        ("restaurant", "🍜 рестораны"),
        ("cafe", "☕ кафе / стритфуд"),
        ("art", "🎨 искусство и культура"),
        ("viewpoint", "📸 смотровые точки"),
        ("other", "📍 другие места"),
    ]
    return "\n".join(
        f"{label}: <b>{counts[key]}</b>"
        for key, label in order
        if counts.get(key)
    )


def haversine(a, b):
    r = 6371000.0
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def route_order_ok(stops):
    groups = [place_group(p) for p in stops]
    return all(not (is_food_group(a) and is_food_group(b)) for a, b in zip(groups, groups[1:]))


def best_order(combo):
    best, best_dist = combo[:], float("inf")
    for start in range(len(combo)):
        remaining = set(range(len(combo)))
        remaining.remove(start)
        order = [start]
        cur = start
        dist = 0.0
        while remaining:
            nxt = min(remaining, key=lambda j: haversine(combo[cur], combo[j]))
            dist += haversine(combo[cur], combo[nxt])
            order.append(nxt)
            remaining.remove(nxt)
            cur = nxt
        candidate = [combo[i] for i in order]
        if route_order_ok(candidate) and dist < best_dist:
            best, best_dist = candidate, dist
    return best, best_dist


def combo_ok(combo, interests):
    groups = [place_group(p) for p in combo]
    food_count = sum(is_food_group(g) for g in groups)

    if food_count > 2:
        return False
    if groups.count("restaurant") > 1:
        return False
    if "restaurant" in groups and sum(g in {"tea", "cafe"} for g in groups) > 1:
        return False
    if groups.count("park") > 2:
        return False
    if groups.count("heritage") > 2:
        return False
    if len(combo) >= 4 and len(set(groups)) < 3:
        return False

    covered = set()
    for p in combo:
        covered.update(k for k in p.get("interest_matches", []) if k in interests)
    return set(interests).issubset(covered)


async def walking_route(stops):
    url = "https://api.geoapify.com/v1/routing"
    params = {
        "waypoints": "|".join(f"{p['lat']},{p['lon']}" for p in stops),
        "mode": "walk",
        "format": "json",
        "type": "balanced",
        "lang": "ru",
        "apiKey": GEOAPIFY_API_KEY,
    }

    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"Routing {response.status}: {body[:300]}")
            data = json.loads(body)

    results = data.get("results") or []
    if not results:
        raise RuntimeError("No route")

    route = results[0]
    return {
        "distance_m": float(route.get("distance") or 0),
        "time_s": float(route.get("time") or 0),
        "legs": [
            {
                "distance_m": float(leg.get("distance") or 0),
                "time_s": float(leg.get("time") or 0),
            }
            for leg in route.get("legs", [])
        ],
    }


def stop_counts(duration):
    # Rich-city target counts: preserve the existing behavior.
    return {
        "2 часа": [3],
        "4 часа": [5, 4],
        "6 часов": [6, 5],
        "весь день": [7, 6],
    }.get(duration, [4])


def compact_stop_counts(duration):
    return {
        "2 часа": [3, 2],
        "4 часа": [4, 3, 2],
        "6 часов": [4, 3, 2],
        "весь день": [5, 4, 3, 2],
    }.get(duration, [3, 2])


def route_fits(route, duration, count, relaxed=False):
    total = DURATION_MINUTES[duration]
    walk = route["time_s"] / 60
    mission_est = 14 * count
    pause = max(15, total * 0.12)

    walk_share = 0.52 if relaxed else 0.45
    leg_limit = MAX_LEG_MINUTES[duration] + (10 if relaxed else 0)

    if walk > total * walk_share:
        return False
    if walk + mission_est + pause > total:
        return False
    if any(leg["time_s"] / 60 > leg_limit for leg in route.get("legs", [])):
        return False
    return True


def relaxed_combo_ok(combo):
    """
    Keep hard UX rules (no food crawl), but stop requiring every selected interest
    to have its own POI in a sparse city.
    """
    groups = [place_group(p) for p in combo]
    food_count = sum(is_food_group(g) for g in groups)

    if food_count > 2:
        return False
    if groups.count("restaurant") > 1:
        return False
    if "restaurant" in groups and sum(g in {"tea", "cafe"} for g in groups) > 1:
        return False

    if len(combo) >= 3 and len(set(groups)) < 2:
        return False

    return True


def covered_interests(places, interests):
    covered = set()
    for p in places:
        covered.update(k for k in p.get("interest_matches", []) if k in interests)

    # These are naturally expressible as mission mechanics even without a dedicated POI.
    for mission_level_interest in {"photo", "tradition", "unusual"}:
        if mission_level_interest in interests:
            covered.add(mission_level_interest)

    return covered


def fake_single_stop_route():
    return {
        "distance_m": 0.0,
        "time_s": 0.0,
        "legs": [],
    }


async def try_route_combinations(pool, wanted_counts, duration, combo_validator, relaxed):
    for wanted in wanted_counts:
        if len(pool) < wanted:
            continue

        scored = []
        for idxs in itertools.combinations(range(len(pool)), wanted):
            combo = [pool[i] for i in idxs]

            if not combo_validator(combo):
                continue

            ordered, approx = best_order(combo)
            if not route_order_ok(ordered):
                continue

            diversity = len(set(place_group(p) for p in combo))
            # Diversity remains valuable, but compactness still matters.
            scored.append((approx - diversity * 260, ordered))

        scored.sort(key=lambda x: x[0])

        for _, ordered in scored[:14]:
            try:
                route = await walking_route(ordered)
            except Exception:
                continue

            if route_fits(route, duration, len(ordered), relaxed=relaxed):
                return ordered, route

    return None


async def select_route(places, interests, duration):
    """
    Adaptive cascade:
    1) existing strict route;
    2) compact route with interests treated as preferences;
    3) explorer route with 2 real anchors;
    4) one confirmed anchor + field missions.
    """
    pool = places[:20]

    # 1. Preserve the current strict behavior when the data supports it.
    strict = await try_route_combinations(
        pool,
        stop_counts(duration),
        duration,
        lambda combo: combo_ok(combo, interests),
        relaxed=False,
    )
    if strict:
        selected, route = strict
        return selected, route, "rich"

    # 2. Compact mode: fewer stops, interests are preferences, hard food rules stay.
    compact = await try_route_combinations(
        pool,
        compact_stop_counts(duration),
        duration,
        relaxed_combo_ok,
        relaxed=True,
    )
    if compact:
        selected, route = compact
        mode = "compact" if len(selected) >= 3 else "explorer"
        return selected, route, mode

    # 3. If routing API cannot connect a sparse set, still try the two closest real anchors.
    if len(pool) >= 2:
        best_pair = None
        best_distance = float("inf")

        for a, b in itertools.combinations(pool, 2):
            if is_food_group(place_group(a)) and is_food_group(place_group(b)):
                continue
            dist = haversine(a, b)
            if dist < best_distance:
                best_pair = [a, b]
                best_distance = dist

        if best_pair:
            ordered, _ = best_order(best_pair)
            try:
                route = await walking_route(ordered)
                if route_fits(route, duration, 2, relaxed=True):
                    return ordered, route, "explorer"
            except Exception:
                pass

    # 4. Last useful fallback: one confirmed real point.
    if pool:
        return [pool[0]], fake_single_stop_route(), "explorer"

    raise RuntimeError("No confirmed POI")


AI_MISSION_MECHANICS = [
    "detail",
    "symbol",
    "text",
    "photo",
    "contrast",
    "nature",
    "color",
    "compare",
    "menu",
    "question",
    "spicy",
    "ingredients",
    "object",
]


AI_META_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "final_challenge": {"type": "string"},
        "missions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "poi_index": {"type": "integer"},
                    "mechanic": {"type": "string", "enum": AI_MISSION_MECHANICS},
                    "title": {"type": "string"},
                    "task": {"type": "string"},
                    "hint": {"type": "string"},
                    "photo": {"type": "string"},
                },
                "required": ["poi_index", "mechanic", "title", "task", "hint", "photo"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "intro", "final_challenge", "missions"],
    "additionalProperties": False,
}


def verified_context_for_ai(place):
    details = place.get("details") or {}
    description = details.get("description")
    if not isinstance(description, str):
        description = ""

    categories = [
        str(value)
        for value in (place.get("categories") or [])[:10]
        if value
    ]

    return {
        "name_ru": safe_russian_name(place),
        "source_name": str(place.get("name") or ""),
        "category": str(place.get("category_label") or ""),
        "address": short_text(place.get("formatted") or "", 180),
        "verified_description": short_text(description, 360),
        "map_categories": categories,
    }


def allowed_mechanics_for_place(place, style):
    group = place_group(place)

    allowed = {
        "heritage": {"detail", "symbol", "text", "photo", "contrast"},
        "temple": {"detail", "symbol", "text", "photo", "contrast"},
        "park": {"nature", "color", "photo", "contrast", "detail"},
        "tea": {"compare", "menu", "text"},
        "restaurant": {"menu", "spicy", "ingredients", "text"},
        "cafe": {"menu", "spicy", "ingredients", "text"},
        "market": {"menu", "object", "symbol", "text", "photo", "detail"},
        "museum": {"detail", "question", "text", "photo"},
        "art": {"photo", "detail", "color", "contrast"},
        "viewpoint": {"photo", "detail", "color", "contrast"},
        "other": {"detail", "text", "photo", "contrast"},
    }.get(group, {"detail", "text", "photo"})

    # Calm mode should never require talking to a stranger or staff member.
    if style == "calm":
        allowed.discard("question")

    return allowed


def ai_rules_for_place(place, style):
    group = place_group(place)
    allowed = sorted(allowed_mechanics_for_place(place, style))

    guidance = {
        "heritage": (
            "наблюдение за реально видимой деталью, формой, надписью, контрастом или фотокомпозицией. "
            "Не придумывай, где именно находится деталь: никаких «на задней стенке», «слева от входа», "
            "«на крыше» и т.п., если этого нет в verified_description"
        ),
        "temple": (
            "наблюдение за реально видимой деталью, символом, формой, надписью или фотокомпозицией; "
            "не утверждай значение символа заранее и не придумывай его расположение"
        ),
        "park": "простое наблюдение за природой, цветом, отражением или сочетанием природы и города; формулируй конкретное действие без фотографического жаргона",
        "tea": (
            "сравнение двух вариантов по меню/аромату или чтение названия. Если нужно обратиться к сотруднику, "
            "не сочиняй вопрос по-русски: напиши, что ниже бот покажет готовую китайскую фразу. Покупка не обязательна"
        ),
        "restaurant": (
            "понятная задача для русскоязычного туриста: сфотографировать блюдо, его название или строку меню, "
            "а затем использовать AI, чтобы перевести название, предположить состав или понять, острое ли блюдо. "
            "Если по фото неясно, бот даст готовую китайскую фразу для сотрудника. Не требуй самому формулировать вопрос"
        ),
        "cafe": (
            "понятная задача для русскоязычного туриста: сфотографировать блюдо/напиток, название или строку меню, "
            "а затем использовать AI для перевода и объяснения. Если по фото неясно, бот даст готовую китайскую фразу. "
            "Не требуй самому формулировать вопрос"
        ),
        "market": "еда/меню ИЛИ предмет/декор/вывеска/символ; пользователь сам выбирает находку; ничего покупать не нужно",
        "museum": "выбор реально увиденного объекта/изображения/детали или вопрос к нему; обязательно учитывать запрет на съёмку, если он есть",
        "art": "простой выбор ракурса, цвета или заметной формы; объясняй действие обычными словами, без терминов про композицию и визуальный ритм",
        "viewpoint": "выбрать понятный ракурс, заметить цвет или свет и сделать фото; не использовать фотографический жаргон",
        "other": "надпись, форма, цвет, необычная видимая деталь, контраст или фотокомпозиция",
    }.get(group, "наблюдение за реально видимой деталью, надписью, формой или фотокомпозицией")

    return {
        "group": group,
        "allowed_mechanics": allowed,
        "guidance": guidance,
    }


async def groq_meta(city, duration, interests, style, places, avoid_missions=None):
    fallback_missions = safe_fallback_missions(places, interests)
    interest_text = ", ".join(INTERESTS[k]["label"] for k in interests)

    poi_payload = []
    for i, (place, fallback) in enumerate(zip(places, fallback_missions), 1):
        poi_payload.append({
            "poi_index": i,
            "verified_place": verified_context_for_ai(place),
            "rules": ai_rules_for_place(place, style),
            "safe_baseline": {
                "title": fallback["title"],
                "task": fallback["text"],
                "hint": fallback["tip"],
                "photo": fallback["photo"],
            },
        })

    old_text = "\n".join(f"- {item}" for item in (avoid_missions or [])) or "- нет"

    prompt = f"""
Ты создаёшь персональный CityQuest China для русскоязычного туриста.

КРИТИЧЕСКАЯ АРХИТЕКТУРА:
- Все места ниже уже найдены и проверены картографическим API.
- Ты НЕ выбираешь POI, НЕ меняешь их названия, адреса, категории или координаты.
- Ты создаёшь только творческую формулировку миссии ВНУТРИ разрешённого коридора для каждого конкретного POI.
- Если verified_description пустой, у тебя НЕТ подтверждённых исторических фактов об этом месте.

Город: {city_display_ru(city)}
Время: {duration}
Интересы пользователя: {interest_text}
Стиль: {STYLE_LABELS[style]}

ПОЧЕМУ НУЖНА ПЕРСОНАЛИЗАЦИЯ:
Миссии в разных городах и у разных точек не должны ощущаться одним и тем же шаблоном.
Учитывай название конкретной точки, её подтверждённую категорию, интересы и стиль пользователя.
Не ограничивайся заменой названия города в одинаковом тексте.

ЖЁСТКИЕ ПРАВИЛА БЕЗОПАСНОСТИ И ФАКТОВ:
1. Используй ровно один объект из списка на каждый poi_index; верни ровно {len(places)} missions.
2. mechanic должен входить в allowed_mechanics именно этого POI.
3. Нельзя придумывать исторические факты, цены, часы работы, экспонаты, блюда, архитектурные элементы или традиции, которых нет в verified_description.
4. Если конкретная деталь не подтверждена, формулируй задачу открыто: «найди одну видимую деталь», «выбери то, что заметишь», «если увидишь…».
5. Никаких «здесь обязательно есть дракон/лев/фреска/блюдо». Наблюдение должно оставаться выполнимым даже если конкретного элемента нет.
6. Еда/меню/чай допустимы только там, где rules это разрешают.
7. Не требуй покупки, заказа, употребления еды/напитка или платной услуги.
8. Не требуй заходить в закрытые зоны, перелезать ограждения, выходить на дорогу, нарушать правила объекта.
9. Не требуй фотографировать незнакомых людей. Если взаимодействие разрешено стилем и категорией — оно должно быть коротким, вежливым и необязательным.
10. Турист не обязан знать китайский. Не вставляй китайские фразы: приложение добавляет проверенные фразы само.
11. Пиши только по-русски, естественно, конкретно, без Markdown и без рекламных формулировок.
12. Пиши как хороший русскоязычный travel-гид, а не как техническое задание. Пользователь должен понять действие с первого чтения.
13. В task сначала скажи ЧТО сделать. В hint — КАК это сделать проще. В photo — ЧТО именно сфотографировать.
14. Не используй канцелярит, фотографический жаргон и расплывчатые команды вроде:
   «разложи в отдельный кадр», «ингредиентное сочетание», «какой вкус доминирует»,
   «подойди вежливо», «сфокусируйся», «учитывай растения/здания/одежду»,
   «проанализируй композицию», «сравни линии и масштаб», «задай вопрос: ...».
   Не пиши человеку, что он должен что-то «учитывать» или «анализировать», если не объяснено конкретное действие.
15. Вместо «подойди вежливо» пиши естественно: «Если хочешь уточнить у сотрудника, используй готовую фразу ниже».
16. Если туристу нужно что-то узнать о еде, задача должна быть выполнима без знания китайского:
   «Сфотографируй блюдо или его название. AI поможет перевести название, предположить состав или понять, острое ли оно.
   Если по фото неясно, используй готовую фразу ниже».
17. Не проси русскоязычного туриста самостоятельно произносить сложный вопрос по-китайски и не оставляй ему вопрос только на русском.
18. Не придумывай расположение деталей внутри объекта: «на задней стенке», «у левой двери», «на крыше» и т.п.,
   если этого нет в verified_description. Говори: «найди хорошо видимую деталь», «выбери надпись, которую заметишь».
19. Поле photo должно звучать естественно и начинаться с понятного действия: «Сфотографируй...», «Сделай фото...» или «Сними...».
20. Перед отправкой мысленно проверь каждую миссию: понял бы русскоязычный турист без дополнительных объяснений, что сделать прямо сейчас?
21. Каждая миссия должна заметно отличаться от других по механике или наблюдению.

МИССИИ ИЗ ПРЕДЫДУЩИХ КВЕСТОВ ЭТОГО ПОЛЬЗОВАТЕЛЯ В ЭТОМ ГОРОДЕ — НЕ ПОВТОРЯЙ ИХ, ЕСЛИ МОЖНО:
{old_text}

РЕАЛЬНЫЕ ТОЧКИ И БЕЗОПАСНЫЕ КОРИДОРЫ:
{json.dumps(poi_payload, ensure_ascii=False, indent=2)}

Верни JSON по схеме:
- title: атмосферное название всего квеста на русском;
- intro: 1–2 коротких предложения;
- final_challenge: короткий финальный фото/рефлексивный челлендж;
- missions: для каждого poi_index ровно одна миссия: mechanic, title, task, hint, photo.
""".strip()

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Только русский JSON по схеме. Реальные POI неизменяемы. "
                    "Персонализируй миссии, но не придумывай факты."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "cityquest_ai_missions_v2",
                "strict": True,
                "schema": AI_META_SCHEMA,
            },
        },
    }

    timeout = aiohttp.ClientTimeout(total=75)
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    body = await response.text()
                    if response.status == 200:
                        data = json.loads(body)
                        result = json.loads(data["choices"][0]["message"]["content"])
                        if isinstance(result, dict):
                            return result
                    logger.error("Groq quest HTTP %s: %s", response.status, body[:700])
        except Exception:
            logger.exception("Groq quest AI Missions v2")
        if attempt == 0:
            await asyncio.sleep(2)

    # Important: the current template mission engine remains the fallback.
    return {}


def phrase_text(phrase):
    if not phrase:
        return ""
    return (
        "\n\n📱 <b>ПОКАЖИ ЭКРАН СОТРУДНИКУ ИЛИ СПРОСИ САМ</b>\n"
        f"🇨🇳 <b>{esc(phrase['hanzi'])}</b>\n"
        f"🔤 <i>{esc(phrase['pinyin'])}</i>\n"
        f"🗣 <b>Примерно:</b> {esc(phrase['ru'])}\n"
        f"💬 {esc(phrase['translation'])}"
    )


def reason_for_place(place, interests):
    """Reason is derived from verified place group; AI cannot contradict it."""
    group = place_group(place)
    matches = set(place.get("interest_matches") or [])
    chosen = set(interests)

    if is_global_chain(place):
        primary = (
            "Знакомая сетевая точка как запасная гастрономическая пауза; "
            "можно сравнить меню с тем, что знакомо дома"
        )
    else:
        primary = {
        "restaurant": "Познакомиться с кухней города через меню и незнакомые блюда",
        "cafe": "Сделать гастрономическую паузу и исследовать меню",
        "tea": "Познакомиться с китайской чайной культурой",
        "market": "Увидеть город через еду, вывески и повседневную жизнь",
        "museum": "Добавить в маршрут историю, культуру и реальные экспонаты",
        "temple": "Увидеть традиционную религиозную архитектуру и символы",
        "heritage": "Рассмотреть историческую архитектуру и китайские декоративные детали",
        "park": "Сменить ритм прогулки и увидеть город через природу",
        "art": "Добавить современное или традиционное искусство",
        "viewpoint": "Посмотреть на город с выразительного ракурса",
        "other": "Добавить в маршрут необычную городскую точку",
        }.get(group, "Добавить в маршрут новый тип впечатления")

    # Add only compatible selected interests.
    compatible = []
    if "photo" in chosen and group in {"heritage", "temple", "museum", "park", "art", "viewpoint", "market"}:
        compatible.append("фото")
    if "tradition" in chosen and group in {"heritage", "temple", "museum", "park"}:
        compatible.append("традиции")
    if "history" in chosen and group in {"heritage", "temple", "museum"}:
        compatible.append("история")
    if "food" in chosen and group in {"restaurant", "cafe", "market"}:
        compatible.append("еда")
    if "tea" in chosen and group == "tea":
        compatible.append("чай")
    if "nature" in chosen and group == "park":
        compatible.append("природа")
    if "art" in chosen and group in {"art", "museum"}:
        compatible.append("искусство")

    if compatible:
        return f"{primary} · {' + '.join(compatible)}"
    return primary


def heritage_mission_variants(place):
    return [
        {
            "type": "symbol",
            "title": "Охота на китайский символ",
            "text": (
                "Найди один реально видимый китайский декоративный элемент: дракона, льва, фонарь, "
                "иероглиф, облачный орнамент, красную деталь или необычную форму крыши. "
                "Если ничего из списка нет — выбери любую характерную деталь."
            ),
            "tip": "Загрузи фото находки и нажми «Что за символ?» — AI попробует объяснить то, что действительно видно.",
            "photo": "Сделай крупный кадр найденного элемента.",
            "xp": 30,
            "minutes": 15,
        },
        {
            "type": "roof",
            "title": "Линия крыши",
            "text": (
                "Найди самый выразительный силуэт крыши, ворот или верхней части здания. "
                "Посмотри на него с нескольких шагов в сторону и выбери ракурс, где форма видна лучше всего."
            ),
            "tip": "Не нужно разбираться в архитектуре — просто выбери ракурс, который тебе нравится.",
            "photo": "Сфотографируй крышу или ворота так, чтобы их форма была хорошо видна.",
            "xp": 30,
            "minutes": 12,
        },
        {
            "type": "character",
            "title": "Один иероглиф",
            "text": (
                "Найди любой хорошо видимый китайский иероглиф на табличке, вывеске, воротах или стенде. "
                "Выбери один, который кажется самым интересным по форме."
            ),
            "tip": "После загрузки фото нажми «Что написано?» — бот попробует прочитать его и дать pinyin и перевод.",
            "photo": "Сфотографируй иероглиф достаточно близко, чтобы он был читаем.",
            "xp": 30,
            "minutes": 12,
        },
        {
            "type": "contrast",
            "title": "Старое и новое",
            "text": (
                "Найди в одном направлении традиционный или исторический элемент и что-то явно современное: "
                "вывеску, транспорт, стеклянное здание, одежду или городскую инфраструктуру."
            ),
            "tip": "Цель — поймать контраст эпох, а не доказать возраст объектов.",
            "photo": "Попробуй поместить оба элемента в один кадр.",
            "xp": 20,
            "minutes": 12,
        },
    ]


def park_mission_variants(photo_interest):
    return [
        {
            "type": "park",
            "title": "Поймай город",
            "text": (
                "Найди место, где в одном кадре можно увидеть и природу, и городскую деталь. "
                "Например: дерево и здание, воду и мост, листья и фонарь."
            ),
            "tip": "Выбери сочетание, которое тебе нравится больше всего. Никакого «правильного» варианта нет.",
            "photo": "Сделай фото, где одновременно видны природа и городская деталь.",
            "xp": 20,
            "minutes": 12,
        },
        {
            "type": "color",
            "title": "Три цвета",
            "text": (
                "Оглянись вокруг и найди три цвета, которые здесь часто повторяются. "
                "Выбери один — пусть он станет «цветом этой остановки»."
            ),
            "tip": (
                "Ищи выбранный цвет в разных местах: например, в листве, зданиях, вывесках или дорожках. "
                "Достаточно найти его хотя бы дважды."
            ),
            "photo": "Сделай фото, где выбранный цвет встречается минимум в двух деталях.",
            "xp": 20,
            "minutes": 12,
        },
        {
            "type": "frame",
            "title": "Рамка внутри кадра",
            "text": (
                "Посмотри, можно ли сфотографировать место через ветви, ворота, арку или проём. "
                "Так эти детали будут как рамка вокруг главного объекта."
            ),
            "tip": "Если подходящей рамки нет, просто выбери другой ракурс — искать её специально не нужно.",
            "photo": "Сделай фото через ветви, ворота, арку или другой подходящий проём.",
            "xp": 30 if photo_interest else 20,
            "minutes": 12,
        },
    ]


def stable_variant_index(place, count, salt=0):
    if count <= 1:
        return 0
    token = f"{place.get('place_id') or ''}|{place.get('name') or ''}|{salt}"
    value = sum((i + 1) * ord(ch) for i, ch in enumerate(token))
    return value % count


def choose_unused_variant(variants, used_titles, preferred_index=0):
    if not variants:
        raise ValueError("Mission variants are empty")

    count = len(variants)
    preferred_index = int(preferred_index or 0) % count
    ordered = [variants[(preferred_index + i) % count] for i in range(count)]

    for variant in ordered:
        if variant["title"] not in used_titles:
            return dict(variant)

    fallback = dict(ordered[len(used_titles) % count])
    fallback["title"] = f"{fallback['title']} · другой ракурс"
    return fallback


def clean_ai_mission_value(value, max_len):
    value = str(value or "").strip()
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("**", "").replace("```", "").replace("`", "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_len].rstrip()


def phrase_for_ai_mechanic(place, mechanic, fallback):
    group = place_group(place)

    if group in {"restaurant", "cafe"}:
        if mechanic == "spicy":
            return PHRASES["spicy"]
        if mechanic == "ingredients":
            return PHRASES["inside"]
        if mechanic == "menu":
            return PHRASES["recommend"]
        return None

    if group == "tea":
        if mechanic == "compare":
            return PHRASES["smell"]
        if mechanic == "menu":
            return PHRASES["recommend"]
        return None

    return fallback.get("phrase")


def ai_mission_is_safe(place, candidate, style):
    if not isinstance(candidate, dict):
        return False, "not_object"

    mechanic = str(candidate.get("mechanic") or "").strip()
    if mechanic not in allowed_mechanics_for_place(place, style):
        return False, "mechanic_not_allowed"

    values = {
        "title": clean_ai_mission_value(candidate.get("title"), 70),
        "task": clean_ai_mission_value(candidate.get("task"), 560),
        "hint": clean_ai_mission_value(candidate.get("hint"), 340),
        "photo": clean_ai_mission_value(candidate.get("photo"), 280),
    }

    if any(not value for value in values.values()):
        return False, "empty_field"

    if not contains_cyrillic(values["title"]) or not contains_cyrillic(values["task"]):
        return False, "not_russian"

    combined = " ".join(values.values()).casefold()

    unsafe_patterns = [
        r"перелез",
        r"перепрыгн",
        r"зайди\s+за\s+ограж",
        r"закрыт(?:ую|ой)\s+зон",
        r"служебн(?:ую|ой)\s+зон",
        r"выйди\s+на\s+проезж",
        r"сфотографируй\s+(?:незнаком|прохож|человек)",
        r"сними\s+(?:незнаком|прохож|человек)\s+крупным",
        r"обязательно\s+куп",
        r"\bкупи\b",
        r"\bзакажи\b",
        r"\bсъешь\b",
        r"\bвыпей\b",
        r"попробуй\s+(?:блюдо|еду|напиток|чай)",
    ]
    if any(re.search(pattern, combined) for pattern in unsafe_patterns):
        return False, "unsafe_action"

    awkward_patterns = [
        r"разлож\w*\s+.*кадр",
        r"ингредиентн\w*\s+сочетан",
        r"какой\s+вкус\s+доминиру",
        r"подойди\s+вежливо",
        r"задай\s+вопрос\s*[:«\"]",
        r"сфокусируйся\s+на\s+задн",
        r"\bучитывай\b",
        r"\bпроанализируй\s+композиц",
        r"\bсравни\s+линии\s*,?\s*масштаб",
        r"\bвизуальн\w*\s+ритм",
        r"\bдоминирующ\w*\s+вкус",
    ]
    if any(re.search(pattern, combined) for pattern in awkward_patterns):
        return False, "awkward_russian"

    # Do not let AI invent a precise location of a detail when such a location
    # is not confirmed by verified map/place description.
    verified_description = str(place.get("description") or place.get("verified_description") or "").casefold()
    spatial_patterns = [
        r"на\s+задней\s+стен",
        r"на\s+задней\s+сторон",
        r"слева\s+от\s+вход",
        r"справа\s+от\s+вход",
        r"у\s+левой\s+двер",
        r"у\s+правой\s+двер",
    ]
    for pattern in spatial_patterns:
        match = re.search(pattern, combined)
        if match and not re.search(pattern, verified_description):
            return False, "unverified_spatial_claim"

    # Photo instruction must be an ordinary action, not abstract AI prose.
    photo_value = values["photo"].casefold()
    if not re.search(r"\b(?:сфотографируй|сделай\s+фото|сними|сделай\s+кадр)\b", photo_value):
        return False, "unclear_photo_instruction"

    group = place_group(place)
    if group not in {"restaurant", "cafe", "tea", "market"}:
        food_terms = re.compile(
            r"\b(?:меню|блюд\w*|еда|напит\w*|остр\w*|ингредиент\w*|чай\w*|аромат\w*|вкус\w*)\b"
        )
        if food_terms.search(combined):
            return False, "food_mismatch"

    if group != "museum" and re.search(r"\bэкспонат\w*\b", combined):
        return False, "museum_mismatch"

    return True, "ok"


def apply_human_mission_copy(place, mission):
    """
    AI may choose a creative title/mechanic, but for food and tea the
    instructions are deterministic so a Russian-speaking tourist always
    understands exactly what to do.
    """
    result = dict(mission)
    group = place_group(place)
    mechanic = str(result.get("mechanic") or result.get("type") or "").strip()

    if group in {"restaurant", "cafe"}:
        if mechanic == "spicy":
            result["text"] = (
                "Выбери незнакомое блюдо и сфотографируй само блюдо или его название в меню. "
                "AI попробует понять, острое оно или нет."
            )
            result["tip"] = (
                "Если по фото это определить нельзя, используй готовую китайскую фразу ниже. "
                "Её можно показать сотруднику на экране или произнести самому."
            )
            result["photo"] = "Сфотографируй блюдо или его название в меню."
        elif mechanic == "ingredients":
            result["text"] = (
                "Выбери незнакомое блюдо и сфотографируй само блюдо или его название в меню. "
                "AI попробует определить основные ингредиенты или объяснить название."
            )
            result["tip"] = (
                "Если по фото состав непонятен, используй готовую китайскую фразу ниже. "
                "Её можно показать сотруднику или произнести самому."
            )
            result["photo"] = "Сфотографируй блюдо или его название в меню."
        else:
            result["text"] = (
                "Сфотографируй блюдо или его название в меню. "
                "AI попробует перевести название и объяснить, что это за блюдо."
            )
            result["tip"] = (
                "Если по фото информации не хватит, используй готовую китайскую фразу ниже "
                "или выбери другой вопрос после загрузки фото."
            )
            result["photo"] = "Сфотографируй блюдо или его название в меню."

    elif group == "tea":
        if mechanic == "compare":
            result["text"] = (
                "Выбери два вида чая и сфотографируй их названия, упаковки или строки меню. "
                "AI поможет перевести названия и понять, чем эти варианты отличаются."
            )
            result["tip"] = (
                "Покупать оба чая не нужно. Если хочешь уточнить аромат у сотрудника, "
                "используй готовую китайскую фразу ниже."
            )
            result["photo"] = "Сфотографируй названия или упаковки двух видов чая."
        else:
            result["text"] = (
                "Сфотографируй название чая или строку меню. "
                "AI попробует перевести название и объяснить, что это за чай."
            )
            result["tip"] = (
                "Если хочется уточнить рекомендацию у сотрудника, используй готовую китайскую фразу ниже."
            )
            result["photo"] = "Сфотографируй название чая или строку меню."

    return result


def merge_ai_mission(place, fallback, candidate, interests, style):
    safe, reason = ai_mission_is_safe(place, candidate, style)
    if not safe:
        result = dict(fallback)
        result["source"] = "template"
        result["fallback_reason"] = reason
        return apply_human_mission_copy(place, result)

    mechanic = str(candidate.get("mechanic") or "").strip()
    result = dict(fallback)
    result.update({
        "title": clean_ai_mission_value(candidate.get("title"), 70),
        "text": clean_ai_mission_value(candidate.get("task"), 560),
        "tip": clean_ai_mission_value(candidate.get("hint"), 340),
        "photo": clean_ai_mission_value(candidate.get("photo"), 280),
        "mechanic": mechanic,
        "source": "ai_v2",
    })

    phrase = phrase_for_ai_mechanic(place, mechanic, fallback)
    if phrase:
        result["phrase"] = phrase
    else:
        result.pop("phrase", None)

    return apply_human_mission_copy(place, result)


def safe_fallback_missions(places, interests):
    used_titles = set()
    missions = []
    for index, place in enumerate(places):
        missions.append(mission_for_place(place, interests, index, used_titles))
    return missions


def mission_for_place(place, interests, index, used_titles):
    group = place_group(place)
    photo_interest = "photo" in interests

    if group in {"heritage", "temple"}:
        variants = heritage_mission_variants(place)
        mission = choose_unused_variant(
            variants,
            used_titles,
            stable_variant_index(place, len(variants), index),
        )

    elif group == "park":
        variants = park_mission_variants(photo_interest)
        mission = choose_unused_variant(
            variants,
            used_titles,
            stable_variant_index(place, len(variants), index),
        )

    elif group == "tea":
        mission = {
            "type": "tea",
            "title": "Два аромата",
            "text": (
                "Если можно, сравни два чая по аромату. Какой кажется более травянистым, цветочным, "
                "ореховым или просто приятнее? Если понюхать нельзя — сравни два напитка по меню."
            ),
            "tip": "Покупать напиток не обязательно. Если нужно — покажи сотруднику готовую китайскую фразу.",
            "photo": "Сфотографируй меню, название чая, упаковку или чашку.",
            "xp": 30,
            "minutes": 15,
            "phrase": PHRASES["smell"],
        }

    elif group in {"restaurant", "cafe"}:
        variants = [
            {
                "type": "food",
                "title": "Местный выбор",
                "text": (
                    "Найди в меню две незнакомые позиции и выбери одну, которую попробовал бы первой. "
                    "Покупать её не обязательно."
                ),
                "tip": (
                    "Сфотографируй блюдо, его название или строку меню. AI поможет перевести название "
                    "и объяснить, что это за блюдо. Если информации не хватит, ниже будет готовая фраза, "
                    "которую можно показать сотруднику или произнести самому."
                ),
                "photo": "Сфотографируй блюдо или его название в меню.",
                "xp": 20,
                "minutes": 12,
                "phrase": PHRASES["recommend"],
            },
            {
                "type": "food",
                "title": "Острое или нет?",
                "text": (
                    "Выбери одно незнакомое блюдо и попробуй выяснить, острое ли оно. "
                    "Самому читать китайские иероглифы не нужно."
                ),
                "tip": (
                    "Сфотографируй блюдо или его название. AI попробует понять по фото и подписи, "
                    "острое оно или нет. Если определить нельзя, используй готовую фразу ниже — "
                    "её можно показать сотруднику или произнести самому."
                ),
                "photo": "Сфотографируй блюдо или его название в меню.",
                "xp": 20,
                "minutes": 10,
                "phrase": PHRASES["spicy"],
            },
            {
                "type": "food",
                "title": "Что внутри?",
                "text": (
                    "Выбери незнакомое блюдо и попробуй понять, из чего оно приготовлено: "
                    "мясо, рыба, овощи или что-то другое."
                ),
                "tip": (
                    "Сфотографируй блюдо, его название или строку меню. AI попробует определить состав "
                    "или объяснить название. Если по фото этого не понять, используй готовую фразу ниже."
                ),
                "photo": "Сфотографируй блюдо или его название в меню.",
                "xp": 20,
                "minutes": 10,
                "phrase": PHRASES["inside"],
            },
        ]
        mission = choose_unused_variant(variants, used_titles, stable_variant_index(place, len(variants), index))


    elif group == "market":
        variants = [
            {
                "type": "market",
                "title": "Выбери свой трофей рынка",
                "text": (
                    "Выбери ОДИН из двух путей:\n"
                    "🍜 ЕДА — найди незнакомое блюдо, напиток или строку меню;\n"
                    "🧧 ПРЕДМЕТ — найди необычный товар, декор, барабан, фонарь, вывеску, упаковку или другой интересный объект.\n"
                    "Тебе не нужно искать еду, если интереснее что-то другое."
                ),
                "tip": (
                    "После загрузки фото бот сначала даст подходящие варианты анализа: "
                    "для еды — состав и острота, для предмета — что это, символика и надписи."
                ),
                "photo": "Сфотографируй выбранный трофей: еду/меню ИЛИ интересный предмет/декор/вывеску.",
                "xp": 20,
                "minutes": 12,
            },
            {
                "type": "market",
                "title": "Одна непонятная находка",
                "text": (
                    "Найди то, что хочется расшифровать: блюдо, предмет, вывеску, символ или необычный декор. "
                    "Сфотографируй находку и попроси AI помочь разобраться."
                ),
                "tip": "Не фотографируй людей крупным планом без разрешения.",
                "photo": "Сними объект или надпись достаточно близко, чтобы детали были видны.",
                "xp": 20,
                "minutes": 10,
            },
        ]
        mission = choose_unused_variant(variants, used_titles, stable_variant_index(place, len(variants), index))

    elif group == "museum":
        variants = [
            {
                "type": "museum",
                "title": "Один вопрос",
                "text": "Найди предмет или изображение, о котором тебе захотелось бы узнать больше, и придумай к нему один вопрос.",
                "tip": "Если съёмка разрешена, загрузи фото — AI разберёт только то, что действительно видно.",
                "photo": "Если можно — сохрани объект; иначе сфотографируй фасад или название музея.",
                "xp": 30,
                "minutes": 15,
            },
            {
                "type": "museum",
                "title": "Самая странная деталь",
                "text": "Выбери одну деталь экспоната, изображения или фасада, которую ты не ожидал увидеть.",
                "tip": "Не нужно знать ответ заранее — любопытство и вопрос считаются частью миссии.",
                "photo": "Если разрешено, сфотографируй эту деталь.",
                "xp": 20,
                "minutes": 12,
            },
        ]
        mission = choose_unused_variant(variants, used_titles, stable_variant_index(place, len(variants), index))

    elif group in {"art", "viewpoint"}:
        variants = [
            {
                "type": "art",
                "title": "Два ракурса",
                "text": "Посмотри на место с двух разных точек и реши, какой ракурс делает его интереснее.",
                "tip": "Попробуй, например, снять место прямо и немного сбоку или ближе и дальше. Выбери вариант, который нравится больше.",
                "photo": "Сделай фото с того ракурса, который тебе понравился больше.",
                "xp": 30 if photo_interest else 20,
                "minutes": 12,
            },
            {
                "type": "art",
                "title": "Куда ведёт взгляд",
                "text": (
                    "Найди деталь, которая будто ведёт взгляд дальше: дорожку, лестницу, ряд колонн, край стены или ограды."
                ),
                "tip": "Встань так, чтобы эта деталь была хорошо видна от начала до конца.",
                "photo": "Сфотографируй место так, чтобы дорожка, лестница или другая линия уводила взгляд в глубину кадра.",
                "xp": 20,
                "minutes": 12,
            },
        ]
        mission = choose_unused_variant(variants, used_titles, stable_variant_index(place, len(variants), index))

    else:
        variants = [
            {
                "type": "observe",
                "title": "Детектив деталей",
                "text": "Найди три детали, которые отличают эту остановку от предыдущей, и выбери одну, которую хочется запомнить.",
                "tip": "Например, это может быть необычный цвет, вывеска, форма здания, звук или предмет, которого не было на прошлой остановке.",
                "photo": "Сфотографируй выбранную деталь.",
                "xp": 20,
                "minutes": 12,
            },
            {
                "type": "observe",
                "title": "Один неожиданный кадр",
                "text": "Найди что-то, что не похоже на твоё первое представление об этом месте.",
                "tip": "Выбирай то, что действительно удивило именно тебя — правильного ответа здесь нет.",
                "photo": "Сфотографируй то, что показалось неожиданным.",
                "xp": 20,
                "minutes": 10,
            },
        ]
        mission = choose_unused_variant(variants, used_titles, stable_variant_index(place, len(variants), index))

    used_titles.add(mission["title"])
    return mission


def optional_social_bonus(place):
    if not is_outdoor_social_place(place):
        return None
    return {
        "text": "Если комфортно, попроси прохожего сфотографировать тебя. Не хочется обращаться — селфи тоже засчитывается.",
        "xp": 10,
        "phrase": PHRASES["photo"],
    }


def missing_interest_labels(places, interests):
    covered = covered_interests(places, interests)
    return [
        INTERESTS[key]["label"]
        for key in interests
        if key not in covered
    ]


def build_field_missions(mode, interests, real_stop_count):
    if mode == "rich":
        return []

    templates = [
        {
            "title": "Короткая вывеска",
            "text": (
                "По пути найди короткую вывеску или табличку с 2–4 иероглифами. "
                "Не обязательно понимать её — просто выбери ту, что заинтересовала."
            ),
        },
        {
            "title": "Неожиданная деталь",
            "text": (
                "Заметь одну вещь, которой не ожидал здесь увидеть: предмет, оформление окна, транспорт, "
                "двор, упаковку, звук или необычную городскую деталь."
            ),
        },
        {
            "title": "Старое рядом с новым",
            "text": (
                "Попробуй найти в одном направлении что-то традиционное или старомодное и что-то явно современное. "
                "Не нужно доказывать возраст — важен визуальный контраст."
            ),
        },
        {
            "title": "Цвет города",
            "text": (
                "Выбери один цвет, который сегодня часто встречается вокруг, и найди его ещё два раза по пути."
            ),
        },
    ]

    if "food" in interests or "tea" in interests:
        templates.insert(1, {
            "title": "Местный вкус без обязательной остановки",
            "text": (
                "Если по пути встретится меню, витрина, чайная вывеска или необычная еда — просто рассмотри её. "
                "Заходить и покупать ничего не обязательно."
            ),
        })

    if "tradition" in interests or "history" in interests:
        templates.insert(1, {
            "title": "Традиционная деталь по пути",
            "text": (
                "Ищи фонарь, иероглиф, форму крыши, ворота, орнамент или другой визуальный элемент, "
                "который кажется тебе связанным с китайской традицией."
            ),
        })

    if "photo" in interests:
        templates.insert(1, {
            "title": "Один кадр вне маршрута",
            "text": (
                "Сделай один кадр не у основной точки, а просто по дороге — то, что лучше всего передаёт ощущение города."
            ),
        })

    wanted = 2 if mode == "compact" else 3
    wanted = min(wanted, max(1, len(templates)))

    return templates[:wanted]


def adaptive_mode_note(mode, places, interests):
    if mode == "rich":
        return ""

    missing = missing_interest_labels(places, interests)
    missing_text = ""

    if missing:
        missing_text = (
            "\n\nНекоторые выбранные темы не получили отдельной точки на карте: "
            + ", ".join(missing)
            + ". Я не буду отправлять тебя далеко ради формального совпадения — эти темы лучше встроить в задания по пути."
        )

    if mode == "compact":
        return (
            "🟡 <b>Компактный режим</b>\n"
            "На карте здесь меньше подробно размеченных POI, поэтому маршрут короче, "
            "но все точки реальные и проверенные. Между ними будут дополнительные задания наблюдения."
            + missing_text
        )

    return (
        "🟠 <b>Исследовательский режим</b>\n"
        "На карте сейчас мало подробно размеченных мест. Это не означает, что в городе нечего смотреть: "
        "подтверждённые точки станут ориентирами, а часть квеста будет происходить по пути."
        + missing_text
    )


def build_quest(city, interests, style, places, ai_meta, adaptive_mode="rich"):
    stops = []
    social_used = False
    used_titles = set()

    ai_candidates = {}
    raw_missions = ai_meta.get("missions") if isinstance(ai_meta, dict) else None
    if isinstance(raw_missions, list):
        for item in raw_missions:
            if not isinstance(item, dict):
                continue
            try:
                poi_index = int(item.get("poi_index")) - 1
            except Exception:
                continue
            if 0 <= poi_index < len(places) and poi_index not in ai_candidates:
                ai_candidates[poi_index] = item

    accepted_ai = 0

    for i, place in enumerate(places):
        name_ru = safe_russian_name(place)
        reason = reason_for_place(place, interests)
        fallback = mission_for_place(place, interests, i, used_titles)
        mission = merge_ai_mission(
            place,
            fallback,
            ai_candidates.get(i),
            interests,
            style,
        )

        if mission.get("source") == "ai_v2":
            accepted_ai += 1

        # The displayed titles must remain unique even after AI personalization.
        base_title = mission["title"]
        if base_title in {stop["mission"]["title"] for stop in stops}:
            mission["title"] = f"{base_title} · {i+1}"

        bonus = None
        if (
            style == "adventure"
            and not social_used
            and i >= 1
            and is_outdoor_social_place(place)
        ):
            bonus = optional_social_bonus(place)
            social_used = bool(bonus)

        stops.append({
            "place": place,
            "name_ru": name_ru,
            "why_here": reason,
            "mission": mission,
            "bonus": bonus,
        })

    logger.info(
        "AI Missions v2 accepted %s/%s for %s",
        accepted_ai,
        len(places),
        city_display_ru(city),
    )

    return {
        "title": str(ai_meta.get("title") or "").strip() or f"CityQuest · {city_display_ru(city)}",
        "intro": str(ai_meta.get("intro") or "").strip() or (
            "Реальный городской квест: разные типы мест, фото-трофеи и небольшие задания вместо обычного списка достопримечательностей."
        ),
        "stops": stops,
        "adaptive_mode": adaptive_mode,
        "adaptive_note": adaptive_mode_note(adaptive_mode, places, interests),
        "field_missions": build_field_missions(adaptive_mode, interests, len(places)),
        "final_challenge": str(ai_meta.get("final_challenge") or "").strip() or (
            "Выбери лучший кадр прогулки и придумай ему короткое название."
        ),
        "ai_missions_accepted": accepted_ai,
    }


def total_xp(quest):
    return sum(
        int(stop["mission"].get("xp", 20)) + (int(stop["bonus"]["xp"]) if stop.get("bonus") else 0)
        for stop in quest["stops"]
    )


def earned_xp(quest, completed, bonuses):
    completed = set(completed)
    bonuses = set(bonuses)
    total = 0
    for i, stop in enumerate(quest["stops"]):
        if i in completed:
            total += int(stop["mission"].get("xp", 20))
        if i in bonuses and stop.get("bonus"):
            total += int(stop["bonus"].get("xp", 10))
    return total


def checklist_text(quest, completed, bonuses, photos):
    completed_set = set(completed)
    lines = ["✅ <b>Чек-лист квеста</b>", "", "Нажимай на миссию, когда выполнишь её.", ""]

    for i, stop in enumerate(quest["stops"]):
        mark = "✅" if i in completed_set else "☐"
        photo_mark = " 📷" if str(i) in photos else ""
        lines.append(f"{mark} <b>{i+1}.</b> {esc(stop['name_ru'])}{photo_mark}")

    lines += [
        "",
        ("🟩" * len(completed_set)) + ("⬜" * (len(quest["stops"]) - len(completed_set))),
        f"Прогресс: <b>{len(completed_set)}/{len(quest['stops'])}</b>",
        f"⭐ XP: <b>{earned_xp(quest, completed, bonuses)}/{total_xp(quest)}</b>",
        f"📷 Фото: <b>{len(photos)}/{len(quest['stops'])}</b>",
    ]
    return "\n".join(lines)


def checklist_keyboard(quest, completed):
    completed_set = set(completed)
    kb = InlineKeyboardBuilder()

    for i, stop in enumerate(quest["stops"]):
        kb.button(
            text=f"{'✅' if i in completed_set else '☐'} {i+1} · {short_text(stop['name_ru'], 30)}",
            callback_data=f"mission_toggle:{i}",
        )
    kb.adjust(1)

    if len(completed_set) == len(quest["stops"]):
        kb.row(InlineKeyboardButton(text="🏁 Завершить квест", callback_data="quest_finish"))
    return kb.as_markup()


def stop_keyboard(place, index, has_bonus):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="📍 Открыть точку на карте",
        url=f"https://www.openstreetmap.org/?mlat={place['lat']}&mlon={place['lon']}#map=17/{place['lat']}/{place['lon']}",
    ))
    kb.row(
        InlineKeyboardButton(text="📷 Добавить фото", callback_data=f"photo_add:{index}"),
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"mission_toggle:{index}"),
    )
    if has_bonus:
        kb.row(InlineKeyboardButton(text="🎁 Засчитать бонус +10 XP", callback_data=f"bonus_toggle:{index}"))
    return kb.as_markup()


def mission_nav_keyboard(quest, idx):
    kb = InlineKeyboardBuilder()
    row = []

    if idx > 0:
        row.append(InlineKeyboardButton(text=f"⬅️ {idx}", callback_data=f"quest_stop:{idx-1}"))

    row.append(InlineKeyboardButton(text=f"↩️ Миссия {idx+1}", callback_data=f"quest_stop:{idx}"))

    if idx + 1 < len(quest["stops"]):
        row.append(InlineKeyboardButton(text=f"{idx+2} ➡️", callback_data=f"quest_stop:{idx+1}"))

    kb.row(*row)
    kb.row(
        InlineKeyboardButton(text="🔄 Другое фото", callback_data=f"photo_replace:{idx}"),
        InlineKeyboardButton(text="✅ Чек-лист", callback_data="show_checklist"),
    )
    return kb.as_markup()


async def send_stop_card(message, quest, route, index):
    stop = quest["stops"][index]
    place = stop["place"]
    mission = stop["mission"]
    legs = route.get("legs", [])

    transition = ""
    if index > 0 and index - 1 < len(legs):
        leg = legs[index - 1]
        transition = (
            f"\n🚶 <b>От предыдущей:</b> ~{fmt_distance(leg['distance_m'])} · "
            f"~{fmt_minutes(leg['time_s']/60)}\n"
        )

    bonus_text = ""
    if stop.get("bonus"):
        bonus = stop["bonus"]
        bonus_text = (
            f"\n\n🎁 <b>BONUS +{bonus['xp']} XP</b>\n{esc(bonus['text'])}"
            f"{phrase_text(bonus['phrase'])}"
        )

    await message.answer(
        f"📍 <b>{index+1}/{len(quest['stops'])}. {esc(stop['name_ru'])}</b>\n"
        f"{esc(place['category_label'])} — <b>{esc(place['name'])}</b>\n"
        f"<i>{esc(place.get('pinyin'))}</i>"
        f"{transition}\n"
        f"💡 <b>Почему здесь:</b> {esc(stop['why_here'])}\n\n"
        f"🎯 <b>Миссия «{esc(mission['title'])}»:</b>\n{esc(mission['text'])}\n\n"
        f"🧭 <b>Подсказка:</b> {esc(mission['tip'])}\n\n"
        f"📷 <b>Фото-трофей:</b> {esc(mission['photo'])}\n"
        f"⭐ Награда: <b>+{mission['xp']} XP</b>"
        f"{phrase_text(mission.get('phrase'))}"
        f"{bonus_text}",
        reply_markup=stop_keyboard(place, index, bool(stop.get("bonus"))),
    )


def phrase_show_keyboard(keys, idx):
    kb = InlineKeyboardBuilder()
    for key in keys:
        kb.button(text=PHRASES[key]["title"], callback_data=f"phrase:{key}:{idx}")
    kb.adjust(1)
    return kb.as_markup()


def photo_actions_keyboard(stop, index, version=0):
    group = place_group(stop["place"])
    kb = InlineKeyboardBuilder()

    suffix = f":{index}:{int(version)}"

    if group in {"restaurant", "cafe", "tea"}:
        kb.button(text="🍜 Что на фото / в меню?", callback_data=f"vision:menu{suffix}")
        kb.button(text="🌶 Острое или нет?", callback_data=f"vision:spicy{suffix}")
        kb.button(text="🥢 Из чего это?", callback_data=f"vision:ingredients{suffix}")
        kb.button(text="🔤 Прочитать / перевести", callback_data=f"vision:text{suffix}")

    elif group == "market":
        kb.button(text="🍜 Это еда / меню", callback_data=f"vision:menu{suffix}")
        kb.button(text="🧧 Это предмет / символ", callback_data=f"vision:monument{suffix}")
        kb.button(text="🏮 Что здесь традиционного?", callback_data=f"vision:tradition{suffix}")
        kb.button(text="🔤 Прочитать / перевести", callback_data=f"vision:text{suffix}")
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place{suffix}")

    elif group in {"heritage", "temple"}:
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place{suffix}")
        kb.button(text="🗿 Что за памятник / объект?", callback_data=f"vision:monument{suffix}")
        kb.button(text="🧠 Что за символ?", callback_data=f"vision:symbol{suffix}")
        kb.button(text="🏮 Что здесь традиционного?", callback_data=f"vision:tradition{suffix}")
        kb.button(text="🔤 Что написано?", callback_data=f"vision:text{suffix}")

    elif group == "museum":
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place{suffix}")
        kb.button(text="🗿 Что за объект?", callback_data=f"vision:monument{suffix}")
        kb.button(text="🧠 Что можно понять по фото?", callback_data=f"vision:object{suffix}")
        kb.button(text="🔤 Прочитать надпись", callback_data=f"vision:text{suffix}")

    else:
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place{suffix}")
        kb.button(text="🗿 Что за памятник / объект?", callback_data=f"vision:monument{suffix}")
        kb.button(text="🏮 Найти китайские элементы", callback_data=f"vision:tradition{suffix}")
        kb.button(text="📸 Оценить кадр", callback_data=f"vision:photo{suffix}")
        kb.button(text="🔤 Что написано?", callback_data=f"vision:text{suffix}")

    kb.button(text="🔄 Другое фото", callback_data=f"photo_replace:{index}")
    kb.button(text="➡️ Продолжить квест", callback_data=f"photo_continue:{index}")
    kb.button(text="✅ Чек-лист", callback_data="show_checklist")
    kb.adjust(1)
    return kb.as_markup()


def free_photo_actions_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏯 Что это за место?", callback_data="visionfree:place")
    kb.button(text="🗿 Что за памятник / объект?", callback_data="visionfree:monument")
    kb.button(text="🍜 Что это за еда?", callback_data="visionfree:menu")
    kb.button(text="🌶 Острое или нет?", callback_data="visionfree:spicy")
    kb.button(text="🥢 Из чего это?", callback_data="visionfree:ingredients")
    kb.button(text="🏮 Что за символ?", callback_data="visionfree:symbol")
    kb.button(text="🔤 Прочитать / перевести", callback_data="visionfree:text")
    kb.button(text="📸 Оценить кадр", callback_data="visionfree:photo")
    kb.button(text="🔄 Другое фото", callback_data="free_photo")
    kb.adjust(1)
    return kb.as_markup()


def free_photo_nav_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📷 Другой снимок", callback_data="free_photo")
    kb.button(text="🏠 Главное меню", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


async def download_photo_bytes(bot, file_id):
    tg_file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buffer)
    return buffer.getvalue()


def vision_prompt(mode, stop):
    place_name = str(stop.get("name_ru") or "фото пользователя")
    known_place = str((stop.get("place") or {}).get("name") or "").strip()
    category = str((stop.get("place") or {}).get("category_label") or "").strip()

    context = (
        f"Контекст: фото связано с точкой «{place_name}»."
        + (f" Реальное название точки в данных карты: {known_place}." if known_place else "")
        + (f" Категория: {category}." if category else "")
    )

    common = """
КРИТИЧЕСКИЕ ПРАВИЛА ДЛЯ ПОЛЬЗОВАТЕЛЯ:
- Весь пользовательский текст — ТОЛЬКО на русском.
- Не показывай рассуждения, chain-of-thought, внутренний анализ, технические замечания, <think>, Markdown-разметку.
- Можно делать полезное вероятностное определение по фото и читаемым подписям, если оно обосновано.
  Формулируй такие выводы словами «похоже», «вероятно», «по подписи».
- Не выдумывай детали, которых нет на изображении.
- Сначала дай ПОНЯТНЫЙ ВЫВОД обычному туристу, а уже потом короткое объяснение.
- Если используешь незнакомый термин, бренд, соус, специю или китайское название продукта,
  СРАЗУ объясни простыми русскими словами, что это такое и что это значит для пользователя.
  Нельзя писать просто «Lao Gan Ma», «málà», «huājiāo», «dòubànjiàng» без объяснения.
- Для вопроса об остроте первый смысл ответа должен быть одним из:
  «Скорее всего острое», «Скорее всего неострое» или «По фото неясно».
- Китайские слова показывай только как: 汉字 + pinyin + русский перевод.
- Если важное нельзя определить уверенно — скажи это и предложи простой следующий шаг.

Верни ТОЛЬКО JSON:
{
  "title": "короткий понятный заголовок на русском",
  "intro": "главный вывод для туриста, 1–2 короткие фразы",
  "items": [
    {
      "name_cn": "汉字 или пустая строка",
      "pinyin": "pinyin или пустая строка",
      "name_ru": "понятное русское название",
      "detail": "короткое полезное пояснение по-русски"
    }
  ],
  "conclusion": "практичный следующий шаг",
  "uncertain": true
}
""".strip()

    prompts = {
        "place": f"""
{context}
Пользователь спрашивает: «Что это за место / достопримечательность?»
Попробуй определить место по фото.
Используй контекст текущей точки как подсказку, но не подменяй визуальную проверку:
если фото действительно похоже на известную точку — скажи «похоже на...».
Если уверенности мало, попроси снять фасад целиком, табличку или вывеску.
Не придумывай исторические даты и факты.
{common}
""",
        "monument": f"""
{context}
Пользователь спрашивает: «Что за памятник / объект?»
Определи тип объекта и, если возможно, конкретное название.
Если это статуя, арка, башня, лев, барабан, стела, павильон и т.п. — объясни простыми словами.
Если конкретное имя не читается и не узнаётся — не выдумывай его.
{common}
""",
        "symbol": f"""
{context}
Пользователь выполняет миссию про китайский символ.
Определи видимый декоративный или культурный элемент.
Если значение известно достаточно уверенно — объясни его 1–2 простыми фразами.
{common}
""",
        "tradition": f"""
{context}
Найди реально видимые элементы китайской традиционной визуальной культуры:
крыша, ворота, фонарь, каллиграфия, орнамент, цветовая символика и т.п.
Не утверждай возраст и историю без оснований.
{common}
""",
        "text": f"""
{context}
Прочитай только достаточно уверенно видимый китайский текст.
Для каждой короткой надписи дай 汉字, pinyin и понятный русский перевод.
Неразборчивое не угадывай.
{common}
""",
        "menu": f"""
{context}
Это фото меню, блюда, напитка, упаковки или вывески.
Если читается название — дай 汉字, pinyin и понятное русское название блюда.
Если можно примерно определить блюдо по подписи и фото — можно так и сказать.
Объясняй незнакомые соусы, специи и названия простыми русскими словами.
Если состав неясен — не фантазируй: предложи спросить сотрудника.
{common}
""",
        "spicy": f"""
{context}
Главный вопрос: острое это или нет?
Ищи явные признаки в тексте и названии: 辣, 麻辣, 香辣, 辣椒, 老干妈 и т.п.
Если видишь Lao Gan Ma / 老干妈 — объясни, что это популярный китайский соус/масло с чили,
поэтому блюдо, вероятно, острое, но степень остроты по меню не определить.
Не делай вывод только по красному цвету блюда.
{common}
""",
        "ingredients": f"""
{context}
Определи состав по читаемому названию/описанию и очевидным ингредиентам.
Разрешено писать «похоже на...» там, где это визуальная оценка.
Незнакомые ингредиенты и соусы сразу объясняй простыми русскими словами.
Если неясно, предложи конкретный вопрос сотруднику.
{common}
""",
        "object": f"""
{context}
Опиши, что реально видно, и что можно осторожно определить об объекте.
Не придумывай название, дату, автора или историю, если этого нет на фото/табличке.
{common}
""",
        "photo": f"""
{context}
Дай короткую оценку кадра: что уже работает и 2 конкретных совета по композиции.
Пиши как дружелюбный travel-помощник, без профессионального жаргона.
{common}
""",
    }
    return prompts.get(mode, prompts["object"]).strip()


async def analyze_photo_with_groq(image_bytes, mode, stop):
    if len(image_bytes) > 3_000_000:
        raise RuntimeError("Photo is too large")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"

    payload = {
        "model": GROQ_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": vision_prompt(mode, stop)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_completion_tokens": 900,
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    url = "https://api.groq.com/openai/v1/chat/completions"
    timeout = aiohttp.ClientTimeout(total=90)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            body = await response.text()
            if response.status != 200:
                logger.error("Vision HTTP %s: %s", response.status, body[:600])
                raise RuntimeError("Vision API error")

            data = json.loads(body)
            raw = str(data["choices"][0]["message"].get("content") or "").strip()

    # Defensive cleanup: even if provider unexpectedly prepends technical text,
    # only the JSON object reaches the user.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    first = raw.find("{")
    last = raw.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise RuntimeError("Vision returned no JSON object")

    return json.loads(raw[first:last + 1])


def glossary_notes_for_text(text_value: str) -> list[str]:
    value = str(text_value or "")
    lower = value.casefold()
    notes = []
    seen = set()

    for key, explanation in FOOD_GLOSSARY.items():
        if key.casefold() in lower and explanation not in seen:
            seen.add(explanation)
            notes.append(explanation)

    return notes[:3]


def render_vision_result(result, mode="object"):
    title = str(result.get("title") or "AI-разбор фото").strip()
    intro = str(result.get("intro") or "").strip()
    conclusion = str(result.get("conclusion") or "").strip()
    uncertain = bool(result.get("uncertain"))

    mode_icon = {
        "menu": "🍜",
        "spicy": "🌶",
        "ingredients": "🥢",
        "symbol": "🏮",
        "tradition": "🏯",
        "text": "🔤",
        "object": "🔎",
        "place": "🏯",
        "monument": "🗿",
        "photo": "📸",
    }.get(mode, "🤖")

    item_icon = {
        "menu": "🍽",
        "spicy": "🌶",
        "ingredients": "🥩",
        "symbol": "🧧",
        "tradition": "🏮",
        "text": "🀄",
        "object": "🔎",
        "place": "📍",
        "monument": "🗿",
        "photo": "📸",
    }.get(mode, "•")

    lines = [f"{mode_icon} <b>{esc(title)}</b>"]
    if intro:
        lines += ["", esc(intro)]

    glossary_source_parts = [intro, conclusion]

    for item in (result.get("items") or [])[:8]:
        ru = str(item.get("name_ru") or "").strip()
        cn = str(item.get("name_cn") or "").strip()
        pinyin = str(item.get("pinyin") or "").strip()
        detail = str(item.get("detail") or "").strip()

        glossary_source_parts.extend([ru, cn, detail])

        lines.append("")
        if ru:
            lines.append(f"{item_icon} <b>{esc(ru)}</b>")
        if cn:
            lines.append(f"🇨🇳 {esc(cn)}")
        if pinyin:
            lines.append(f"🔤 <i>{esc(pinyin)}</i>")
        if detail:
            lines.append(f"💬 {esc(detail)}")

    glossary_notes = glossary_notes_for_text(" ".join(glossary_source_parts))
    if glossary_notes:
        lines += ["", "📚 <b>Что это значит:</b>"]
        for note in glossary_notes:
            lines.append(f"• {esc(note)}")

    if uncertain:
        lines += ["", "🤔 <b>По этому фото не всё можно определить точно.</b>"]

    if conclusion:
        lines += ["", f"👉 {esc(conclusion)}"]

    return "\n".join(lines)


def followup_phrase_keys(mode):
    if mode == "spicy":
        return ["spicy", "what"]
    if mode == "ingredients":
        return ["inside", "meatfish", "what"]
    if mode == "menu":
        return ["spicy", "meatfish", "inside", "recommend"]
    return []


TRAVEL_CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
    },
    "required": ["caption"],
    "additionalProperties": False,
}


async def generate_travel_caption(data):
    quest = data.get("quest") or {}
    city = data.get("city") or {}
    completed = data.get("completed") or []
    photos = data.get("photos") or {}
    interests = data.get("interests") or []

    interest_text = ", ".join(
        INTERESTS.get(key, {}).get("label", key)
        for key in interests
    )

    prompt = f"""
Ты пишешь одну короткую подпись для travel-открытки после городского квеста.
Только по-русски. Без Markdown, без кавычек, без хэштегов.
Не перечисляй статистику: она будет отдельно.
Тон: тёплый, живой, как личная заметка из путешествия.
Не придумывай факты о городе и местах.
Длина: максимум 110 символов.

Город: {city_display_ru(city)}
Название квеста: {quest.get('title') or 'CityQuest'}
Интересы: {interest_text}
Выполнено миссий: {len(completed)}
Фото: {len(photos)}
""".strip()

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "Верни только русский JSON по заданной схеме.",
            },
            {"role": "user", "content": prompt},
        ],
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "travel_card_caption",
                "strict": True,
                "schema": TRAVEL_CAPTION_SCHEMA,
            },
        },
        "temperature": 0.7,
    }

    timeout = aiohttp.ClientTimeout(total=35)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                body = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"Caption API HTTP {response.status}")

                data_json = json.loads(body)
                result = json.loads(
                    data_json["choices"][0]["message"]["content"]
                )
                caption = str(result.get("caption") or "").strip()
                caption = re.sub(r"[#*_`<>]", "", caption)
                if caption:
                    return caption[:140]
    except Exception:
        logger.exception("Travel caption generation failed")

    return "Маленькое путешествие, которое получилось увидеть своими глазами."


def load_card_font(size, bold=False):
    """
    Use DejaVu bundled with matplotlib.
    It supports Cyrillic reliably even when the BotHost image has no system fonts.
    """
    candidates = []

    try:
        import matplotlib
        font_dir = os.path.join(
            os.path.dirname(matplotlib.__file__),
            "mpl-data",
            "fonts",
            "ttf",
        )
        candidates.append(
            os.path.join(
                font_dir,
                "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
            )
        )
    except Exception:
        logger.exception("Could not locate matplotlib bundled font")

    if bold:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ])
    else:
        candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ])

    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass

    raise RuntimeError(
        "No Cyrillic-capable font found for travel card"
    )


def clean_card_text(value):
    """
    Travel card is Russian-first. Remove emoji/CJK/unsupported decorative symbols
    from the raster image; the Telegram messages can still contain them.
    """
    value = str(value or "")
    value = re.sub(r"[\u3400-\u4dbf\u4e00-\u9fff]", "", value)
    value = re.sub(
        r"[\U0001F000-\U0001FAFF\u2600-\u27BF]",
        "",
        value,
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def wrap_by_pixels(draw, text_value, font, max_width):
    words = str(text_value or "").split()
    if not words:
        return []

    lines = []
    current = words[0]

    for word in words[1:]:
        trial = current + " " + word
        box = draw.textbbox((0, 0), trial, font=font)
        if box[2] - box[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def rounded_photo(source, size, radius=28):
    fitted = ImageOps.fit(
        source.convert("RGB"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    mask = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, size[0], size[1]),
        radius=radius,
        fill=255,
    )

    output = Image.new("RGB", size, (246, 241, 231))
    output.paste(fitted, (0, 0), mask)
    return output


def photo_layout_boxes(count):
    x0, y0 = 64, 415
    width, height = 952, 560
    gap = 16

    if count <= 1:
        return [(x0, y0, width, height)]

    if count == 2:
        w = (width - gap) // 2
        return [
            (x0, y0, w, height),
            (x0 + w + gap, y0, w, height),
        ]

    if count == 3:
        left_w = 565
        right_w = width - left_w - gap
        right_h = (height - gap) // 2
        return [
            (x0, y0, left_w, height),
            (x0 + left_w + gap, y0, right_w, right_h),
            (x0 + left_w + gap, y0 + right_h + gap, right_w, right_h),
        ]

    if count == 4:
        w = (width - gap) // 2
        h = (height - gap) // 2
        return [
            (x0, y0, w, h),
            (x0 + w + gap, y0, w, h),
            (x0, y0 + h + gap, w, h),
            (x0 + w + gap, y0 + h + gap, w, h),
        ]

    top_h = 305
    bottom_h = height - top_h - gap
    top_w = (width - gap) // 2
    bottom_w = (width - 2 * gap) // 3
    return [
        (x0, y0, top_w, top_h),
        (x0 + top_w + gap, y0, top_w, top_h),
        (x0, y0 + top_h + gap, bottom_w, bottom_h),
        (x0 + bottom_w + gap, y0 + top_h + gap, bottom_w, bottom_h),
        (x0 + 2 * (bottom_w + gap), y0 + top_h + gap, bottom_w, bottom_h),
    ]


def render_travel_card(data, photo_images, caption):
    width, height = 1080, 1350

    bg = (246, 241, 230)
    paper = (255, 252, 245)
    ink = (39, 42, 38)
    muted = (104, 101, 92)
    red = (165, 61, 49)
    deep_red = (125, 42, 35)
    gold = (191, 148, 62)
    soft_gold = (239, 226, 190)
    line = (221, 213, 195)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    city_font = load_card_font(76, bold=True)
    title_font = load_card_font(38, bold=True)
    kicker_font = load_card_font(19, bold=True)
    stat_value_font = load_card_font(30, bold=True)
    stat_label_font = load_card_font(17, bold=True)
    body_font = load_card_font(25, bold=False)
    route_font = load_card_font(18, bold=False)
    route_bold_font = load_card_font(18, bold=True)
    footer_font = load_card_font(16, bold=False)

    quest = data.get("quest") or {}
    city = data.get("city") or {}
    route = data.get("route") or {}
    completed = data.get("completed") or []
    bonuses = data.get("bonuses") or []

    city_name = clean_card_text(city_display_ru(city)).upper()
    title = clean_card_text(
        quest.get("title") or f"CityQuest {city_display_ru(city)}"
    )
    caption = clean_card_text(caption)

    xp = earned_xp(quest, completed, bonuses) if quest else 0
    distance = fmt_distance(float(route.get("distance_m") or 0))
    duration = clean_card_text(data.get("duration") or "")
    stop_count = len(quest.get("stops", []))
    photo_count = min(len(photo_images), 5)

    # Background decorations: subtle travel/postcard feel.
    draw.rounded_rectangle(
        (40, 35, 1040, 1315),
        radius=36,
        fill=paper,
    )
    draw.rectangle((40, 35, 55, 1315), fill=red)

    # Top identity.
    draw.text((78, 68), "CITYQUEST CHINA", font=kicker_font, fill=gold)
    draw.line((78, 102, 1000, 102), fill=line, width=2)

    # Decorative seal, drawn without unsupported glyphs.
    draw.ellipse((915, 58, 1000, 143), fill=red)
    seal_font = load_card_font(24, bold=True)
    draw.text((934, 84), "CQ", font=seal_font, fill=(255, 247, 235))

    # Large Russian city.
    draw.text((78, 132), city_name, font=city_font, fill=deep_red)

    # Quest title.
    title_lines = wrap_by_pixels(draw, title, title_font, 790)[:2]
    ty = 230
    for line_text in title_lines:
        draw.text((80, ty), line_text, font=title_font, fill=ink)
        ty += 47

    # Stats as proper cards, no emoji/symbol glyph dependency.
    stats = [
        ("МИССИИ", f"{len(completed)}/{stop_count}"),
        ("XP", str(xp)),
        ("МАРШРУТ", distance),
        ("ВРЕМЯ", duration or "—"),
    ]

    card_y = 325
    card_w = 218
    card_h = 72
    gap = 18

    for i, (label, value) in enumerate(stats):
        x = 78 + i * (card_w + gap)
        draw.rounded_rectangle(
            (x, card_y, x + card_w, card_y + card_h),
            radius=18,
            fill=(249, 244, 233),
            outline=soft_gold,
            width=2,
        )
        draw.text(
            (x + 16, card_y + 12),
            clean_card_text(value),
            font=stat_value_font,
            fill=ink,
        )
        draw.text(
            (x + 16, card_y + 46),
            label,
            font=stat_label_font,
            fill=muted,
        )

    # Photo area.
    boxes = photo_layout_boxes(max(photo_count, 1))

    if photo_count:
        for photo, box in zip(photo_images[:5], boxes):
            x, y, w, h = box

            # Shadow.
            draw.rounded_rectangle(
                (x + 7, y + 8, x + w + 7, y + h + 8),
                radius=28,
                fill=(226, 218, 202),
            )

            card = rounded_photo(photo, (w, h), radius=28)
            image.paste(card, (x, y))

        # Small label above the collage/poster.
        label = (
            "ФОТО-ТРОФЕЙ"
            if photo_count == 1
            else f"ФОТО-ТРОФЕИ · {photo_count}"
        )
        draw.rounded_rectangle(
            (78, 424, 78 + 225, 458),
            radius=16,
            fill=red,
        )
        draw.text(
            (93, 432),
            label,
            font=stat_label_font,
            fill=(255, 247, 236),
        )
    else:
        x, y, w, h = boxes[0]
        draw.rounded_rectangle(
            (x, y, x + w, y + h),
            radius=28,
            outline=line,
            width=3,
        )
        empty_font = load_card_font(32, bold=True)
        draw.text(
            (x + 205, y + 245),
            "Здесь будут фото-трофеи",
            font=empty_font,
            fill=muted,
        )

    # Bottom: personal caption + route stops.
    bottom_top = 1002
    draw.line((78, bottom_top, 1000, bottom_top), fill=line, width=2)

    draw.text(
        (78, bottom_top + 25),
        "ВПЕЧАТЛЕНИЯ",
        font=kicker_font,
        fill=gold,
    )

    cap_lines = wrap_by_pixels(
        draw,
        caption or "Моя прогулка по Китаю.",
        body_font,
        900,
    )[:3]

    cy = bottom_top + 60
    for line_text in cap_lines:
        draw.text((78, cy), line_text, font=body_font, fill=ink)
        cy += 34

    # Compact route line(s): user sees actual places included.
    stops = [
        clean_card_text(stop.get("name_ru") or "")
        for stop in quest.get("stops", [])
        if clean_card_text(stop.get("name_ru") or "")
    ][:4]

    if stops:
        route_y = 1162
        draw.text(
            (78, route_y),
            "МАРШРУТ",
            font=kicker_font,
            fill=gold,
        )

        route_text = "  •  ".join(stops)
        route_lines = wrap_by_pixels(
            draw,
            route_text,
            route_font,
            900,
        )[:2]

        ry = route_y + 34
        for line_text in route_lines:
            draw.text((78, ry), line_text, font=route_bold_font, fill=muted)
            ry += 27

    draw.line((78, 1280, 1000, 1280), fill=line, width=2)
    draw.text(
        (78, 1292),
        "CITYQUEST CHINA  ·  PERSONAL TRAVEL CARD",
        font=footer_font,
        fill=muted,
    )

    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=93,
        optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


async def collect_card_photos(bot, photos):
    images = []

    def sort_key(item):
        key, _ = item
        try:
            return int(key)
        except Exception:
            return 999

    for _, file_id in sorted(photos.items(), key=sort_key)[:5]:
        try:
            tg_file = await bot.get_file(file_id)
            buffer = io.BytesIO()
            await bot.download_file(tg_file.file_path, destination=buffer)
            buffer.seek(0)
            images.append(Image.open(buffer).convert("RGB"))
        except Exception:
            logger.exception("Failed to download card photo")

    return images


async def create_and_save_travel_card(bot, user_id, data, caption_override=None):
    photos = data.get("photos") or {}
    if not photos:
        return None, None

    caption = (
        str(caption_override).strip()
        if caption_override is not None
        else await generate_travel_caption(data)
    )
    images = await collect_card_photos(bot, photos)

    if not images:
        return None, None

    card_bytes = render_travel_card(data, images, caption)

    cards_dir = os.path.join(DATA_DIR, "travel_cards")
    os.makedirs(cards_dir, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(cards_dir, f"{int(user_id)}_{stamp}.jpg")

    with open(path, "wb") as f:
        f.write(card_bytes)

    return path, caption


def travel_card_actions_keyboard(custom=False, initial=False, editable=True):
    kb = InlineKeyboardBuilder()

    if editable:
        if custom:
            kb.button(text="✍️ Изменить впечатления", callback_data="custom_impression")
            kb.button(text="↩️ Вернуть готовый вариант", callback_data="restore_ai_impression")
        else:
            kb.button(text="✍️ Написать свои впечатления", callback_data="custom_impression")
            if initial:
                kb.button(text="✨ Оставить готовый текст", callback_data="keep_ai_impression")

    kb.button(text="🎒 Мои приключения", callback_data="my_quests")
    kb.button(text="🧭 Новый квест", callback_data="new_quest")
    kb.button(text="🏠 Главное меню", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()


def route_summary(route, quest, duration):
    walk = route["time_s"] / 60
    missions = sum(int(s["mission"].get("minutes", 12)) for s in quest["stops"])
    total = DURATION_MINUTES[duration]
    pause = max(15, int(total * 0.12))
    reserve = max(0, total - walk - missions - pause)

    if len(quest.get("stops", [])) <= 1:
        return (
            "🗺 <b>Основа квеста проверена</b>\n"
            "📍 Подтверждённая точка: 1\n"
            f"🎯 Основная миссия: ~{fmt_minutes(missions)}\n"
            "🧭 Остальное время — исследовательские задания вокруг этой точки и по ближайшим улицам."
        )

    return (
        "🗺 <b>Маршрут проверен</b>\n"
        f"🚶 Пешком: ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(walk)}\n"
        f"🎯 Миссии: ~{fmt_minutes(missions)}\n"
        f"☕ Паузы: ~{fmt_minutes(pause)}\n"
        f"🕒 Запас: ~{fmt_minutes(reserve)}"
    )


async def ask_city(message, state):
    await state.set_state(QuestForm.waiting_city)
    await message.answer(
        "🧭 <b>Новый CityQuest</b>\n\n"
        "Напиши город Китая. Можно по-русски, по-китайски или по-английски. Сначала я предложу самый вероятный вариант. Если это не он — покажу другие одноимённые города по провинциям.\n\n"
        "Например:\n"
        "• Чэнду · 成都 · Chengdu\n"
        "• Сиань · 西安 · Xi'an\n"
        "• Ханчжоу · 杭州 · Hangzhou"
    )


async def show_interests(message, state):
    data = await state.get_data()
    await state.set_state(QuestForm.choosing_interests)
    await message.answer(
        "✨ <b>Что тебе интересно?</b>\n\n"
        "Выбери 1–3 темы. Фото всё равно будет частью квеста; выбор «Фото» делает задания более композиционными.",
        reply_markup=interests_keyboard(data.get("interests", [])),
    )


async def send_quest(message, state):
    data = await state.get_data()
    quest, route = data["quest"], data["route"]
    duration, city, style = data["duration"], data["city"], data["style"]

    mode_note = quest.get("adaptive_note") or ""
    mode_block = f"\n\n{mode_note}" if mode_note else ""

    await message.answer(
        f"🏮 <b>{esc(quest['title'])}</b>\n\n"
        f"{esc(quest['intro'])}\n\n"
        f"📍 {esc(city_display_ru(city))} · ⏱ {esc(duration)} · {esc(STYLE_LABELS[style])}\n\n"
        f"{route_summary(route, quest, duration)}"
        f"{mode_block}"
    )

    for i in range(len(quest["stops"])):
        await send_stop_card(message, quest, route, i)

        # In compact/explorer mode add lightweight tasks between real POIs.
        field_missions = quest.get("field_missions") or []
        if i < len(field_missions):
            field = field_missions[i]
            await message.answer(
                f"🧭 <b>По пути · {esc(field['title'])}</b>\n\n"
                f"{esc(field['text'])}\n\n"
                "Это дополнительная исследовательская миссия — она не требует отдельной точки на карте."
            )

    await state.update_data(completed=[], bonuses=[], photos={}, photo_versions={})
    await state.set_state(QuestForm.quest_active)
    await persist_active_state(message.from_user.id, state)
    await message.answer(
        checklist_text(quest, [], [], {}),
        reply_markup=checklist_keyboard(quest, []),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    saved = db_load_active(message.from_user.id)

    if saved and saved.get("quest"):
        await state.update_data(**saved)
        await state.set_state(QuestForm.quest_active)

    name = esc(message.from_user.first_name if message.from_user else "путешественник")

    active_text = ""
    if saved and saved.get("quest"):
        active_text = (
            "\n\n💾 <b>У тебя есть незавершённый квест.</b>\n"
            "Он сохранён даже после перезапуска бота.\n\n"
            f"{active_quest_summary(saved)}"
        )

    await message.answer(
        f"🏮 <b>CityQuest China 城市奇遇</b>\n\n"
        f"Привет, {name}!\n\n"
        "Я превращаю прогулку по китайскому городу в AI-квест: реальные места, маршрут, миссии, фото-трофеи и китайские фразы."
        f"{active_text}\n\n"
        "С чего начнём?",
        reply_markup=main_menu(has_active=bool(saved and saved.get("quest"))),
    )


@router.message(Command("newquest"))
async def newquest(message: Message, state: FSMContext):
    await state.clear()
    await ask_city(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    saved = db_load_active(message.from_user.id)

    if saved and saved.get("quest"):
        await message.answer(
            "Действие отменено. 💾 Незавершённый квест остался сохранён.",
            reply_markup=main_menu(has_active=True),
        )
    else:
        await message.answer(
            "Действие отменено.",
            reply_markup=main_menu(),
        )


@router.callback_query(F.data == "new_quest")
async def newquest_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await ask_city(callback.message, state)


def choices_are_one_city(candidates):
    if not candidates:
        return False
    if len(candidates) == 1:
        return True

    first = candidates[0]
    first_name = normalize_city_text(first.get("city"))
    first_state = normalize_city_text(first.get("state"))

    for candidate in candidates[1:]:
        if normalize_city_text(candidate.get("city")) != first_name:
            return False

        state = normalize_city_text(candidate.get("state"))
        # Missing region does not create a second city.
        if first_state and state and state != first_state:
            return False

        if not same_city_nearby(first, candidate):
            return False

    return True


@router.message(QuestForm.waiting_city)
async def city_received(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    status = await message.answer("🗺 Ищу город в Китае…")

    try:
        candidates = await geocode_city_candidates(query)
    except Exception:
        logger.exception("City lookup")
        candidates = []

    if not candidates:
        await status.edit_text(
            "🤔 Не удалось быстро подтвердить город.\n\n"
            "Попробуй название с провинцией, например: Langzhong, Sichuan, "
            "или другое написание: Сиань · 西安 · Xi'an."
        )
        return

    # Keep every valid alternative in state, but do NOT overload the user with them.
    best = candidates[0]
    await state.update_data(
        city_candidates=candidates,
        city=best,
        interests=[],
    )

    province = (
        f"\nПровинция / регион: <b>{esc(region_ru(best.get('state')))}</b>"
        if best.get("state") else ""
    )
    county = (
        f"\nАдминистративный район: <b>{esc(admin_ru(best.get('county')))}</b>"
        if best.get("county") else ""
    )

    extra_hint = ""
    if len(candidates) > 1:
        extra_hint = (
            "\n\nЕсли это не тот город, нажми «Другой город» — "
            "я покажу остальные одноимённые варианты."
        )

    display_ru = city_display_ru(best)
    secondary = city_display_secondary(best)
    secondary_line = f"\n{esc(secondary)}" if secondary else ""

    await status.edit_text(
        f"🇨🇳 <b>Нашёл город!</b>\n\n"
        f"<b>{esc(display_ru)}</b>"
        f"{secondary_line}\n"
        f"{esc(best['formatted'])}"
        f"{province}{county}\n"
        f"📍 {best['lat']:.5f}, {best['lon']:.5f}\n\n"
        "Это тот город?"
        f"{extra_hint}",
        reply_markup=city_confirmation_keyboard(),
    )


@router.callback_query(F.data.startswith("city_select:"))
async def city_select(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    candidates = data.get("city_candidates") or []

    try:
        index = int(callback.data.split(":", 1)[1])
        city = candidates[index]
    except (ValueError, IndexError):
        await callback.answer("Вариант уже недоступен. Введи город ещё раз.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(city=city, interests=[])

    province = (
        f"\nПровинция / регион: <b>{esc(region_ru(city.get('state')))}</b>"
        if city.get("state") else ""
    )
    county = (
        f"\nАдминистративный район: <b>{esc(admin_ru(city.get('county')))}</b>"
        if city.get("county") else ""
    )

    display_ru = city_display_ru(city)
    secondary = city_display_secondary(city)
    secondary_line = f"\n{esc(secondary)}" if secondary else ""

    await callback.message.answer(
        f"🇨🇳 <b>Выбран город:</b>\n\n"
        f"<b>{esc(display_ru)}</b>"
        f"{secondary_line}\n"
        f"{esc(city['formatted'])}"
        f"{province}{county}\n"
        f"📍 {city['lat']:.5f}, {city['lon']:.5f}\n\n"
        "Это тот город?",
        reply_markup=city_confirmation_keyboard(),
    )


@router.callback_query(F.data == "city_retry")
async def city_retry(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    candidates = data.get("city_candidates") or []

    # First show the alternative results we already found for the SAME query.
    if len(candidates) > 1:
        alternatives = candidates[1:]
        indexes = list(range(1, len(candidates)))

        lines = [
            "🔎 <b>Другие варианты с таким названием</b>",
            "",
            "Выбери по провинции / административному району:",
            "",
        ]

        for original_index, city in zip(indexes, alternatives):
            region = region_ru(city.get("state"))
            county_value = admin_ru(city.get("county"))
            county = f" · {county_value}" if county_value else ""
            lines.append(
                f"<b>{original_index+1}.</b> "
                f"{esc(city.get('city') or city.get('name') or 'Город')} "
                f"— {esc(region)}{esc(county)}"
            )

        await callback.message.answer(
            "\n".join(lines),
            reply_markup=city_choices_keyboard(alternatives, indexes=indexes),
        )
        return

    # No alternatives from the same search — ask for a new name.
    await state.set_state(QuestForm.waiting_city)
    await callback.message.answer(
        "✏️ Напиши другой город.\n\n"
        "Можно добавить провинцию, если название неоднозначное: "
        "например <b>Langzhong, Sichuan</b>."
    )


@router.callback_query(F.data == "city_manual")
async def city_manual(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.waiting_city)
    await callback.message.answer(
        "⌨️ Напиши город заново.\n\n"
        "Можно по-русски, по-китайски или по-английски. "
        "Для маленького одноимённого города можно сразу добавить провинцию."
    )


@router.callback_query(F.data == "city_confirm")
async def city_confirm(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "⏱ <b>Сколько времени у тебя есть?</b>",
        reply_markup=duration_keyboard(),
    )


@router.callback_query(F.data.startswith("duration_"))
async def duration_cb(callback: CallbackQuery, state: FSMContext):
    mapping = {
        "duration_2": "2 часа",
        "duration_4": "4 часа",
        "duration_6": "6 часов",
        "duration_day": "весь день",
    }
    await callback.answer()
    await state.update_data(duration=mapping[callback.data], interests=[])
    await show_interests(callback.message, state)


@router.callback_query(F.data.startswith("interest:"))
async def interest_cb(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("interests", []))

    if key in selected:
        selected.remove(key)
    elif len(selected) >= 3:
        await callback.answer("Максимум 3 интереса.", show_alert=True)
        return
    else:
        selected.append(key)

    await state.update_data(interests=selected)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=interests_keyboard(selected))


@router.callback_query(F.data == "interests_continue")
async def interests_continue(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    interests, city, duration = data.get("interests", []), data.get("city"), data.get("duration")

    if not interests:
        await callback.answer("Выбери хотя бы один интерес.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer(
        "🔎 <b>Ищу реальные места…</b>\n\n"
        "Собираю разные типы точек. Сырые названия карты показывать не буду — сначала соберу нормальный маршрут."
    )

    try:
        places = await search_places(city, interests, duration)
    except Exception:
        logger.exception("Places")
        places = []

    if not places:
        await status.edit_text(
            "🤔 На карте пока не удалось найти ни одной подходящей подробно размеченной точки в этом городе.\n\n"
            "Это ограничение данных карты, а не оценка самого города. "
            "Попробуй другой город или вернись к нему позже."
        )
        return

    pool_mode = suggested_mode_from_pool(places)
    await state.update_data(candidates=places, pool_mode=pool_mode)
    await state.set_state(QuestForm.choosing_style)

    await status.edit_text(
        f"📍 <b>Нашёл {len(places)} кандидата</b>\n\n"
        f"{candidate_summary(places)}\n\n"
        f"{pool_mode_message(pool_mode)}\n\n"
        "Сейчас ничего выбирать не нужно. После выбора стиля бот сам попробует самый качественный режим: "
        "сначала полный маршрут, затем компактный, а при очень скудной разметке — исследовательский.\n\n"
        "🎯 <b>Как будем исследовать город?</b>",
        reply_markup=style_keyboard(),
    )


@router.callback_query(F.data.startswith("style:"))
async def style_cb(callback: CallbackQuery, state: FSMContext):
    style = callback.data.split(":", 1)[1]
    data = await state.get_data()
    city, duration = data.get("city"), data.get("duration")
    interests, candidates = data.get("interests", []), data.get("candidates", [])

    await callback.answer()
    await state.set_state(QuestForm.generating)
    status = await callback.message.answer(
        "🧭 <b>Собираю маршрут…</b>\n\n"
        "Проверяю разнообразие и реальные пешие переходы."
    )

    try:
        selected, route, adaptive_mode = await select_route(candidates, interests, duration)
    except Exception:
        logger.exception("Route")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🗺 На карте не удалось подтвердить даже одну пригодную точку для квеста.\n\n"
            "Это похоже на нехватку данных карты для этого города, а не на отсутствие интересных мест. "
            "Можно попробовать другой город или другие интересы.",
            reply_markup=style_keyboard(),
        )
        return

    await status.edit_text(
        f"🔎 <b>{esc(ADAPTIVE_MODE_LABELS.get(adaptive_mode, 'Квест'))}</b>\n\n"
        f"Подтверждённых точек в маршруте: <b>{len(selected)}</b>.\n"
        f"Пешком между ними: ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(route['time_s']/60)}.\n"
        "Уточняю финальные точки и их типы через Place Details…"
    )

    selected = await enrich_final_places(selected)

    await status.edit_text(
        f"🤖 <b>Точки проверены.</b>\n\n"
        f"Пешком ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(route['time_s']/60)}.\n"
        "AI создаёт персональные миссии именно для этих реальных точек. "
        "Названия, категории и координаты он менять не может; сомнительная миссия автоматически заменяется безопасной."
    )

    avoid_missions = db_recent_missions_for_city(
        callback.from_user.id,
        city_display_ru(city),
    )
    ai_meta = await groq_meta(
        city,
        duration,
        interests,
        style,
        selected,
        avoid_missions=avoid_missions,
    )
    quest = build_quest(city, interests, style, selected, ai_meta, adaptive_mode=adaptive_mode)

    await state.update_data(quest=quest, route=route, style=style)
    await status.edit_text("✅ <b>Квест готов!</b>")
    await send_quest(callback.message, state)


@router.callback_query(F.data.startswith("photo_continue:"))
async def photo_continue(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")
    route = data.get("route") or {"distance_m": 0, "time_s": 0, "legs": []}

    if not quest:
        await callback.answer("Активный квест не найден.", show_alert=True)
        return

    await callback.answer()

    if idx + 1 < len(quest["stops"]):
        next_idx = idx + 1
        await callback.message.answer(
            f"➡️ <b>Продолжаем: миссия {next_idx + 1}</b>"
        )
        await send_stop_card(
            callback.message,
            quest,
            route,
            next_idx,
        )
        await callback.message.answer(
            "🧭 <b>Навигация:</b>",
            reply_markup=mission_nav_keyboard(quest, next_idx),
        )
    else:
        await callback.message.answer(
            "🏁 Это последняя точка маршрута.\n"
            "Открой чек-лист и отметь выполненные миссии.",
            reply_markup=checklist_keyboard(
                quest,
                data.get("completed", []),
            ),
        )


@router.callback_query(F.data.startswith("mission_toggle:"))
async def mission_toggle(callback: CallbackQuery, state: FSMContext):
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")

    if not quest:
        await callback.answer("Квест не найден.", show_alert=True)
        return

    idx = int(callback.data.split(":", 1)[1])
    completed = list(data.get("completed", []))

    if idx in completed:
        completed.remove(idx)
        msg = "Отметка снята"
    else:
        completed.append(idx)
        msg = "Миссия выполнена ✅"

    completed = sorted(set(completed))
    await state.update_data(completed=completed)
    await persist_active_state(callback.from_user.id, state)
    await callback.answer(msg)

    refreshed = await state.get_data()
    await callback.message.answer(
        checklist_text(
            quest,
            completed,
            refreshed.get("bonuses", []),
            refreshed.get("photos", {}),
        ),
        reply_markup=checklist_keyboard(quest, completed),
    )


@router.callback_query(F.data.startswith("bonus_toggle:"))
async def bonus_toggle(callback: CallbackQuery, state: FSMContext):
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")
    idx = int(callback.data.split(":", 1)[1])

    if not quest or not quest["stops"][idx].get("bonus"):
        await callback.answer("Бонуса нет.", show_alert=True)
        return

    bonuses = list(data.get("bonuses", []))
    if idx in bonuses:
        bonuses.remove(idx)
        msg = "Бонус снят"
    else:
        bonuses.append(idx)
        msg = "Бонус +10 XP 🎁"

    await state.update_data(bonuses=sorted(set(bonuses)))
    await persist_active_state(callback.from_user.id, state)
    await callback.answer(msg)


@router.callback_query(F.data.startswith("photo_add:"))
async def photo_add(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")

    if not quest or idx >= len(quest["stops"]):
        await callback.answer("Миссия не найдена.", show_alert=True)
        return

    await state.update_data(photo_target=idx)
    await state.set_state(QuestForm.waiting_photo)
    await callback.answer()

    await callback.message.answer(
        f"📷 <b>Фото для {idx+1}. {esc(quest['stops'][idx]['name_ru'])}</b>\n\n"
        "Отправь фотографию следующим сообщением. После загрузки появятся AI-кнопки для этой точки.\n\n"
        "Для отмены: /cancelphoto"
    )



@router.callback_query(F.data.startswith("photo_replace:"))
async def photo_replace(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")

    if not quest or idx < 0 or idx >= len(quest["stops"]):
        await callback.answer("Миссия не найдена.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(photo_target=idx)
    await state.set_state(QuestForm.waiting_photo)

    await callback.message.answer(
        f"🔄 <b>Другое фото для {idx+1}. {esc(quest['stops'][idx]['name_ru'])}</b>\n\n"
        "Пришли новый снимок. Он заменит предыдущее фото этой миссии, "
        "и следующие AI-вопросы будут относиться уже к новому снимку."
    )


@router.message(Command("cancelphoto"))
async def cancelphoto(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(photo_target=None)
    if data.get("quest"):
        await state.set_state(QuestForm.quest_active)
    else:
        await state.clear()
    await message.answer("Загрузка фото отменена.")


@router.message(QuestForm.waiting_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    quest, idx = data.get("quest"), data.get("photo_target")

    if not quest or idx is None:
        await message.answer("Не удалось определить миссию.")
        return

    suitable = [
        p for p in message.photo
        if not p.file_size or p.file_size <= 2_500_000
    ]
    chosen = suitable[-1] if suitable else message.photo[0]

    photos = dict(data.get("photos", {}))
    versions = dict(data.get("photo_versions", {}))

    key = str(idx)
    previous_exists = bool(photos.get(key))

    photos[key] = chosen.file_id
    versions[key] = int(versions.get(key, 0)) + 1
    version = versions[key]

    await state.update_data(
        photos=photos,
        photo_versions=versions,
        photo_target=None,
    )
    await state.set_state(QuestForm.quest_active)
    await persist_active_state(message.from_user.id, state)

    stop = quest["stops"][idx]
    verb = "заменено" if previous_exists else "сохранено"

    await message.answer(
        f"📷 <b>Фото {verb}: {esc(stop['name_ru'])}</b>\n\n"
        "Для каждой миссии хранится <b>один фото-трофей</b>. "
        "Если нажать «🔄 Другое фото», этот снимок будет заменён.\n\n"
        "AI-разбор необязателен: можно сразу нажать «➡️ Продолжить квест».",
        reply_markup=photo_actions_keyboard(stop, idx, version=version),
    )


@router.message(QuestForm.waiting_photo)
async def not_photo(message: Message):
    await message.answer("Пришли фотографию или используй /cancelphoto.")


@router.callback_query(F.data.startswith("vision:"))
async def vision_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    mode = parts[1]
    idx = int(parts[2])
    expected_version = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else None

    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")
    photos = data.get("photos", {})
    versions = data.get("photo_versions", {})

    file_id = photos.get(str(idx))
    current_version = int(versions.get(str(idx), 0))

    if not quest or not file_id:
        await callback.answer("Сначала добавь фото.", show_alert=True)
        return

    # New v7.1 buttons are tied to the exact photo revision.
    if expected_version is not None and expected_version != current_version:
        await callback.answer(
            "Это кнопка от предыдущего фото. Используй кнопки под новым снимком.",
            show_alert=True,
        )
        await callback.message.answer(
            "🔄 <b>Фото этой миссии уже заменено.</b>\n"
            "Ниже — действия для текущего снимка.",
            reply_markup=photo_actions_keyboard(
                quest["stops"][idx],
                idx,
                version=current_version,
            ),
        )
        return

    request_file_id = file_id
    request_version = current_version

    await callback.answer()
    status = await callback.message.answer("🤖 <b>Смотрю фотографию…</b>")

    try:
        image_bytes = await download_photo_bytes(callback.bot, request_file_id)
        result = await analyze_photo_with_groq(
            image_bytes,
            mode,
            quest["stops"][idx],
        )
    except Exception:
        logger.exception("Vision")

        latest = await restore_active_state(callback.from_user.id, state)
        latest_photos = latest.get("photos", {})
        latest_versions = latest.get("photo_versions", {})

        if (
            latest_photos.get(str(idx)) != request_file_id
            or int(latest_versions.get(str(idx), 0)) != request_version
        ):
            await status.edit_text(
                "🔄 Пока я анализировал снимок, ты уже заменил фото. "
                "Старый результат не показываю."
            )
            return

        await status.edit_text(
            "🤖 Сейчас не получилось разобрать фото.\n\n"
            "Можно попробовать ещё раз или просто продолжить квест."
        )

        keys = followup_phrase_keys(mode)
        if keys:
            await callback.message.answer(
                "💬 <b>Простые вопросы:</b>",
                reply_markup=phrase_show_keyboard(keys, idx),
            )

        await callback.message.answer(
            "🧭 <b>Продолжить квест:</b>",
            reply_markup=mission_nav_keyboard(quest, idx),
        )
        return

    # Stale-response protection: do not show analysis for a photo that has
    # already been replaced while the AI request was running.
    latest = await restore_active_state(callback.from_user.id, state)
    latest_photos = latest.get("photos", {})
    latest_versions = latest.get("photo_versions", {})

    if (
        latest_photos.get(str(idx)) != request_file_id
        or int(latest_versions.get(str(idx), 0)) != request_version
    ):
        await status.edit_text(
            "🔄 Пока я анализировал снимок, ты уже заменил фото. "
            "Старый AI-разбор скрыт — используй кнопки под новым снимком."
        )
        return

    await status.edit_text(render_vision_result(result, mode))

    keys = followup_phrase_keys(mode)
    if keys:
        await callback.message.answer(
            "💬 <b>Если по фото всё равно неясно — можно спросить сотрудника:</b>",
            reply_markup=phrase_show_keyboard(keys, idx),
        )

    await callback.message.answer(
        "🧭 <b>Продолжить квест:</b>",
        reply_markup=mission_nav_keyboard(quest, idx),
    )


@router.callback_query(F.data.startswith("phrase:"))
async def phrase_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    key = parts[1]
    idx = int(parts[2]) if len(parts) > 2 else 0

    phrase = PHRASES.get(key)
    if not phrase:
        await callback.answer()
        return

    await callback.answer()
    await callback.message.answer(
        "📱 <b>ПОКАЖИ ЭКРАН СОТРУДНИКУ ИЛИ СПРОСИ САМ</b>\n\n"
        f"<b>{esc(phrase['hanzi'])}</b>\n\n"
        f"🔤 <i>{esc(phrase['pinyin'])}</i>\n"
        f"🗣 Примерно: <b>{esc(phrase['ru'])}</b>\n"
        f"💬 {esc(phrase['translation'])}"
    )

    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")
    if quest:
        await callback.message.answer(
            "🧭 <b>Продолжить квест:</b>",
            reply_markup=mission_nav_keyboard(quest, idx),
        )
    elif data.get("free_photo_id"):
        await callback.message.answer(
            "Что дальше?",
            reply_markup=free_photo_nav_keyboard(),
        )


@router.callback_query(F.data.startswith("quest_stop:"))
async def quest_stop_callback(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await restore_active_state(callback.from_user.id, state)
    quest, route = data.get("quest"), data.get("route")

    if not quest or not route:
        await callback.answer("Активный квест не найден.", show_alert=True)
        return

    await callback.answer()
    await send_stop_card(callback.message, quest, route, idx)
    await callback.message.answer(
        "🧭 <b>Навигация:</b>",
        reply_markup=mission_nav_keyboard(quest, idx),
    )


@router.callback_query(F.data == "show_checklist")
async def show_checklist_callback(callback: CallbackQuery, state: FSMContext):
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")

    if not quest:
        await callback.answer("Активный квест не найден.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(
        checklist_text(
            quest,
            data.get("completed", []),
            data.get("bonuses", []),
            data.get("photos", {}),
        ),
        reply_markup=checklist_keyboard(quest, data.get("completed", [])),
    )



@router.callback_query(F.data == "free_photo")
async def free_photo_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.waiting_free_photo)
    await callback.message.answer(
        "📷 <b>Спроси по фотографии</b>\n\n"
        "Пришли любой снимок из Китая: достопримечательность, памятник, меню, блюдо, вывеску или символ.\n"
        "После загрузки я предложу варианты вопросов."
    )


@router.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    active = db_load_active(callback.from_user.id)
    await callback.message.answer(
        "🏠 <b>Главное меню</b>",
        reply_markup=main_menu(has_active=bool(active and active.get("quest"))),
    )


@router.message(QuestForm.waiting_free_photo, F.photo)
async def receive_free_photo(message: Message, state: FSMContext):
    suitable = [p for p in message.photo if not p.file_size or p.file_size <= 2_500_000]
    chosen = suitable[-1] if suitable else message.photo[0]

    await state.update_data(free_photo_id=chosen.file_id)
    await state.set_state(QuestForm.free_photo_ready)

    await message.answer(
        "📷 <b>Фото получено.</b>\n\nЧто хочешь узнать?",
        reply_markup=free_photo_actions_keyboard(),
    )


@router.message(QuestForm.waiting_free_photo)
async def receive_free_nonphoto(message: Message):
    await message.answer("Пришли фотографию.")


@router.callback_query(F.data.startswith("visionfree:"))
async def vision_free_callback(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":", 1)[1]
    data = await state.get_data()
    file_id = data.get("free_photo_id")

    if not file_id:
        await callback.answer("Сначала пришли фотографию.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer("🤖 <b>Смотрю фотографию…</b>")

    generic_stop = {
        "name_ru": "Фото пользователя",
        "place": {
            "name": "",
            "category_label": "",
        },
    }

    try:
        image_bytes = await download_photo_bytes(callback.bot, file_id)
        result = await analyze_photo_with_groq(image_bytes, mode, generic_stop)
    except Exception:
        logger.exception("Free vision")
        await status.edit_text(
            "🤖 Сейчас не получилось разобрать фото. Попробуй ещё раз или пришли другой снимок."
        )
        await callback.message.answer(
            "Что дальше?",
            reply_markup=free_photo_nav_keyboard(),
        )
        return

    await status.edit_text(render_vision_result(result, mode))

    keys = followup_phrase_keys(mode)
    if keys:
        await callback.message.answer(
            "💬 <b>Если хочешь уточнить у человека на месте:</b>",
            reply_markup=phrase_show_keyboard(keys, 0),
        )

    await callback.message.answer(
        "Что дальше?",
        reply_markup=free_photo_nav_keyboard(),
    )


@router.callback_query(F.data == "quest_finish")
async def quest_finish(callback: CallbackQuery, state: FSMContext):
    data = await restore_active_state(callback.from_user.id, state)
    quest = data.get("quest")
    completed = set(data.get("completed", []))

    if not quest:
        await callback.answer("Квест не найден.", show_alert=True)
        return

    if len(completed) != len(quest["stops"]):
        await callback.answer("Сначала отметь все миссии.", show_alert=True)
        return

    bonuses = data.get("bonuses", [])
    photos = data.get("photos", {})

    await callback.answer()

    card_status = None
    card_path = None
    travel_caption = None

    if photos:
        card_status = await callback.message.answer(
            "🎨 <b>Собираю твою travel-открытку…</b>\n"
            "Если фото несколько — получится коллаж; если одно — постер с фото-трофеем."
        )

        try:
            card_path, travel_caption = await create_and_save_travel_card(
                callback.bot,
                callback.from_user.id,
                data,
            )
        except Exception:
            logger.exception("Travel card generation failed")

        if card_path:
            await state.update_data(
                travel_card_path=card_path,
                travel_caption=travel_caption,
                travel_ai_caption=travel_caption,
            )
            data = await state.get_data()

    db_archive_completed(callback.from_user.id, data)
    await state.set_state(QuestForm.quest_finished)

    await callback.message.answer(
        "🏆 <b>CityQuest завершён!</b>\n\n"
        f"✅ Миссии: <b>{len(completed)}/{len(quest['stops'])}</b>\n"
        f"⭐ XP: <b>{earned_xp(quest, completed, bonuses)}/{total_xp(quest)}</b>\n"
        f"📷 Фото-трофеи: <b>{len(photos)}</b>\n\n"
        f"🎁 <b>Финальный штрих:</b> {esc(quest['final_challenge'])}"
    )

    if card_path and os.path.exists(card_path):
        if card_status:
            await card_status.edit_text("✅ <b>Travel-открытка готова!</b>")

        with open(card_path, "rb") as f:
            card_bytes = f.read()

        await callback.message.answer_photo(
            BufferedInputFile(
                card_bytes,
                filename="cityquest_travel_card.jpg",
            ),
            caption=(
                "🖼 <b>Твоя travel-открытка</b>\n\n"
                "Ею можно поделиться обычной кнопкой Telegram «Переслать»."
            ),
            reply_markup=travel_card_actions_keyboard(initial=True),
        )
    elif photos:
        if card_status:
            await card_status.edit_text(
                "🤔 Квест сохранён, но открытку сейчас собрать не получилось. "
                "Попробуем ещё раз из «Моих приключений» позже."
            )
    else:
        await callback.message.answer(
            "📷 В этом квесте не было фото-трофеев, поэтому фотоколлаж не создавался.",
            reply_markup=travel_card_actions_keyboard(editable=False),
        )


@router.callback_query(F.data == "resume_quest")
async def resume_quest(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    saved = db_load_active(callback.from_user.id)

    if not saved or not saved.get("quest"):
        await callback.message.answer(
            "💾 Сохранённого активного квеста сейчас нет.",
            reply_markup=main_menu(),
        )
        return

    await state.clear()
    await state.update_data(**saved)
    await state.set_state(QuestForm.quest_active)

    quest = saved["quest"]
    route = saved.get("route") or {"distance_m": 0, "time_s": 0, "legs": []}
    completed = saved.get("completed", [])
    bonuses = saved.get("bonuses", [])
    photos = saved.get("photos", {})

    await callback.message.answer(
        "▶️ <b>Продолжаем квест</b>\n\n"
        f"{active_quest_summary(saved)}"
    )

    # Put the next unfinished mission at the bottom of the chat.
    next_index = None
    completed_set = set(completed)
    for i in range(len(quest.get("stops", []))):
        if i not in completed_set:
            next_index = i
            break

    if next_index is not None:
        await send_stop_card(
            callback.message,
            quest,
            route,
            next_index,
        )
        await callback.message.answer(
            "🧭 <b>Навигация:</b>",
            reply_markup=mission_nav_keyboard(quest, next_index),
        )

    await callback.message.answer(
        checklist_text(quest, completed, bonuses, photos),
        reply_markup=checklist_keyboard(quest, completed),
    )


@router.callback_query(F.data == "my_quests")
async def my_quests(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    active = db_load_active(callback.from_user.id)
    history = db_completed_list(callback.from_user.id, limit=5)

    lines = ["🎒 <b>Мои приключения</b>"]

    if active and active.get("quest"):
        lines += [
            "",
            "▶️ <b>Активный квест</b>",
            active_quest_summary(active),
        ]
    else:
        lines += ["", "Сейчас нет незавершённого квеста."]

    if history:
        lines += ["", "🏆 <b>Завершённые квесты</b>"]
        for i, item in enumerate(history, 1):
            city = item.get("city") or "Китай"
            title = item.get("title") or "CityQuest"
            lines += [
                "",
                f"<b>{i}. {esc(title)}</b>",
                f"📍 {esc(city)} · ⭐ {int(item.get('xp') or 0)} XP · 📷 {int(item.get('photos') or 0)}",
            ]
    else:
        lines += ["", "Завершённых квестов пока нет."]

    if not PERSISTENCE_OK:
        lines += [
            "",
            "⚠️ Постоянное хранилище сейчас недоступно; данные будут жить только до перезапуска.",
        ]

    latest_card = db_latest_card(callback.from_user.id)

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=adventures_keyboard(
            has_active=bool(active and active.get("quest")),
            has_card=bool(latest_card),
        ),
    )


@router.callback_query(F.data == "custom_impression")
async def custom_impression_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    record_id, payload = db_latest_completed_payload(callback.from_user.id)
    if not payload or not payload.get("travel_card_path"):
        await callback.message.answer(
            "🤔 Не нашла travel-открытку, для которой можно изменить впечатления."
        )
        return

    await state.update_data(custom_impression_record_id=record_id)
    await state.set_state(QuestForm.waiting_custom_impression)

    current = str(payload.get("travel_caption") or "").strip()
    current_text = (
        f"\n\nСейчас на открытке:\n<i>{esc(current)}</i>"
        if current else ""
    )

    await callback.message.answer(
        "✍️ <b>Напиши свои впечатления</b>\n\n"
        "Пришли 1–3 коротких предложения одним сообщением. "
        "Я заменю готовую подпись твоим текстом и пересоберу открытку."
        f"{current_text}\n\n"
        "Для отмены: /cancelimpression"
    )


@router.message(Command("cancelimpression"))
async def cancel_custom_impression(message: Message, state: FSMContext):
    await state.set_state(QuestForm.quest_finished)
    await message.answer("Изменение впечатлений отменено.")


@router.message(QuestForm.waiting_custom_impression)
async def custom_impression_received(message: Message, state: FSMContext):
    custom_text = (message.text or "").strip()

    if len(custom_text) < 5:
        await message.answer(
            "Напиши чуть подробнее — хотя бы одно короткое предложение."
        )
        return

    if len(custom_text) > 420:
        await message.answer(
            "Для открытки текст слишком длинный. Сократи примерно до 1–3 предложений."
        )
        return

    state_data = await state.get_data()
    wanted_record_id = state_data.get("custom_impression_record_id")
    record_id, payload = db_latest_completed_payload(message.from_user.id)

    if not payload or (wanted_record_id and record_id != wanted_record_id):
        await message.answer("🤔 Не удалось найти нужную travel-открытку.")
        await state.set_state(QuestForm.quest_finished)
        return

    original_ai_caption = str(
        payload.get("travel_ai_caption")
        or payload.get("travel_caption")
        or ""
    ).strip()

    status = await message.answer(
        "🎨 <b>Пересобираю открытку с твоими впечатлениями…</b>"
    )

    try:
        card_path, _ = await create_and_save_travel_card(
            message.bot,
            message.from_user.id,
            payload,
            caption_override=custom_text,
        )
    except Exception:
        logger.exception("Custom travel card regeneration failed")
        card_path = None

    if not card_path:
        await status.edit_text(
            "🤔 Не получилось пересобрать открытку. Попробуй ещё раз позже."
        )
        await state.set_state(QuestForm.quest_finished)
        return

    payload["travel_ai_caption"] = original_ai_caption
    payload["travel_caption"] = custom_text
    payload["travel_card_path"] = card_path
    db_update_completed_payload(record_id, payload)

    await state.set_state(QuestForm.quest_finished)

    with open(card_path, "rb") as f:
        card_bytes = f.read()

    await status.edit_text(
        "✅ <b>Готово — теперь на открытке твои впечатления.</b>"
    )

    await message.answer_photo(
        BufferedInputFile(
            card_bytes,
            filename="cityquest_travel_card.jpg",
        ),
        caption="✍️ <b>Открытка с твоими впечатлениями</b>",
        reply_markup=travel_card_actions_keyboard(custom=True),
    )


@router.callback_query(F.data == "restore_ai_impression")
async def restore_ai_impression(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    record_id, payload = db_latest_completed_payload(callback.from_user.id)
    if not payload:
        await callback.message.answer("🤔 Завершённый квест не найден.")
        return

    ai_caption = str(
        payload.get("travel_ai_caption")
        or payload.get("travel_caption")
        or ""
    ).strip()

    if not ai_caption:
        await callback.message.answer(
            "🤔 Исходный готовый текст для этой открытки не сохранился."
        )
        return

    status = await callback.message.answer(
        "↩️ <b>Возвращаю готовый вариант впечатлений…</b>"
    )

    try:
        card_path, _ = await create_and_save_travel_card(
            callback.bot,
            callback.from_user.id,
            payload,
            caption_override=ai_caption,
        )
    except Exception:
        logger.exception("Restore AI travel card failed")
        card_path = None

    if not card_path:
        await status.edit_text("🤔 Не получилось пересобрать открытку.")
        return

    payload["travel_caption"] = ai_caption
    payload["travel_ai_caption"] = ai_caption
    payload["travel_card_path"] = card_path
    db_update_completed_payload(record_id, payload)

    with open(card_path, "rb") as f:
        card_bytes = f.read()

    await status.edit_text("✅ <b>Готовый вариант восстановлен.</b>")

    await callback.message.answer_photo(
        BufferedInputFile(
            card_bytes,
            filename="cityquest_travel_card.jpg",
        ),
        caption="✨ <b>Открытка с готовыми впечатлениями</b>",
        reply_markup=travel_card_actions_keyboard(custom=False),
    )


@router.callback_query(F.data == "keep_ai_impression")
async def keep_ai_impression(callback: CallbackQuery):
    await callback.answer("Оставляем готовый текст ✨")
    await callback.message.answer(
        "✨ Готовый вариант впечатлений оставлен. "
        "Позже его можно изменить через «Мои приключения»."
    )


@router.callback_query(F.data == "latest_card")
async def latest_card(callback: CallbackQuery):
    await callback.answer()

    record_id, payload = db_latest_completed_payload(callback.from_user.id)
    if not payload:
        await callback.message.answer(
            "🖼 Сохранённая travel-открытка не найдена."
        )
        return

    path = payload.get("travel_card_path")
    if not path or not os.path.exists(path):
        await callback.message.answer(
            "🖼 Сохранённая travel-открытка не найдена."
        )
        return

    current_caption = str(payload.get("travel_caption") or "").strip()
    ai_caption = str(
        payload.get("travel_ai_caption")
        or current_caption
    ).strip()
    is_custom = bool(
        current_caption
        and ai_caption
        and current_caption != ai_caption
    )

    try:
        with open(path, "rb") as f:
            card_bytes = f.read()

        await callback.message.answer_photo(
            BufferedInputFile(
                card_bytes,
                filename="cityquest_travel_card.jpg",
            ),
            caption=(
                "🖼 <b>Последняя travel-открытка</b>\n\n"
                "Текст блока «Впечатления» можно изменить."
            ),
            reply_markup=travel_card_actions_keyboard(custom=is_custom),
        )
    except Exception:
        logger.exception("Failed to send latest travel card")
        await callback.message.answer(
            "🤔 Не получилось открыть сохранённую открытку."
        )


@router.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>Как работает CityQuest China</b>\n\n"
        "• Город можно написать по-русски, по-китайски или по-английски.\n"
        "• Geoapify находит реальные места и проверяет пеший маршрут.\n"
        "• AI персонализирует миссии для конкретных реальных точек, интересов и стиля прогулки.\n"
        "• Названия и координаты мест берутся только из Geoapify; если AI-миссия не проходит проверку, включается безопасный шаблон.\n"
        "• Vision AI разбирает фото, меню, надписи, символы, достопримечательности и памятники.\n"
        "• Если AI не уверен — бот даёт простую китайскую фразу, которую можно показать человеку.\n"
        "• Активный квест, прогресс, XP и фото сохраняются между перезапусками BotHost.\n"
        "• После завершения квеста бот собирает travel-открытку из фото-трофеев.\n"
        "• Блок «Впечатления» можно заменить своим текстом и вернуть готовый вариант."
    )


async def main():
    init_persistence()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Starting CityQuest China v8.2 Clear Human Instructions")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
