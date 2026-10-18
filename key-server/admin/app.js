"use strict";

// Админка открыта по /jobshturman/_/admin/, сервер ключей — /jobshturman/keys/ (см. key-server/API.md).
const API_BASE = "../../keys/v1/admin";
const TOKEN_KEY = "hrs-keys-admin-token";
const root = document.getElementById("app");
const dialog = document.getElementById("dialog");
const filters = { query: "", status: "all", tariff: "all" };
const data = { keys: [], tariffs: [], stats: null, current: null, role: null, draft: null, route: null, file: null };
const statusNames = { active: "Активен", expiring: "Истекает", expired: "Истёк", revoked: "Заблокирован" };
const reasonNames = { refund: "Возврат оплаты", violation: "Нарушение условий", reissue: "Перевыпуск ключа", other: "Другое" };
const deniedNames = { revoked: "ключ заблокирован", expired: "срок истёк", device_limit: "превышен лимит устройств" };
// Методика: главный промпт — файл внутри пакета, агент читает его первым (key-server/API.md, «Редактор методики»).
const GENESIS = "agent/genesis.md";
const MANIFEST = "library_manifest.json";
const roleNames = { admin: "Администратор", methodologist: "Автор методики" };
const channelNames = { test: "Тестовые ключи", stable: "Все пользователи" };
const changeNames = { added: "Добавлен", removed: "Удалён", changed: "Изменён" };
const progressNames = {
  intake: "Анкета заполнена", facts: "Собраны факты", facts_confirmed: "Факты подтверждены", resume: "Есть резюме",
  vacancies: "Найдены вакансии", scored: "Вакансии оценены", decided: "Решения по вакансиям", sent: "Отклики отправлены", funnel: "Есть ответы работодателей",
};
let fullKey = null;
let noticeTimer;
let renderSeq = 0;
let shellShown = false;

// Оболочка админки появляется в DOM только после входа; до входа на странице одна карточка.
// Автор методики видит только методику, главный промпт и версии; администратор — всё.
const NAV = {
  keys: ['<circle cx="8" cy="15" r="5"/><path d="m11.5 11.5 8-8L22 6l-2 2-2-2-2 2 2 2-3 3"/>', 'Ключи <span class="nav-count" id="nav-count"></span>'],
  tariffs: ['<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M7 15h4"/>', "Тарифы"],
  methodology: ['<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5Z"/><path d="M19 19v2H6M8 7h7M8 11h5"/>', "Методика"],
  genesis: ['<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5 5-2Z"/>', "Главный промпт"],
  bundles: ['<path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z"/><path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5"/>', "Версии"],
};

function shell(role) {
  const pages = role === "admin" ? ["keys", "tariffs", "methodology", "genesis", "bundles"] : ["methodology", "genesis", "bundles"];
  const home = role === "admin" ? "keys" : "methodology";
  return `
  <a class="skip-link" href="#main">Перейти к содержимому</a>
  <aside class="sidebar">
    <a class="brand" href="#${home}" aria-label="JobShturman — ${role === "admin" ? "список ключей" : "методика"}">
      <span class="brand-mark" aria-hidden="true">H</span>
      <span><strong>JobShturman</strong><small>${role === "admin" ? "Сервер ключей" : "Редактор методики"}</small></span>
    </a>
    <div class="nav-label">${roleNames[role] || "Управление"}</div>
    <nav aria-label="Главная навигация">
      ${pages.map((page) => `<a href="#${page}" data-nav="${page}">
        <svg viewBox="0 0 24 24" aria-hidden="true">${NAV[page][0]}</svg>
        ${NAV[page][1]}
      </a>`).join("")}
    </nav>
    <div class="sidebar-foot"><button class="button" data-action="logout" type="button">Выйти</button><p>Токен хранится только в этой вкладке.</p></div>
  </aside>
  <div class="workspace">
    <header class="topbar"><span>${role === "admin" ? "Администрирование" : "Редактор"} <span class="divider">/</span> <strong id="breadcrumb">Ключи</strong></span><span class="topbar-note">${role === "admin" ? "Ручная выдача после оплаты" : "Каждое сохранение — новая версия черновика"}</span></header>
    <main id="main" tabindex="-1"></main>
    <footer class="page-footer">JobShturman <span>${role === "admin" ? "Только ключи, устройства и номера заказов" : "Пользователи получают методику только после публикации"}</span></footer>
  </div>`;
}

class AuthError extends Error {}

const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
// Без « г.» в конце, чтобы после даты не получалось «г..».
const date = (value) => value ? new Date(value).toLocaleDateString("ru-RU", { day: "2-digit", month: "short", year: "numeric" }).replace(/\s?г\.$/, "") : "—";
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const tariffTitle = (id) => data.tariffs.find((tariff) => tariff.id === id)?.title || id;
const keyLabel = (key) => `HRS-${key.key_hint}…`;
const rub = (value) => `${Number(value).toLocaleString("ru-RU")} ₽`;
const priceOf = (tariff) => tariff.price_rub_from === tariff.price_rub_to ? rub(tariff.price_rub_from) : `${rub(tariff.price_rub_from)}–${rub(tariff.price_rub_to)}`;
const months = (n) => `${n} ${n % 10 === 1 && n % 100 !== 11 ? "месяц" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "месяца" : "месяцев"}`;

// --- токен и запросы ---

function readToken() {
  try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function writeToken(token) {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* Без sessionStorage вход не переживёт перезагрузку. */ }
}

function apiUrl(path) {
  return new URL(`${API_BASE}${path}`, window.location.href).toString();
}

function errorText(body, status) {
  if (typeof body?.message === "string") return body.message;
  if (Array.isArray(body?.detail)) return `Проверьте поля: ${body.detail.map((item) => item.loc?.at(-1)).filter(Boolean).join(", ")}.`;
  return `Сервер ключей ответил ошибкой ${status}.`;
}

async function api(path, { method = "GET", json, form } = {}) {
  const token = readToken();
  if (!token) throw new AuthError("Введите токен администратора.");
  const headers = { Authorization: `Bearer ${token}` };
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  else if (form) body = form;
  let response;
  try {
    response = await fetch(apiUrl(path), { method, headers, body, cache: "no-store" });
  } catch {
    throw new Error("Сервер ключей недоступен. Проверьте соединение и повторите.");
  }
  const result = await response.json().catch(() => null);
  if (response.status === 401 || response.status === 403) throw new AuthError(`${errorText(result, response.status).replace(/\.$/, "")}. Войдите заново.`);
  if (!response.ok) throw Object.assign(new Error(errorText(result, response.status)), { status: response.status, code: result?.error });
  return result;
}

function logout(message = "", isError = true) {
  writeToken("");
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  Object.assign(data, { keys: [], stats: null, current: null, role: null, draft: null, route: null, file: null });
  if (dialog.open) dialog.close();
  renderLogin(message, isError);
}

// --- модель ---

function addMonths(value, count) {
  const result = new Date(value);
  const originalDay = result.getDate();
  result.setDate(1);
  result.setMonth(result.getMonth() + count);
  const lastDay = new Date(result.getFullYear(), result.getMonth() + 1, 0).getDate();
  result.setDate(Math.min(originalDay, lastDay));
  return result;
}

