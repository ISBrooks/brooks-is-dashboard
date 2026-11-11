/* Brooks IS Dashboard - revenue.js
   Revenue tab (by agent) and the same components drawn for Revenue by Source on the Marketing tab.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

/* ---------- revenue tab ----------
   One set of components (records, chart, table, year picker) drawn for two
   views: the Revenue tab (by agent) and the Sources tab's Revenue by Source
   (by lead source). Each view keeps its own state object; every renderer takes
   the view as its last argument and defaults to the Revenue tab. */
const REV = {
  year: null, data: null, chart: null, hidden: new Set(), colorOf: {},
  api: "/api/revenue", apiOverview: "/api/revenue_overview",
  el: { years: "#rev-years", records: "#rev-records", chart: "#rev-chart-box", table: "#rev-table" },
  noun: "sellers", col: "Agent", total: "Team", hof: "🏆 Hall of Fame · individual",
};

const SREV = {
  year: null, data: null, chart: null, hidden: new Set(), colorOf: {},
  api: "/api/source_revenue", apiOverview: "/api/source_revenue_overview",
  el: { years: "#srev-years", records: "#srev-records", chart: "#srev-chart-box", table: "#srev-table" },
  level: "channel", q: "&level=channel",
  noun: "channels", col: "Channel", total: "All sources", hof: "🏆 Top channel",
};

// four fixed colours for the channels so they read the same on every visit
const CHANNEL_COLORS = { "Paid": "#1e3659", "Organic": "#1e8e5a", "Affiliate Marketing": "#d98b1f", "Generic": "#7048ba", "Other": "#9aa7b4" };

// Sources tab: Channels (Paid / Organic / Affiliate / Generic / Other, opening to their
// source groups) or Sources (groups, opening to their providers)
function initSrevLevel() {
  const strip = $("#srev-level");
  if (!strip || strip.dataset.bound) return;
  strip.dataset.bound = "1";
  for (const [val, label] of [["channel", "Channels"], ["source", "Sources"]]) {
    const b = document.createElement("button");
    b.textContent = label; b.className = val === SREV.level ? "on" : "";
    b.addEventListener("click", () => {
      if (SREV.level === val) return;
      SREV.level = val; SREV.q = "&level=" + val;
      SREV.noun = val === "channel" ? "channels" : "sources";
      SREV.col = val === "channel" ? "Channel" : "Source";
      SREV.hof = val === "channel" ? "🏆 Top channel" : "🏆 Top source";
      strip.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      SREV.year === "overview" ? loadRevenueOverview(SREV) : loadRevenue(SREV.year, SREV);
    });
    strip.appendChild(b);
  }
}

async function loadRevenue(year, S = REV) {
  const d = await (await fetch(`${S.api}?year=${year || 0}${S.q || ""}`)).json();
  S.year = d.year; S.data = d; S.chart = d; S.hidden = new Set(); S.colorOf = {};
  for (const a of d.agents) S.colorOf[a.agent] = a.is_other ? "#9aa7b4" : (CHANNEL_COLORS[a.agent] || colorFor(a.agent));
  renderYearPicker(d.years, d.year, S);
  renderRevRecords(d, S);
  renderRevChart(S);
  renderRevTable(d, S);
}

// All-time view: one chart line per YEAR, the per-agent table by year, and
// all-time records. Same components as a year tab, different data.
async function loadRevenueOverview(S = REV) {
  const d = await (await fetch(`${S.apiOverview}?v=1${S.q || ""}`)).json();
  // S.year is a sentinel here so re-opening the tab doesn't reset to a year
  S.year = "overview"; S.data = d; S.chart = d.chart;
  S.hidden = new Set(); S.colorOf = {};
  const pal = ["#1e3659", "#d98b1f", "#7048ba", "#1e8e5a", "#b3323c", "#0e7490"];
  // newest year first, so the current year always gets the strongest colour
  d.chart.agents.slice().reverse().forEach((s, i) => { S.colorOf[s.agent] = pal[i % pal.length]; });
  renderYearPicker(d.years, "overview", S);
  renderRevRecords(d, S);
  renderRevChart(S);
  renderRevOverviewTable(d, S);
}

function revColor(name, S = REV) { return S.colorOf[name] || "#9aa7b4"; }

function renderYearPicker(years, active, S = REV) {
  const box = $(S.el.years); box.replaceChildren();
  const ov = document.createElement("button");
  ov.className = "year-btn" + (active === "overview" ? " active" : "");
  ov.textContent = "Overview";
  ov.addEventListener("click", () => loadRevenueOverview(S));
  box.appendChild(ov);
  for (const y of years.slice().reverse()) {
    const b = document.createElement("button");
    b.className = "year-btn" + (y === active ? " active" : "");
    b.textContent = y;
    b.addEventListener("click", () => loadRevenue(y, S));
    box.appendChild(b);
  }
}

