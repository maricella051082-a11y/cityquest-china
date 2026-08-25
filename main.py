import asyncio
import html
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

# One optional rest stop may be mixed into 4h+ routes.
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


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(text or "")))


def contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", str(text or "")))


def short_text(value: str, max_len: int = 42) -> str:
    value = str(value or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def strip_emoji(label: str) -> str:
    value = str(label or "").strip()
    value = re.sub(r"^[^\wА-Яа-яЁё]+", "", value).strip()
    return value or "интересное место"


def fmt_minutes(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч" if not mins else f"{hours} ч {mins} мин"


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
            syllables = [
                p for p in lazy_pinyin(base, style=Style.NORMAL, errors="ignore") if p
            ]
            if syllables:
                variants.extend(
                    [
                        "".join(syllables),
                        " ".join(syllables),
                        "'".join(syllables),
                    ]
                )

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


def interests_keyboard(selected: list[str] | None = None):
    selected = set(selected or [])
    kb = InlineKeyboardBuilder()

    for key, meta in INTERESTS.items():
        prefix = "✅ " if key in selected else ""
        kb.button(text=f"{prefix}{meta['label']}", callback_data=f"interest:{key}")

    kb.adjust(2)
    markup = kb.as_markup()

    if selected:
        markup.inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"Продолжить ({len(selected)}/3) →",
                    callback_data="interests_continue",
                )
            ]
        )
    return markup


def style_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="😌 Спокойно", callback_data="style:calm")
    kb.button(text="🔎 Исследователь", callback_data="style:explorer")
    kb.button(text="🔥 Приключение", callback_data="style:adventure")
    kb.adjust(1)
    return kb.as_markup()


def clean_category_label(categories: list[str]) -> str:
    cats = categories or []

    def has(prefix: str) -> bool:
        return any(c == prefix or c.startswith(prefix + ".") for c in cats)

    if has("catering.cafe.tea"):
        return "🍵 чайная"
    if has("catering.restaurant"):
        return "🍜 ресторан"
    if has("catering.food_court"):
        return "🍽 фуд-корт"
    if has("catering.fast_food"):
        return "🥡 кафе / стритфуд"
    if has("catering.cafe"):
        return "☕ кафе"
    if has("commercial.marketplace"):
        return "🛍 рынок"
    if has("entertainment.museum"):
        return "🏛 музей"
    if has("tourism.sights.place_of_worship"):
        return "🛕 храм / религиозное место"
    if has("tourism.sights.city_gate"):
        return "🏮 исторические ворота"
    if has("tourism.attraction.viewpoint"):
        return "📸 смотровая точка"
    if has("tourism.attraction.artwork"):
        return "🎨 арт-объект"
    if has("leisure.park.garden"):
        return "🌺 сад"
    if has("leisure.park"):
        return "🌿 парк"
    if has("natural"):
        return "🌿 природное место"
    if has("entertainment.culture"):
        return "🎭 культурное место"
    if has("heritage"):
        return "🏯 историческое место"
    if has("tourism.sights"):
        return "🏯 достопримечательность"
    if has("tourism.attraction"):
        return "📍 достопримечательность"
    return "📍 интересное место"


def place_group(place: dict) -> str:
    label = (place.get("category_label") or "").lower()

    if "ресторан" in label:
        return "restaurant"
    if "чайная" in label:
        return "tea"
    if "кафе" in label or "фуд-корт" in label or "стритфуд" in label:
        return "cafe"
    if "рынок" in label:
        return "market"
    if "парк" in label or "сад" in label or "природ" in label:
        return "park"
    if "музей" in label:
        return "museum"
    if "храм" in label or "религиоз" in label:
        return "temple"
    if "истор" in label or "ворота" in label or "достопримеч" in label:
        return "heritage"
    if "арт" in label or "культур" in label:
        return "art"
    if "смотров" in label:
        return "viewpoint"
    return "other"


def is_food_group(group: str) -> bool:
    return group in {"restaurant", "tea", "cafe"}


def is_outdoor_social_place(place: dict) -> bool:
    return place_group(place) in {
        "park", "market", "heritage", "temple", "viewpoint", "art", "other"
    }


def safe_russian_name(place: dict, ai_name: str = "") -> str:
    if ai_name and contains_cyrillic(ai_name):
        return short_text(ai_name, 48)

    original = str(place.get("name") or "").strip()
    if original in EXACT_RU_NAMES:
        return EXACT_RU_NAMES[original]

    lower = original.casefold()
    if original and not contains_han(original):
        if "muslim street" in lower:
            return "Мусульманская улица / рынок"
        if "market" in lower:
            return "Рынок"
        if "museum" in lower:
            return "Музей"
        if "temple" in lower:
            return "Храм"
        if "park" in lower:
            return "Парк"
        if "tea" in lower:
            return "Чайная"
        if "cafe" in lower or "coffee" in lower:
            return "Кафе"
        return short_text(original, 48)

    if original.endswith("广场"):
        return "Площадь"
    if original.endswith("博物馆"):
        return "Музей"
    if original.endswith("公园"):
        return "Парк"
    if original.endswith("园"):
        return "Сад / парк"
    if original.endswith("寺") or original.endswith("庙"):
        return "Храм"
    if original.endswith("塔"):
        return "Пагода / башня"
    if original.endswith("城墙"):
        return "Городская стена"
    if original.endswith("市场"):
        return "Рынок"
    if original.endswith("茶馆") or original.endswith("茶楼"):
        return "Чайная"
    if original.endswith("街"):
        return "Улица / квартал"
    if "咖啡" in original:
        return "Кафе"

    return strip_emoji(place.get("category_label") or "интересное место").capitalize()


