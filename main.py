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
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from pypinyin import Style, lazy_pinyin

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEOAPIFY_API_KEY = os.getenv("GEOAPIFY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not GEOAPIFY_API_KEY:
    raise RuntimeError("GEOAPIFY_API_KEY is not set")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cityquest")
router = Router()


class QuestForm(StatesGroup):
    waiting_city = State()
    choosing_interests = State()
    choosing_style = State()
    generating = State()
    quest_active = State()
    waiting_photo = State()
    waiting_free_photo = State()
    free_photo_ready = State()
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
    "макао": "Macao",
    "гонконг": "Hong Kong",
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
        for base in {original, stripped}:
            syllables = [p for p in lazy_pinyin(base, style=Style.NORMAL, errors="ignore") if p]
            if syllables:
                variants.extend(["".join(syllables), " ".join(syllables), "'".join(syllables)])

    out, seen = [], set()
    for value in variants:
        for query in (value, f"{value}, China"):
            key = query.casefold().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(query.strip())
    return out


def main_menu():
    kb = InlineKeyboardBuilder()
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


async def geocode_city(city_query):
    url = "https://api.geoapify.com/v1/geocode/search"
    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for query in city_query_variants(city_query):
            params = {
                "text": query,
                "filter": "countrycode:cn",
                "type": "city",
                "limit": 10,
                "format": "json",
                "lang": "en",
                "apiKey": GEOAPIFY_API_KEY,
            }
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    continue
                results = (await response.json()).get("results", [])

            valid = [
                r for r in results
                if (r.get("country_code") or "").lower() == "cn"
                and (r.get("result_type") or "").lower() == "city"
            ]
            if not valid:
                continue

            def rank(item):
                rr = item.get("rank") or {}
                return (
                    float(rr.get("confidence_city_level") or 0),
                    float(rr.get("confidence") or 0),
                    float(rr.get("popularity") or 0),
                )

            item = max(valid, key=rank)
            return {
                "place_id": item.get("place_id"),
                "formatted": item.get("formatted") or city_query,
                "city": item.get("city") or city_query,
                "state": item.get("state"),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "input_name": city_query,
            }
    return None


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


async def search_places(city, interests, duration):
    sources = [(key, INTERESTS[key]["categories"]) for key in interests]
    if duration != "2 часа" and "tea" not in interests and "food" not in interests:
        sources.append(("rest", REST_CATEGORIES))

    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(*[
            fetch_places_source(session, city, key, cats)
            for key, cats in sources
        ])

    merged = {}
    per_source = {key: [] for key, _ in sources}

    for (source_key, _), places in zip(sources, results):
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
    for pos in range(14):
        for source_key, _ in sources:
            arr = per_source[source_key]
            if pos < len(arr) and arr[pos] not in seen:
                seen.add(arr[pos])
                ordered.append(arr[pos])
            if len(ordered) >= 24:
                break
        if len(ordered) >= 24:
            break

    for key in merged:
        if key not in seen and len(ordered) < 24:
            ordered.append(key)
    return [merged[k] for k in ordered]


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
    return {
        "2 часа": [3],
        "4 часа": [5, 4],
        "6 часов": [6, 5],
        "весь день": [7, 6],
    }.get(duration, [4])


def route_fits(route, duration, count):
    total = DURATION_MINUTES[duration]
    walk = route["time_s"] / 60
    mission_est = 14 * count
    pause = max(15, total * 0.12)

    if walk > total * 0.45:
        return False
    if walk + mission_est + pause > total:
        return False
    if any(leg["time_s"] / 60 > MAX_LEG_MINUTES[duration] for leg in route.get("legs", [])):
        return False
    return True


async def select_route(places, interests, duration):
    pool = places[:18]

    for wanted in stop_counts(duration):
        if len(pool) < wanted:
            continue

        scored = []
        for idxs in itertools.combinations(range(len(pool)), wanted):
            combo = [pool[i] for i in idxs]
            if not combo_ok(combo, interests):
                continue

            ordered, approx = best_order(combo)
            if not route_order_ok(ordered):
                continue

            diversity = len(set(place_group(p) for p in combo))
            scored.append((approx - diversity * 250, ordered))

        scored.sort(key=lambda x: x[0])

        for _, ordered in scored[:10]:
            try:
                route = await walking_route(ordered)
            except Exception:
                continue

            if route_fits(route, duration, len(ordered)):
                return ordered, route

    raise RuntimeError("No diversified realistic route")


AI_META_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "final_challenge": {"type": "string"},
    },
    "required": ["title", "intro", "final_challenge"],
    "additionalProperties": False,
}


