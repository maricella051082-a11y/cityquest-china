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
    city_confirmed = State()
    choosing_interests = State()
    choosing_style = State()
    generating = State()
    quest_active = State()
    waiting_photo = State()
    quest_finished = State()


INTERESTS = {
    "history": {
        "label": "🏯 История",
        "categories": ["tourism.sights", "heritage", "entertainment.museum"],
    },
    "tea": {
        "label": "🍵 Чай",
        "categories": ["catering.cafe.tea", "catering.cafe"],
    },
    "food": {
        "label": "🍜 Еда",
        "categories": [
            "catering.restaurant",
            "catering.fast_food",
            "catering.food_court",
            "commercial.marketplace",
        ],
    },
    "photo": {
        "label": "📸 Фото",
        "categories": [
            "tourism.attraction",
            "tourism.attraction.viewpoint",
            "tourism.sights",
            "leisure.park",
        ],
    },
    "nature": {
        "label": "🌿 Природа",
        "categories": ["leisure.park", "leisure.park.garden", "natural"],
    },
    "art": {
        "label": "🎨 Искусство",
        "categories": [
            "entertainment.culture",
            "entertainment.museum",
            "tourism.attraction.artwork",
        ],
    },
    "tradition": {
        "label": "🏮 Традиции",
        "categories": [
            "tourism.sights.place_of_worship",
            "tourism.sights.city_gate",
            "tourism.sights",
            "heritage",
        ],
    },
    "unusual": {
        "label": "🕵️ Необычное",
        "categories": [
            "tourism.attraction",
            "tourism.sights",
            "commercial.marketplace",
        ],
    },
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

MISSION_LABELS = {
    "photo": "📸 Фото-квест",
    "symbol": "🏮 Охота на символ",
    "compare": "🔎 Сравнение",
    "tea": "🍵 Два аромата",
    "food": "🥟 Местный выбор",
    "museum": "🧠 Один вопрос",
    "market": "🛍 Визуальная охота",
    "park": "🌿 Поймай место",
    "art": "🎨 Лучший ракурс",
    "observe": "👀 Детектив деталей",
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

PHRASES = {
    "spicy": {
        "title": "🌶 Спросить: острое?",
        "hanzi": "这个辣不辣？",
        "pinyin": "Zhège là bu là?",
        "ru": "Чжэгэ ла бу ла?",
        "translation": "Это острое?",
    },
    "meatfish": {
        "title": "🥩 Спросить: мясо или рыба?",
        "hanzi": "这是肉还是鱼？",
        "pinyin": "Zhè shì ròu háishi yú?",
        "ru": "Чжэ ши жоу хайши юй?",
        "translation": "Это мясо или рыба?",
    },
    "inside": {
        "title": "🥢 Спросить: что внутри?",
        "hanzi": "里面有什么？",
        "pinyin": "Lǐmiàn yǒu shénme?",
        "ru": "Лимьен ёу шэньмэ?",
        "translation": "Что внутри / из чего это?",
    },
    "what": {
        "title": "❓ Спросить: что это?",
        "hanzi": "这个是什么？",
        "pinyin": "Zhège shì shénme?",
        "ru": "Чжэгэ ши шэньмэ?",
        "translation": "Что это?",
    },
    "recommend": {
        "title": "🍜 Спросить: что рекомендуете?",
        "hanzi": "你推荐什么？",
        "pinyin": "Nǐ tuījiàn shénme?",
        "ru": "Ни туэйцзень шэньмэ?",
        "translation": "Что вы рекомендуете?",
    },
    "smell": {
        "title": "🍵 Спросить: можно понюхать?",
        "hanzi": "我可以闻一下吗？",
        "pinyin": "Wǒ kěyǐ wén yíxià ma?",
        "ru": "Во кэ-и вэнь и-ся ма?",
        "translation": "Можно понюхать?",
    },
    "photo": {
        "title": "📸 Попросить сфотографировать",
        "hanzi": "请帮我拍张照片，可以吗？",
        "pinyin": "Qǐng bāng wǒ pāi zhāng zhàopiàn, kěyǐ ma?",
        "ru": "Цин бан во пай чжан чжаопьен, кэ-и ма?",
        "translation": "Можете меня сфотографировать, пожалуйста?",
    },
}


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(text or "")))


def contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", str(text or "")))


def short_text(value: str, max_len: int = 42) -> str:
    value = str(value or "").strip()
    return value if len(value) <= max_len else value[: max_len - 1].rstrip() + "…"


def strip_emoji(label: str) -> str:
    value = str(label or "").strip()
    value = re.sub(r"^[^\wА-Яа-яЁё]+", "", value).strip()
    return value or "интересное место"


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
    variants = [original]

    if contains_han(original):
        bases = [original]
        stripped = re.sub(r"[市县区]$", "", original)
        if stripped and stripped != original:
            bases.append(stripped)

        for base in bases:
            syllables = [p for p in lazy_pinyin(base, style=Style.NORMAL, errors="ignore") if p]
            if syllables:
                variants += ["".join(syllables), " ".join(syllables), "'".join(syllables)]

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
        markup.inline_keyboard.append(
            [InlineKeyboardButton(
                text=f"Продолжить ({len(selected)}/3) →",
                callback_data="interests_continue",
            )]
        )
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


def safe_russian_name(place, ai_name=""):
    if ai_name and contains_cyrillic(ai_name):
        return short_text(ai_name, 48)

    original = str(place.get("name") or "").strip()
    if original in EXACT_RU_NAMES:
        return EXACT_RU_NAMES[original]

    lower = original.casefold()
    if original and not contains_han(original):
        if "muslim street" in lower: return "Мусульманская улица / рынок"
        if "market" in lower: return "Рынок"
        if "museum" in lower: return "Музей"
        if "temple" in lower: return "Храм"
        if "park" in lower: return "Парк"
        if "tea" in lower: return "Чайная"
        if "cafe" in lower or "coffee" in lower: return "Кафе"
        return short_text(original, 48)

    if original.endswith("广场"): return "Площадь"
    if original.endswith("博物馆"): return "Музей"
    if original.endswith("公园"): return "Парк"
    if original.endswith("园"): return "Сад / парк"
    if original.endswith("寺") or original.endswith("庙"): return "Храм"
    if original.endswith("塔"): return "Пагода / башня"
    if original.endswith("城墙"): return "Городская стена"
    if original.endswith("市场"): return "Рынок"
    if original.endswith("茶馆") or original.endswith("茶楼"): return "Чайная"
    if original.endswith("街"): return "Улица / квартал"
    if "咖啡" in original: return "Кафе"

    return strip_emoji(place.get("category_label") or "интересное место").capitalize()


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
            logger.warning("Places %s HTTP %s", source_key, response.status)
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

        categories_raw = props.get("categories") or []
        output.append({
            "place_id": props.get("place_id") or "",
            "name": name,
            "pinyin": place_pinyin(name),
            "category_label": clean_category_label(categories_raw),
            "categories": categories_raw,
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
    groups = {}
    for place in places:
        g = place_group(place)
        groups[g] = groups.get(g, 0) + 1

    labels = [
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

    lines = []
    for key, label in labels:
        if groups.get(key):
            lines.append(f"{label}: <b>{groups[key]}</b>")
    return "\n".join(lines)


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
        indexes = [start]
        cur = start
        total = 0.0
        while remaining:
            nxt = min(remaining, key=lambda j: haversine(combo[cur], combo[j]))
            total += haversine(combo[cur], combo[nxt])
            indexes.append(nxt)
            remaining.remove(nxt)
            cur = nxt
        ordered = [combo[i] for i in indexes]
        if route_order_ok(ordered) and total < best_dist:
            best, best_dist = ordered, total
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
    if groups.count("park") > 2 or groups.count("heritage") > 2:
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
            {"distance_m": float(x.get("distance") or 0), "time_s": float(x.get("time") or 0)}
            for x in route.get("legs", [])
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
        "friendly_names": {"type": "array", "items": {"type": "string"}},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "final_challenge": {"type": "string"},
    },
    "required": ["title", "intro", "friendly_names", "reasons", "final_challenge"],
    "additionalProperties": False,
}


async def groq_meta(city, duration, interests, style, places):
    poi_lines = "\n".join(
        f"{i}. {p['name']} | {p.get('pinyin') or '-'} | {p['category_label']}"
        for i, p in enumerate(places, 1)
    )
    interest_text = ", ".join(INTERESTS[k]["label"] for k in interests)
    prompt = f"""
Ты только оформляешь готовый безопасный CityQuest. Не выдумывай миссии и факты.

Город: {city.get('input_name')}
Время: {duration}
Интересы: {interest_text}
Стиль: {STYLE_LABELS[style]}

Верни title, intro, friendly_names, reasons, final_challenge.
friendly_names: ровно {len(places)} коротких понятных названий НА РУССКОМ.
Можно переводить буквальное название точки, если перевод очевиден из названия.
Если не уверен — используй безопасный тип: парк, музей, историческое место, чайная, рынок.
reasons: ровно {len(places)} коротких причин на русском.
Не утверждай, что на месте есть конкретный объект, если во входных данных этого нет.

Точки:
{poi_lines}
""".strip()

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "Ты редактор CityQuest China, не источник фактов о местах."},
            {"role": "user", "content": prompt},
        ],
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "cityquest_meta", "strict": True, "schema": AI_META_SCHEMA},
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
                    logger.error("Groq meta %s: %s", response.status, body[:500])
        except Exception:
            logger.exception("Groq meta error")
        if attempt == 0:
            await asyncio.sleep(2)
    return {}


