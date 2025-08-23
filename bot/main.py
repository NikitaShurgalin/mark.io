import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
import json

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# States for ConversationHandler
CHOOSING_SCHEDULE, WORK_DURATION, REST_DURATION, HOUSEHOLD_TIME, HOUSEHOLD_TEXT = range(5)

# Callback data
STOP_SCHEDULE_CB = "stop_schedule"
RESUME_SCHEDULE_CB = "resume_schedule"

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("time_to_work_bot")

STATE_PATH = os.path.join(os.path.dirname(__file__), "schedule_state.json")


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to load state: %s", e)
        return {}


def _save_state_for_chat(chat_id: int, work_m: int, rest_m: int) -> None:
    data = _load_state()
    data[str(chat_id)] = {"work": int(work_m), "rest": int(rest_m)}
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, STATE_PATH)


def _get_saved_durations(chat_id: int) -> Optional[tuple[int, int]]:
    data = _load_state()
    entry = data.get(str(chat_id))
    if isinstance(entry, dict) and "work" in entry and "rest" in entry:
        try:
            return int(entry["work"]), int(entry["rest"])
        except Exception:
            return None
    return None


def _get_choice_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["Рабочее", "Бытовое"],
    ], resize_keyboard=True, one_time_keyboard=True)


def _get_stop_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="Остановить расписание", callback_data=STOP_SCHEDULE_CB)]
    ])


def _get_resume_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="Возобновить расписание", callback_data=RESUME_SCHEDULE_CB)]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Cancel any ongoing schedules on /start
    await _cancel_any_running_schedule(context)

    await update.message.reply_text(
        "Выбери вид расписания на сегодня",
        reply_markup=_get_choice_keyboard(),
    )
    return CHOOSING_SCHEDULE


