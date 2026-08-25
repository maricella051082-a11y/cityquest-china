import asyncio
import html
import itertools
import json
import logging
import math
import os
import re

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
    quest_finished = State()


INTERESTS = {
    "history": {
        "label": "🏯 История",
        "categories": [
            "tourism.sights",
            "heritage",
            "entertainment.museum",
        ],
    },
    "tea": {
        "label": "🍵 Чай",
        "categories": [
            "catering.cafe.tea",
            "catering.cafe",
        ],
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
        ],
    },
    "nature": {
        "label": "🌿 Природа",
        "categories": [
            "leisure.park",
            "leisure.park.garden",
            "natural",
        ],
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
            "tourism.sights.place_of_worship.temple",
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

REST_CATEGORIES = [
    "catering.cafe.tea",
    "catering.cafe",
]

STYLE_LABELS = {
    "calm": "😌 Спокойно",
    "explorer": "🔎 Исследователь",
    "adventure": "🔥 Приключение",
}

STYLE_INSTRUCTIONS = {
    "calm": (
        "Спокойный стиль: наблюдение, атмосфера, красивые детали, "
        "минимум спешки и никаких обязательных разговоров с незнакомцами."
    ),
    "explorer": (
        "Стиль исследователя: сравнивать, замечать детали, делать маленькие открытия, "
        "искать особенности места."
    ),
    "adventure": (
        "Приключенческий стиль: разные игровые задания, поиск деталей, маленькие вызовы "
        "и один необязательный социальный бонус, но без риска и без необходимости знать китайский."
    ),
}

MISSION_TYPE_LABELS = {
    "photo": "📸 Фото",
    "observe": "👀 Наблюдение",
    "compare": "🔎 Сравнение",
    "tradition": "🏮 Традиционная деталь",
    "taste_smell": "🍵 Вкус / аромат",
    "choice": "🎯 Личный выбор",
    "social_bonus": "🤝 Общение — только по желанию",
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
    "весь день": 60,
}


def esc(value) -> str:
    return html.escape(str(value or ""))


def short_text(value: str, max_len: int = 25) -> str:
    value = str(value or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def contains_cyrillic(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", str(text or "")))


def clean_category_ru(label: str) -> str:
    """Remove leading emoji from a Russian category label."""
    value = str(label or "").strip()
    value = re.sub(r"^[^\wА-Яа-яЁё]+\s*", "", value)
    return value or "интересное место"


def russian_place_label(stop: dict) -> str:
    """
    Short human-readable Russian label for checklist/cards.
    Priority:
    1) AI-provided Russian name;
    2) safe heuristic from the verified POI name;
    3) verified Geoapify Russian category.
    """
    friendly = str(stop.get("friendly_name") or "").strip()
    if friendly and contains_cyrillic(friendly):
        return friendly

    place = stop.get("place") or {}
    original = str(place.get("name") or "").strip()

    # Common Chinese POI names/suffixes. These are UI labels, not historical claims.
    exact_map = {
        "天府广场": "Площадь Тяньфу",
        "钟楼": "Колокольная башня",
        "鼓楼": "Барабанная башня",
        "西安鼓楼博物馆": "Музей Барабанной башни Сианя",
        "西安钟楼": "Колокольная башня Сианя",
        "西安城墙": "Городская стена Сианя",
        "大雁塔": "Большая пагода диких гусей",
        "小雁塔": "Малая пагода диких гусей",
        "星巴克": "Starbucks",
        "喜茶": "Чайная HEYTEA",
    }
    if original in exact_map:
        return exact_map[original]

    # English names: keep recognizable brand/name, but add a Russian type if useful.
    lower = original.casefold()
    if original and not contains_han(original):
        if "market" in lower:
            return "Рынок"
        if "museum" in lower:
            return "Музей"
        if "park" in lower:
            return "Парк"
        if "temple" in lower:
            return "Храм"
        if "cafe" in lower or "coffee" in lower:
            return "Кафе"
        if "tea" in lower:
            return "Чайная"
        # Brand or known Latin name is readable even without Chinese.
        return original

    # Chinese suffix/type heuristics.
    if original:
        if original.endswith("广场"):
            base = original[:-2]
            if base:
                return f"Площадь {place.get('pinyin') or base}"
            return "Площадь"
        if original.endswith("博物馆"):
            return "Музей"
        if original.endswith("公园"):
            return "Парк"
        if original.endswith("花园") or original.endswith("园"):
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
        if "咖啡" in original:
            return "Кафе"
        if original.endswith("街"):
            return "Улица / квартал"

    return clean_category_ru(place.get("category_label") or "интересное место")


def bilingual_stop_label(stop: dict, max_len: int = 52) -> str:
    """
    Checklist label: Russian first; original only when it adds useful orientation.
    Examples:
      Колокольная башня · 钟楼
      Рынок · Muslim Street Food Market
    """
    place = stop.get("place") or {}
    original = str(place.get("name") or "").strip()
    russian = russian_place_label(stop)

    if original and original.casefold() != russian.casefold():
        value = f"{russian} · {original}"
    else:
        value = russian

    return short_text(value, max_len)


def fmt_minutes(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} ч"
    return f"{hours} ч {mins} мин"


def fmt_distance(meters: float) -> str:
    if meters < 1000:
        return f"{int(round(meters / 10) * 10)} м"
    return f"{meters / 1000:.1f} км"


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
    selected_set = set(selected or [])
    kb = InlineKeyboardBuilder()

    for key, meta in INTERESTS.items():
        prefix = "✅ " if key in selected_set else ""
        kb.button(
            text=f"{prefix}{meta['label']}",
            callback_data=f"interest:{key}",
        )

    kb.adjust(2)
    markup = kb.as_markup()

    if selected_set:
        markup.inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"Продолжить ({len(selected_set)}/3) →",
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


def checklist_keyboard(quest: dict, completed: list[int]):
    completed_set = set(completed)
    kb = InlineKeyboardBuilder()

    for index, stop in enumerate(quest.get("stops", [])):
        done = index in completed_set
        label = bilingual_stop_label(stop, 44)
        kb.button(
            text=f"{'✅' if done else '☐'} {index + 1} · {label}",
            callback_data=f"mission_toggle:{index}",
        )

    kb.adjust(1)

    if quest.get("stops") and len(completed_set) == len(quest["stops"]):
        kb.row(
            InlineKeyboardButton(
                text="🏁 Завершить квест",
                callback_data="quest_finish",
            )
        )

    return kb.as_markup()


def checklist_text(quest: dict, completed: list[int]) -> str:
    completed_set = set(completed)
    total = len(quest.get("stops", []))
    lines = [
        "✅ <b>Чек-лист квеста</b>",
        "",
        "Нажимай на название миссии, когда выполнишь её.",
        "",
    ]

    for index, stop in enumerate(quest.get("stops", [])):
        mark = "✅" if index in completed_set else "☐"
        lines.append(
            f"{mark} <b>{index + 1}.</b> "
            f"{esc(bilingual_stop_label(stop, 62))}"
        )

    earned_xp = sum(
        int(stop.get("xp", 20))
        for idx, stop in enumerate(quest.get("stops", []))
        if idx in completed_set
    )
    total_xp = sum(int(stop.get("xp", 20)) for stop in quest.get("stops", []))

    lines.extend(
        [
            "",
            ("🟩" * len(completed_set)) + ("⬜" * max(0, total - len(completed_set))),
            f"Прогресс: <b>{len(completed_set)}/{total}</b>",
            f"⭐ XP: <b>{earned_xp}/{total_xp}</b>",
        ]
    )
    return "\n".join(lines)


def contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def chinese_syllables(text: str) -> list[str]:
    return [
        part
        for part in lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
        if part
    ]


def chinese_to_pinyin(text: str) -> str:
    return "".join(chinese_syllables(text)).replace(" ", "").strip().lower()


def city_query_variants(text: str) -> list[str]:
    original = text.strip()
    variants = [original]

    if contains_han(original):
        bases = [original]
        stripped = re.sub(r"[市县区]$", "", original)
        if stripped and stripped != original:
            bases.append(stripped)

        for base in bases:
            syllables = chinese_syllables(base)
            if not syllables:
                continue
            variants.extend(
                [
                    "".join(syllables),
                    " ".join(syllables),
                    "'".join(syllables),
                ]
            )

    unique = []
    seen = set()

    for item in variants:
        for query in (item, f"{item}, China"):
            key = query.casefold().strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(query.strip())

    return unique


def place_name_pinyin(text: str) -> str:
    if not contains_han(text):
        return ""

    parts = lazy_pinyin(
        text,
        style=Style.TONE,
        errors=lambda chars: [chars],
    )
    value = " ".join(part for part in parts if part).strip()
    if value:
        value = value[0].upper() + value[1:]
    return value


def place_category_label(categories: list[str]) -> str:
    cats = categories or []

    def has(prefix: str) -> bool:
        return any(cat == prefix or cat.startswith(prefix + ".") for cat in cats)

    if has("catering.cafe.tea"):
        return "🍵 чайная"
    if has("catering.restaurant"):
        return "🍜 ресторан"
    if has("catering.food_court"):
        return "🍽 фуд-корт"
    if has("catering.fast_food"):
        return "🥡 кафе / фастфуд"
    if has("catering.cafe"):
        return "☕ кафе"
    if has("commercial.marketplace"):
        return "🛍 рынок"
    if has("entertainment.museum"):
        return "🏛 музей"
    if has("tourism.sights.place_of_worship") or has("religion"):
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


async def geo_search_city(
    session: aiohttp.ClientSession,
    query: str,
) -> list[dict]:
    url = "https://api.geoapify.com/v1/geocode/search"
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
            body = await response.text()
            logger.error("Geoapify city search failed: %s %s", response.status, body[:800])
            raise RuntimeError("Geoapify request failed")

        data = await response.json()
        return data.get("results", [])


def city_rank(item: dict) -> tuple:
    rank = item.get("rank") or {}
    return (
        float(rank.get("confidence_city_level") or 0),
        float(rank.get("confidence") or 0),
        float(rank.get("popularity") or 0),
        float(rank.get("importance") or 0),
    )


def choose_city(results: list[dict]) -> dict | None:
    valid = []

    for item in results:
        country_code = (item.get("country_code") or "").lower()
        result_type = (item.get("result_type") or "").lower()

        if country_code != "cn":
            continue

        # Critical guard: a county/postcode/district is NOT accepted as a city.
        if result_type != "city":
            continue

        valid.append(item)

    if not valid:
        return None

    return max(valid, key=city_rank)


async def geocode_city(city_query: str) -> dict | None:
    original = city_query.strip()
    variants = city_query_variants(original)

    logger.info("City input %r variants=%r", original, variants[:8])

    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for query in variants:
            logger.info("Geoapify strict city lookup query=%r", query)
            results = await geo_search_city(session, query)
            item = choose_city(results)

            if item:
                logger.info(
                    "Strict city match %r -> %r (%s)",
                    original,
                    item.get("formatted"),
                    item.get("result_type"),
                )

                return {
                    "place_id": item.get("place_id"),
                    "formatted": item.get("formatted") or original,
                    "city": (
                        item.get("city")
                        or item.get("town")
                        or item.get("village")
                        or original
                    ),
                    "state": item.get("state"),
                    "country": item.get("country") or "China",
                    "lat": item.get("lat"),
                    "lon": item.get("lon"),
                    "result_type": item.get("result_type"),
                    "input_name": original,
                    "search_name": query,
                }

    return None


async def fetch_places_for_source(
    session: aiohttp.ClientSession,
    city: dict,
    source_key: str,
    categories: list[str],
    limit: int = 18,
) -> list[dict]:
    url = "https://api.geoapify.com/v2/places"

    if city.get("place_id"):
        spatial_filter = f"place:{city['place_id']}"
    else:
        spatial_filter = f"circle:{city['lon']},{city['lat']},15000"

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
            body = await response.text()
            logger.error(
                "Geoapify Places failed for %s: %s %s",
                source_key,
                response.status,
                body[:800],
            )
            return []

        data = await response.json()

    output = []

    for feature in data.get("features", []):
        props = feature.get("properties", {})

        # Do not use address/postcode as a fake POI name.
        name = (props.get("name") or "").strip()
        if not name:
            continue
        if re.match(r"^\d{5,6}(?:\s|$)", name):
            continue

        lat = props.get("lat")
        lon = props.get("lon")
        if lat is None or lon is None:
            continue

        raw_categories = props.get("categories") or []

        output.append(
            {
                "place_id": props.get("place_id") or "",
                "name": name,
                "pinyin": place_name_pinyin(name),
                "category_label": place_category_label(raw_categories),
                "formatted": props.get("formatted") or "",
                "categories": raw_categories,
                "lat": float(lat),
                "lon": float(lon),
                "distance": props.get("distance"),
                "interest_matches": [source_key],
            }
        )

    return output


async def search_places(
    city: dict,
    interests: list[str],
    duration: str,
) -> list[dict]:
    sources = [
        (key, INTERESTS[key]["categories"])
        for key in interests
        if key in INTERESTS
    ]

    # For 4+ hours, allow one cafe/tea stop as a rest point even when tea was not selected.
    if duration != "2 часа" and "tea" not in interests:
        sources.append(("rest", REST_CATEGORIES))

    timeout = aiohttp.ClientTimeout(total=35)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            fetch_places_for_source(session, city, source_key, categories)
            for source_key, categories in sources
        ]
        source_results = await asyncio.gather(*tasks)

    merged = {}
    source_order = {key: [] for key, _ in sources}

    for (source_key, _), places in zip(sources, source_results):
        for place in places:
            key = place.get("place_id") or (
                f"{place['name'].casefold()}:{place['lat']:.5f}:{place['lon']:.5f}"
            )

            if key in merged:
                if source_key not in merged[key]["interest_matches"]:
                    merged[key]["interest_matches"].append(source_key)
            else:
                merged[key] = place

            if key not in source_order[source_key]:
                source_order[source_key].append(key)

    # Round-robin keeps the pool balanced across the selected interests.
    balanced_keys = []
    seen = set()

    for position in range(12):
        for source_key, _ in sources:
            source_list = source_order.get(source_key, [])
            if position >= len(source_list):
                continue

            key = source_list[position]
            if key in seen:
                continue

            seen.add(key)
            balanced_keys.append(key)

            if len(balanced_keys) >= 24:
                break

        if len(balanced_keys) >= 24:
            break

    if len(balanced_keys) < 24:
        for key in merged:
            if key not in seen:
                balanced_keys.append(key)
                seen.add(key)
                if len(balanced_keys) >= 24:
                    break

    return [merged[key] for key in balanced_keys]


def haversine_meters(a: dict, b: dict) -> float:
    radius = 6371000.0
    lat1 = math.radians(a["lat"])
    lat2 = math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])

    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(h))


