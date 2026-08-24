import os
import logging
import html
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# ---- Загрузка переменных окружения ----
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")
if not OWNER_ID:
    raise ValueError("OWNER_ID не задан в переменных окружения!")

try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    raise ValueError("OWNER_ID должен быть целым числом")

# ---- Логирование ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Вспомогательная функция для безопасного форматирования ----
def format_sender_info_html(user):
    """Возвращает HTML-строку с информацией об отправителе (все данные экранированы)."""
    safe_name = html.escape(user.full_name or "Без имени")
    safe_username = f"@{html.escape(user.username)}" if user.username else "нет username"
    return f"👤 От: {safe_name} (ID: <code>{user.id}</code>, {safe_username})"

# ---- Обработчик сообщений ----
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not message or not user:
        return
    if user.id == OWNER_ID:   # не пересылаем сообщения от самого владельца
        return

    sender_info = format_sender_info_html(user)

    try:
        if message.text:
            # Экранируем текст сообщения
            safe_text = html.escape(message.text)
            full_text = f"{sender_info}\n\n📝 Сообщение:\n{safe_text}"
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=full_text,
                parse_mode=ParseMode.HTML
            )
        elif message.photo or message.video or message.document or message.audio or message.voice:
            # Формируем подпись с экранированием
            caption = f"{sender_info}\n\n📎 Медиа-сообщение"
            if message.caption:
                safe_caption = html.escape(message.caption)
                caption += f"\n\n📝 Текст: {safe_caption}"
            await message.copy(
                chat_id=OWNER_ID,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            # Прочие типы (стикеры, контакты, локации)
            await message.copy(chat_id=OWNER_ID)
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=sender_info,
                parse_mode=ParseMode.HTML
            )

        await message.reply_text("✅ Ваше сообщение передано в поддержку.")
    except Exception as e:
        logger.error(f"Ошибка при пересылке: {e}")
        await message.reply_text("❌ Не удалось отправить. Попробуйте позже.")

# ---- Команда /start ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот поддержки.\n"
        "Отправьте любое сообщение, и я передам его оператору.\n\n",
        parse_mode=ParseMode.MARKDOWN
    )

# ---- Команда /help ----
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Доступные команды:\n"
        "/start – приветствие\n"
        "/help  – справка\n\n"
        "👤 Для пользователей:\n"
        "Просто отправьте сообщение.\n\n"
        "🛠 Для оператора:\n"
        "/reply <user_id> <текст> – ответить пользователю.\n"
        "Пример: `/reply 123456789 Привет!`",
        parse_mode=ParseMode.MARKDOWN
    )

# ---- Команда /reply (только для владельца) ----
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Нет прав.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/reply <user_id> <текст>`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id должен быть числом.")
        return
    reply_text = " ".join(args[1:])
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📩 Ответ от поддержки:\n\n{reply_text}"
        )
        await update.message.reply_text(
            f"✅ Ответ отправлен пользователю ID `{target_user_id}`.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить. Ошибка: {e}")

# ---- Запуск ----
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reply", reply_to_user))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    logger.info("🚀 Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
