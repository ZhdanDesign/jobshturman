# API сервера ключей

Базовый адрес: `https://tools.owai.ru/jobshturman/keys`. Все ответы JSON, ошибки — `{"error": "<код>", "message": "<текст для человека>"}`. Время — ISO 8601 в UTC с `Z`.

## Клиент (`hrs`)

| Запрос | Тело или заголовок | Ответ |
|---|---|---|
| `POST /v1/activate` | `{"key", "install_id", "client"}` | `token`, `token_expires_at` (сутки, не дольше срока ключа) и статус |
| `GET /v1/status` | `Authorization: Bearer <token>` | статус |
| `GET /v1/bundle/latest` | `Authorization: Bearer <token>` | zip методики; заголовки `X-Bundle-Version`, `X-Bundle-Sha256`, `X-Bundle-Signature` (Ed25519, base64, подпись всего zip) |
| `GET /v1/health` | — | `{"ok": true}` |

Статус: `key_hint` (первые 4 знака ключа), `tariff {id, title}`, `expires_at`, `days_left`, `state` (`active` или `expiring` — меньше суток до конца), `warning` (текст со ссылкой на продление или `null`), `renew_url`, `bundle {version, sha256}`, `watermark` (8 hex-знаков: id водяного знака ключа, первые знаки sha256 от хэша ключа; hrs вписывает его в выданные тексты методики).

Отказы: `invalid_key` 404, `key_expired` 403, `key_revoked` 403, `device_limit` 403, `invalid_token`/`token_expired` 401, `no_bundle` 404. Ключ и токен проверяются при каждом запросе: отозванный ключ теряет доступ сразу, даже с живым токеном.

## Админка

Заголовок `Authorization: Bearer <токен>`. В `HRS_KEYS_ADMIN_TOKEN` можно перечислить несколько токенов через запятую, по одному на администратора; чтобы отозвать токен, уберите его из списка и перезапустите сервер. Для страницы админки с другого адреса задайте `HRS_KEYS_ADMIN_ORIGINS`.

| Запрос | Тело | Ответ |
|---|---|---|
| `GET /v1/admin/stats` | — | `keys`, `by_state {active, expiring, expired, revoked}`, `devices`, `latest_bundle` |
| `GET /v1/admin/tariffs` | — | список тарифов |
| `PATCH /v1/admin/tariffs/{id}` | любые из `title`, `price_rub_from`, `price_rub_to`, `months`, `device_limit`, `active` | тариф |
| `GET /v1/admin/keys?state=` | — | список ключей |
| `POST /v1/admin/keys` | `tariff_id`, необязательно `months` или `expires_at`, `order_ref`, `device_limit` | ключ; поле `key` с самим ключом есть **только в этом ответе** |
| `GET /v1/admin/keys/{id}` | — | ключ |
| `POST /v1/admin/keys/{id}/extend` | `{"months": N}` (прибавляются к более поздней из дат: сегодня или текущий конец) или `{"until": "2027-01-31"}` | ключ |
| `POST /v1/admin/keys/{id}/revoke` | `{"reason": "refund" \| "violation" \| "reissue" \| "other"}` | ключ |
| `POST /v1/admin/keys/{id}/restore` | — | ключ |
| `POST /v1/admin/keys/{id}/reset-devices` | — | ключ |
| `DELETE /v1/admin/keys/{id}/devices/{install}` | — | ключ; отвязано одно устройство по 8 знакам из `devices[].install`. `not_found` 404, если такого нет; `ambiguous_device` 409, если префикс подходит к нескольким |
| `GET /v1/admin/keys/{id}/events?limit=100` | — | последние события ключа, новые сверху: `[{kind, detail, at}]`; `kind` — `activate`, `check`, `download`, `denied` или `admin`; `limit` от 1 до 1000 |
| `GET /v1/admin/bundles` | — | опубликованные версии методики |
| `POST /v1/admin/bundles` | multipart, поле `file` — zip от `hrs-keys bundle build` | версия: `version`, `sha256`, `size`, `signature`, `published_at` |
| `GET /v1/admin/signing-key` | — | `public_key` для `hrs` |

Тариф: `id` (`self`, `guided`, `experts`), `title`, `price_rub_from`, `price_rub_to`, `months`, `device_limit`, `active` (0/1).

Ключ: `id`, `key_hint`, `tariff_id`, `issued_at`, `expires_at`, `device_limit`, `order_ref`, `revoked_at`, `revoke_reason`, `state` (`active`, `expiring`, `expired`, `revoked`), `devices [{install, first_seen, last_seen}]` (`install` — 8 знаков хэша установки), `last_seen`, `downloads`, `denied`, `watermark`.

Персональных данных в API нет: ни имён, ни почт, ни телефонов, ни IP. `order_ref` — только номер заказа платёжного сервиса.
