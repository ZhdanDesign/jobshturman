const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");

// Логика и обработчики проверяются с заглушками DOM и fetch; отрисовку проверяет браузерный прогон.
const TARIFFS = [
  { id: "self", title: "Самостоятельно", price_rub_from: 15000, price_rub_to: 15000, months: 3, device_limit: 2, active: 1 },
  { id: "guided", title: "С сопровождением", price_rub_from: 30000, price_rub_to: 80000, months: 3, device_limit: 2, active: 1 },
  { id: "experts", title: "С группой экспертов", price_rub_from: 200000, price_rub_to: 200000, months: 3, device_limit: 2, active: 0 },
];
const KEY = {
  id: "a1b2c3d4e5f6", key_hint: "QW7K", tariff_id: "self", issued_at: "2026-10-01T10:00:00Z",
  expires_at: "2027-01-01T10:00:00Z", device_limit: 2, order_ref: "ORD-2026-1042", revoked_at: null, revoke_reason: null,
  state: "active", devices: [{ install: "9f3a2b1c", first_seen: "2026-10-02T10:00:00Z", last_seen: "2026-10-10T10:00:00Z" }],
  last_seen: "2026-10-10T10:00:00Z", downloads: 1, denied: 0,
};
const KEYS = [KEY, { ...KEY, id: "0000aaaa1111", key_hint: "ZX9P", tariff_id: "guided", order_ref: "ORD-2026-2001", state: "revoked", revoke_reason: "refund", devices: [] }];
const STATS = { keys: 2, by_state: { active: 1, revoked: 1 }, devices: 1, latest_bundle: "0.4.0" };

function load({ token = "", routes = {} } = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: "", textContent: "", hidden: false, open: false, listeners: {}, value: "",
      addEventListener(type, callback) { this.listeners[type] = callback; },
      replaceChildren() { this.innerHTML = ""; },
      querySelector() { return this.form; },
      showModal() { this.open = true; },
      close() { this.open = false; this.listeners.close(); },
    });
    return elements.get(id);
  };
  const storage = new Map(token ? [["hrs-keys-admin-token", token]] : []);
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({ url, ...options, body: options.body && typeof options.body === "string" ? JSON.parse(options.body) : options.body });
    const path = url.replace("https://tools.owai.ru/jobshturman/keys/v1/admin", "");
    const route = routes[`${options.method} ${path}`] || routes[path];
    const [status, body] = typeof route === "function" ? route(options) : route || [404, { error: "not_found", message: "Нет такого ключа" }];
    return { status, ok: status < 400, json: async () => body };
  };
  const context = vm.createContext({
    window: {
      location: { href: "https://tools.owai.ru/jobshturman/_/admin/", pathname: "/jobshturman/_/admin/", search: "", hash: "" }, addEventListener() {},
      history: { replaceState(state, title, url) { context.window.location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : ""; } },
      sessionStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k) },
    },
    document: { getElementById: element, querySelectorAll: () => [], addEventListener() {}, title: "Вход" },
    URL, fetch, setTimeout: () => 0, clearTimeout() {},
    FormData: class { constructor(form) { this.values = form.values; } get(name) { return this.values[name] ?? null; } },
  });
  vm.runInContext(readFileSync(join(__dirname, "app.js"), "utf8"), context);
  return { run: (source) => vm.runInContext(source, context), context, element, storage, calls };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const api = { "/whoami": [200, { role: "admin" }], "/stats": [200, STATS], "/keys": [200, KEYS], "/tariffs": [200, TARIFFS] };

async function submitDialog(app, id, values, dataset = {}) {
  const submit = { disabled: false };
  const form = { id, dataset, values, reportValidity: () => true, querySelector: () => submit };
  app.element("dialog").open = true;
  app.element("dialog").form = form;
  await app.element("dialog").listeners.submit({ preventDefault() {}, target: form });
  await settle();
  return { form, submit };
}

