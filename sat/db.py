"""Доступ к Postgres для SAT-витрины.

Хранит снапшоты сводной части таблицы. Словарные таблицы (vocab_words,
bot_chats) этот модуль не трогает вообще.

Все функции синхронные и блокирующие — вызывать из async-кода только
через asyncio.to_thread, иначе встанет event loop бота.
"""

import hashlib
import json
import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Пул создаётся лениво и один на процесс.

    В отличие от bot.py, который открывает новое соединение на каждый вызов,
    здесь соединения переиспользуются: с появлением второго сервиса общий
    лимит max_connections у Postgres стал реальным ограничением.
    """
    global _pool

    if _pool is None:
        _pool = ConnectionPool(
            config.database_url(),
            min_size=1,
            max_size=config.pool_max_size(),
            kwargs={"row_factory": dict_row},
            name="sat",
            open=False,
        )
        _pool.open()

    return _pool


def connection():
    """Контекстный менеджер соединения из пула.

    На выходе транзакция коммитится, при исключении — откатывается,
    поэтому явный commit() не нужен.
    """
    return get_pool().connection()


def close_pool() -> None:
    global _pool

    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema() -> None:
    """Создаёт таблицу снапшотов. Идемпотентна.

    Вызывать только из worker'а: параллельный CREATE TABLE IF NOT EXISTS
    из двух процессов может упасть на гонке в системном каталоге Postgres.
    """
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sat_snapshots (
                id           SERIAL PRIMARY KEY,
                captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                content_hash TEXT NOT NULL,
                payload      JSONB NOT NULL
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS sat_snapshots_captured_at_idx
                ON sat_snapshots (captured_at DESC);
        """)

    logger.info("Схема sat_snapshots готова")


def compute_hash(payload: dict[str, Any]) -> str:
    """Отпечаток содержимого сводной.

    sort_keys обязателен: без него порядок ключей в dict может меняться
    между запусками и одинаковые данные дадут разные хеши.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def store_if_changed(payload: dict[str, Any]) -> bool:
    """Пишет снапшот, только если содержимое отличается от последнего.

    Возвращает True, если строка добавлена.

    Без этой проверки опрос раз в 10 минут дал бы ~4300 одинаковых строк
    в месяц. С ней таблица содержит только точки реального изменения —
    ровно то, что нужно для графика динамики.
    """
    content_hash = compute_hash(payload)

    with connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT content_hash
            FROM sat_snapshots
            ORDER BY captured_at DESC, id DESC
            LIMIT 1;
        """)

        latest = cur.fetchone()

        if latest and latest["content_hash"] == content_hash:
            return False

        cur.execute(
            """
            INSERT INTO sat_snapshots (content_hash, payload)
            VALUES (%s, %s);
            """,
            (content_hash, Jsonb(payload)),
        )

    logger.info("Записан новый снапшот сводной (hash=%s)", content_hash[:12])
    return True


def latest_snapshot() -> dict[str, Any] | None:
    """Последний снапшот или None, если синк ещё ни разу не отработал."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT captured_at, payload
            FROM sat_snapshots
            ORDER BY captured_at DESC, id DESC
            LIMIT 1;
        """)

        row = cur.fetchone()

    if row is None:
        return None

    return {"captured_at": row["captured_at"], "payload": row["payload"]}


def history(days: int = 90) -> list[dict[str, Any]]:
    """Снапшоты за последние N дней, по возрастанию времени — для графиков."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT captured_at, payload
            FROM sat_snapshots
            WHERE captured_at >= now() - make_interval(days => %s)
            ORDER BY captured_at ASC;
            """,
            (days,),
        )

        rows = cur.fetchall()

    return [{"captured_at": row["captured_at"], "payload": row["payload"]} for row in rows]