async def geocode_city(city_query: str) -> dict | None:
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
                    logger.error("Geocode error %s: %s", response.status, (await response.text())[:500])
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
                ranks = item.get("rank") or {}
                return (
                    float(ranks.get("confidence_city_level") or 0),
                    float(ranks.get("confidence") or 0),
                    float(ranks.get("popularity") or 0),
                )

            item = max(valid, key=rank)
            logger.info("City matched %r -> %r", city_query, item.get("formatted"))
            return {
                "place_id": item.get("place_id"),
                "formatted": item.get("formatted") or city_query,
                "city": item.get("city") or city_query,
                "state": item.get("state"),
                "lat": float(item.get("lat")),
                "lon": float(item.get("lon")),
                "input_name": city_query,
            }

    return None


async def fetch_places_source(
    session: aiohttp.ClientSession,
    city: dict,
    source_key: str,
    categories: list[str],
    limit: int = 20,
) -> list[dict]:
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
            logger.warning("Places %s failed: %s", source_key, response.status)
            return []
        data = await response.json()

    output = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = (props.get("name") or "").strip()
        if not name or re.match(r"^\d{5,6}(?:\s|$)", name):
            continue

        lat, lon = props.get("lat"), props.get("lon")
        if lat is None or lon is None:
            continue

        categories_raw = props.get("categories") or []
        output.append(
            {
                "place_id": props.get("place_id") or "",
                "name": name,
                "pinyin": place_pinyin(name),
                "category_label": clean_category_label(categories_raw),
                "categories": categories_raw,
                "lat": float(lat),
                "lon": float(lon),
                "interest_matches": [source_key],
            }
        )
    return output


async def search_places(city: dict, interests: list[str], duration: str) -> list[dict]:
    sources = [(key, INTERESTS[key]["categories"]) for key in interests]

    # A single possible rest stop, but never let it dominate.
    if duration != "2 часа" and "tea" not in interests and "food" not in interests:
        sources.append(("rest", REST_CATEGORIES))

    timeout = aiohttp.ClientTimeout(total=35)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(
            *[
                fetch_places_source(session, city, key, categories)
                for key, categories in sources
            ]
        )

    merged: dict[str, dict] = {}
    per_source: dict[str, list[str]] = {key: [] for key, _ in sources}

    for (source_key, _), places in zip(sources, results):
        for place in places:
            unique = place.get("place_id") or (
                f"{place['name'].casefold()}:{place['lat']:.5f}:{place['lon']:.5f}"
            )
            if unique in merged:
                if source_key not in merged[unique]["interest_matches"]:
                    merged[unique]["interest_matches"].append(source_key)
            else:
                merged[unique] = place
            if unique not in per_source[source_key]:
                per_source[source_key].append(unique)

    # Round-robin across interests prevents one broad category from filling the whole pool.
    ordered, seen = [], set()
    for pos in range(14):
        for source_key, _ in sources:
            source_list = per_source.get(source_key, [])
            if pos >= len(source_list):
                continue
            key = source_list[pos]
            if key not in seen:
                seen.add(key)
                ordered.append(key)
            if len(ordered) >= 24:
                break
        if len(ordered) >= 24:
            break

    for key in merged:
        if key not in seen and len(ordered) < 24:
            ordered.append(key)
            seen.add(key)

    return [merged[key] for key in ordered]


def haversine(a: dict, b: dict) -> float:
    r = 6371000.0
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


def best_nearest_order(combo: list[dict]) -> tuple[list[dict], float]:
    best, best_dist = combo[:], float("inf")

    for start in range(len(combo)):
        remaining = set(range(len(combo)))
        remaining.remove(start)
        indexes = [start]
        current = start
        distance = 0.0

        while remaining:
            nxt = min(remaining, key=lambda j: haversine(combo[current], combo[j]))
            distance += haversine(combo[current], combo[nxt])
            indexes.append(nxt)
            remaining.remove(nxt)
            current = nxt

        ordered = [combo[i] for i in indexes]
        if route_order_rules_ok(ordered) and distance < best_dist:
            best, best_dist = ordered, distance

    return best, best_dist


def route_order_rules_ok(stops: list[dict]) -> bool:
    groups = [place_group(p) for p in stops]

    # Never two food/tea/cafe stops consecutively.
    for a, b in zip(groups, groups[1:]):
        if is_food_group(a) and is_food_group(b):
            return False
    return True


