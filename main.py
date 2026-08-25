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
        label = short_text(stop.get("friendly_name") or stop["place"]["name"], 28)
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
            f"{esc(short_text(stop.get('friendly_name') or stop['place']['name'], 34))}"
        )

    lines.extend(
        [
            "",
            ("🟩" * len(completed_set)) + ("⬜" * max(0, total - len(completed_set))),
            f"Прогресс: <b>{len(completed_set)}/{total}</b>",
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


def build_groq_prompt(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    stops: list[dict],
    route: dict,
    feedback: str = "",
) -> str:
    poi_lines = []

    for index, place in enumerate(stops, start=1):
        interest_labels = [
            INTERESTS[key]["label"]
            for key in place.get("interest_matches", [])
            if key in INTERESTS
        ]

        poi_lines.append(
            f"{index}. name={place['name']}; "
            f"pinyin={place.get('pinyin') or '-'}; "
            f"type={place.get('category_label')}; "
            f"matches={', '.join(interest_labels) or 'дополнительная остановка'}"
        )

    interest_text = ", ".join(INTERESTS[key]["label"] for key in interests)
    walk_minutes = round(route["time_s"] / 60)
    distance_km = route["distance_m"] / 1000

    social_rule = (
        """
Для стиля «Приключение» добавь ОДИН необязательный социальный бонус на подходящей точке.
Он не должен быть основной миссией. Если бонус — попросить прохожего сфотографировать,
используй ТОЧНО эту фразу:
请帮我拍张照片，可以吗？
и перевод:
«Можете меня сфотографировать, пожалуйста?»
Обязательно добавь альтернативу: «Если не хочется обращаться к людям — сделай селфи».
"""
        if style == "adventure"
        else ""
    )

    return f"""
Создай городской квест по Китаю для туриста, который НЕ обязан знать китайский язык.

ГОРОД: {city.get('input_name') or city.get('city')}
ВРЕМЯ: {duration}
ИНТЕРЕСЫ: {interest_text}
СТИЛЬ: {STYLE_LABELS.get(style, style)}
ОПИСАНИЕ СТИЛЯ: {STYLE_INSTRUCTIONS.get(style, '')}

Маршрут УЖЕ проверен картографическим Routing API:
- пешком примерно {distance_km:.1f} км;
- переходы примерно {walk_minutes} минут.

Ниже дан окончательный список реальных точек УЖЕ В НУЖНОМ ПОРЯДКЕ.
Верни РОВНО {len(stops)} остановок и используй каждую точку РОВНО ОДИН РАЗ.
poi_index должен идти строго 1,2,3... без перестановок.

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Не придумывай новые места.
2. Не выдумывай исторические факты, статуи, здания, экспонаты, виды из окна,
   традиционные элементы или услуги, если этих сведений нет во входных данных.
3. Если миссия связана с традициями, формулируй условно:
   «Если увидишь традиционный элемент — например, крышу, фонарь, каллиграфию,
   ворота или павильон — выбери самый интересный».
   Нельзя утверждать, что конкретный элемент точно есть.
4. Не делай все задания фотографическими.
5. Для 4 и более остановок используй минимум 3 разных mission_type.
6. mission_type=photo — максимум у 2 остановок.
7. Используй разные механики: наблюдение, сравнение, личный выбор, фото,
   поиск традиционной детали, вкус/аромат там, где это уместно.
8. Не требуй покупать еду или напиток. В кафе/чайной можно сделать миссию,
   которую можно выполнить без покупки; покупка только как необязательный вариант.
9. Не требуй знания китайского языка.
10. Никаких опасных действий, закрытых зон, дороги, нарушений правил.
11. Общение с незнакомцами — только как необязательный bonus.
12. friendly_name: короткое русское пояснение или пиньинь.
    Если точный перевод неизвестен — НЕ выдумывай его.
13. why_here, mission, tip — короткие, максимум 2 предложения.
14. mission_minutes — 5–25 минут.
15. bonus, chinese_phrase, phrase_translation можно оставить пустыми строками,
    если они не нужны.

{social_rule}

РЕАЛЬНЫЕ ТОЧКИ:
{chr(10).join(poi_lines)}

{feedback}
""".strip()


async def call_groq(prompt: str) -> dict:
    url = "https://api.groq.com/openai/v1/chat/completions"

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — CityQuest China, дизайнер городских квестов. "
                    "Нельзя выдумывать сведения о реальных местах. "
                    "Возвращай только JSON по заданной схеме."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "cityquest",
                "strict": True,
                "schema": QUEST_SCHEMA,
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=75)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            body = await response.text()

            if response.status != 200:
                logger.error("Groq failed: HTTP %s %s", response.status, body[:800])
                raise RuntimeError("Groq request failed")

            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)


