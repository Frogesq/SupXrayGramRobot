import os
import html
import logging
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

# ---------- ENV ----------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
OPERATOR_IDS_STR = os.getenv("OPERATOR_IDS", "")
OWNER_ID_STR = os.getenv("OWNER_ID", "")
DB_PATH = os.getenv("DB_PATH", "support_bot.sqlite3")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан")

def parse_ids(s: str) -> list[int]:
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise ValueError(f"Некорректный ID: {part}")
    return out

OPERATOR_IDS = parse_ids(OPERATOR_IDS_STR) or parse_ids(OWNER_ID_STR)
if not OPERATOR_IDS:
    raise ValueError("Не задан OPERATOR_IDS или OWNER_ID")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)


# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        full_name   TEXT,
        username    TEXT,
        created_at  TEXT
    );
    CREATE TABLE IF NOT EXISTS tickets (
        ticket_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open',
        created_at  TEXT,
        updated_at  TEXT,
        closed_at   TEXT
    );
    CREATE TABLE IF NOT EXISTS messages (
        message_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id      INTEGER NOT NULL,
        from_user_id   INTEGER NOT NULL,
        is_operator    INTEGER NOT NULL DEFAULT 0,
        text           TEXT,
        created_at     TEXT
    );
    CREATE TABLE IF NOT EXISTS message_links (
        operator_id     INTEGER NOT NULL,
        operator_msg_id INTEGER NOT NULL,
        ticket_id       INTEGER NOT NULL,
        user_id         INTEGER NOT NULL,
        PRIMARY KEY (operator_id, operator_msg_id)
    );
    CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
    CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id);
    """)
    conn.commit()
    conn.close()


def now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def esc(s: str | None) -> str:
    return html.escape(s or "")

def is_operator(uid: int) -> bool:
    return uid in OPERATOR_IDS


# ---------- DB helpers ----------
def save_user(user):
    conn = db()
    conn.execute(
        "INSERT INTO users(user_id, full_name, username, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, "
        "username=excluded.username",
        (user.id, user.full_name, user.username, now()),
    )
    conn.commit()
    conn.close()


def get_or_create_open_ticket(user_id: int) -> int:
    conn = db()
    row = conn.execute(
        "SELECT ticket_id FROM tickets WHERE user_id=? AND status='open' "
        "ORDER BY ticket_id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        conn.close()
        return row["ticket_id"]
    cur = conn.execute(
        "INSERT INTO tickets(user_id, status, created_at, updated_at) "
        "VALUES (?, 'open', ?, ?)",
        (user_id, now(), now()),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def add_message(ticket_id: int, from_user_id: int, is_operator: bool, text: str):
    conn = db()
    conn.execute(
        "INSERT INTO messages(ticket_id, from_user_id, is_operator, text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticket_id, from_user_id, 1 if is_operator else 0, text, now()),
    )
    conn.execute("UPDATE tickets SET updated_at=? WHERE ticket_id=?", (now(), ticket_id))
    conn.commit()
    conn.close()


def link_message(op_id: int, op_msg_id: int, ticket_id: int, user_id: int):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO message_links(operator_id, operator_msg_id, ticket_id, user_id) "
        "VALUES (?, ?, ?, ?)",
        (op_id, op_msg_id, ticket_id, user_id),
    )
    conn.commit()
    conn.close()


def lookup_link(op_id: int, op_msg_id: int):
    conn = db()
    row = conn.execute(
        "SELECT ticket_id, user_id FROM message_links "
        "WHERE operator_id=? AND operator_msg_id=?",
        (op_id, op_msg_id),
    ).fetchone()
    conn.close()
    return row


def get_ticket(tid: int):
    conn = db()
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (tid,)).fetchone()
    conn.close()
    return row


def set_ticket_status(tid: int, status: str):
    conn = db()
    if status == "closed":
        conn.execute(
            "UPDATE tickets SET status='closed', closed_at=? WHERE ticket_id=?",
            (now(), tid),
        )
    else:
        conn.execute(
            "UPDATE tickets SET status='open', closed_at=NULL WHERE ticket_id=?",
            (tid,),
        )
    conn.commit()
    conn.close()


# ---------- Клавиатуры ----------
def menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Открытые", callback_data="list:open:0"),
        InlineKeyboardButton("Закрытые", callback_data="list:closed:0"),
    ]])


def tickets_list_kb(status: str, page: int = 0, per_page: int = 8):
    conn = db()
    rows = conn.execute(
        "SELECT t.ticket_id, t.user_id, t.updated_at, u.full_name "
        "FROM tickets t LEFT JOIN users u ON u.user_id=t.user_id "
        "WHERE t.status=? ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
        (status, per_page, page * per_page),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE status=?", (status,)
    ).fetchone()[0]
    conn.close()

    buttons = []
    for r in rows:
        name = (r["full_name"] or str(r["user_id"]))[:22]
        buttons.append([InlineKeyboardButton(
            f"#{r['ticket_id']} · {name}",
            callback_data=f"ticket:{r['ticket_id']}",
        )])

    pages = max(1, (total + per_page - 1) // per_page)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹", callback_data=f"list:{status}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("›", callback_data=f"list:{status}:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("← Меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def ticket_view_kb(t):
    btn = (
        InlineKeyboardButton("Закрыть", callback_data=f"close:{t['ticket_id']}")
        if t["status"] == "open" else
        InlineKeyboardButton("Открыть", callback_data=f"reopen:{t['ticket_id']}")
    )
    return InlineKeyboardMarkup([
        [btn],
        [InlineKeyboardButton("← К списку", callback_data=f"list:{t['status']}:0")],
    ])


def render_ticket(tid: int, limit: int = 15):
    conn = db()
    t = conn.execute(
        "SELECT t.*, u.full_name, u.username FROM tickets t "
        "LEFT JOIN users u ON u.user_id=t.user_id WHERE t.ticket_id=?",
        (tid,),
    ).fetchone()
    if not t:
        conn.close()
        return None, None
    msgs = conn.execute(
        "SELECT * FROM messages WHERE ticket_id=? ORDER BY message_id DESC LIMIT ?",
        (tid, limit),
    ).fetchall()[::-1]
    conn.close()

    status = "открыт" if t["status"] == "open" else "закрыт"
    uname = f"@{t['username']}" if t["username"] else ""
    head = (
        f"<b>Тикет #{t['ticket_id']}</b> · {status}\n"
        f"{esc(t['full_name'])} · <code>{t['user_id']}</code> {esc(uname)}"
    )
    lines = [head, "────────────"]
    for m in msgs:
        who = "<b>Оператор</b>" if m["is_operator"] else "<b>Пользователь</b>"
        lines.append(f"{who}\n{esc(m['text'])}")
    text = "\n\n".join(lines).strip()
    if len(text) > 3800:
        text = text[-3800:]
    return text, t


# ---------- Пользователь ----------
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    save_user(user)

    ticket_id = get_or_create_open_ticket(user.id)
    text = message.text or message.caption or "[медиа]"
    add_message(ticket_id, user.id, False, text)

    header = f"<b>Тикет #{ticket_id}</b>\n{esc(user.full_name)} · <code>{user.id}</code>"

    for op_id in OPERATOR_IDS:
        try:
            if message.text:
                sent = await context.bot.send_message(
                    chat_id=op_id,
                    text=f"{header}\n\n{esc(message.text)}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                caption = header
                if message.caption:
                    caption += f"\n\n{esc(message.caption)}"
                sent = await message.copy(
                    chat_id=op_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            link_message(op_id, sent.message_id, ticket_id, user.id)
        except Exception as e:
            log.error(f"send to op {op_id}: {e}")

    await message.reply_text(f"Тикет #{ticket_id} создан. Оператор ответит здесь.")


# ---------- Оператор: свайп-ответ ----------
async def handle_operator_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    link = lookup_link(user.id, message.reply_to_message.message_id)
    if not link:
        await message.reply_text("Не удалось найти тикет для этого сообщения.")
        return

    ticket_id = link["ticket_id"]
    target_uid = link["user_id"]

    t = get_ticket(ticket_id)
    if t and t["status"] != "open":
        await message.reply_text(
            f"Тикет #{ticket_id} закрыт. Откройте: /open {ticket_id}"
        )
        return

    text = message.text or message.caption or "[медиа]"
    add_message(ticket_id, user.id, True, text)

    try:
        if message.text:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"<b>Оператор:</b>\n{esc(message.text)}",
                parse_mode=ParseMode.HTML,
            )
        else:
            caption = "<b>Оператор</b>"
            if message.caption:
                caption += f"\n{esc(message.caption)}"
            await message.copy(
                chat_id=target_uid,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

        # уведомить других операторов
        head = f"<b>Тикет #{ticket_id}</b> · ответ оператора"
        for op_id in OPERATOR_IDS:
            if op_id == user.id:
                continue
            try:
                sent = await context.bot.send_message(
                    chat_id=op_id,
                    text=f"{head}\n\n{esc(text)}",
                    parse_mode=ParseMode.HTML,
                )
                link_message(op_id, sent.message_id, ticket_id, target_uid)
            except Exception:
                pass

        await message.reply_text(f"Отправлено. Тикет #{ticket_id}.")
    except Exception as e:
        log.error(f"reply fail: {e}")
        await message.reply_text(f"Не доставлено: {e}")


# ---------- Команды ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_operator(user.id):
        await update.message.reply_text(
            "Панель оператора.\n/tickets — тикеты\n/stats — статистика\n\n"
            "Чтобы ответить пользователю — свайпните на его сообщение."
        )
    else:
        save_user(user)
        await update.message.reply_text(
            "Здравствуйте. Подробно опишите вашу проблему, и операторы попробуют вам помочь. Спасибо!"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_operator(user.id):
        await update.message.reply_text(
            "/tickets — список тикетов\n"
            "/stats — статистика\n"
            "/close &lt;id&gt; — закрыть\n"
            "/open &lt;id&gt; — открыть\n\n"
            "Ответ — свайпом на сообщение пользователя.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "Просто напишите сообщение — оператор ответит здесь."
        )


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_operator(update.effective_user.id):
        return
    await update.message.reply_text("Тикеты:", reply_markup=menu_kb())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_operator(update.effective_user.id):
        return
    conn = db()
    o = conn.execute("SELECT COUNT(*) FROM tickets WHERE status='open'").fetchone()[0]
    c = conn.execute("SELECT COUNT(*) FROM tickets WHERE status='closed'").fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Открытых: {o}\nЗакрытых: {c}")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_operator(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /close &lt;id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    t = get_ticket(tid)
    if not t:
        await update.message.reply_text("Тикет не найден.")
        return
    set_ticket_status(tid, "closed")
    await update.message.reply_text(f"Тикет #{tid} закрыт.")
    try:
        await context.bot.send_message(t["user_id"], f"Тикет #{tid} закрыт.")
    except Exception:
        pass


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_operator(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /open &lt;id&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    t = get_ticket(tid)
    if not t:
        await update.message.reply_text("Тикет не найден.")
        return
    set_ticket_status(tid, "open")
    await update.message.reply_text(f"Тикет #{tid} открыт.")


# ---------- Callback-роутер ----------
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_operator(update.effective_user.id):
        await q.answer("Нет прав", show_alert=True)
        return
    data = q.data
    await q.answer()

    if data == "noop":
        return

    if data == "menu":
        try:
            await q.edit_message_text("Тикеты:", reply_markup=menu_kb())
        except Exception:
            pass
        return

    if data.startswith("list:"):
        _, status, page = data.split(":")
        page = int(page)
        title = "Открытые" if status == "open" else "Закрытые"
        try:
            await q.edit_message_text(
                f"{title} тикеты:", reply_markup=tickets_list_kb(status, page)
            )
        except Exception:
            pass
        return

    if data.startswith("ticket:"):
        tid = int(data.split(":")[1])
        text, t = render_ticket(tid)
        if not t:
            await q.edit_message_text("Тикет не найден.")
            return
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=ticket_view_kb(t)
            )
        except Exception as e:
            log.error(f"render ticket: {e}")
        return

    if data.startswith(("close:", "reopen:")):
        action, tid_s = data.split(":")
        tid = int(tid_s)
        new_status = "closed" if action == "close" else "open"
        set_ticket_status(tid, new_status)

        text, t = render_ticket(tid)
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=ticket_view_kb(t)
            )
        except Exception:
            pass

        try:
            if new_status == "closed":
                await context.bot.send_message(
                    t["user_id"],
                    f"Тикет #{tid} закрыт. Напишите снова, если понадобится помощь.",
                )
            else:
                await context.bot.send_message(
                    t["user_id"], f"Тикет #{tid} снова открыт."
                )
        except Exception:
            pass
        return


# ---------- Диспетчер личных сообщений ----------
async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if is_operator(user.id):
        if message.reply_to_message:
            await handle_operator_reply(update, context)
        # оператор без свайпа — игнорируем, пусть пользуется /tickets
        return

    await handle_user_message(update, context)


# ---------- Запуск ----------
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help)) 
    app.add_handler(CommandHandler("tickets", cmd_tickets))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private,
    ))

    log.info(f"Бот запущен. Операторов: {len(OPERATOR_IDS)}")
    app.run_polling()


if __name__ == "__main__":
    main()
