import re
import logging
import aiosqlite
from typing import List, Dict, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = "8799600206:AAGBPTbpC_NcM0SIRHoCFuHf62DyqwPPwKs"
DB_PATH = "phones.db"
MIN_PHONE_DIGITS = 7

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PHONE_CANDIDATE_RE = re.compile(
    r"""
    (?<!\w)
    (
        (?:\+?7|8)
        [\d\(\)\-\s]{10,20}
    )
    (?!\w)
    """,
    re.VERBOSE,
)
def extract_phone_candidates(text: str) -> List[str]:
    if not text:
        return []
    return PHONE_CANDIDATE_RE.findall(text)

def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)

def normalize_phone(raw: str) -> Optional[Dict[str, str]]:
    if not raw:
        return None

    raw = raw.strip()
    d = digits_only(raw)

    # делаем строгий RU формат
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]

    # принимаем только RU номера
    if len(d) != 11 or not d.startswith("7"):
        return None

    strict_key = d
    fuzzy_key = d

    return {
        "display": "+7" + d[1:],
        "strict_key": strict_key,
        "fuzzy_key": fuzzy_key,
    }

def extract_normalized_phones(text: str) -> List[Dict[str, str]]:
    candidates = extract_phone_candidates(text)
    result = []
    seen = set()

    for candidate in candidates:
        normalized = normalize_phone(candidate)
        if not normalized:
            continue

        dedupe_key = (normalized["strict_key"], normalized["fuzzy_key"])
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        result.append(normalized)

    return result

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS phone_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strict_key TEXT NOT NULL,
    fuzzy_key TEXT NOT NULL,
    display_phone TEXT NOT NULL,
    raw_phone TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_type TEXT,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT,
    full_name TEXT,
    message_text TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_phone_mentions_strict_key ON phone_mentions(strict_key);",
    "CREATE INDEX IF NOT EXISTS idx_phone_mentions_fuzzy_key ON phone_mentions(fuzzy_key);",
]

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        for sql in CREATE_INDEXES_SQL:
            await db.execute(sql)
        await db.commit()

async def save_phone_mention(
    strict_key: str,
    fuzzy_key: str,
    display_phone: str,
    raw_phone: str,
    chat_id: int,
    chat_type: str,
    message_id: int,
    user_id: Optional[int],
    username: Optional[str],
    full_name: Optional[str],
    message_text: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO phone_mentions (
                strict_key, fuzzy_key, display_phone, raw_phone,
                chat_id, chat_type, message_id, user_id, username, full_name, message_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strict_key,
                fuzzy_key,
                display_phone,
                raw_phone,
                chat_id,
                chat_type,
                message_id,
                user_id,
                username,
                full_name,
                message_text,
            ),
        )
        await db.commit()

async def find_exact_duplicates(strict_key: str, current_chat_id: int, current_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT display_phone, chat_id, message_id, username, full_name, created_at
FROM phone_mentions
            WHERE strict_key = ?
            AND chat_id = ?
            AND NOT (chat_id = ? AND message_id = ?)
            ORDER BY id ASC
            LIMIT 1000
            """,
            (strict_key, current_chat_id, current_chat_id, current_message_id),
        )
        return await cursor.fetchall()

async def find_fuzzy_duplicates(fuzzy_key: str, strict_key: str, current_chat_id: int, current_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT display_phone, strict_key, chat_id, message_id, username, full_name, created_at
            FROM phone_mentions
            WHERE fuzzy_key = ?
              AND chat_id = ?
              AND strict_key != ?
              AND NOT (chat_id = ? AND message_id = ?)
            ORDER BY id ASC
            LIMIT 1000
            """,
            (fuzzy_key, current_chat_id, strict_key, current_chat_id, current_message_id),
        )
        return await cursor.fetchall()

def format_user(username: Optional[str], full_name: Optional[str]) -> str:
    if username:
        return f"@{username}"
    if full_name:
        return full_name
    return "unknown"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот запущен. Просто отправь в чат сообщение с номером телефона."
    )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text
    found_phones = extract_normalized_phones(text)

    if not found_phones:
        return

    user = message.from_user
    username = user.username if user else None
    full_name = user.full_name if user else None
    user_id = user.id if user else None
    chat = message.chat
    chat_type = chat.type if chat else None

    replies = []

    for item in found_phones:
        strict_key = item["strict_key"]
        fuzzy_key = item["fuzzy_key"]
        display_phone = item["display"]

        exact_matches = await find_exact_duplicates(strict_key, message.chat_id, message.message_id)
        fuzzy_matches = await find_fuzzy_duplicates(fuzzy_key, strict_key, message.chat_id, message.message_id)

        await save_phone_mention(
            strict_key=strict_key,
            fuzzy_key=fuzzy_key,
            display_phone=display_phone,
            raw_phone=display_phone,
            chat_id=message.chat_id,
            chat_type=chat_type,
            message_id=message.message_id,
            user_id=user_id,
            username=username,
            full_name=full_name,
            message_text=text,
        )

        if exact_matches:
            replies.append(f"🚨 Дубль: {display_phone}")
        else:
            replies.append(f"✅ Не дубль: {display_phone}")

    if replies:
        await message.reply_text("\n\n".join(replies))

async def post_init(application: Application) -> None:
    await init_db()
    logger.info("База данных готова.")

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