test("продление сохраняет конец месяца, учитывает високосный год; дата — конец дня без долей секунды", () => {
  const { run } = load();
  assert.equal(run('inputDate(addMonths(new Date(2026, 0, 31, 10), 1))'), "2026-02-28");
  assert.equal(run('inputDate(addMonths(new Date(2028, 0, 31, 10), 1))'), "2028-02-29");
  assert.equal(run('inputDate(addMonths(new Date(2026, 9, 11, 10), 12))'), "2027-10-11");
  assert.equal(run('endOfDay("2027-01-15").getHours()'), 23);
  assert.equal(run('endOfDay("2027-01-15").getMilliseconds()'), 0);
  assert.equal(run('months(1) + "|" + months(3) + "|" + months(12) + "|" + months(21)'), "1 месяц|3 месяца|12 месяцев|21 месяц");
});

test("API ищется относительно /jobshturman/_/admin/ и получает токен из sessionStorage", async () => {
  const app = load({ token: "t-123", routes: api });
  assert.equal(app.run('apiUrl("/stats")'), "https://tools.owai.ru/jobshturman/keys/v1/admin/stats");
  await settle();
  assert.deepEqual(app.calls.map((call) => call.url.split("/admin")[1]).sort(), ["/keys", "/stats", "/tariffs", "/whoami"]);
  assert.ok(app.calls.every((call) => call.headers.Authorization === "Bearer t-123" && call.method === "GET"));
  assert.match(app.element("key-rows").innerHTML, /HRS-QW7K…/);
  assert.match(app.element("key-rows").innerHTML, /Заблокирован/);
  assert.equal(app.element("nav-count").textContent, 2);
});

test("без токена — экран входа; вход сохраняет токен, неверный токен не сохраняется", async () => {
  const app = load({ routes: { ...api, "/whoami": (options) => options.headers.Authorization === "Bearer good" ? [200, { role: "admin" }] : [403, { error: "forbidden", message: "Неверный токен администратора" }] } });
  const screen = app.element("app").innerHTML;
  assert.match(screen, /id="login-form"/);
  assert.doesNotMatch(screen, /JobShturman|Сервер ключей|data-nav|Ручная выдача|номера заказов/);
  assert.equal(app.context.document.title, "Вход");
  assert.equal(app.calls.length, 0);
  const error = { hidden: true, textContent: "" };
  const form = (token) => ({ id: "login-form", values: { token }, classList: { contains: () => false }, querySelector: (s) => s === ".form-error" ? error : { disabled: false } });
  await app.run("submitMainForm")(form("bad"));
  assert.equal(app.storage.size, 0);
  assert.equal(error.hidden, false);
  assert.match(error.textContent, /Неверный токен администратора\. Войдите заново\./);
  await app.run("submitMainForm")(form("  good "));
  await settle();
  assert.equal(app.storage.get("hrs-keys-admin-token"), "good");
  assert.match(app.element("app").innerHTML, /data-nav="keys"[\s\S]*data-action="logout"/);
  assert.equal(app.context.document.title, "Сервер ключей · JobShturman");
  assert.match(app.element("key-rows").innerHTML, /ORD-2026-1042/);
});

test("ответ 401 на любой странице удаляет токен и возвращает на вход с понятным текстом", async () => {
  const app = load({ token: "old", routes: { ...api, "/stats": [401, { error: "unauthorized", message: "Нужен заголовок Authorization: Bearer ..." }] } });
  await settle();
  assert.equal(app.storage.size, 0);
  assert.match(app.element("app").innerHTML, /id="login-form"/);
  assert.match(app.element("app").innerHTML, /Войдите заново/);
  app.storage.set("hrs-keys-admin-token", "x");
  app.context.window.location.hash = "#keys";
  app.element("app").listeners.click({ target: { closest: () => ({ dataset: { action: "logout" } }) } });
  assert.equal(app.storage.size, 0);
  assert.match(app.element("app").innerHTML, /Вы вышли/);
  assert.doesNotMatch(app.element("app").innerHTML, /data-nav/);
  assert.equal(app.context.window.location.hash, "");
});