def phrase_data(key):
    return PHRASES[key]


def mission_for_place(place, interests, index):
    group = place_group(place)
    photo_interest = "photo" in interests
    tradition_interest = "tradition" in interests

    mission = {
        "type": "observe",
        "title": "Детектив деталей",
        "text": "Найди три детали, которые отличают эту остановку от предыдущей, и выбери одну, которую хочется запомнить.",
        "tip": "Это может быть цвет, форма, надпись, звук или устройство пространства.",
        "photo": "Сфотографируй выбранную деталь — это будет трофей этой остановки.",
        "xp": 20,
        "minutes": 12,
        "phrase": None,
    }

    if group in {"heritage", "temple"}:
        mission.update({
            "type": "symbol",
            "title": "Охота на китайский символ",
            "text": (
                "Осмотрись и попробуй найти любой реально видимый национальный элемент: "
                "дракона, льва, фонарь, иероглиф, облачный орнамент, красную декоративную деталь "
                "или необычную форму крыши. Если ни одного примера нет — выбери любую деталь, "
                "которая кажется тебе особенно китайской."
            ),
            "tip": "Сфотографируй находку. После загрузки фото можно нажать «Что за символ?» и попросить AI объяснить её.",
            "photo": "Сделай крупный кадр найденного элемента.",
            "xp": 30,
            "minutes": 15,
        })

    elif group == "park":
        variants = [
            (
                "Поймай город",
                "Найди место, где в одном взгляде встречаются природа и что-то созданное человеком. Выбери самый красивый контраст.",
                "Подойдут дерево + здание, вода + мост, листья + фонарь, тень + дорожка.",
                "Сделай кадр из двух миров: природа + городская деталь.",
            ),
            (
                "Три цвета",
                "Найди три цвета, которые чаще всего встречаются вокруг. Выбери один как «цвет этого места».",
                "Учитывай растения, здания, вывески и дорожки.",
                "Собери кадр, где выбранный цвет заметен минимум дважды.",
            ),
            (
                "Рамка внутри кадра",
                "Найди естественную рамку для фотографии: ветви, ворота, арку, проём или две детали по краям.",
                "Если готовой рамки нет — создай её из объектов по краям кадра.",
                "Сделай один кадр через найденную рамку.",
            ),
        ]
        title, txt, tip, photo = variants[index % len(variants)]
        mission.update({
            "type": "park",
            "title": title,
            "text": txt,
            "tip": tip,
            "photo": photo,
            "xp": 30 if photo_interest and index % 3 == 2 else 20,
            "minutes": 12,
        })
        if tradition_interest and index % 2 == 0:
            mission["text"] += " Если увидишь традиционный элемент — крышу, фонарь, каллиграфию, ворота или павильон — попробуй включить его в наблюдение."

    elif group == "tea":
        mission.update({
            "type": "tea",
            "title": "Два аромата",
            "text": (
                "Если сотрудники могут показать или дать понюхать два чая, сравни их аромат. "
                "Если такой возможности нет, сравни два напитка по названию, описанию или фотографии."
            ),
            "tip": "Покупать напиток не обязательно.",
            "photo": "Сфотографируй меню, название чая, упаковку или чашку — если удобно и разрешено.",
            "xp": 30,
            "minutes": 15,
            "phrase": phrase_data("smell"),
        })

    elif group in {"restaurant", "cafe"}:
        mission.update({
            "type": "food",
            "title": "Местный выбор",
            "text": (
                "Найди в меню две позиции, которые тебе незнакомы или кажутся необычными. "
                "Выбери одну, которую попробовал бы первой. Покупать её не обязательно."
            ),
            "tip": "Сфотографируй меню: после загрузки AI сможет попробовать прочитать название, оценить, похоже ли блюдо на острое, и предложить вопрос сотруднику, если не уверен.",
            "photo": "Сохрани фото меню, названия блюда, вывески или подачи.",
            "xp": 20,
            "minutes": 12,
            "phrase": phrase_data("recommend"),
        })

    elif group == "market":
        mission.update({
            "type": "market",
            "title": "Что здесь самое необычное?",
            "text": "Найди три вещи, блюда, упаковки или вывески, которых обычно не видишь дома. Выбери одну как главный трофей улицы.",
            "tip": "Не фотографируй людей крупным планом без разрешения.",
            "photo": "Сфотографируй выбранный трофей или его название.",
            "xp": 20,
            "minutes": 12,
            "phrase": phrase_data("what"),
        })

    elif group == "museum":
        mission.update({
            "type": "museum",
            "title": "Один вопрос",
            "text": (
                "Если музей открыт, найди предмет или изображение, о котором захотелось бы узнать больше, "
                "и придумай к нему один вопрос. Если внутрь не заходишь — выбери деталь фасада или входной зоны."
            ),
            "tip": "Если съёмка разрешена, загрузи фото и спроси AI, что он может определить по изображению.",
            "photo": "Если съёмка разрешена — сохрани объект; иначе сфотографируй фасад или название музея.",
            "xp": 30,
            "minutes": 15,
        })

    elif group in {"art", "viewpoint"}:
        mission.update({
            "type": "art",
            "title": "Два ракурса",
            "text": "Посмотри на место с двух разных точек или дистанций. Реши, какой ракурс делает его интереснее.",
            "tip": "Сравни линии, масштаб, свет и передний план.",
            "photo": "Сделай лучший из двух кадров.",
            "xp": 30 if photo_interest else 20,
            "minutes": 12,
        })

    if photo_interest and group not in {"tea", "restaurant", "cafe"}:
        mission["photo"] += " Попробуй не открытку, а свой необычный ракурс."

    return mission


