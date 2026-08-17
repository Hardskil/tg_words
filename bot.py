import asyncio
import logging
import os

from openai import OpenAI
from psycopg_pool import ConnectionPool
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


# Пул вместо соединения на каждый вызов: один пост открывал до пяти
# коннектов, а с появлением второго сервиса общий лимит max_connections
# у Postgres стал реальным ограничением.
# timeout задан явно: в отличие от psycopg.connect(), который падал сразу,
# пул ждёт свободного соединения. Ожидание полезно — на Railway Postgres
# поднимается не мгновенно, — но дефолтные 30 секунд молчания при старте
# слишком долгие, чтобы понять, что происходит.
db_pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=5, timeout=15, name="bot", open=False
)


def get_db_connection():
    """Соединение из пула. На выходе транзакция коммитится, как и раньше."""
    return db_pool.connection()


# Отдельный мьютекс на чат. Раньше блокирующий I/O случайно выстраивал
# обработку в очередь; теперь вызовы ушли в потоки, и без него два
# одновременных «!v слово» прошли бы проверку дубля оба и создали две карточки.
_chat_locks: dict[int, asyncio.Lock] = {}


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    # Гонки при создании самого лока нет: функция синхронная, между
    # чтением и записью нет await, а event loop однопоточный.
    lock = _chat_locks.get(chat_id)

    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock

    return lock


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
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (normalized_word, chat_id)
                );
            """)

            cur.execute("""
                ALTER TABLE vocab_words
                ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_chats (
                    chat_id BIGINT PRIMARY KEY,
                    chat_type TEXT,
                    title TEXT,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            conn.commit()


def save_chat_info(chat) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_chats
                (chat_id, chat_type, title, username, first_name, last_name, is_active, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                ON CONFLICT (chat_id) DO UPDATE SET
                    chat_type = EXCLUDED.chat_type,
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    is_active = TRUE,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    chat.id,
                    chat.type,
                    getattr(chat, "title", None),
                    getattr(chat, "username", None),
                    getattr(chat, "first_name", None),
                    getattr(chat, "last_name", None),
                ),
            )
            conn.commit()


def find_word(normalized_word: str, chat_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT word, message_id
                FROM vocab_words
                WHERE normalized_word = %s
                AND chat_id = %s
                AND is_active = TRUE
                LIMIT 1;
                """,
                (normalized_word, chat_id),
            )
            return cur.fetchone()


def save_word(normalized_word: str, word: str, message_id: int, chat_id: int, source: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vocab_words
                (normalized_word, word, message_id, chat_id, source, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (normalized_word, chat_id) DO UPDATE SET
                    word = EXCLUDED.word,
                    message_id = EXCLUDED.message_id,
                    source = EXCLUDED.source,
                    is_active = TRUE;
                """,
                (normalized_word, word, message_id, chat_id, source),
            )
            conn.commit()


def delete_word(normalized_word: str, chat_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE vocab_words
                SET is_active = FALSE
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
    chat = update.effective_chat

    if not message or not message.text or not chat:
        return

    # Запросы к БД и к OpenAI синхронные и блокирующие. Вызванные напрямую,
    # они останавливали весь event loop — пока ждали модель, бот не обрабатывал
    # вообще ничего. Поэтому каждый уходит в отдельный поток.
    await asyncio.to_thread(save_chat_info, chat)

    chat_id = chat.id
    text = message.text.strip()

    if not text.lower().startswith("!v "):
        normalized_text = normalize_word(text)

        if normalized_text:
            await asyncio.to_thread(
                save_word,
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

    # Обработка «!v» под замком: проверка дубля и запись должны быть неделимы.
    async with get_chat_lock(chat_id):
        normalized_word = normalize_word(word)
        existing_word = await asyncio.to_thread(find_word, normalized_word, chat_id)

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
                await asyncio.to_thread(delete_word, normalized_word, chat_id)

        data = await asyncio.to_thread(ai_process, word)

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

        await asyncio.to_thread(
            save_word,
            normalized_word=normalized_word,
            word=data["word"],
            message_id=sent_message.message_id,
            chat_id=chat_id,
            source="bot_vocab",
        )

        await asyncio.to_thread(
            save_word,
            normalized_word=normalize_word(data["word"]),
            word=data["word"],
            message_id=sent_message.message_id,
            chat_id=chat_id,
            source="bot_vocab",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка при обработке update:", exc_info=context.error)


async def start_sat_sync(application) -> None:
    """Запускает фоновую синхронизацию SAT-витрины.

    Импорт внутри функции и широкий except — намеренно: витрина не должна
    мешать словарю. Если пакет sat не настроен или Google недоступен,
    бот обязан продолжить работу как обычно.
    """
    try:
        from sat.sync import start

        start()
        logger.info("Синхронизация SAT-витрины запущена")
    except Exception:
        logger.exception("Синхронизация SAT не запустилась, словарь работает как обычно")


def main() -> None:
    # Пул создан с open=False, чтобы импорт модуля не лез в базу.
    db_pool.open()

    init_db()

    # post_init, а не вызов до run_polling: таск нужно создавать
    # внутри уже работающего event loop.
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(start_sat_sync).build()

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