test("поиск и фильтр тарифа — на месте, фильтр статуса уходит в API", async () => {
  const app = load({ token: "t", routes: { ...api, "/keys?state=revoked": [200, [KEYS[1]]] } });
  await settle();
  app.run('filters.query = " ord-2026-2001 "');
  assert.equal(app.run("filteredKeys().length"), 1);
  app.run('filters.query = "qw7"; filters.tariff = "guided"');
  assert.equal(app.run("filteredKeys().length"), 0);
  app.run('filters.query = ""; filters.tariff = "all"');
  app.element("app").listeners.change({ target: { id: "status-filter", value: "revoked" } });
  await settle();
  assert.ok(app.calls.some((call) => call.url.endsWith("/keys?state=revoked")));
  assert.equal(app.run("data.keys.length"), 1);
  assert.equal(app.run('escapeHTML("<script>&")'), "&lt;script&gt;&amp;");
});

test("выдача шлёт тариф, срок, лимит и номер заказа; полный ключ виден один раз", async () => {
  const issued = { ...KEY, id: "ffff00001111", key: "HRS-QW7K-AAAA-BBBB-CCCC-DDDD", devices: [] };
  const app = load({ token: "t", routes: { ...api, "POST /keys": () => [201, { ...issued }] } });
  await settle();
  await submitDialog(app, "issue-form", { tariff: "self", term: "6", until: "", deviceLimit: "1", order: " TEST-PAID-001 " });
  const post = app.calls.find((call) => call.method === "POST");
  assert.deepEqual(post.body, { tariff_id: "self", order_ref: "TEST-PAID-001", device_limit: 1, months: 6 });
  assert.equal(post.headers["Content-Type"], "application/json");
  assert.equal(app.run("fullKey"), issued.key);
  assert.match(app.element("dialog").innerHTML, /Ключ выдан/);
  assert.equal(JSON.stringify(app.run("data")).includes(issued.key), false);
  app.element("dialog").listeners.close();
  assert.equal(app.run("fullKey"), null);
  assert.equal(app.element("dialog").innerHTML, "");

  await submitDialog(app, "issue-form", { tariff: "experts", term: "3", until: "", deviceLimit: "1", order: "X" });
  assert.match(app.element("form-error").textContent, /Проверьте тариф/);
  await submitDialog(app, "issue-form", { tariff: "self", term: "date", until: "2099-12-31", deviceLimit: "2", order: "X" });
  const byDate = app.calls.filter((call) => call.method === "POST").at(-1).body;
  assert.equal(byDate.months, undefined);
  assert.equal(new Date(byDate.expires_at).getTime(), new Date("2099-12-31T23:59:59").getTime());
});