def greedy_best_order(combo: list[dict]) -> tuple[list[dict], float]:
    if len(combo) <= 1:
        return combo[:], 0.0

    best_order = None
    best_distance = float("inf")

    for start in range(len(combo)):
        remaining = set(range(len(combo)))
        remaining.remove(start)
        order_indexes = [start]
        current = start
        total = 0.0

        while remaining:
            nxt = min(
                remaining,
                key=lambda idx: haversine_meters(combo[current], combo[idx]),
            )
            total += haversine_meters(combo[current], combo[nxt])
            order_indexes.append(nxt)
            remaining.remove(nxt)
            current = nxt

        if total < best_distance:
            best_distance = total
            best_order = order_indexes

    return [combo[i] for i in best_order], best_distance


def stop_count_for_duration(duration: str) -> int:
    return {
        "2 часа": 3,
        "4 часа": 4,
        "6 часов": 5,
        "весь день": 6,
    }.get(duration, 4)


def combination_is_diverse(
    combo: list[dict],
    selected_interests: list[str],
    available_interests: set[str],
) -> bool:
    covered = set()

    for place in combo:
        covered.update(
            key
            for key in place.get("interest_matches", [])
            if key in selected_interests
        )

    if not available_interests.issubset(covered):
        return False

    labels = [place.get("category_label") for place in combo]
    counts = {}

    for label in labels:
        counts[label] = counts.get(label, 0) + 1

    if any(count > 2 for count in counts.values()):
        return False

    unique_labels = len(set(labels))
    if len(combo) >= 4 and unique_labels < 3:
        return False
    if len(combo) == 3 and unique_labels < 2:
        return False

    if sum("rest" in place.get("interest_matches", []) for place in combo) > 1:
        return False

    return True