def optional_social_bonus(place):
    if not is_outdoor_social_place(place):
        return None
    return {
        "text": "Если комфортно, попроси прохожего сфотографировать тебя. Не хочется обращаться — селфи тоже засчитывается.",
        "xp": 10,
        "phrase": phrase_data("photo"),
    }


def build_quest(city, interests, style, places, ai_meta):
    names = ai_meta.get("friendly_names") if isinstance(ai_meta.get("friendly_names"), list) else []
    reasons = ai_meta.get("reasons") if isinstance(ai_meta.get("reasons"), list) else []
    stops, social_used = [], False

    for i, place in enumerate(places):
        ai_name = str(names[i]).strip() if i < len(names) else ""
        name_ru = safe_russian_name(place, ai_name)
        reason = (
            str(reasons[i]).strip()
            if i < len(reasons) and str(reasons[i]).strip()
            else "Эта точка добавляет в маршрут другой тип впечатления и подходит под выбранные интересы."
        )
        mission = mission_for_place(place, interests, i)
        bonus = None
        if style == "adventure" and not social_used and is_outdoor_social_place(place) and i >= 1:
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
        "intro": str(ai_meta.get("intro") or "").strip() or "Реальный городской квест: ищи детали, собирай фото-трофеи и отмечай миссии.",
        "stops": stops,
        "final_challenge": str(ai_meta.get("final_challenge") or "").strip() or "Выбери лучший кадр прогулки и придумай ему название.",
    }