test("действия карточки: продление, блокировка, отвязка, сброс; ошибка API показывается из message", async () => {
  const app = load({ token: "t", routes: {
    ...api, [`/keys/${KEY.id}`]: [200, KEY], [`/keys/${KEY.id}/events?limit=100`]: [200, [{ kind: "admin", detail: "issue", at: KEY.issued_at }]],
    [`POST /keys/${KEY.id}/extend`]: [200, { ...KEY, expires_at: "2027-04-01T10:00:00Z" }],
    [`POST /keys/${KEY.id}/revoke`]: [200, { ...KEY, state: "revoked" }],
    [`DELETE /keys/${KEY.id}/devices/9f3a2b1c`]: [409, { error: "ambiguous_device", message: "Префикс подходит к нескольким устройствам: сбросьте все устройства" }],
    [`POST /keys/${KEY.id}/reset-devices`]: [200, { ...KEY, devices: [] }],
  } });
  app.context.window.location.hash = `#key-${KEY.id}`;
  await app.run("render()");
  assert.match(app.element("main").innerHTML, /Журнал событий[\s\S]*Ключ выдан/);
  assert.match(app.element("main").innerHTML, /data-device="9f3a2b1c"/);

  await submitDialog(app, "extend-form", { term: "3" });
  await submitDialog(app, "revoke-form", { reason: "violation" });
  const before = app.calls.length;
  await submitDialog(app, "extend-form", { term: "date", until: "2026-12-01" });
  assert.equal(app.calls.length, before);
  assert.match(app.element("form-error").textContent, /Новая дата должна/);
  const { submit } = await submitDialog(app, "detach-form", {}, { device: "9f3a2b1c" });
  assert.equal(app.element("form-error").textContent, "Префикс подходит к нескольким устройствам: сбросьте все устройства");
  assert.equal(submit.disabled, false);
  await submitDialog(app, "reset-form", {});

  const writes = app.calls.filter((call) => call.method !== "GET").map((call) => [call.method, call.url.split("/admin")[1], call.body]);
  assert.deepEqual(writes, [
    ["POST", `/keys/${KEY.id}/extend`, { months: 3 }],
    ["POST", `/keys/${KEY.id}/revoke`, { reason: "violation" }],
    ["DELETE", `/keys/${KEY.id}/devices/9f3a2b1c`, undefined],
    ["POST", `/keys/${KEY.id}/reset-devices`, undefined],
  ]);
});

test("журнал переводится на русский, тариф сохраняет только изменённые поля", () => {
  const { run } = load();
  const plain = (source) => JSON.parse(JSON.stringify(run(source)));
  const text = (kind, detail) => run(`eventText(${JSON.stringify({ kind, detail })})`);
  assert.equal(text("download", "0.4.0"), "Скачан пакет v0.4.0");
  assert.equal(text("denied", "device_limit"), "Отказ: превышен лимит устройств");
  assert.equal(text("admin", "revoke refund"), "Ключ заблокирован · Возврат оплаты");
  assert.equal(text("admin", "unbind device 9f3a2b1c"), "Устройство отвязано · 9f3a2b1c");
  assert.match(text("admin", "extend to 2027-01-01T10:00:00Z"), /^Ключ продлён до /);
  const fields = (values) => `new FormData({ values: ${JSON.stringify(values)} })`;
  const self = JSON.stringify(TARIFFS[0]);
  assert.deepEqual(plain(`changedTariffFields(${self}, ${fields({ title: "Самостоятельно", price_rub_from: "16000", price_rub_to: "16000", months: "3", device_limit: "2", active: "on" })})`),
    { price_rub_from: 16000, price_rub_to: 16000 });
  assert.deepEqual(plain(`changedTariffFields(${self}, ${fields({ title: "Самостоятельно", price_rub_from: "15000", price_rub_to: "15000", months: "3", device_limit: "2" })})`), { active: false });
  assert.throws(() => run(`changedTariffFields(${self}, ${fields({ title: "A", price_rub_from: "9", price_rub_to: "1", months: "3", device_limit: "2" })})`), /Цена «от»/);
});

// --- методика ---

const DRAFT = {
  head: { id: 3, at: "2026-10-18T10:00:00Z", by: "methodologist", message: "Маршрут: этапы и шаги", files: 5 },
  files: [
    { path: "agent/genesis.md", sha: "g1", size: 900 }, { path: "library_manifest.json", sha: "m1", size: 400 },
    { path: "skills/intake/SKILL.md", sha: "s1", size: 300 }, { path: "skills/resume/SKILL.md", sha: "s2", size: 300 },
    { path: "README.md", sha: "r1", size: 10 },
  ],
  route: null, manifest_version: "0.4.0", current: { stable: "0.4.0.r2", test: "0.4.0.r2" }, genesis: "agent/genesis.md",
  steps: [{ id: "about", title: "О вас" }, { id: "apply", title: "Отклики" }],
  stages: [
    { id: "intake", title: "Анкета", step: "about", dir: "skills/intake", progress: "intake", tools: ["intake_form"], files: ["skills/intake/SKILL.md"], position: 0 },
    { id: "resume", title: "Резюме", step: "about", dir: "skills/resume", tools: [], files: ["skills/resume/SKILL.md"], position: 1 },
  ],
  check: { ok: true, errors: [], findings: [{ code: "unknown_tool", detail: "инструмент «x»", stage: "resume" }] },
};
const REVISIONS = [{ id: 3, at: "2026-10-18T10:00:00Z", by: "methodologist", message: "Маршрут", files: 5 }, { id: 2, at: "2026-10-17T10:00:00Z", by: "admin", message: "Загружен library.zip", files: 5 }];
const methodologyApi = { ...api, "/whoami": [200, { role: "methodologist" }], "/methodology": [200, DRAFT], "/methodology/revisions?limit=50": [200, REVISIONS] };
const writes = (app) => app.calls.filter((call) => call.method !== "GET").map((call) => [call.method, call.url.split("/admin")[1], call.body]);

