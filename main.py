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
OPERATOR_IDS_STR = os.getenv("OPERATOR_IDS", "")
OWNER_ID_STR = os.getenv("OWNER_ID", "")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

# ---- Определяем список операторов ----
OPERATOR_IDS = []

# Сначала пытаемся прочитать OPERATOR_IDS (список через запятую)
if OPERATOR_IDS_STR.strip():
    try:
        OPERATOR_IDS = [int(x.strip()) for x in OPERATOR_IDS_STR.split(",") if x.strip()]
    except ValueError:
        raise ValueError("OPERATOR_IDS должен содержать целые числа, разделённые запятыми")

# Если OPERATOR_IDS пуст, пробуем использовать OWNER_ID (один оператор)
if not OPERATOR_IDS and OWNER_ID_STR.strip():
    try:
        OPERATOR_IDS = [int(OWNER_ID_STR.strip())]
        print("⚠️ Используется OWNER_ID как единственный оператор. Рекомендуется перейти на OPERATOR_IDS.")
    except ValueError:
        raise ValueError("OWNER_ID должен быть целым числом")

# Если всё равно пусто – ошибка
if not OPERATOR_IDS:
    raise ValueError("Не задан ни OPERATOR_IDS, ни OWNER_ID. Укажите хотя бы одного оператора!")

# ---- Логирование ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.info(f"Операторы: {OPERATOR_IDS}")

# ---- Вспомогательная функция для безопасного форматирования (HTML) ----
def format_sender_info_html(user):
    safe_name = html.escape(user.full_name or "Без имени")
    safe_username = f"@{html.escape(user.username)}" if user.username else "нет username"
    return f"👤 От: {safe_name} (ID: <code>{user.id}</code>, {safe_username})"

# ---- Обработчик всех сообщений от пользователей ----
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not message or not user:
        return

    # Если сообщение от оператора – не пересылаем
    if user.id in OPERATOR_IDS:
        return

    sender_info = format_sender_info_html(user)

    try:
        # Пересылаем каждому оператору
        for operator_id in OPERATOR_IDS:
            if message.text:
                safe_text = html.escape(message.text)
                full_text = f"{sender_info}\n\n📝 Сообщение:\n{safe_text}"
                await context.bot.send_message(
                    chat_id=operator_id,
                    text=full_text,
                    parse_mode=ParseMode.HTML
                )
            elif message.photo or message.video or message.document or message.audio or message.voice:
                caption = f"{sender_info}\n\n📎 Медиа-сообщение"
                if message.caption:
                    safe_caption = html.escape(message.caption)
                    caption += f"\n\n📝 Текст: {safe_caption}"
                await message.copy(
                    chat_id=operator_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            else:
                # Стикеры, контакты, локации и т.п.
                await message.copy(chat_id=operator_id)
                await context.bot.send_message(
                    chat_id=operator_id,
                    text=sender_info,
                    parse_mode=ParseMode.HTML
                )

        # Подтверждение пользователю
        await message.reply_text("✅ Ваше сообщение передано в поддержку.")
    except Exception as e:
        logger.error(f"Ошибка при пересылке: {e}")
        await message.reply_text("❌ Не удалось отправить. Попробуйте позже.")

# ---- Команда /start ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот поддержки.\n"
        "Отправьте любое сообщение, и я передам его операторам.",
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
        "🛠 Для операторов:\n"
        "/reply <user_id> <текст> – ответить пользователю.\n"
        "Пример: `/reply 123456789 Привет!`",
        parse_mode=ParseMode.MARKDOWN
    )

# ---- Команда /reply (только для операторов) ----
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OPERATOR_IDS:
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

# ---- Команда /operators (показывает список операторов) ----
async def list_operators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OPERATOR_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return
    names = []
    for op_id in OPERATOR_IDS:
        try:
            chat = await context.bot.get_chat(op_id)
            name = chat.full_name or str(op_id)
        except:
            name = str(op_id)
        names.append(f"• {name} (ID: `{op_id}`)")
    await update.message.reply_text(
        "👥 Список операторов:\n" + "\n".join(names),
        parse_mode=ParseMode.MARKDOWN
    )

# ---- Запуск ----
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reply", reply_to_user))
    app.add_handler(CommandHandler("operators", list_operators))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info(f"🚀 Бот запущен. Операторов: {len(OPERATOR_IDS)}")
    app.run_polling()

if __name__ == "__main__":
    main()
