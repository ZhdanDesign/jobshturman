# HR Shturman — локальное приложение

Приложение работает на компьютере кандидата (macOS). Кандидат пользуется им через свой Claude по MCP. Архитектура и план описаны в [ARCHITECTURE.md](ARCHITECTURE.md).

Статус: черновик для разработки. Установщика пока нет, запуск выполняется из исходников.

## Что нужно

- macOS с включённым FileVault. Приложение не шифрует базу само и полагается на шифрование диска.
- Python 3.12+ и пакеты из `pyproject.toml`: `mcp` 2.x (как в `collector`), `python-docx`, `fpdf2`, `pypdf`. Проверено на mcp 2.3.0. Шрифт для PDF (Noto Sans) лежит в пакете, системный Arial не нужен. Обычно приложение запускают через `hrs serve` (см. `../hrs/README.md`).

## Подключить к Claude

Claude Code, из корня проекта `HRShturman/`:

```bash
claude mcp add hrshturman \
  --env PYTHONPATH="$PWD/core:$PWD/local-app" \
  -- python3 -m hrshturman_local.mcp_server
```

Claude Desktop, файл `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hrshturman": {
      "command": "/полный/путь/к/python3",
      "args": ["-m", "hrshturman_local.mcp_server"],
      "env": {
        "PYTHONPATH": "/полный/путь/HRShturman/core:/полный/путь/HRShturman/local-app"
      }
    }
  }
}
```

После перезапуска Claude напишите в чат: «Покажи статус поиска работы». Агент вызовет `status_report` и предложит следующий шаг.

## Локальный интерфейс

```bash
PYTHONPATH=core:local-app python3 -m hrshturman_local.ui_server
```

Откройте `http://127.0.0.1:8741`. Четыре раздела: диагностика, факты и резюме, карточки откликов, статус поиска. Интерфейс и Claude работают с одной базой, поэтому правка в одном месте сразу видна в другом. Сервер слушает только `127.0.0.1`, отклоняет запросы с чужим заголовком Host и принимает изменения только в JSON.

Утверждённое резюме можно выгрузить в DOCX и PDF кнопкой «Сделать DOCX и PDF» или инструментом `resume_render`. Файлы сохраняются в `<workspace>/materials/`. Приложение проверяет, что весь текст читается из файлов, а вёрстку нужно посмотреть самому.

Комплект под вакансию готовит AI через MCP, поэтому в интерфейсе нет кнопки «подготовить». Кандидат решает, разрешает отклик и отмечает отправку.

## Настройки

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `HRSHTURMAN_WORKSPACE` | `~/HRShturman/workspace` | Папка с базой `workspace.sqlite3` |
| `HRSHTURMAN_CANDIDATE` | `me` | candidate_id владельца компьютера |
| `HRSHTURMAN_TZ` | `Europe/Moscow` | Часовой пояс кандидата |
| `HRSHTURMAN_LIBRARY` | библиотека навыков из комплекта | Откуда брать процедуры этапов |
| `HRSHTURMAN_COLLECTOR_URL` | `http://127.0.0.1:8765` | Адрес сборщика вакансий (API с состоянием, `collector/CONTRACT.md`) |
| `HRS_COLLECTOR_TOKEN` | нет | Токен сборщика, если он запущен с авторизацией |

## HTTP-транспорт (задел, официально не поддерживается)

```bash
PYTHONPATH=core:local-app python3 -m hrshturman_local.mcp_server --http --port 8742
```

Сервер слушает только `127.0.0.1:8742/mcp`. Авторизации нет, поэтому наружу его не открывать. Подключение ChatGPT через туннель требует отдельного решения.

## Тесты

```bash
cd core && python3 -m unittest discover -s tests                                  # доменное ядро
cd local-app && PYTHONPATH=../core:. python3 -m unittest discover -s tests       # MCP через настоящий клиент
```
