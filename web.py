"""Веб-сервис витрины SAT.

Отдельный процесс от бота: падение или редеплой сайта не должны ронять
словарь, и наоборот. Общая с ботом только база.

Запуск: uvicorn web:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import logging
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

from sat import auth, config, db

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
    snapshot = await asyncio.to_thread(db.latest_snapshot)

    if snapshot is None:
        return {"captured_at": None, "data": None}

    return {"captured_at": snapshot["captured_at"], "data": snapshot["payload"]}


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