async def groq_meta(city, duration, interests, style, places):
    place_lines = "\n".join(
        f"{i}. {safe_russian_name(p)} | {p['category_label']}"
        for i, p in enumerate(places, 1)
    )
    interest_text = ", ".join(INTERESTS[k]["label"] for k in interests)

    prompt = f"""
Ты редактор русскоязычного CityQuest China.
Реальные места, их типы, названия, причины выбора и миссии уже определены программой.
ТЫ НЕ ИМЕЕШЬ ПРАВА переименовывать точки, переводить их названия, менять их тип или придумывать факты.

Город: {city.get('input_name')}
Время: {duration}
Интересы: {interest_text}
Стиль: {STYLE_LABELS[style]}

Верни только:
- title: атмосферное название квеста на русском;
- intro: 1–2 предложения на русском;
- final_challenge: короткий финальный фото/рефлексивный челлендж.

Финальные точки для контекста:
{place_lines}
""".strip()

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Только русский JSON по схеме. Не переименовывай реальные места."},
            {"role": "user", "content": prompt},
        ],
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "cityquest_meta",
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
                        return json.loads(data["choices"][0]["message"]["content"])
                    logger.error("Groq meta HTTP %s: %s", response.status, body[:500])
        except Exception:
            logger.exception("Groq meta")
        if attempt == 0:
            await asyncio.sleep(2)
    return {}


def phrase_text(phrase):
    if not phrase:
        return ""
    return (
        f"\n\n🇨🇳 <b>{esc(phrase['hanzi'])}</b>\n"
        f"🔤 <i>{esc(phrase['pinyin'])}</i>\n"
        f"🗣 <b>Примерно:</b> {esc(phrase['ru'])}\n"
        f"💬 {esc(phrase['translation'])}"
    )


def reason_for_place(place, interests):
    """Reason is derived from verified place group; AI cannot contradict it."""
    group = place_group(place)
    matches = set(place.get("interest_matches") or [])
    chosen = set(interests)

    primary = {
        "restaurant": "Познакомиться с местной кухней",
        "cafe": "Сделать гастрономическую паузу и посмотреть местное меню",
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
                "Проследи глазами его линию от одного края до другого и выбери точку, где форма выглядит интереснее всего."
            ),
            "tip": "Не нужно знать архитектурный стиль — задача в форме, ритме и силуэте.",
            "photo": "Сними силуэт так, чтобы линия крыши или ворот хорошо читалась.",
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
            "text": "Найди место, где рядом видны природа и что-то созданное человеком. Выбери самый красивый контраст.",
            "tip": "Подойдут дерево + здание, вода + мост, листья + фонарь, тень + дорожка.",
            "photo": "Сделай кадр из двух миров: природа + город.",
            "xp": 20,
            "minutes": 12,
        },
        {
            "type": "color",
            "title": "Три цвета",
            "text": "Найди три цвета, которые чаще всего встречаются вокруг, и выбери один как «цвет этого места».",
            "tip": "Учитывай растения, здания, вывески, одежду и дорожки.",
            "photo": "Сделай кадр, где выбранный цвет повторяется минимум два раза.",
            "xp": 20,
            "minutes": 12,
        },
        {
            "type": "frame",
            "title": "Рамка внутри кадра",
            "text": "Найди естественную рамку: ветви, ворота, арку, проём или две детали по краям.",
            "tip": "Если готовой рамки нет — создай её из двух объектов по краям кадра.",
            "photo": "Сделай фотографию через найденную «рамку».",
            "xp": 30 if photo_interest else 20,
            "minutes": 12,
        },
    ]


