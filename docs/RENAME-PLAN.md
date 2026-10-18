# Переименование: HR Штурман → JobShturman

Решение владельца 18.10.2026: продукт называется **JobShturman** (слитно, CamelCase), адреса — `jobshturman`. Видимое имя берётся из одной константы `core/hrshturman_core/brand.py` (`PRODUCT_NAME`); варианты, по которым агент узнаёт продукт, — там же (`ALIASES`) и в описании навыка.

## Уже сделано в коде

| Что | Было | Стало |
|---|---|---|
| Имя в README, docs, навыке, плагине, сообщениях hrs, описаниях MCP, странице анкеты, админке, LICENSE | HR Штурман / HR Shturman | JobShturman |
| Сайт, оферта, админка | `tools.owai.ru/hrshturman/`, `…/_/admin/` | `tools.owai.ru/jobshturman/`, `…/_/admin/` |
| Сервер ключей по умолчанию в hrs | `…/hrshturman/keys` | `…/jobshturman/keys`; старый адрес в `license.json` переписывается при чтении |
| Репозитории GitHub | `ZhdanDesign/hrshturman`, `…-method` | `ZhdanDesign/jobshturman`, `…-method` (GitHub оставляет редирект со старых имён) |
| Выпуск | `hrshturman-wheels.zip` | `jobshturman-wheels.zip`; в выпусках кладём оба имени, чтобы старые установщики находили файл |
| User-Agent сборщика для hh.ru | `HRShturman-collector/0.1` | `JobShturman-collector/0.1` |

## Не меняем сейчас: технические имена

Их знают установленные копии, конфиги агентов и сервер. Замена без миграции ломает работающих пользователей.

| Имя | Где | Почему пока остаётся | Как сменить позже, обратно совместимо |
|---|---|---|---|
| Команда `hrs` | `~/.local/bin/hrs`, конфиги Claude Code и Codex | Нейтральное, путь к ней записан в MCP-конфигах | Не нужно |
| MCP-сервер `hrshturman` | `claude mcp`, `~/.codex/config.toml`, имена инструментов `mcp__hrshturman__*` | Смена имени = новые имена инструментов, старые разрешения агентов перестают работать | `hrs setup` добавляет `jobshturman` и удаляет `hrshturman`; один выпуск держать оба |
| Навык и плагин `hrshturman` | `~/.claude/skills/hrshturman`, `~/.agents/skills/hrshturman`, `hrshturman@hrshturman` | Установленный плагин ищется по имени | Новый плагин `jobshturman` в том же маркетплейсе, старый с пометкой «переименован» до следующего выпуска |
| Ресурс MCP App `ui://hrshturman/app.html` | клиенты MCP Apps | Технический URI | Вместе с именем MCP-сервера |
| Сервис связки ключей `hrshturman` | Keychain macOS | Там лежат ключи пользователей | Читать оба сервиса, писать новый, старую запись удалять после чтения |
| Папка данных `HRShturman` | `~/Library/Application Support/HRShturman`, `%APPDATA%\HRShturman` | Анкеты, резюме, отклики пользователей | Если новой папки нет, а старая есть, переименовать один раз при запуске |
| Переменные `HRSHTURMAN_*`, `HRS_*` | окружение, тесты | Ими пользуются установки и тесты | Читать `JOBSHTURMAN_*` первыми, старые — как запасные |
| Пакеты `hrshturman`, `hrshturman-core`, `hrshturman-local`, `hrs-collector`, `hrs-keys` и модули Python | колёса, `uv tool` | `uv tool upgrade` ищет пакет по имени | Новый пакет-обёртка с зависимостью от старого, затем перенос |
| Корень репозитория и локальная папка `HRShturman` | машина владельца | По решению владельца не трогаем | — |
| Сервер: compose-проект `hrshturman-keys`, том `hrshturman-keys_keysdata`, `/opt/hrshturman-keys`, `/var/www/hrshturman*` | 90.156.208.37 | Данные ключей и бэкапы | Можно оставить навсегда: снаружи не видны |
| Лендинг | `landing/` | Его ведёт тред редизайна | На сервер переносим как есть в новый путь |
| `web-app/` | старое демо | Не входит в продукт | Удалить отдельной задачей |
| `PLAN-SKILL.md`, ТЗ и журналы решений | история | Исторические записи со старыми адресами | — |

## Порядок выкатки адресов

1. Сервер: бэкап nginx и статики; новые `location` для `/jobshturman/` (сайт, оферта, админка) и `/jobshturman/keys/` (API). Старый `/hrshturman/` — 301 на новый для страниц; `/hrshturman/keys/` продолжает проксировать на тот же сервер ключей (установленные копии шлют POST, редирект их ломает). `HRS_KEYS_RENEW_URL` → `https://tools.owai.ru/jobshturman/`.
2. GitHub: `gh repo rename` для обоих репозиториев, затем `git remote set-url`.
3. Выпуск v0.2.0 с `jobshturman-wheels.zip` и копией `hrshturman-wheels.zip`.
4. Проверка: чистая установка по README с нового адреса; копия, установленная со старого адреса, по-прежнему проверяет ключ.
