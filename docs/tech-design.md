# Tech design: «Что выложить» — проверка рынка по кнопке

Источник: `docs/requirements.md` (ред. 3), `docs/decisions.md` (ADR-002). Дата: 2026-10-03.
Стек как есть: Python, Playwright+bs4, SQLite/SQLAlchemy 2, aiogram 3, APScheduler 3. Новых зависимостей нет.
Эталон (только чтение): `avito_niche/catalog.js` — список `TOP`, поиск подкатегорий, разбор «№ id · дата · N просмотров (+M сегодня)».

## 1. Суть

По `/report` (или кнопке) бот тем же Chromium-профилем, что и мониторинг D&G, обходит подкатегории товарных разделов
в порядке «давно не проверялись / были находки», в каждой листает выдачу «по дате» до первой непродвинутой карточки
старше 7 дней, склеивает копии товара, открывает ≤2 карточки на группу (≤12 на подкатегорию), у кандидатов сверяет
дату в профиле продавца и шлёт находки порциями. Бюджет 600 загрузок; прогон можно остановить и продолжить в течение 12 ч.
Расписания нет (AC-1.3): задача APScheduler остаётся только у мониторинга.

## 2. Схема

```
/report, кнопка ──► MarketCrawler.start() ──asyncio.Task──► run loop ──► BrowserGate ◄── Scanner (interval, как было)
/stop, кнопка ───► crawler.stop_requested = True                │  (уступает между подкатегориями)
                                                                ▼
                                 AvitoBrowserProvider.fetch(url) — пауза 2–5 с, warm-up, is_blocked
                                                                ▼ HTML
          avito_parser: parse_search_html + promoted_ids / parse_subcategories / parse_item_page / parse_seller_date
                                                                ▼
                 market_logic (чистые функции): возраст, отсечка, группы, выбор карточек, фильтры, порядок, текст
                                                                ▼
                    SQLite: categories, crawl_runs, finds        TelegramNotifier: send_text / edit_text
```

## 3. Модули (новых файлов: 2)

| Файл | Изменение |
|---|---|
| `app/providers/base.py` | `BrowserGate` (~15 строк): `asyncio.Lock` + счётчик ждущих; `async with gate.hold()`, свойство `contended`. Не-абстрактный `AvitoProvider.fetch(url) -> Page` (`NotImplementedError`), старый `FakeProvider` не ломается |
| `app/providers/avito_browser.py` | вынести тело `search()` в `fetch(url, ready_selector=None) -> Page(html, title, final_url)`: warm-up, goto, случайная пауза `PAGE_DELAY_MIN..MAX` перед загрузкой, `is_blocked → ProviderBlocked`; `search()` = обёртка. Счётчик `loads` (warm-up тоже считается). `_dump()` переиспользуется |
| `app/providers/avito_parser.py` | в `SELECTORS`: `seller_link` (`[data-marker="seller-link/link"]`, `a[href*="/user/"]`, `a[href*="/brands/"]`), `profile_item`; `TEXT_PATTERNS` (одно место, NFR-6): `promoted` = «Продвинуто\|Забронировано», `item_stats` = сегмент после «№ <id>» из `catalog.js`; `BLOCK_MARKERS` += «Вы робот». Функции: `promoted_ids(html)`, `parse_subcategories(html, section, limit)` (регулярка ссылок из `catalog.js`, путь → `/rossiya/<section>/<sub>`), `parse_item_page(html, id) -> ItemStats(views, today, date_text, seller_url)`, `parse_seller_date(html, id) -> str\|None`. `parse_published` += даты вида «25 сентября», «1 неделю назад» (порт `ageDays`) |
| `app/services/market_logic.py` **(новый)** | чистые функции, §6 |
| `app/services/market.py` **(новый)** | `MarketCrawler`: старт/продолжение, цикл обхода, бюджет, стоп, прогресс, порции, запись в БД |
| `app/services/notifier.py` | `send_text(...) -> int \| None` (id сообщения), `edit_text(message_id, text)`; ошибки логируются как сейчас |
| `app/services/scanner.py` | параметр `gate: BrowserGate \| None = None`; `async with gate.hold():` вокруг `provider_factory()`. Больше ничего (AC-5.4) |
| `app/db.py` | 3 новые таблицы (§4) |
| `app/telegram/handlers.py` | `/report`, `/stop`, кнопки в `/start`; `build_router(..., crawler)` |
| `app/main.py` | один `BrowserGate` на `Scanner` и `MarketCrawler`; при старте помечает зависший `running`-прогон как `interrupted` |