def choose_unused_variant(variants, used_titles):
    for variant in variants:
        if variant["title"] not in used_titles:
            return dict(variant)
    # If every template of this category was already used, rotate but alter title visibly.
    fallback = dict(variants[len(used_titles) % len(variants)])
    fallback["title"] = f"{fallback['title']} · второй раунд"
    return fallback


def mission_for_place(place, interests, index, used_titles):
    group = place_group(place)
    photo_interest = "photo" in interests

    if group in {"heritage", "temple"}:
        mission = choose_unused_variant(heritage_mission_variants(place), used_titles)

    elif group == "park":
        mission = choose_unused_variant(park_mission_variants(photo_interest), used_titles)

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
                "text": "Найди в меню две незнакомые позиции и выбери одну, которую попробовал бы первой.",
                "tip": "Загрузи меню: AI попробует прочитать подпись и примерно определить блюдо. Если информации мало — предложит вопрос сотруднику.",
                "photo": "Сохрани фото меню или названия выбранного блюда.",
                "xp": 20,
                "minutes": 12,
                "phrase": PHRASES["recommend"],
            },
            {
                "type": "food",
                "title": "Острое или нет?",
                "text": "Выбери одно незнакомое блюдо и попробуй понять по названию или меню, острое ли оно.",
                "tip": "По фото бот может проверить слова 辣, 麻辣, 香辣. Если неясно — покажет вопрос 这个辣不辣？",
                "photo": "Сфотографируй название или строку меню.",
                "xp": 20,
                "minutes": 10,
                "phrase": PHRASES["spicy"],
            },
        ]
        mission = choose_unused_variant(variants, used_titles)

    elif group == "market":
        variants = [
            {
                "type": "market",
                "title": "Что здесь самое необычное?",
                "text": "Найди три вещи, блюда, упаковки или вывески, которых обычно не видишь дома. Выбери одну как трофей улицы.",
                "tip": "Не фотографируй людей крупным планом без разрешения.",
                "photo": "Сфотографируй выбранный трофей или его название.",
                "xp": 20,
                "minutes": 12,
                "phrase": PHRASES["what"],
            },
            {
                "type": "market",
                "title": "Одна непонятная вывеска",
                "text": "Найди короткую надпись или название товара, которое тебе совершенно непонятно.",
                "tip": "Загрузи фото — AI попробует прочитать китайский текст и объяснить его.",
                "photo": "Сними надпись достаточно близко и ровно.",
                "xp": 20,
                "minutes": 10,
            },
        ]
        mission = choose_unused_variant(variants, used_titles)

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
        mission = choose_unused_variant(variants, used_titles)

    elif group in {"art", "viewpoint"}:
        variants = [
            {
                "type": "art",
                "title": "Два ракурса",
                "text": "Посмотри на место с двух разных точек и реши, какой ракурс делает его интереснее.",
                "tip": "Сравни линии, масштаб, свет и передний план.",
                "photo": "Сделай лучший из двух кадров.",
                "xp": 30 if photo_interest else 20,
                "minutes": 12,
            },
            {
                "type": "art",
                "title": "Главная линия",
                "text": "Найди одну линию или форму, которая сильнее всего ведёт взгляд по этому месту.",
                "tip": "Это может быть край стены, лестница, дорожка, колонна или силуэт.",
                "photo": "Построй кадр вокруг этой линии.",
                "xp": 20,
                "minutes": 12,
            },
        ]
        mission = choose_unused_variant(variants, used_titles)

    else:
        variants = [
            {
                "type": "observe",
                "title": "Детектив деталей",
                "text": "Найди три детали, которые отличают эту остановку от предыдущей, и выбери одну, которую хочется запомнить.",
                "tip": "Это может быть цвет, форма, надпись, звук или устройство пространства.",
                "photo": "Сфотографируй выбранную деталь.",
                "xp": 20,
                "minutes": 12,
            },
            {
                "type": "observe",
                "title": "Один неожиданный кадр",
                "text": "Найди что-то, что не похоже на твоё первое представление об этом месте.",
                "tip": "Не ищи «правильный» ответ — важна личная неожиданность.",
                "photo": "Сохрани найденный контраст.",
                "xp": 20,
                "minutes": 10,
            },
        ]
        mission = choose_unused_variant(variants, used_titles)

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


