import os
import logging
import html
import sqlite3
from datetime import datetime

from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
from dotenv import load_dotenv

# ---- Загрузка переменных окружения ----
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OPERATOR_IDS_STR = os.getenv("OPERATOR_IDS", "")
OWNER_ID_STR = os.getenv("OWNER_ID", "")
SUPPORT_GROUP_ID_STR = os.getenv("SUPPORT_GROUP_ID", "")  # ID группы поддержки
USE_TOPICS = os.getenv("USE_TOPICS", "false").lower() == "true"  # Включить форум-топики
DB_PATH = os.getenv("DB_PATH", "support_bot.sqlite3")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

# ---- Парсинг ID ----
def parse_ids(id_str: str) -> list:
    if not id_str.strip():
        return []
    parts = [x.strip() for x in id_str.split(",") if x.strip()]
    ids = []
    for part in parts:
        try:
            ids.append(int(part))
        except ValueError:
            raise ValueError(f"Некорректный ID: '{part}'")
    return ids

OPERATOR_IDS = []
if OPERATOR_IDS_STR.strip():
    OPERATOR_IDS = parse_ids(OPERATOR_IDS_STR)
if not OPERATOR_IDS and OWNER_ID_STR.strip():
    OPERATOR_IDS = parse_ids(OWNER_ID_STR)
    if OPERATOR_IDS:
        print("⚠️ Используется OWNER_ID как список операторов.")
if not OPERATOR_IDS:
    raise ValueError("Не задан ни OPERATOR_IDS, ни OWNER_ID!")

SUPPORT_GROUP_ID = None
if SUPPORT_GROUP_ID_STR.strip():
    try:
        SUPPORT_GROUP_ID = int(SUPPORT_GROUP_ID_STR.strip())
    except ValueError:
        raise ValueError("SUPPORT_GROUP_ID должен быть числом!")

# ---- Логирование ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
#  ХРАНИЛИЩЕ СВЯЗЕЙ СООБЩЕНИЙ (message_map)
# ============================================================
class MessageMap:
    """
    Хранит соответствие:
      message_id в чате поддержки  →  user_id (кому отвечать)
      message_id в чате поддержки  →  original_message_id (для reply-цепочек)
    """
    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self._memory: dict[int, dict] = {}  # message_id -> {user_id, original_msg_id, topic_id}
        if db_path:
            self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_links (
                support_msg_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                original_msg_id INTEGER,
                topic_id INTEGER,
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()

    def add(self, support_msg_id: int, user_id: int,
            original_msg_id: int = None, topic_id: int = None):
        entry = {
            "user_id": user_id,
            "original_msg_id": original_msg_id,
            "topic_id": topic_id,
        }
        self._memory[support_msg_id] = entry
        if self.db_path:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT OR REPLACE INTO message_links VALUES (?, ?, ?, ?, ?)",
                (support_msg_id, user_id, original_msg_id, topic_id,
                 datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()

    def get_user_id(self, support_msg_id: int) -> int | None:
        entry = self._memory.get(support_msg_id)
        if entry:
            return entry["user_id"]
        if self.db_path:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT user_id FROM message_links WHERE support_msg_id = ?",
                (support_msg_id,)
            ).fetchone()
            conn.close()
            if row:
                return row[0]
        return None

    def get_topic_id(self, support_msg_id: int) -> int | None:
        entry = self._memory.get(support_msg_id)
        if entry:
            return entry["topic_id"]
        if self.db_path:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT topic_id FROM message_links WHERE support_msg_id = ?",
                (support_msg_id,)
            ).fetchone()
            conn.close()
            if row:
                return row[0]
        return None


message_map = MessageMap(DB_PATH if DB_PATH else None)

# ============================================================
#  РАБОТА С ТОПИКАМИ (форум-топики)
# ============================================================
# topic_map: user_id -> topic_id (кэш в памяти)
topic_map: dict[int, int] = {}