def combination_rules_ok(combo: list[dict], interests: list[str]) -> bool:
    groups = [place_group(p) for p in combo]

    # Food should enrich the walk, not become the walk.
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

    # A 5-stop route should have at least 3 broad groups.
    if len(combo) >= 5 and len(set(groups)) < 3:
        return False
    if len(combo) == 4 and len(set(groups)) < 3:
        return False

    # Cover every chosen interest if such places exist in the pool.
    covered = set()
    for p in combo:
        covered.update(k for k in p.get("interest_matches", []) if k in interests)
    return set(interests).issubset(covered)


async def walking_route(stops: list[dict]) -> dict:
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
                raise RuntimeError(f"Routing HTTP {response.status}: {body[:400]}")
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


def candidate_stop_counts(duration: str) -> list[int]:
    return {
        "2 часа": [3],
        "4 часа": [5, 4],
        "6 часов": [6, 5],
        "весь день": [7, 6],
    }.get(duration, [4])


def route_fits(route: dict, duration: str, stop_count: int) -> bool:
    total = DURATION_MINUTES[duration]
    walk = route["time_s"] / 60
    estimated_missions = 14 * stop_count
    break_buffer = max(15, total * 0.12)

    if walk > total * 0.45:
        return False
    if walk + estimated_missions + break_buffer > total:
        return False

    leg_limit = MAX_LEG_MINUTES[duration]
    if any(leg["time_s"] / 60 > leg_limit for leg in route.get("legs", [])):
        return False
    return True


async def select_route(
    places: list[dict],
    interests: list[str],
    duration: str,
) -> tuple[list[dict], dict]:
    pool = places[:18]

    for wanted in candidate_stop_counts(duration):
        if len(pool) < wanted:
            continue

        scored: list[tuple[float, list[dict]]] = []
        for indexes in itertools.combinations(range(len(pool)), wanted):
            combo = [pool[i] for i in indexes]
            if not combination_rules_ok(combo, interests):
                continue
            ordered, approx = best_nearest_order(combo)
            if not route_order_rules_ok(ordered):
                continue

            diversity = len(set(place_group(p) for p in combo))
            score = approx - diversity * 250
            scored.append((score, ordered))

        scored.sort(key=lambda x: x[0])

        for _, ordered in scored[:10]:
            try:
                route = await walking_route(ordered)
            except Exception:
                continue
            logger.info(
                "Route %s stops: %.1f km / %.1f min / %s",
                len(ordered),
                route["distance_m"] / 1000,
                route["time_s"] / 60,
                [place_group(p) for p in ordered],
            )
            if route_fits(route, duration, len(ordered)):
                return ordered, route

    raise RuntimeError("No realistic diversified route")


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