async def choose_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = (update.message.text or "").strip().lower()

    if choice == "рабочее":
        await update.message.reply_text(
            "Сколько времени на работу? (Введите ответ в минутах)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return WORK_DURATION

    if choice == "бытовое":
        await update.message.reply_text(
            "Во сколько вам надо напомнить? (запишите в виде 00:00)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return HOUSEHOLD_TIME

    await update.message.reply_text(
        "Пожалуйста, выберите один из вариантов: Рабочее | Бытовое",
        reply_markup=_get_choice_keyboard(),
    )
    return CHOOSING_SCHEDULE


async def work_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Введите положительное число минут, например 50")
        return WORK_DURATION

    context.user_data["work_minutes"] = int(text)
    await update.message.reply_text("Сколько времени на отдых? (Введите ответ в минутах)")
    return REST_DURATION


async def rest_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Введите положительное число минут, например 10")
        return REST_DURATION

    context.user_data["rest_minutes"] = int(text)

    # Start cyclic schedule immediately
    work_m = int(context.user_data["work_minutes"])  # type: ignore[index]
    rest_m = int(context.user_data["rest_minutes"])  # type: ignore[index]

    await update.message.reply_text(
        f"Ок! Запускаю расписание: работа {work_m} мин, отдых {rest_m} мин.",
    )

    # Ensure any existing schedule is cancelled
    await _cancel_any_running_schedule(context)

    # Create and store a background task for this chat
    chat_id = update.effective_chat.id
    task = asyncio.create_task(_work_rest_loop(context, chat_id, work_m, rest_m))
    context.chat_data["work_rest_task"] = task
    context.chat_data["last_work_m"] = work_m
    context.chat_data["last_rest_m"] = rest_m
    _save_state_for_chat(chat_id, work_m, rest_m)

    # Send immediate first phase for clarity
    await context.bot.send_message(
        chat_id=chat_id,
        text="Пора работать!",
        reply_markup=_get_stop_inline_keyboard(),
    )

    # Return to end conversation; controls are via inline button now
    return ConversationHandler.END


async def household_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        target_time = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите время как 00:00")
        return HOUSEHOLD_TIME

    # Compute delay from now to next occurrence of target HH:MM
    now = datetime.now()
    today_target = datetime.combine(now.date(), target_time)
    if today_target <= now:
        # schedule for tomorrow
        target_dt = today_target + timedelta(days=1)
    else:
        target_dt = today_target

    delay_seconds = int((target_dt - now).total_seconds())
    context.user_data["household_delay"] = delay_seconds

    await update.message.reply_text("О чем вас напомнить?")
    return HOUSEHOLD_TEXT


async def _household_job_cb(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    job = getattr(ctx, "job", None)
    chat_id = getattr(job, "chat_id", None)
    text = getattr(job, "data", None)
    if chat_id is None or not text:
        logger.warning("Household job missing chat_id or text: %s", job)
        return
    await ctx.bot.send_message(chat_id=chat_id, text=text)


async def household_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Пожалуйста, введите текст напоминания")
        return HOUSEHOLD_TEXT

    delay_seconds = int(context.user_data.get("household_delay", 0))
    chat_id = update.effective_chat.id

    # Schedule one-time job via JobQueue with explicit chat_id and data
    when = timedelta(seconds=delay_seconds) if delay_seconds > 0 else timedelta(seconds=0)
    context.job_queue.run_once(_household_job_cb, when=when, chat_id=chat_id, data=text)

    # Inform user
    minutes = delay_seconds // 60
    await update.message.reply_text(
        f"Готово! Напомню через {minutes} мин.",
    )

    return ConversationHandler.END


async def stop_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == STOP_SCHEDULE_CB:
        cancelled = await _cancel_any_running_schedule(context)
        if cancelled:
            await query.edit_message_text("Расписание остановлено.")
            chat_id = query.message.chat_id
            await context.bot.send_message(
                chat_id=chat_id,
                text="Хотите возобновить?",
                reply_markup=_get_resume_inline_keyboard(),
            )
        else:
            await query.edit_message_text("Нет активного расписания.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cancelled = await _cancel_any_running_schedule(context)
    if cancelled:
        await update.message.reply_text(
            "Расписание остановлено. Хотите возобновить?",
            reply_markup=_get_resume_inline_keyboard(),
        )
    else:
        await update.message.reply_text("Нет активного расписания.")


async def resume_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data != RESUME_SCHEDULE_CB:
        return

    chat_id = query.message.chat_id
    work_m = context.chat_data.get("last_work_m")
    rest_m = context.chat_data.get("last_rest_m")

    if not isinstance(work_m, int) or not isinstance(rest_m, int):
        saved = _get_saved_durations(chat_id)
        if saved is None:
            await query.edit_message_text("Нет сохранённых настроек. Запустите заново через /start.")
            return
        work_m, rest_m = saved

    # Ensure any existing schedule is cancelled (safety)
    await _cancel_any_running_schedule(context)

    # Start loop again
    task = asyncio.create_task(_work_rest_loop(context, chat_id, work_m, rest_m))
    context.chat_data["work_rest_task"] = task

    await query.edit_message_text(f"Расписание возобновлено: работа {work_m} мин, отдых {rest_m} мин.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    work_m = context.chat_data.get("last_work_m")
    rest_m = context.chat_data.get("last_rest_m")

    if not isinstance(work_m, int) or not isinstance(rest_m, int):
        saved = _get_saved_durations(chat_id)
        if saved is None:
            await update.message.reply_text("Нет сохранённых настроек. Запустите заново через /start.")
            return
        work_m, rest_m = saved

    await _cancel_any_running_schedule(context)

    task = asyncio.create_task(_work_rest_loop(context, chat_id, work_m, rest_m))
    context.chat_data["work_rest_task"] = task
    context.chat_data["last_work_m"] = work_m
    context.chat_data["last_rest_m"] = rest_m

    await update.message.reply_text(f"Расписание возобновлено: работа {work_m} мин, отдых {rest_m} мин.")


async def _cancel_any_running_schedule(context: ContextTypes.DEFAULT_TYPE) -> bool:
    task: Optional[asyncio.Task] = context.chat_data.get("work_rest_task")  # type: ignore[assignment]
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        context.chat_data.pop("work_rest_task", None)
        return True
    context.chat_data.pop("work_rest_task", None)
    return False


async def _work_rest_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, work_m: int, rest_m: int) -> None:
    try:
        while True:
            # Work phase
            await context.bot.send_message(
                chat_id=chat_id,
                text="Пора работать!",
                reply_markup=_get_stop_inline_keyboard(),
            )
            await asyncio.sleep(work_m * 60)

            # Rest phase
            await context.bot.send_message(
                chat_id=chat_id,
                text="Пора отдыхать!",
                reply_markup=_get_stop_inline_keyboard(),
            )
            await asyncio.sleep(rest_m * 60)
    except asyncio.CancelledError:
        logger.info("Work/rest loop cancelled for chat %s", chat_id)
        raise


def _get_token() -> str:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env или переменных окружения")
    return token


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: update=%s", update)


def build_application():
    token = _get_token()
    app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_SCHEDULE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, choose_schedule)
            ],
            WORK_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, work_duration)
            ],
            REST_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rest_duration)
            ],
            HOUSEHOLD_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, household_time)
            ],
            HOUSEHOLD_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, household_text)
            ],
        },
        fallbacks=[CommandHandler("stop", stop_command)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(stop_button, pattern=f"^{STOP_SCHEDULE_CB}$"))
    app.add_handler(CallbackQueryHandler(resume_button, pattern=f"^{RESUME_SCHEDULE_CB}$"))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_error_handler(_error_handler)
    return app


def main() -> None:
    app = build_application()
    logger.info("Starting Time to work bot...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()