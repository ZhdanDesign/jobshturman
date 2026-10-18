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
  mail: "mailto:alexeyshturbin@gmail.com",
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

// Вкладки Codex / Claude Code.
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
