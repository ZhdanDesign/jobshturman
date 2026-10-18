# Контракт сборщика вакансий JobShturman

Версия контракта: **1.0** (stable) · сборщик 0.1.0 · 10.10.2026
Статус: **stable**. Поля и операции ниже реализованы, покрыты тестами и проверены живым сбором по 213 источникам и сквозным прогоном local-app. `GET /health` отдаёт ту же версию в поле `contract`. Изменения — только по правилам §10.

Источник истины для типов — `src/hrs_collector/models.py` (Pydantic v2). OpenAPI-схема генерируется сервером: `GET /openapi.json`. Если этот файл и код расходятся, прав код, а файл надо поправить.

## 1. Что это

Один Python-пакет `hrs_collector` с общим ядром и тремя входами:

| Вход | Для кого | Как запустить |
|---|---|---|
| HTTP API | web-app (сервис), local-app, любые интеграции | `hrs-collector serve --port 8765` |
| MCP (stdio) | агент кандидата (Claude Code, Codex и т.п.) | `hrs-collector mcp` |
| CLI | разработчик, ручной запуск | `hrs-collector run`, `vacancies`, `sources` … |

Ядро работает в двух режимах:

- **Без состояния** (`/v1/ops/*`). Вызывающий хранит курсор и результаты. Этот режим для web-app: у сервиса свой Postgres, сборщик там просто исполнитель операций.
- **С состоянием** (`/v1/sources`, `/v1/runs`, `/v1/vacancies`). Сборщик сам хранит источники, курсоры и вакансии в SQLite. Этот режим для локальной версии и для MCP.

Оба режима используют один и тот же алгоритм `collect_batch` (`engine.py`), поэтому семантика курсоров и покрытия одинаковая.

## 2. Источники и транспорты

| platform | transport | Что нужно | Что умеет | Проверено запуском |
|---|---|---|---|---|
| `telegram` | `telegram_web` | ничего | публичные каналы с веб-превью `t.me/s/<username>`, вся история через пагинацию | да |
| `telegram` | `telegram_mtproto` | `api_id`, `api_hash`, сессия аккаунта кандидата | всё, что видит аккаунт, включая закрытые каналы, где он уже состоит, и каналы без веб-превью | нет, нужен аккаунт |
| `hh` | `hh_web` | ничего | публичный поиск hh.ru, полная карточка вакансии | да |
| `hh` | `hh_api` | access token hh.ru | официальный API | нет, нужен токен |
| `linkedin` | `linkedin_guest` | ничего | гостевой поиск вакансий и полное описание; выдача не сортируется по дате, окно времени задаётся фильтром площадки | да |

`SourceSpec` описывает, что читать. Курсора и секретов в нём нет.

```json
{"platform": "telegram", "transport": "telegram_web", "kind": "channel", "config": {"username": "c_level_top"}}
{"platform": "telegram", "transport": "telegram_mtproto", "kind": "channel", "config": {"chat_id": "2459163270"}}
{"platform": "hh", "transport": "hh_web", "kind": "search", "config": {"text": "финансовый директор", "area": "1", "params": {}}}
{"platform": "linkedin", "transport": "linkedin_guest", "kind": "search", "config": {"keywords": "CFO", "location": "Dubai"}}
```

`config.params` у поисковых источников передаётся площадке как есть (например `experience`, `schedule` для hh.ru, `f_TPR` для LinkedIn).

## 3. Операции

Названия совпадают с ТЗ §11. Каждый ответ содержит конверт `OpEnvelope`:

```
platform, transport, operation, adapter_version, observed_at, status, evidence{}, error: ErrorInfo|null
ErrorInfo = {code, message, stage, can_retry, retry_after_seconds}
```

### 3.1 `probe_capabilities` — `POST /v1/ops/probe`

Вход: `{spec, credentials?}`. Реально читает первую страницу источника.

Выход `ProbeResult`: `status` (`ok`|`error`), `levels` {`configured`, `service_reachable`, `account_authorized`, `scenario_verified`} — четыре уровня хранятся раздельно, как требует ТЗ §11; `access_status`; `supported_operations`; `limits` (честные ограничения транспорта текстом); `source_meta` (`current_title`, `stable_chat_id`, `total_results`).

