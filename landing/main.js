"use strict";

// Название продукта — только здесь. Тексты в HTML — запасной вариант, если JS не загрузился;
// при переименовании замените и их: grep -rl "JobShturman" landing | xargs sed -i '' 's/JobShturman/НовоеИмя/g'
const BRAND = "JobShturman";
const FALLBACK_BRAND = "JobShturman";
document.querySelectorAll("[data-brand]").forEach((el) => { el.textContent = BRAND; });
document.querySelectorAll("[data-brand-attr]").forEach((el) => {
  const attr = el.dataset.brandAttr;
  el.setAttribute(attr, el.getAttribute(attr).replaceAll(FALLBACK_BRAND, BRAND));
});
document.title = document.title.replaceAll(FALLBACK_BRAND, BRAND);

// Все внешние адреса — только здесь. channel и bot пока заглушки: замените на настоящие ссылки.
const LINKS = {
  author: "https://t.me/Aleksei_Shturbin",
  channel: "#tg-channel-todo",
  bot: "#tg-bot-todo",
};

document.querySelectorAll("[data-link]").forEach((link) => {
  link.href = LINKS[link.dataset.link];
});

const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Сцена в первом экране: весь путь от выбора агента до откликов, по кругу, с паузой и переходом по клику.
// Каждый новый круг выбирает другого агента: Claude Code, затем Codex.
const scene = document.querySelector(".scene");
const sceneSteps = [...document.querySelectorAll(".scene-steps li")];
const pause = document.querySelector(".scene-pause");
const SCENE_MS = [3400, 4600, 3200, 3000, 3400, 3200, 4800];
const FRAMES = SCENE_MS.length;
const sceneStep = document.querySelector(".scene-step");
let current = 1;
let timer = 0;
let playing = !calm;

function show(n) {
  current = n;
  scene.dataset.scene = String(n);
  sceneStep.textContent = `${n} / ${FRAMES}`;
  sceneSteps.forEach((li) => li.classList.toggle("is-active", li.dataset.for === String(n)));
  const sent = scene.querySelector(".num[data-to]");
  if (sent) countUp(sent, n === FRAMES);
}

function countUp(el, run) {
  const from = Number(el.dataset.from);
  const to = Number(el.dataset.to);
  if (!run || calm) { el.textContent = String(to); return; }
  el.textContent = String(from);
  let value = from;
  const tick = () => {
    if (scene.dataset.scene !== String(FRAMES) || value >= to) return;
    value += 1;
    el.textContent = String(value);
    setTimeout(tick, 420);
  };
  setTimeout(tick, 600);
}

function schedule() {
  clearTimeout(timer);
  if (!playing) return;
  timer = setTimeout(() => {
    if (current === FRAMES) scene.dataset.agent = scene.dataset.agent === "claude" ? "codex" : "claude";
    show(current % FRAMES + 1);
    schedule();
  }, SCENE_MS[current - 1]);
}

function setPlaying(next) {
  playing = next;
  pause.setAttribute("aria-pressed", String(!playing));
  pause.textContent = playing ? "Пауза" : "Смотреть";
  schedule();
}

if (scene) {
  show(calm ? FRAMES : 1);
  sceneSteps.forEach((li) => {
    li.querySelector("button").addEventListener("click", () => {
      show(Number(li.dataset.for));
      setPlaying(false);
    });
  });
  pause.addEventListener("click", () => setPlaying(!playing));
  // Не крутить сцену, когда её не видно.
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) clearTimeout(timer);
      else schedule();
    }).observe(scene);
  }
  setPlaying(playing);
}

// Вкладки агентов: Codex, Claude Code, Grok Bot, OpenClaw, Hermes, DSH.
const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectTab(tab) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
  play(document.getElementById(tab.getAttribute("aria-controls")).querySelector(".app"));
}
tabs.forEach((tab, i) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    const next = tabs[(i + step + tabs.length) % tabs.length];
    next.focus();
    selectTab(next);
  });
});

// Окно агента проигрывает диалог установки, когда попадает на экран.
function play(app) {
  if (!app) return;
  app.classList.remove("is-playing");
  if (calm) {
    app.querySelectorAll(".msg").forEach((m) => { m.style.opacity = "1"; m.style.translate = "none"; });
    return;
  }
  void app.offsetWidth;
  app.classList.add("is-playing");
}
const apps = document.querySelectorAll(".app");
if ("IntersectionObserver" in window) {
  const seen = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      play(entry.target);
      seen.unobserve(entry.target);
    });
  }, { threshold: 0.4 });
  apps.forEach((app) => seen.observe(app));
} else {
  apps.forEach(play);
}

