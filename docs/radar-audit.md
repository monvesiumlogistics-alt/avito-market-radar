# Аудит AvitoHunter → Daily Market Radar

Дата: 2026-10-03. Состояние кода: ветка master, 200 тестов зелёные, ~3 600 строк в `app/` + `scripts/`.
Документ — только анализ и план; код не менялся.

---

## 1. Как устроен проект сейчас

| Слой | Файлы | Что делает |
|---|---|---|
| Entry points | `app/main.py` (бот), `scripts/run_report.py` (один прогон без кнопки), `scripts/import_map.py` (карта категорий), `app/auth.py` (ручной вход/капча в профиле), `scripts/save_fixtures.py` (тестовые страницы) | |
| Telegram | `app/telegram/handlers.py` | `/start /report /stop /top /export /price /cats /skip /unskip` + мониторинг `/status /check /last /pause /resume`, кнопки 👍/👎; фильтр по admin chat id |
| Сообщения | `app/services/notifier.py`, `panels.py`, `market_logic.py` (форматирование) | send/edit, панели-тексты, сводка «D», прогресс, премиум-иконки |
| Playwright | `app/providers/avito_browser.py` | постоянный профиль `data/avito_profile`, warm-up главной, `fetch(url)` с паузой 4–9 с, детект блока (403/429/439 + маркеры), `wait_unblocked` (ждёт человека) |
| Парсер | `app/providers/avito_parser.py` | `SELECTORS` в одном месте; выдача → `Listing` (id, title, price, url, image, location, seller, published_at), подкатегории (rubricator + fallback), карточка → просмотры/«+сегодня»/дата/продавец, дата из профиля, `is_gone`, `promoted_ids` |
| Логика рынка | `app/services/market.py` (773 стр.) | `MarketCrawler`: start/stop/resume, бюджет загрузок, обход подкатегорий, перепроверка находок, передача браузера мониторингу, ожидание капчи, breaker, сохранение, сводка |
| Чистые функции | `app/services/market_logic.py` (826 стр.) | возраст/vpd, группировка копий (`norm_title`, ±20% цены), `model_key`, `goofish_query`, маржа, `market_stats`, порядок обхода, форматирование, разбиение сообщений |
| Команды по базе | `app/services/market_cmds.py` | `/top`, CSV, `/price`, `/cats`, `/skip` |
| БД | `app/db.py` (SQLAlchemy 2 + SQLite) | `watch_rules`, `listings` (мониторинг), `categories` (карта + срез последнего обхода), `crawl_runs`, `finds`; новые колонки — идемпотентный `ALTER` (`_ADDED_COLUMNS`) |
| Категории | таблица `categories` | 209 подкатегорий из `catalog.csv` + 48 «бренд × пол» (`?q=`); `prior_score`, `skipped`, `last_*` (срез только **последнего** обхода) |
| Планировщик | APScheduler в `main.py` | только мониторинг по интервалу; проверка рынка — по кнопке |
| Конфиг | `app/config.py` ← `.env` | все пороги/лимиты/паузы/прокси |
| Мониторинг (старое ядро) | `scanner.py`, `matcher.py` | новые объявления по поисковой ссылке (сейчас правило выключено) |

## 2. Текущий flow «🔎 Проверить рынок»

1. `handlers.report` → `MarketCrawler.start()`: новый `CrawlRun` или продолжение прерванного (< 12 ч), задача `_run()` в фоне, свежий бюджет загрузок.
2. `_run`: берёт `BrowserGate` (браузер один на всё), открывает провайдер (warm-up главной = 1 загрузка), шлёт живой прогресс (правится раз в 20 с).
3. `_recheck_finds`: до 20 прошлых находок 2–14 дней → открывает карточку → «ушло»/жива + просмотры.
4. `discover_sections` (если карта старше 30 дней).
5. `_crawl_all`: порядок `crawl_order` (никогда не обходились → по `prior_score`; иначе давность × лучший vpd × 👍/👎), группами по разделам.
6. `crawl_subcategory` для каждой:
   - `_collect`: страницы выдачи `?s=104&pmin=MIN_PRICE&p=N` до первой непромо-карточки старше 7 дней или `REPORT_MAX_PAGES` (5); из выдачи — id, title, price, дата (дневная точность: «Вчера»).
   - `_evaluate`: группы копий → открывает ≤ 2 карточки на группу, ≤ 12 на подкатегорию → просмотры, vpd; кандидатам с vpd ≥ 50 — профиль продавца (реальная дата) → `Find`.
   - `_save`: находки + срез рынка в `categories.last_*` одним коммитом; 🔥 сразу в Telegram.