async def get_or_create_topic(bot, user) -> int | None:
    """Возвращает topic_id для пользователя, создавая тему при необходимости."""
    if not USE_TOPICS or not SUPPORT_GROUP_ID:
        return None

    if user.id in topic_map:
        return topic_map[user.id]

    # Проверяем в БД
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT topic_id FROM user_topics WHERE user_id = ?", (user.id,)
    ).fetchone()
    conn.close()
    if row:
        topic_map[user.id] = row[0]
        return row[0]

    # Создаём новую тему
    try:
        result = await bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            name=f"{user.full_name or 'User'} | ID: {user.id}"
        )
        topic_id = result.message_thread_id
        topic_map[user.id] = topic_id

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO user_topics VALUES (?, ?)",
            (user.id, topic_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"Создана тема {topic_id} для пользователя {user.id}")
        return topic_id
    except Exception as e:
        logger.error(f"Не удалось создать тему: {e}")
        return None


def _init_topics_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_topics (
            user_id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()

if USE_TOPICS and DB_PATH:
    _init_topics_table()

# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def format_sender_info_html(user) -> str:
    safe_name = html.escape(user.full_name or "Без имени")
    safe_username = f"@{html.escape(user.username)}" if user.username else "нет username"
    return (
        f"👤 <b>{safe_name}</b>\n"
        f"🆔 <code>{user.id}</code>\n"
        f"🔗 {safe_username}"
    )


def get_support_chat_id() -> int:
    """Куда отправлять сообщения пользователей."""
    if SUPPORT_GROUP_ID:
        return SUPPORT_GROUP_ID
    # Fallback: личка первого оператора
    return OPERATOR_IDS[0]


# ============================================================
#  ОБРАБОТЧИК СООБЩЕНИЙ ОТ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not message or not user:
        return
    # Игнорируем сообщения от операторов (они пишут в группу поддержки)
    if user.id in OPERATOR_IDS:
        return
    # Игнорируем сообщения из группы поддержки
    if SUPPORT_GROUP_ID and update.effective_chat.id == SUPPORT_GROUP_ID:
        return

    support_chat_id = get_support_chat_id()

    # Определяем topic_id для форум-топиков
    topic_id = None
    if USE_TOPICS:
        topic_id = await get_or_create_topic(context.bot, user)

    # Формируем сообщение для операторов
    sender_info = format_sender_info_html(user)

    try:
        sent_msg: Message | None = None

        if message.text:
            safe_text = html.escape(message.text)
            full_text = f"{sender_info}\n\n📝 <b>Сообщение:</b>\n{safe_text}"
            sent_msg = await context.bot.send_message(
                chat_id=support_chat_id,
                text=full_text,
                parse_mode=ParseMode.HTML,
                message_thread_id=topic_id,
                reply_markup=None  # можно добавить кнопку «Ответить»
            )
        elif any([message.photo, message.video, message.document,
                  message.audio, message.voice, message.video_note]):
            caption = f"{sender_info}\n\n📎 <b>Медиа-сообщение</b>"
            if message.caption:
                safe_caption = html.escape(message.caption)
                caption += f"\n\n📝 <b>Текст:</b> {safe_caption}"
            sent_msg = await message.copy(
                chat_id=support_chat_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                message_thread_id=topic_id
            )
        else:
            # Прочие типы (стикеры, гифки и т.д.)
            sent_msg = await message.copy(
                chat_id=support_chat_id,
                message_thread_id=topic_id
            )
            # Дополнительно отправляем инфо
            await context.bot.send_message(
                chat_id=support_chat_id,
                text=sender_info,
                parse_mode=ParseMode.HTML,
                message_thread_id=topic_id
            )

        # === КЛЮЧЕВОЙ МОМЕНТ: сохраняем связь ===
        if sent_msg:
            message_map.add(
                support_msg_id=sent_msg.message_id,
                user_id=user.id,
                original_msg_id=message.message_id,
                topic_id=topic_id
            )

        await message.reply_text("✅ Ваше сообщение передано в поддержку.")

    except Exception as e:
        logger.error(f"Ошибка при пересылке: {e}")
        await message.reply_text("❌ Не удалось отправить. Попробуйте позже.")


# ============================================================
#  ОБРАБОТЧИК СООБЩЕНИЙ ОТ ОПЕРАТОРОВ (СВАЙП-ОТВЕТ)
# ============================================================
async def handle_operator_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловит ответы операторов в группе поддержки.
    Если оператор свайпнул (reply) на сообщение бота — отправляем ответ пользователю.
    """
    user = update.effective_user
    message = update.effective_message

    if not message or not user:
        return
    # Только для операторов
    if user.id not in OPERATOR_IDS:
        return
    # Только в чате поддержки
    if SUPPORT_GROUP_ID and update.effective_chat.id != SUPPORT_GROUP_ID:
        return

    # Проверяем, что это ответ на сообщение
    if not message.reply_to_message:
        # Если оператор пишет без reply — можно проигнорировать или подсказать
        await message.reply_text(
            "💡 Чтобы ответить пользователю, <b>свайпните</b> на его сообщение и напишите ответ.",
            parse_mode=ParseMode.HTML
        )
        return

    replied_msg_id = message.reply_to_message.message_id
    target_user_id = message_map.get_user_id(replied_msg_id)

    if not target_user_id:
        await message.reply_text(
            "❌ Не удалось определить пользователя. Возможно, сообщение слишком старое.",
            parse_mode=ParseMode.HTML
        )
        return

    # Отправляем ответ пользователю в личку
    try:
        # Формируем текст ответа
        if message.text:
            reply_text = message.text
        elif message.caption:
            reply_text = message.caption
        else:
            reply_text = "📎 Медиа-ответ"

        # Отправляем с сохранением reply-цепочки (если возможно)
        original_msg_id = message_map._memory.get(replied_msg_id, {}).get("original_msg_id")

        if message.text:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"📩 <b>Ответ от поддержки:</b>\n\n{html.escape(reply_text)}",
                parse_mode=ParseMode.HTML
            )
        else:
            # Копируем медиа
            await message.copy(
                chat_id=target_user_id,
                caption=f"📩 <b>Ответ от поддержки</b>\n\n{html.escape(reply_text)}",
                parse_mode=ParseMode.HTML
            )

        # Подтверждаем оператору (реакция на его сообщение)
        await message.reply_text("✅ Ответ отправлен.", parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю {target_user_id}: {e}")
        await message.reply_text(
            f"❌ Не удалось отправить: {e}",
            parse_mode=ParseMode.HTML
        )


# ============================================================
#  КОМАНДЫ
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in OPERATOR_IDS:
        await update.message.reply_text(
            "👋 Вы оператор. Пишите в группу поддержки и отвечайте свайпом на сообщения пользователей.",
        )
        return
    await update.message.reply_text(
        "👋 Привет! Я бот поддержки.\n"
        "Отправьте любое сообщение, и я передам его операторам.\n\n"
        "💡 Операторы ответят вам прямо здесь.",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Доступные команды:</b>\n"
        "/start – приветствие\n"
        "/help  – справка\n\n"
        "👤 <b>Для пользователей:</b>\n"
        "Просто отправьте сообщение.\n\n"
        "🛠 <b>Для операторов:</b>\n"
        "• <b>Свайп-ответ</b> на сообщение пользователя — ответ уйдёт ему в личку.\n"
        "• /reply &lt;user_id&gt; &lt;текст&gt; — ответ по ID (если свайп не сработал).\n"
        "• /operators — список операторов.\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: /reply <user_id> <текст>"""
    user = update.effective_user
    if user.id not in OPERATOR_IDS:
        await update.message.reply_text("⛔ Нет прав.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: <code>/reply &lt;user_id&gt; &lt;текст&gt;</code>",
            parse_mode=ParseMode.HTML
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
            text=f"📩 <b>Ответ от поддержки:</b>\n\n{html.escape(reply_text)}",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text(
            f"✅ Ответ отправлен пользователю <code>{target_user_id}</code>.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка /reply: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")


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
        names.append(f"• {name} (ID: <code>{op_id}</code>)")

    await update.message.reply_text(
        "👥 <b>Операторы:</b>\n" + "\n".join(names),
        parse_mode=ParseMode.HTML
    )


# ============================================================
#  ЗАПУСК
# ============================================================
def main():
    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reply", reply_to_user))
    app.add_handler(CommandHandler("operators", list_operators))

    # Сообщения от пользователей (личка)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_user_message
    ))

    # Сообщения от операторов (в группе поддержки) — с приоритетом на reply
    app.add_handler(MessageHandler(
        filters.Chat(SUPPORT_GROUP_ID) & ~filters.COMMAND & filters.REPLY,
        handle_operator_reply
    ))

    logger.info(f"🚀 Бот запущен. Операторов: {len(OPERATOR_IDS)}")
    logger.info(f"📢 Группа поддержки: {SUPPORT_GROUP_ID}")
    logger.info(f"🧵 Топики: {'включены' if USE_TOPICS else 'выключены'}")

    app.run_polling()


if __name__ == "__main__":
    main()
