"use strict";

// --- Nav background + scroll progress ---------------------------------------

const nav = document.querySelector(".site-nav");
const progressBar = document.getElementById("progress-bar");

function onScroll() {
  nav.classList.toggle("scrolled", window.scrollY > 10);
  const max = document.documentElement.scrollHeight - window.innerHeight;
  progressBar.style.width = `${max > 0 ? (window.scrollY / max) * 100 : 0}%`;
}
document.addEventListener("scroll", onScroll, { passive: true });
onScroll();

// --- Reveal-on-scroll ---------------------------------------------------------

const io = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("in-view");
        io.unobserve(entry.target);
      }
    }
  },
  { threshold: 0.2, rootMargin: "0px 0px -8% 0px" }
);
document.querySelectorAll(".reveal").forEach((el) => io.observe(el));

// --- Result section: live counts from data/manifest.json ----------------------

function animateNumber(el, target, suffix = "") {
  const duration = 900;
  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(target * eased).toLocaleString("pt-BR") + suffix;
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

async function loadResults() {
  const statGrid = document.getElementById("stat-grid");
  const chart = document.getElementById("chart");
  try {
    const resp = await fetch("data/manifest.json");
    const manifest = await resp.json();
    const municipalities = manifest.municipalities || [];

    const totalsByVariant = {};
    const rows = [];
    for (const m of municipalities) {
      for (const v of m.variants) {
        totalsByVariant[v.id] = (totalsByVariant[v.id] || 0) + v.count;
        rows.push({ municipality: m.name, state: m.state, variant: v.label, count: v.count });
      }
    }

    const stats = [
      { value: municipalities.length, label: "município(s) modelados", suffix: "" },
      ...Object.entries(totalsByVariant).map(([id, total]) => ({
        value: total,
        label: municipalities.find((m) => m.variants.some((v) => v.id === id)).variants.find((v) => v.id === id)
          .label,
        suffix: "",
      })),
    ];

    statGrid.innerHTML = stats
      .map(
        (s, i) => `
      <div class="stat-card reveal" style="transition-delay:${i * 0.08}s">
        <div class="value" data-target="${s.value}">0</div>
        <div class="label">${s.label}</div>
      </div>`
      )
      .join("");
    statGrid.querySelectorAll(".reveal").forEach((el) => io.observe(el));

    const maxCount = Math.max(...rows.map((r) => r.count), 1);
    chart.innerHTML = rows
      .map(
        (r, i) => `
      <div class="chart-row reveal" style="transition-delay:${i * 0.06}s">
        <div class="name">${r.municipality} — ${r.state}<small>${r.variant}</small></div>
        <div class="chart-track"><div class="chart-fill" data-pct="${(r.count / maxCount) * 100}"></div></div>
        <div class="count" data-target="${r.count}">0</div>
      </div>`
      )
      .join("");

    const chartObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const row = entry.target;
          row.classList.add("in-view");
          const fill = row.querySelector(".chart-fill");
          fill.style.width = `${fill.dataset.pct}%`;
          const countEl = row.querySelector(".count");
          animateNumber(countEl, Number(countEl.dataset.target));
          chartObserver.unobserve(row);
        }
      },
      { threshold: 0.4 }
    );
    chart.querySelectorAll(".chart-row").forEach((row) => chartObserver.observe(row));

    const statObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const valueEl = entry.target.querySelector(".value");
          if (valueEl) animateNumber(valueEl, Number(valueEl.dataset.target));
          statObserver.unobserve(entry.target);
        }
      },
      { threshold: 0.4 }
    );
    statGrid.querySelectorAll(".stat-card").forEach((card) => statObserver.observe(card));
  } catch (err) {
    statGrid.innerHTML = "";
    chart.innerHTML = `<p class="chart-note">Não foi possível carregar os dados agora (${err.message}).</p>`;
  }
}

loadResults();
