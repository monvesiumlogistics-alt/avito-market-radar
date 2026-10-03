"""Проверка официального Avito API для бизнеса: что реально доступно с ключами из .env.

Ключи: AVITO_CLIENT_ID, AVITO_CLIENT_SECRET (Avito → Профиль → API, тип client_credentials).
Ничего секретного не печатает: только HTTP-коды и короткую сводку ответа. Ничего не меняет в аккаунте (только GET и
чтение статистики).

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.avito_api_probe
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from app.config import settings

API = "https://api.avito.ru"


def call(method: str, path: str, token: str | None = None, body: dict | None = None, form: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def brief(payload) -> str:
    """Структура ответа без значений: ключи верхнего уровня и длины списков."""
    if isinstance(payload, dict):
        return "{" + ", ".join(f"{k}: {len(v)} шт" if isinstance(v, list) else k for k, v in payload.items()) + "}"
    if isinstance(payload, list):
        return f"[{len(payload)} шт]"
    return type(payload).__name__


def main() -> None:
    cid, secret = settings.avito_client_id, settings.avito_client_secret
    if not cid or not secret:
        sys.exit("Нет AVITO_CLIENT_ID / AVITO_CLIENT_SECRET в .env")
    status, tok = call("POST", "/token", form={"grant_type": "client_credentials", "client_id": cid,
                                                "client_secret": secret})  # fmt: skip
    print(f"/token: HTTP {status}")
    if status != 200 or not tok or "access_token" not in tok:
        sys.exit("токен не получен — проверь ключи")
    token = tok["access_token"]

    status, me = call("GET", "/core/v1/accounts/self", token)
    print(f"/core/v1/accounts/self: HTTP {status} {brief(me)}")
    user_id = me.get("id") if isinstance(me, dict) else None

    status, items = call("GET", "/core/v1/items?per_page=25", token)
    print(f"/core/v1/items (мои объявления): HTTP {status} {brief(items)}")
    ids = [i["id"] for i in (items or {}).get("resources", []) if "id" in i][:25] if isinstance(items, dict) else []

    if user_id and ids:
        today = date.today()
        body = {"dateFrom": str(today - timedelta(days=7)), "dateTo": str(today), "itemIds": ids,
                "fields": ["uniqViews", "uniqContacts", "uniqFavorites"], "periodGrouping": "day"}  # fmt: skip
        status, stats = call("POST", f"/stats/v1/accounts/{user_id}/items", token, body=body)
        print(f"/stats/v1/accounts/<id>/items (статистика моих объявлений): HTTP {status} {brief(stats)}")
    else:
        print("статистику пропускаю: нет id аккаунта или объявлений")


if __name__ == "__main__":
    main()