async def calculate_walking_route(stops: list[dict]) -> dict:
    if len(stops) < 2:
        return {
            "distance_m": 0,
            "time_s": 0,
            "legs": [],
        }

    url = "https://api.geoapify.com/v1/routing"
    waypoints = "|".join(
        f"{place['lat']},{place['lon']}"
        for place in stops
    )

    params = {
        "waypoints": waypoints,
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
                logger.error("Geoapify Routing failed: %s %s", response.status, body[:800])
                raise RuntimeError("Routing API failed")

            data = json.loads(body)

    results = data.get("results") or []
    if not results:
        raise RuntimeError("Routing API returned no route")

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


def route_fits_duration(route: dict, duration: str, stop_count: int) -> bool:
    total_minutes = DURATION_MINUTES[duration]
    walk_minutes = route["time_s"] / 60
    assumed_missions = 12 * stop_count
    buffer_minutes = max(15, round(total_minutes * 0.12))
    max_walk_minutes = total_minutes * 0.45
    max_leg_minutes = MAX_LEG_MINUTES[duration]

    if walk_minutes > max_walk_minutes:
        return False

    if walk_minutes + assumed_missions + buffer_minutes > total_minutes:
        return False

    if route.get("legs"):
        if max((leg["time_s"] / 60 for leg in route["legs"]), default=0) > max_leg_minutes:
            return False

    return True


async def select_compact_route(
    places: list[dict],
    interests: list[str],
    duration: str,
) -> tuple[list[dict], dict]:
    needed = stop_count_for_duration(duration)

    if len(places) < needed:
        raise RuntimeError("Too few candidate places")

    pool = places[:20]

    available_interests = {
        interest
        for interest in interests
        if any(interest in p.get("interest_matches", []) for p in pool)
    }

    scored = []

    for indexes in itertools.combinations(range(len(pool)), needed):
        combo = [pool[i] for i in indexes]

        if not combination_is_diverse(
            combo,
            interests,
            available_interests,
        ):
            continue

        order, approx_distance = greedy_best_order(combo)

        # Slight penalty for repeated categories, so equally compact but more varied routes win.
        category_count = len({p.get("category_label") for p in combo})
        penalty = (needed - category_count) * 350
        scored.append((approx_distance + penalty, order))

    if not scored:
        logger.warning("No strict diversified combination; relaxing category diversity")

        for indexes in itertools.combinations(range(len(pool)), needed):
            combo = [pool[i] for i in indexes]

            covered = set()
            for place in combo:
                covered.update(
                    key
                    for key in place.get("interest_matches", [])
                    if key in interests
                )

            if not available_interests.issubset(covered):
                continue

            order, approx_distance = greedy_best_order(combo)
            scored.append((approx_distance, order))

    if not scored:
        raise RuntimeError("No suitable combinations")

    scored.sort(key=lambda item: item[0])

    # Call the real routing API only for the best geographic candidates.
    best_fallback = None

    for _, ordered in scored[:8]:
        try:
            route = await calculate_walking_route(ordered)
        except RuntimeError:
            continue

        logger.info(
            "Route candidate: %.1f km, %.1f min, categories=%s",
            route["distance_m"] / 1000,
            route["time_s"] / 60,
            [p.get("category_label") for p in ordered],
        )

        if best_fallback is None or route["time_s"] < best_fallback[1]["time_s"]:
            best_fallback = (ordered, route)

        if route_fits_duration(route, duration, needed):
            return ordered, route

    if best_fallback:
        ordered, route = best_fallback

        # Do not silently return a wildly unrealistic route.
        if route["time_s"] / 60 <= DURATION_MINUTES[duration] * 0.55:
            return ordered, route

    raise RuntimeError("No realistic route fits the selected duration")



def category_key(place: dict) -> str:
    label = (place.get("category_label") or "").lower()

    if "чайная" in label:
        return "tea"
    if "кафе" in label or "ресторан" in label or "фуд" in label:
        return "cafe"
    if "музей" in label:
        return "museum"
    if "храм" in label or "религиоз" in label:
        return "temple"
    if "ворота" in label:
        return "heritage"
    if "истор" in label or "достопримеч" in label:
        return "heritage"
    if "сад" in label:
        return "garden"
    if "парк" in label or "природ" in label:
        return "park"
    if "арт" in label or "культур" in label:
        return "art"
    if "рынок" in label:
        return "market"
    if "смотров" in label:
        return "viewpoint"

    return "general"


def mission_blueprint(
    place: dict,
    selected_interests: list[str],
    style: str,
    index: int,
) -> dict:
    """Safe mission that never assumes an unverified object exists at the POI."""
    kind = category_key(place)
    tradition_selected = "tradition" in selected_interests
    photo_selected = "photo" in selected_interests

    # Rotate mechanics so a route does not become four identical photo tasks.
    rotation = index % 3

    if kind in {"park", "garden"}:
        if tradition_selected and rotation == 0:
            return {
                "mission_type": "tradition",
                "mission": (
                    "Осмотрись вокруг. Если увидишь элемент, который кажется традиционным — "
                    "например, форму крыши, фонарь, ворота, каллиграфию или павильон — "
                    "выбери самый интересный. Если ничего такого нет, выбери деталь, "
                    "которая лучше всего передаёт характер этого места."
                ),
                "tip": "Ничего специально искать по карте не нужно — работай только с тем, что реально видишь вокруг.",
                "xp": 30,
            }
        if photo_selected or rotation == 1:
            return {
                "mission_type": "photo",
                "mission": (
                    "Собери кадр из трёх слоёв: что-то природное на переднем плане, "
                    "пространство парка в середине и любую заметную деталь вдали. "
                    "Сделай один кадр, который лучше всего передаёт атмосферу места."
                ),
                "tip": "Не ищи конкретный объект — выбери любые реально видимые элементы.",
                "xp": 20,
            }
        return {
            "mission_type": "compare",
            "mission": (
                "Найди две разные фактуры или формы в этом месте — например, природную и созданную человеком. "
                "Сравни их и выбери, какая сильнее задаёт настроение парку."
            ),
            "tip": "Подойдут листья, камень, вода, дорожка, ограда, здание или любой другой реально видимый элемент.",
            "xp": 20,
        }

    if kind == "tea":
        return {
            "mission_type": "taste_smell",
            "mission": (
                "Миссия «Два аромата»: если сотрудники могут показать или дать понюхать два чая, "
                "сравни их аромат и выбери более травянистый, цветочный, ореховый или просто более приятный тебе. "
                "Если такой возможности нет, сравни два названия или описания в меню и выбери, какой напиток ты бы попробовал."
            ),
            "tip": "Покупать напиток не обязательно.",
            "xp": 30,
            "phrase": "我可以闻一下吗？",
            "pinyin": "Wǒ kěyǐ wén yíxià ma?",
            "ru_pronunciation": "Во кэ-и вэнь и-ся ма?",
            "translation": "Можно понюхать?",
        }

    if kind == "cafe":
        return {
            "mission_type": "choice",
            "mission": (
                "Выбери две позиции или два вкусовых сочетания, которые видишь в меню. "
                "Не покупая ничего, реши, какое кажется тебе более необычным для этой прогулки, и запомни свой выбор."
            ),
            "tip": "Если меню непонятно, ориентируйся на фотографии, английские слова или переводчик в телефоне.",
            "xp": 20,
        }

    if kind in {"heritage", "temple"}:
        return {
            "mission_type": "tradition",
            "mission": (
                "Найди один реально видимый декоративный элемент: узор, цветовое сочетание, "
                "форму крыши, ворота, надпись или другой архитектурный штрих. "
                "Выбери тот, который тебе хочется рассмотреть дольше всего."
            ),
            "tip": "Тебе не нужно знать его значение. Задача — заметить форму, цвет или ритм.",
            "xp": 30,
        }

    if kind == "museum":
        return {
            "mission_type": "observe",
            "mission": (
                "Если музей открыт, найди один предмет или изображение, о котором тебе захотелось бы узнать больше, "
                "и сформулируй один вопрос к нему. Если музей закрыт или ты не заходишь внутрь — "
                "сделай то же самое с любой деталью фасада или входной зоны."
            ),
            "tip": "Ответ искать не обязательно — хороший вопрос уже считается выполнением миссии.",
            "xp": 30,
        }

    if kind == "market":
        return {
            "mission_type": "observe",
            "mission": (
                "Найди три повторяющихся цвета, формы или типа упаковки/вывесок. "
                "Выбери один визуальный мотив, который сильнее всего отличает это место от привычных тебе магазинов."
            ),
            "tip": "Ничего покупать и фотографировать людей без разрешения не нужно.",
            "xp": 20,
        }

    if kind in {"art", "viewpoint"}:
        return {
            "mission_type": "photo" if photo_selected else "compare",
            "mission": (
                "Посмотри на место с двух разных точек или дистанций. "
                "Выбери ракурс, в котором оно кажется наиболее выразительным; если хочется — сделай один кадр."
            ),
            "tip": "Сравнивай композицию, линии и пространство, а не ищи заранее заданный объект.",
            "xp": 20,
        }

    # Universal fallback, safe for any named POI.
    return {
        "mission_type": "observe",
        "mission": (
            "За две минуты найди три детали, которые отличают это место от предыдущей остановки. "
            "Выбери одну и коротко сформулируй, почему именно она запомнилась."
        ),
        "tip": "Это могут быть цвет, звук, форма, запах, движение людей или устройство пространства.",
        "xp": 20,
    }


def social_bonus_for_adventure() -> dict:
    return {
        "bonus": (
            "Если тебе комфортно, попроси прохожего сфотографировать тебя. "
            "Если не хочется обращаться к людям — просто сделай селфи."
        ),
        "chinese_phrase": "请帮我拍张照片，可以吗？",
        "pinyin": "Qǐng bāng wǒ pāi zhāng zhàopiàn, kěyǐ ma?",
        "ru_pronunciation": "Цин бан во пай чжан чжаопьен, кэ-и ма?",
        "phrase_translation": "Можете меня сфотографировать, пожалуйста?",
    }


def build_safe_quest(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
    ai_meta: dict | None = None,
) -> dict:
    """Deterministic, safe quest. AI may supply only atmosphere/title/reasons."""
    ai_meta = ai_meta or {}
    stops = []
    social_added = False

    ai_reasons = ai_meta.get("reasons") or []
    ai_names = ai_meta.get("friendly_names") or []

    for index, place in enumerate(places):
        blueprint = mission_blueprint(place, interests, style, index)

        stop = {
            "poi_index": index + 1,
            "place": place,
            "friendly_name": (
                str(ai_names[index]).strip()
                if (
                    index < len(ai_names)
                    and str(ai_names[index]).strip()
                    and contains_cyrillic(str(ai_names[index]))
                )
                else clean_category_ru(place.get("category_label") or "интересное место")
            ),
            "why_here": (
                str(ai_reasons[index]).strip()
                if index < len(ai_reasons) and str(ai_reasons[index]).strip()
                else "Эта точка подходит под выбранные интересы и компактно входит в маршрут."
            ),
            "mission_type": blueprint["mission_type"],
            "mission": blueprint["mission"],
            "tip": blueprint["tip"],
            "mission_minutes": 12 if blueprint["xp"] == 20 else 15,
            "xp": blueprint["xp"],
            "bonus": "",
            "chinese_phrase": blueprint.get("phrase", ""),
            "phrase_pinyin": blueprint.get("pinyin", ""),
            "ru_pronunciation": blueprint.get("ru_pronunciation", ""),
            "phrase_translation": blueprint.get("translation", ""),
        }

        # Add exactly one optional social bonus in Adventure mode.
        if style == "adventure" and not social_added and index >= 1:
            social = social_bonus_for_adventure()
            stop.update(
                {
                    "bonus": social["bonus"],
                    "chinese_phrase": social["chinese_phrase"],
                    "phrase_pinyin": social["pinyin"],
                    "ru_pronunciation": social["ru_pronunciation"],
                    "phrase_translation": social["phrase_translation"],
                }
            )
            social_added = True

        stops.append(stop)

    title = str(ai_meta.get("title") or f"CityQuest · {city.get('input_name') or city.get('city')}")
    intro = str(
        ai_meta.get("intro")
        or "Небольшое приключение по реальным точкам города: наблюдай, сравнивай и собирай свои впечатления."
    )
    final_challenge = str(
        ai_meta.get("final_challenge")
        or "Выбери одну фотографию или одну деталь маршрута, которая лучше всего запомнилась, и дай ей своё название."
    )

    return {
        "title": title,
        "intro": intro,
        "stops": stops,
        "final_challenge": final_challenge,
    }


AI_META_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "friendly_names": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
        },
        "final_challenge": {"type": "string"},
    },
    "required": ["title", "intro", "friendly_names", "reasons", "final_challenge"],
    "additionalProperties": False,
}


QUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "stops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "poi_index": {"type": "integer"},
                    "friendly_name": {"type": "string"},
                    "why_here": {"type": "string"},
                    "mission_type": {
                        "type": "string",
                        "enum": [
                            "photo",
                            "observe",
                            "compare",
                            "tradition",
                            "taste_smell",
                            "choice",
                            "social_bonus",
                        ],
                    },
                    "mission": {"type": "string"},
                    "tip": {"type": "string"},
                    "mission_minutes": {"type": "integer"},
                    "bonus": {"type": "string"},
                    "chinese_phrase": {"type": "string"},
                    "phrase_translation": {"type": "string"},
                },
                "required": [
                    "poi_index",
                    "friendly_name",
                    "why_here",
                    "mission_type",
                    "mission",
                    "tip",
                    "mission_minutes",
                    "bonus",
                    "chinese_phrase",
                    "phrase_translation",
                ],
                "additionalProperties": False,
            },
        },
        "final_challenge": {"type": "string"},
    },
    "required": ["title", "intro", "stops", "final_challenge"],
    "additionalProperties": False,
}


def build_ai_meta_prompt(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
    route: dict,
) -> str:
    interest_text = ", ".join(INTERESTS[key]["label"] for key in interests)
    place_lines = []

    for index, place in enumerate(places, start=1):
        place_lines.append(
            f"{index}. name={place['name']}; "
            f"pinyin={place.get('pinyin') or '-'}; "
            f"type={place.get('category_label') or 'место'}"
        )

    return f"""
Ты создаёшь только атмосферную оболочку для готового CityQuest.
Миссии уже сформированы безопасными шаблонами в коде — НЕ придумывай задания,
предметы, статуи, часы, старые карты, экспонаты, архитектурные детали, меню или услуги.

ГОРОД: {city.get('input_name') or city.get('city')}
ВРЕМЯ: {duration}
ИНТЕРЕСЫ: {interest_text}
СТИЛЬ: {STYLE_LABELS.get(style, style)}

Нужно вернуть:
- title: короткое цепляющее название квеста;
- intro: 1–2 предложения;
- friendly_names: по одному КОРОТКОМУ названию на русском для каждой точки;
- reasons: по одной короткой фразе «почему здесь» для каждой точки, используя ТОЛЬКО тип места и выбранные интересы;
- final_challenge: финальная рефлексия/фото-задание без необходимости искать внешний материал.

Строго:
- friendly_names и reasons должны иметь ровно {len(places)} элементов;
- friendly_names ОБЯЗАТЕЛЬНО должны содержать короткое понятное русское название для туриста;
- примеры хорошего формата: «Площадь Тяньфу», «Колокольная башня», «Чайная», «Храм», «Сад», «Рынок»;
- если точный перевод названия неизвестен, дай безопасное русское описание по типу места
  (например «историческое место», «парк», «чайная», «рынок»), не выдумывай перевод;
- нельзя писать, что на месте точно есть башня, часы, статуя, старинная карта, конкретный аромат,
  конкретное дерево или традиционная деталь, если этого нет во входных данных;
- нельзя требовать от пользователя заранее подготовленных материалов;
- язык русский, понятно человеку без знания китайского.

ТОЧКИ:
{chr(10).join(place_lines)}
""".strip()


