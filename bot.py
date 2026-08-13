import asyncio
import json
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import load_config
from db import Memory
from ollama_client import OllamaClient
from orchestrator import TravelOrchestrator, default_trip_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
TRIP_DUMP_MAX_CHARS = 8000


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into Telegram-sendable chunks, preferring to break on a
    newline near the limit so paragraphs/lists don't get cut mid-line."""
    if len(text) <= limit:
        return [text] if text else []

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


async def reply_in_chunks(message, text: str) -> None:
    try:
        for chunk in split_message(text):
            await message.reply_text(chunk)
    except Exception:
        logger.exception("Failed to send reply to chat %s", message.chat_id)


WELCOME_TEXT = (
    "Hi! I'm your AI travel planning assistant. Tell me where you'd like to go, your dates, "
    "budget, and who's traveling, and I'll research destinations, flights, hotels, weather, visas, "
    "activities, food, and put together a day-by-day itinerary.\n\n"
    "Commands:\n"
    "/newtrip - start planning a fresh trip (keeps your saved preferences)\n"
    "/trip - show what I currently know about your trip\n"
    "/reset - forget this chat's history and trip entirely"
)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    memory: Memory = context.bot_data["memory"]
    orchestrator: TravelOrchestrator = context.bot_data["orchestrator"]
    history_limit: int = context.bot_data["history_limit"]

    try:
        await memory.add_message(chat_id, "user", message.text)
        history = await memory.history(chat_id, history_limit)

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await orchestrator.handle_turn(chat_id, history, message.text)
        await memory.add_message(chat_id, "assistant", reply)
    except Exception:
        logger.exception("Failed to handle message for chat %s", chat_id)
        await reply_in_chunks(message, "Sorry, I ran into a problem planning that. Please try again.")
        return

    await reply_in_chunks(message, reply)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(WELCOME_TEXT)


async def handle_newtrip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    chat_id = update.effective_chat.id
    memory: Memory = context.bot_data["memory"]

    trip_state = await memory.get_trip_state(chat_id)
    preferences = (trip_state or {}).get("preferences")
    if isinstance(preferences, dict) and any(preferences.values()):
        await memory.save_preferences(chat_id, preferences)

    await memory.reset_trip_state(chat_id)

    fresh_state = default_trip_state()
    saved_preferences = await memory.get_preferences(chat_id)
    if saved_preferences:
        fresh_state["preferences"].update(saved_preferences)
        await memory.save_trip_state(chat_id, fresh_state)

    await message.reply_text("Started a fresh trip. Where would you like to go?")


async def handle_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    chat_id = update.effective_chat.id
    memory: Memory = context.bot_data["memory"]
    trip_state = await memory.get_trip_state(chat_id)
    if not trip_state:
        await message.reply_text("I don't have any trip details yet. Tell me where you'd like to go!")
        return

    summary = json.dumps(trip_state, indent=2, default=str)
    if len(summary) > TRIP_DUMP_MAX_CHARS:
        summary = summary[:TRIP_DUMP_MAX_CHARS] + "\n... (truncated)"
    await reply_in_chunks(message, f"Here's what I currently know about your trip:\n\n{summary}")


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    chat_id = update.effective_chat.id
    memory: Memory = context.bot_data["memory"]
    await memory.reset_trip_state(chat_id)
    await message.reply_text("Cleared this trip. Starting over - where would you like to go?")


async def main() -> None:
    config = load_config()
    memory = await Memory.connect(config.postgres_dsn)
    ollama = OllamaClient(config.ollama_host, config.ollama_model, config.ollama_api_key)
    orchestrator = TravelOrchestrator(ollama, memory)

    application = Application.builder().token(config.telegram_token).build()
    application.bot_data["memory"] = memory
    application.bot_data["ollama"] = ollama
    application.bot_data["orchestrator"] = orchestrator
    application.bot_data["history_limit"] = config.history_limit
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("newtrip", handle_newtrip))
    application.add_handler(CommandHandler("trip", handle_trip))
    application.add_handler(CommandHandler("reset", handle_reset))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot started, polling for updates")
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await ollama.close()
        await memory.close()


if __name__ == "__main__":
    asyncio.run(main())