def total_xp(quest):
    return sum(
        int(s["mission"].get("xp", 20)) + (int(s["bonus"]["xp"]) if s.get("bonus") else 0)
        for s in quest.get("stops", [])
    )


def earned_xp(quest, completed, bonuses):
    c, b = set(completed), set(bonuses)
    total = 0
    for i, stop in enumerate(quest.get("stops", [])):
        if i in c:
            total += int(stop["mission"].get("xp", 20))
        if i in b and stop.get("bonus"):
            total += int(stop["bonus"].get("xp", 10))
    return total


def checklist_text(quest, completed, bonuses, photos):
    c = set(completed)
    lines = ["✅ <b>Чек-лист квеста</b>", "", "Нажимай на миссию, когда выполнишь её.", ""]
    for i, stop in enumerate(quest["stops"]):
        mark = "✅" if i in c else "☐"
        photo = " 📷" if str(i) in photos else ""
        lines.append(f"{mark} <b>{i+1}.</b> {esc(stop['name_ru'])}{photo}")

    lines += [
        "",
        ("🟩" * len(c)) + ("⬜" * (len(quest["stops"]) - len(c))),
        f"Прогресс: <b>{len(c)}/{len(quest['stops'])}</b>",
        f"⭐ XP: <b>{earned_xp(quest, completed, bonuses)}/{total_xp(quest)}</b>",
        f"📷 Фото: <b>{len(photos)}/{len(quest['stops'])}</b>",
    ]
    return "\n".join(lines)


def checklist_keyboard(quest, completed):
    c = set(completed)
    kb = InlineKeyboardBuilder()
    for i, stop in enumerate(quest["stops"]):
        kb.button(
            text=f"{'✅' if i in c else '☐'} {i+1} · {short_text(stop['name_ru'], 30)}",
            callback_data=f"mission_toggle:{i}",
        )
    kb.adjust(1)
    if len(c) == len(quest["stops"]):
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


def phrase_text(phrase):
    if not phrase:
        return ""
    return (
        f"\n\n🇨🇳 <b>{esc(phrase['hanzi'])}</b>\n"
        f"🔤 <i>{esc(phrase['pinyin'])}</i>\n"
        f"🗣 <b>Примерно:</b> {esc(phrase['ru'])}\n"
        f"💬 {esc(phrase['translation'])}"
    )


def phrase_show_keyboard(keys):
    kb = InlineKeyboardBuilder()
    for key in keys:
        kb.button(text=PHRASES[key]["title"], callback_data=f"phrase:{key}")
    kb.adjust(1)
    return kb.as_markup()