`account_authorized = null` значит, что у транспорта нет аккаунта (анонимное чтение), а не «неизвестно».

### 3.2 `collect_batch` — `POST /v1/ops/collect`

Вход:

```json
{
  "spec": SourceSpec,
  "cursor": CursorState | null,
  "params": {"lookback_days": 7, "overlap_hours": 72, "page_budget": 10, "fetch_full": false, "now": null},
  "credentials": Credentials | null
}
```

Выход `BatchResult`: `status` (`ok` | `partial` | `no_new` | `error`), `items: VacancyData[]`, `next_cursor: CursorState`, `coverage`, `source_meta`.

```
Coverage = {from_at, to_at, complete, pages, items_seen, reason}
```

Правила курсора (ТЗ target_channels §5, ТЗ §13 п.8):

1. Первый вызов (`cursor = null`) читает назад до `now − lookback_days`.
2. Следующие вызовы читают назад до `watermark_at` и закрывают любой простой целиком (пять дней, месяц), а не только последние 72 часа. Затем, если хватает бюджета, ещё `overlap_hours` назад, чтобы поймать правки. Перепроверка правок не блокирует продвижение: если бюджета на неё не хватило, вызов всё равно `ok`, а `coverage.reason = "page_budget: overlap re-check truncated"`.
3. Если `page_budget` кончился раньше, чем диапазон стал непрерывным, статус `partial`, `watermark` не двигается, а незавершённый диапазон сохраняется в `resume_*`. Следующий вызов сначала дочитывает его, потом читает свежий верх.
4. **Вызывающий обязан сначала сохранить `items`, потом `next_cursor`.** Повторное чтение идемпотентно по `item_key`, поэтому сбой между этими шагами ничего не теряет и не размножает.
5. `status = error` может нести непустые `items`: прочитанное до ошибки сохранять можно и нужно, курсор в этом случае указывает на последнюю сохранённую точку.
6. `partial` и `error` не означают «вакансий нет». Ноль показывается только вместе с `coverage`.
7. Рекламные карточки hh.ru (`raw.promoted = true`) стоят вверху выдачи вне порядка дат; на границу окна они не влияют, но сохраняются как обычные записи.
8. Если площадка не сортирует выдачу по дате (сейчас это `linkedin_guest`), окно `[since, now]` передаётся ей фильтром, а чтение заканчивается только в конце выдачи или по бюджету.

`CursorState` непрозрачен для вызывающего: хранить целиком, не интерпретировать.

```
CursorState = {version, watermark_at, watermark_id, resume_token, resume_stop_at, resume_top_at, resume_top_id, history_complete}
```

### 3.3 `fetch_full_item` — `POST /v1/ops/fetch`

Вход: `{spec, external_id, credentials?}`. Выход `FetchResult`: `status`, `item: VacancyData` с `completeness = "full"`.

Нужен для hh.ru и LinkedIn: в поисковой выдаче нет описания. У Telegram пост уже полный.

### 3.4 `read_inbox`, `prepare_action`, `submit_action`, `reconcile_action`

**Не реализованы.** Имена зарезервированы за этим контрактом, формы входа и выхода будут взяты из ТЗ §11 (таблица операций) и `references/contracts.md`. Ни один транспорт сейчас не пишет на площадки. Пока этих операций нет, продукт честно называется подготовкой (ТЗ §20). Перед реализацией нужно отдельное согласование: `operation_id`, хранение receipt и правила `send_unknown` уже описаны в ТЗ §13, их берём без изменений.

## 4. Формат вакансии

`VacancyData` — одна собранная публикация, приведённая к полям вакансии.