test("автор методики видит только методику, главный промпт и версии; ключи ему не открываются", async () => {
  const app = load({ token: "author", routes: methodologyApi });
  app.context.window.location.hash = "#keys";
  await app.run("render()");
  const page = app.element("app").innerHTML;
  assert.match(page, /data-nav="methodology"[\s\S]*data-nav="genesis"[\s\S]*data-nav="bundles"/);
  assert.doesNotMatch(page, /data-nav="keys"|data-nav="tariffs"/);
  assert.ok(!app.calls.some((call) => /^\/(keys|stats|tariffs)/.test(call.url.split("/admin")[1])));
  const main = app.element("main").innerHTML;
  assert.match(main, /Черновик: версия 3/);
  assert.match(main, /Анкета[\s\S]*Резюме/);
  assert.match(main, /href="#edit\/agent%2Fgenesis\.md"/);
  assert.match(main, /Замечания: 1/);
});

test("маршрут: перенос, новый этап и шаг, выключение; сохраняется одним запросом в нужном порядке", async () => {
  const app = load({ token: "author", routes: { ...methodologyApi, "PUT /methodology/route": [200, { id: 4 }] } });
  app.context.window.location.hash = "#methodology";
  await app.run("render()");
  app.run("moveStage(1, -1)");
  await submitDialog(app, "step-form", { code: "money", title: "Деньги" });
  await submitDialog(app, "stage-form", { code: "salary-talk", title: "Разговор о деньгах", step: "money" });
  await submitDialog(app, "stage-form", { code: "Bad Id", title: "x", step: "money" });
  assert.match(app.element("form-error").textContent, /латинские строчные/);
  app.run('editRoute({ dataset: { field: "on" }, checked: false, closest: () => ({ dataset: { stage: "1" } }) })');
  app.run('editRoute({ dataset: { field: "version" }, value: "0.5.0" })');
  assert.match(app.element("route-panel").innerHTML, /Разговор о деньгах[\s\S]*Есть несохранённые изменения/);
  assert.deepEqual([0, 1, 2].map((i) => app.run(`stageNumber(data.route.stages, ${i})`)), [0, null, 1]);
  await app.element("app").listeners.click({ target: { closest: () => ({ dataset: { action: "route-save" }, disabled: false }) } });
  await settle();
  const [[method, path, body]] = writes(app);
  assert.deepEqual([method, path], ["PUT", "/methodology/route"]);
  assert.equal(body.version, "0.5.0");
  assert.deepEqual(body.steps.map((step) => step.id), ["about", "apply", "money"]);
  assert.deepEqual(body.stages.map((stage) => [stage.id, stage.step, stage.status]), [["resume", "about", undefined], ["intake", "about", "off"], ["salary-talk", "money", undefined]]);
  assert.ok(body.stages.every((stage) => !("files" in stage) && !("position" in stage)));
});