def photo_actions_keyboard(stop, index):
    group = place_group(stop["place"])
    kb = InlineKeyboardBuilder()

    if group in {"restaurant", "cafe", "tea", "market"}:
        kb.button(text="🍜 Что на фото / в меню?", callback_data=f"vision:menu:{index}")
        kb.button(text="🌶 Похоже на острое?", callback_data=f"vision:spicy:{index}")
        kb.button(text="🥢 Из чего это?", callback_data=f"vision:ingredients:{index}")
        kb.button(text="🔤 Прочитать / перевести", callback_data=f"vision:text:{index}")
        kb.adjust(1)
    elif group in {"heritage", "temple"}:
        kb.button(text="🧠 Что за символ?", callback_data=f"vision:symbol:{index}")
        kb.button(text="🏮 Что здесь традиционного?", callback_data=f"vision:tradition:{index}")
        kb.button(text="🔤 Что написано?", callback_data=f"vision:text:{index}")
        kb.adjust(1)
    elif group == "museum":
        kb.button(text="🧠 Что можно понять по фото?", callback_data=f"vision:object:{index}")
        kb.button(text="🔤 Прочитать надпись", callback_data=f"vision:text:{index}")
        kb.adjust(1)
    else:
        kb.button(text="🏮 Найти китайские элементы", callback_data=f"vision:tradition:{index}")
        kb.button(text="📸 Оценить кадр", callback_data=f"vision:photo:{index}")
        kb.button(text="🔤 Что написано?", callback_data=f"vision:text:{index}")
        kb.adjust(1)

    return kb.as_markup()


async def download_photo_bytes(bot, file_id):
    tg_file = await bot.get_file(file_id)
    buffer = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buffer)
    return buffer.getvalue()


def vision_prompt(mode, stop):
    place_name = stop["name_ru"]
    context = f"Фото сделано пользователем во время CityQuest, остановка: {place_name}."

    common = """
КРИТИЧЕСКИ:
- не выдумывай то, чего на фото не видно;
- если не уверен — прямо скажи «не уверен»;
- различай «видно на фото» и «вероятно»;
- отвечай по-русски, коротко и практично;
- китайский текст, если читается, пиши: 汉字 + pinyin + перевод.
""".strip()

    prompts = {
        "symbol": f"""
{context}
Пользователь выполнял миссию «Охота на китайский символ».
Определи, какой декоративный/культурный элемент виден на фото, только если это можно reasonably определить.
Если это дракон, лев, иероглиф, орнамент, фонарь и т.п. — кратко объясни возможное культурное значение.
Если точная идентификация неуверенная, скажи это.
{common}
""",
        "tradition": f"""
{context}
Найди на фото элементы, которые выглядят связанными с китайской традиционной визуальной культурой:
крыша, ворота, фонарь, каллиграфия, орнамент, цветовая символика и т.п.
Не утверждай исторический возраст или значение без уверенности.
{common}
""",
        "text": f"""
{context}
Попробуй прочитать видимый китайский текст.
Для каждой уверенно читаемой короткой надписи дай:
1) 汉字
2) pinyin
3) перевод на русский.
Неразборчивое не угадывай.
{common}
""",
        "menu": f"""
{context}
Это фото меню, еды, напитка или вывески.
Если читается название блюда/напитка — дай 汉字, pinyin и понятное русское название.
Если можно уверенно понять основные ингредиенты из надписи — перечисли.
Если состав по фото/тексту непонятен, не угадывай и напиши, что лучше спросить сотрудника.
{common}
""",
        "spicy": f"""
{context}
Определи, есть ли на фото/в тексте признаки острого блюда: например 辣, 麻辣, 香辣, 辣椒 и т.п.
Если таких признаков не видно — НЕ делай вывод по цвету блюда и скажи, что по фото неясно.
{common}
""",
        "ingredients": f"""
{context}
Попробуй понять состав только по читаемому названию/описанию или очень очевидным ингредиентам.
Особенно отметь, если уверенно видно/написано мясо, рыба, курица, свинина, говядина, овощи.
Если не уверен — предложи спросить сотрудника.
{common}
""",
        "object": f"""
{context}
Опиши, что реально видно на фото и что можно осторожно предположить об объекте.
Не придумывай название экспоната, дату, автора или историю, если это не читается на табличке.
{common}
""",
        "photo": f"""
{context}
Дай 3 коротких совета по композиции именно этого кадра: что уже работает и что можно изменить,
не требуя специальной техники.
{common}
""",
    }
    return prompts.get(mode, prompts["object"]).strip()


async def analyze_photo_with_groq(image_bytes, mode, stop):
    # Groq base64 requests have a smaller limit; Telegram gives several photo sizes,
    # and we intentionally store a compressed variant <= ~2.5 MB.
    if len(image_bytes) > 3_000_000:
        raise RuntimeError("Photo is too large for vision request")

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
            return data["choices"][0]["message"]["content"].strip()


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
        "Напиши город Китая. Можно по-китайски или по-английски.\n"
        "Например: 成都 · 西安 · Hangzhou"
    )