async def call_groq_meta(prompt: str) -> dict:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    schema_payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — редактор CityQuest China. Ты не создаёшь миссии и не выдумываешь факты о местах. "
                    "Ты только даёшь название, короткое вступление, понятные названия точек и причины выбора."
                ),
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

    json_payload = {
        "model": GROQ_MODEL,
        "messages": schema_payload["messages"]
        + [
            {
                "role": "system",
                "content": (
                    "Если строгая схема недоступна, верни только JSON-объект с ключами "
                    "title, intro, friendly_names, reasons, final_challenge."
                ),
            }
        ],
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
    }

    timeout = aiohttp.ClientTimeout(total=75)
    last_error = None

    # First: strict schema, with retries for transient errors.
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=schema_payload) as response:
                    body = await response.text()
                    if response.status == 200:
                        data = json.loads(body)
                        return json.loads(data["choices"][0]["message"]["content"])

                    last_error = RuntimeError(f"Groq strict HTTP {response.status}: {body[:600]}")
                    logger.error("Groq strict failed: %s %s", response.status, body[:600])

                    # 400 can be model/schema compatibility; go to JSON fallback.
                    if response.status == 400:
                        break
        except Exception as exc:
            last_error = exc
            logger.exception("Groq strict exception")

        if attempt < 2:
            await asyncio.sleep(2 + attempt)

    # Second: plain JSON object mode.
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=json_payload) as response:
                    body = await response.text()
                    if response.status == 200:
                        data = json.loads(body)
                        return json.loads(data["choices"][0]["message"]["content"])

                    last_error = RuntimeError(f"Groq JSON HTTP {response.status}: {body[:600]}")
                    logger.error("Groq JSON fallback failed: %s %s", response.status, body[:600])
        except Exception as exc:
            last_error = exc
            logger.exception("Groq JSON fallback exception")

        if attempt == 0:
            await asyncio.sleep(2)

    raise RuntimeError("Groq meta generation failed") from last_error


