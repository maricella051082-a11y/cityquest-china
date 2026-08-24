import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

router = Router()


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🧭 Создать квест", callback_data="new_quest")
    kb.button(text="🎒 Мои приключения", callback_data="my_quests")
    kb.button(text="ℹ️ Как это работает", callback_data="about")
    kb.adjust(1)
    return kb.as_markup()


@router.message(CommandStart())
async def cmd_start(message: Message):
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
        "/help — помощь"
    )


@router.message(Command("newquest"))
async def cmd_newquest(message: Message):
    await message.answer(
        "🧭 <b>Новый квест</b>\n\n"
        "Следующим шагом мы добавим сюда ввод города и подбор реальных мест."
    )


@router.callback_query(F.data == "new_quest")
async def cb_new_quest(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🧭 <b>Создание квеста</b>\n\n"
        "Напиши город Китая, например:\n"
        "• 成都\n"
        "• Chengdu\n"
        "• Чэнду\n\n"
        "Пока это демонстрационный экран — подключение карты будет следующим шагом."
    )


@router.callback_query(F.data == "my_quests")
async def cb_my_quests(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🎒 <b>Мои приключения</b>\n\n"
        "Здесь будут храниться созданные и завершённые CityQuest.\n"
        "Базу данных подключим после генерации первого маршрута."
    )


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>Как работает CityQuest China</b>\n\n"
        "1. Ты выбираешь город, время и интересы.\n"
        "2. Бот находит реальные места.\n"
        "3. ИИ превращает их в персональный городской квест.\n"
        "4. Ты отмечаешь выполненные миссии прямо в Telegram."
    )


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
