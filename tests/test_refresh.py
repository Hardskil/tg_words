"""Проверка ручного обновления витрины: POST /api/refresh.

Запуск:  venv\Scripts\python.exe tests\test_refresh.py

Google и Telegram подменяются заглушками, поэтому тест не ходит наружу
и не требует настроенного окружения.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

os.environ["TELEGRAM_BOT_TOKEN"] = "123456:FAKE-TOKEN-FOR-TESTS"
os.environ["SESSION_SECRET"] = "test-secret-value"
os.environ["SAT_CHANNEL_ID"] = "-1001234567890"
os.environ["SAT_BOT_USERNAME"] = "my_sat_bot"
os.environ["PUBLIC_BASE_URL"] = "https://sat.example.com"
os.environ["SAT_COOKIE_SECURE"] = "false"

from fastapi.testclient import TestClient

import web
from sat import auth, config, db, sheets

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ok = []


def check(name, condition, got=None):
    ok.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL':4}  {name}" + ("" if condition else f"   <-- {got!r}"))


def sign(data):
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    data = dict(data)
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return data


async def fake_is_subscriber(user_id):
    return True


auth.is_subscriber = fake_is_subscriber

calls = {"fetch": 0, "store": 0}
PAYLOAD = {"rows": [{"date": "2026-08-01", "section": "Math", "result": "Правильно"}]}
SNAPSHOT = {"captured_at": datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc), "payload": PAYLOAD}

fetch_error = None


def fake_fetch():
    calls["fetch"] += 1
    if fetch_error:
        raise fetch_error
    return PAYLOAD


def fake_store(payload):
    calls["store"] += 1
    return True


sheets.fetch_summary = fake_fetch
db.store_if_changed = fake_store
db.latest_snapshot = lambda: SNAPSHOT

anon = TestClient(web.app, follow_redirects=False)
check("POST /api/refresh без входа -> 401", anon.post("/api/refresh").status_code == 401)

r = anon.get("/auth/callback", params=sign({
    "id": "777000", "first_name": "Bekzod", "auth_date": str(int(time.time()))}))
client = TestClient(web.app, follow_redirects=False, cookies={"sat_session": r.cookies["sat_session"]})

print("--- успешное обновление ---")
web._refreshed_at = 0.0
r = client.post("/api/refresh")
body = r.json()
check("200", r.status_code == 200, r.status_code)
check("таблица прочитана", calls["fetch"] == 1, calls["fetch"])
check("снапшот записан", calls["store"] == 1, calls["store"])
check("changed отдан", body["changed"] is True, body.get("changed"))
check("skipped=False", body["skipped"] is False, body.get("skipped"))
check("данные вернулись сразу", body["data"] == PAYLOAD)
check("дата в ISO", body["captured_at"].startswith("2026-08-17T09:00"), body["captured_at"])

print("--- пауза между обновлениями ---")
r = client.post("/api/refresh")
body = r.json()
check("повтор сразу -> 200, а не ошибка", r.status_code == 200, r.status_code)
check("в Google повторно не ходили", calls["fetch"] == 1, calls["fetch"])
check("помечено как skipped", body["skipped"] is True, body.get("skipped"))
check("данные всё равно отданы", body["data"] == PAYLOAD)

print("--- по истечении паузы ---")
web._refreshed_at = time.monotonic() - web.REFRESH_COOLDOWN - 1
r = client.post("/api/refresh")
check("новое чтение состоялось", calls["fetch"] == 2, calls["fetch"])
check("skipped снова False", r.json()["skipped"] is False)

print("--- Google недоступен ---")
fetch_error = RuntimeError("quota exceeded")
web._refreshed_at = 0.0
r = client.post("/api/refresh")
check("-> 502", r.status_code == 502, r.status_code)
check("причина названа", "quota exceeded" in r.json()["detail"], r.json().get("detail"))
check("сбой не сдвинул паузу", web._refreshed_at == 0.0, web._refreshed_at)

print("--- нет настроек Google ---")
fetch_error = config.ConfigError("Не задана переменная окружения SAT_SHEET_ID")
r = client.post("/api/refresh")
check("-> 503, а не 502", r.status_code == 503, r.status_code)
check("сказано, чего не хватает", "SAT_SHEET_ID" in r.json()["detail"], r.json().get("detail"))
fetch_error = None

print("--- GET не принимается ---")
check("GET /api/refresh -> 405", client.get("/api/refresh").status_code == 405)

print()
print(f"Итог: {sum(ok)}/{len(ok)} проверок пройдено")
raise SystemExit(0 if all(ok) else 1)