async def groq_meta(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
) -> dict:
    poi_lines = "\n".join(
        f"{i}. {p['name']} | {p.get('pinyin') or '-'} | {p['category_label']}"
        for i, p in enumerate(places, 1)
    )
    interest_text = ", ".join(INTERESTS[k]["label"] for k in interests)

    prompt = f"""
Ты редактируешь уже готовый безопасный городской квест.
НЕ ПРИДУМЫВАЙ миссии, факты, предметы и достопримечательности.
Нужна только атмосферная оболочка.

Город: {city.get('input_name') or city.get('city')}
Время: {duration}
Интересы: {interest_text}
Стиль: {STYLE_LABELS.get(style, style)}

Верни:
1) короткое название квеста;
2) intro — 1–2 предложения;
3) friendly_names — ровно {len(places)} коротких понятных названий НА РУССКОМ;
4) reasons — ровно {len(places)} коротких причин, почему эта точка подходит;
5) final_challenge — финальное фото/рефлексия без внешних материалов.

Если точный перевод китайского названия неизвестен, пиши безопасный русский тип:
«парк», «чайная», «историческое место», «рынок», «музей» и т.п.
Не выдумывай перевод.

Точки:
{poi_lines}
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
                "content": "Ты редактор CityQuest China. Только оформление, никаких выдуманных фактов.",
            },
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
            logger.exception("Groq meta exception")
        if attempt == 0:
            await asyncio.sleep(2)

    raise RuntimeError("Groq meta unavailable")


def phrase_block(
    hanzi: str,
    pinyin: str,
    ru: str,
    translation: str,
) -> dict:
    return {
        "hanzi": hanzi,
        "pinyin": pinyin,
        "ru": ru,
        "translation": translation,
    }


def mission_for_place(
    place: dict,
    interests: list[str],
    style: str,
    index: int,
) -> dict:
    group = place_group(place)
    photo_interest = "photo" in interests
    tradition_interest = "tradition" in interests

    # Defaults shared by all missions.
    mission = {
        "type": "observe",
        "title": "Детектив деталей",
        "text": (
            "Найди три детали, которые отличают эту остановку от предыдущей, "
            "и выбери одну, которую хочется запомнить."
        ),
        "tip": "Это может быть цвет, форма, надпись, звук или устройство пространства.",
        "photo": "Сфотографируй выбранную деталь — это будет трофей этой остановки.",
        "xp": 20,
        "phrase": None,
        "bonus": None,
    }

    if group in {"heritage", "temple"}:
        mission.update(
            {
                "type": "symbol",
                "title": "Охота на китайский символ",
                "text": (
                    "Осмотрись и попробуй найти любой реально видимый национальный элемент: "
                    "дракона, льва, фонарь, иероглиф, облачный орнамент, красную декоративную деталь "
                    "или необычную форму крыши. Если ни одного из примеров нет — выбери любую деталь, "
                    "которая кажется тебе особенно китайской."
                ),
                "tip": "Не нужно знать значение символа: сначала просто найди и рассмотри его.",
                "photo": "Сфотографируй найденную деталь крупно. Позже из таких кадров получится travel-коллаж.",
                "xp": 30,
            }
        )

    elif group == "park":
        variants = [
            {
                "type": "park",
                "title": "Поймай город",
                "text": (
                    "Найди место, где в одном взгляде встречаются природа и что-то созданное человеком. "
                    "Выбери самый красивый контраст."
                ),
                "tip": "Подойдут дерево + здание, вода + мост, листья + фонарь, тень + дорожка.",
                "photo": "Сделай кадр из двух миров: природа + городская деталь.",
                "xp": 20,
            },
            {
                "type": "compare",
                "title": "Три цвета",
                "text": (
                    "Найди три цвета, которые чаще всего встречаются вокруг. "
                    "Выбери один цвет как «цвет этого места»."
                ),
                "tip": "Смотри не только на растения: учитывай здания, одежду, вывески и дорожки.",
                "photo": "Собери кадр, в котором выбранный цвет заметен минимум дважды.",
                "xp": 20,
            },
            {
                "type": "photo",
                "title": "Рамка внутри кадра",
                "text": (
                    "Найди естественную рамку для фотографии: ветви, ворота, арку, проём "
                    "или любые две детали по краям."
                ),
                "tip": "Если готовой рамки нет — создай её из двух объектов по краям кадра.",
                "photo": "Сделай один кадр через найденную «рамку».",
                "xp": 30 if photo_interest else 20,
            },
        ]
        mission.update(variants[index % len(variants)])

        if tradition_interest and index % 2 == 0:
            mission["text"] += (
                " Если увидишь традиционный элемент — фонарь, крышу, каллиграфию, ворота или павильон — "
                "попробуй включить его в наблюдение."
            )

    elif group == "tea":
        mission.update(
            {
                "type": "tea",
                "title": "Два аромата",
                "text": (
                    "Если сотрудники могут показать или дать понюхать два чая, сравни их аромат: "
                    "какой кажется более травянистым, цветочным, ореховым или просто приятным тебе? "
                    "Если такой возможности нет, сравни два напитка по названию, описанию или фотографии."
                ),
                "tip": "Покупать напиток не обязательно.",
                "photo": "Сфотографируй вывеску, меню, упаковку или чашку — только то, что удобно и разрешено.",
                "xp": 30,
                "phrase": phrase_block(
                    "我可以闻一下吗？",
                    "Wǒ kěyǐ wén yíxià ma?",
                    "Во кэ-и вэнь и-ся ма?",
                    "Можно понюхать?",
                ),
            }
        )

    elif group in {"restaurant", "cafe"}:
        mission.update(
            {
                "type": "food",
                "title": "Местный выбор",
                "text": (
                    "Найди в меню две позиции, которые тебе незнакомы или кажутся необычными. "
                    "Выбери одну, которую ты бы попробовал первой. Покупать её не обязательно."
                ),
                "tip": "Можно ориентироваться на фотографии, английские слова или переводчик в телефоне.",
                "photo": "Сохрани фото названия блюда, меню, вывески или подачи — если это уместно и разрешено.",
                "xp": 20,
                "phrase": phrase_block(
                    "你推荐什么？",
                    "Nǐ tuījiàn shénme?",
                    "Ни туэйцзень шэньмэ?",
                    "Что вы рекомендуете?",
                ),
            }
        )

    elif group == "market":
        mission.update(
            {
                "type": "market",
                "title": "Что здесь самое необычное?",
                "text": (
                    "Найди три вещи, блюда, упаковки или вывески, которых обычно не видишь дома. "
                    "Выбери одну как главный «трофей улицы»."
                ),
                "tip": "Не фотографируй людей крупным планом без разрешения.",
                "photo": "Сфотографируй выбранный трофей или его название.",
                "xp": 20,
                "phrase": phrase_block(
                    "这个是什么？",
                    "Zhège shì shénme?",
                    "Чжэгэ ши шэньмэ?",
                    "Что это?",
                ),
            }
        )

    elif group == "museum":
        mission.update(
            {
                "type": "museum",
                "title": "Один вопрос",
                "text": (
                    "Если музей открыт и фотографировать можно, найди предмет или изображение, "
                    "о котором тебе захотелось бы узнать больше, и придумай к нему один вопрос. "
                    "Если внутрь не заходишь — выбери интересную деталь входной зоны или фасада."
                ),
                "tip": "Ответ искать не обязательно — хороший вопрос уже считается результатом.",
                "photo": "Если съёмка разрешена — сохрани объект; иначе сфотографируй фасад или название музея.",
                "xp": 30,
            }
        )

    elif group in {"art", "viewpoint"}:
        mission.update(
            {
                "type": "art",
                "title": "Два ракурса",
                "text": (
                    "Посмотри на место с двух разных точек или дистанций. "
                    "Реши, какой ракурс делает его интереснее и почему."
                ),
                "tip": "Сравни линии, масштаб, свет и то, что попадает на передний план.",
                "photo": "Сделай лучший из двух кадров.",
                "xp": 30 if photo_interest else 20,
            }
        )

    mission["minutes"] = 15 if mission.get("xp", 20) >= 30 else 12

    # Photo is a core mechanic even when not selected.
    if photo_interest and mission["type"] not in {"tea", "food"}:
        mission["photo"] += " Попробуй сделать кадр не как туристическую открытку, а со своим необычным ракурсом."

    return mission


def optional_social_bonus(place: dict) -> dict | None:
    if not is_outdoor_social_place(place):
        return None
    return {
        "text": (
            "Если тебе комфортно, попроси прохожего сфотографировать тебя. "
            "Если не хочется обращаться к людям — сделай селфи, это тоже засчитывается."
        ),
        "xp": 10,
        "phrase": phrase_block(
            "请帮我拍张照片，可以吗？",
            "Qǐng bāng wǒ pāi zhāng zhàopiàn, kěyǐ ma?",
            "Цин бан во пай чжан чжаопьен, кэ-и ма?",
            "Можете меня сфотографировать, пожалуйста?",
        ),
    }


def build_quest(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
    ai_meta: dict | None,
) -> dict:
    ai_meta = ai_meta or {}
    names = ai_meta.get("friendly_names") if isinstance(ai_meta.get("friendly_names"), list) else []
    reasons = ai_meta.get("reasons") if isinstance(ai_meta.get("reasons"), list) else []

    stops = []
    social_used = False

    for i, place in enumerate(places):
        ai_name = str(names[i]).strip() if i < len(names) else ""
        name_ru = safe_russian_name(place, ai_name)
        reason = (
            str(reasons[i]).strip()
            if i < len(reasons) and str(reasons[i]).strip()
            else "Эта точка добавляет в маршрут другой тип впечатления и подходит под выбранные интересы."
        )

        mission = mission_for_place(place, interests, style, i)
        bonus = None
        if style == "adventure" and not social_used and is_outdoor_social_place(place) and i >= 1:
            bonus = optional_social_bonus(place)
            social_used = bool(bonus)

        stops.append(
            {
                "place": place,
                "name_ru": name_ru,
                "why_here": reason,
                "mission": mission,
                "bonus": bonus,
            }
        )

    title = str(ai_meta.get("title") or "").strip() or f"CityQuest · {city.get('input_name')}"
    intro = str(ai_meta.get("intro") or "").strip() or (
        "Маршрут по реальным точкам города: ищи детали, собирай фото-трофеи и отмечай выполненные миссии."
    )
    final_challenge = str(ai_meta.get("final_challenge") or "").strip() or (
        "Выбери лучший кадр квеста и придумай ему короткое название."
    )

    return {
        "title": title,
        "intro": intro,
        "stops": stops,
        "final_challenge": final_challenge,
    }


def total_possible_xp(quest: dict) -> int:
    total = 0
    for stop in quest.get("stops", []):
        total += int(stop["mission"].get("xp", 20))
        if stop.get("bonus"):
            total += int(stop["bonus"].get("xp", 10))
    return total


def earned_xp(quest: dict, completed: list[int], bonuses: list[int]) -> int:
    completed_set, bonus_set = set(completed), set(bonuses)
    total = 0
    for i, stop in enumerate(quest.get("stops", [])):
        if i in completed_set:
            total += int(stop["mission"].get("xp", 20))
        if i in bonus_set and stop.get("bonus"):
            total += int(stop["bonus"].get("xp", 10))
    return total


def checklist_text(quest: dict, completed: list[int], bonuses: list[int], photos: dict) -> str:
    completed_set = set(completed)
    lines = [
        "✅ <b>Чек-лист квеста</b>",
        "",
        "Нажимай на название миссии, когда выполнишь её.",
        "",
    ]

    for i, stop in enumerate(quest.get("stops", [])):
        mark = "✅" if i in completed_set else "☐"
        photo_mark = " 📷" if str(i) in photos else ""
        lines.append(f"{mark} <b>{i + 1}.</b> {esc(stop['name_ru'])}{photo_mark}")

    total = len(quest.get("stops", []))
    xp = earned_xp(quest, completed, bonuses)
    lines.extend(
        [
            "",
            ("🟩" * len(completed_set)) + ("⬜" * max(0, total - len(completed_set))),
            f"Прогресс: <b>{len(completed_set)}/{total}</b>",
            f"⭐ XP: <b>{xp}/{total_possible_xp(quest)}</b>",
            f"📷 Фото: <b>{len(photos)}/{total}</b>",
        ]
    )
    return "\n".join(lines)


def checklist_keyboard(quest: dict, completed: list[int]):
    completed_set = set(completed)
    kb = InlineKeyboardBuilder()
    for i, stop in enumerate(quest.get("stops", [])):
        mark = "✅" if i in completed_set else "☐"
        kb.button(
            text=f"{mark} {i + 1} · {short_text(stop['name_ru'], 30)}",
            callback_data=f"mission_toggle:{i}",
        )
    kb.adjust(1)

    if quest.get("stops") and len(completed_set) == len(quest["stops"]):
        kb.row(InlineKeyboardButton(text="🏁 Завершить квест", callback_data="quest_finish"))
    return kb.as_markup()


def stop_keyboard(place: dict, index: int, has_bonus: bool):
    kb = InlineKeyboardBuilder()
    lat, lon = place["lat"], place["lon"]
    kb.row(
        InlineKeyboardButton(
            text="📍 Открыть точку на карте",
            url=f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}",
        )
    )
    kb.row(
        InlineKeyboardButton(text="📷 Добавить фото", callback_data=f"photo_add:{index}"),
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"mission_toggle:{index}"),
    )
    if has_bonus:
        kb.row(
            InlineKeyboardButton(text="🎁 Засчитать бонус +10 XP", callback_data=f"bonus_toggle:{index}")
        )
    return kb.as_markup()


def phrase_text(phrase: dict | None) -> str:
    if not phrase:
        return ""
    return (
        f"\n\n🇨🇳 <b>{esc(phrase['hanzi'])}</b>\n"
        f"🔤 <i>{esc(phrase['pinyin'])}</i>\n"
        f"🗣 <b>Примерно:</b> {esc(phrase['ru'])}\n"
        f"💬 {esc(phrase['translation'])}"
    )


def route_summary(route: dict, quest: dict, duration: str) -> str:
    walk = route["time_s"] / 60
    missions = sum(int(s["mission"].get("minutes", 14)) for s in quest["stops"])
    total_window = DURATION_MINUTES[duration]
    pause = max(15, int(total_window * 0.12))
    reserve = max(0, total_window - walk - missions - pause)

    return (
        "🗺 <b>Маршрут проверен</b>\n"
        f"🚶 Пешком: ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(walk)}\n"
        f"🎯 Миссии: ~{fmt_minutes(missions)}\n"
        f"☕ Паузы: ~{fmt_minutes(pause)}\n"
        f"🕒 Запас: ~{fmt_minutes(reserve)}"
    )


async def ask_city(message: Message, state: FSMContext):
    await state.set_state(QuestForm.waiting_city)
    await message.answer(
        "🧭 <b>Новый CityQuest</b>\n\n"
        "Напиши город Китая. Можно по-китайски или по-английски.\n\n"
        "Например: 成都 · 西安 · Hangzhou"
    )


async def show_interests(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(QuestForm.choosing_interests)
    await message.answer(
        "✨ <b>Что тебе интересно?</b>\n\n"
        "Выбери от 1 до 3 тем. Фото используется в квесте в любом случае; "
        "выбор «Фото» просто сделает задания более композиционными.",
        reply_markup=interests_keyboard(data.get("interests", [])),
    )


async def send_quest(message: Message, state: FSMContext):
    data = await state.get_data()
    quest = data["quest"]
    route = data["route"]
    duration = data["duration"]
    city = data["city"]
    style = data["style"]

    await message.answer(
        f"🏮 <b>{esc(quest['title'])}</b>\n\n"
        f"{esc(quest['intro'])}\n\n"
        f"📍 {esc(city.get('input_name'))} · ⏱ {esc(duration)} · {esc(STYLE_LABELS[style])}\n\n"
        f"{route_summary(route, quest, duration)}\n\n"
        "🤖 <i>ИИ оформляет квест, а факты, маршрут и игровые механики ограничены проверенными данными.</i>"
    )

    legs = route.get("legs", [])

    for i, stop in enumerate(quest["stops"]):
        place = stop["place"]
        transition = ""
        if i > 0 and i - 1 < len(legs):
            leg = legs[i - 1]
            transition = (
                f"\n🚶 <b>От предыдущей:</b> ~{fmt_distance(leg['distance_m'])} · "
                f"~{fmt_minutes(leg['time_s'] / 60)}\n"
            )

        mission = stop["mission"]
        bonus_text = ""
        if stop.get("bonus"):
            b = stop["bonus"]
            bonus_text = (
                f"\n\n🎁 <b>BONUS +{b['xp']} XP</b>\n{esc(b['text'])}"
                f"{phrase_text(b.get('phrase'))}"
            )

        await message.answer(
            f"📍 <b>{i + 1}/{len(quest['stops'])}. {esc(stop['name_ru'])}</b>\n"
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
            f"{bonus_text}",
            reply_markup=stop_keyboard(place, i, bool(stop.get("bonus"))),
        )

    completed, bonuses, photos = [], [], {}
    await state.update_data(completed=completed, bonuses=bonuses, photos=photos)
    await state.set_state(QuestForm.quest_active)
    await message.answer(
        checklist_text(quest, completed, bonuses, photos),
        reply_markup=checklist_keyboard(quest, completed),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    name = esc(message.from_user.first_name if message.from_user else "путешественник")
    await message.answer(
        f"🏮 <b>CityQuest China 城市奇遇</b>\n\n"
        f"Привет, {name}!\n\n"
        "Я превращаю прогулку по реальному китайскому городу в персональный AI-квест: "
        "места, пеший маршрут, миссии, фото-трофеи и XP.\n\n"
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
    if callback.message:
        await ask_city(callback.message, state)


@router.message(QuestForm.waiting_city)
async def city_received(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    status = await message.answer("🗺 Ищу именно город в Китае…")
    try:
        city = await geocode_city(query)
    except Exception:
        logger.exception("City search failed")
        city = None

    if not city:
        await status.edit_text(
            "🤔 Не получилось однозначно найти именно город.\n"
            "Попробуй другое написание, например 西安 или Xi'an."
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
    if callback.message:
        await callback.message.answer("✏️ Напиши другой город:")


@router.callback_query(F.data == "city_confirm")
async def city_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.city_confirmed)
    if callback.message:
        await callback.message.answer(
            "⏱ <b>Сколько времени у тебя есть?</b>",
            reply_markup=duration_keyboard(),
        )


@router.callback_query(F.data.startswith("duration_"))
async def duration_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mapping = {
        "duration_2": "2 часа",
        "duration_4": "4 часа",
        "duration_6": "6 часов",
        "duration_day": "весь день",
    }
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
    else:
        if len(selected) >= 3:
            await callback.answer("Максимум 3 интереса.", show_alert=True)
            return
        selected.append(key)

    await state.update_data(interests=selected)
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=interests_keyboard(selected))


@router.callback_query(F.data == "interests_continue")
async def interests_continue(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    interests = data.get("interests", [])
    city, duration = data.get("city"), data.get("duration")

    if not interests:
        await callback.answer("Выбери хотя бы один интерес.", show_alert=True)
        return

    await callback.answer()
    if not callback.message:
        return

    status = await callback.message.answer(
        "🔎 <b>Ищу разные реальные места…</b>\n\n"
        "Собираю кандидатов отдельно по интересам, чтобы маршрут не превратился "
        "в три кафе или четыре одинаковых парка."
    )

    try:
        places = await search_places(city, interests, duration)
    except Exception:
        logger.exception("Places search failed")
        places = []

    if len(places) < 3:
        await status.edit_text("🤔 Нашлось слишком мало подходящих мест. Попробуй изменить интересы.")
        return

    await state.update_data(candidates=places)
    await state.set_state(QuestForm.choosing_style)

    lines = []
    for i, place in enumerate(places[:8], 1):
        lines.append(
            f"{i}. {esc(place['category_label'])} — <b>{esc(place['name'])}</b>"
            + (f"\n   <i>{esc(place['pinyin'])}</i>" if place.get("pinyin") else "")
        )

    await status.edit_text(
        f"📍 <b>Кандидаты: {len(places)}</b>\n\n"
        + "\n".join(lines)
        + "\n\nПосле выбора стиля бот проверит разнообразие, расстояния и реальный пеший маршрут.\n\n"
        "🎯 <b>Как будем исследовать город?</b>",
        reply_markup=style_keyboard(),
    )


@router.callback_query(F.data.startswith("style:"))
async def style_cb(callback: CallbackQuery, state: FSMContext):
    style = callback.data.split(":", 1)[1]
    data = await state.get_data()
    city = data.get("city")
    duration = data.get("duration")
    interests = data.get("interests", [])
    candidates = data.get("candidates", [])

    await callback.answer()
    if not callback.message:
        return

    await state.set_state(QuestForm.generating)
    status = await callback.message.answer(
        "🧭 <b>Собираю маршрут…</b>\n\n"
        "Не ставлю гастро-точки подряд, ограничиваю одинаковые места "
        "и проверяю реальные пешие переходы."
    )

    try:
        selected, route = await select_route(candidates, interests, duration)
    except Exception:
        logger.exception("Route selection failed")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🗺 Для этого набора пока не удалось собрать хороший компактный маршрут.\n"
            "Попробуй другой набор интересов или больше времени.",
            reply_markup=style_keyboard(),
        )
        return

    await status.edit_text(
        f"🤖 <b>Маршрут готов.</b>\n\n"
        f"Пешком ~{fmt_distance(route['distance_m'])} · ~{fmt_minutes(route['time_s']/60)}.\n"
        "ИИ оформляет историю, а миссии собираются из проверенного игрового движка."
    )

    ai_meta = {}
    try:
        ai_meta = await groq_meta(city, duration, interests, style, selected)
        logger.info("Groq meta success")
    except Exception:
        logger.exception("Groq meta unavailable; local fallback used")

    quest = build_quest(city, duration, interests, style, selected, ai_meta)
    await state.update_data(quest=quest, route=route, style=style)
    await status.edit_text("✅ <b>Квест готов!</b>")
    await send_quest(callback.message, state)


@router.callback_query(F.data.startswith("mission_toggle:"))
async def mission_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quest = data.get("quest")
    if not quest:
        await callback.answer("Активный квест не найден.", show_alert=True)
        return

    idx = int(callback.data.split(":", 1)[1])
    completed = list(data.get("completed", []))
    if idx in completed:
        completed.remove(idx)
        text = "Отметка снята"
    else:
        completed.append(idx)
        text = "Миссия выполнена ✅"

    completed = sorted(set(completed))
    await state.update_data(completed=completed)
    await callback.answer(text)

    # If this callback came from a stop card, do not overwrite that card with checklist.
    # Send/update a fresh compact checklist instead.
    if callback.message:
        data = await state.get_data()
        await callback.message.answer(
            checklist_text(
                quest,
                completed,
                data.get("bonuses", []),
                data.get("photos", {}),
            ),
            reply_markup=checklist_keyboard(quest, completed),
        )


@router.callback_query(F.data.startswith("bonus_toggle:"))
async def bonus_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quest = data.get("quest")
    idx = int(callback.data.split(":", 1)[1])

    if not quest or not quest["stops"][idx].get("bonus"):
        await callback.answer("Для этой точки бонуса нет.", show_alert=True)
        return

    bonuses = list(data.get("bonuses", []))
    if idx in bonuses:
        bonuses.remove(idx)
        msg = "Бонус снят"
    else:
        bonuses.append(idx)
        msg = "Бонус +10 XP засчитан 🎁"

    bonuses = sorted(set(bonuses))
    await state.update_data(bonuses=bonuses)
    await callback.answer(msg)


@router.callback_query(F.data.startswith("photo_add:"))
async def photo_add(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    quest = data.get("quest")

    if not quest or idx < 0 or idx >= len(quest["stops"]):
        await callback.answer("Точка не найдена.", show_alert=True)
        return

    await state.update_data(photo_target=idx)
    await state.set_state(QuestForm.waiting_photo)
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            f"📷 <b>Фото для миссии {idx + 1}: {esc(quest['stops'][idx]['name_ru'])}</b>\n\n"
            "Отправь фотографию следующим сообщением. Я привяжу её к этой точке.\n"
            "Для отмены: /cancelphoto"
        )


@router.message(Command("cancelphoto"))
async def cancel_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("quest"):
        await state.set_state(QuestForm.quest_active)
        await state.update_data(photo_target=None)
        await message.answer("Загрузка фото отменена.")
    else:
        await state.clear()


@router.message(QuestForm.waiting_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    quest = data.get("quest")
    idx = data.get("photo_target")

    if not quest or idx is None:
        await state.set_state(QuestForm.quest_active)
        await message.answer("Не удалось определить миссию для фото.")
        return

    photos = dict(data.get("photos", {}))
    file_id = message.photo[-1].file_id
    photos[str(idx)] = file_id

    await state.update_data(photos=photos, photo_target=None)
    await state.set_state(QuestForm.quest_active)

    await message.answer(
        f"📷 Фото сохранено для миссии <b>{idx + 1}. {esc(quest['stops'][idx]['name_ru'])}</b>.\n\n"
        "Позже из фото квеста можно будет собрать итоговый travel-коллаж."
    )


@router.message(QuestForm.waiting_photo)
async def receive_not_photo(message: Message):
    await message.answer("Пришли именно фотографию или используй /cancelphoto.")


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

    bonuses = data.get("bonuses", [])
    photos = data.get("photos", {})
    xp = earned_xp(quest, list(completed), bonuses)

    await callback.answer()
    await state.set_state(QuestForm.quest_finished)

    if callback.message:
        await callback.message.answer(
            "🏆 <b>CityQuest завершён!</b>\n\n"
            f"✅ Миссии: <b>{len(completed)}/{len(quest['stops'])}</b>\n"
            f"⭐ XP: <b>{xp}/{total_possible_xp(quest)}</b>\n"
            f"📷 Фото-трофеи: <b>{len(photos)}</b>\n\n"
            f"🎁 <b>Финальный штрих:</b> {esc(quest['final_challenge'])}\n\n"
            "Следующий большой этап — собрать эти фотографии в красивую открытку-коллаж."
        )


@router.callback_query(F.data == "my_quests")
async def my_quests(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎒 <b>Мои приключения</b>\n\n"
            "Пока активный квест хранится в памяти бота. Постоянную историю и галерею подключим отдельным этапом."
        )


@router.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "ℹ️ <b>CityQuest China</b>\n\n"
            "• Geoapify проверяет город и реальные места.\n"
            "• Routing API проверяет пешие расстояния.\n"
            "• Mission Engine не даёт маршруту превратиться в ряд одинаковых кафе или парков.\n"
            "• Groq AI оформляет историю, но не выдумывает факты о местах.\n"
            "• Фото можно прикреплять к миссиям прямо в Telegram."
        )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Starting CityQuest China Mission Engine v2")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
