"""Фоновая синхронизация Google Sheets → Postgres.

Живёт в процессе бота: worker гарантированно всегда запущен, а веб-сервис
должен оставаться stateless и свободно перезапускаться.

Главное требование — цикл не должен падать никогда. Витрина может отставать
от таблицы, это терпимо; а вот умерший таск замораживает данные навсегда,
и заметить это можно очень нескоро.
"""

import asyncio
import logging

from . import config, db, sheets

logger = logging.getLogger(__name__)

# Потолок отступа: при затяжном сбое ждём не дольше interval * 6.
MAX_BACKOFF_FACTOR = 6

_task: asyncio.Task | None = None


def _delay(failures: int) -> int:
    """Пауза до следующего прохода.

    При повторных сбоях растёт: если Google лежит или ключ отозвали,
    долбиться каждые 10 минут бессмысленно — только логи засоряются.
    """
    interval = config.sync_interval()

    if failures == 0:
        return interval

    return interval * min(2 ** (failures - 1), MAX_BACKOFF_FACTOR)


async def run_once() -> bool:
    """Один проход: прочитать таблицу и записать снапшот, если он изменился.

    Возвращает True, если данные обновились. Исключения пробрасывает —
    их ловит цикл. Отдельной функцией, чтобы синк можно было дёрнуть
    вручную при отладке.
    """
    payload = await asyncio.to_thread(sheets.fetch_summary)
    return await asyncio.to_thread(db.store_if_changed, payload)


async def sync_loop() -> None:
    schema_ready = False
    failures = 0

    while True:
        try:
            # Схему создаёт только worker — параллельный CREATE TABLE
            # из двух процессов может упасть на гонке в каталоге Postgres.
            # Повторяем до успеха: база могла быть недоступна на старте.
            if not schema_ready:
                await asyncio.to_thread(db.init_schema)
                schema_ready = True

            changed = await run_once()
            failures = 0

            if changed:
                logger.info("Витрина обновлена")
            else:
                logger.debug("Данные не изменились, снапшот не добавлен")

        # CancelledError наследуется от BaseException, поэтому сюда
        # не попадает и остановка процесса проходит штатно.
        except Exception:
            failures += 1
            logger.exception("Синк SAT не удался (сбоев подряд: %s)", failures)

        await asyncio.sleep(_delay(failures))


def start() -> asyncio.Task:
    """Запускает цикл. Вызывать из работающего event loop.

    Ссылка на таск хранится в модуле: у asyncio.create_task слабая ссылка,
    и без этого сборщик мусора может убить задачу посреди работы.

    Первый проход начинается сразу, без ожидания interval — иначе после
    каждого редеплоя витрина стояла бы пустой десять минут.
    """
    global _task

    if _task is None or _task.done():
        _task = asyncio.create_task(sync_loop(), name="sat-sync")

    return _task


async def stop() -> None:
    global _task

    if _task is not None and not _task.done():
        _task.cancel()

        try:
            await _task
        except asyncio.CancelledError:
            pass

    _task = None
