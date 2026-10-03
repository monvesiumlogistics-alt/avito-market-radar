# AvitoHunter

Telegram-бот для анализа рынка Avito: находит категории и товары, которые сейчас пользуются спросом,
чтобы закупать их в Китае и продавать «под заказ». Плюс (опционально) мониторинг новых объявлений по поиску.

Официального API для поиска чужих объявлений у Avito нет, поэтому данные берутся из обычного Chromium
(Playwright) с постоянным профилем — так же, как их видит человек в браузере.

> **Капча и защита Avito.** Проект **не обходит** капчу, проверки и защитные механизмы Avito и не должен
> этого делать: нет решателей капчи, антидетекта, ротации отпечатков. Если Avito показывает проверку,
> бот присылает в Telegram «🧩» и ждёт, пока **человек** пройдёт её в окне браузера бота
> (`CAPTCHA_WAIT_MINUTES`). Блок «проблема с IP» лечится только сменой сети/адреса.

## Возможности

- **Проверка рынка** (`/report`, кнопка «🔎 Проверить рынок»): обход карты из ~209 подкатегорий Avito
  с сортировкой «по дате» до объявлений старше 7 дней; отсев дешёвых (`MIN_PRICE`) и продвинутых;
  склейка копий одного товара; просмотры в день со страницы объявления; реальная дата по профилю продавца.
- **Находка** = свежее объявление с ≥ `VPD_MIN` просмотров в день (🔥 от `VPD_HOT`). 🔥 приходят сразу
  карточкой с 👍/👎, остальные — в итоговой сводке.
- **Срез рынка** по каждой подкатегории: сколько свежих объявлений, медианная цена, просмотры от–до.
- **Модели** с несколькими объявлениями (одна модель у разных продавцов), ссылка на поиск на goofish.
- **«Ушло за N дней»**: перепроверка прошлых находок (исчезло объявление ≈ продано).
- Живой прогресс одним сообщением, лимит загрузок на прогон, продолжение прерванного прогона.
- `/top`, `/export` (CSV), `/price` (маржа по цене в юанях), `/skip` / `/unskip` / `/cats`.
- Мониторинг новых объявлений по поисковой ссылке (`/check`, раз в `CHECK_INTERVAL_MINUTES`).

## Стек

Python 3.11+ · aiogram 3 · Playwright (Chromium) · BeautifulSoup · SQLAlchemy 2 + SQLite ·
APScheduler · pydantic-settings · pytest · ruff

## Требования