| Поле | Тип | Смысл |
|---|---|---|
| `item_key` | str | Устойчивый ключ публикации: `hh:<vacancy_id>`, `telegram:<chat_id>:<message_id>`, `linkedin:<job_id>`. Переименование канала или смена поискового запроса его не меняют. |
| `platform` | `hh`\|`telegram`\|`linkedin` | |
| `external_id` | str | id на площадке (vacancy_id, message_id, job_id) |
| `url` | str\|null | ссылка на оригинал |
| `title`, `company`, `location` | str\|null | для Telegram извлекаются эвристикой из текста поста и могут быть пустыми или неточными |
| `salary` | `{min, max, currency, gross, period}`\|null | |
| `employment` | str\|null | как отдаёт площадка (`FULL`, `Full-time`, текст из поста) |
| `work_format` | str[] | `ON_SITE`, `REMOTE`, `HYBRID` … (hh.ru) |
| `published_at` | datetime UTC\|null | время публикации; у LinkedIn точность до дня |
| `edited_at` | datetime\|null | если площадка его отдаёт (hh.ru full, MTProto) |
| `text` | str | текст публикации без разметки |
| `contacts` | `{emails[], telegram[], phones[], links[]}` | найденные в тексте |
| `is_vacancy` | `likely`\|`unlikely`\|`unknown` | грубая эвристика по словам; не оценка релевантности |
| `completeness` | `snippet`\|`full` | есть ли полное описание |
| `content_hash` | str | sha256 содержимого; изменение = новая версия |
| `fingerprint` | str | ключ кандидатов в дубли по **всему** тексту (не по первым 140 знакам) |
| `raw` | object | транспортные подробности (`chat_id`, `edited`, `employer_id`, `key_skills` …); состав может расширяться |

В режиме с состоянием возвращается `Vacancy` = `VacancyData` плюс:

| Поле | Смысл |
|---|---|
| `vacancy_id` | `vac_` + sha256(candidate_id, item_key)[:20]; детерминирован |
| `candidate_id`, `source_id` | владелец и первый источник |
| `version` | растёт при изменении `content_hash`; прошлые версии хранятся |
| `status` | `normalized` (оценка, выбор и отправка — в других блоках) |
| `first_seen_at`, `last_seen_at`, `changed_at` | |
| `canonical_id`, `duplicate_status` | `unique` / `duplicate` (тот же текст, что у `canonical_id`) / `suspected_duplicate` (та же должность и компания, другой текст). Ничего не удаляется. |

Одна публикация сейчас даёт одну запись. Пост-подборка с несколькими вакансиями пока остаётся одной записью; разбиение — задача AI-этапа в другом блоке (ТЗ §8 разрешает несколько Vacancy на пост).

## 5. Учётные данные кандидата

Два способа, выбираются вызывающим:

**На одну операцию (сервис).** Поле `credentials` в теле `probe` / `collect` / `fetch` / `runs`:

```json
{"hh": {"access_token": "..."},
 "telegram": {"api_id": 123, "api_hash": "...", "session_string": "..."}}
```

Сборщик держит их в памяти только на время вызова, не пишет на диск, в логи, в ответы и в курсор. Поля `SecretStr`, в сериализации маскируются. Web-app хранит секреты у себя (`platform_credentials`, шифрование конвертом) и передаёт расшифрованное значение на одну операцию по внутренней сети.

**Локально (локальная версия).** `hrs-collector telegram-login` создаёт сессию Telegram интерактивно: владелец сам вводит код и пароль 2FA. Сессия сохраняется в `<data>/credentials.json` с правами 0600. Токен hh.ru — через `HRS_HH_ACCESS_TOKEN`. Переданные в вызове `credentials` важнее локальных.

MCP-инструменты секретов не принимают и не возвращают никогда.

## 6. Сервисная авторизация

- Если задан `HRS_COLLECTOR_TOKEN`, каждый запрос к `/v1/*` требует `Authorization: Bearer <token>`. Сравнение в постоянное время. `/health` открыт.
- Без токена сервер слушает только loopback; `serve --host 0.0.0.0` без токена откажется стартовать.
- В сервисе сборщик живёт во внутренней сети, токен — секрет окружения web-app и сборщика. Ротация: перезапуск с новым значением.
- Изоляция арендаторов в режиме без состояния — ответственность web-app (сборщик ничего не хранит). В режиме с состоянием каждая запись несёт `candidate_id`, выборки фильтруются по нему (тест `test_candidates_are_isolated`).