def normalize_ai_quest(
    ai_data: dict,
    places: list[dict],
) -> dict:
    by_index = {}

    valid_types = set(MISSION_TYPE_LABELS)

    for stop in ai_data.get("stops", []):
        try:
            poi_index = int(stop.get("poi_index"))
        except (TypeError, ValueError):
            continue

        if poi_index < 1 or poi_index > len(places):
            continue

        mission_type = str(stop.get("mission_type") or "observe")
        if mission_type not in valid_types:
            mission_type = "observe"

        minutes = stop.get("mission_minutes", 15)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 15
        minutes = max(5, min(25, minutes))

        place = places[poi_index - 1]

        by_index[poi_index] = {
            "poi_index": poi_index,
            "place": place,
            "friendly_name": str(
                stop.get("friendly_name")
                or place.get("pinyin")
                or place["name"]
            ),
            "why_here": str(stop.get("why_here") or ""),
            "mission_type": mission_type,
            "mission": str(stop.get("mission") or ""),
            "tip": str(stop.get("tip") or ""),
            "mission_minutes": minutes,
            "bonus": str(stop.get("bonus") or ""),
            "chinese_phrase": str(stop.get("chinese_phrase") or ""),
            "phrase_translation": str(stop.get("phrase_translation") or ""),
        }

    if set(by_index) != set(range(1, len(places) + 1)):
        raise RuntimeError("AI did not return every route stop")

    stops = [by_index[i] for i in range(1, len(places) + 1)]

    return {
        "title": str(ai_data.get("title") or "CityQuest"),
        "intro": str(ai_data.get("intro") or ""),
        "stops": stops,
        "final_challenge": str(
            ai_data.get("final_challenge")
            or "Выбери лучший момент прогулки."
        ),
    }


def mission_diversity_ok(quest: dict) -> tuple[bool, str]:
    stops = quest.get("stops", [])
    types = [stop.get("mission_type") for stop in stops]

    if len(stops) >= 4 and len(set(types)) < 3:
        return False, "Используй минимум 3 разных типа заданий."

    if types.count("photo") > 2:
        return False, "Слишком много фото-миссий. Фото может быть максимум у двух остановок."

    normalized_missions = [
        re.sub(r"\s+", " ", stop.get("mission", "").casefold()).strip()
        for stop in stops
    ]

    if len(set(normalized_missions)) != len(normalized_missions):
        return False, "Не повторяй одинаковые миссии."

    return True, ""


async def generate_ai_quest(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
    route: dict,
) -> dict:
    feedback = ""
    last_quest = None

    for attempt in range(3):
        prompt = build_groq_prompt(
            city,
            duration,
            interests,
            style,
            places,
            route,
            feedback,
        )

        ai_data = await call_groq(prompt)
        quest = normalize_ai_quest(ai_data, places)
        last_quest = quest

        ok, reason = mission_diversity_ok(quest)
        if ok:
            return quest

        feedback = (
            "\nПРЕДЫДУЩИЙ ВАРИАНТ ОТКЛОНЁН: "
            + reason
            + " Исправь это в новой версии."
        )
        logger.warning("AI mission diversity retry: %s", reason)

    if last_quest:
        return last_quest

    raise RuntimeError("AI generation failed")


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
            bonus_block += (
                f"\n🇨🇳 <b>{esc(stop['chinese_phrase'])}</b>"
                f"\n💬 {esc(stop.get('phrase_translation'))}"
            )

        await message.answer(
            f"📍 <b>{index + 1}/{len(quest['stops'])}. "
            f"{esc(stop['friendly_name'])}</b>\n"
            f"{esc(place.get('category_label'))} — <b>{esc(place['name'])}</b>"
            f"{pinyin_line}"
            f"{transition}\n"
            f"💡 <b>Почему здесь:</b> {esc(stop['why_here'])}\n\n"
            f"{esc(MISSION_TYPE_LABELS.get(stop['mission_type'], '🎯 Миссия'))}\n"
            f"🎯 <b>Миссия:</b> {esc(stop['mission'])}\n\n"
            f"🧭 <b>Подсказка:</b> {esc(stop['tip'])}\n"
            f"⏱ На миссию: ~{stop['mission_minutes']} мин"
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
        "Создаю разные миссии без выдуманных фактов о местах."
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
