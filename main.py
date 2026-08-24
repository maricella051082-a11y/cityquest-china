import html
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cityquest")


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧭 Создать квест", callback_data="new_quest")],
            [InlineKeyboardButton("🎒 Мои приключения", callback_data="my_quests")],
            [InlineKeyboardButton("ℹ️ Как это работает", callback_data="about")],
        ]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    first_name = "путешественник"
    if update.effective_user and update.effective_user.first_name:
        first_name = html.escape(update.effective_user.first_name)

    text = (
        "🏮 <b>CityQuest China 城市奇遇</b>\n\n"
        f"Привет, {first_name}!\n\n"
        "Я превращаю прогулки по городам Китая в персональные квесты. "
        "Выбери город, время и интересы — а затем выполняй миссии прямо в Telegram.\n\n"
        "С чего начнём?"
    )
    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "🏮 <b>CityQuest China</b>\n\n"
        "/start — главное меню\n"
        "/newquest — создать новый квест\n"
        "/help — помощь",
        parse_mode=ParseMode.HTML,
    )


async def cmd_newquest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "🧭 <b>Новый квест</b>\n\n"
        "Следующим шагом мы добавим сюда ввод города и подбор реальных мест.",
        parse_mode=ParseMode.HTML,
    )


async def cb_new_quest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if query.message:
        await query.message.reply_text(
            "🧭 <b>Создание квеста</b>\n\n"
            "Напиши город Китая, например:\n"
            "• 成都\n"
            "• Chengdu\n"
            "• Чэнду\n\n"
            "Пока это демонстрационный экран — подключение карты будет следующим шагом.",
            parse_mode=ParseMode.HTML,
        )


async def cb_my_quests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if query.message:
        await query.message.reply_text(
            "🎒 <b>Мои приключения</b>\n\n"
            "Здесь будут храниться созданные и завершённые CityQuest.\n"
            "Базу данных подключим после генерации первого маршрута.",
            parse_mode=ParseMode.HTML,
        )


async def cb_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if query.message:
        await query.message.reply_text(
            "ℹ️ <b>Как работает CityQuest China</b>\n\n"
            "1. Ты выбираешь город, время и интересы.\n"
            "2. Бот находит реальные места.\n"
            "3. ИИ превращает их в персональный городской квест.\n"
            "4. Ты отмечаешь выполненные миссии прямо в Telegram.",
            parse_mode=ParseMode.HTML,
        )


def build_application() -> Application:
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=45.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("newquest", cmd_newquest))
    application.add_handler(CallbackQueryHandler(cb_new_quest, pattern="^new_quest$"))
    application.add_handler(CallbackQueryHandler(cb_my_quests, pattern="^my_quests$"))
    application.add_handler(CallbackQueryHandler(cb_about, pattern="^about$"))

    return application


def main() -> None:
    logger.info("Starting CityQuest China with python-telegram-bot")
    application = build_application()
    application.run_polling(
        bootstrap_retries=-1,
        drop_pending_updates=False,
        poll_interval=0.5,
        timeout=30,
    )


if __name__ == "__main__":
    main()
