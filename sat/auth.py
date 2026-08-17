"""Авторизация витрины: кто такой посетитель и подписан ли он на канал.

Две независимые проверки:
  1. Подпись Telegram Login Widget — доказывает, что человек действительно
     тот Telegram-аккаунт, за который себя выдаёт.
  2. getChatMember — доказывает, что этот аккаунт подписан на канал.

Обе обязательны: первая без второй пускает кого угодно из Telegram,
вторая без первой доверяет user_id, который можно подставить руками.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Mapping

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from . import config

logger = logging.getLogger(__name__)

# Статусы, которые считаем подпиской. RESTRICTED сюда не входит:
# в каналах он не встречается, а в группах означает урезанные права.
SUBSCRIBED_STATUSES = frozenset({
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
})

# Допуск на расхождение часов между Telegram и нашим сервером.
CLOCK_SKEW_TOLERANCE = 60

SESSION_SALT = "sat-session"


class AuthError(Exception):
    """Посетитель не прошёл проверку подписи."""


# --- 1. Проверка подписи Telegram Login Widget ---


def verify_login_widget(data: Mapping[str, str]) -> dict[str, Any]:
    """Проверяет ответ Login Widget и возвращает данные пользователя.

    ВАЖНО: data должна содержать ровно те поля, что прислал Telegram.
    Любой лишний параметр в query string попадёт в подписываемую строку
    и подпись не сойдётся — своих параметров в auth-callback не добавлять.
    """
    received_hash = data.get("hash")

    if not received_hash:
        raise AuthError("В ответе Telegram нет поля hash")

    # Схема отличается от Telegram Mini App: там секрет —
    # HMAC_SHA256(key="WebAppData", msg=token), здесь — SHA256(token).
    secret_key = hashlib.sha256(config.bot_token().encode("utf-8")).digest()

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(data.items()) if key != "hash"
    )

    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise AuthError("Подпись Telegram не сошлась")

    _check_freshness(data.get("auth_date"))

    try:
        user_id = int(data["id"])
    except (KeyError, ValueError):
        raise AuthError("В ответе Telegram нет корректного id") from None

    return {
        "id": user_id,
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "username": data.get("username"),
        "photo_url": data.get("photo_url"),
    }


def _check_freshness(auth_date: str | None) -> None:
    """Отсекает переигранный старый ответ.

    Без этой проверки один раз перехваченная ссылка работает вечно.
    """
    if auth_date is None:
        raise AuthError("В ответе Telegram нет поля auth_date")

    try:
        issued_at = int(auth_date)
    except ValueError:
        raise AuthError("Некорректный auth_date") from None

    age = time.time() - issued_at

    if age > config.auth_max_age():
        raise AuthError("Ответ Telegram просрочен, войдите заново")

    if age < -CLOCK_SKEW_TOLERANCE:
        raise AuthError("auth_date из будущего")


# --- 2. Проверка подписки на канал ---

_bot: Bot | None = None
_bot_lock = asyncio.Lock()

# user_id -> (подписан, момент проверки по monotonic)
_membership_cache: dict[int, tuple[bool, float]] = {}


async def get_bot() -> Bot:
    """Один инициализированный Bot на процесс.

    PTB требует initialize() до первого запроса: без него не поднят
    HTTP-клиент. Двойная проверка под локом — чтобы при одновременных
    запросах не создать два клиента.
    """
    global _bot

    if _bot is None:
        async with _bot_lock:
            if _bot is None:
                bot = Bot(config.bot_token())
                await bot.initialize()
                _bot = bot

    return _bot


async def shutdown() -> None:
    global _bot

    if _bot is not None:
        await _bot.shutdown()
        _bot = None

    _membership_cache.clear()


async def is_subscriber(user_id: int) -> bool:
    """Подписан ли пользователь на канал витрины.

    Результат кэшируется: без кэша каждая загрузка страницы каждым
    посетителем била бы в Telegram API и упёрлась в rate limit.
    """
    now = time.monotonic()
    cached = _membership_cache.get(user_id)

    # monotonic, а не time(): перевод системных часов не должен
    # ни продлевать, ни обнулять кэш.
    if cached is not None and now - cached[1] < config.membership_ttl():
        return cached[0]

    subscribed = await _fetch_membership(user_id)

    if subscribed is None:
        # Telegram временно недоступен — ответа нет, а не «не подписан».
        # Кэшировать такое нельзя: секундный сбой запер бы подписчика
        # на весь TTL. Держимся за прошлый известный ответ, если он был.
        return cached[0] if cached is not None else False

    _membership_cache[user_id] = (subscribed, now)

    return subscribed


async def _fetch_membership(user_id: int) -> bool | None:
    """True/False — достоверный ответ, None — Telegram не смог ответить.

    Порядок except важен: в PTB BadRequest и TimedOut наследуются от
    NetworkError, поэтому конкретные случаи должны идти раньше общего.
    """
    try:
        # Создание клиента внутри try: при неверном токене оно бросает
        # InvalidToken, и снаружи это выглядело бы голой пятисоткой.
        bot = await get_bot()
        member = await bot.get_chat_member(config.channel_id(), user_id)
    except Forbidden as e:
        # Бот не админ канала — проверить подписку невозможно в принципе.
        # Это ошибка конфигурации, а не отказ конкретному человеку.
        logger.error("Нет доступа к каналу SAT_CHANNEL_ID, бот должен быть админом: %s", e)
        return False
    except BadRequest as e:
        # Пользователь не найден или не состоит в канале — окончательный ответ.
        logger.info("getChatMember отказал для user_id=%s: %s", user_id, e)
        return False
    except (NetworkError, RetryAfter) as e:
        logger.warning("Telegram временно недоступен, решение не кэшируем: %s", e)
        return None
    except TelegramError as e:
        logger.info("getChatMember отказал для user_id=%s: %s", user_id, e)
        return False

    return member.status in SUBSCRIBED_STATUSES


def forget_membership(user_id: int) -> None:
    """Сбросить кэш подписки — например, после выхода из аккаунта."""
    _membership_cache.pop(user_id, None)


# --- 3. Сессия ---


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret(), salt=SESSION_SALT)


def create_session(user: Mapping[str, Any]) -> str:
    """Подписанный токен для куки. Хранит минимум — id и имя для показа."""
    return _serializer().dumps({
        "uid": user["id"],
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    })


def read_session(token: str | None) -> dict[str, Any] | None:
    """Разбирает куку. None — если подписи нет, она битая или протухла."""
    if not token:
        return None

    try:
        return _serializer().loads(token, max_age=config.session_ttl())
    except SignatureExpired:
        logger.debug("Сессионная кука просрочена")
        return None
    except BadSignature:
        logger.warning("Сессионная кука с неверной подписью")
        return None