## 7. API с состоянием

Все запросы принимают `candidate_id` (query, по умолчанию `default`).

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | версия, контракт, транспорты |
| GET | `/v1/sources` | источники с `access_status`, курсором, backoff |
| POST | `/v1/sources` | добавить/заменить источник `SourceIn {source_id, spec, title?, category?, enabled?, origin?}` |
| PATCH | `/v1/sources/{id}` | `{enabled}`; выключенный источник не читается и получает `access_status = disabled` |
| POST | `/v1/sources/import-telegram-registry` | `{csv_path}`: импорт реестра 210 каналов; повтор сохраняет выбор `enabled` и курсоры |
| POST | `/v1/sources/{id}/probe` | probe + запись статуса доступа |
| POST | `/v1/runs` | `RunRequest`; `?wait=true` ждёт результат, иначе возвращает `run_id` сразу |
| GET | `/v1/runs/{id}`, `/v1/runs` | отчёт запуска |
| GET | `/v1/vacancies` | поиск, см. ниже |
| GET | `/v1/vacancies/{id}` | `{vacancy, origins[], versions[], related[]}` |
| POST | `/v1/vacancies/{id}/fetch-full` | дочитать полное описание |
| GET | `/v1/stats` | счётчики |

`RunRequest = {candidate_id, source_ids?, platforms?, params: CollectParams, force, credentials?}`. `force` игнорирует backoff.

Отчёт запуска `Run`: по каждому источнику `status` ∈ `ok` | `no_new` | `partial` | `no_access` | `error` | `skipped`, плюс `pages`, `items_seen`, `items_new`, `items_updated`, `coverage`, `error`. Неудачный источник не останавливает остальные. Источники разных площадок читаются параллельно, одной площадки — последовательно с паузой между запросами.

Backoff: временная ошибка — 15 мин × 2^(n−1), максимум 24 ч, либо `Retry-After` площадки; отсутствие доступа — 24 ч. Один источник одновременно читает только один запуск (lease); устаревший запуск не может записать страницу (`lease_lost`).

`GET /v1/vacancies` параметры: `q` (все слова, без учёта регистра, по title/company/location/text), `platform`, `source_id`, `since` (ISO), `is_vacancy`, `include_duplicates` (по умолчанию `false`), `limit` ≤ 500, `page_token`. Порядок: `published_at` по убыванию, затем `vacancy_id`. Пагинация keyset: одинаковый запрос к одинаковому состоянию даёт одинаковый ответ.

## 8. MCP-инструменты

`list_sources`, `add_search_source`, `add_telegram_channel`, `set_source_enabled`, `probe_source`, `run_collection`, `get_run`, `search_vacancies`, `get_vacancy`, `fetch_full_vacancy`. Они вызывают то же ядро, что и HTTP API в режиме с состоянием. В инструкциях сервера агенту сказано: `partial` и `no_access` не означают отсутствия вакансий, а собранная вакансия не даёт права откликаться.

## 9. Коды ошибок

`not_configured`, `service_unreachable`, `not_authenticated`, `captcha_required`, `two_factor_required`, `rate_limited`, `unavailable`, `private_invite_required`, `web_preview_unavailable`, `changed_page`, `invalid_schema`, `unsupported_environment`, `incomplete_source`, `lease_lost`, `internal_error`. Смысл совпадает с ТЗ §15. `can_retry` в `ErrorInfo` — решение сборщика, повторять ли автоматически.

`access_status` источника: `unknown`, `accessible`, `not_joined`, `private_invite_required`, `read_failed`, `unavailable`, `not_authenticated`, `disabled`.

## 10. Совместимость

- Добавление необязательных полей и новых транспортов повышает минорную версию (1.1, 1.2 …), старые клиенты продолжают работать. Удаление или смена смысла поля — новая мажорная версия контракта.
- `raw` и `source_meta` не стабильны, на них нельзя строить логику других блоков.
- `item_key` Telegram через `telegram_web` и `telegram_mtproto` должен совпадать (`chat_id` без префикса −100). Совпадение для MTProto не проверено живым аккаунтом.