Пропущено: Alembic (`create_all` добавляет новые таблицы, старые не трогает — NFR-5), отдельный провайдер, «репозитории».

## 4. Данные (`app/db.py`)

```
categories  id PK, section str(64), name str(200), url str(300) UNIQUE, discovered_at,
            last_crawled_at NULL, last_run_id NULL, last_best_vpd int NULL, last_status str(16) NULL  -- ok|error
            last_days_covered float NULL
crawl_runs  id PK, started_at, finished_at NULL, status str(16)  -- running|stopped|blocked|interrupted|budget|done|failed
            loads int=0, finds_count int=0, progress_msg_id int NULL, note str NULL
finds       id PK, run_id FK, category_id FK, group_key str(300) INDEX, title, price_min int, price_max int,
            vpd int, today int NULL, age_days float, date_checked bool, copies int, url, external_id str(64),
            hot bool, sent bool=0, created_at, china_price int NULL   -- US-8, в v1 пусто
```

- Подкатегории раздела ищутся по странице раздела один раз и обновляются, если старше 30 дней (константа) — AC-2.1.
- «Подкатегория пройдена в этом прогоне» = `last_run_id == run.id`: отдельная очередь не хранится, продолжение
  просто пересчитывает порядок без пройденных (AC-4.4).
- «Уже было» (AC-7.1) = есть `finds` с тем же `group_key` и `category_id` в другом прогоне → дата первой такой находки.
- Удаления данных нет: ~50 подкатегорий и ~десятки находок за прогон.

## 5. Прогон (`MarketCrawler`)

**Старт** (`/report`): идёт задача → «проверка уже идёт» + текущий прогресс (AC-1.2). Иначе последний прогон в статусе
`stopped|blocked|interrupted|failed` и `started_at > now − REPORT_RESUME_HOURS` → «Продолжаю проверку» с тем же `run`
(бюджет считается от уже сделанных `loads`); иначе новый `run` и «Начинаю проверку рынка» (AC-1.1, AC-4.4).
Обход идёт в `asyncio.create_task`, ссылка хранится в crawler; хендлер сразу отвечает.

**Цикл**:
```
async with gate.hold(), provider_factory() as p:
  discover sections without fresh subcategories (1 загрузка на раздел)
  for cat in crawl_order(categories, run):                 # §6, сгруппировано по разделам
      if gate.contended: закрыть браузер, отдать gate, взять снова, открыть браузер   # AC-5.3
      if stop_requested or loads >= REPORT_BUDGET: break
      try: crawl_subcategory(p, cat)                        # ошибки карточек внутри — пропуск карточки
      except ProviderBlocked: alert; status=blocked; break  # AC-5.1, без повторов
      except BudgetExhausted|StopRequested: break           # проверяются перед каждой загрузкой (AC-4.3)
      except Exception: cat.last_status=error; log          # AC-5.2
      сменился раздел и есть неотправленные находки → порция (AC-4.2)
      прогресс: edit_text не чаще 1/мин (AC-4.1)
итог (все находки прогона) + «осталось N подкатегорий, пойдут первыми» при бюджете (AC-4.5)
```
Падение всей задачи → `status=failed`, лог + «⚠️ Проверка упала, найденное сохранено»; мониторинг не затронут (AC-5.4).

**crawl_subcategory**:
1. Выдача `cat.url?s=104&pmin=MIN_PRICE&p=N`, N = 1..`REPORT_MAX_PAGES`. Карточки без промо (`promoted_ids`) с
   `age_from_search(published_at)`: первая > `MAX_AGE_DAYS` → стоп листания; промо не останавливает (AC-2.2).
   Неразобранная дата → карточка пропускается, листание не останавливает. `days_covered` = возраст последней
   учтённой карточки, если упёрлись в лимит страниц (AC-2.3).
2. Отсев: цена < `MIN_PRICE` или возраст > 7 дн (по выдаче) — карточку не открываем (AC-2.4). `pmin` в URL только
   экономит страницы; локальный фильтр всё равно обязателен (Avito иногда игнорирует цену в URL).
