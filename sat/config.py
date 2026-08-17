"""Конфигурация SAT-витрины.

Переменные читаются лениво, а не на import-time: worker'у не нужен
SESSION_SECRET, а веб-сервису — ключ Google. Если валидировать всё сразу,
любой из процессов не запустится из-за переменной, которая ему не нужна.
"""

import json
import os

# Значения по умолчанию
DEFAULT_SYNC_INTERVAL = 600  # секунд между опросами Google Sheets
DEFAULT_POOL_MAX_SIZE = 5  # соединений к Postgres на процесс
DEFAULT_SESSION_TTL = 7 * 24 * 3600  # срок жизни сессионной куки
DEFAULT_AUTH_MAX_AGE = 24 * 3600  # предельный возраст ответа Login Widget
DEFAULT_MEMBERSHIP_TTL = 3600  # как долго доверять проверенной подписке


class ConfigError(RuntimeError):
    """Переменная окружения отсутствует или имеет неверный формат."""


def get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    return value.strip()


def require(name: str) -> str:
    value = get(name)

    if value is None:
        raise ConfigError(f"Не задана переменная окружения {name}")

    return value


def get_int(name: str, default: int) -> int:
    raw = get(name)

    if raw is None:
        return default

    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должна быть целым числом, получено: {raw!r}") from None


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def get_bool(name: str, default: bool) -> bool:
    raw = get(name)

    if raw is None:
        return default

    lowered = raw.lower()

    if lowered in TRUE_VALUES:
        return True

    if lowered in FALSE_VALUES:
        return False

    raise ConfigError(f"{name} должна быть true/false, получено: {raw!r}")


# --- Общее для обоих процессов ---


def database_url() -> str:
    return require("DATABASE_URL")


def bot_token() -> str:
    return require("TELEGRAM_BOT_TOKEN")


def pool_max_size() -> int:
    return get_int("SAT_POOL_MAX_SIZE", DEFAULT_POOL_MAX_SIZE)


# --- Только worker (синк из Google) ---


def google_credentials() -> dict:
    """JSON-ключ сервис-аккаунта, положенный в env одной строкой."""
    raw = require("GOOGLE_SERVICE_ACCOUNT_JSON")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"GOOGLE_SERVICE_ACCOUNT_JSON — невалидный JSON: {e}") from None


def sheet_id() -> str:
    return require("SAT_SHEET_ID")


def dashboard_sheet() -> str | None:
    """Имя листа со сводной. None — определить автоматически по заголовку.

    Координаты блоков нигде не задаются: сводная состоит из нескольких
    таблиц, разбросанных по листу, и парсер ищет их по тексту заголовков.
    Поэтому вставка строк в таблице ничего не ломает.
    """
    return get("SAT_DASHBOARD_SHEET")


def journal_sheet() -> str | None:
    """Имя листа с журналом. None — определить автоматически."""
    return get("SAT_JOURNAL_SHEET")


def sync_interval() -> int:
    return get_int("SAT_SYNC_INTERVAL", DEFAULT_SYNC_INTERVAL)


# --- Только веб-сервис ---


def channel_id() -> int | str:
    """Канал, подписка на который открывает доступ к витрине.

    Принимает и числовой ID (-100...), и @username — Telegram понимает оба.
    """
    raw = require("SAT_CHANNEL_ID")

    try:
        return int(raw)
    except ValueError:
        return raw


def session_secret() -> str:
    return require("SESSION_SECRET")


def session_ttl() -> int:
    return get_int("SAT_SESSION_TTL", DEFAULT_SESSION_TTL)


def auth_max_age() -> int:
    return get_int("SAT_AUTH_MAX_AGE", DEFAULT_AUTH_MAX_AGE)


def bot_username() -> str:
    """Username бота без @ — нужен виджету входа."""
    return require("SAT_BOT_USERNAME").lstrip("@")


def public_base_url() -> str:
    """Внешний адрес сайта: из него собирается auth-url для виджета.

    Домен обязан совпадать с тем, что задан боту через /setdomain,
    иначе Telegram откажется отрисовывать кнопку входа.
    """
    return require("PUBLIC_BASE_URL").rstrip("/")


def cookie_secure() -> bool:
    """Отдавать куку только по HTTPS.

    По умолчанию включено. Выключать только для локальной отладки по http,
    иначе браузер не сохранит куку и вход будет молча зацикливаться.
    """
    return get_bool("SAT_COOKIE_SECURE", True)


def membership_ttl() -> int:
    """Сколько секунд доверять уже проверенной подписке.

    Кука живёт неделю, но подписку перепроверяем чаще: иначе отписавшийся
    сохранял бы доступ до истечения куки. Час — компромисс между задержкой
    отзыва доступа и нагрузкой на Telegram API.
    """
    return get_int("SAT_MEMBERSHIP_TTL", DEFAULT_MEMBERSHIP_TTL)