def normalize_ai_meta(ai_data: dict, place_count: int) -> dict:
    friendly_names = ai_data.get("friendly_names") or []
    reasons = ai_data.get("reasons") or []

    if not isinstance(friendly_names, list):
        friendly_names = []
    if not isinstance(reasons, list):
        reasons = []

    # We may use partial meta safely; missing elements fall back locally.
    validated_names = []
    for item in friendly_names[:place_count]:
        value = str(item).strip()
        validated_names.append(value if contains_cyrillic(value) else "")

    return {
        "title": str(ai_data.get("title") or "").strip(),
        "intro": str(ai_data.get("intro") or "").strip(),
        "friendly_names": validated_names,
        "reasons": [str(x).strip() for x in reasons[:place_count]],
        "final_challenge": str(ai_data.get("final_challenge") or "").strip(),
    }


async def generate_ai_quest(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
    route: dict,
) -> dict:
    # AI enriches the quest, but it is no longer allowed to invent the actual missions.
    ai_meta = {}

    try:
        prompt = build_ai_meta_prompt(
            city,
            duration,
            interests,
            style,
            places,
            route,
        )
        raw_meta = await call_groq_meta(prompt)
        ai_meta = normalize_ai_meta(raw_meta, len(places))
        logger.info("AI meta generated successfully")
    except Exception:
        # Critical reliability feature: the bot still returns a valid quest when Groq is unavailable.
        logger.exception("AI meta failed; using safe local quest fallback")

    return build_safe_quest(
        city=city,
        duration=duration,
        interests=interests,
        style=style,
        places=places,
        ai_meta=ai_meta,
    )



def route_summary_text(
    route: dict,
    quest: dict,
    duration: str,
) -> str:
    walking_minutes = route["time_s"] / 60
    mission_minutes = sum(
        stop.get("mission_minutes", 0)
        for stop in quest.get("stops", [])
    )

    total_window = DURATION_MINUTES[duration]
    planned_break = max(15, round(total_window * 0.12))
    active_total = walking_minutes + mission_minutes + planned_break
    reserve = max(0, total_window - active_total)

    return (
        "🗺 <b>Проверка маршрута</b>\n"
        f"🚶 Пешком: ~{fmt_distance(route['distance_m'])} · "
        f"~{fmt_minutes(walking_minutes)}\n"
        f"🎯 Задания: ~{fmt_minutes(mission_minutes)}\n"
        f"☕ Отдых / паузы: ~{fmt_minutes(planned_break)}\n"
        f"🕒 Свободный запас: ~{fmt_minutes(reserve)}"
    )


def map_url(place: dict) -> str:
    lat = place["lat"]
    lon = place["lon"]
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}"


def mission_map_keyboard(place: dict):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📍 Открыть точку на карте",
            url=map_url(place),
        )
    )
    return kb.as_markup()


async def ask_for_city(message: Message, state: FSMContext):
    await state.set_state(QuestForm.waiting_city)
    await message.answer(
        "🧭 <b>Новый CityQuest</b>\n\n"
        "Напиши город Китая, который хочешь исследовать.\n\n"
        "Можно написать по-китайски или по-английски.\n\n"
        "Например:\n"
        "• 成都\n"
        "• 西安\n"
        "• Hangzhou"
    )


