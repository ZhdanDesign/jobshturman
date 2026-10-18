"use strict";

// Админка открыта по /hrshturman/_/admin/, сервер ключей — /hrshturman/keys/ (см. key-server/API.md).
const API_BASE = "../../keys/v1/admin";
const TOKEN_KEY = "hrs-keys-admin-token";
const root = document.getElementById("app");
const dialog = document.getElementById("dialog");
const filters = { query: "", status: "all", tariff: "all" };
const data = { keys: [], tariffs: [], stats: null, current: null };
const statusNames = { active: "Активен", expiring: "Истекает", expired: "Истёк", revoked: "Заблокирован" };
const reasonNames = { refund: "Возврат оплаты", violation: "Нарушение условий", reissue: "Перевыпуск ключа", other: "Другое" };
const deniedNames = { revoked: "ключ заблокирован", expired: "срок истёк", device_limit: "превышен лимит устройств" };
let fullKey = null;
let noticeTimer;
let renderSeq = 0;
let shellShown = false;

// Оболочка админки появляется в DOM только после входа; до входа на странице одна карточка.
const SHELL = `
  <a class="skip-link" href="#main">Перейти к содержимому</a>
  <aside class="sidebar">
    <a class="brand" href="#keys" aria-label="HR Shturman — список ключей">
      <span class="brand-mark" aria-hidden="true">H</span>
      <span><strong>HR Shturman</strong><small>Сервер ключей</small></span>
    </a>
    <div class="nav-label">Управление</div>
    <nav aria-label="Главная навигация">
      <a href="#keys" data-nav="keys">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="15" r="5"/><path d="m11.5 11.5 8-8L22 6l-2 2-2-2-2 2 2 2-3 3"/></svg>
        Ключи <span class="nav-count" id="nav-count"></span>
      </a>
      <a href="#tariffs" data-nav="tariffs">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M7 15h4"/></svg>
        Тарифы
      </a>
      <a href="#bundles" data-nav="bundles">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z"/><path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5"/></svg>
        Методика
      </a>
    </nav>
    <div class="sidebar-foot"><button class="button" data-action="logout" type="button">Выйти</button><p>Токен хранится только в этой вкладке.</p></div>
  </aside>
  <div class="workspace">
    <header class="topbar"><span>Администрирование <span class="divider">/</span> <strong id="breadcrumb">Ключи</strong></span><span class="topbar-note">Ручная выдача после оплаты</span></header>
    <main id="main" tabindex="-1"></main>
    <footer class="page-footer">HR Shturman <span>Только ключи, устройства и номера заказов</span></footer>
  </div>`;

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
  if (!response.ok) throw new Error(errorText(result, response.status));
  return result;
}

function logout(message = "", isError = true) {
  writeToken("");
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  Object.assign(data, { keys: [], stats: null, current: null });
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
    root.innerHTML = SHELL;
    shellShown = true;
    document.title = "Сервер ключей · HR Shturman";
  }
  document.getElementById("breadcrumb").textContent = { tariffs: "Тарифы", bundles: "Методика", key: "Карточка ключа", keys: "Ключи" }[page];
  document.querySelectorAll("[data-nav]").forEach((link) => {
    if (link.dataset.nav === (page === "key" ? "keys" : page)) link.setAttribute("aria-current", "page");
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
  ];
  if (key.revoked_at) facts.push(["Заблокирован", `${dateTime(key.revoked_at)} · ${reason}`]);
  const revoked = key.state === "revoked";
  mainEl().innerHTML = `
    <a class="back-link" href="#keys">${icon("back")}Все ключи</a>
    <div class="page-heading detail-heading"><div><h1 class="mono">${escapeHTML(keyLabel(key))}</h1>${badge(key)}</div>
      <div class="actions"><button class="button" data-action="extend">Продлить</button><button class="button ${revoked ? "" : "danger"}" data-action="${revoked ? "restore" : "revoke"}">${revoked ? "Разблокировать" : "Заблокировать"}</button></div></div>
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
  mainEl().innerHTML = `<div class="page-heading"><div><h1>Методика</h1><p>Опубликованные версии пакета. Пользователи получают последнюю.</p></div></div>
    <section class="panel upload"><h2>Новая версия</h2>
      <form id="bundle-form"><div class="field"><label for="bundle-file">Пакет zip</label><input id="bundle-file" name="file" type="file" accept=".zip,application/zip" required><small>Собирается командой <code>hrs-keys bundle build</code>; версия берётся из bundle.json внутри. Сервер подписывает пакет сам.</small></div>
        <p class="form-error" role="alert" hidden></p>
        <div class="dialog-actions"><button type="submit" class="button primary">${icon("upload")}Опубликовать</button></div></form></section>
    <div class="table-wrap" role="region" aria-label="Версии методики, прокручивается по горизонтали" tabindex="0">
      <table class="bundles"><thead><tr><th scope="col">Версия</th><th scope="col">Опубликована</th><th scope="col">Размер</th><th scope="col">SHA-256</th></tr></thead><tbody>${bundles.map((bundle, i) => `<tr>
        <td><span class="mono">v${escapeHTML(bundle.version)}</span>${i === 0 ? ' <span class="badge active">Текущая</span>' : ""}</td><td>${dateTime(bundle.published_at)}</td>
        <td>${(bundle.size / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} КБ</td><td class="mono muted">${escapeHTML(bundle.sha256.slice(0, 16))}…</td></tr>`).join("")}</tbody></table>
      ${bundles.length ? "" : '<div class="empty"><h3>Версий пока нет</h3><p class="muted">Опубликуйте первый пакет методики.</p></div>'}
    </div>`;
}

async function render() {
  const seq = ++renderSeq;
  if (!readToken()) { renderLogin(); return; }
  const route = window.location.hash.slice(1);
  const page = route === "tariffs" || route === "bundles" ? route : route.startsWith("key-") ? "key" : "keys";
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
  draw();
  if (data.stats) document.getElementById("nav-count").textContent = data.stats.keys;
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
      await api("/stats");
    } else if (form.classList.contains("tariff-form")) {
      const tariff = data.tariffs.find((item) => item.id === form.dataset.id);
      const changes = changedTariffFields(tariff, new FormData(form));
      if (!Object.keys(changes).length) { notice("Изменений нет."); submit.disabled = false; return; }
      await api(`/tariffs/${encodeURIComponent(tariff.id)}`, { method: "PATCH", json: changes });
      notice(`Тариф «${changes.title || tariff.title}» сохранён.`);
    } else if (form.id === "bundle-form") {
      const published = await api("/bundles", { method: "POST", form: new FormData(form) });
      notice(`Опубликована версия v${published.version}.`);
    }
    await render();
  } catch (failure) {
    if (form.id === "login-form") {
      writeToken("");
      error.textContent = failure.message;
      error.hidden = false;
    } else showError(error, failure);
    submit.disabled = false;
  }
}

root.addEventListener("input", (event) => {
  if (event.target.id === "search") { filters.query = event.target.value; updateList(); }
});
root.addEventListener("change", (event) => {
  if (event.target.id === "status-filter") { filters.status = event.target.value; render(); }
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
