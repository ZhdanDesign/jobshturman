# JobShturman

Агент в Claude Code или Codex ведёт поиск работы по методике из 26 этапов: анкета, факты и резюме, сбор вакансий с hh.ru, Telegram и LinkedIn, отклики, учёт ответов и подготовка к встречам. Данные кандидата остаются на компьютере. Методика выдаётся по ключу доступа: [tools.owai.ru/jobshturman](https://tools.owai.ru/jobshturman/).

## Что нужно

- macOS (Windows — в проверке) и Claude Code или Codex.
- Ключ доступа вида `HRS-XXXX-XXXX-XXXX-XXXX-XXXX` из письма после оплаты.

## Установка

Одна команда в Терминале. Она поставит `uv` (если его нет), программу `hrs` из последнего выпуска и подключит её к Claude Code и Codex:

```bash
curl -fsSL https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.sh | sh
```

Windows (PowerShell): `irm https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.ps1 | iex`

После установки полностью перезапустите Claude Code или Codex: новый MCP-сервер виден только в новой сессии.

**Claude Code, через маркетплейс плагинов** (вместо команды выше):

```bash
claude plugin marketplace add ZhdanDesign/jobshturman
claude plugin install hrshturman@hrshturman
```

Потом в новой сессии напишите «JobShturman»: навык сам поставит `hrs` и попросит перезапустить Claude Code.

## Начало работы

1. Напишите агенту «JobShturman».
2. Агент попросит ключ доступа. Вставьте его из письма. Ключ хранится в связке ключей macOS, агенту его повторять не нужно.
3. Агент спросит, где удобнее отвечать: в чате или в окне браузера.
4. Напишите «начни этап 0». Агент проведёт анкету и покажет карту запроса.

Проверить ключ из Терминала: `hrs license`. Новая версия методики ставится только с вашего согласия: `hrs update` покажет, что изменилось, `hrs update --yes` поставит её.

## Где данные

macOS: `~/Library/Application Support/HRShturman`, Windows: `%APPDATA%\HRShturman`. Там анкета, факты, резюме, вакансии и отклики; методика — только в зашифрованном виде. Серверу ключей уходят ключ и случайный id установки, больше ничего.

## Обновление и удаление

Программа обновляется сама: раз в день по расписанию и после того, как агент закрыт. Если новая версия не запустится, вернётся прежняя; данные не трогаются. Обновить сразу: `hrs self-update`. Выключить: переменная `HRS_AUTO_UPDATE=off`.

Удалить:

```bash
claude mcp remove hrshturman --scope user
uv tool uninstall hrshturman
rm -rf ~/Library/Application\ Support/HRShturman ~/.claude/skills/hrshturman ~/.agents/skills/hrshturman
```

Блок `[mcp_servers.hrshturman]` из `~/.codex/config.toml` удалите вручную.

## Состав репозитория

`hrs/` — программа для агента (MCP-сервер, сборщик, команды). `core/`, `local-app/`, `collector/` — её части. `plugin/` — навык и установщики. `key-server/` — сервер ключей и админка. `landing/` — сайт. Методика в репозиторий не входит.

## Лицензия

[PolyForm Strict 1.0.0](LICENSE): использование только некоммерческое, без распространения и изменений. Методика распространяется по ключу на условиях [оферты](https://tools.owai.ru/jobshturman/oferta/).
