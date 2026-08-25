import asyncio
import html
import json
import logging
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
        "Приключенческий стиль: игровые фото-задания, поиск деталей, небольшие вызовы, "
        "но без риска, нарушения правил и необходимости знать китайский."
    ),
}


def esc(value) -> str:
    return html.escape(str(value or ""))


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


def checklist_keyboard(total: int, completed: list[int]):
    completed_set = set(completed)
    kb = InlineKeyboardBuilder()

    for index in range(total):
        done = index in completed_set
        kb.button(
            text=f"{'✅' if done else '☐'} {index + 1}",
            callback_data=f"mission_toggle:{index}",
        )

    kb.adjust(3)

    if total > 0 and len(completed_set) == total:
        kb.row(
            InlineKeyboardButton(
                text="🏁 Завершить квест",
                callback_data="quest_finish",
            )
        )

    return kb.as_markup()


def progress_bar(total: int, completed: list[int]) -> str:
    completed_count = len(set(completed))
    return "🟩" * completed_count + "⬜" * max(0, total - completed_count)


def contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def chinese_to_pinyin(text: str) -> str:
    parts = lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
    return "".join(parts).replace(" ", "").strip().lower()


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


async def geo_search(session: aiohttp.ClientSession, query: str) -> list[dict]:
    url = "https://api.geoapify.com/v1/geocode/search"
    params = {
        "text": query,
        "filter": "countrycode:cn",
        "limit": 5,
        "format": "json",
        "lang": "en",
        "apiKey": GEOAPIFY_API_KEY,
    }

    async with session.get(url, params=params) as response:
        if response.status != 200:
            body = await response.text()
            logger.error("Geoapify geocoding failed: %s %s", response.status, body[:800])
            raise RuntimeError("Geoapify request failed")
        data = await response.json()
        return data.get("results", [])


def choose_city(results: list[dict]) -> dict | None:
    if not results:
        return None

    preferred_types = {"city", "town", "village", "locality", "district"}

    for item in results:
        country_code = (item.get("country_code") or "").lower()
        result_type = (item.get("result_type") or "").lower()
        if country_code == "cn" and result_type in preferred_types:
            return item

    for item in results:
        if (item.get("country_code") or "").lower() == "cn":
            return item

    return None


async def geocode_city(city_query: str) -> dict | None:
    original = city_query.strip()
    queries = [original]

    if contains_han(original):
        pinyin = chinese_to_pinyin(original)
        logger.info("Chinese city input %r converted to pinyin %r", original, pinyin)
        if pinyin and pinyin.casefold() != original.casefold():
            queries = [pinyin, f"{pinyin}, China", original]

    unique_queries = []
    seen = set()
    for query in queries:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            unique_queries.append(query)

    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for query in unique_queries:
            logger.info("Geoapify city lookup query=%r", query)
            results = await geo_search(session, query)
            item = choose_city(results)

            if item:
                logger.info("Geoapify matched %r -> %r", original, item.get("formatted"))
                return {
                    "place_id": item.get("place_id"),
                    "formatted": item.get("formatted") or item.get("address_line1") or original,
                    "city": (
                        item.get("city")
                        or item.get("town")
                        or item.get("village")
                        or item.get("county")
                        or original
                    ),
                    "state": item.get("state"),
                    "country": item.get("country") or "China",
                    "lat": item.get("lat"),
                    "lon": item.get("lon"),
                    "input_name": original,
                    "search_name": query,
                }

    return None


def categories_for_interests(interests: list[str]) -> list[str]:
    categories = []
    seen = set()

    for interest in interests:
        for category in INTERESTS.get(interest, {}).get("categories", []):
            if category not in seen:
                seen.add(category)
                categories.append(category)

    return categories