function inputDate(value) {
  const d = new Date(value);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Конец выбранного дня по местному времени браузера, без долей секунды.
function endOfDay(until) {
  return new Date(`${until}T23:59:59`);
}

function filteredKeys() {
  const query = filters.query.trim().toLocaleLowerCase("ru");
  return data.keys.filter((key) =>
    (!query || [key.key_hint, key.order_ref || "", key.id].some((text) => text.toLocaleLowerCase("ru").includes(query))) &&
    (filters.tariff === "all" || key.tariff_id === filters.tariff));
}

function eventText(event) {
  const detail = event.detail || "";
  if (event.kind === "activate") return "Вход с устройства";
  if (event.kind === "check") return "Проверка ключа";
  if (event.kind === "download") return detail ? `Скачан пакет v${detail}` : "Скачан пакет";
  if (event.kind === "denied") return `Отказ: ${deniedNames[detail] || detail}`;
  if (detail === "issue") return "Ключ выдан";
  if (detail === "restore") return "Ключ разблокирован";
  if (detail === "reset devices") return "Сброшены все устройства";
  if (detail.startsWith("extend to ")) return `Ключ продлён до ${dateTime(detail.slice(10))}`;
  if (detail.startsWith("revoke ")) return `Ключ заблокирован · ${reasonNames[detail.slice(7)] || detail.slice(7)}`;
  if (detail.startsWith("unbind device ")) return `Устройство отвязано · ${detail.slice(14)}`;
  return detail || event.kind;
}

function changedTariffFields(tariff, fields) {
  const next = {
    title: String(fields.get("title") || "").trim(),
    price_rub_from: Number(fields.get("price_rub_from")), price_rub_to: Number(fields.get("price_rub_to")),
    months: Number(fields.get("months")), device_limit: Number(fields.get("device_limit")),
    active: fields.get("active") === "on",
  };
  if (!next.title) throw new Error("Укажите название тарифа.");
  if (!["price_rub_from", "price_rub_to", "months", "device_limit"].every((name) => Number.isSafeInteger(next[name]) && next[name] >= 0)) throw new Error("Цены, срок и лимит — целые числа не меньше нуля.");
  if (next.price_rub_from > next.price_rub_to) throw new Error("Цена «от» не может быть больше цены «до».");
  return Object.fromEntries(Object.entries(next).filter(([name, value]) => (name === "active" ? Boolean(tariff.active) : tariff[name]) !== value));
}

// --- разметка ---

function icon(name) {
  const paths = {
    plus: '<path d="M12 5v14M5 12h14"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    back: '<path d="m10 5-7 7 7 7M3 12h18"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
}

function badge(key) {
  return `<span class="badge ${escapeHTML(key.state)}"><span class="dot" aria-hidden="true"></span>${statusNames[key.state] || escapeHTML(key.state)}</span>`;
}

function notice(text) {
  const element = document.getElementById("notice");
  clearTimeout(noticeTimer);
  element.textContent = text;
  element.hidden = false;
  noticeTimer = setTimeout(() => { element.hidden = true; }, 4500);
}

const mainEl = () => document.getElementById("main");

function setNav(page) {
  if (!shellShown) {
    root.innerHTML = shell(data.role);
    shellShown = true;
    document.title = data.role === "admin" ? "Сервер ключей · JobShturman" : "Методика · JobShturman";
  }
  document.getElementById("breadcrumb").textContent = { tariffs: "Тарифы", bundles: "Версии", key: "Карточка ключа", keys: "Ключи", methodology: "Методика", genesis: "Главный промпт", edit: "Файл методики" }[page];
  const current = { key: "keys", edit: "methodology" }[page] || page;
  document.querySelectorAll("[data-nav]").forEach((link) => {
    if (link.dataset.nav === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function renderLogin(message = "", isError = true) {
  renderSeq++;
  shellShown = false;
  document.title = "Вход";
  root.innerHTML = `<main class="login-screen"><form id="login-form" class="panel login" autocomplete="off">
    <h1>Вход</h1>
    <div class="field"><label for="token">Токен</label><input id="token" name="token" type="password" required spellcheck="false" autocomplete="off"></div>
    ${message && !isError ? `<p class="muted" role="status">${escapeHTML(message)}</p>` : ""}
    <p class="form-error" role="alert" ${message && isError ? "" : "hidden"}>${isError ? escapeHTML(message) : ""}</p>
    <button type="submit" class="button primary">Войти</button></form></main>`;
}

function renderFailure(message) {
  mainEl().innerHTML = `<div class="empty"><h1>Не удалось загрузить</h1><p class="muted">${escapeHTML(message)}</p><button class="button" data-action="reload">Повторить</button></div>`;
}

function renderKeys() {
  const stats = data.stats;
  const by = stats.by_state || {};
  mainEl().innerHTML = `
    <div class="page-heading"><div><h1>Ключи доступа</h1><p>Выдача и управление доступом к методике.</p></div>
      <button class="button primary" data-action="issue">${icon("plus")}Выдать ключ</button></div>
    <div class="stats" aria-label="Сводка по всем ключам">
      <div class="stat"><span class="stat-top"><span class="dot success-text" aria-hidden="true"></span>Активных</span><strong>${(by.active || 0) + (by.expiring || 0)}</strong><small class="muted">Из них истекают за сутки: ${by.expiring || 0}</small></div>
      <div class="stat"><span class="stat-top"><span class="dot warning-text" aria-hidden="true"></span>Истекли</span><strong>${by.expired || 0}</strong><small class="muted">Можно продлить</small></div>
      <div class="stat"><span class="stat-top"><span class="dot danger-text" aria-hidden="true"></span>Заблокировано</span><strong>${by.revoked || 0}</strong><small class="muted">Доступ приостановлен</small></div>
      <div class="stat"><span class="stat-top"><span class="dot" aria-hidden="true"></span>Устройств</span><strong>${stats.devices}</strong><small class="muted">Методика ${stats.latest_bundle ? `v${escapeHTML(stats.latest_bundle)}` : "не опубликована"}</small></div>
    </div>
    <div class="filters">
      <div class="search">${icon("search")}<input id="search" type="search" value="${escapeHTML(filters.query)}" placeholder="Начало ключа, номер заказа или ID" aria-label="Поиск по началу ключа, номеру заказа или ID"></div>
      <select id="status-filter" aria-label="Фильтр по статусу"><option value="all">Все статусы</option>${Object.entries(statusNames).map(([id, title]) => `<option value="${id}" ${filters.status === id ? "selected" : ""}>${title}</option>`).join("")}</select>
      <select id="tariff-filter" aria-label="Фильтр по тарифу"><option value="all">Все тарифы</option>${data.tariffs.map((tariff) => `<option value="${escapeHTML(tariff.id)}" ${filters.tariff === tariff.id ? "selected" : ""}>${escapeHTML(tariff.title)}</option>`).join("")}</select>
    </div>
    <div class="result-line"><span id="result-count" role="status" aria-live="polite"></span><button class="text-button" id="reset-filters" data-action="reset">Сбросить фильтры</button></div>
    <div class="table-wrap" role="region" aria-label="Список ключей, прокручивается по горизонтали" tabindex="0">
      <table><thead><tr><th scope="col">Ключ</th><th scope="col">Тариф</th><th scope="col">Статус</th><th scope="col">Срок до</th><th scope="col">Устройства</th><th scope="col">Последний вход</th><th scope="col">Номер заказа</th></tr></thead><tbody id="key-rows"></tbody></table>
      <div id="empty" class="empty" hidden><h3>Ключи не найдены</h3><p class="muted">Измените запрос или сбросьте фильтры.</p></div>
    </div>
    <div class="info-note">${icon("info")}<p>Ключи выдаются вручную после оплаты. За сутки до окончания агент предупреждает о продлении. Сервер хранит только хэш ключа, устройства и номер заказа.</p></div>`;
  updateList();
}

function updateList() {
  const visible = filteredKeys();
  document.getElementById("key-rows").innerHTML = visible.map((key) => `<tr>
    <td><a class="key-link" href="#key-${escapeHTML(key.id)}" aria-label="Открыть ключ ${escapeHTML(keyLabel(key))}">${escapeHTML(keyLabel(key))}</a></td>
    <td>${escapeHTML(tariffTitle(key.tariff_id))}</td><td>${badge(key)}</td><td>${date(key.expires_at)}</td>
    <td>${key.devices.length}<span class="muted"> / ${key.device_limit}</span></td><td class="muted small">${dateTime(key.last_seen)}</td><td class="mono muted">${escapeHTML(key.order_ref || "—")}</td>
  </tr>`).join("");
  document.getElementById("result-count").textContent = `Показано ${visible.length} из ${data.keys.length} ключей`;
  document.getElementById("empty").hidden = visible.length !== 0;
  document.getElementById("reset-filters").hidden = !filters.query && filters.status === "all" && filters.tariff === "all";
}

function renderDetail(key, events) {
  const reason = escapeHTML(reasonNames[key.revoke_reason] || key.revoke_reason || "");
  const facts = [
    ["Идентификатор", `<code>${escapeHTML(key.id)}</code>`], ["Тариф", escapeHTML(tariffTitle(key.tariff_id))],
    ["Дата выдачи", dateTime(key.issued_at)], ["Срок до", dateTime(key.expires_at)],
    ["Лимит устройств", `${key.devices.length} из ${key.device_limit}`], ["Последний вход", dateTime(key.last_seen)],
    ["Скачиваний пакета", key.downloads], ["Отказов в доступе", key.denied],
    ["Номер заказа", key.order_ref ? `<code>${escapeHTML(key.order_ref)}</code>` : "—"],
    ["Версии методики", key.channel === "test" ? "Тестовые: получает новую версию первым" : "Обычные"],
    ["Версия у пользователя", key.pinned_version ? `<span class="mono">v${escapeHTML(key.pinned_version)}</span>` : "Ещё не скачана"],
  ];
  if (key.revoked_at) facts.push(["Заблокирован", `${dateTime(key.revoked_at)} · ${reason}`]);
  const revoked = key.state === "revoked";
  mainEl().innerHTML = `
    <a class="back-link" href="#keys">${icon("back")}Все ключи</a>
    <div class="page-heading detail-heading"><div><h1 class="mono">${escapeHTML(keyLabel(key))}</h1>${badge(key)}</div>
      <div class="actions"><button class="button" data-action="toggle-channel">${key.channel === "test" ? "Вернуть в обычные" : "Сделать тестовым"}</button><button class="button" data-action="extend">Продлить</button><button class="button ${revoked ? "" : "danger"}" data-action="${revoked ? "restore" : "revoke"}">${revoked ? "Разблокировать" : "Заблокировать"}</button></div></div>
    ${revoked ? `<p class="warning-note blocked-note"><strong>Доступ заблокирован.</strong> Причина: ${reason}</p>` : key.state === "expired" ? '<p class="warning-note">Срок ключа истёк. Доступ к методике отключён; наработки пользователя остаются в его локальном хранилище.</p>' : key.state === "expiring" ? '<p class="warning-note">До окончания меньше суток. Агент предупреждает пользователя и даёт ссылку на продление.</p>' : ""}
    <div class="detail-grid"><div class="stack">
      <section class="panel"><h2>Детали ключа</h2><dl class="facts">${facts.map(([title, value]) => `<dt>${title}</dt><dd>${value}</dd>`).join("")}</dl></section>
      <section class="panel"><div class="panel-heading"><h2>Устройства <span class="muted">${key.devices.length} / ${key.device_limit}</span></h2>${key.devices.length ? '<button class="button" data-action="reset-devices">Сбросить все</button>' : ""}</div>
        ${key.devices.length ? key.devices.map((device) => `<div class="device"><div class="device-info"><code>${escapeHTML(device.install)}</code><p class="small muted">Первый вход ${dateTime(device.first_seen)}<br>Последний вход ${dateTime(device.last_seen)}</p></div><button class="button" data-action="detach" data-device="${escapeHTML(device.install)}" aria-label="Отвязать устройство ${escapeHTML(device.install)}">Отвязать</button></div>`).join("") : '<p class="muted">Активированных устройств пока нет.</p>'}
        <p class="small muted">Начало хэша случайного ID установки. Отвязка освобождает место для нового устройства.</p>
      </section>
    </div><section class="panel"><h2>Журнал событий</h2>${events.length ? `<ol class="timeline">${events.map((event) => `<li>${escapeHTML(eventText(event))}<time datetime="${escapeHTML(event.at)}">${dateTime(event.at)}</time></li>`).join("")}</ol>` : '<p class="muted">Событий пока нет.</p>'}</section></div>`;
}

function renderTariffs() {
  mainEl().innerHTML = `<div class="page-heading"><div><h1>Тарифы</h1><p>Цена, срок и лимит устройств по умолчанию для новых ключей.</p></div></div>
    <div class="tariff-grid">${data.tariffs.map((tariff, i) => `<section class="panel tariff"><span class="tariff-number">0${i + 1} / ТАРИФ</span>
      <h2>${escapeHTML(tariff.title)}</h2><p class="price">${priceOf(tariff)}</p>
      <form class="tariff-form" data-id="${escapeHTML(tariff.id)}">
        <div class="field"><label for="title-${i}">Название</label><input id="title-${i}" name="title" maxlength="60" required value="${escapeHTML(tariff.title)}"></div>
        <div class="form-row"><div class="field"><label for="from-${i}">Цена от, ₽</label><input id="from-${i}" name="price_rub_from" type="number" min="0" step="1" required value="${tariff.price_rub_from}"></div>
          <div class="field"><label for="to-${i}">Цена до, ₽</label><input id="to-${i}" name="price_rub_to" type="number" min="0" step="1" required value="${tariff.price_rub_to}"></div></div>
        <div class="form-row"><div class="field"><label for="months-${i}">Срок, месяцев</label><input id="months-${i}" name="months" type="number" min="1" max="60" step="1" required value="${tariff.months}"></div>
          <div class="field"><label for="devices-${i}">Лимит устройств</label><input id="devices-${i}" name="device_limit" type="number" min="1" max="10" step="1" required value="${tariff.device_limit}"></div></div>
        <label class="check"><input type="checkbox" name="active" ${tariff.active ? "checked" : ""}> Доступен для выдачи</label>
        <p class="form-error" role="alert" hidden></p>
        <div class="dialog-actions"><button type="submit" class="button primary">Сохранить</button></div>
      </form></section>`).join("")}</div>
    <div class="info-note">${icon("info")}<p>Цены предварительные, по решениям владельца. Срок и лимит — значения по умолчанию для новых ключей; выданные ключи их сохраняют.</p></div>`;
}

function renderBundles(bundles) {
  const upload = data.role === "admin" ? `<section class="panel upload"><h2>Загрузить готовый пакет</h2>
      <form id="bundle-form"><div class="field"><label for="bundle-file">Пакет zip</label><input id="bundle-file" name="file" type="file" accept=".zip,application/zip" required><small>Собирается командой <code>hrs-keys bundle build</code>; версия берётся из bundle.json внутри. Пакет сразу становится доступным всем. Обычный путь — публикация из раздела «Методика».</small></div>
        <div class="field"><label for="bundle-note">Что изменилось</label><textarea id="bundle-note" name="note" maxlength="1000" required placeholder="Например: новый этап про переговоры о зарплате, поправлены шаблоны писем"></textarea><small>Обязательно. Агент покажет это людям, когда предложит обновиться.</small></div>
    <label class="check"><input type="checkbox" name="important"> Важное: поставить без вопроса</label><small class="muted">Для исправлений, без которых работать нельзя. Агент сам перейдёт на эту версию и объяснит человеку, что изменилось.</small>
        <p class="form-error" role="alert" hidden></p>
        <div class="dialog-actions"><button type="submit" class="button primary">${icon("upload")}Опубликовать</button></div></form></section>` : "";
  const target = (bundle, channel) => bundle.channels.includes(channel) ? "" : `<button class="button" data-action="set-current" data-version="${escapeHTML(bundle.version)}" data-channel="${channel}">${channel === "test" ? "Тестовым" : "Всем"}</button>`;
  mainEl().innerHTML = `<div class="page-heading"><div><h1>Версии методики</h1><p>Версия с меткой «Тестовые ключи» предлагается тестовым ключам, с меткой «Все пользователи» — остальным. Люди переходят на неё, когда согласятся; «Важное» ставится само. Откат — сделать доступной прежнюю версию: ключи с более новой вернутся на неё.</p></div></div>
    ${upload}
    <div class="table-wrap" role="region" aria-label="Версии методики, прокручивается по горизонтали" tabindex="0">
      <table class="bundles"><thead><tr><th scope="col">Версия</th><th scope="col">Опубликована</th><th scope="col">Что изменилось</th><th scope="col">Ключей на версии</th><th scope="col">Размер</th><th scope="col">Сделать доступной</th></tr></thead><tbody>${bundles.map((bundle) => `<tr>
        <td><span class="mono">v${escapeHTML(bundle.version)}</span>${bundle.channels.map((channel) => ` <span class="badge ${channel === "stable" ? "active" : "expiring"}">${channelNames[channel]}</span>`).join("")}${bundle.min_hrs ? `<br><small class="muted">Нужен hrs ${escapeHTML(bundle.min_hrs)} или новее</small>` : ""}</td>
        <td>${dateTime(bundle.published_at)}</td><td class="small">${bundle.important ? '<span class="badge expiring">Важное</span> ' : ""}${escapeHTML(bundle.note || (bundle.revision ? `Черновик, версия ${bundle.revision}` : "—"))}</td><td>${bundle.keys ?? 0}</td>
        <td>${(bundle.size / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} КБ</td><td><div class="actions">${target(bundle, "test")}${target(bundle, "stable")}</div></td></tr>`).join("")}</tbody></table>
      ${bundles.length ? "" : '<div class="empty"><h3>Версий пока нет</h3><p class="muted">Опубликуйте методику из раздела «Методика».</p></div>'}
    </div>
    <div class="info-note">${icon("info")}<p>«Всем» делает версию доступной и тестовым ключам. Старые установки hrs (до 0.4.0) не спрашивают и получают версию сразу; если она им не подходит — последнюю подходящую и подсказку обновить программу.</p></div>`;
}

// --- методика: черновик, маршрут, файлы, история ---

const stageNumber = (stages, index) => stages[index].status === "off" ? null : stages.slice(0, index).filter((stage) => stage.status !== "off").length;

function routeCopy(draft) {
  return {
    version: draft.manifest_version || "",
    steps: draft.steps.map((step) => ({ ...step })),
    stages: draft.stages.map(({ files, position, number, ...stage }) => ({ ...stage })),
    dirty: false,
  };
}

function checkPanel(check) {
  if (!check) return "";
  const item = (entry) => `<li><code>${escapeHTML(entry.code)}</code>${entry.stage ? ` · ${escapeHTML(entry.stage)}` : ""} — ${escapeHTML(entry.detail)}</li>`;
  return `<section class="panel" id="check-panel"><h2>Проверка ${check.ok ? '<span class="badge active">Можно публиковать</span>' : '<span class="badge revoked">Есть ошибки</span>'}</h2>
    ${check.errors.length ? `<p class="small">Ошибки не дают опубликовать:</p><ul class="issues">${check.errors.map(item).join("")}</ul>` : ""}
    ${check.findings.length ? `<details><summary class="small">Замечания: ${check.findings.length}. Публикации не мешают</summary><ul class="issues">${check.findings.map(item).join("")}</ul></details>` : ""}
    ${check.ok && !check.findings.length ? '<p class="muted">Ошибок и замечаний нет.</p>' : ""}</section>`;
}

const fileSize = (file) => `${(file.size / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} КБ`;
const fileItem = (file, dir = "") => `<li><a class="mono small" href="#edit/${encodeURIComponent(file.path)}">${escapeHTML(dir && file.path.startsWith(`${dir}/`) ? file.path.slice(dir.length + 1) : file.path)}</a> <span class="muted small">${fileSize(file)}</span></li>`;

function stageFiles(stage) {
  const paths = data.draft.stages.find((saved) => saved.id === stage.id)?.files || [];
  return data.draft.files.filter((file) => paths.includes(file.path));
}

function routePanel(open = new Set()) {
  const route = data.route;
  const option = (value, title, selected) => `<option value="${escapeHTML(value)}" ${value === selected ? "selected" : ""}>${escapeHTML(title)}</option>`;
  const stepTitle = (id) => route.steps.find((step) => step.id === id)?.title || id || "Без шага";
  const rows = route.stages.map((stage, i) => {
    const number = stageNumber(route.stages, i);
    const files = stageFiles(stage);
    const off = stage.status === "off";
    return `<li class="stage${off ? " off" : ""}" data-stage="${i}" data-id="${escapeHTML(stage.id)}"><details ${open.has(stage.id) ? "open" : ""}>
      <summary><span class="stage-number mono">${number === null ? "—" : number}</span><span class="stage-title">${escapeHTML(stage.title || stage.id)}</span>
        <span class="stage-meta"><span class="badge">${escapeHTML(stepTitle(stage.step))}</span>${off ? '<span class="badge revoked">Выключен</span>' : ""}<span class="muted small">${files.length ? `Файлов: ${files.length}` : "Новый этап"}</span></span></summary>
      <div class="stage-body">
        <div class="stage-fields">
          <div class="field"><label for="stage-title-${i}">Название</label><input id="stage-title-${i}" data-field="title" value="${escapeHTML(stage.title || "")}" maxlength="120"></div>
          <div class="field"><label for="stage-step-${i}">Шаг</label><select id="stage-step-${i}" data-field="step">${route.steps.map((step) => option(step.id, step.title, stage.step)).join("")}${route.steps.some((step) => step.id === stage.step) ? "" : option(stage.step || "", `Нет шага «${stage.step || ""}»`, stage.step || "")}</select></div>
          <div class="field"><label for="stage-progress-${i}">Чем отмечается прогресс</label><select id="stage-progress-${i}" data-field="progress">${option("", "—", stage.progress || "")}${Object.entries(progressNames).map(([id, title]) => option(id, title, stage.progress || "")).join("")}</select></div>
          <label class="check"><input type="checkbox" data-field="on" ${off ? "" : "checked"}> Этап включён</label>
        </div>
        <div class="stage-files"><h3>Файлы этапа <span class="muted small mono">${escapeHTML(stage.dir || "")}</span></h3>
          ${files.length ? `<ul>${files.map((file) => fileItem(file, stage.dir)).join("")}</ul>` : '<p class="muted small">Файлы появятся после сохранения маршрута.</p>'}
          <button class="button" data-action="file-new" data-dir="${escapeHTML(stage.dir || "")}">${icon("plus")}Файл в этап</button></div>
        <div class="actions stage-actions"><button class="button" data-action="stage-up" ${i ? "" : "disabled"}>↑ Выше</button><button class="button" data-action="stage-down" ${i < route.stages.length - 1 ? "" : "disabled"}>↓ Ниже</button><button class="button danger" data-action="stage-remove">Убрать из маршрута</button></div>
      </div></details></li>`;
  }).join("");
  const steps = route.steps.map((step, i) => `<li data-step="${i}"><input data-field="step-title" value="${escapeHTML(step.title || "")}" aria-label="Название шага ${escapeHTML(step.id)}" maxlength="80"><code>${escapeHTML(step.id)}</code>
    <span class="muted small">Этапов: ${route.stages.filter((stage) => stage.step === step.id).length}</span>
    <button class="button ghost" data-action="step-remove" aria-label="Убрать шаг ${escapeHTML(step.id)}" ${route.stages.some((stage) => stage.step === step.id) ? "disabled" : ""}>${icon("close")}</button></li>`).join("");
  return `<div class="panel-heading"><h2>Маршрут <span class="muted">включено этапов: ${route.stages.filter((stage) => stage.status !== "off").length}</span></h2>
      <div class="actions"><button class="button" data-action="step-add">${icon("plus")}Шаг</button><button class="button" data-action="stage-add">${icon("plus")}Этап</button></div></div>
    <p class="small muted">Агент идёт по включённым этапам сверху вниз; номер этапа — его место в маршруте. Откройте этап, чтобы изменить его или открыть его файлы. Выключенный или убранный этап остаётся в файлах черновика.</p>
    <details class="steps-box"><summary>Шаги — крупные группы этапов, их видит пользователь: ${route.steps.map((step) => escapeHTML(step.title || step.id)).join(" · ")}</summary><ol class="steps">${steps}</ol></details>
    <ol class="stages">${rows}</ol>
    <div class="form-row route-save"><div class="field"><label for="route-version">Версия методики</label><input id="route-version" data-field="version" value="${escapeHTML(route.version)}" maxlength="40" placeholder="0.5.0"></div>
      <div class="dialog-actions"><span class="small muted" role="status">${route.dirty ? "Есть несохранённые изменения" : ""}</span><button class="button" data-action="route-reset" ${route.dirty ? "" : "disabled"}>Отменить</button><button class="button primary" data-action="route-save" ${route.dirty ? "" : "disabled"}>Сохранить маршрут</button></div></div>`;
}

function updateRoute() {
  const panel = document.getElementById("route-panel");
  const open = new Set([...(panel.querySelectorAll?.("li.stage") || [])].filter((item) => item.querySelector("details")?.open).map((item) => item.dataset.id));
  panel.innerHTML = routePanel(open);
}

function commonFiles(draft) {
  const staged = new Set(draft.stages.flatMap((stage) => stage.files));
  const main = draft.files.filter((file) => file.path === GENESIS || file.path === MANIFEST || file.path.startsWith("agent/"));
  return [["Главный промпт и маршрут", main], ["Не относятся к этапам", draft.files.filter((file) => !staged.has(file.path) && !main.includes(file))]].filter(([, files]) => files.length);
}

function renderMethodology(draft, revisions) {
  data.draft = draft;
  data.route = routeCopy(draft);
  const head = draft.head;
  const current = Object.entries(draft.current || {}).map(([channel, version]) => `${channelNames[channel]}: <span class="mono">v${escapeHTML(version)}</span>`).join(" · ");
  const publish = `<button class="button" data-action="publish" data-channel="test">Опубликовать тестовым</button><button class="button primary" data-action="publish" data-channel="stable">Опубликовать всем</button>`;
  mainEl().innerHTML = `<div class="page-heading"><div><h1>Методика</h1><p>${head ? `Черновик: версия ${head.id} от ${dateTime(head.at)}, ${head.files} файлов.` : "Черновик пуст."} ${current ? `Сейчас у пользователей — ${current}.` : "Пользователи пока ничего не получили."}</p></div>
      ${head ? `<div class="actions">${publish}</div>` : ""}</div>
    ${head ? "" : `<div class="info-note">${icon("info")}<p>Начните с загрузки библиотеки (zip) ниже или возьмите опубликованную версию. Старая библиотека с папками stage-00…25 подходит: маршрут и главный промпт добавятся сами.</p></div>`}
    ${checkPanel(draft.check)}
    ${head ? `<section class="panel" id="route-panel">${routePanel()}</section>` : ""}
    ${head ? `<section class="panel"><div class="panel-heading"><h2>Общие файлы</h2><div class="actions"><button class="button" data-action="file-new">${icon("plus")}Новый файл</button></div></div>
      <p class="small muted">Файлы этапов открываются из маршрута выше.</p>
      ${commonFiles(draft).map(([title, files]) => `<details class="files" open><summary>${escapeHTML(title)} <span class="muted small">${files.length}</span></summary><ul>${files.map((file) => fileItem(file)).join("")}</ul></details>`).join("")}</section>` : ""}
    <section class="panel upload"><h2>Загрузить</h2>
      <form id="draft-upload-form"><div class="field"><label for="draft-file">Файл или архив zip</label><input id="draft-file" name="file" type="file" required><small>Архив zip целиком заменяет черновик: прежний останется в истории. Отдельный файл добавляется или заменяет файл с тем же путём.</small></div>
        <div class="field"><label for="draft-path">Путь для отдельного файла</label><input id="draft-path" name="path" placeholder="skills/salary-talk/references/примеры.md" maxlength="300" spellcheck="false"><small>Пусто — файл ляжет в корень под своим именем.</small></div>
        <p class="form-error" role="alert" hidden></p>
        <div class="dialog-actions"><button type="button" class="button" data-action="import-current">Взять опубликованную версию</button>${head ? '<button type="button" class="button" data-action="export">Скачать черновик</button>' : ""}<button type="submit" class="button primary">${icon("upload")}Загрузить</button></div></form></section>
    ${revisions.length ? `<section class="panel"><h2>История черновика</h2><ol class="timeline revisions">${revisions.map((rev, i) => `<li><span><strong>Версия ${rev.id}</strong> · ${escapeHTML(rev.message)} <span class="muted small">${escapeHTML(roleNames[rev.by] || rev.by)}, файлов: ${rev.files}</span></span>
      <span class="actions"><time datetime="${escapeHTML(rev.at)}">${dateTime(rev.at)}</time><button class="button ghost" data-action="diff" data-rev="${rev.id}">Изменения</button>${i ? `<button class="button ghost" data-action="restore-rev" data-rev="${rev.id}">Вернуть</button>` : ""}</span></li>`).join("")}</ol></section>` : ""}`;
}

function renderFile(file, { genesis = false } = {}) {
  data.file = file;
  const fixed = file.path === GENESIS || file.path === MANIFEST;
  const heading = genesis
    ? `<div class="page-heading"><div><h1>Главный промпт</h1><p>Правила, по которым агент (Claude Code, Codex и другие) ведёт кандидата через все этапы. Агент читает их первыми; они главнее текста навыка в установке. К пользователям попадают после публикации.</p></div>
        <div class="actions"><button class="button" data-action="publish" data-channel="test">Опубликовать тестовым</button><button class="button primary" data-action="publish" data-channel="stable">Опубликовать всем</button></div></div>`
    : `<a class="back-link" href="#methodology">${icon("back")}Методика</a>
      <div class="page-heading detail-heading"><div><h1 class="mono">${escapeHTML(file.path)}</h1>${file.isNew ? '<span class="badge expiring">Новый файл</span>' : ""}</div></div>`;
  mainEl().innerHTML = `${heading}
    <form id="file-form" class="panel editor-form">
      <div class="field"><label for="file-text">${genesis ? `<code>${GENESIS}</code>` : "Текст"}${file.isNew ? "" : ` <span class="muted small">${(file.size / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} КБ</span>`}</label><textarea id="file-text" name="text" class="editor" spellcheck="false"></textarea></div>
      <div class="field"><label for="file-message">Что изменилось</label><input id="file-message" name="message" maxlength="200" placeholder="Необязательно: попадёт в историю"></div>
      <p class="form-error" role="alert" hidden></p>
      <div class="dialog-actions">${file.isNew || fixed ? "" : '<button type="button" class="button danger" data-action="file-delete">Удалить файл</button>'}<button type="submit" class="button primary">Сохранить</button></div>
    </form>
    <div class="info-note">${icon("info")}<p>Сохранение создаёт новую версию черновика; прежнюю можно вернуть в истории раздела «Методика». Если файл успели изменить в другой вкладке, сохранение не перезапишет чужую правку.</p></div>`;
  document.getElementById("file-text").value = file.text;
}

function renderNoGenesis() {
  mainEl().innerHTML = `<div class="empty"><h1>Главного промпта пока нет</h1><p class="muted">Он появится, когда в черновик загрузят библиотеку методики.</p><a class="button" href="#methodology">Открыть методику</a></div>`;
}

// Построчное сравнение для окна «Изменения»; большие файлы показываются целиком до и после.
function lineDiff(before, after) {
  const a = (before ?? "").split("\n");
  const b = (after ?? "").split("\n");
  if (a.length * b.length > 4_000_000) return [...a.map((line) => ["-", line]), ...b.map((line) => ["+", line])];
  const lcs = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) { out.push([" ", a[i]]); i++; j++; }
    else if (j < b.length && (i === a.length || lcs[i][j + 1] > lcs[i + 1][j])) out.push(["+", b[j++]]);
    else out.push(["-", a[i++]]);
  }
  // Неизменённые строки сворачиваются: остаются по три вокруг правок, пропуск отмечен «…».
  const near = out.map((_, k) => out.slice(Math.max(0, k - 3), k + 4).some(([mark]) => mark !== " "));
  return out.flatMap((line, k) => near[k] ? [line] : near[k - 1] || k === 0 ? [[" ", "…"]] : []);
}

function showDiff(revision, changes) {
  const body = changes.length ? changes.map((change) => `<details class="diff-file" ${changes.length < 4 ? "open" : ""}><summary><span class="badge ${change.change === "removed" ? "revoked" : change.change === "added" ? "active" : "neutral"}">${changeNames[change.change]}</span> <span class="mono small">${escapeHTML(change.path)}</span></summary>
    ${"after" in change ? `<pre class="diff">${lineDiff(change.before, change.after).map(([mark, line]) => `<span class="${mark === "+" ? "add" : mark === "-" ? "del" : ""}">${mark} ${escapeHTML(line)}</span>`).join("\n")}</pre>` : '<p class="small muted">Двоичный файл.</p>'}</details>`).join("") : '<p class="muted">Файлы не менялись.</p>';
  openDialog(`Изменения в версии ${revision}`, "По сравнению с предыдущей версией черновика.", `<div class="diff-list">${body}</div><div class="dialog-actions"><button class="button" data-action="close">Закрыть</button></div>`);
}

function showPublish(channel) {
  const check = data.draft?.check;
  openDialog(channel === "test" ? "Опубликовать для тестовых ключей" : "Опубликовать для всех", channel === "test"
    ? "Версию предложат только тестовым ключам. Остальные остаются на своей."
    : "Версию предложат всем: агент покажет, что изменилось, и спросит «Обновить?». Каждый остаётся на своей версии, пока не согласится. Откатить можно в разделе «Версии».", `<form id="publish-form" data-channel="${channel}">
    ${check && !check.ok ? '<p class="warning-note">В проверке есть ошибки: сервер не опубликует черновик, пока их не исправят.</p>' : ""}
    <div class="field"><label for="publish-note">Что изменилось</label><textarea id="publish-note" name="note" maxlength="1000" required placeholder="Например: новый этап про переговоры о зарплате, поправлены шаблоны писем"></textarea><small>Обязательно. Агент покажет это людям, когда предложит обновиться.</small></div>
    <label class="check"><input type="checkbox" name="important"> Важное: поставить без вопроса</label><small class="muted">Для исправлений, без которых работать нельзя. Агент сам перейдёт на эту версию и объяснит человеку, что изменилось.</small>
    ${formActions("Опубликовать")}</form>`);
}

function showStageAdd() {
  const steps = data.route.steps;
  if (!steps.length) { notice("Сначала добавьте шаг."); return; }
  openDialog("Новый этап", "Этап встанет в конец маршрута; перенесите его стрелками. Сервер создаст для него SKILL.md и процедуру по шаблону.", `<form id="stage-form">
    <div class="field"><label for="stage-title">Название</label><input id="stage-title" name="title" maxlength="120" required autofocus></div>
    <div class="field"><label for="stage-id">Папка (латиница, цифры, дефис)</label><input id="stage-id" name="code" maxlength="63" required pattern="[a-z0-9][a-z0-9\\-]*" placeholder="salary-talk" spellcheck="false"><small>Агент и hrs узнают этап по ней; после сохранения не меняется.</small></div>
    <div class="field"><label for="stage-step">Шаг</label><select id="stage-step" name="step">${steps.map((step) => `<option value="${escapeHTML(step.id)}">${escapeHTML(step.title)}</option>`).join("")}</select></div>
    ${formActions("Добавить")}</form>`);
}

function showStepAdd() {
  openDialog("Новый шаг", "Шаг — крупная группа этапов, которую видит пользователь.", `<form id="step-form">
    <div class="field"><label for="step-title">Название</label><input id="step-title" name="title" maxlength="80" required autofocus></div>
    <div class="field"><label for="step-id">Код (латиница, цифры, дефис)</label><input id="step-id" name="code" maxlength="63" required pattern="[a-z0-9][a-z0-9\\-]*" placeholder="interviews" spellcheck="false"></div>
    ${formActions("Добавить")}</form>`);
}

function showFileNew(dir = "") {
  openDialog("Новый файл", dir ? "Файл появится в папке этапа. Допишите имя файла." : "Путь внутри методики, например папка этапа и имя файла.", `<form id="file-new-form">
    <div class="field"><label for="new-path">Путь</label><input id="new-path" name="path" maxlength="300" required value="${dir ? escapeHTML(`${dir}/references/`) : ""}" placeholder="skills/salary-talk/references/примеры.md" spellcheck="false" autofocus></div>
    ${formActions("Открыть редактор")}</form>`);
}

function showRestore(revision) {
  openDialog(`Вернуть версию ${revision}`, "Черновик станет таким, как в этой версии. Это новая версия: текущую тоже можно будет вернуть.", `<form id="restore-form" data-rev="${revision}">${formActions("Вернуть", true)}</form>`);
}

function showFileDelete(file) {
  openDialog("Удалить файл", `${escapeHTML(file.path)} пропадёт из черновика. Вернуть можно из истории.`, `<form id="file-delete-form">${formActions("Удалить", true)}</form>`);
}

const ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

function addStage(fields) {
  const id = String(fields.get("code") || "").trim();
  const title = String(fields.get("title") || "").trim();
  if (!ID_RE.test(id)) throw new Error("Папка: латинские строчные буквы, цифры и дефис.");
  if (data.route.stages.some((stage) => stage.id === id)) throw new Error(`Этап «${id}» уже есть.`);
  if (!title) throw new Error("Укажите название этапа.");
  data.route.stages.push({ id, title, step: fields.get("step"), tools: [] });
  data.route.dirty = true;
}

function addStep(fields) {
  const id = String(fields.get("code") || "").trim();
  const title = String(fields.get("title") || "").trim();
  if (!ID_RE.test(id)) throw new Error("Код: латинские строчные буквы, цифры и дефис.");
  if (data.route.steps.some((step) => step.id === id)) throw new Error(`Шаг «${id}» уже есть.`);
  if (!title) throw new Error("Укажите название шага.");
  data.route.steps.push({ id, title });
  data.route.dirty = true;
}

function routeBody() {
  const route = data.route;
  return {
    steps: route.steps.map((step) => ({ id: step.id, title: step.title })),
    stages: route.stages.map((stage) => ({ ...stage })),
    ...(route.version.trim() ? { version: route.version.trim() } : {}),
  };
}

function editRoute(target) {
  const field = target.dataset.field;
  if (field === "version") data.route.version = target.value;
  else if (field === "step-title") data.route.steps[Number(target.closest("[data-step]").dataset.step)].title = target.value;
  else {
    const stage = data.route.stages[Number(target.closest("[data-stage]").dataset.stage)];
    if (field === "on") { if (target.checked) delete stage.status; else stage.status = "off"; }
    else if (field === "progress" && !target.value) delete stage.progress;
    else stage[field] = target.value;
  }
  data.route.dirty = true;
}

function moveStage(index, by) {
  const stages = data.route.stages;
  const to = index + by;
  if (to < 0 || to >= stages.length) return;
  [stages[index], stages[to]] = [stages[to], stages[index]];
  data.route.dirty = true;
  updateRoute();
}

async function download(path, filename) {
  const response = await fetch(apiUrl(path), { headers: { Authorization: `Bearer ${readToken()}` }, cache: "no-store" });
  if (!response.ok) throw new Error(`Сервер ключей ответил ошибкой ${response.status}.`);
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const PAGES = ["tariffs", "bundles", "methodology", "genesis"];

async function render() {
  const seq = ++renderSeq;
  if (!readToken()) { renderLogin(); return; }
  if (!data.role) {
    try {
      data.role = (await api("/whoami")).role;
    } catch (failure) {
      if (seq !== renderSeq) return;
      if (failure instanceof AuthError) logout(failure.message);
      else renderLogin(failure.message);
      return;
    }
    if (seq !== renderSeq) return;
  }
  const route = window.location.hash.slice(1);
  let page = PAGES.includes(route) ? route : route.startsWith("edit/") ? "edit" : route.startsWith("key-") ? "key" : "keys";
  if (data.role !== "admin" && ["keys", "key", "tariffs"].includes(page)) page = "methodology";
  setNav(page);
  mainEl().innerHTML = '<p class="muted loading" role="status">Загрузка…</p>';
  let draw;
  try {
    if (page === "keys") {
      const query = filters.status === "all" ? "" : `?state=${encodeURIComponent(filters.status)}`;
      const [stats, keys, tariffs] = await Promise.all([api("/stats"), api(`/keys${query}`), api("/tariffs")]);
      draw = () => { Object.assign(data, { stats, keys, tariffs }); renderKeys(); };
    } else if (page === "key") {
      const id = encodeURIComponent(route.slice(4));
      const [key, events, tariffs] = await Promise.all([api(`/keys/${id}`), api(`/keys/${id}/events?limit=100`), api("/tariffs")]);
      draw = () => { Object.assign(data, { current: key, tariffs }); renderDetail(key, events); };
    } else if (page === "tariffs") {
      const tariffs = await api("/tariffs");
      draw = () => { data.tariffs = tariffs; renderTariffs(); };
    } else if (page === "methodology") {
      const [draft, revisions] = await Promise.all([api("/methodology"), api("/methodology/revisions?limit=50")]);
      draw = () => renderMethodology(draft, revisions);
    } else if (page === "edit" || page === "genesis") {
      const path = page === "genesis" ? GENESIS : decodeURIComponent(route.slice(5));
      const pending = data.file?.isNew && data.file.path === path ? data.file : null;
      const file = pending || await api(`/methodology/file?path=${encodeURIComponent(path)}`).catch((failure) => {
        if (failure.status === 404 && page === "genesis") return null;
        throw failure;
      });
      draw = () => file ? renderFile(file, { genesis: page === "genesis" }) : renderNoGenesis();
    } else {
      const bundles = await api("/bundles");
      draw = () => renderBundles(bundles);
    }
  } catch (failure) {
    if (seq !== renderSeq) return;
    if (failure instanceof AuthError) logout(failure.message);
    else renderFailure(failure.message);
    return;
  }
  if (seq !== renderSeq) return;
  if (page !== "key") data.current = null;
  if (page !== "edit" && page !== "genesis") data.file = null;
  draw();
  if (data.stats && data.role === "admin") document.getElementById("nav-count").textContent = data.stats.keys;
}

// --- окна ---

function openDialog(title, description, body) {
  dialog.innerHTML = `<div class="dialog-heading"><h2 id="dialog-title">${title}</h2><button class="button ghost close-button" data-action="close" aria-label="Закрыть окно">${icon("close")}</button></div><p class="dialog-description">${description}</p>${body}`;
  if (!dialog.open) dialog.showModal();
}

function termFields(selected = 3, minDate = new Date()) {
  const choices = [...new Set([1, 3, 6, 12, Number(selected)])].sort((a, b) => a - b);
  return `<div class="field"><label for="term">Срок</label><select id="term" name="term" autofocus>${choices.map((n) => `<option value="${n}" ${n === Number(selected) ? "selected" : ""}>${months(n)}</option>`).join("")}<option value="date">До даты</option></select></div>
    <div class="field" id="until-field" hidden><label for="until">Дата окончания, включительно</label><input id="until" name="until" type="date" min="${inputDate(minDate)}" disabled></div>`;
}

function formActions(label, focusCancel = false) {
  return `<p class="form-error" id="form-error" role="alert" hidden></p><div class="dialog-actions"><button type="button" class="button" data-action="close"${focusCancel ? " autofocus" : ""}>Отмена</button><button type="submit" class="button primary">${label}</button></div>`;
}

function showIssue() {
  const active = data.tariffs.filter((tariff) => tariff.active);
  if (!active.length) { notice("Нет тарифов, доступных для выдачи."); return; }
  openDialog("Выдать ключ", "Укажите параметры доступа по оплаченному заказу.", `<form id="issue-form">
    <div class="field"><label for="tariff">Тариф</label><select id="tariff" name="tariff" autofocus>${active.map((tariff) => `<option value="${escapeHTML(tariff.id)}">${escapeHTML(tariff.title)} · ${priceOf(tariff)}</option>`).join("")}</select></div>
    ${termFields(active[0].months)}
    <div class="field"><label for="device-limit">Лимит устройств</label><input id="device-limit" name="deviceLimit" type="number" min="1" max="10" step="1" value="${active[0].device_limit}" required></div>
    <div class="field"><label for="order">Номер заказа платёжного сервиса</label><input id="order" name="order" placeholder="Например, ORD-2026-1060" maxlength="64" required autocomplete="off"><small>Только номер оплаченного заказа, без имени и контактов.</small></div>
    ${formActions("Выдать ключ")}</form>`);
}

function showExtend(key) {
  const base = new Date(Math.max(Date.now(), new Date(key.expires_at).getTime()));
  openDialog("Продлить ключ", `${escapeHTML(keyLabel(key))} · текущий срок до ${date(key.expires_at)}.`, `<form id="extend-form">
    ${termFields(3, base)}<p class="small muted">Месяцы добавляются к текущему сроку; для истёкшего ключа — к сегодняшней дате. Продление не снимает блокировку.</p>${formActions("Продлить")}</form>`);
}

function showRevoke(key) {
  openDialog("Заблокировать ключ", `${escapeHTML(keyLabel(key))} · доступ к методике прекратится сразу.`, `<form id="revoke-form"><div class="field"><label for="reason">Причина блокировки</label><select id="reason" name="reason" autofocus>${Object.entries(reasonNames).map(([id, title]) => `<option value="${id}">${title}</option>`).join("")}</select><small>Причина останется в журнале событий.</small></div>${formActions("Заблокировать")}</form>`);
}

function showDetach(key, install) {
  openDialog("Отвязать устройство", "Место освободится. Для доступа с этого устройства потребуется повторный вход по ключу.", `<form id="detach-form" data-device="${escapeHTML(install)}"><p class="mono">${escapeHTML(install)}</p>${formActions("Отвязать", true)}</form>`);
}

function showResetDevices(key) {
  openDialog("Сбросить все устройства", `${escapeHTML(keyLabel(key))} · отвязать ${key.devices.length} из ${key.device_limit}. Каждому устройству потребуется повторный вход.`, `<form id="reset-form">${formActions("Сбросить", true)}</form>`);
}

function showIssued(key) {
  openDialog("Ключ выдан", "Скопируйте и передайте ключ по оплаченному заказу.", `<div class="key-success">${icon("check")}</div>
    <div class="field"><label for="full-key">Полный ключ доступа</label><textarea id="full-key" class="key-output" readonly spellcheck="false" autocomplete="off"></textarea><button class="button" data-action="copy" autofocus>${icon("copy")}Скопировать</button><p class="small muted" id="copy-result" role="status"></p></div>
    <p class="warning-note">Ключ показан только один раз. После закрытия окна он больше не появится: сервер хранит только хэш. Убедитесь, что сохранили ключ.</p>
    <p class="small muted">${escapeHTML(tariffTitle(key.tariff_id))} · до ${date(key.expires_at)} · ${escapeHTML(key.order_ref || "")}</p>
    <div class="dialog-actions"><button class="button primary" data-action="issued-done" data-id="${escapeHTML(key.id)}">Сохранено, открыть карточку</button></div>`);
  document.getElementById("full-key").value = fullKey;
}

// --- действия ---

// Окна раздела «Методика». false — форма не отсюда.
async function submitMethodology(form) {
  const fields = new FormData(form);
  if (form.id === "stage-form" || form.id === "step-form") {
    if (form.id === "stage-form") addStage(fields); else addStep(fields);
    dialog.close();
    updateRoute();
    notice("Добавлено. Сохраните маршрут, чтобы изменение попало в черновик.");
    return true;
  }
  if (form.id === "file-new-form") {
    const path = String(fields.get("path") || "").trim().replace(/^\/+/, "");
    if (!path || path.split("/").some((part) => !part || part.startsWith("."))) throw new Error("Путь без «..», скрытых папок и пустых частей.");
    if (data.draft?.files.some((file) => file.path === path)) throw new Error("Такой файл уже есть: откройте его из списка.");
    data.file = { path, text: "", isNew: true };
    dialog.close();
    window.location.hash = `edit/${encodeURIComponent(path)}`;
    return true;
  }
  let message;
  if (form.id === "publish-form") {
    const note = String(fields.get("note") || "").trim();
    const published = await api("/methodology/publish", { method: "POST", json: { channel: form.dataset.channel, note, important: fields.get("important") === "on" } });
    message = `Опубликована версия v${published.version} для: ${form.dataset.channel === "test" ? "тестовых ключей" : "всех пользователей"}.`;
  } else if (form.id === "restore-form") {
    const head = await api(`/methodology/revisions/${encodeURIComponent(form.dataset.rev)}/restore`, { method: "POST" });
    message = `Версия ${form.dataset.rev} возвращена. Черновик: версия ${head.id}.`;
  } else if (form.id === "file-delete-form") {
    const file = data.file;
    await api(`/methodology/file?path=${encodeURIComponent(file.path)}&base_sha=${encodeURIComponent(file.sha)}`, { method: "DELETE" });
    message = `Файл ${file.path} удалён из черновика.`;
    dialog.close();
    notice(message);
    window.location.hash = "methodology";
    return true;
  } else return false;
  dialog.close();
  notice(message);
  await render();
  return true;
}

async function issueKey(form) {
  const fields = new FormData(form);
  const tariff = data.tariffs.find((item) => item.id === fields.get("tariff") && item.active);
  const limit = Number(fields.get("deviceLimit"));
  const order = String(fields.get("order") || "").trim();
  const body = { tariff_id: tariff?.id, order_ref: order, device_limit: limit };
  if (fields.get("term") === "date") {
    const expires = endOfDay(fields.get("until"));
    if (!Number.isFinite(expires.getTime()) || expires <= new Date()) throw new Error("Дата окончания должна быть в будущем.");
    body.expires_at = expires.toISOString();
  } else body.months = Number(fields.get("term"));
  if (!tariff || !Number.isSafeInteger(limit) || limit < 1 || limit > 10 || !order || order.length > 64) throw new Error("Проверьте тариф, срок, лимит устройств и номер заказа.");
  const issued = await api("/keys", { method: "POST", json: body });
  fullKey = issued.key;
  delete issued.key;
  showIssued(issued);
  render();
}

async function extendKey(key, fields) {
  const path = `/keys/${encodeURIComponent(key.id)}/extend`;
  if (fields.get("term") !== "date") return api(path, { method: "POST", json: { months: Number(fields.get("term")) } });
  const until = endOfDay(fields.get("until"));
  if (!Number.isFinite(until.getTime()) || until.getTime() <= Math.max(Date.now(), new Date(key.expires_at).getTime())) throw new Error("Новая дата должна быть позже текущего срока и сегодняшнего дня.");
  return api(path, { method: "POST", json: { until: until.toISOString() } });
}

async function copyKey() {
  if (!fullKey) return;
  let copied = false;
  try {
    await navigator.clipboard.writeText(fullKey);
    copied = true;
  } catch {
    const output = document.getElementById("full-key");
    if (!output) return;
    output.focus();
    output.select();
    try { copied = document.execCommand("copy"); } catch { /* Ручное копирование остаётся доступным. */ }
  }
  const result = document.getElementById("copy-result");
  if (result) result.textContent = copied ? "Ключ скопирован." : "Ключ выделен. Скопируйте его сочетанием ⌘C или Ctrl+C.";
}

function showError(element, failure) {
  if (failure instanceof AuthError) { logout(failure.message); return; }
  element.textContent = failure.message;
  element.hidden = false;
}

async function submitMainForm(form) {
  const submit = form.querySelector('[type="submit"]');
  const error = form.querySelector(".form-error");
  if (submit.disabled) return;
  submit.disabled = true;
  error.hidden = true;
  try {
    if (form.id === "login-form") {
      writeToken(String(new FormData(form).get("token") || "").trim());
      data.role = (await api("/whoami")).role;
    } else if (form.classList.contains("tariff-form")) {
      const tariff = data.tariffs.find((item) => item.id === form.dataset.id);
      const changes = changedTariffFields(tariff, new FormData(form));
      if (!Object.keys(changes).length) { notice("Изменений нет."); submit.disabled = false; return; }
      await api(`/tariffs/${encodeURIComponent(tariff.id)}`, { method: "PATCH", json: changes });
      notice(`Тариф «${changes.title || tariff.title}» сохранён.`);
    } else if (form.id === "bundle-form") {
      const published = await api("/bundles", { method: "POST", form: new FormData(form) });
      notice(`Опубликована версия v${published.version}.`);
    } else if (form.id === "draft-upload-form") {
      const fields = new FormData(form);
      const path = String(fields.get("path") || "").trim();
      const head = await api(`/methodology/upload${path ? `?path=${encodeURIComponent(path)}` : ""}`, { method: "POST", form: fields });
      notice(head.unchanged ? "Изменений нет: такие файлы уже в черновике." : `Загружено. Черновик: версия ${head.id}.`);
    } else if (form.id === "file-form") {
      const file = data.file;
      const fields = new FormData(form);
      const message = String(fields.get("message") || "").trim();
      const head = await api("/methodology/file", { method: "PUT", json: { path: file.path, text: String(fields.get("text") ?? ""), base_sha: file.isNew ? null : file.sha, ...(message ? { message } : {}) } });
      data.file = null;
      notice(head.unchanged ? "Изменений нет." : `Сохранено. Черновик: версия ${head.id}.`);
    }
    await render();
  } catch (failure) {
    if (form.id === "login-form") {
      writeToken("");
      data.role = null;
      error.textContent = failure.message;
      error.hidden = false;
    } else showError(error, failure);
    submit.disabled = false;
  }
}

root.addEventListener("input", (event) => {
  if (event.target.id === "search") { filters.query = event.target.value; updateList(); }
  else if (event.target.dataset?.field && data.route) {
    editRoute(event.target);
    if (event.target.dataset.field === "title") { const title = event.target.closest(".stage")?.querySelector(".stage-title"); if (title) title.textContent = event.target.value; }
    // Кнопки включаются сразу, без перерисовки: иначе потерялся бы фокус в поле.
    ["route-save", "route-reset"].forEach((action) => { const button = document.querySelector?.(`[data-action="${action}"]`); if (button) button.disabled = false; });
  }
});
root.addEventListener("change", (event) => {
  if (event.target.dataset?.field && data.route) {
    editRoute(event.target);
    if (event.target.tagName === "SELECT" || event.target.type === "checkbox") updateRoute();
  }
  else if (event.target.id === "status-filter") { filters.status = event.target.value; render(); }
  else if (event.target.id === "tariff-filter") { filters.tariff = event.target.value; updateList(); }
});
root.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const key = data.current;
  const action = button.dataset.action;
  if (action === "logout") logout("Вы вышли. Токен удалён из этой вкладки.", false);
  else if (action === "issue") showIssue();
  else if (action === "reload") render();
  else if (action === "reset") { Object.assign(filters, { query: "", status: "all", tariff: "all" }); render(); }
  else if (action === "publish") showPublish(button.dataset.channel);
  else if (action === "stage-add") showStageAdd();
  else if (action === "step-add") showStepAdd();
  else if (action === "file-new") showFileNew(button.dataset.dir);
  else if (action === "restore-rev") showRestore(Number(button.dataset.rev));
  else if (action === "file-delete" && data.file) showFileDelete(data.file);
  else if (action === "stage-up" || action === "stage-down") moveStage(Number(button.closest("[data-stage]").dataset.stage), action === "stage-up" ? -1 : 1);
  else if (action === "stage-remove") { data.route.stages.splice(Number(button.closest("[data-stage]").dataset.stage), 1); data.route.dirty = true; updateRoute(); }
  else if (action === "step-remove") { data.route.steps.splice(Number(button.closest("[data-step]").dataset.step), 1); data.route.dirty = true; updateRoute(); }
  else if (action === "route-reset") { data.route = routeCopy(data.draft); updateRoute(); }
  else if (["route-save", "diff", "import-current", "export", "set-current", "toggle-channel"].includes(action)) {
    button.disabled = true;
    try {
      if (action === "route-save") {
        const head = await api("/methodology/route", { method: "PUT", json: routeBody() });
        notice(head.unchanged ? "Изменений нет." : `Маршрут сохранён. Черновик: версия ${head.id}.`);
      } else if (action === "diff") {
        showDiff(button.dataset.rev, await api(`/methodology/revisions/${encodeURIComponent(button.dataset.rev)}/diff`));
        button.disabled = false;
        return;
      } else if (action === "import-current") {
        const head = await api("/methodology/import-current", { method: "POST" });
        notice(head.unchanged ? "Черновик уже совпадает с опубликованной версией." : `${head.message}. Черновик: версия ${head.id}.`);
      } else if (action === "export") {
        await download("/methodology/export", "methodology-draft.zip");
        button.disabled = false;
        return;
      } else if (action === "set-current") {
        await api(`/bundles/${encodeURIComponent(button.dataset.version)}/current`, { method: "POST", json: { channel: button.dataset.channel } });
        notice(`v${button.dataset.version} — текущая для: ${button.dataset.channel === "test" ? "тестовых ключей" : "всех пользователей"}.`);
      } else if (action === "toggle-channel" && key) {
        const channel = key.channel === "test" ? "stable" : "test";
        await api(`/keys/${encodeURIComponent(key.id)}/channel`, { method: "POST", json: { channel } });
        notice(channel === "test" ? "Ключ получает тестовые версии методики." : "Ключ получает обычные версии методики.");
      }
      await render();
    } catch (failure) {
      if (failure instanceof AuthError) logout(failure.message);
      else { notice(failure.message); button.disabled = false; }
    }
  }
  else if (key && action === "extend") showExtend(key);
  else if (key && action === "revoke") showRevoke(key);
  else if (key && action === "detach") showDetach(key, button.dataset.device);
  else if (key && action === "reset-devices") showResetDevices(key);
  else if (key && action === "restore") {
    button.disabled = true;
    try {
      const restored = await api(`/keys/${encodeURIComponent(key.id)}/restore`, { method: "POST" });
      notice(restored.state === "expired" ? "Блокировка снята. Срок ключа истёк — нужно продление." : "Ключ разблокирован.");
      await render();
    } catch (failure) {
      if (failure instanceof AuthError) logout(failure.message);
      else { notice(failure.message); button.disabled = false; }
    }
  }
});
root.addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.target.reportValidity()) submitMainForm(event.target);
});

dialog.addEventListener("change", (event) => {
  if (event.target.id === "term") {
    const custom = event.target.value === "date";
    document.getElementById("until-field").hidden = !custom;
    const until = document.getElementById("until");
    until.disabled = !custom;
    until.required = custom;
  } else if (event.target.id === "tariff") {
    const tariff = data.tariffs.find((item) => item.id === event.target.value);
    const term = document.getElementById("term");
    if (![...term.options].some((option) => option.value === String(tariff.months))) term.add(new Option(months(tariff.months), tariff.months), 0);
    term.value = tariff.months;
    document.getElementById("device-limit").value = tariff.device_limit;
    term.dispatchEvent(new Event("change", { bubbles: true }));
  }
});

dialog.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  if (button.dataset.action === "close") dialog.close();
  else if (button.dataset.action === "copy") copyKey();
  else if (button.dataset.action === "issued-done") {
    dialog.close();
    window.location.hash = `key-${button.dataset.id}`;
  }
});