async def show_interests(message, state):
    data = await state.get_data()
    await state.set_state(QuestForm.choosing_interests)
    await message.answer(
        "✨ <b>Что тебе интересно?</b>\n\n"
        "Выбери 1–3 темы. Фото всё равно будет частью квеста; выбор «Фото» "
        "делает задания более композиционными.",
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
        f"{route_summary(route, quest, duration)}\n\n"
        "🤖 <i>ИИ оформляет квест; реальные точки и пеший маршрут проверяются отдельно.</i>"
    )

    legs = route.get("legs", [])
    for i, stop in enumerate(quest["stops"]):
        place, mission = stop["place"], stop["mission"]
        transition = ""
        if i > 0 and i - 1 < len(legs):
            leg = legs[i - 1]
            transition = (
                f"\n🚶 <b>От предыдущей:</b> ~{fmt_distance(leg['distance_m'])} · "
                f"~{fmt_minutes(leg['time_s']/60)}\n"
            )

        bonus = ""
        if stop.get("bonus"):
            b = stop["bonus"]
            bonus = (
                f"\n\n🎁 <b>BONUS +{b['xp']} XP</b>\n{esc(b['text'])}"
                f"{phrase_text(b['phrase'])}"
            )

        await message.answer(
            f"📍 <b>{i+1}/{len(quest['stops'])}. {esc(stop['name_ru'])}</b>\n"
            f"{esc(place['category_label'])} — <b>{esc(place['name'])}</b>\n"
            f"<i>{esc(place.get('pinyin'))}</i>"
            f"{transition}\n"
            f"💡 <b>Почему здесь:</b> {esc(stop['why_here'])}\n\n"
            f"{MISSION_LABELS.get(mission['type'], '🎯 Миссия')}\n"
            f"🎯 <b>Миссия «{esc(mission['title'])}»:</b>\n{esc(mission['text'])}\n\n"
            f"🧭 <b>Подсказка:</b> {esc(mission['tip'])}\n\n"
            f"📷 <b>Фото-трофей:</b> {esc(mission['photo'])}\n"
            f"⭐ Награда: <b>+{mission['xp']} XP</b>"
            f"{phrase_text(mission.get('phrase'))}"
            f"{bonus}",
            reply_markup=stop_keyboard(place, i, bool(stop.get("bonus"))),
        )

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
        f"🏮 <b>CityQuest China 城市奇遇</b>\n\nПривет, {name}!\n\n"
        "Я превращаю прогулку по китайскому городу в AI-квест: реальные места, "
        "пеший маршрут, миссии, фото-трофеи и подсказки по китайскому.\n\nС чего начнём?",
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
    if callback.message:
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
        await status.edit_text("🤔 Не удалось однозначно найти город. Попробуй 西安 или Xi'an.")
        return

    await state.update_data(city=city, interests=[])
    province = f"\nПровинция / регион: <b>{esc(city.get('state'))}</b>" if city.get("state") else ""
    await status.edit_text(
        f"🇨🇳 <b>Нашёл город!</b>\n\n<b>{esc(city['input_name'])}</b> · {esc(city['formatted'])}"
        f"{province}\n📍 {city['lat']:.5f}, {city['lon']:.5f}\n\nЭто тот город?",
        reply_markup=city_confirmation_keyboard(),
    )


@router.callback_query(F.data == "city_retry")
async def city_retry(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.waiting_city)
    if callback.message:
        await callback.message.answer("✏️ Напиши другой город:")