async def search_places(city: dict, interests: list[str]) -> list[dict]:
    categories = categories_for_interests(interests)
    if not categories:
        return []

    url = "https://api.geoapify.com/v2/places"

    if city.get("place_id"):
        spatial_filter = f"place:{city['place_id']}"
    else:
        spatial_filter = f"circle:{city['lon']},{city['lat']},15000"

    params = {
        "categories": ",".join(categories),
        "filter": spatial_filter,
        "bias": f"proximity:{city['lon']},{city['lat']}",
        "limit": 35,
        "lang": "zh",
        "apiKey": GEOAPIFY_API_KEY,
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                body = await response.text()
                logger.error("Geoapify Places failed: %s %s", response.status, body[:800])
                raise RuntimeError("Geoapify Places request failed")
            data = await response.json()

    candidates = []
    seen = set()

    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = (props.get("name") or props.get("address_line1") or "").strip()

        if not name:
            continue

        place_id = props.get("place_id") or ""
        key = place_id or name.casefold()

        if key in seen:
            continue
        seen.add(key)

        lat = props.get("lat")
        lon = props.get("lon")

        if lat is None or lon is None:
            continue

        raw_categories = props.get("categories") or []

        candidates.append(
            {
                "place_id": place_id,
                "name": name,
                "pinyin": place_name_pinyin(name),
                "category_label": place_category_label(raw_categories),
                "formatted": props.get("formatted") or "",
                "categories": raw_categories,
                "lat": lat,
                "lon": lon,
                "distance": props.get("distance"),
            }
        )

    return candidates


def stop_count_for_duration(duration: str) -> int:
    return {
        "2 часа": 3,
        "4 часа": 4,
        "6 часов": 5,
        "весь день": 6,
    }.get(duration, 4)


def build_groq_prompt(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
) -> str:
    stop_count = stop_count_for_duration(duration)

    poi_lines = []
    for index, place in enumerate(places, start=1):
        poi_lines.append(
            f"{index}. name={place['name']}; "
            f"pinyin={place.get('pinyin') or '-'}; "
            f"type={place.get('category_label') or 'место'}; "
            f"lat={place['lat']}; lon={place['lon']}"
        )

    interest_text = ", ".join(INTERESTS[key]["label"] for key in interests)

    return f"""
Создай персональный городской квест по Китаю для туриста, который НЕ обязан знать китайский язык.

ГОРОД: {city.get('input_name') or city.get('city')}
ВРЕМЯ: {duration}
ИНТЕРЕСЫ: {interest_text}
СТИЛЬ: {STYLE_LABELS.get(style, style)}
ОПИСАНИЕ СТИЛЯ: {STYLE_INSTRUCTIONS.get(style, '')}

Нужно выбрать РОВНО {stop_count} разных точек только из списка POI ниже и расположить их
в приблизительно логичном географическом порядке по координатам, чтобы не делать
очевидных зигзагов. Не заявляй точные расстояния и время в пути: маршрутизация будет
подключена отдельно.

Для каждой выбранной точки:
- poi_index — номер точки из списка, никаких новых мест;
- friendly_name — короткое понятное русское название или транслитерация.
  Если точный перевод названия неизвестен, используй пиньинь и НЕ выдумывай перевод;
- why_here — 1 короткая фраза, почему эта точка подходит под интересы;
- mission — конкретная игровая миссия, которую реально выполнить на месте;
- tip — короткая практическая подсказка;
- mission_minutes — примерное время именно на выполнение миссии, от 5 до 30 минут.

Правила:
1. Нельзя придумывать новые POI или менять их номера.
2. Не выдумывай исторические факты, архитектурные детали, экспонаты или услуги,
   которых нет во входных данных.
3. Миссии должны быть наблюдательными и универсальными: найти интересную деталь,
   сравнить элементы, сделать фото, выбрать любимый вид/аромат/объект и т.п.
4. Не требуй знания китайского языка.
5. Не требуй покупать что-либо. Покупка может быть только необязательным вариантом.
6. Никаких опасных действий, выхода на проезжую часть, проникновения в закрытые зоны,
   нарушения правил, навязчивого общения с незнакомцами.
7. Пиши по-русски, живо, но коротко.
8. intro — максимум 2 предложения.
9. why_here, mission и tip — каждое максимум 2 коротких предложения.
10. final_challenge — приятное финальное задание, которое подводит итог прогулке.

РЕАЛЬНЫЕ POI:
{chr(10).join(poi_lines)}
""".strip()


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
                    "mission": {"type": "string"},
                    "tip": {"type": "string"},
                    "mission_minutes": {"type": "integer"},
                },
                "required": [
                    "poi_index",
                    "friendly_name",
                    "why_here",
                    "mission",
                    "tip",
                    "mission_minutes",
                ],
                "additionalProperties": False,
            },
        },
        "final_challenge": {"type": "string"},
    },
    "required": ["title", "intro", "stops", "final_challenge"],
    "additionalProperties": False,
}


