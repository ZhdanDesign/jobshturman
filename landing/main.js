"use strict";

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

// Маршрут: метка один раз проходит 26 этапов при загрузке.
const route = document.querySelector(".route");
const course = document.getElementById("course");
if (route && course && !calm && typeof course.beginElement === "function") {
  route.querySelector(".marker").removeAttribute("transform");
  route.classList.add("is-ready", "is-running");
  course.beginElement();
} else if (route) {
  route.classList.add("is-ready");
}

// Шаги «Как это работает» оживают, когда попадают на экран.
const steps = document.querySelectorAll(".step");
if (!calm && "IntersectionObserver" in window) {
  const seen = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-visible");
      seen.unobserve(entry.target);
    });
  }, { threshold: 0.35 });
  steps.forEach((step) => seen.observe(step));
} else {
  steps.forEach((step) => step.classList.add("is-visible"));
}