@router.callback_query(F.data == "city_confirm")
async def city_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.city_confirmed)
    if callback.message:
        await callback.message.answer("⏱ <b>Сколько времени у тебя есть?</b>", reply_markup=duration_keyboard())


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
    if callback.message:
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
    if callback.message:
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
        "Собираю разные типы точек. Сырые названия карты показывать не буду — "
        "сначала соберу нормальный маршрут."
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

    summary = candidate_summary(places)
    await status.edit_text(
        f"📍 <b>Нашёл {len(places)} кандидата</b>\n\n"
        f"{summary}\n\n"
        "Сейчас ничего выбирать не нужно. После выбора стиля бот сам соберёт "
        "разнообразный маршрут и покажет уже нормальные названия финальных точек.\n\n"
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
        "Не ставлю гастро-точки подряд и проверяю реальные пешие переходы."
    )

    try:
        selected, route = await select_route(candidates, interests, duration)
    except Exception:
        logger.exception("Route")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🗺 Не получилось собрать хороший компактный маршрут. "
            "Попробуй другой набор интересов или больше времени.",
            reply_markup=style_keyboard(),
        )
        return

    await status.edit_text(
        f"🤖 <b>Маршрут готов.</b>\n\n"
        f"Пешком ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(route['time_s']/60)}.\n"
        "ИИ оформляет названия и атмосферу."
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

    if callback.message:
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
        "Отправь фотографию следующим сообщением. После загрузки появятся AI-кнопки "
        "именно для этой точки.\n\nДля отмены: /cancelphoto"
    )


@router.message(Command("cancelphoto"))
async def cancelphoto(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.update_data(photo_target=None)
    await state.set_state(QuestForm.quest_active if data.get("quest") else None)
    await message.answer("Загрузка фото отменена.")


@router.message(QuestForm.waiting_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    quest, idx = data.get("quest"), data.get("photo_target")
    if not quest or idx is None:
        await message.answer("Не удалось определить миссию.")
        return

    # Pick the largest Telegram-compressed variant that should remain safe for Groq base64.
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
        await callback.answer("Сначала добавь фото для этой миссии.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer("🤖 <b>Смотрю фотографию…</b>")

    try:
        image_bytes = await download_photo_bytes(callback.bot, file_id)
        result = await analyze_photo_with_groq(image_bytes, mode, quest["stops"][idx])
    except Exception:
        logger.exception("Vision analysis")
        await status.edit_text(
            "🤖 Не получилось проанализировать фото сейчас.\n"
            "Можно попробовать ещё раз или воспользоваться готовой китайской фразой."
        )
        keys = followup_phrase_keys(mode)
        if keys:
            await callback.message.answer(
                "💬 Можно спросить человека на месте:",
                reply_markup=phrase_show_keyboard(keys),
            )
        return

    await status.edit_text(
        f"🤖 <b>AI-разбор фото</b>\n\n{esc(result)}"
    )

    keys = followup_phrase_keys(mode)
    if keys:
        await callback.message.answer(
            "💬 <b>Если по фото всё равно неясно — спроси сотрудника:</b>",
            reply_markup=phrase_show_keyboard(keys),
        )


@router.callback_query(F.data.startswith("phrase:"))
async def phrase_callback(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
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


@router.callback_query(F.data == "quest_finish")
async def finish(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quest = data.get("quest")
    completed = set(data.get("completed", []))
    if not quest:
        await callback.answer("Квест не найден.", show_alert=True)
        return
    if len(completed) != len(quest["stops"]):
        await callback.answer("Сначала отметь все миссии.", show_alert=True)
        return

    bonuses, photos = data.get("bonuses", []), data.get("photos", {})
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
        "Пока активный квест и фото хранятся в памяти процесса. "
        "Постоянную историю и итоговые коллажи подключим следующим этапом."
    )


@router.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>Как работает CityQuest China</b>\n\n"
        "1. Geoapify находит реальный город и POI.\n"
        "2. Routing API проверяет пеший маршрут.\n"
        "3. Mission Engine следит за разнообразием.\n"
        "4. Groq AI оформляет квест.\n"
        "5. Vision AI может разбирать загруженные фото, меню, надписи и символы.\n"
        "6. Если AI не уверен — бот предлагает простую китайскую фразу, которую можно показать человеку."
    )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Starting CityQuest China Mission Engine v2 + Photo AI")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