- Windows / Linux / macOS, Python 3.11+
- Telegram-бот (токен от [@BotFather](https://t.me/BotFather)) и ваш chat id
- Доступ к avito.ru из сети, где запускается бот (дата-центровые VPN/прокси Avito режет)

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt        # работа бота
.\.venv\Scripts\python -m pip install -r requirements-dev.txt    # + тесты и линтер
.\.venv\Scripts\python -m playwright install chromium             # браузер для Playwright
```

(Linux/macOS: `.venv/bin/python …`)

## Конфигурация (`.env`)

Вся конфигурация — в `.env` (читается `app/config.py` через pydantic-settings). Шаблон — `.env.example`.

```powershell
copy .env.example .env
notepad .env
```

Обязательно: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID` (свой chat id — например, через [@userinfobot](https://t.me/userinfobot);
напишите своему боту `/start`, иначе он не сможет вам писать). Остальное имеет значения по умолчанию:

| Переменная | Что делает |
|---|---|
| `TELEGRAM_PROXY` | прокси только для Telegram, если api.telegram.org недоступен |
| `AVITO_PROXY` | прокси только для браузера Avito (HTTP с логином; SOCKS5 — без логина) |
| `HEADLESS` | `false` — видимое окно (нужно для ручной капчи) |
| `MIN_PRICE`, `VPD_MIN`, `VPD_HOT` | порог цены и просмотров в день для находок |
| `REPORT_BUDGET`, `REPORT_MAX_PAGES` | лимит загрузок на прогон, страниц выдачи на подкатегорию |
| `PAGE_DELAY_MIN/MAX` | пауза между загрузками, сек |
| `CAPTCHA_WAIT_MINUTES` | сколько ждать, пока человек пройдёт проверку |
| `CNY_RATE`, `CARGO_*` | курс и тарифы доставки для `/price` |
| `PREMIUM_EMOJI` | иконки-эмодзи (работают, если Telegram показывает custom emoji от бота) |
| `AVITO_SEARCH_URLS`, `KEYWORDS`, `PRICE_*` | мониторинг новых объявлений по поиску |

Секреты (`.env`), база (`data/app.db`), профиль браузера с cookies Avito (`data/avito_profile`) и логи
лежат вне git (см. `.gitignore`).

## Запуск

```powershell
# 1. Карта категорий (один раз): CSV с колонками parent;name;score;...;key
.\.venv\Scripts\python -m scripts.import_map data\catalog.csv

# 2. (необязательно) вход в Avito или ручная проверка в профиле бота — закройте окно, когда закончите
.\.venv\Scripts\python -m app.auth

# 3. Бот
.\.venv\Scripts\python -m app.main
```

Playwright запускается самим ботом по нажатию кнопки (профиль `AVITO_PROFILE_PATH`, по умолчанию
`data/avito_profile`). Профиль может быть открыт только одним процессом — не запускайте `app.auth`
и бота одновременно.

Без Telegram-кнопки: `python -m scripts.run_report` (один прогон). Автозапуск на Windows:
`install_autostart.ps1` (запускается вручную, регистрирует задачу Планировщика с `run_bot.bat`).

## Команды Telegram

| Команда | Что делает |
|---|---|
| `/start` | кнопки «🔎 Проверить рынок» / «⏹ Стоп», справка |
| `/report`, `/stop` | запустить/продолжить проверку рынка, остановить (прогресс сохраняется) |
| `/top [дней]` | лучшие находки за N дней из базы (без заходов на Avito) |
| `/export` | все находки файлом CSV |
| `/price <id> <юани>` | маржа для находки `#id`: себестоимость (¥ × курс + доставка по весу) vs цена Avito |
| `/cats`, `/skip <текст>`, `/unskip <текст>` | разделы; выключить / вернуть категории |
| `/status`, `/check`, `/last`, `/watchlist`, `/pause`, `/resume` | мониторинг новых объявлений |

Команды работают только из чата `TELEGRAM_ADMIN_CHAT_ID`.

## Структура

```
app/
  main.py                 точка входа: БД, бот, планировщик мониторинга, общий BrowserGate
  config.py               настройки из .env (pydantic-settings)
  db.py                   модели SQLAlchemy, init_db + безопасные ALTER для новых колонок
  auth.py                 ручной вход / проверка в профиле браузера бота
  providers/
    avito_browser.py      Playwright: профиль, warm-up, fetch, ожидание ручной проверки
    avito_parser.py       разбор HTML (селекторы в одном месте), даты, просмотры, блоки
    base.py               интерфейс провайдера, BrowserGate (браузер по очереди)
  services/
    market.py             проверка рынка: прогон, бюджет, продолжение, перепроверка, сохранение
    market_logic.py       чистые функции: фильтры, группировка, метрики, форматирование
    market_cmds.py        /top, /export, /price, /cats, /skip
    panels.py             тексты-панели (/start, /status, алерты)
    scanner.py, matcher.py  мониторинг новых объявлений
    notifier.py           отправка/редактирование сообщений Telegram
  telegram/handlers.py    команды и кнопки
scripts/                  import_map, run_report, save_fixtures (обновление тестовых страниц)
tests/                    pytest; fixtures — сохранённые обезличенные страницы Avito
docs/                     требования, дизайн, решения (ADR), ревью, roadmap
```

## Разработка

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check .
```

Если Avito поменял вёрстку: селекторы — `SELECTORS` в `app/providers/avito_parser.py`; HTML пустой/заблокированной
страницы сохраняется в `data/debug/`. Тестовые страницы обновляются `scripts/save_fixtures.py` (с обезличиванием).
