import logging
import os

import psycopg
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

print("OPENAI_API_KEY exists:", bool(OPENAI_API_KEY))
print("TELEGRAM_BOT_TOKEN exists:", bool(TELEGRAM_BOT_TOKEN))
print("DATABASE_URL exists:", bool(DATABASE_URL))

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Не найдена переменная окружения TELEGRAM_BOT_TOKEN")

if not OPENAI_API_KEY:
    raise ValueError("Не найдена переменная окружения OPENAI_API_KEY")

if not DATABASE_URL:
    raise ValueError("Не найдена переменная окружения DATABASE_URL")

client = OpenAI(api_key=OPENAI_API_KEY)


def normalize_word(word: str) -> str:
    return " ".join(word.casefold().split())


def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def init_db() -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vocab_words (
                    id SERIAL PRIMARY KEY,
                    normalized_word TEXT NOT NULL,
                    word TEXT NOT NULL,
                    message_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    source TEXT DEFAULT 'bot_vocab',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (normalized_word, chat_id)
                );
            """)
            conn.commit()


def find_word(normalized_word: str, chat_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT word, message_id
                FROM vocab_words
                WHERE normalized_word = %s AND chat_id = %s
                LIMIT 1;
                """,
                (normalized_word, chat_id),
            )
            return cur.fetchone()


def save_word(
    normalized_word: str,
    word: str,
    message_id: int,
    chat_id: int,
    source: str,
) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vocab_words
                (normalized_word, word, message_id, chat_id, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (normalized_word, chat_id) DO NOTHING;
                """,
                (normalized_word, word, message_id, chat_id, source),
            )
            conn.commit()


def delete_word(normalized_word: str, chat_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM vocab_words
                WHERE normalized_word = %s AND chat_id = %s;
                """,
                (normalized_word, chat_id),
            )
            conn.commit()


async def delete_message(message) -> None:
    try:
        await message.delete()
    except Exception as e:
        print(f"Не удалось удалить сообщение: {e}")


def ai_process(word: str) -> dict | None:
    try:
        response = client.responses.create(
            model="gpt-5-mini",
            input=f"""
For the word: {word}

Do all of the following:
1. Give a short and clear English definition.
2. Translate the word into Russian.
3. Translate the English definition into Russian.

Return STRICTLY in this format:

word: ...
definition: ...
translation: ...
definition_ru: ...
""",
        )

        text = response.output_text.strip()
        result = {}

        for line in text.splitlines():
            line = line.strip()

            if line.startswith("word:"):
                result["word"] = line.replace("word:", "", 1).strip()
            elif line.startswith("definition:"):
                result["definition"] = line.replace("definition:", "", 1).strip()
            elif line.startswith("translation:"):
                result["translation"] = line.replace("translation:", "", 1).strip()
            elif line.startswith("definition_ru:"):
                result["definition_ru"] = line.replace("definition_ru:", "", 1).strip()

        required_keys = ["word", "definition", "translation", "definition_ru"]

        if not all(key in result for key in required_keys):
            print("AI вернул неполный ответ:", text)
            return None

        return result

    except Exception as e:
        print("Ошибка AI:", e)
        return None


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if not message or not message.text:
        return

    chat_id = update.effective_chat.id
    text = message.text.strip()

    if not text.lower().startswith("!v "):
        normalized_text = normalize_word(text)

        if normalized_text:
            save_word(
                normalized_word=normalized_text,
                word=text,
                message_id=message.message_id,
                chat_id=chat_id,
                source="chat_message",
            )

        return

    word = text[3:].strip()

    if not word:
        return

    normalized_word = normalize_word(word)
    existing_word = find_word(normalized_word, chat_id)

    if existing_word:
        display_word, old_message_id = existing_word

        await delete_message(message)

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Слово «{display_word}» уже есть в списке.",
                reply_to_message_id=old_message_id,
                allow_sending_without_reply=False,
            )
            return

        except Exception as e:
            print(f"Старое сообщение не найдено, удаляю слово из БД: {e}")
            delete_word(normalized_word, chat_id)

    data = ai_process(word)

    if not data:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не удалось обработать слово через AI.",
        )
        return

    result_text = (
        f"#vocabболь\n\n"
        f"📌 {data['word']} — {data['translation']}\n\n"
        f"Definition: {data['definition']}\n"
        f"Перевод: {data['definition_ru']}"
    )

    await delete_message(message)

    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        text=result_text,
    )

    save_word(
        normalized_word=normalized_word,
        word=data["word"],
        message_id=sent_message.message_id,
        chat_id=chat_id,
        source="bot_vocab",
    )

    save_word(
        normalized_word=normalize_word(data["word"]),
        word=data["word"],
        message_id=sent_message.message_id,
        chat_id=chat_id,
        source="bot_vocab",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка при обработке update:", exc_info=context.error)


def main() -> None:
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.CHANNEL, handle_channel_post)
    )

    app.add_error_handler(error_handler)

    print("Бот запущен...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()