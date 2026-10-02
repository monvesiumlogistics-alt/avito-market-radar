# AvitoHunter

Мониторинг новых объявлений Avito с уведомлениями в Telegram.
Цепочка: **Avito (Playwright) → локальный фильтр → SQLite → дедупликация → Telegram**.

Официального API для поиска чужих объявлений у Avito нет, поэтому источник данных — обычный Chromium
с постоянным профилем. Обхода капчи и защиты нет: если Avito ограничит доступ, бот пришлёт алерт.

## Запуск (Windows, PowerShell, из папки проекта)

### 1. Зависимости

```powershell
cd C:\Users\Administrator\Desktop\avito-hunter
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

### 2. Chromium для Playwright

```powershell
.\.venv\Scripts\python -m playwright install chromium
```

### 3. Telegram-бот

1. В Telegram открой [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен.
2. Узнай свой chat id: напиши [@userinfobot](https://t.me/userinfobot), он ответит числом `Id`.
3. Напиши своему новому боту `/start`, иначе он не сможет тебе писать.

### 4. `.env`

```powershell
copy .env.example .env
notepad .env
```

Заполни `TELEGRAM_BOT_TOKEN` и `TELEGRAM_ADMIN_CHAT_ID`.
Если `api.telegram.org` с ПК не открывается, укажи `TELEGRAM_PROXY=http://127.0.0.1:10809` (прокси Happ).

### 5. Вход в Avito (один раз)

```powershell
.\.venv\Scripts\python -m app.auth
```

Откроется Chromium: войди в аккаунт (телефон, SMS, капча), затем закрой окно.
Пароль не сохраняется — в `data/avito_profile` остаются только cookies.
Шаг необязателен: выдача Avito публичная, бот работает и без входа.

### 6. Search URL

На avito.ru настрой поиск (категория, запрос, цена, сортировка «По дате»), скопируй адрес из строки браузера
и положи в `.env`:

```
AVITO_SEARCH_URLS=Мужское|https://www.avito.ru/...;Женское|https://www.avito.ru/...
```

Метка перед `|` попадает в уведомление (строка 👤). Бот всё равно перепроверяет цену, ключевые слова
и стоп-слова сам (`KEYWORDS`, `EXCLUDE_KEYWORDS`, `PRICE_MIN`, `PRICE_MAX`) — Avito часто подмешивает лишнее
и игнорирует фильтр цены в URL.

### 7. Запуск

```powershell
.\.venv\Scripts\python -m app.main
```

Первая проверка через ~10 секунд. Она **молча** индексирует текущую выдачу (`INITIAL_SCAN_NOTIFY=false`),
дальше в Telegram приходят только новые объявления. Окно Chromium на время проверки появляется и закрывается
(`HEADLESS=false` — в фоновом режиме Avito блокирует чаще).

### 8–9. Проверка в Telegram

- `/status` — состояние, последняя/следующая проверка, сколько найдено
- `/check` — проверить прямо сейчас
- `/last` — последние найденные
- `/watchlist` — правила, `/pause` / `/resume` — пауза

Команды работают только из чата `TELEGRAM_ADMIN_CHAT_ID`.

## Настройки

| Переменная | Что делает |
|---|---|
| `CHECK_INTERVAL_MINUTES` | интервал проверки (60, 30, 15, 5…) |
| `INITIAL_SCAN_NOTIFY` | `true` — прислать и уже существующие объявления при первом скане |
| `MAX_PAGES` | сколько страниц листать, если вся первая страница новая |
| `HEADLESS` | `true` — без окна браузера (риск блокировки выше) |

Поменял `AVITO_SEARCH_URLS` → следующий скан снова молча проиндексирует выдачу, без спама.

## Если что-то сломалось

- **«⚠️ Avito ограничил доступ»** — останови бота (Ctrl+C), запусти `python -m app.auth`, открой любую страницу
  Avito, пройди проверку, закрой окно и запусти бота снова.
- **В логе `0 карточек; HTML сохранён в data/debug/...`** — Avito поменял вёрстку. Селекторы лежат в одном
  месте: `SELECTORS` в `app/providers/avito_parser.py`.
- **`app.auth` и бот одновременно** не работают: профиль браузера может быть открыт только одним процессом.

## Разработка

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check .
```

Структура: `providers/` (откуда объявления; новый источник = новый класс `AvitoProvider`),
`services/matcher.py` (фильтр), `services/scanner.py` (цикл: fetch → match → dedupe → notify),
`services/notifier.py` (Telegram), `telegram/handlers.py` (команды), `db.py` (SQLite/SQLAlchemy 2).
