import os
import asyncio
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Загружаем переменные из файла .env
load_dotenv()

# Читаем настройки из окружения
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

# Проверка: если переменные не заданы — бот не запустится
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не найдена в .env файле!")
if not OWNER_ID:
    raise ValueError("Переменная OWNER_ID не найдена в .env файле!")

# Преобразуем ID в число (так как в .env он хранится как строка)
try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    raise ValueError("OWNER_ID должен быть целым числом!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все входящие сообщения и пересылает владельцу."""
    user = update.effective_user
    message = update.effective_message

    if not message or not user:
        return

    try:
        # Пересылаем копию сообщения владельцу
        await message.copy(chat_id=OWNER_ID)
        await message.reply_text("✅ Ваше сообщение отправлено в поддержку.")
    except Exception as e:
        await message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        print(f"Ошибка пересылки: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение по команде /start."""
    await update.message.reply_text(
        "👋 Привет! Я бот поддержки.\n"
        "Просто отправьте мне любое сообщение (текст, фото, видео), "
        "и я передам его оператору."
    )

def main():
    # Создаём приложение с токеном из переменной
    app = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики
    app.add_handler(MessageHandler(filters.COMMAND, start))  # /start
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))  # всё остальное

    print("🚀 Бот запущен! Ожидаю сообщения...")
    app.run_polling()

if __name__ == "__main__":
    main()
