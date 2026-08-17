"""Чтение SAT-таблицы из Google Sheets.

Устройство модуля: поход в Google (fetch_grids) отделён от разбора (parse).
Разбор — чистая функция от двух двумерных списков строк, поэтому его можно
проверять на зафиксированных данных, не имея доступа к таблице.

Сводная — не одна таблица, а несколько блоков, разбросанных по листу.
Блоки ищутся по тексту заголовка, а не по координатам: вставка строки
в таблицу не должна ломать витрину.

Все функции синхронные и блокирующие — из async-кода вызывать только
через asyncio.to_thread.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Sequence

import gspread
from google.oauth2.service_account import Credentials

from . import config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DASHBOARD_KEYWORDS = ("дашборд", "dashboard", "сводн", "анализ")
JOURNAL_KEYWORDS = ("журнал", "journal", "log")

# Заголовки блоков сводной
BLOCK_SPEED = "Скорость"
BLOCK_BOOKS = "Сравнение книг и источников"
BLOCK_SLOW_TOPICS = "Самые медленные темы"
BLOCK_ERROR_REASONS = "Причины ошибок"
BLOCK_DIFFICULTY = "Сложность"
BLOCK_RESULTS = "Результаты"

# Подписи KPI-плиток: значение лежит в строке под подписью
KPI_LABELS = {
    "total_questions": "Всего вопросов",
    "accuracy": "Точность",
    "avg_time": "Среднее время",
    "slow_questions": "Медленных вопросов",
    "focus_area": "Главная зона роста",
}

# Колонки журнала. Ищутся по названию, поэтому перестановка колонок
# в таблице ничего не сломает.
COL_DATE = "Дата"
COL_SECTION = "Раздел SAT"
COL_BOOK = "Книга / источник"
COL_TOPIC = "Тема"
COL_RESULT = "Результат"
COL_TIME = "Время на вопрос, сек"
COL_SPEED = "Статус скорости"

RESULT_CORRECT = "Правильно"
RESULT_WRONG = "Неправильно"
RESULT_SKIPPED = "Пропущено"
SPEED_SLOW = "Медленно"


class SheetsError(RuntimeError):
    """Не удалось прочитать или разобрать таблицу."""


# --- Разбор значений ---

_CLEAN_RE = re.compile(r"[\s ]+")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return _CLEAN_RE.sub(" ", str(value)).strip()


def _key(value: Any) -> str:
    """Нормализованный вид для сравнения заголовков."""
    return _text(value).casefold().rstrip("=:")


def _num(value: Any) -> float | None:
    """Число из ячейки.

    В таблице встречается русский формат с запятой, единицы прямо в
    значении ("101,0 сек") и проценты — часть ячеек отформатирована как
    длительность, хотя хранит количество. Всё это приводится к числу.
    """
    text = _text(value)

    if not text:
        return None

    text = text.replace("−", "-")  # юникодный минус
    text = re.sub(r"(сек|%)", "", text, flags=re.IGNORECASE)
    text = text.replace(" ", "").replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(round(number))


def _pct(value: Any) -> float | None:
    """Проценты в долю: "85,4%" -> 0.854."""
    number = _num(value)
    return None if number is None else number / 100.0


def _ratio(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def _date(value: Any) -> str | None:
    """Дата журнала (ДД.ММ.ГГГГ) в ISO."""
    text = _text(value)

    if not text:
        return None

    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue

    return None


# --- Работа с сеткой ---

Grid = Sequence[Sequence[str]]


def _cell(grid: Grid, row: int, col: int) -> str:
    if row < 0 or row >= len(grid):
        return ""

    line = grid[row]

    if col < 0 or col >= len(line):
        return ""

    return _text(line[col])


def _find(grid: Grid, title: str) -> tuple[int, int] | None:
    """Позиция ячейки с заданным заголовком."""
    wanted = _key(title)

    for row_index, row in enumerate(grid):
        for col_index, value in enumerate(row):
            if _key(value) == wanted:
                return row_index, col_index

    return None


def _read_block(grid: Grid, title: str) -> list[dict[str, str]]:
    """Таблица под заголовком: строка заголовка + строки до первой пустой.

    Ширина определяется по непустым ячейкам строки заголовков — это важно,
    потому что блоки стоят бок о бок и без границы разбор залез бы в соседний.
    """
    position = _find(grid, title)

    if position is None:
        logger.warning("Блок %r не найден в сводной", title)
        return []

    title_row, first_col = position
    header_row = title_row + 1

    headers: list[str] = []
    col = first_col

    while True:
        name = _cell(grid, header_row, col)

        if not name:
            break

        headers.append(name)
        col += 1

    if not headers:
        logger.warning("У блока %r нет строки заголовков", title)
        return []

    rows: list[dict[str, str]] = []
    row_index = header_row + 1

    while row_index < len(grid):
        if not _cell(grid, row_index, first_col):
            break

        rows.append({
            name: _cell(grid, row_index, first_col + offset)
            for offset, name in enumerate(headers)
        })
        row_index += 1

    return rows


def _raw(grid: Grid, row: int, col: int) -> str:
    """Содержимое ячейки без схлопывания переносов строк."""
    if row < 0 or row >= len(grid):
        return ""

    line = grid[row]

    if col < 0 or col >= len(line):
        return ""

    return str(line[col] if line[col] is not None else "")


def _read_kpi(grid: Grid, label: str) -> str:
    """Значение KPI-плитки.

    Встречаются две раскладки. Основная: подпись и значение лежат в одной
    объединённой ячейке через перенос строки. Запасная: значение в строке
    под подписью.

    У запасной есть подвох — подпись плитки может совпасть с заголовком
    колонки таблицы («Точность», «Среднее время»), и тогда вместо плитки
    подхватится первое значение чужой таблицы. Отличаем по соседней ячейке:
    плитка объединена, справа от неё пусто, а у заголовка таблицы справа
    стоит следующий заголовок.
    """
    wanted = _key(label)

    for row_index, row in enumerate(grid):
        for col_index in range(len(row)):
            text = _raw(grid, row_index, col_index)

            if "\n" not in text:
                continue

            head, _, tail = text.partition("\n")

            if _key(head) == wanted and _text(tail):
                return _text(tail)

    for row_index, row in enumerate(grid):
        for col_index in range(len(row)):
            if _key(_raw(grid, row_index, col_index)) != wanted:
                continue

            if _cell(grid, row_index, col_index + 1):
                continue  # справа ещё колонка — это таблица, а не плитка

            below = _cell(grid, row_index + 1, col_index)

            if below:
                return below

    logger.warning("KPI %r не найден в сводной", label)
    return ""


def _has_data(rows: Iterable[dict[str, Any]], numeric_keys: Sequence[str]) -> bool:
    """Есть ли в блоке хоть что-то кроме нулей.

    Блоки «Причины ошибок» и «Сложность» существуют в таблице, но их нечем
    заполнить — в журнале нет соответствующих колонок. Показывать на сайте
    ряды нулей смысла нет.
    """
    for row in rows:
        for key in numeric_keys:
            value = row.get(key)

            if value:
                return True

    return False


# --- Разбор сводной ---


def _parse_dashboard(grid: Grid) -> dict[str, Any]:
    kpi = {name: _read_kpi(grid, label) for name, label in KPI_LABELS.items()}

    speed = [
        {"status": row.get("Статус", ""), "count": _int(row.get("Количество")) or 0}
        for row in _read_block(grid, BLOCK_SPEED)
    ]

    books = [
        {
            "name": row.get("Книга / источник", ""),
            "questions": _int(row.get("Вопросов")) or 0,
            "correct": _int(row.get("Правильных")) or 0,
            "accuracy": _pct(row.get("Точность")),
            "avg_time": _num(row.get("Среднее время")),
            "slow": _int(row.get("Медленных")) or 0,
            "skipped": _int(row.get("Пропущено")) or 0,
        }
        for row in _read_block(grid, BLOCK_BOOKS)
    ]

    slow_topics = [
        {
            "topic": row.get("Тема", ""),
            "section": row.get("Раздел", ""),
            "questions": _int(row.get("Вопросов")) or 0,
            "accuracy": _pct(row.get("Точность")),
            "avg_time": _num(row.get("Среднее время")),
            "slow": _int(row.get("Медленных")) or 0,
            "book": row.get("Книга", ""),
        }
        for row in _read_block(grid, BLOCK_SLOW_TOPICS)
    ]

    results = [
        {
            "result": row.get("Результат", ""),
            "count": _int(row.get("Количество")) or 0,
            "share": _pct(row.get("Доля")),
        }
        for row in _read_block(grid, BLOCK_RESULTS)
    ]

    difficulty = [
        {
            "level": row.get("Сложность", ""),
            "questions": _int(row.get("Вопросов")) or 0,
            "accuracy": _pct(row.get("Точность")),
        }
        for row in _read_block(grid, BLOCK_DIFFICULTY)
    ]

    error_reasons = [
        {
            "reason": row.get("Причина", ""),
            "count": _int(row.get("Количество")) or 0,
            "share": _pct(row.get("Доля ошибок")),
            "avg_time": _num(row.get("Среднее время")),
        }
        for row in _read_block(grid, BLOCK_ERROR_REASONS)
    ]

    return {
        "kpi": {
            "total_questions": _int(kpi["total_questions"]) or 0,
            "accuracy": _pct(kpi["accuracy"]),
            "avg_time": _num(kpi["avg_time"]),
            "slow_questions": _int(kpi["slow_questions"]) or 0,
            "focus_area": kpi["focus_area"],
        },
        "speed": speed,
        "books": books,
        "slow_topics": slow_topics,
        "results": results,
        # Пустые блоки схлопываем в [], чтобы фронтенд просто их не рисовал.
        "difficulty": difficulty if _has_data(difficulty, ("questions", "accuracy")) else [],
        "error_reasons": error_reasons if _has_data(error_reasons, ("count",)) else [],
    }


# --- Разбор журнала ---


def _journal_rows(grid: Grid) -> list[dict[str, str]]:
    """Строки журнала. Заголовок ищется по названиям колонок."""
    header_row = None

    for row_index, row in enumerate(grid):
        keys = {_key(value) for value in row}

        if _key(COL_DATE) in keys and _key(COL_RESULT) in keys:
            header_row = row_index
            break

    if header_row is None:
        raise SheetsError("В журнале не найдена строка заголовков")

    columns = {_key(value): index for index, value in enumerate(grid[header_row]) if _text(value)}

    rows = []

    for row_index in range(header_row + 1, len(grid)):
        if not _cell(grid, row_index, columns[_key(COL_DATE)]):
            continue

        rows.append({
            name: _cell(grid, row_index, index) for name, index in columns.items()
        })

    return rows


def _blank_bucket() -> dict[str, Any]:
    return {"questions": 0, "correct": 0, "wrong": 0, "skipped": 0, "slow": 0, "time_sum": 0.0, "timed": 0}


def _accumulate(bucket: dict[str, Any], row: dict[str, str]) -> None:
    result = _text(row.get(_key(COL_RESULT)))
    speed = _text(row.get(_key(COL_SPEED)))
    seconds = _num(row.get(_key(COL_TIME)))

    bucket["questions"] += 1

    if result == RESULT_CORRECT:
        bucket["correct"] += 1
    elif result == RESULT_WRONG:
        bucket["wrong"] += 1
    elif result == RESULT_SKIPPED:
        bucket["skipped"] += 1

    if speed == SPEED_SLOW:
        bucket["slow"] += 1

    if seconds is not None:
        bucket["time_sum"] += seconds
        bucket["timed"] += 1


def _finish(bucket: dict[str, Any], **extra: Any) -> dict[str, Any]:
    questions = bucket["questions"]

    return {
        **extra,
        "questions": questions,
        "correct": bucket["correct"],
        "wrong": bucket["wrong"],
        "skipped": bucket["skipped"],
        "slow": bucket["slow"],
        "accuracy": _ratio(bucket["correct"], questions),
        "avg_time": round(bucket["time_sum"] / bucket["timed"], 1) if bucket["timed"] else None,
    }


def _parse_journal(grid: Grid) -> dict[str, Any]:
    """Разбивка по разделам и динамика по дням.

    Разделы считаются здесь, а не берутся из сводной: в таблице этот блок
    ищет раздел «Reading and Writing», тогда как журнал пишет «Writing»,
    поэтому в самой таблице он показывает нули.
    """
    rows = _journal_rows(grid)

    by_section: dict[str, dict] = defaultdict(_blank_bucket)
    by_day: dict[str, dict] = defaultdict(_blank_bucket)
    by_day_section: dict[tuple[str, str], dict] = defaultdict(_blank_bucket)

    for row in rows:
        section = _text(row.get(_key(COL_SECTION))) or "Без раздела"
        day = _date(row.get(_key(COL_DATE)))

        _accumulate(by_section[section], row)

        if day is not None:
            _accumulate(by_day[day], row)
            _accumulate(by_day_section[(day, section)], row)

    sections = sorted(
        (_finish(bucket, name=name) for name, bucket in by_section.items()),
        key=lambda item: item["questions"],
        reverse=True,
    )

    daily = [_finish(by_day[day], date=day) for day in sorted(by_day)]

    daily_by_section: dict[str, list] = defaultdict(list)

    for (day, section) in sorted(by_day_section):
        daily_by_section[section].append(_finish(by_day_section[(day, section)], date=day))

    return {
        "sections": sections,
        "daily": daily,
        "daily_by_section": dict(daily_by_section),
        "journal_rows": len(rows),
    }


# --- Сборка ---


def parse(dashboard: Grid, journal: Grid) -> dict[str, Any]:
    """Чистая функция: две сетки -> payload витрины."""
    payload = _parse_dashboard(dashboard)
    payload.update(_parse_journal(journal))

    return payload


def _open_worksheet(
    spreadsheet, configured: str | None, keywords: Sequence[str], what: str, env_name: str
):
    """Находит нужный лист. При неоднозначности падает, а не выбирает наугад.

    В реальной таблице под «журнал» подходят и рабочий лист, и заготовка,
    и архивная копия. Взять первый попавшийся означало бы молча считать
    статистику не по тем данным — а такую ошибку почти невозможно заметить.
    """
    titles = [worksheet.title for worksheet in spreadsheet.worksheets()]

    if configured:
        for worksheet in spreadsheet.worksheets():
            if worksheet.title == configured:
                return worksheet

        raise SheetsError(f"Лист {configured!r} не найден. Есть такие: {titles}")

    matches = [ws for ws in spreadsheet.worksheets()
               if any(keyword in ws.title.casefold() for keyword in keywords)]

    if len(matches) == 1:
        logger.info("Лист «%s» определён автоматически: %r", what, matches[0].title)
        return matches[0]

    if not matches:
        raise SheetsError(
            f"Не удалось определить лист «{what}»: ничего не подошло. "
            f"Задайте {env_name} явно. Листы таблицы: {titles}"
        )

    raise SheetsError(
        f"Под «{what}» подходит несколько листов: {[ws.title for ws in matches]}. "
        f"Выбирать наугад нельзя — задайте {env_name} явно."
    )


def fetch_grids() -> tuple[list[list[str]], list[list[str]]]:
    """Читает оба листа. Два обращения к Google на один синк."""
    credentials = Credentials.from_service_account_info(
        config.google_credentials(), scopes=SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(config.sheet_id())

    dashboard = _open_worksheet(
        spreadsheet, config.dashboard_sheet(), DASHBOARD_KEYWORDS,
        "сводная", "SAT_DASHBOARD_SHEET",
    )
    journal = _open_worksheet(
        spreadsheet, config.journal_sheet(), JOURNAL_KEYWORDS,
        "журнал", "SAT_JOURNAL_SHEET",
    )

    return dashboard.get_all_values(), journal.get_all_values()


def fetch_summary() -> dict[str, Any]:
    """Полный цикл: сходить в Google и разобрать."""
    dashboard, journal = fetch_grids()
    payload = parse(dashboard, journal)

    logger.info(
        "Прочитано: %s вопросов в журнале, %s разделов, %s дней",
        payload["journal_rows"],
        len(payload["sections"]),
        len(payload["daily"]),
    )

    return payload
