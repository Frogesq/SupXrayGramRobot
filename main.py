import os
import html
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
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
AUTO_CLOSE_HOURS = int(os.getenv("AUTO_CLOSE_HOURS", "24"))

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv("DB_PATH") or str(DATA_DIR / "support_bot.sqlite3")
LOG_PATH = DATA_DIR / "bot.log"


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


# Ранги
OWNER_IDS = parse_ids(os.getenv("OWNER_IDS", "") or os.getenv("OWNER_ID", ""))
OPERATOR_L3 = parse_ids(os.getenv("OPERATOR_L3", ""))
OPERATOR_L2 = parse_ids(os.getenv("OPERATOR_L2", ""))
OPERATOR_L1 = parse_ids(os.getenv("OPERATOR_L1", ""))

# Обратная совместимость со старым OPERATOR_IDS
_old_ops = parse_ids(os.getenv("OPERATOR_IDS", ""))
if _old_ops and not (OPERATOR_L1 or OPERATOR_L2 or OPERATOR_L3):
    OPERATOR_L2 = _old_ops

ALL_OPERATOR_IDS = list(set(OWNER_IDS + OPERATOR_L3 + OPERATOR_L2 + OPERATOR_L1))

if not ALL_OPERATOR_IDS:
    raise ValueError("Не задан ни один оператор/владелец (OWNER_IDS / OPERATOR_L1/L2/L3)")

# Права по рангам
ROLE_PERMS = {
    "owner": {"reply", "view", "close", "stats", "ban", "banlist"},
    "l3":    {"reply", "view", "close", "stats", "ban", "banlist"},
    "l2":    {"reply", "view", "close", "stats"},
    "l1":    {"reply", "view"},
}

ROLE_NAMES = {
    "owner": "Владелец",
    "l3": "Оператор 3 уровня",
    "l2": "Оператор 2 уровня",
    "l1": "Оператор 1 уровня",
}


def get_role(uid: int) -> str | None:
    if uid in OWNER_IDS:
        return "owner"
    if uid in OPERATOR_L3:
        return "l3"
    if uid in OPERATOR_L2:
        return "l2"
    if uid in OPERATOR_L1:
        return "l1"
    return None


def is_operator(uid: int) -> bool:
    return uid in ALL_OPERATOR_IDS


def has_perm(uid: int, perm: str) -> bool:
    role = get_role(uid)
    if not role:
        return False
    return perm in ROLE_PERMS.get(role, set())


logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)
log.info(f"DB: {DB_PATH}")
log.info(
    f"Ранги: Owner={len(OWNER_IDS)}, L3={len(OPERATOR_L3)}, "
    f"L2={len(OPERATOR_L2)}, L1={len(OPERATOR_L1)}"
)


# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
        assigned_to INTEGER,
        rating      INTEGER,
        rated_at    TEXT,
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
    CREATE TABLE IF NOT EXISTS bans (
        user_id     INTEGER PRIMARY KEY,
        banned_at   TEXT NOT NULL,
        banned_by   INTEGER,
        reason      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
    CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id);
    """)
    for ddl in (
        "ALTER TABLE tickets ADD COLUMN assigned_to INTEGER",
        "ALTER TABLE tickets ADD COLUMN rating INTEGER",
        "ALTER TABLE tickets ADD COLUMN rated_at TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def esc(s: str | None) -> str:
    return html.escape(s or "")


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


def has_open_ticket(user_id: int) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM tickets WHERE user_id=? AND status='open' LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row is not None


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


def set_rating(tid: int, score: int) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT rating FROM tickets WHERE ticket_id=?", (tid,)
    ).fetchone()
    if not row or row["rating"] is not None:
        conn.close()
        return False
    conn.execute(
        "UPDATE tickets SET rating=?, rated_at=? WHERE ticket_id=?",
        (score, now(), tid),
    )
    conn.commit()
    conn.close()
    return True


def find_stale_open_tickets(hours: int) -> list:
    threshold = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    ).isoformat(timespec="seconds")
    conn = db()
    rows = conn.execute(
        "SELECT ticket_id, user_id FROM tickets "
        "WHERE status='open' AND updated_at < ?",
        (threshold,),
    ).fetchall()
    conn.close()
    return rows


# ---------- Ban helpers ----------
def is_banned(user_id: int) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM bans WHERE user_id=? LIMIT 1", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def ban_user(user_id: int, banned_by: int, reason: str | None = None) -> bool:
    if is_banned(user_id):
        return False
    conn = db()
    conn.execute(
        "INSERT INTO bans(user_id, banned_at, banned_by, reason) VALUES (?, ?, ?, ?)",
        (user_id, now(), banned_by, reason),
    )
    conn.execute(
        "UPDATE tickets SET status='closed', closed_at=? "
        "WHERE user_id=? AND status='open'",
        (now(), user_id),
    )
    conn.commit()
    conn.close()
    return True


def unban_user(user_id: int) -> bool:
    conn = db()
    cur = conn.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def get_ban_info(user_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM bans WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def list_bans(limit: int = 50):
    conn = db()
    rows = conn.execute(
        "SELECT b.*, u.full_name, u.username "
        "FROM bans b LEFT JOIN users u ON u.user_id = b.user_id "
        "ORDER BY b.banned_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


# ---------- Клавиатуры ----------
def menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Открытые", callback_data="list:open:0"),
        InlineKeyboardButton("Закрытые", callback_data="list:closed:0"),
    ]])


def tickets_list_kb(status: str, page: int = 0, per_page: int = 8):
    conn = db()
    rows = conn.execute(
        "SELECT t.ticket_id, t.user_id, t.updated_at, t.rating, u.full_name "
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
        mark = f" [{r['rating']}/5]" if r["rating"] else ""
        buttons.append([InlineKeyboardButton(
            f"#{r['ticket_id']} · {name}{mark}",
            callback_data=f"ticket:{r['ticket_id']}",
        )])
    pages = max(1, (total + per_page - 1) // per_page)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("<", callback_data=f"list:{status}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton(">", callback_data=f"list:{status}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("Меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def ticket_view_kb(t, uid: int):
    buttons = []
    if has_perm(uid, "close"):
        btn = (
            InlineKeyboardButton("Закрыть", callback_data=f"close:{t['ticket_id']}")
            if t["status"] == "open" else
            InlineKeyboardButton("Открыть", callback_data=f"reopen:{t['ticket_id']}")
        )
        buttons.append([btn])
    buttons.append([InlineKeyboardButton("К списку", callback_data=f"list:{t['status']}:0")])
    return InlineKeyboardMarkup(buttons)


def new_ticket_kb(ticket_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть карточку", callback_data=f"ticket:{ticket_id}"),
        InlineKeyboardButton("Закрыть", callback_data=f"close:{ticket_id}"),
    ]])


def rating_kb(ticket_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("1", callback_data=f"rate:{ticket_id}:1"),
        InlineKeyboardButton("2", callback_data=f"rate:{ticket_id}:2"),
        InlineKeyboardButton("3", callback_data=f"rate:{ticket_id}:3"),
        InlineKeyboardButton("4", callback_data=f"rate:{ticket_id}:4"),
        InlineKeyboardButton("5", callback_data=f"rate:{ticket_id}:5"),
    ]])


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
    if t["rating"]:
        lines.append(f"────────────\nОценка пользователя: <b>{t['rating']}/5</b>")
    text = "\n\n".join(lines).strip()
    if len(text) > 3800:
        text = text[-3800:]
    return text, t


# ---------- Закрытие + оценка ----------
async def close_ticket_and_notify(bot, ticket_id: int, reason: str = "manual"):
    set_ticket_status(ticket_id, "closed")
    t = get_ticket(ticket_id)
    if not t:
        return
    try:
        if reason == "auto":
            await bot.send_message(
                t["user_id"],
                f"Тикет #{ticket_id} закрыт автоматически из-за неактивности.\n"
                "Если нужна помощь — просто напишите новое сообщение.\n\n"
                "Оцените качество поддержки:",
                reply_markup=rating_kb(ticket_id),
            )
        else:
            await bot.send_message(
                t["user_id"],
                f"Тикет #{ticket_id} закрыт.\n\nОцените качество поддержки:",
                reply_markup=rating_kb(ticket_id),
            )
    except Exception as e:
        log.error(f"close notify {ticket_id}: {e}")


# ---------- Пользователь ----------
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    save_user(user)

    if is_banned(user.id):
        ban = get_ban_info(user.id)
        reason = ban["reason"] if ban and ban["reason"] else "не указана"
        await message.reply_text(
            f"Вы заблокированы в боте поддержки.\n"
            f"Причина: {esc(reason)}\n\n"
            "Если считаете, что это ошибка — обратитесь к администрации другим способом.",
            parse_mode=ParseMode.HTML,
        )
        return

    is_new = not has_open_ticket(user.id)
    ticket_id = get_or_create_open_ticket(user.id)
    text = message.text or message.caption or "[медиа]"
    add_message(ticket_id, user.id, False, text)

    header = (
        f"<b>Тикет #{ticket_id}</b>"
        + (" · <b>NEW</b>" if is_new else "")
        + f"\n{esc(user.full_name)} · <code>{user.id}</code>"
    )
    kb = new_ticket_kb(ticket_id) if is_new else None

    for op_id in ALL_OPERATOR_IDS:
        try:
            if message.text:
                sent = await context.bot.send_message(
                    chat_id=op_id,
                    text=f"{header}\n\n{esc(message.text)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            else:
                caption = header
                if message.caption:
                    caption += f"\n\n{esc(message.caption)}"
                sent = await message.copy(
                    chat_id=op_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            link_message(op_id, sent.message_id, ticket_id, user.id)
        except Exception as e:
            log.error(f"send to op {op_id}: {e}")

    if is_new:
        await message.reply_text(
            f"<b>Тикет #{ticket_id} создан</b>\n\n"
            "Оператор скоро ответит прямо здесь.\n"
            "Если нужно что-то добавить — просто напишите ещё сообщение.",
            parse_mode=ParseMode.HTML,
        )


# ---------- Оператор: свайп-ответ ----------
async def handle_operator_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not has_perm(user.id, "reply"):
        await message.reply_text("У вас нет прав отвечать на тикеты.")
        return

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
        head = f"<b>Тикет #{ticket_id}</b> · ответ оператора"
        for op_id in ALL_OPERATOR_IDS:
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
    role = get_role(user.id)

    if role:
        role_name = ROLE_NAMES.get(role, role)
        lines = [
            f"Панель оператора · <b>{role_name}</b>\n",
            "/tickets — тикеты",
        ]
        if has_perm(user.id, "stats"):
            lines.append("/stats — статистика")
        if has_perm(user.id, "close"):
            lines.append("/close &lt;id&gt; — закрыть тикет")
            lines.append("/open &lt;id&gt; — открыть тикет")
        if has_perm(user.id, "ban"):
            lines.append("/ban &lt;id&gt; [причина] — забанить")
            lines.append("/unban &lt;id&gt; — разбанить")
        if has_perm(user.id, "banlist"):
            lines.append("/banlist — список банов")
        lines.append("\nЧтобы ответить пользователю — свайпните на его сообщение.")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    else:
        if is_banned(user.id):
            ban = get_ban_info(user.id)
            reason = ban["reason"] if ban and ban["reason"] else "не указана"
            await update.message.reply_text(
                f"Вы заблокированы в боте поддержки.\nПричина: {esc(reason)}",
                parse_mode=ParseMode.HTML,
            )
            return
        save_user(user)
        await update.message.reply_text(
            "Здравствуйте. Подробно опишите вашу проблему, "
            "и операторы попробуют вам помочь. Спасибо!"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = get_role(user.id)

    if not role:
        await update.message.reply_text(
            "Просто напишите сообщение — оператор ответит здесь."
        )
        return

    role_name = ROLE_NAMES.get(role, role)
    lines = [f"<b>{role_name}</b>\n"]
    lines.append("/tickets — список тикетов")
    if has_perm(user.id, "stats"):
        lines.append("/stats — статистика")
    if has_perm(user.id, "close"):
        lines.append("/close &lt;id&gt; — закрыть тикет")
        lines.append("/open &lt;id&gt; — открыть тикет")
    if has_perm(user.id, "ban"):
        lines.append("/ban &lt;id&gt; [причина] — забанить")
        lines.append("/unban &lt;id&gt; — разбанить")
    if has_perm(user.id, "banlist"):
        lines.append("/banlist — список забаненных")
    lines.append("\nОтвет — свайпом на сообщение пользователя.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "view"):
        return
    await update.message.reply_text("Тикеты:", reply_markup=menu_kb())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "stats"):
        await update.message.reply_text("Недостаточно прав.")
        return
    conn = db()
    o = conn.execute("SELECT COUNT(*) FROM tickets WHERE status='open'").fetchone()[0]
    c = conn.execute("SELECT COUNT(*) FROM tickets WHERE status='closed'").fetchone()[0]
    r = conn.execute(
        "SELECT AVG(rating), COUNT(rating) FROM tickets WHERE rating IS NOT NULL"
    ).fetchone()
    bans_count = conn.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
    conn.close()
    avg = f"{r[0]:.2f}" if r[0] else "—"
    text = (
        f"Открытых: {o}\n"
        f"Закрытых: {c}\n"
        f"Средняя оценка: {avg} ({r[1]} оценок)\n"
        f"Забанено: {bans_count}"
    )
    await update.message.reply_text(text)


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "close"):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: /close &lt;id&gt;", parse_mode=ParseMode.HTML
        )
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
    await close_ticket_and_notify(context.bot, tid, reason="manual")
    await update.message.reply_text(f"Тикет #{tid} закрыт.")


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "close"):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: /open &lt;id&gt;", parse_mode=ParseMode.HTML
        )
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


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "ban"):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: /ban &lt;user_id&gt; [причина]",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    if is_operator(uid):
        await update.message.reply_text("Нельзя забанить оператора/владельца.")
        return

    reason = " ".join(context.args[1:]).strip() or None
    was_banned = ban_user(uid, update.effective_user.id, reason)

    if not was_banned:
        await update.message.reply_text(
            f"Пользователь <code>{uid}</code> уже забанен.", parse_mode=ParseMode.HTML
        )
        return

    try:
        reason_text = reason or "не указана"
        await context.bot.send_message(
            uid,
            f"Вы заблокированы в боте поддержки.\nПричина: {esc(reason_text)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    reason_info = f"\nПричина: {esc(reason)}" if reason else ""
    await update.message.reply_text(
        f"Пользователь <code>{uid}</code> забанен.{reason_info}\n"
        f"Все его открытые тикеты закрыты.",
        parse_mode=ParseMode.HTML,
    )
    log.info(f"Ban: {uid} by {update.effective_user.id}, reason={reason}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "ban"):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text(
            "Использование: /unban &lt;user_id&gt;",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    if not unban_user(uid):
        await update.message.reply_text(
            f"Пользователь <code>{uid}</code> не был забанен.", parse_mode=ParseMode.HTML
        )
        return

    try:
        await context.bot.send_message(
            uid,
            "Вы разблокированы в боте поддержки. Можете снова писать сообщения.",
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"Пользователь <code>{uid}</code> разбанен.",
        parse_mode=ParseMode.HTML,
    )
    log.info(f"Unban: {uid} by {update.effective_user.id}")


async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_perm(update.effective_user.id, "banlist"):
        await update.message.reply_text("Недостаточно прав.")
        return
    rows = list_bans(40)
    if not rows:
        await update.message.reply_text("Список банов пуст.")
        return

    lines = ["<b>Забаненные пользователи:</b>\n"]
    for r in rows:
        name = r["full_name"] or "—"
        uname = f"@{r['username']}" if r["username"] else ""
        reason = r["reason"] or "—"
        lines.append(
            f"• <code>{r['user_id']}</code> {esc(name)} {esc(uname)}\n"
            f"  Причина: {esc(reason)}\n"
            f"  Дата: {r['banned_at'][:16]}"
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------- Callback: оценка (только пользователь) ----------
async def user_cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if not data.startswith("rate:"):
        return
    _, tid_s, score_s = data.split(":")
    tid = int(tid_s)
    score = int(score_s)
    t = get_ticket(tid)
    if not t:
        await q.answer("Тикет не найден", show_alert=True)
        return
    if t["user_id"] != q.from_user.id:
        await q.answer("Это не ваш тикет.", show_alert=True)
        return
    if not set_rating(tid, score):
        await q.answer("Оценка уже получена. Спасибо!", show_alert=True)
        return
    await q.answer("Спасибо за оценку!")
    try:
        await q.edit_message_text(f"Тикет #{tid} закрыт. Оценка: {score}/5. Спасибо!")
    except Exception:
        pass


# ---------- Callback: оператор ----------
async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id

    if not is_operator(uid):
        await q.answer("Нет прав", show_alert=True)
        return

    data = q.data
    await q.answer()

    if data == "noop":
        return

    if data == "menu":
        if not has_perm(uid, "view"):
            return
        try:
            await q.edit_message_text("Тикеты:", reply_markup=menu_kb())
        except Exception:
            pass
        return

    if data.startswith("list:"):
        if not has_perm(uid, "view"):
            return
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
        if not has_perm(uid, "view"):
            return
        tid = int(data.split(":")[1])
        text, t = render_ticket(tid)
        if not t:
            try:
                await q.edit_message_text("Тикет не найден.")
            except Exception:
                pass
            return
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=ticket_view_kb(t, uid)
            )
        except Exception as e:
            log.error(f"render ticket: {e}")
        return

    if data.startswith("close:"):
        if not has_perm(uid, "close"):
            await q.answer("Недостаточно прав", show_alert=True)
            return
        tid = int(data.split(":")[1])
        t = get_ticket(tid)
        if not t:
            return
        await close_ticket_and_notify(context.bot, tid, reason="manual")
        text, t = render_ticket(tid)
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=ticket_view_kb(t, uid)
            )
        except Exception:
            pass
        return

    if data.startswith("reopen:"):
        if not has_perm(uid, "close"):
            await q.answer("Недостаточно прав", show_alert=True)
            return
        tid = int(data.split(":")[1])
        set_ticket_status(tid, "open")
        text, t = render_ticket(tid)
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=ticket_view_kb(t, uid)
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                t["user_id"], f"Тикет #{tid} снова открыт."
            )
        except Exception:
            pass
        return


# ---------- Авто-закрытие ----------
async def auto_close_job(context: ContextTypes.DEFAULT_TYPE):
    rows = find_stale_open_tickets(AUTO_CLOSE_HOURS)
    if not rows:
        return
    log.info(f"Авто-закрытие: {len(rows)} тикет(ов)")
    for r in rows:
        await close_ticket_and_notify(context.bot, r["ticket_id"], reason="auto")


# ---------- Диспетчер личных сообщений ----------
async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if is_operator(user.id):
        if message.reply_to_message:
            await handle_operator_reply(update, context)
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
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("banlist", cmd_banlist))

    app.add_handler(CallbackQueryHandler(user_cb_router, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private,
    ))

    if app.job_queue:
        app.job_queue.run_repeating(auto_close_job, interval=1800, first=60)
    else:
        log.warning("JobQueue недоступен — авто-закрытие отключено")

    log.info(f"Бот запущен. Операторов всего: {len(ALL_OPERATOR_IDS)}")
    app.run_polling()


if __name__ == "__main__":
    main()