const revMoney = (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString());

function renderRevRecords(d, S = REV) {
  const card = (title, rec, withAgent) => {
    if (!rec) return `<div class="rev-card"><div class="rev-card-label">${title}</div><div class="rev-card-amt">–</div></div>`;
    return `<div class="rev-card"><div class="rev-card-label">${title}</div>`
      + `<div class="rev-card-amt">${revMoney(rec.amount)}</div>`
      + `<div class="rev-card-sub">${withAgent ? `<b>${rec.agent}</b> · ` : ""}${rec.label}</div></div>`;
  };
  const tr = d.team_records, hof = d.hall_of_fame;
  const per = d.year == null ? "all time" : d.year;
  $(S.el.records).innerHTML =
    `<div class="rev-hero"><span class="rev-hero-amt">${revMoney(d.team_total)}</span>`
    + `<span class="rev-hero-sub">total revenue sold ${d.year == null ? "all time" : "in " + d.year} · ${d.seller_count} ${S.noun}</span></div>`
    + `<div class="rev-groups">`
    + `<div class="rev-group"><h4>Team records · ${per}</h4>`
    + `<div class="rev-cards g-team">${card("Biggest Day", tr.day)}${card("Biggest Week", tr.week)}${card("Biggest Month", tr.month)}</div></div>`
    + `<div class="rev-group"><h4>${S.hof} · ${per}</h4>`
    + `<div class="rev-cards g-hof">${card("Biggest Day", hof.day, true)}${card("Biggest Week", hof.week, true)}${card("Biggest Month", hof.month, true)}</div></div>`
    + `</div>`;
}

function renderRevTable(d, S = REV) {
  const wrap = document.createElement("div"); wrap.className = "table-wrap sticky-grid";
  const t = document.createElement("table");
  const reals = d.agents.filter((a) => !a.is_other);
  const other = d.agents.find((a) => a.is_other);
  const cols = [{ key: "agent", label: S.col }]
    .concat(d.months.map((m, i) => ({ key: i, label: m })))
    .concat([{ key: "total", label: "Total" },
             { key: "best_day", label: "Best Day" },
             { key: "best_week", label: "Best Week" }]);
  const st = { key: "total", asc: false };  // sort real agents; Other/Team pinned
  const valOf = (a, key) => (
    key === "agent" ? a.agent
    : key === "total" ? (a.total || 0)
    : key === "best_day" ? (a.best_day?.amount || 0)
    : key === "best_week" ? (a.best_week?.amount || 0)
    : (a.months[key] || 0));
  const dayLbl = (iso) => new Date(iso + "T00:00:00")
    .toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const bestCell = (b, lbl) => (b
    ? `<td class="rev-best">${revMoney(b.amount)}<span>${lbl}</span></td>`
    : `<td>–</td>`);
  const rowHtml = (a, cls, child) => `<tr${cls ? ` class="${cls}"` : ""}>`
    + (child ? `<td class="src-child-name">${esc(a.agent)}</td>` : nameCell(a))
    + a.months.map((v) => `<td>${v ? revMoney(v) : "–"}</td>`).join("")
    + `<td class="hl">${revMoney(a.total)}</td>`
    + bestCell(a.best_day, a.best_day ? dayLbl(a.best_day.date) : "")
    + bestCell(a.best_week, a.best_week ? a.best_week.label : "")
    + `</tr>`;
  const open = new Set();
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const nameCell = (a) => (a.children?.length
    ? `<td><span class="src-toggle" data-grp="${esc(a.agent)}">${open.has(a.agent) ? "▼" : "▶"}</span> ${esc(a.agent)}</td>`
    : `<td>${esc(a.agent)}</td>`);
  const withKids = (a) => rowHtml(a) + (open.has(a.agent)
    ? a.children.map((c) => rowHtml(c, "src-child", true)).join("") : "");

  function render() {
    const sorted = reals.slice().sort((x, y) => {
      const a = valOf(x, st.key), b = valOf(y, st.key);
      const c = typeof a === "string" ? a.localeCompare(b) : a - b;
      return st.asc ? c : -c;
    });
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const col of cols) {
      const th = document.createElement("th");
      th.textContent = col.label;
      if (st.key === col.key) th.classList.add("sorted", ...(st.asc ? ["asc"] : []));
      th.addEventListener("click", () => {
        if (st.key === col.key) st.asc = !st.asc;
        else { st.key = col.key; st.asc = col.key === "agent"; }  // names A→Z, numbers high→low
        render();
      });
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    const tb = document.createElement("tbody");
    let html = sorted.map(withKids).join("");
    if (other) html += rowHtml(other, "dim");
    const tr_ = d.team_records || {};
    html += `<tr class="total-row"><td>${S.total}</td>`
      + d.team_month_totals.map((v) => `<td>${v ? revMoney(v) : "–"}</td>`).join("")
      + `<td>${revMoney(d.team_total)}</td>`
      + bestCell(tr_.day && { amount: tr_.day.amount }, tr_.day ? dayLbl(tr_.day.label) : "")
      + bestCell(tr_.week && { amount: tr_.week.amount }, tr_.week ? tr_.week.label : "")
      + `</tr>`;
    tb.innerHTML = html;
    tb.addEventListener("click", (ev) => {
      const g = ev.target.closest(".src-toggle");
      if (!g) return;
      open.has(g.dataset.grp) ? open.delete(g.dataset.grp) : open.add(g.dataset.grp);
      render();
    });
    t.replaceChildren(thead, tb);
  }
  render();
  wrap.appendChild(t);
  $(S.el.table).replaceChildren(wrap);
}