def build_quest(city, interests, style, places, ai_meta):
    stops = []
    social_used = False
    used_titles = set()

    for i, place in enumerate(places):
        name_ru = safe_russian_name(place)
        reason = reason_for_place(place, interests)
        mission = mission_for_place(place, interests, i, used_titles)

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

    return {
        "title": str(ai_meta.get("title") or "").strip() or f"CityQuest · {city.get('input_name')}",
        "intro": str(ai_meta.get("intro") or "").strip() or (
            "Реальный городской квест: разные типы мест, фото-трофеи и небольшие задания вместо обычного списка достопримечательностей."
        ),
        "stops": stops,
        "final_challenge": str(ai_meta.get("final_challenge") or "").strip() or (
            "Выбери лучший кадр прогулки и придумай ему короткое название."
        ),
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
    kb.row(InlineKeyboardButton(text="✅ Чек-лист", callback_data="show_checklist"))
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


def photo_actions_keyboard(stop, index):
    group = place_group(stop["place"])
    kb = InlineKeyboardBuilder()

    if group in {"restaurant", "cafe", "tea", "market"}:
        kb.button(text="🍜 Что на фото / в меню?", callback_data=f"vision:menu:{index}")
        kb.button(text="🌶 Острое или нет?", callback_data=f"vision:spicy:{index}")
        kb.button(text="🥢 Из чего это?", callback_data=f"vision:ingredients:{index}")
        kb.button(text="🔤 Прочитать / перевести", callback_data=f"vision:text:{index}")
    elif group in {"heritage", "temple"}:
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place:{index}")
        kb.button(text="🗿 Что за памятник / объект?", callback_data=f"vision:monument:{index}")
        kb.button(text="🧠 Что за символ?", callback_data=f"vision:symbol:{index}")
        kb.button(text="🏮 Что здесь традиционного?", callback_data=f"vision:tradition:{index}")
        kb.button(text="🔤 Что написано?", callback_data=f"vision:text:{index}")
    elif group == "museum":
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place:{index}")
        kb.button(text="🗿 Что за объект?", callback_data=f"vision:monument:{index}")
        kb.button(text="🧠 Что можно понять по фото?", callback_data=f"vision:object:{index}")
        kb.button(text="🔤 Прочитать надпись", callback_data=f"vision:text:{index}")
    else:
        kb.button(text="🏯 Что это за место?", callback_data=f"vision:place:{index}")
        kb.button(text="🗿 Что за памятник / объект?", callback_data=f"vision:monument:{index}")
        kb.button(text="🏮 Найти китайские элементы", callback_data=f"vision:tradition:{index}")
        kb.button(text="📸 Оценить кадр", callback_data=f"vision:photo:{index}")
        kb.button(text="🔤 Что написано?", callback_data=f"vision:text:{index}")

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


def route_summary(route, quest, duration):
    walk = route["time_s"] / 60
    missions = sum(int(s["mission"].get("minutes", 12)) for s in quest["stops"])
    total = DURATION_MINUTES[duration]
    pause = max(15, int(total * 0.12))
    reserve = max(0, total - walk - missions - pause)

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
        "Напиши город Китая. Можно по-русски, по-китайски или по-английски.\n\n"
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

    await message.answer(
        f"🏮 <b>{esc(quest['title'])}</b>\n\n"
        f"{esc(quest['intro'])}\n\n"
        f"📍 {esc(city['input_name'])} · ⏱ {esc(duration)} · {esc(STYLE_LABELS[style])}\n\n"
        f"{route_summary(route, quest, duration)}"
    )

    for i in range(len(quest["stops"])):
        await send_stop_card(message, quest, route, i)

    await state.update_data(completed=[], bonuses=[], photos={})
    await state.set_state(QuestForm.quest_active)
    await message.answer(
        checklist_text(quest, [], [], {}),
        reply_markup=checklist_keyboard(quest, []),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    name = esc(message.from_user.first_name if message.from_user else "путешественник")
    await message.answer(
        f"🏮 <b>CityQuest China 城市奇遇</b>\n\n"
        f"Привет, {name}!\n\n"
        "Я превращаю прогулку по китайскому городу в AI-квест: реальные места, маршрут, миссии, фото-трофеи и китайские фразы.\n\n"
        "С чего начнём?",
        reply_markup=main_menu(),
    )


@router.message(Command("newquest"))
async def newquest(message: Message, state: FSMContext):
    await state.clear()
    await ask_city(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Квест отменён.", reply_markup=main_menu())


@router.callback_query(F.data == "new_quest")
async def newquest_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await ask_city(callback.message, state)


@router.message(QuestForm.waiting_city)
async def city_received(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    status = await message.answer("🗺 Ищу именно город в Китае…")

    try:
        city = await geocode_city(query)
    except Exception:
        logger.exception("City lookup")
        city = None

    if not city:
        await status.edit_text(
            "🤔 Не удалось однозначно найти город.\n\n"
            "Попробуй другое написание: например Сиань, 西安 или Xi'an."
        )
        return

    await state.update_data(city=city, interests=[])
    province = f"\nПровинция / регион: <b>{esc(city.get('state'))}</b>" if city.get("state") else ""

    await status.edit_text(
        f"🇨🇳 <b>Нашёл город!</b>\n\n"
        f"<b>{esc(city['input_name'])}</b> · {esc(city['formatted'])}"
        f"{province}\n📍 {city['lat']:.5f}, {city['lon']:.5f}\n\n"
        "Это тот город?",
        reply_markup=city_confirmation_keyboard(),
    )


@router.callback_query(F.data == "city_retry")
async def city_retry(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.waiting_city)
    await callback.message.answer("✏️ Напиши другой город:")


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

    if len(places) < 3:
        await status.edit_text("🤔 Нашлось слишком мало мест. Попробуй изменить интересы.")
        return

    await state.update_data(candidates=places)
    await state.set_state(QuestForm.choosing_style)

    await status.edit_text(
        f"📍 <b>Нашёл {len(places)} кандидата</b>\n\n"
        f"{candidate_summary(places)}\n\n"
        "Сейчас ничего выбирать не нужно. После выбора стиля бот сам соберёт разнообразный маршрут "
        "и покажет уже финальные точки с понятными названиями.\n\n"
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
        selected, route = await select_route(candidates, interests, duration)
    except Exception:
        logger.exception("Route")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🗺 Не получилось собрать хороший компактный маршрут. Попробуй другой набор интересов или больше времени.",
            reply_markup=style_keyboard(),
        )
        return

    await status.edit_text(
        f"🔎 <b>Маршрут готов.</b>\n\n"
        f"Пешком ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(route['time_s']/60)}.\n"
        "Уточняю финальные точки и их типы через Place Details…"
    )

    selected = await enrich_final_places(selected)

    await status.edit_text(
        f"🤖 <b>Точки проверены.</b>\n\n"
        f"Пешком ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(route['time_s']/60)}.\n"
        "ИИ теперь отвечает только за атмосферное название квеста — типы мест и причины он не придумывает."
    )

    ai_meta = await groq_meta(city, duration, interests, style, selected)
    quest = build_quest(city, interests, style, selected, ai_meta)

    await state.update_data(quest=quest, route=route, style=style)
    await status.edit_text("✅ <b>Квест готов!</b>")
    await send_quest(callback.message, state)


@router.callback_query(F.data.startswith("mission_toggle:"))
async def mission_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
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
    data = await state.get_data()
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
    await callback.answer(msg)


@router.callback_query(F.data.startswith("photo_add:"))
async def photo_add(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
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

    suitable = [p for p in message.photo if not p.file_size or p.file_size <= 2_500_000]
    chosen = suitable[-1] if suitable else message.photo[0]

    photos = dict(data.get("photos", {}))
    photos[str(idx)] = chosen.file_id

    await state.update_data(photos=photos, photo_target=None)
    await state.set_state(QuestForm.quest_active)

    stop = quest["stops"][idx]
    await message.answer(
        f"📷 <b>Фото сохранено: {esc(stop['name_ru'])}</b>\n\n"
        "Что сделать с фотографией?",
        reply_markup=photo_actions_keyboard(stop, idx),
    )


@router.message(QuestForm.waiting_photo)
async def not_photo(message: Message):
    await message.answer("Пришли фотографию или используй /cancelphoto.")


@router.callback_query(F.data.startswith("vision:"))
async def vision_callback(callback: CallbackQuery, state: FSMContext):
    _, mode, idx_raw = callback.data.split(":", 2)
    idx = int(idx_raw)

    data = await state.get_data()
    quest = data.get("quest")
    photos = data.get("photos", {})
    file_id = photos.get(str(idx))

    if not quest or not file_id:
        await callback.answer("Сначала добавь фото.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer("🤖 <b>Смотрю фотографию…</b>")

    try:
        image_bytes = await download_photo_bytes(callback.bot, file_id)
        result = await analyze_photo_with_groq(image_bytes, mode, quest["stops"][idx])
    except Exception:
        logger.exception("Vision")
        await status.edit_text(
            "🤖 Сейчас не получилось разобрать фото.\n\n"
            "Можно попробовать ещё раз или спросить человека на месте."
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
        "📱 <b>ПОКАЖИ ЭКРАН СОТРУДНИКУ</b>\n\n"
        f"<b>{esc(phrase['hanzi'])}</b>\n\n"
        f"🔤 <i>{esc(phrase['pinyin'])}</i>\n"
        f"🗣 Примерно: <b>{esc(phrase['ru'])}</b>\n"
        f"💬 {esc(phrase['translation'])}"
    )

    data = await state.get_data()
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
    data = await state.get_data()
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
    data = await state.get_data()
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
    await callback.message.answer(
        "🏠 <b>Главное меню</b>",
        reply_markup=main_menu(),
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
    data = await state.get_data()
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
    await state.set_state(QuestForm.quest_finished)

    await callback.message.answer(
        "🏆 <b>CityQuest завершён!</b>\n\n"
        f"✅ Миссии: <b>{len(completed)}/{len(quest['stops'])}</b>\n"
        f"⭐ XP: <b>{earned_xp(quest, completed, bonuses)}/{total_xp(quest)}</b>\n"
        f"📷 Фото-трофеи: <b>{len(photos)}</b>\n\n"
        f"🎁 <b>Финальный штрих:</b> {esc(quest['final_challenge'])}\n\n"
        "Следующий этап — собрать фото в красивую travel-открытку."
    )


@router.callback_query(F.data == "my_quests")
async def my_quests(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🎒 <b>Мои приключения</b>\n\n"
        "Пока активный квест и фото хранятся в памяти процесса. Постоянную историю подключим следующим этапом."
    )


@router.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>Как работает CityQuest China</b>\n\n"
        "• Город можно написать по-русски, по-китайски или по-английски.\n"
        "• Geoapify находит реальные места и проверяет пеший маршрут.\n"
        "• AI оформляет квест на русском.\n"
        "• Vision AI разбирает фото, меню, надписи, символы, достопримечательности и памятники.\n"
        "• Если AI не уверен — бот даёт простую китайскую фразу, которую можно показать человеку."
    )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Starting CityQuest China v4 Photo Travel Assistant")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