3. `group_cards` → `pick_cards` (≤2 самых старых на группу, ≤`REPORT_CARDS_PER_SUBCAT` всего, приоритет группам
   с самыми старыми карточками — AC-2.4a/c). Вторая карточка группы открывается, только если первая не дала vpd ≥ порога.
4. Карточка: `parse_item_page`. Дата на странице > 7 дн → группа отброшена, профиль не смотрим (AC-3.5).
   `vpd = views / max(age, 1)`; < `VPD_MIN` → не находка (AC-2.5).
5. Кандидат и `CHECK_SELLER_DATE=true` → профиль продавца (1 загрузка). Дата найдена: > 7 дн → отброшен; иначе
   пересчёт возраста и vpd, повторный фильтр (AC-3.2/3.3). Не найдена/ошибка → находка «дата не проверена» (AC-3.4).
6. Запись: `finds`, `categories.last_crawled_at/last_run_id/last_best_vpd/last_days_covered`, `run.loads`.

**Бюджет и время**: ~10–15 загрузок на подкатегорию → 600 ≈ 40–60 подкатегорий; первый прогон тратит ещё до 25
на разделы. При паузе 2–5 с и загрузке ~2–3 с прогон ≈ 55–65 мин, чуть больше оценки NFR-2 (40–50). Ускоряет только
`PAGE_DELAY_MAX=4` или бюджет поменьше — на усмотрение пользователя.

**Соседство с мониторингом** (AC-5.3): `BrowserGate` = общий замок + счётчик ждущих. Прогон держит браузер, но перед
каждой подкатегорией смотрит `contended`: если проверка D&G ждёт — закрывает Chromium и отдаёт замок (asyncio.Lock
отдаёт его в порядке очереди), после проверки забирает и открывает снова (+1 загрузка warm-up). Задержка мониторинга ≤ одной
подкатегории (≤ ~20 загрузок ≈ 2–3 мин) ≪ 15 мин. `/check` во время прогона ждёт так же.

## 6. Чистые функции (`market_logic.py`)

- `age_days(published_at, now)` — дни, минимум 1 для vpd.
- `norm_title(title)` = `matcher.normalize` + удаление стоп-слов (`новый, новая, новое, новые, оригинал, original, new, шт, комплект`); цифры и модели сохраняются.
- `group_cards(cards)` — одинаковый `norm_title`; внутри — по цене по возрастанию, новая группа, если цена > 1.2 × минимальной в группе. Без нечёткого сравнения (открытый вопрос 3).
- `pick_cards(groups, per_group=2, limit=12)` — группы по возрасту самой старой карточки по убыванию, внутри — старшие первыми.
- `is_find(vpd, price, age)`, `is_hot(vpd)`.
- `crawl_order(cats, now)` — непройденные в этом прогоне; приоритет = `∞`, если ни разу не обходили, иначе
  `дней_с_обхода × (1 + min(last_best_vpd or 0, 500)/100)`; затем разделы по лучшему приоритету их подкатегорий,
  внутри раздела — по приоритету (нужно для порций «после раздела»). Пропущенные из-за бюджета сами окажутся первыми (AC-4.5/4.6).
- `sort_finds(finds)` — ключ `(not hot, already_seen, −vpd)` (AC-2.6, AC-7.1).
- `format_find`, `format_progress`, `format_summary`, `split_message(text, 4096)` — режет только по границам строк.

## 7. Контракт

| Вход | Ответ |
|---|---|
| `/report`, «🔎 Проверить рынок» | «Начинаю проверку рынка» / «Продолжаю проверку (пройдено 23 подкатегории)» / «Проверка уже идёт» + прогресс |
| `/stop`, «⏹ Стоп» | «Останавливаю после текущей страницы…» → итог; если прогона нет — «Проверка не идёт» |
| `/start` | текст + `ReplyKeyboardMarkup` с двумя кнопками (текстовые, ловятся `F.text == …`) |

Все — под существующим фильтром `F.chat.id == admin_chat_id` (AC-6.2).

Сообщения (HTML, каждая строка самодостаточна по тегам):
```
⏳ Проверка рынка: разделов 7/25 · подкатегорий 23 · загрузок 210/600 · находок 12   ← одно, правится ≤1/мин

<b>Аудио и видео — находки</b>
🔥 <a href="…">Pioneer XDJ-RX3</a> — 95–110 тыс ₽ · 180/день (+40 сегодня) · 3 дн · выставлено 2 раза · DJ-оборудование
<a href="…">Колонка JBL PartyBox 710</a> — 42 000 ₽ · 64/день (+9) · дата не проверена · 1 раз · Акустика · уже было 01.10

<b>Итог проверки — 03.10</b>  (все находки, тот же формат) + «покрыто N дней из 7: Телефоны (2/7), …»
+ «ошибка: …» + «осталось 140 подкатегорий, пойдут первыми» / «⚠️ Avito ограничил доступ, продолжу по /report»
```

