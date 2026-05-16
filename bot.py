import json
import os
from pathlib import Path

from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

print("OPENAI_API_KEY exists:", bool(os.getenv("OPENAI_API_KEY")))
print("TELEGRAM_BOT_TOKEN exists:", bool(os.getenv("TELEGRAM_BOT_TOKEN")))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WORDS_INDEX_FILE = Path("vocab_words.json")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Не найдена переменная окружения TELEGRAM_BOT_TOKEN")

if not OPENAI_API_KEY:
    raise ValueError("Не найдена переменная окружения OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


def normalize_word(word: str) -> str:
    return " ".join(word.casefold().split())


def load_words_index() -> dict:
    if not WORDS_INDEX_FILE.exists():
        return {}

    try:
        with WORDS_INDEX_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as e:
        print("Не удалось прочитать индекс слов:", e)
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def save_words_index(words_index: dict) -> None:
    try:
        with WORDS_INDEX_FILE.open("w", encoding="utf-8") as file:
            json.dump(words_index, file, ensure_ascii=False, indent=2)
    except OSError as e:
        print("Не удалось сохранить индекс слов:", e)


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

    text = message.text.strip()

    if not text.lower().startswith("!v "):
        return

    word = text[3:].strip()
    if not word:
        return

    words_index = load_words_index()
    normalized_word = normalize_word(word)

    if normalized_word in words_index:
        saved_word = words_index[normalized_word]
        reply_to_message_id = saved_word.get("message_id")
        display_word = saved_word.get("word", word)

        await delete_message(message)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Слово «{display_word}» уже есть в списке.",
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
        )
        return

    data = ai_process(word)
    if not data:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Не удалось обработать слово через AI.",
        )
        return

    result_text = (
        f"#vocabболь\n\n"
        f"📌 {data['word']} — {data['translation']}\n\n"
        f" Definition: {data['definition']}\n"
        f" Перевод: {data['definition_ru']}"
    )

    await delete_message(message)

    sent_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=result_text,
    )

    words_index[normalized_word] = {
        "word": data["word"],
        "message_id": sent_message.message_id,
    }
    save_words_index(words_index)


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.CHANNEL, handle_channel_post)
    )

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