7. Перед каждой загрузкой `_before_load`: стоп / бюджет / передача браузера мониторингу. Блок → «🧩» и ожидание человека до 15 мин → продолжение или статус `blocked`. 3 ошибки подряд → `failed`.
8. Финиш: статус, `loads`, итоговая сводка одним сообщением (разделы, модели, «📈 Рынок», «ушло»).

## 3. Что переиспользовать без изменений

- Весь `avito_browser.py` (профиль, warm-up, блок-детект, ожидание человека, паузы) и `BrowserGate`.
- `avito_parser.py`: выдача, карточка, профиль, даты, `is_gone`, `promoted_ids`, подкатегории.
- Каркас `MarketCrawler`: бюджет, стоп, продолжение, breaker, прогресс, сводка — их нужно **расширить**, а не заменить.
- `market_logic`: `norm_title`, `group_cards`, `model_key`, `goofish_query`, `market_stats`, `calc_margin`, форматирование и `split_message`/`tg_len`.
- Telegram-слой, `/top /export /price /skip`, премиум-стиль.
- Тестовая инфраструктура: `FakeProvider`, фикстуры страниц, in-memory SQLite.

## 4. Bottlenecks

| Где | Проблема |
|---|---|
| **Скорость** | ~6–10 загрузок/мин (пауза 4–9 с + ~2–3 с загрузка). 600 загрузок ≈ 1–1.5 ч. Один браузер, последовательно — и параллелить нельзя (блоки). |
| **Запросы** | Просмотры есть **только на странице карточки** — в выдаче их нет. Любая метрика «просмотры/день по категории» = открытие карточек = главный расход бюджета (сейчас до 12 на подкатегорию). |
| **Блоки Avito** | Дата-центровый IP режется через 100–250 загрузок; профиль браузера «помечается» (нужно было пересоздать). Домашний IP + ручная капча — рабочий, но лимит неизвестен. Это главный внешний потолок объёма. |
| **Повторный сбор** | Выдача каждый раз читается с нуля; объявления из выдачи **не сохраняются** (только находки). Нет «уже видели этот id» → нельзя остановить листание на вчерашних, нельзя считать new_24h / disappeared. |
| **История** | `categories.last_*` перезаписывается — истории по дням нет, сравнивать «сегодня vs 7 дней» не с чем. |
| **Покрытие** | В крупных категориях 5 страниц = последние часы, не неделя («Не вся неделя»). |
| **БД/архитектура** | `market.py` уже 773 строки и совмещает обход, оценку, сохранение и сообщения; новые фазы туда дописывать нельзя — нужен отдельный модуль. SQLite достаточно (десятки тысяч строк/день). |
| **Восстановление** | Есть (продолжение < 12 ч, breaker, `mark_interrupted`), но гранулярность — подкатегория; прерванная подкатегория проходится заново. |
| **Captcha** | Требует человека у ПК; ночной/утренний прогон без человека остановится на первом блоке. |

## 5. Как превратить в Market Radar (минимально разрушительно)

Ключевая идея: **отделить дешёвый «снимок рынка» (выдача) от дорогого «замера просмотров» (карточки)** и начать
**копить историю**, не ломая текущий `/report`.

1. **Sweep** — новый режим того же краулера: только страницы выдачи по всем подкатегориям, каждое объявление
   upsert в `ad` (first_seen / last_seen). Инкрементально: листать до первого id, уже виденного в прошлом sweep
   (или до 7 дней) → ежедневный sweep небольших категорий = 1 страница.
