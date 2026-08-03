import re
import os
import logging
import aiosqlite
from typing import List, Optional, Dict

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "phones.db"

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

PHONE_RE = re.compile(r"(?<!\w)((?:\+?7|8)[\d\(\)\-\s]{10,20})(?!\w)")


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def normalize_phone(raw: str) -> Optional[str]:
    digits = digits_only(raw)

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]

    if len(digits) != 11 or not digits.startswith("7"):
        return None

    return digits


def extract_phones(text: str) -> List[Dict[str, str]]:
    result = []
    seen = set()

    for raw in PHONE_RE.findall(text or ""):
        normalized = normalize_phone(raw)
        if not normalized:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        result.append({
            "key": normalized,
            "display": "+7" + normalized[1:]
        })

    return result


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS phone_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    phone_key TEXT NOT NULL,
    display_phone TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT,
    full_name TEXT,
    message_text TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_phone ON phone_mentions(chat_id, phone_key);"
        )
        await db.commit()


async def phone_exists_in_chat(chat_id: int, phone_key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id
            FROM phone_mentions
            WHERE chat_id = ? AND phone_key = ?
            LIMIT 1
            """,
            (chat_id, phone_key),
        )
        row = await cursor.fetchone()
        return row is not None


async def save_phone(
    chat_id: int,
    phone_key: str,
    display_phone: str,
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
                chat_id, phone_key, display_phone, message_id,
                user_id, username, full_name, message_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                phone_key,
                display_phone,
                message_id,
                user_id,
                username,
                full_name,
                message_text,
            ),
        )
        await db.commit()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Бот запущен. Отправь номер телефона — я проверю, дубль он или нет."
        )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if not message or not message.text:
        return

    phones = extract_phones(message.text)

    if not phones:
        return

    user = message.from_user
    replies = []

    for phone in phones:
        phone_key = phone["key"]
        display_phone = phone["display"]

        exists = await phone_exists_in_chat(message.chat_id, phone_key)

        if exists:
            replies.append(f"🚨 Дубль: {display_phone}")
        else:
            await save_phone(
                chat_id=message.chat_id,
phone_key=phone_key,
                display_phone=display_phone,
                message_id=message.message_id,
                user_id=user.id if user else None,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                message_text=message.text,
            )
            replies.append(f"✅ Не дубль: {display_phone}")

    await message.reply_text("\n".join(replies))


async def post_init(application: Application) -> None:
    await init_db()
    logging.info("База данных готова.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