// Тарифы берутся с сервера ключей: правка в админке доходит до лендинга без выкладки.
// Если сервер не ответил, остаются цены из разметки.
const plans = document.querySelectorAll("[data-tariff]");
if (plans.length) {
  const NBSP = "\u00a0";
  const rub = (n) => n.toLocaleString("ru-RU").replace(/\s/g, NBSP) + NBSP + "₽";
  const plural = (n, one, few, many) => {
    const d = n % 10, dd = n % 100;
    return d === 1 && dd !== 11 ? one : d >= 2 && d <= 4 && (dd < 12 || dd > 14) ? few : many;
  };
  fetch(new URL("keys/v1/tariffs", document.baseURI))
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((list) => {
      const byId = Object.fromEntries(list.map((t) => [t.id, t]));
      plans.forEach((card) => {
        const t = byId[card.dataset.tariff];
        if (!t) return;
        if (!t.active) { card.hidden = true; return; }
        const price = t.price_rub_to > t.price_rub_from ? "от" + NBSP + rub(t.price_rub_from) : rub(t.price_rub_from);
        const term = t.months + NBSP + plural(t.months, "месяц", "месяца", "месяцев");
        // «до 1 устройства», «до 2 устройств»
        const devices = t.device_limit + NBSP + (t.device_limit % 10 === 1 && t.device_limit % 100 !== 11 ? "устройства" : "устройств");
        card.querySelectorAll('[data-t="price"]').forEach((el) => { el.textContent = price; });
        card.querySelectorAll('[data-t="term"]').forEach((el) => { el.textContent = term; });
        card.querySelectorAll('[data-t="devices"]').forEach((el) => { el.textContent = devices; });
      });
    })
    .catch(() => {});
}

// Кнопка «Скопировать».
document.querySelectorAll(".copy").forEach((box) => {
  const button = box.querySelector(".copy-btn");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(box.querySelector("code").textContent);
      button.textContent = "Скопировано";
      button.classList.add("is-done");
      setTimeout(() => { button.textContent = "Скопировать"; button.classList.remove("is-done"); }, 2000);
    } catch {
      getSelection().selectAllChildren(box.querySelector("code"));
    }
  });
});

// Линия шагов настройки заполняется по мере прокрутки, пройденные шаги становятся зелёными.
const path = document.querySelector(".path-steps");
if (path) {
  const items = [...path.children];
  let queued = false;
  const update = () => {
    queued = false;
    const cursor = innerHeight * 0.6;
    const nums = items.map((li) => li.querySelector(".step-n").getBoundingClientRect());
    const first = nums[0].top + nums[0].height / 2;
    const last = nums[nums.length - 1].top + nums[nums.length - 1].height / 2;
    const progress = Math.min(1, Math.max(0, (cursor - first) / (last - first)));
    path.style.setProperty("--progress", progress.toFixed(4));
    path.style.setProperty("--tail", `${path.getBoundingClientRect().bottom - last}px`);
    items.forEach((li, i) => li.classList.toggle("is-done", nums[i].top + nums[i].height / 2 <= cursor));
  };
  const request = () => { if (!queued) { queued = true; requestAnimationFrame(update); } };
  addEventListener("scroll", request, { passive: true });
  addEventListener("resize", request);
  update();
}

// Мобильное меню: гамбургер раскрывает разделы, выбор пункта закрывает меню.
const siteHeader = document.querySelector(".site-header");
const menuToggle = siteHeader?.querySelector(".menu-toggle");
if (menuToggle) {
  const setMenu = (open) => {
    siteHeader.classList.toggle("menu-open", open);
    menuToggle.setAttribute("aria-expanded", String(open));
    menuToggle.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
  };
  menuToggle.addEventListener("click", () => setMenu(!siteHeader.classList.contains("menu-open")));
  siteHeader.querySelectorAll("nav a").forEach((link) => link.addEventListener("click", () => setMenu(false)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });
  document.addEventListener("click", (e) => { if (!siteHeader.contains(e.target)) setMenu(false); });
}

// Номер последней версии берётся со страницы «Что нового»: при выпуске правится только она.
const latestVersion = document.querySelector("[data-latest-version]");
if (latestVersion) {
  fetch(new URL("releases/", document.baseURI))
    .then((r) => (r.ok ? r.text() : Promise.reject(r.status)))
    .then((html) => {
      const first = new DOMParser().parseFromString(html, "text/html").querySelector(".release[data-version]");
      if (first) latestVersion.textContent = first.dataset.version;
    })
    .catch(() => {});
}

// На телефоне тап по окну агента подсказывает, что это только пример.
document.querySelectorAll(".app").forEach((app) => {
  let timer;
  app.addEventListener("click", () => {
    app.classList.add("is-noted");
    clearTimeout(timer);
    timer = setTimeout(() => app.classList.remove("is-noted"), 4000);
  });
});
