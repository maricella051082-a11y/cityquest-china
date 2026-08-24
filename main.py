import asyncio
import logging
import os

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


async def geocode_city(city_query: str) -> dict | None:
    """
    Search a real city in China via Geoapify.
    First try strict city search, then a broader China-only fallback.
    """
    url = "https://api.geoapify.com/v1/geocode/search"

    base_params = {
        "text": city_query,
        "filter": "countrycode:cn",
        "limit": 5,
        "format": "json",
        "apiKey": GEOAPIFY_API_KEY,
    }

    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Strict city lookup
        strict_params = dict(base_params)
        strict_params["type"] = "city"

        async with session.get(url, params=strict_params) as response:
            if response.status != 200:
                body = await response.text()
                logger.error("Geoapify city search failed: %s %s", response.status, body)
                raise RuntimeError("Geoapify request failed")

            data = await response.json()
            results = data.get("results", [])

        # Fallback: sometimes localized city names are indexed differently
        if not results:
            async with session.get(url, params=base_params) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Geoapify fallback failed: %s %s", response.status, body)
                    raise RuntimeError("Geoapify request failed")

                data = await response.json()
                results = data.get("results", [])

        if not results:
            return None

        # Prefer actual city/town-like result if available
        preferred = None
        for item in results:
            result_type = (item.get("result_type") or "").lower()
            if result_type in {"city", "town", "village", "suburb", "district"}:
                preferred = item
                break

        item = preferred or results[0]

        return {
            "place_id": item.get("place_id"),
            "formatted": item.get("formatted") or item.get("address_line1") or city_query,
            "city": item.get("city")
                or item.get("town")
                or item.get("village")
                or item.get("county")
                or city_query,
            "state": item.get("state"),
            "country": item.get("country") or "China",
            "lat": item.get("lat"),
            "lon": item.get("lon"),
        }


async def ask_for_city(message: Message, state: FSMContext):
    await state.set_state(QuestForm.waiting_city)
    await message.answer(
        "🧭 <b>Новый CityQuest</b>\n\n"
        "Напиши город Китая, который хочешь исследовать.\n\n"
        "Можно написать по-китайски, по-английски или по-русски.\n\n"
        "Например:\n"
        "• 成都\n"
        "• Chengdu\n"
        "• Чэнду"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    first_name = message.from_user.first_name if message.from_user else "путешественник"
    text = (
        f"🏮 <b>CityQuest China 城市奇遇</b>\n\n"
        f"Привет, {first_name}!\n\n"
        "Я превращаю прогулки по городам Китая в персональные квесты. "
        "Выбери город, время и интересы — а затем выполняй миссии прямо в Telegram.\n\n"
        "С чего начнём?"
    )
    await message.answer(text, reply_markup=main_menu())


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
        logger.exception("Geoapify error")
        await status_message.edit_text(
            "🗺 Карта сейчас не ответила.\n\n"
            "Попробуй ещё раз через несколько секунд."
        )
        return

    if not city:
        await status_message.edit_text(
            "🤔 Не получилось найти этот город в Китае.\n\n"
            "Попробуй другое написание — например, по-английски или по-китайски."
        )
        return

    await state.update_data(city=city)

    state_line = f"\nПровинция / регион: <b>{city['state']}</b>" if city.get("state") else ""
    coords = ""
    if city.get("lat") is not None and city.get("lon") is not None:
        coords = f"\n📍 {city['lat']:.5f}, {city['lon']:.5f}"

    text = (
        "🇨🇳 <b>Нашёл!</b>\n\n"
        f"<b>{city['formatted']}</b>"
        f"{state_line}"
        f"{coords}\n\n"
        "Это тот город?"
    )

    await status_message.edit_text(
        text,
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
            f"✅ Отлично! Берём <b>{city['city']}</b>.\n\n"
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
    data = await state.get_data()
    city = data.get("city", {})

    await state.update_data(duration=duration)

    if callback.message:
        await callback.message.answer(
            f"🏮 <b>{city.get('city', 'Город')}</b> · {duration}\n\n"
            "Город уже подтверждён через Geoapify ✅\n\n"
            "Следующим шагом добавим выбор интересов: чай, еда, история, фото, природа и необычные места."
        )


@router.callback_query(F.data == "my_quests")
async def cb_my_quests(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎒 <b>Мои приключения</b>\n\n"
            "Здесь будут храниться созданные и завершённые CityQuest.\n"
            "Базу данных подключим после генерации первого полноценного квеста."
        )


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "ℹ️ <b>Как работает CityQuest China</b>\n\n"
            "1. Ты выбираешь город, время и интересы.\n"
            "2. Бот находит реальные места через картографический API.\n"
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
