import os
import logging
from telegram import Update, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# ---- Загрузка переменных окружения ----
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not OWNER_ID:
    raise ValueError("OWNER_ID не задан в .env")

try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    raise ValueError("OWNER_ID должен быть числом")

# ---- Настройка логирования ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Вспомогательная функция для форматирования информации об отправителе ----
def format_sender_info(user):
    """Возвращает строку с ID, именем и username пользователя."""
    name = user.full_name or "Без имени"
    username = f"@{user.username}" if user.username else "нет username"
    return f"👤 От: {name} (ID: `{user.id}`, {username})"

# ---- Обработчик всех сообщений от пользователей (кроме команд) ----
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not message or not user:
        return

    # Игнорируем сообщения от самого владельца (чтобы не зациклить)
    if user.id == OWNER_ID:
        return

    # Формируем информацию об отправителе
    sender_info = format_sender_info(user)

    try:
        # Если сообщение содержит текст – отправляем владельцу новый текст с информацией
        if message.text:
            full_text = f"{sender_info}\n\n📝 Сообщение:\n{message.text}"
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=full_text,
                parse_mode=ParseMode.MARKDOWN
            )
        # Если сообщение содержит медиа (фото, видео, документ и т.д.)
        elif message.photo or message.video or message.document or message.audio or message.voice:
            # Пересылаем медиа с подписью (caption)
            caption = f"{sender_info}\n\n📎 Медиа-сообщение"
            if message.caption:
                caption += f"\n\n📝 Текст: {message.caption}"
            await message.copy(
                chat_id=OWNER_ID,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            # Прочие типы (стикеры, контакты, локации) – пересылаем как есть
            await message.copy(chat_id=OWNER_ID)
            # Дополнительно отправляем информацию об отправителе отдельным сообщением
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=sender_info,
                parse_mode=ParseMode.MARKDOWN
            )

        # Уведомляем пользователя, что сообщение доставлено
        await message.reply_text("✅ Ваше сообщение передано в поддержку.")

    except Exception as e:
        logger.error(f"Ошибка при пересылке: {e}")
        await message.reply_text("❌ Не удалось отправить сообщение. Попробуйте позже.")

# ---- Команда /start ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот поддержки.\n"
        "Просто отправьте мне любое сообщение (текст, фото, видео), "
        "и я передам его оператору.\n\n"
        "Оператор может ответить вам с помощью команды `/reply`.", 
        parse_mode=ParseMode.MARKDOWN
    )

# ---- Команда /help ----
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Доступные команды:\n"
        "/start - приветствие\n"
        "/help  - эта справка\n\n"
        "👤 Для пользователей:\n"
        "Просто отправьте сообщение – оно будет переслано оператору.\n\n"
        "🛠 Для оператора:\n"
        "/reply <user_id> <текст> – отправить ответ пользователю.\n"
        "Пример: `/reply 123456789 Привет!`",
        parse_mode=ParseMode.MARKDOWN
    )

# ---- Команда /reply (только для владельца) ----
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Проверяем, что команду выполнил владелец
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ У вас нет прав для использования этой команды.")
        return

    # Разбираем аргументы
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/reply <user_id> <текст>`\n"
            "Пример: `/reply 123456789 Привет, мы уже решаем ваш вопрос!`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target_user_id = args[0]
    reply_text = " ".join(args[1:])

    # Проверяем, что user_id – число
    try:
        target_user_id = int(target_user_id)
    except ValueError:
        await update.message.reply_text("❌ user_id должен быть числом.")
        return

    try:
        # Отправляем сообщение пользователю
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📩 Ответ от поддержки:\n\n{reply_text}"
        )
        # Подтверждаем владельцу
        await update.message.reply_text(f"✅ Ответ отправлен пользователю ID `{target_user_id}`.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа: {e}")
        await update.message.reply_text(
            f"❌ Не удалось отправить ответ. Возможно, пользователь не начал диалог с ботом или заблокировал бота.\nОшибка: {e}"
        )

# ---- Точка входа ----
def main():
    app = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reply", reply_to_user))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info("🚀 Бот запущен и ждёт сообщения...")
    app.run_polling()

if __name__ == "__main__":
    main()