async def call_groq(prompt: str) -> dict:
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты — CityQuest China, методист путешествий и дизайнер городских квестов. "
                    "Строго используй только переданные реальные POI. "
                    "Возвращай ответ только по заданной JSON-схеме."
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

    last_error = None

    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    body = await response.text()

                    if response.status == 200:
                        data = json.loads(body)
                        content = data["choices"][0]["message"]["content"]
                        return json.loads(content)

                    last_error = RuntimeError(
                        f"Groq HTTP {response.status}: {body[:800]}"
                    )
                    logger.error("Groq failed: HTTP %s %s", response.status, body[:800])

                    if response.status not in {429, 500, 502, 503, 504}:
                        break

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.exception("Groq network/JSON error")

        if attempt == 0:
            await asyncio.sleep(2)

    raise RuntimeError("Groq request failed") from last_error


def normalize_ai_quest(ai_data: dict, places: list[dict], desired_count: int) -> dict:
    normalized_stops = []
    used = set()

    for stop in ai_data.get("stops", []):
        try:
            poi_index = int(stop.get("poi_index"))
        except (TypeError, ValueError):
            continue

        if poi_index < 1 or poi_index > len(places):
            continue
        if poi_index in used:
            continue

        used.add(poi_index)
        place = places[poi_index - 1]

        minutes = stop.get("mission_minutes", 15)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 15
        minutes = max(5, min(30, minutes))

        normalized_stops.append(
            {
                "poi_index": poi_index,
                "place": place,
                "friendly_name": str(stop.get("friendly_name") or place.get("pinyin") or place["name"]),
                "why_here": str(stop.get("why_here") or ""),
                "mission": str(stop.get("mission") or ""),
                "tip": str(stop.get("tip") or ""),
                "mission_minutes": minutes,
            }
        )

        if len(normalized_stops) >= desired_count:
            break

    if len(normalized_stops) < 2:
        raise RuntimeError("AI returned too few valid POIs")

    return {
        "title": str(ai_data.get("title") or "CityQuest"),
        "intro": str(ai_data.get("intro") or ""),
        "stops": normalized_stops,
        "final_challenge": str(ai_data.get("final_challenge") or "Выбери лучший момент прогулки."),
    }


async def generate_ai_quest(
    city: dict,
    duration: str,
    interests: list[str],
    style: str,
    places: list[dict],
) -> dict:
    prompt = build_groq_prompt(city, duration, interests, style, places)
    ai_data = await call_groq(prompt)
    return normalize_ai_quest(
        ai_data,
        places,
        stop_count_for_duration(duration),
    )


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