async def show_interests(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("interests", [])
    await state.set_state(QuestForm.choosing_interests)

    await message.answer(
        "✨ <b>Что тебе интересно?</b>\n\n"
        "Выбери от 1 до 3 тем. Можно нажимать повторно, чтобы снять выбор.",
        reply_markup=interests_keyboard(selected),
    )


async def send_generated_quest(
    message: Message,
    state: FSMContext,
):
    data = await state.get_data()
    quest = data["generated_quest"]
    route = data["route_info"]
    city = data.get("city", {})
    duration = data.get("duration", "")
    style = data.get("quest_style", "")

    await message.answer(
        f"🏮 <b>{esc(quest['title'])}</b>\n\n"
        f"{esc(quest['intro'])}\n\n"
        f"📍 {esc(city.get('input_name') or city.get('city'))} · "
        f"⏱ {esc(duration)} · {esc(STYLE_LABELS.get(style, style))}\n\n"
        f"{route_summary_text(route, quest, duration)}\n\n"
        "🤖 <i>Точки проверены Geoapify, пешие переходы — Geoapify Routing, "
        "миссии созданы ИИ.</i>"
    )

    legs = route.get("legs", [])

    for index, stop in enumerate(quest["stops"]):
        place = stop["place"]
        pinyin_line = (
            f"\n<i>{esc(place.get('pinyin'))}</i>"
            if place.get("pinyin")
            else ""
        )

        transition = ""
        if index > 0 and index - 1 < len(legs):
            leg = legs[index - 1]
            transition = (
                f"\n🚶 <b>От предыдущей:</b> ~{fmt_distance(leg['distance_m'])} · "
                f"~{fmt_minutes(leg['time_s'] / 60)}\n"
            )

        bonus_block = ""
        if stop.get("bonus"):
            bonus_block += f"\n\n🎁 <b>Бонус:</b> {esc(stop['bonus'])}"

        if stop.get("chinese_phrase"):
            bonus_block += f"\n🇨🇳 <b>{esc(stop['chinese_phrase'])}</b>"

            if stop.get("phrase_pinyin"):
                bonus_block += f"\n🔤 <i>{esc(stop['phrase_pinyin'])}</i>"

            if stop.get("ru_pronunciation"):
                bonus_block += (
                    f"\n🗣 <b>Если не знаешь китайского, попробуй примерно так:</b> "
                    f"{esc(stop['ru_pronunciation'])}"
                )

            if stop.get("phrase_translation"):
                bonus_block += f"\n💬 {esc(stop['phrase_translation'])}"

        await message.answer(
            f"📍 <b>{index + 1}/{len(quest['stops'])}. "
            f"{esc(russian_place_label(stop))}</b>\n"
            f"{esc(place.get('category_label'))} — <b>{esc(place['name'])}</b>"
            f"{pinyin_line}"
            f"{transition}\n"
            f"💡 <b>Почему здесь:</b> {esc(stop['why_here'])}\n\n"
            f"{esc(MISSION_TYPE_LABELS.get(stop['mission_type'], '🎯 Миссия'))}\n"
            f"🎯 <b>Миссия:</b> {esc(stop['mission'])}\n\n"
            f"🧭 <b>Подсказка:</b> {esc(stop['tip'])}\n"
            f"⏱ На миссию: ~{stop['mission_minutes']} мин\n"
            f"⭐ Награда: <b>+{stop.get('xp', 20)} XP</b>"
            f"{bonus_block}",
            reply_markup=mission_map_keyboard(place),
        )

    completed = []
    await state.update_data(completed_missions=completed)
    await state.set_state(QuestForm.quest_active)

    await message.answer(
        checklist_text(quest, completed),
        reply_markup=checklist_keyboard(quest, completed),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    first_name = esc(
        message.from_user.first_name
        if message.from_user
        else "путешественник"
    )

    await message.answer(
        f"🏮 <b>CityQuest China 城市奇遇</b>\n\n"
        f"Привет, {first_name}!\n\n"
        "Я превращаю прогулки по городам Китая в персональные квесты. "
        "Выбери город, время и интересы — а затем выполняй миссии прямо в Telegram.\n\n"
        "С чего начнём?",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🏮 <b>CityQuest China</b>\n\n"
        "/start — главное меню\n"
        "/newquest — создать новый квест\n"
        "/cancel — отменить текущий выбор\n"
        "/help — помощь"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Текущий квест отменён.",
        reply_markup=main_menu(),
    )


@router.message(Command("newquest"))
async def cmd_newquest(message: Message, state: FSMContext):
    await state.clear()
    await ask_for_city(message, state)


@router.callback_query(F.data == "new_quest")
async def cb_new_quest(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()

    if callback.message:
        await ask_for_city(callback.message, state)


@router.message(QuestForm.waiting_city)
async def process_city(message: Message, state: FSMContext):
    city_query = (message.text or "").strip()

    if len(city_query) < 2:
        await message.answer("Напиши название города чуть подробнее.")
        return

    status_message = await message.answer(
        "🗺 Ищу именно город в Китае…"
    )

    try:
        city = await geocode_city(city_query)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
        logger.exception("Geoapify city error")
        await status_message.edit_text(
            "🗺 Карта сейчас не ответила.\n\n"
            "Попробуй ещё раз через несколько секунд."
        )
        return

    if not city:
        await status_message.edit_text(
            "🤔 Не получилось однозначно найти именно город в Китае.\n\n"
            "Попробуй другое написание — например, 西安 или Xi'an."
        )
        return

    await state.update_data(city=city, interests=[])

    state_line = (
        f"\nПровинция / регион: <b>{esc(city['state'])}</b>"
        if city.get("state")
        else ""
    )

    coords = ""
    if city.get("lat") is not None and city.get("lon") is not None:
        coords = f"\n📍 {city['lat']:.5f}, {city['lon']:.5f}"

    input_name = city["input_name"]
    formatted = city["formatted"]

    if input_name.casefold() not in formatted.casefold():
        place_line = f"<b>{esc(input_name)}</b> · {esc(formatted)}"
    else:
        place_line = f"<b>{esc(formatted)}</b>"

    await status_message.edit_text(
        "🇨🇳 <b>Нашёл город!</b>\n\n"
        f"{place_line}"
        f"{state_line}"
        f"{coords}\n\n"
        "Это тот город?",
        reply_markup=city_confirmation_keyboard(),
    )


@router.callback_query(F.data == "city_retry")
async def cb_city_retry(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(QuestForm.waiting_city)

    if callback.message:
        await callback.message.answer(
            "✏️ Хорошо. Напиши другой город Китая:"
        )


@router.callback_query(F.data == "city_confirm")
async def cb_city_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    city = data.get("city")

    if not city:
        if callback.message:
            await callback.message.answer(
                "Я потерял данные города. Давай выберем его ещё раз.",
                reply_markup=main_menu(),
            )
        await state.clear()
        return

    await state.set_state(QuestForm.city_confirmed)

    if callback.message:
        await callback.message.answer(
            f"✅ Отлично! Берём <b>{esc(city['input_name'])}</b>.\n\n"
            "⏱ <b>Сколько времени у тебя есть?</b>",
            reply_markup=duration_keyboard(),
        )


@router.callback_query(F.data.startswith("duration_"))
async def cb_duration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    duration_map = {
        "duration_2": "2 часа",
        "duration_4": "4 часа",
        "duration_6": "6 часов",
        "duration_day": "весь день",
    }

    duration = duration_map.get(callback.data, "не указано")
    await state.update_data(duration=duration, interests=[])

    if callback.message:
        await show_interests(callback.message, state)


@router.callback_query(F.data.startswith("interest:"))
async def cb_interest(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]

    if key not in INTERESTS:
        await callback.answer(
            "Неизвестный интерес",
            show_alert=True,
        )
        return

    data = await state.get_data()
    selected = list(data.get("interests", []))

    if key in selected:
        selected.remove(key)
    else:
        if len(selected) >= 3:
            await callback.answer(
                "Можно выбрать максимум 3 интереса.",
                show_alert=True,
            )
            return
        selected.append(key)

    await state.update_data(interests=selected)
    await callback.answer()

    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=interests_keyboard(selected)
        )


@router.callback_query(F.data == "interests_continue")
async def cb_interests_continue(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    interests = list(data.get("interests", []))
    city = data.get("city")
    duration = data.get("duration")

    if not interests:
        await callback.answer(
            "Выбери хотя бы один интерес.",
            show_alert=True,
        )
        return

    if not city or not duration:
        await callback.answer(
            "Данные квеста потерялись. Начни заново.",
            show_alert=True,
        )
        return

    await callback.answer()

    if not callback.message:
        return

    selected_labels = " · ".join(
        INTERESTS[key]["label"]
        for key in interests
    )

    status = await callback.message.answer(
        "🔎 <b>Ищу разные реальные места…</b>\n\n"
        f"{selected_labels}\n"
        "Собираю кандидатов отдельно по каждому интересу."
    )

    try:
        places = await search_places(
            city,
            interests,
            duration,
        )
    except Exception:
        logger.exception("Geoapify Places error")
        await status.edit_text(
            "🗺 Не удалось получить места от карты.\n\n"
            "Попробуй ещё раз чуть позже."
        )
        return

    if len(places) < stop_count_for_duration(duration):
        await status.edit_text(
            "🤔 Для такого набора интересов нашлось слишком мало понятных реальных мест.\n\n"
            "Попробуй изменить один из интересов."
        )
        return

    await state.update_data(
        poi_candidates=places,
    )
    await state.set_state(QuestForm.choosing_style)

    preview = places[:8]
    lines = []

    for index, place in enumerate(preview, start=1):
        source_labels = [
            INTERESTS[key]["label"]
            for key in place.get("interest_matches", [])
            if key in INTERESTS
        ]

        source_note = (
            f" · {' / '.join(source_labels)}"
            if source_labels
            else " · ☕ возможная передышка"
        )

        line = (
            f"{index}. {esc(place['category_label'])} — "
            f"<b>{esc(place['name'])}</b>{source_note}"
        )

        if place.get("pinyin"):
            line += f"\n   <i>{esc(place['pinyin'])}</i>"

        lines.append(line)

    await status.edit_text(
        f"📍 <b>Кандидаты в {esc(city.get('input_name'))}</b>\n\n"
        "Я специально смешал разные типы мест:\n\n"
        + "\n".join(lines)
        + f"\n\nВсего кандидатов: <b>{len(places)}</b>.\n\n"
        "После выбора стиля бот проверит расстояния и соберёт компактный "
        "пеший маршрут — одинаковые парки подряд больше не являются нормой.\n\n"
        "🎯 <b>Как будем исследовать город?</b>",
        reply_markup=style_keyboard(),
    )


@router.callback_query(F.data.startswith("style:"))
async def cb_style(
    callback: CallbackQuery,
    state: FSMContext,
):
    style = callback.data.split(":", 1)[1]

    if style not in STYLE_LABELS:
        await callback.answer(
            "Неизвестный стиль",
            show_alert=True,
        )
        return

    data = await state.get_data()
    city = data.get("city")
    duration = data.get("duration")
    interests = data.get("interests", [])
    candidates = data.get("poi_candidates", [])

    if not city or not duration or not interests or not candidates:
        await callback.answer(
            "Не хватает данных. Начни через /start.",
            show_alert=True,
        )
        return

    await callback.answer()
    await state.update_data(quest_style=style)
    await state.set_state(QuestForm.generating)

    if not callback.message:
        return

    status = await callback.message.answer(
        "🧭 <b>Проверяю маршрут…</b>\n\n"
        "Сравниваю расстояния между реальными точками и проверяю, "
        f"помещается ли прогулка в {esc(duration)}."
    )

    try:
        selected_places, route = await select_compact_route(
            candidates,
            interests,
            duration,
        )
    except Exception:
        logger.exception("Route selection failed")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🗺 Не удалось собрать достаточно компактный маршрут для этого времени.\n\n"
            "Попробуй другой набор интересов или больше времени."
        )
        return

    await status.edit_text(
        "🤖 <b>Маршрут подходит. Теперь работает ИИ…</b>\n\n"
        f"Пешие переходы: ~{fmt_distance(route['distance_m'])}, "
        f"~{fmt_minutes(route['time_s'] / 60)}.\n"
        "ИИ оформляет квест, а задания собираются из проверенных безопасных механик."
    )

    try:
        quest = await generate_ai_quest(
            city,
            duration,
            interests,
            style,
            selected_places,
            route,
        )
    except Exception:
        logger.exception("AI quest generation failed")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🤖 ИИ сейчас не смог собрать квест.\n\n"
            "Попробуй ещё раз через несколько секунд.",
            reply_markup=style_keyboard(),
        )
        return

    await state.update_data(
        generated_quest=quest,
        route_info=route,
        selected_places=selected_places,
    )

    await status.edit_text(
        "✅ <b>Квест готов и маршрут проверен!</b>"
    )

    await send_generated_quest(
        callback.message,
        state,
    )


@router.callback_query(F.data.startswith("mission_toggle:"))
async def cb_mission_toggle(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    quest = data.get("generated_quest")

    if not quest:
        await callback.answer(
            "Квест не найден. Начни новый через /start.",
            show_alert=True,
        )
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return

    total = len(quest["stops"])
    if index < 0 or index >= total:
        await callback.answer()
        return

    completed = list(
        data.get("completed_missions", [])
    )

    if index in completed:
        completed.remove(index)
        message = "Отметка снята"
    else:
        completed.append(index)
        message = "Миссия выполнена ✅"

    completed = sorted(set(completed))
    await state.update_data(
        completed_missions=completed
    )

    await callback.answer(message)

    if callback.message:
        await callback.message.edit_text(
            checklist_text(quest, completed),
            reply_markup=checklist_keyboard(
                quest,
                completed,
            ),
        )


@router.callback_query(F.data == "quest_finish")
async def cb_quest_finish(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    quest = data.get("generated_quest")
    completed = set(
        data.get("completed_missions", [])
    )

    if not quest:
        await callback.answer(
            "Квест не найден.",
            show_alert=True,
        )
        return

    total = len(quest["stops"])

    if len(completed) != total:
        await callback.answer(
            "Сначала отметь все миссии.",
            show_alert=True,
        )
        return

    await callback.answer()
    await state.set_state(QuestForm.quest_finished)

    if callback.message:
        await callback.message.edit_text(
            "🏆 <b>CityQuest завершён!</b>\n\n"
            + ("🟩" * total)
            + f"\nВсе миссии: <b>{total}/{total}</b>\n\n"
            f"🎁 <b>Финальный штрих:</b> "
            f"{esc(quest.get('final_challenge'))}\n\n"
            "Чтобы создать новый квест, нажми /start."
        )


@router.callback_query(F.data == "my_quests")
async def cb_my_quests(callback: CallbackQuery):
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "🎒 <b>Мои приключения</b>\n\n"
            "Историю завершённых квестов сохраним в базе следующим этапом. "
            "Сейчас активный квест и чек-лист работают до перезапуска бота."
        )


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery):
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "ℹ️ <b>Как работает CityQuest China</b>\n\n"
            "1. Geoapify проверяет, что выбран именно город.\n"
            "2. Places API ищет реальные места по твоим интересам.\n"
            "3. Routing API проверяет расстояния и пешее время.\n"
            "4. ИИ через Groq создаёт разные персональные миссии.\n"
            "5. Выполнение отмечается в чек-листе Telegram."
        )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    dp = Dispatcher(
        storage=MemoryStorage()
    )
    dp.include_router(router)

    logger.info(
        "Starting CityQuest China with strict city guard + routing + Groq AI"
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