2. **Daily aggregates** — после sweep чистая функция считает `category_daily` из `ad` (без загрузок).
3. **Deep scan** — переиспользует `_evaluate`/`_open_card`, но выбор карточек не «первые 12», а по бюджету:
   ~75% перспективные категории/товары, ~25% exploration (давно не мерили / случайные), + выборка для
   **базовой линии** категории (несколько случайных свежих карточек → медиана vpd).
4. **Trends** — правила по `category_daily` / `product_daily` относительно собственной истории (z-score /
   отношение к медиане 7 и 30 дней) — включать после 7+ дней данных.
5. **Report** — новый формат «MARKET RADAR» поверх существующего форматирования.

Текущий `/report` остаётся как есть до Phase 3, потом становится «deep scan по запросу».

## 6. Новые модули (под эту структуру)

```
app/services/
  sweep.py          обход выдачи по всем подкатегориям → ad (инкрементально)   [переиспользует avito_browser, parser]
  history.py        category_daily / product_daily из ad + card_obs (чистые функции + запись)
  deep_scan.py      выбор карточек по бюджету (exploit/explore/baseline) → card_obs  [переиспользует _open_card]
  products.py       product_key: нормализация → brand/model extraction → RapidFuzz; embeddings позже
  trends.py         детекторы событий (breakout, acceleration, flood, emerging, high attention, cooling, new cluster)
  radar_report.py   ежедневный отчёт (поверх market_logic форматирования)
app/migrations.py   версии схемы (schema_version) — вместо разрастающегося _ADDED_COLUMNS
```
`market.py` — выделить из него общий «runner» (бюджет, gate, капча, прогресс), которым пользуются sweep и deep_scan.

## 7. Схема БД (новые таблицы, существующие не трогаем)

```sql
CREATE TABLE ad (                      -- каждое объявление из выдачи, одна строка на id
  external_id TEXT PRIMARY KEY, category_id INT NOT NULL,
  title TEXT, title_norm TEXT, product_key TEXT, price INT, url TEXT, seller TEXT,
  promoted BOOL, published_at TIMESTAMP,
  first_seen_at TIMESTAMP NOT NULL, first_seen_scan INT,
  last_seen_at TIMESTAMP NOT NULL,  last_seen_scan INT,
  gone_at TIMESTAMP);
CREATE INDEX ix_ad_cat_seen ON ad(category_id, last_seen_at);
CREATE INDEX ix_ad_cat_first ON ad(category_id, first_seen_at);
CREATE INDEX ix_ad_product ON ad(product_key);

CREATE TABLE scan (id INTEGER PRIMARY KEY, kind TEXT, started_at, finished_at, status TEXT, loads INT);

CREATE TABLE card_obs (                -- замеры карточки (deep scan): несколько во времени → скорость
  external_id TEXT, observed_at TIMESTAMP, views INT, today INT, page_date TIMESTAMP, seller_date TIMESTAMP,
  PRIMARY KEY (external_id, observed_at));

CREATE TABLE category_daily (
  category_id INT, day DATE,
  new_24h INT, fresh_7d INT, unique_sellers INT, disappeared INT, promoted_share REAL,
  price_p25 INT, price_p50 INT, price_p75 INT,
  sampled INT, vpd_p50 REAL, vpd_p75 REAL, vpd_p90 REAL, vpd_max REAL,
  share_vpd_gt20 REAL, share_vpd_gt50 REAL, share_vpd_gt100 REAL,
  unique_products INT, repeated_models INT,
  PRIMARY KEY (category_id, day));

CREATE TABLE product (id INTEGER PRIMARY KEY, key TEXT UNIQUE, brand TEXT, model TEXT, label TEXT, first_seen DATE);
CREATE TABLE product_daily (product_id INT, day DATE, listings INT, sellers INT, categories INT,
  vpd_p50 REAL, price_p50 INT, PRIMARY KEY (product_id, day));

CREATE TABLE trend_event (id INTEGER PRIMARY KEY, day DATE, kind TEXT, entity TEXT, entity_id INT,
  metrics TEXT /* JSON */, created_at TIMESTAMP);
CREATE INDEX ix_trend_day ON trend_event(day, kind);
```
`finds` и `categories` остаются; `finds` можно позже заполнять из `card_obs`. Миграции — `schema_version` +
пронумерованные функции в `app/migrations.py` (идемпотентно, только ADD).