test("файл сохраняется с версией, которую открыли; чужая правка не перезаписывается", async () => {
  let conflict = false;
  const app = load({ token: "author", routes: { ...methodologyApi,
    "/methodology/file?path=agent%2Fgenesis.md": [200, { path: "agent/genesis.md", sha: "g1", text: "# Правила\n", size: 10 }],
    "PUT /methodology/file": () => conflict ? [409, { error: "conflict", message: "agent/genesis.md изменили, пока он был открыт: обновите страницу" }] : [200, { id: 4 }],
  } });
  app.context.window.location.hash = "#genesis";
  await app.run("render()");
  assert.match(app.element("main").innerHTML, /Главный промпт[\s\S]*Опубликовать тестовым/);
  assert.equal(app.element("file-text").value, "# Правила\n");
  const error = { hidden: true, textContent: "" };
  const form = { id: "file-form", values: { text: "# Правила v2\n", message: "без пауз" }, classList: { contains: () => false }, querySelector: (s) => s === ".form-error" ? error : { disabled: false } };
  await app.run("submitMainForm")(form);
  conflict = true;
  await app.run("render()");
  await app.run("submitMainForm")(form);
  assert.deepEqual(writes(app).map(([, , body]) => body), [
    { path: "agent/genesis.md", text: "# Правила v2\n", base_sha: "g1", message: "без пауз" },
    { path: "agent/genesis.md", text: "# Правила v2\n", base_sha: "g1", message: "без пауз" },
  ]);
  assert.match(error.textContent, /изменили, пока он был открыт/);
  assert.equal(error.hidden, false);
});

test("публикация тестовым и откат версии для всех", async () => {
  const BUNDLES = [{ version: "0.5.0.r4", published_at: "2026-10-18T11:00:00Z", size: 2048, sha256: "a".repeat(64), channels: ["test"], min_hrs: "0.3.0", revision: 4, note: "новый этап", important: true, keys: 1 },
    { version: "0.4.0.r2", published_at: "2026-10-17T11:00:00Z", size: 2048, sha256: "b".repeat(64), channels: ["stable"], min_hrs: null, revision: 2, note: null, important: false, keys: 7 }];
  const app = load({ token: "author", routes: { ...methodologyApi, "/bundles": [200, BUNDLES],
    "POST /methodology/publish": [201, { version: "0.5.0.r4", channels: ["test"] }], "POST /bundles/0.4.0.r2/current": [200, {}] } });
  app.context.window.location.hash = "#methodology";
  await app.run("render()");
  await submitDialog(app, "publish-form", { note: "новый этап", important: "on" }, { channel: "test" });
  app.context.window.location.hash = "#bundles";
  await app.run("render()");
  const table = app.element("main").innerHTML;
  assert.match(table, /v0\.5\.0\.r4[\s\S]*Тестовые ключи[\s\S]*Нужен hrs 0\.3\.0/);
  assert.match(table, /Важное<\/span> новый этап<\/td><td>1<\/td>/);
  assert.match(table, /<td>7<\/td>/);
  assert.doesNotMatch(table, /id="bundle-form"/);
  await app.element("app").listeners.click({ target: { closest: () => ({ dataset: { action: "set-current", version: "0.4.0.r2", channel: "stable" }, disabled: false }) } });
  await settle();
  assert.deepEqual(writes(app), [
    ["POST", "/methodology/publish", { channel: "test", note: "новый этап", important: true }],
    ["POST", "/bundles/0.4.0.r2/current", { channel: "stable" }],
  ]);
});

test("сравнение версий показывает строки с правками и сворачивает остальное", () => {
  const app = load();
  const before = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"].join("\n");
  const after = ["a", "b", "c", "d", "e", "f", "g", "H", "i", "j", "k"].join("\n");
  const lines = JSON.parse(app.run(`JSON.stringify(lineDiff(${JSON.stringify(before)}, ${JSON.stringify(after)}).map(([mark, line]) => mark + line))`));
  assert.deepEqual(lines, [" …", " e", " f", " g", "-h", "+H", " i", " j", "+k"]);
});