function renderRevOverviewTable(d, S = REV) {
  const wrap = document.createElement("div"); wrap.className = "table-wrap sticky-grid";
  const t = document.createElement("table");
  const reals = d.agents.filter((a) => !a.is_other);
  const other = d.agents.find((a) => a.is_other);
  const cur = d.years[d.years.length - 1];
  const cols = [{ key: "agent", label: S.col }]
    .concat(d.years.map((y, i) => ({ key: i, label: String(y), tip: y === cur ? "Year to date" : "" })))
    .concat([{ key: "total", label: "Total" },
             { key: "best_year", label: "Best Year" },
             { key: "best_month", label: "Best Month" }]);
  const st = { key: "total", asc: false };  // sort real agents; Other/Team pinned
  const valOf = (a, key) => (
    key === "agent" ? a.agent
    : key === "total" ? (a.total || 0)
    : key === "best_year" ? (a.best_year?.amount || 0)
    : key === "best_month" ? (a.best_month?.amount || 0)
    : (a.years[key] || 0));
  const bestCell = (b) => (b
    ? `<td class="rev-best">${revMoney(b.amount)}<span>${b.label}</span></td>`
    : `<td>–</td>`);
  const rowHtml = (a, cls, child) => `<tr${cls ? ` class="${cls}"` : ""}>`
    + (child ? `<td class="src-child-name">${esc(a.agent)}</td>` : nameCell(a))
    + a.years.map((v) => `<td>${v ? revMoney(v) : "–"}</td>`).join("")
    + `<td class="hl">${revMoney(a.total)}</td>`
    + bestCell(a.best_year) + bestCell(a.best_month)
    + `</tr>`;
  const open = new Set();
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  const nameCell = (a) => (a.children?.length
    ? `<td><span class="src-toggle" data-grp="${esc(a.agent)}">${open.has(a.agent) ? "▼" : "▶"}</span> ${esc(a.agent)}</td>`
    : `<td>${esc(a.agent)}</td>`);
  const withKids = (a) => rowHtml(a) + (open.has(a.agent)
    ? a.children.map((c) => rowHtml(c, "src-child", true)).join("") : "");

  function render() {
    const sorted = reals.slice().sort((x, y) => {
      const a = valOf(x, st.key), b = valOf(y, st.key);
      const c = typeof a === "string" ? a.localeCompare(b) : a - b;
      return st.asc ? c : -c;
    });
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const col of cols) {
      const th = document.createElement("th");
      th.textContent = col.label;
      if (col.tip) th.title = col.tip;
      if (st.key === col.key) th.classList.add("sorted", ...(st.asc ? ["asc"] : []));
      th.addEventListener("click", () => {
        if (st.key === col.key) st.asc = !st.asc;
        else { st.key = col.key; st.asc = col.key === "agent"; }  // names A→Z, numbers high→low
        render();
      });
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    const tb = document.createElement("tbody");
    let html = sorted.map(withKids).join("");
    if (other) html += rowHtml(other, "dim");
    html += `<tr class="total-row"><td>${S.total}</td>`
      + d.year_totals.map((v) => `<td>${v ? revMoney(v) : "–"}</td>`).join("")
      + `<td>${revMoney(d.team_total)}</td>`
      + bestCell(d.team_best_year) + bestCell(d.team_records?.month)
      + `</tr>`;
    tb.innerHTML = html;
    tb.addEventListener("click", (ev) => {
      const g = ev.target.closest(".src-toggle");
      if (!g) return;
      open.has(g.dataset.grp) ? open.delete(g.dataset.grp) : open.add(g.dataset.grp);
      render();
    });
    t.replaceChildren(thead, tb);
  }
  render();
  wrap.appendChild(t);
  $(S.el.table).replaceChildren(wrap);
}