Объём: ~50 карточек × 209 × 1–5 стр. ≈ 10–50 тыс. строк `ad` в день — SQLite справится; через 30 дней
старые `ad` без активности можно архивировать в агрегаты.

## 8. Performance (замеры этой машины: ~6–10 загрузок/мин)

| Сценарий | Загрузок | Объявлений в выдаче | Время |
|---|---|---|---|
| Sweep 209 × 5 стр. (с нуля) | ~1 050 | ~50 000 | 1.8–3 ч |
| Sweep 209 × 10 стр. (с нуля) | ~2 100 | ~100 000 | 3.5–6 ч |
| **Инкрементальный sweep** (до вчерашних id) | ~300–500 | новые за сутки | 0.5–1.5 ч |
| Deep scan: 5 карточек × 209 (базовая линия) | ~1 050 | — | 1.8–3 ч |
| Deep scan по бюджету 300 карточек/день | ~300 | — | 0.5–1 ч |

Экономия:
- **Из выдачи без открытия**: id, title, price, url, город, дата (с точностью до дня), промо, позиция, продавец
  (если есть в разметке), фото → `new_24h`, `fresh_7d`, цены, `promoted_share`, `disappeared`, повторы моделей,
  `unique_products` — **всё без карточек**. Просмотры — только из карточки.
- Инкрементальное листание: стоп на первом уже виденном id (sort=date).
- Не открывать повторно: карточку, замеренную < 24 ч назад (если не нужна скорость), промо, явные дубли
  (копии одного товара — 1 замер на группу), цены ниже порога, объявления старше 7 дней.
- Базовую линию категории мерить ротацией (каждая категория раз в 2–3 дня), а не все 209 ежедневно.
- Реалистичный дневной бюджет с одного домашнего IP: **~600–900 загрузок** (sweep ~400 + deep ~300) —
  выше растёт риск блоков; это главный ограничитель масштаба.

## 9. План разработки (под этот код)

| Фаза | Что | Зависит | Риск |
|---|---|---|---|
| **0. Runner** | вынести из `market.py` общий раннер (бюджет, gate, капча, прогресс) | — | средний (рефакторинг под тестами) |
| **1. History** | таблицы `ad`, `scan`, `category_daily`, `card_obs`, миграции; `/report` начинает писать в `ad` и `card_obs` | 0 | низкий |
| **2. Daily sweep** | `sweep.py` (инкрементальный), запуск раз в день при старте бота / по кнопке, агрегаты `category_daily` | 1 | блоки Avito |
| **3. Adaptive deep scan** | бюджет exploit 75 / explore 25 + базовая линия; текущий `/report` = deep scan | 2 | — |
| **4. Trends** | детекторы событий по собственной истории; включать после ≥ 7 дней данных | 3 + время | ложные сигналы → пороги на реальных данных |
| **5. Products** | `product_key`: нормализация + бренд/модель + RapidFuzz; embeddings/кластеры — если не хватит | 1 | средний |
| **6. Radar report** | ежедневный отчёт формата «MARKET RADAR»; LLM только для названий кластеров/итогового текста | 4–5 | — |
| **7. External** | Wordstat, goofish/1688 цены → маржа | 6 | доступность источников |

## 10. Итог / что нужно решить до начала

- Ежедневный прогон в 09:00 требует включённого ПК и человека на случай капчи. Предлагаю «раз в день при старте
  бота, если сегодня ещё не было» + кнопку.
- Объём ограничен Avito (один IP): ~600–900 загрузок/день — поэтому sweep обязательно инкрементальный,
  а deep scan — выборочный.
- Начать с Фаз 0–1 (без изменения поведения бота) — история начнёт копиться сразу, тренды станут осмысленными
  через 1–2 недели.
