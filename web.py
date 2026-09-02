"""Веб-сервис витрины SAT.

Отдельный процесс от бота: падение или редеплой сайта не должны ронять
словарь, и наоборот. Общая с ботом только база.

Запуск: uvicorn web:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from sat import auth, config, db, sheets

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# httpx на уровне INFO печатает полный URL запроса, а у Telegram токен —
# часть адреса (/bot<TOKEN>/getMe). В результате токен утекает в логи
# открытым текстом при каждом обращении.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "sat_session"

# Витрина всегда персональная и меняется при каждом синке — кэшировать нечего,
# а закэшированная браузером страница пережила бы отписку от канала.
NO_STORE = {"Cache-Control": "no-store"}

# Ручное обновление ходит в Google, а у Sheets API квота 60 запросов
# в минуту на пользователя. Витрину смотрит любой подписчик, так что
# без паузы несколько человек с кнопкой выбрали бы лимит за полминуты.
REFRESH_COOLDOWN = 20

_refresh_lock = asyncio.Lock()
_refreshed_at = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await auth.shutdown()
    db.close_pool()


app = FastAPI(title="SAT витрина", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(config.ConfigError)
async def config_error_handler(request: Request, exc: config.ConfigError) -> Response:
    """Отдельный ответ на нехватку настроек.

    Иначе забытая переменная окружения выглядит как обычная пятисотка,
    и понять причину можно только по трассировке в логах платформы.
    """
    logger.error("Сервис настроен не полностью: %s", exc)

    return JSONResponse(
        status_code=503,
        content={"detail": f"Сервис настроен не полностью: {exc}"},
        headers=NO_STORE,
    )


# --- Авторизация ---


async def require_subscriber(request: Request) -> dict:
    """Пускает дальше только подписчика канала. Иначе 401 или 403.

    Проверок две, и обе нужны: кука доказывает, что человек когда-то вошёл
    через Telegram, а is_subscriber — что он всё ещё подписан.
    """
    session = auth.read_session(request.cookies.get(COOKIE_NAME))

    if session is None:
        raise HTTPException(status_code=401, detail="Требуется вход через Telegram")

    if not await auth.is_subscriber(session["uid"]):
        raise HTTPException(status_code=403, detail="Нужна подписка на канал")

    return session


# --- Страницы ---


def _render(filename: str, **replacements: str) -> str:
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")

    for key, value in replacements.items():
        html = html.replace("{{" + key + "}}", value)

    return html


ERROR_MESSAGES = {
    "signature": "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз.",
    "subscription": "Этот аккаунт не подписан на канал.",
}


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str | None = None) -> Response:
    message = ERROR_MESSAGES.get(error or "", "")
    block = f'<p class="error">{message}</p>' if message else ""

    html = _render(
        "login.html",
        BOT_USERNAME=config.bot_username(),
        AUTH_URL=f"{config.public_base_url()}/auth/callback",
        MESSAGE=block,
    )

    return HTMLResponse(html, headers=NO_STORE)


@app.get("/auth/callback")
async def auth_callback(request: Request) -> Response:
    """Приём ответа Telegram Login Widget.

    В query приходят только поля Telegram — своих параметров сюда
    добавлять нельзя, они попадут в подписываемую строку и сломают подпись.
    """
    try:
        user = auth.verify_login_widget(dict(request.query_params))
    except auth.AuthError as e:
        logger.warning("Вход отклонён: %s", e)
        return RedirectResponse("/login?error=signature", status_code=303)

    if not await auth.is_subscriber(user["id"]):
        logger.info("Вход отклонён: user_id=%s не подписан", user["id"])
        return RedirectResponse("/login?error=subscription", status_code=303)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        auth.create_session(user),
        max_age=config.session_ttl(),
        httponly=True,
        secure=config.cookie_secure(),
        samesite="lax",
        path="/",
    )

    logger.info("Вход выполнен: user_id=%s", user["id"])
    return response


@app.get("/logout")
async def logout(request: Request) -> Response:
    session = auth.read_session(request.cookies.get(COOKIE_NAME))

    if session is not None:
        auth.forget_membership(session["uid"])

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")

    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Витрина. Неавторизованного отправляем на вход, а не отдаём 401:
    это страница для человека, а не эндпоинт для скрипта."""
    session = auth.read_session(request.cookies.get(COOKIE_NAME))

    if session is None:
        return RedirectResponse("/login", status_code=303)

    if not await auth.is_subscriber(session["uid"]):
        return RedirectResponse("/login?error=subscription", status_code=303)

    return FileResponse(STATIC_DIR / "index.html", headers=NO_STORE)


# --- API ---


@app.get("/api/summary")
async def api_summary(session: dict = Depends(require_subscriber)) -> dict:
    """Последний снапшот сводной.

    Пустой результат — не ошибка: синк мог ещё ни разу не отработать.
    Отдаём null, чтобы фронтенд показал состояние «данных пока нет».
    """
    return _snapshot_body(await asyncio.to_thread(db.latest_snapshot))


def _snapshot_body(snapshot: dict | None, **extra) -> dict:
    if snapshot is None:
        return {"captured_at": None, "data": None, **extra}

    return {"captured_at": snapshot["captured_at"], "data": snapshot["payload"], **extra}


@app.post("/api/refresh")
async def api_refresh(session: dict = Depends(require_subscriber)) -> dict:
    """Перечитать таблицу прямо сейчас.

    Воркер синхронизирует раз в десять минут; кнопка нужна, чтобы увидеть
    только что внесённую правку, не дожидаясь тика.

    Замок обязателен: без него два одновременных нажатия дали бы два
    параллельных похода в Google и две попытки записать один и тот же
    снапшот.
    """
    global _refreshed_at

    async with _refresh_lock:
        since = time.monotonic() - _refreshed_at

        if since < REFRESH_COOLDOWN:
            # Кто-то только что обновил. Отдаём его результат вместо отказа:
            # данные всё равно свежие, а ошибка тут была бы враньём.
            logger.info("Обновление пропущено, свежее было %.0f с назад", since)
            snapshot = await asyncio.to_thread(db.latest_snapshot)
            return _snapshot_body(snapshot, changed=False, skipped=True)

        try:
            payload = await asyncio.to_thread(sheets.fetch_summary)
        except config.ConfigError:
            raise  # обработается отдельно и скажет, какой переменной нет
        except Exception as e:
            logger.exception("Ручное обновление не удалось")
            raise HTTPException(
                status_code=502, detail=f"Не удалось прочитать таблицу: {e}"
            ) from None

        changed = await asyncio.to_thread(db.store_if_changed, payload)
        _refreshed_at = time.monotonic()

    snapshot = await asyncio.to_thread(db.latest_snapshot)
    return _snapshot_body(snapshot, changed=changed, skipped=False)


@app.get("/api/history")
async def api_history(
    days: int = Query(default=90, ge=1, le=365),
    session: dict = Depends(require_subscriber),
) -> dict:
    """Ряд снапшотов для графиков динамики.

    days ограничен сверху, чтобы один запрос не вытянул всю таблицу.
    """
    rows = await asyncio.to_thread(db.history, days)

    return {
        "days": days,
        "points": [
            {"captured_at": row["captured_at"], "data": row["payload"]} for row in rows
        ],
    }


@app.get("/healthz")
async def healthz() -> dict:
    """Проверка живости для Railway.

    Намеренно не ходит в базу: иначе моргнувший Postgres выглядел бы как
    мёртвый веб-сервис и платформа начала бы его перезапускать.
    """
    return {"status": "ok"}