## 8. Конфиг (`.env.example`, блок «Проверка рынка»)

```
REPORT_SECTIONS=<25 slug из TOP catalog.js>   REPORT_MAX_SUBCATS=18   REPORT_MAX_PAGES=5
REPORT_MAX_AGE_DAYS=7     MIN_PRICE=10000     VPD_MIN=50     VPD_HOT=100
REPORT_CARDS_PER_SUBCAT=12   REPORT_BUDGET=600   REPORT_RESUME_HOURS=12
CHECK_SELLER_DATE=true    PAGE_DELAY_MIN=2    PAGE_DELAY_MAX=5
```
«≤2 карточки на группу», «±20 %», «1 правка в минуту», «30 дней на обновление подкатегорий» — константы в коде.

## 9. Сбои и безопасность

| Ситуация | Поведение |
|---|---|
| Капча / «Доступ ограничен» / «Вы робот» | стоп сразу, алерт, итог, `status=blocked` → продолжение по `/report` в пределах 12 ч |
| Ошибка подкатегории / карточки / профиля | пропуск + «ошибка» в итоге / пропуск карточки / «дата не проверена» |
| 0 карточек, нет счётчика просмотров, нет даты в профиле | `_dump()` в `data/debug/` + warning (NFR-6) |
| Перезапуск бота / выключение ПК | при старте `running → interrupted`; найденное уже в БД |
| Профиль занят `app.auth` | браузер не стартует → `status=failed`, сообщение |
| Telegram не принял порцию | `finds.sent` остаётся 0, находки всё равно попадут в итог |

Безопасность: команды режет существующий фильтр чата; текст из Avito экранируется `html.escape`; без прокси,
решателей капчи и подмены отпечатков (NFR-3); браузер только читает страницы.

## 10. Тесты (pytest, без живого Avito — NFR-7)

Фикстуры (сохранить один раз живьём при остановленном боте): выдача подкатегории `s=104` с промо и датами
«N дней назад»/«25 сентября», страница раздела (подкатегории), карточка, профиль продавца, `avito_blocked.html` (есть).
- `test_parser.py` += `promoted_ids`, `parse_subcategories`, `parse_item_page`, `parse_seller_date`, новые форматы дат.
- `test_market_logic.py`: возраст/отсечка, `norm_title` (XDJ-RX2 ≠ XDJ-RX3), `group_cards` ±20 %, `pick_cards` (старшие первыми, 2/12),
  фильтры и 🔥, `crawl_order`, `sort_finds` с «уже было», `split_message`.
- `test_market.py` (`FakeProvider.fetch` по словарю URL→HTML, фейковый notifier, SQLite в памяти): бюджет, `/stop`,
  блок посреди → итог + состояние, продолжение < 12 ч без повторов и новый прогон ≥ 12 ч, ошибка подкатегории,
  «проверка уже идёт», прогресс ≤1/мин (время подменяется), профиль не открывается при старой дате на карточке.
- `test_scanner.py` += скан ждёт `BrowserGate` и получает его до следующей подкатегории. Старые 23 теста зелёные.

## 11. Решения, которые стоит подтвердить

1. **Как делить браузер с мониторингом.** Проверка рынка уступает браузер, когда мониторингу D&G он нужен: закрывает
   окно после текущей подкатегории, мониторинг отрабатывает, проверка продолжается. Задержка мониторинга — минуты.
2. **Свежесть по профилю продавца** (открытый вопрос 2) не проверена на живом Avito. По умолчанию включена (`CHECK_SELLER_DATE=true`);
   первым шагом реализации — посмотреть 3–5 поднятых объявлений; если дата на карточке не меняется, выключить и сэкономить бюджет.
3. **Фильтр цены прямо в адресе выдачи (`pmin=10000`).** Дешёвые объявления не занимают страницы, неделя покрывается
   глубже за тот же бюджет. Бот всё равно перепроверяет цену сам.