function renderRevChart(S = REV) {
  const d = S.chart;
  const box = $(S.el.chart); box.replaceChildren();
  const chart = document.createElement("div"); chart.className = "rev-chart";
  const tip = document.createElement("div"); tip.className = "rev-tip"; tip.style.display = "none";
  chart.appendChild(tip);
  chart.appendChild(revChartSvg(d, chart, tip, S));
  box.appendChild(chart);
  const legend = document.createElement("div"); legend.className = "rev-legend";
  for (const a of d.agents) {
    const b = document.createElement("button");
    b.className = "rev-leg" + (S.hidden.has(a.agent) ? " off" : "");
    b.innerHTML = `<span class="sw" style="background:${revColor(a.agent, S)}"></span>${a.agent}`;
    b.title = (S.right && S.right.keys.has(a.agent)) ? "" : (S.valFmt || revMoney)(a.total);
    b.addEventListener("click", () => {
      S.hidden.has(a.agent) ? S.hidden.delete(a.agent) : S.hidden.add(a.agent);
      renderRevChart(S);
    });
    legend.appendChild(b);
  }
  box.appendChild(legend);
}

function revChartSvg(d, container, tip, S = REV) {
  const ns = "http://www.w3.org/2000/svg";
  // S.right = { keys: Set(series names), axisFmt, valFmt, yFloor } puts those
  // series on a second scale drawn on the right-hand edge (dashed lines)
  const right = S.right || null;
  const onRight = (name) => !!(right && right.keys.has(name));
  const W = 920, H = 360, M = { t: 14, r: right ? 58 : 16, b: 30, l: 60 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const visible = d.agents.filter((a) => !S.hidden.has(a.agent));
  const nMo = d.months.length;
  const ymax = Math.max(S.yFloor ?? 1000, ...visible.filter((a) => !onRight(a.agent)).flatMap((a) => a.months)) * 1.08;
  // right axis: either fixed ticks (padded by one step, widened if the data runs past them) or 0..max
  const rightVals = right ? visible.filter((a) => onRight(a.agent)).flatMap((a) => a.months).filter((v) => v > 0) : [];
  let yminR = 0, ymaxR = right ? Math.max(right.yFloor ?? 10, ...rightVals) * 1.08 : 1;
  let rticks = null;
  if (right && right.ticks && right.ticks.length) {
    const step = right.ticks[1] - right.ticks[0] || 5;
    let lo = right.ticks[0] - step, hi = right.ticks[right.ticks.length - 1] + step;
    const dmax = Math.max(...rightVals, 0), dmin = rightVals.length ? Math.min(...rightVals) : lo;
    while (hi < dmax) hi += step;
    while (lo > dmin && lo - step >= 0) lo -= step;
    yminR = lo; ymaxR = hi;
    rticks = []; for (let t = lo; t <= hi + 1e-9; t += step) rticks.push(t);
  }
  const axisFmt = S.axisFmt || ((v) => "$" + Math.round(v / 1000) + "k");
  const valFmt = S.valFmt || revMoney;
  const fmtFor = (name) => (onRight(name) ? (right.valFmt || valFmt) : valFmt);
  const X = (i) => M.l + (i / Math.max(1, nMo - 1)) * iw;
  const Y = (v) => M.t + ih - (v / ymax) * ih;
  const Y2 = (v) => M.t + ih - (Math.min(Math.max(v, yminR), ymaxR) - yminR) / (ymaxR - yminR) * ih;
  const Yof = (name) => (onRight(name) ? Y2 : Y);
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "rev-svg");
  for (let g = 0; g <= 4; g++) {
    const v = (ymax / 4) * g;
    if (!rticks) {                               // with fixed right ticks the gridlines belong to them
      const ln = document.createElementNS(ns, "line");
      ln.setAttribute("x1", M.l); ln.setAttribute("x2", W - M.r);
      ln.setAttribute("y1", Y(v)); ln.setAttribute("y2", Y(v)); ln.setAttribute("class", "trend-grid");
      svg.appendChild(ln);
    }
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", M.l - 8); t.setAttribute("y", Y(v) + 3);
    t.setAttribute("text-anchor", "end"); t.setAttribute("class", "axis-label");
    t.textContent = axisFmt(v); svg.appendChild(t);
    if (right && !rticks) {
      const tr = document.createElementNS(ns, "text");
      tr.setAttribute("x", W - M.r + 8); tr.setAttribute("y", Y(v) + 3);
      tr.setAttribute("text-anchor", "start"); tr.setAttribute("class", "axis-label");
      tr.textContent = (right.axisFmt || axisFmt)((ymaxR / 4) * g); svg.appendChild(tr);
    }
  }
  for (const tv of rticks || []) {               // hard lines at every right-axis tick
    const ln = document.createElementNS(ns, "line");
    ln.setAttribute("x1", M.l); ln.setAttribute("x2", W - M.r);
    ln.setAttribute("y1", Y2(tv)); ln.setAttribute("y2", Y2(tv)); ln.setAttribute("class", "trend-grid trend-grid-strong");
    svg.appendChild(ln);
    const tr = document.createElementNS(ns, "text");
    tr.setAttribute("x", W - M.r + 8); tr.setAttribute("y", Y2(tv) + 3);
    tr.setAttribute("text-anchor", "start"); tr.setAttribute("class", "axis-label");
    tr.textContent = (right.axisFmt || axisFmt)(tv); svg.appendChild(tr);
  }
  d.months.forEach((m, i) => {
    if (S.labelEvery && i % S.labelEvery) return;      // thin out crowded week labels
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", X(i)); t.setAttribute("y", H - M.b + 20);
    t.setAttribute("text-anchor", "middle"); t.setAttribute("class", "axis-label");
    t.textContent = m; svg.appendChild(t);
  });
  for (const a of visible) {
    const color = revColor(a.agent, S);
    const Yc = Yof(a.agent);
    let dstr = "";
    a.months.forEach((v, i) => { dstr += (i ? "L" : "M") + X(i).toFixed(1) + " " + Yc(v).toFixed(1) + " "; });
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", dstr); p.setAttribute("fill", "none"); p.setAttribute("stroke", color);
    p.setAttribute("stroke-width", "2"); p.setAttribute("stroke-linejoin", "round");
    if (onRight(a.agent)) p.setAttribute("stroke-dasharray", "6 4");
    p.setAttribute("stroke-linecap", "round"); svg.appendChild(p);
  }
  const guide = document.createElementNS(ns, "line");
  guide.setAttribute("class", "trend-guide"); guide.setAttribute("y1", M.t); guide.setAttribute("y2", H - M.b);
  guide.style.display = "none"; svg.appendChild(guide);
  const ov = document.createElementNS(ns, "rect");
  ov.setAttribute("x", M.l); ov.setAttribute("y", M.t); ov.setAttribute("width", iw); ov.setAttribute("height", ih);
  ov.setAttribute("fill", "transparent"); svg.appendChild(ov);
  const showAt = (clientX) => {
    const rect = svg.getBoundingClientRect();
    let i = Math.round(((clientX - rect.left) / rect.width * W - M.l) / iw * (nMo - 1));
    i = Math.max(0, Math.min(nMo - 1, i));
    guide.setAttribute("x1", X(i)); guide.setAttribute("x2", X(i)); guide.style.display = "";
    const items = visible.map((a) => ({ name: a.agent, v: a.months[i] })).filter((x) => x.v > 0)
      .sort((a, b) => b.v - a.v).slice(0, 12);
    tip.innerHTML = `<div class="rev-tip-h">${d.months[i]}${d.year ? " " + d.year : ""}</div>`
      + (items.map((x) => `<div><span class="sw" style="background:${revColor(x.name, S)}"></span>${x.name}<b>${fmtFor(x.name)(x.v)}</b></div>`).join("") || "<div class='dim'>nothing</div>");
    tip.style.display = "block";
    const px = (X(i) / W) * rect.width;
    tip.style.left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth) + "px";
  };
  const hide = () => { guide.style.display = "none"; tip.style.display = "none"; };
  ov.addEventListener("mousemove", (ev) => showAt(ev.clientX));
  ov.addEventListener("mouseleave", hide);
  ov.addEventListener("touchstart", (ev) => showAt(ev.touches[0].clientX), { passive: true });
  ov.addEventListener("touchmove", (ev) => { ev.preventDefault(); showAt(ev.touches[0].clientX); }, { passive: false });
  ov.addEventListener("touchend", hide);
  return svg;
}