async def send_generated_quest(message: Message, state: FSMContext):
    data = await state.get_data()
    quest = data["generated_quest"]
    city = data.get("city", {})
    duration = data.get("duration", "")
    style = data.get("quest_style", "")

    await message.answer(
        f"🏮 <b>{esc(quest['title'])}</b>\n\n"
        f"{esc(quest['intro'])}\n\n"
        f"📍 {esc(city.get('input_name') or city.get('city'))} · "
        f"⏱ {esc(duration)} · {esc(STYLE_LABELS.get(style, style))}\n\n"
        "🤖 <i>Квест создан ИИ на основе реальных точек Geoapify.</i>"
    )

    for index, stop in enumerate(quest["stops"], start=1):
        place = stop["place"]
        pinyin_line = (
            f"\n<i>{esc(place.get('pinyin'))}</i>"
            if place.get("pinyin")
            else ""
        )

        await message.answer(
            f"📍 <b>{index}/{len(quest['stops'])}. {esc(stop['friendly_name'])}</b>\n"
            f"{esc(place.get('category_label'))} — <b>{esc(place['name'])}</b>"
            f"{pinyin_line}\n\n"
            f"💡 <b>Почему здесь:</b> {esc(stop['why_here'])}\n\n"
            f"🎯 <b>Миссия:</b> {esc(stop['mission'])}\n\n"
            f"🧭 <b>Подсказка:</b> {esc(stop['tip'])}\n"
            f"⏱ На миссию: ~{stop['mission_minutes']} мин"
        )

    completed = []
    await state.update_data(completed_missions=completed)
    await state.set_state(QuestForm.quest_active)

    total = len(quest["stops"])
    await message.answer(
        "✅ <b>Чек-лист квеста</b>\n\n"
        "Отмечай миссии прямо здесь по мере выполнения.\n\n"
        f"{progress_bar(total, completed)}\n"
        f"Прогресс: <b>0/{total}</b>",
        reply_markup=checklist_keyboard(total, completed),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    first_name = esc(message.from_user.first_name if message.from_user else "путешественник")

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
    await message.answer("Текущий квест отменён.", reply_markup=main_menu())


@router.message(Command("newquest"))
async def cmd_newquest(message: Message, state: FSMContext):
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

    status_message = await message.answer("🗺 Ищу город на карте Китая…")

    try:
        city = await geocode_city(city_query)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
        logger.exception("Geoapify geocoding error")
        await status_message.edit_text(
            "🗺 Карта сейчас не ответила.\n\nПопробуй ещё раз через несколько секунд."
        )
        return

    if not city:
        await status_message.edit_text(
            "🤔 Не получилось найти этот город в Китае.\n\nПопробуй другое написание."
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
        "🇨🇳 <b>Нашёл!</b>\n\n"
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
        await callback.message.answer("✏️ Хорошо. Напиши другой город Китая:")


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
        await callback.answer("Неизвестный интерес", show_alert=True)
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
async def cb_interests_continue(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    interests = list(data.get("interests", []))
    city = data.get("city")

    if not interests:
        await callback.answer("Выбери хотя бы один интерес.", show_alert=True)
        return

    if not city:
        await callback.answer("Город потерялся. Начни квест заново.", show_alert=True)
        return

    await callback.answer()

    if not callback.message:
        return

    selected_labels = " · ".join(INTERESTS[key]["label"] for key in interests)

    status = await callback.message.answer(
        "🔎 <b>Ищу реальные места…</b>\n\n"
        f"{selected_labels}\n"
        "Проверяю точки внутри выбранного города."
    )

    try:
        places = await search_places(city, interests)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
        logger.exception("Geoapify Places error")
        await status.edit_text(
            "🗺 Не удалось получить места от карты.\n\n"
            "Попробуй ещё раз чуть позже."
        )
        return

    if len(places) < 3:
        await status.edit_text(
            "🤔 Я нашёл слишком мало подходящих мест для хорошего квеста.\n\n"
            "Попробуй выбрать более широкие интересы — например, "
            "«История», «Фото» или «Необычное»."
        )
        return

    await state.update_data(poi_candidates=places[:20])
    await state.set_state(QuestForm.choosing_style)

    preview = places[:6]
    lines = []

    for index, place in enumerate(preview, start=1):
        line = (
            f"{index}. {esc(place.get('category_label', '📍 место'))} — "
            f"<b>{esc(place['name'])}</b>"
        )
        if place.get("pinyin"):
            line += f"\n   <i>{esc(place['pinyin'])}</i>"
        lines.append(line)

    city_label = city.get("input_name") or city.get("city") or "город"

    await status.edit_text(
        f"📍 <b>Нашёл реальные места в {esc(city_label)}</b>\n\n"
        "Вот несколько примеров:\n\n"
        + "\n".join(lines)
        + f"\n\nВсего подходящих кандидатов: <b>{len(places)}</b>.\n\n"
        "Сейчас ничего выбирать не нужно — это предварительный список. "
        "После выбора стиля ИИ сам выберет лучшие точки для квеста.\n\n"
        "🎯 <b>Как будем исследовать город?</b>",
        reply_markup=style_keyboard(),
    )


@router.callback_query(F.data.startswith("style:"))
async def cb_style(callback: CallbackQuery, state: FSMContext):
    style = callback.data.split(":", 1)[1]

    if style not in STYLE_LABELS:
        await callback.answer("Неизвестный стиль", show_alert=True)
        return

    current_state = await state.get_state()
    if current_state == QuestForm.generating.state:
        await callback.answer("Квест уже создаётся 🤖", show_alert=True)
        return

    data = await state.get_data()
    city = data.get("city")
    duration = data.get("duration")
    interests = data.get("interests", [])
    places = data.get("poi_candidates", [])

    if not city or not duration or not interests or len(places) < 2:
        await callback.answer(
            "Не хватает данных. Начни новый квест через /start.",
            show_alert=True,
        )
        return

    await callback.answer()
    await state.update_data(quest_style=style)
    await state.set_state(QuestForm.generating)

    if not callback.message:
        return

    status = await callback.message.answer(
        "🤖 <b>ИИ собирает твой CityQuest…</b>\n\n"
        "Выбираю реальные точки, связываю их с интересами и придумываю миссии."
    )

    try:
        quest = await generate_ai_quest(
            city=city,
            duration=duration,
            interests=interests,
            style=style,
            places=places,
        )
    except Exception:
        logger.exception("AI quest generation failed")
        await state.set_state(QuestForm.choosing_style)
        await status.edit_text(
            "🤖 ИИ сейчас не смог собрать квест.\n\n"
            "Попробуй ещё раз через несколько секунд или выбери другой стиль.",
            reply_markup=style_keyboard(),
        )
        return

    await state.update_data(generated_quest=quest)
    await status.edit_text("✅ <b>Квест готов!</b>")

    await send_generated_quest(callback.message, state)


@router.callback_query(F.data.startswith("mission_toggle:"))
async def cb_mission_toggle(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quest = data.get("generated_quest")

    if not quest:
        await callback.answer("Квест не найден. Начни новый через /start.", show_alert=True)
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

    completed = list(data.get("completed_missions", []))

    if index in completed:
        completed.remove(index)
    else:
        completed.append(index)

    completed = sorted(set(completed))
    await state.update_data(completed_missions=completed)
    await callback.answer("Готово ✅" if index in completed else "Отметка снята")

    if callback.message:
        await callback.message.edit_text(
            "✅ <b>Чек-лист квеста</b>\n\n"
            "Отмечай миссии прямо здесь по мере выполнения.\n\n"
            f"{progress_bar(total, completed)}\n"
            f"Прогресс: <b>{len(completed)}/{total}</b>",
            reply_markup=checklist_keyboard(total, completed),
        )


@router.callback_query(F.data == "quest_finish")
async def cb_quest_finish(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quest = data.get("generated_quest")
    completed = set(data.get("completed_missions", []))

    if not quest:
        await callback.answer("Квест не найден.", show_alert=True)
        return

    total = len(quest["stops"])

    if len(completed) != total:
        await callback.answer("Сначала отметь все миссии.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(QuestForm.quest_finished)

    if callback.message:
        await callback.message.edit_text(
            "🏆 <b>CityQuest завершён!</b>\n\n"
            f"{progress_bar(total, list(completed))}\n"
            f"Все миссии выполнены: <b>{total}/{total}</b>\n\n"
            f"🎁 <b>Финальный штрих:</b> {esc(quest.get('final_challenge'))}\n\n"
            "Спасибо за приключение! Чтобы создать новый квест, нажми /start."
        )


@router.callback_query(F.data == "my_quests")
async def cb_my_quests(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎒 <b>Мои приключения</b>\n\n"
            "Сохранение истории квестов подключим следующим этапом. "
            "Текущий активный квест и чек-лист уже работают в рамках запущенного бота."
        )


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "ℹ️ <b>Как работает CityQuest China</b>\n\n"
            "1. Ты выбираешь город, время и интересы.\n"
            "2. Geoapify находит реальные места.\n"
            "3. ИИ через Groq выбирает подходящие точки и создаёт персональные миссии.\n"
            "4. Миссии можно отмечать галочками прямо в Telegram."
        )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Starting CityQuest China with Geoapify + Groq AI")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
