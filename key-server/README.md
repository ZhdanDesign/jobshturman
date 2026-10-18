# Сервер ключей HR Штурман

Выдаёт и проверяет ключи доступа, хранит тарифы и срок, считает активации и отдаёт подписанный пакет методики. Персональных данных не хранит: ключ лежит в виде HMAC-хэша, установка — хэшем случайного id, IP не пишется (`access_log` выключен). API — в [API.md](API.md).

## Запуск локально

```bash
cd key-server && uv sync
export HRS_KEYS_DATA=./data HRS_KEYS_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))") HRS_KEYS_ADMIN_TOKEN=...
uv run hrs-keys signing-key            # один раз: ключ подписи, печатает public_key для hrs
uv run hrs-keys bundle build "<папка Библиотека навыков>" bundle.zip --include "telegram_channels.csv=<путь к реестру каналов>"
uv run hrs-keys bundle publish bundle.zip
uv run hrs-keys issue self --order "<номер заказа>"   # ключ печатается один раз
uv run hrs-keys serve                  # 127.0.0.1:8098
uv run hrs-keys watermark decode copied.txt   # чей ключ у скопированного текста методики
```

`HRS_KEYS_SECRET` менять нельзя: из него считаются хэши ключей, со сменой все ключи перестанут находиться. `data/signing_key.pem` — закрытый ключ подписи; его публичную часть нужно вписать в `hrs/hrs_cli/license.py` (`PUBLIC_KEYS`) до выпуска `hrs`.

Пакет методики собирается из библиотеки навыков без файлов для нас: `references/acceptance.md` этапов, тестов библиотеки и их фикстур.

Остальные команды: `hrs-keys --help` (`tariffs`, `list`, `extend`, `revoke`, `restore`, `reset-devices`).

## Тесты

```bash
cd key-server && uv run pytest -q
```

## Развёртывание

`Dockerfile` и `compose.yaml` — для сервера Timeweb рядом с демо, порт `127.0.0.1:8098`, nginx отдаёт его по `/hrshturman/keys/`. Развёрнут: /opt/hrshturman-keys, compose-проект hrshturman-keys, nginx отдаёт /hrshturman/keys/.
