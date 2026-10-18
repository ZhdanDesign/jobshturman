# hrs — HR Shturman для кодового агента

Одна команда вместо трёх пакетов: локальный MCP-сервер (`local-app`), сборщик вакансий (`collector`) и чтение документов через MarkItDown. Ядро — `core`. Работает на macOS и Windows, данные не покидают компьютер. План: [PLAN-SKILL.md](../PLAN-SKILL.md), этапы S1–S2.

## Установка для пользователя

Установщик лежит в навыке `plugin/skills/hrshturman/scripts/`. Он ставит uv, если его нет, затем `hrs` со своим Python 3.12, затем `hrs setup`. Источник `hrs` — первый аргумент или `HRS_SOURCE`: ссылка или путь к архиву выпуска, папка с колёсами или папка проекта.

```bash
sh plugin/skills/hrshturman/scripts/bootstrap.sh <источник>                                        # macOS
powershell -NoProfile -ExecutionPolicy Bypass -File plugin\skills\hrshturman\scripts\bootstrap.ps1 <источник>   # Windows
```

`hrs setup` можно запускать повторно, он ничего не дублирует:

- Claude Code: `claude mcp add --scope user hrshturman -- <полный путь к hrs> serve`.
- Codex: блок `[mcp_servers.hrshturman]` в `~/.codex/config.toml` с `default_tools_approval_mode = "approve"`. Без него Codex отклоняет вызовы MCP в `codex exec`. Прежний конфиг сохраняется в `config.toml.bak-hrs`.
- Навык копируется в `~/.agents/skills/hrshturman` (Codex и другие агенты) и в `~/.claude/skills/hrshturman`, если навык пришёл не из плагина.

Путь к `hrs` записывается полностью, поэтому агент, запущенный не из терминала, тоже его находит.

**Плагин для Claude Code и Cowork.** Корень проекта — маркетплейс (`.claude-plugin/marketplace.json`), плагин `hrshturman` в `plugin/` несёт только навык. MCP-сервер в плагин не вложен: в `.mcp.json` нельзя одинаково указать `~/.local/bin` для macOS и Windows, поэтому сервер подключает `hrs setup`.

```bash
claude plugin marketplace add <репозиторий или папка>
claude plugin install hrshturman@hrshturman
```

**Выпуск.** `uv run --no-project python hrs/build_dist.py` собирает `dist/hrshturman-<версия>-wheels.zip` с колёсами core, local-app, collector и hrs. Остальные зависимости ставятся из PyPI.

## Установка из исходников

Нужен [uv](https://docs.astral.sh/uv/). Он сам поставит Python 3.12+.

```bash
uv tool install --editable ./hrs        # из корня HRShturman
hrs paths                               # где лежат данные
```

## Подключить к агенту

```bash
claude mcp add hrshturman -- hrs serve          # Claude Code
codex mcp add hrshturman -- hrs serve           # Codex
```

Дальше в чате: «Покажи статус поиска работы».

## Команды

| Команда | Что делает |
|---|---|
| `hrs serve` | MCP-сервер по stdio: инструменты учёта (анкета, факты, резюме, отклики), сборщика (источники, сбор, поиск) и `document_import`. `--http --port 8742` — streamable HTTP на 127.0.0.1 |
| `hrs collect …` | Команды сборщика: `add-hh`, `add-telegram`, `import-telegram`, `run`, `vacancies` и др. (`hrs collect --help`) |
| `hrs doc import <файл>` | PDF, DOCX, PPTX, XLSX, HTML, TXT, MD, RTF → Markdown в локальной папке документов |
| `hrs paths` | Папки данных, библиотеки и id кандидата |
| `hrs setup [--skill-dir DIR] [--from-plugin]` | Подключить `hrs` к Claude Code и Codex и поставить навык; печатает отчёт JSON |
| `hrs login <ключ> [--server URL]` | Ввести ключ доступа: активировать его на этом компьютере и скачать методику |
| `hrs sync` | Проверить ключ и скачать новую версию методики |
| `hrs license` | Состояние ключа: тариф, срок, ссылка на продление, скачана ли методика |
| `hrs stage <n>` | Процедура одного этапа методики (0–25); весь пакет не выдаётся |
| `hrs ui [--view intake\|progress] [--no-open]` | Страница анкеты этапа 0 и маршрута в браузере: `127.0.0.1`, одноразовая ссылка |
| `hrs selftest [--library DIR] [--json]` | Контрактная проверка пакета методики, без модели: 26 этапов выдаются, водяной знак не меняет текст, YAML-шапку, код и таблицы, ссылки ведут в пакет, лишних файлов нет, этапы не смешаны. Замечания к текстам методики только печатает |

Сборщик работает внутри `hrs serve`: отдельный сервер на порту 8765 больше не нужен.

## Где данные

| ОС | Папка |
|---|---|
| macOS | `~/Library/Application Support/HRShturman` |
| Windows | `%APPDATA%\HRShturman` |
| Linux | `~/.local/share/hrshturman` |

Внутри: `workspace/` (база учёта и `documents/`), `collector/` (источники и вакансии), `license.json` (id установки и ответ сервера ключей, доступ только владельцу; сам ключ доступа на macOS лежит в связке ключей, сервис `hrshturman`, на Windows и Linux — в файле `access_key` с доступом только владельцу, `license_status` предупреждает об этом; ключ из старого `license.json` переносится туда при первом чтении) и `methodology/bundle.enc` (методика, подписанная сервером). На диске пакет лежит только зашифрованным: AES-256-GCM, ключ выводится через HKDF из ключа доступа и id установки. Расшифровка идёт в памяти, агент получает по одному этапу (`stage_procedure`, `hrs stage <n>`), и в каждом выданном тексте невидимый водяной знак ключа: его id из символов нулевой ширины после первого слова каждого абзаца, заголовка и пункта списка, кроме блоков кода. Старый открытый `bundle.zip` удаляется при `hrs sync`.

Методика доступна, пока ключ действует. Сервер ключей спрашивается не чаще раза в сутки; без сети последний ответ действует 7 дней, но не дольше срока ключа. За день до конца срока `license_status` даёт агенту предупреждение со ссылкой на продление. После конца срока этапы методики не выдаются, а наработки кандидата остаются на месте. Подпись пакета проверяется ключом из `hrs_cli/license.py` (`PUBLIC_KEYS`) или из переменной `HRS_BUNDLE_PUBKEYS`; адрес сервера — `HRS_KEY_SERVER`. Переменные `HRSHTURMAN_HOME`, `HRSHTURMAN_WORKSPACE`, `HRS_COLLECTOR_DATA`, `HRSHTURMAN_LIBRARY`, `HRSHTURMAN_CANDIDATE` меняют пути.

## Тесты

```bash
cd hrs && uv run --with pytest --with respx pytest -q
```
