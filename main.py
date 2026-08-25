import asyncio
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
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from pypinyin import lazy_pinyin, Style

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEOAPIFY_API_KEY = os.getenv("GEOAPIFY_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not GEOAPIFY_API_KEY:
    raise RuntimeError("GEOAPIFY_API_KEY is not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cityquest")

router = Router()


class QuestForm(StatesGroup):
    waiting_city = State()
    city_confirmed = State()
    choosing_interests = State()
    choosing_style = State()
    ready_to_generate = State()


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

    # Add the continue button manually after the grid.
    if selected_set:
        from aiogram.types import InlineKeyboardButton
        markup.inline_keyboard.append(
            [InlineKeyboardButton(
                text=f"Продолжить ({len(selected_set)}/3) →",
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


def contains_han(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def chinese_to_pinyin(text: str) -> str:
    parts = lazy_pinyin(text, style=Style.NORMAL, errors="ignore")
    return "".join(parts).replace(" ", "").strip().lower()


def place_name_pinyin(text: str) -> str:
    """Readable pinyin for Chinese POI names shown to a non-Chinese-speaking user."""
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
    """Convert Geoapify categories into a short Russian label for the UI."""
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
            logger.error("Geoapify geocoding failed: %s %s", response.status, body)
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
                    "city": item.get("city")
                        or item.get("town")
                        or item.get("village")
                        or item.get("county")
                        or original,
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

    # Prefer exact city boundary. If place_id is missing, fall back to a circle.
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
                logger.error("Geoapify Places failed: %s %s", response.status, body)
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

        candidates.append({
            "place_id": place_id,
            "name": name,
            "pinyin": place_name_pinyin(name),
            "category_label": place_category_label(raw_categories),
            "formatted": props.get("formatted") or "",
            "categories": raw_categories,
            "lat": lat,
            "lon": lon,
            "distance": props.get("distance"),
        })

    return candidates


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


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    first_name = message.from_user.first_name if message.from_user else "путешественник"

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

    state_line = f"\nПровинция / регион: <b>{city['state']}</b>" if city.get("state") else ""
    coords = ""
    if city.get("lat") is not None and city.get("lon") is not None:
        coords = f"\n📍 {city['lat']:.5f}, {city['lon']:.5f}"

    input_name = city["input_name"]
    formatted = city["formatted"]

    if input_name.casefold() not in formatted.casefold():
        place_line = f"<b>{input_name}</b> · {formatted}"
    else:
        place_line = f"<b>{formatted}</b>"

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
            f"✅ Отлично! Берём <b>{city['input_name']}</b>.\n\n"
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

    selected_labels = " · ".join(
        INTERESTS[key]["label"] for key in interests
    )

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

    # Keep enough candidates for the AI step; show only a short preview.
    await state.update_data(poi_candidates=places[:20])
    await state.set_state(QuestForm.choosing_style)

    preview = places[:6]
    lines = []
    for index, place in enumerate(preview, start=1):
        line = (
            f"{index}. {place.get('category_label', '📍 место')} — "
            f"<b>{place['name']}</b>"
        )
        if place.get("pinyin"):
            line += f"\n   <i>{place['pinyin']}</i>"
        lines.append(line)

    city_label = city.get("input_name") or city.get("city") or "город"

    await status.edit_text(
        f"📍 <b>Нашёл реальные места в {city_label}</b>\n\n"
        "Вот несколько примеров:\n\n"
        + "\n".join(lines)
        + f"\n\nВсего подходящих кандидатов: <b>{len(places)}</b>.\n\n"
        "Сейчас ничего выбирать не нужно — это предварительный список. "
        "После выбора стиля бот сам выберет лучшие точки для квеста.\n\n"
        "🎯 <b>Как будем исследовать город?</b>",
        reply_markup=style_keyboard(),
    )


@router.callback_query(F.data.startswith("style:"))
async def cb_style(callback: CallbackQuery, state: FSMContext):
    style = callback.data.split(":", 1)[1]

    if style not in STYLE_LABELS:
        await callback.answer("Неизвестный стиль", show_alert=True)
        return

    await callback.answer()
    await state.update_data(quest_style=style)
    await state.set_state(QuestForm.ready_to_generate)

    data = await state.get_data()
    city = data.get("city", {})
    duration = data.get("duration", "")
    interests = data.get("interests", [])
    places = data.get("poi_candidates", [])

    selected_labels = ", ".join(
        INTERESTS[key]["label"] for key in interests
    )

    if callback.message:
        await callback.message.answer(
            "✅ <b>Основа квеста готова</b>\n\n"
            f"🏮 Город: <b>{city.get('input_name') or city.get('city', '')}</b>\n"
            f"⏱ Время: <b>{duration}</b>\n"
            f"✨ Интересы: {selected_labels}\n"
            f"🎯 Стиль: <b>{STYLE_LABELS[style]}</b>\n"
            f"📍 Реальных мест в резерве: <b>{len(places)}</b>\n\n"
            "Следующий шаг — подключить ИИ: он выберет из этих реальных точек "
            "подходящие места и превратит их в персональные миссии."
        )


@router.callback_query(F.data == "my_quests")
async def cb_my_quests(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎒 <b>Мои приключения</b>\n\n"
            "Здесь будут храниться созданные и завершённые CityQuest."
        )


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "ℹ️ <b>Как работает CityQuest China</b>\n\n"
            "1. Ты выбираешь город, время и интересы.\n"
            "2. Бот находит реальные места через Geoapify.\n"
            "3. ИИ превращает их в персональный городской квест.\n"
            "4. Ты отмечаешь выполненные миссии прямо в Telegram."
        )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Starting CityQuest China")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