dialog.addEventListener("close", () => {
  fullKey = null;
  dialog.replaceChildren();
});

dialog.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  if (!form.reportValidity()) return;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  const error = document.getElementById("form-error");
  error.hidden = true;
  try {
    if (form.id === "issue-form") { await issueKey(form); return; }
    if (await submitMethodology(form)) return;
    const key = data.current;
    if (!key) throw new Error("Ключ не найден.");
    const fields = new FormData(form);
    const path = `/keys/${encodeURIComponent(key.id)}`;
    let message;
    if (form.id === "extend-form") {
      const extended = await extendKey(key, fields);
      message = `Ключ продлён до ${date(extended.expires_at)}.`;
    } else if (form.id === "revoke-form") {
      await api(`${path}/revoke`, { method: "POST", json: { reason: fields.get("reason") } });
      message = "Ключ заблокирован.";
    } else if (form.id === "detach-form") {
      await api(`${path}/devices/${encodeURIComponent(form.dataset.device)}`, { method: "DELETE" });
      message = "Устройство отвязано. Место освободилось.";
    } else if (form.id === "reset-form") {
      await api(`${path}/reset-devices`, { method: "POST" });
      message = "Все устройства отвязаны.";
    }
    dialog.close();
    notice(message);
    await render();
  } catch (failure) {
    showError(error, failure);
    submit.disabled = false;
  }
});

window.addEventListener("hashchange", () => { render(); mainEl()?.focus(); });
render();
