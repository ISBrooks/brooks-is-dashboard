/* Brooks IS Dashboard - home.js
   Home tab: the metric rows, their 13-week trends and per-day drop-downs, and the year-to-date table.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

/* ---------- home ---------- */
const M_COLORS = {
  all: "#0d9488", phone: "#1e3659", form: "#1e8e5a",
  multiyear: "#7048ba", acv: "#d98b1f", aip: "#c0396f", sl: "#4f46e5", sts: "#5b6b7c",
  signed: "#b3323c", quality: "#0e7490",
};

let _sparkId = 0;

async function loadHome() {
  const [h, tr] = await Promise.all([
    fetch("/api/home").then((r) => r.json()),
    fetch("/api/home_trends").then((r) => r.json()),
  ]);
  const pct = (v) => (v == null ? "–" : v.toFixed(1) + "%");
  const money = (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString());
  const S = tr.series || {};

  // All Leads is first and gets its own color (teal), distinct from Phone.
  const defs = [
    { key: "all", name: "All Leads Conversion", sub: "sheet + form combined",
      d30: h.all30, d7: h.all7, val: (d) => pct(d && d.pct),
      detail: (d) => (d ? `${d.conv} of ${d.total} leads` : ""), delta: () => null },
    { key: "phone", name: "Phone Conversion", sub: "audit team's sales ÷ qualified",
      d30: h.sheet30, d7: h.sheet7, val: (d) => pct(d && d.pct),
      detail: (d) => (d ? `${d.sales} of ${d.qual} qualified calls` : ""),
      delta: (d) => (d && d.prev_pct != null ? d.pct - d.prev_pct : null) },
    { key: "form", name: "Form Conversion", sub: "net form leads that converted",
      d30: h.form30, d7: h.form7, val: (d) => pct(d && d.pct),
      detail: (d) => (d ? `${d.conv} of ${d.net} net form leads` : ""),
      delta: (d) => (d && d.prev_pct != null ? d.pct - d.prev_pct : null), daily: true },
    { key: "multiyear", name: "Team Multiyear", sub: "18mo+ agreements",
      d30: h.team30, d7: h.team7, val: (d) => pct(d && d.multiyear_pct),
      detail: (d) => (d ? `on ${d.subs.toLocaleString()} counted subs` : ""),
      delta: (d) => (d && d.prev_multiyear_pct != null ? d.multiyear_pct - d.prev_multiyear_pct : null), daily: true },
    { key: "acv", name: "Avg Contract Value", sub: "per counted subscription",
      d30: h.team30, d7: h.team7, val: (d) => money(d && d.acv), money: true,
      detail: (d) => (d ? `across ${d.subs.toLocaleString()} subs` : ""),
      delta: (d) => (d && d.prev_acv ? ((d.acv - d.prev_acv) / d.prev_acv) * 100 : null), unit: "%", daily: true },
    { key: "aip", name: "Avg Initial Price", sub: "first service price per counted subscription",
      d30: h.team30, d7: h.team7, val: (d) => money(d && d.init_avg), money: true,
      detail: (d) => (d ? `across ${d.subs.toLocaleString()} subs` : ""),
      delta: (d) => (d && d.prev_init_avg ? ((d.init_avg - d.prev_init_avg) / d.prev_init_avg) * 100 : null), unit: "%", daily: true },
    SL_DEF(null, null, true),  // Service Level — filled in async from Genesys
    QUAL_DEF(null, null, true),  // Quality Score — filled in async from VoiceOps
    { key: "sts", name: "Sold → Serviced", sub: "first service done vs scheduled",
      d30: h.sts30, d7: h.sts7, val: (d) => pct(d && d.sts_pct),
      detail: (d) => (d ? `${d.serviced} serviced, ${d.pending} pending` : ""),
      delta: (d) => (d && d.prev_pct != null ? d.sts_pct - d.prev_pct : null) },
    { key: "signed", name: "Signed Agreement", sub: "signed contracts per counted subscription",
      d30: h.team30, d7: h.team7, val: (d) => pct(d && d.signed_pct),
      detail: (d) => (d ? `across ${d.subs.toLocaleString()} subs` : ""),
      delta: (d) => (d && d.prev_signed_pct != null ? d.signed_pct - d.prev_signed_pct : null),
      daily: true },
  ];

  const head = document.createElement("div");
  head.className = "metric-head";
  head.innerHTML = "<div></div><div>Last 30 complete days</div><div>Last 7 complete days</div><div>Last 90 days</div>";
  const weeks = tr.weeks || [];
  $("#home-metrics").replaceChildren(head, ...defs.map((m) => metricRow(m, S[m.key] || [], weeks)));

  loadHomeYtd();
  loadServiceLevel();  // Genesys — patches the Service Level row when it lands
  loadQuality();       // VoiceOps — patches the Quality Score row when it lands
}

// Quality Score row definition (shared by the placeholder + the async patch).
// VoiceOps grades every inside-sales call against our models; this is the
// call-weighted team mean, and it counts only the agents mapped in Settings.
function QUAL_DEF(d30, d7, loading) {
  return {
    key: "quality", name: "Quality Score", sub: "VoiceOps AI grading of inside-sales calls",
    d30, d7, chartKind: "count",
    // drop-down only once real data has landed: loadQuality() swaps the loading
    // placeholder out, and a panel opened on the placeholder would be orphaned
    daily: !loading && d30 != null,
    val: (d) => (d && d.pct != null ? d.pct.toFixed(1) : (loading ? "…" : "–")),
    detail: (d) => (d && d.pct != null ? `${(d.calls || 0).toLocaleString()} calls graded`
      : (loading ? "loading…" : "unavailable")),
    delta: (d) => (d && d.pct != null && d.prev_pct != null ? d.pct - d.prev_pct : null),
    unit: " pts",
  };
}

async function loadQuality() {
  let q = null;
  try { q = await (await fetch("/api/home_quality")).json(); } catch (e) { /* keep placeholder */ }
  const old = document.querySelector('.metric-row[data-metric="quality"]');
  if (!old) return;
  if (!q || q.error) {
    old.replaceWith(metricRow(QUAL_DEF(null, null, false), [], []));
    return;
  }
  old.replaceWith(metricRow(QUAL_DEF(q.q30, q.q7, false), q.values || [], q.weeks || []));
}

// Service Level row definition (shared by the placeholder + the async patch).
function SL_DEF(d30, d7, loading) {
  return {
    key: "sl", name: "Service Level", sub: "80% of calls answered within 30s",
    d30, d7,
    val: (d) => (d && d.pct != null ? d.pct.toFixed(1) + "%" : (loading ? "…" : "–")),
    detail: (d) => (d && d.pct != null ? `${(d.offered || 0).toLocaleString()} calls offered`
      : (loading ? "loading…" : "unavailable")),
    delta: () => null,
  };
}

async function loadServiceLevel() {
  let s = null;
  try { s = await (await fetch("/api/home_servicelevel")).json(); } catch (e) { /* keep placeholder */ }
  const old = document.querySelector('.metric-row[data-metric="sl"]');
  if (!old) return;
  if (!s || s.error) {
    old.replaceWith(metricRow(SL_DEF(null, null, false), [], []));
    return;
  }
  old.replaceWith(metricRow(SL_DEF(s.sl30, s.sl7, false), s.values || [], s.weeks || []));
}

function metricRow(m, trend, weeks) {
  const color = m.color || M_COLORS[m.key] || "#1e3659";
  const row = document.createElement("div");
  row.className = "metric-row" + (m.daily ? " expandable" : "");
  row.dataset.metric = m.key;
  const name = document.createElement("div");
  name.className = "metric-name";
  name.style.borderLeftColor = color;
  name.innerHTML = `<b>${m.name}${m.daily ? ' <span class="metric-chev">▾</span>' : ""}</b>` +
    `<span class="metric-sub">${m.sub}</span>`;
  row.appendChild(name);
  for (const [d, per] of [[m.d30, "Last 30 days"], [m.d7, "Last 7 days"]]) {
    row.appendChild(metricBubble(m.val(d), m.delta(d), m.detail(d), color, m.unit || " pts", per,
                                 d && d.range, d && d.prev_range, m.lowerBetter));
  }
  const sp = document.createElement("div");
  sp.className = "metric-spark";
  // wide screens show the cards two-up with a shorter chart so they all fit
  sp.appendChild(trendChart(trend, weeks, color,
    m.chartKind !== undefined ? m.chartKind : !m.money,
    window.innerWidth >= 1100 ? 50 : 0));
  row.appendChild(sp);
  if (m.daily) row.addEventListener("click", (e) => {
    if (e.target.closest(".metric-spark")) return;  // chart keeps its own hover/tap
    toggleDaily(row, m.key, color);
  });
  return row;
}

/* ---- per-day drop-down under a metric row (last 7 days through yesterday) */
let _dailyCache = null;

function fetchDaily() {
  if (!_dailyCache) _dailyCache = fetch("/api/home_daily").then((r) => r.json());
  return _dailyCache;
}

async function toggleDaily(row, key, color) {
  const open = row.nextElementSibling?.classList?.contains("metric-daily");
  document.querySelectorAll(".metric-daily").forEach((p) => p.remove());
  document.querySelectorAll(".metric-row.open").forEach((r) => r.classList.remove("open"));
  if (open) return;                       // second click = close
  row.classList.add("open");
  const panel = document.createElement("div");
  panel.className = "metric-daily";
  panel.innerHTML = `<div class="metric-daily-loading">Loading daily breakdown…</div>`;
  row.after(panel);
  const d = await fetchDaily();
  if (!row.classList.contains("open")) return;   // closed while loading
  const days = d.days || [];
  const wd = (iso) => new Date(iso + "T00:00:00")
    .toLocaleDateString("en-US", { weekday: "short" });
  const md = (iso) => new Date(iso + "T00:00:00")
    .toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const money = (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString());
  const pct = (v) => (v == null ? "–" : v.toFixed(1) + "%");
  const CFG = {
    form: { note: "Conversions mature for up to 90 days — the newest days always read low at first.",
            big: (r) => pct(r.form.pct),
            sub: (r) => `${r.form.conv} of ${r.form.net}`,
            sub2: () => "net leads converted" },
    multiyear: { big: (r) => pct(r.multiyear_pct),
                 sub: (r) => `${r.subs} subs`, sub2: () => "18mo+ share" },
    acv: { big: (r) => money(r.acv),
           sub: (r) => `${r.subs} subs`, sub2: () => "avg contract" },
    aip: { big: (r) => money(r.aip),
           sub: (r) => `${r.subs} subs`, sub2: () => "avg initial" },
    signed: { big: (r) => pct(r.signed_pct),
              sub: (r) => `${r.subs} subs`, sub2: () => "signed agreements" },
    quality: { note: "Days with no graded calls (usually Sundays) read blank rather than zero.",
               big: (r) => (r.quality && r.quality.pct != null ? r.quality.pct.toFixed(1) : "–"),
               sub: (r) => (r.quality && r.quality.calls
                 ? `${r.quality.calls.toLocaleString()} calls graded` : "no graded calls"),
               sub2: (r) => (r.quality && r.quality.agents ? `${r.quality.agents} agents` : "") },
  };
  const cfg = CFG[key];
  const grid = document.createElement("div");
  grid.className = "metric-daily-grid";
  for (const r of days) {
    const card = document.createElement("div");
    card.className = "metric-daily-card";
    card.innerHTML =
      `<div class="mdc-day">${wd(r.date)} <span>${md(r.date)}</span></div>` +
      `<div class="mdc-big" style="color:${color}">${cfg.big(r)}</div>` +
      `<div class="mdc-sub">${cfg.sub(r)}</div>` +
      `<div class="mdc-sub2">${cfg.sub2(r)}</div>`;
    grid.appendChild(card);
  }
  panel.replaceChildren(grid);
  panel.style.borderLeftColor = color;
  if (cfg.note) {
    const n = document.createElement("div");
    n.className = "metric-daily-note";
    n.textContent = cfg.note;
    panel.appendChild(n);
  }
}

function metricBubble(val, delta, detail, color, unit, period, range, prevRange, lowerBetter) {
  const b = document.createElement("div");
  b.className = "metric-bubble";
  // "Aug 12 – Aug 18" style label for a {start,end} Pacific day range
  const fmtRange = (r) => {
    if (!r || !r.start || !r.end) return "";
    const f = (iso) => new Date(iso + "T00:00:00")
      .toLocaleDateString("en-US", { month: "short", day: "numeric" });
    return `${f(r.start)} – ${f(r.end)}`;
  };
  if (period) {
    const p = document.createElement("div");
    p.className = "bubble-period";
    p.textContent = period;
    const rl = fmtRange(range);
    if (rl) {
      const s = document.createElement("span");
      s.className = "bubble-range"; s.textContent = rl;
      p.appendChild(s);
    }
    b.appendChild(p);
  }
  const big = document.createElement("div");
  big.className = "bubble-big"; big.textContent = val; big.style.color = color;
  b.appendChild(big);
  if (delta != null && isFinite(delta)) {
    const dl = document.createElement("div");
    // green = moving the right way (for "lower is better" metrics that's down)
    dl.className = "bubble-delta " + ((lowerBetter ? delta <= 0 : delta >= 0) ? "up" : "down");
    dl.textContent = (delta >= 0 ? "▲ +" : "▼ ") + delta.toFixed(1) + unit;
    const pl = fmtRange(prevRange);
    if (pl) {
      const vs = document.createElement("span");
      vs.className = "bubble-vs"; vs.textContent = ` vs ${pl}`;
      dl.appendChild(vs);
    }
    b.appendChild(dl);
  }
  const det = document.createElement("div");
  det.className = "bubble-detail"; det.textContent = detail;
  b.appendChild(det);
  return b;
}

// Interactive 90-day (13-week) trend: smoothed line + area, faint scale grid,
// high/low markers, and a hover tooltip showing the week's value and dates.
// Renders at the container's pixel width (via ResizeObserver) so markers stay
// round and the hover math is exact.
function trendChart(values, weeks, color, isPct, height) {
  const ns = "http://www.w3.org/2000/svg";
  const H = height || 70, padX = 8, padT = 12, padB = 10;
  const fmt = (v) => (isPct === true ? v.toFixed(1) + "%"
    : isPct === "count" ? Math.round(v).toLocaleString()
    : isPct === "num" ? v.toFixed(1)
    : isPct === "time" ? `${Math.floor(v / 60)}:${String(Math.round(v % 60)).padStart(2, "0")}`
    : "$" + Math.round(v).toLocaleString());
  const weekLabel = (i) => {
    if (!weeks[i]) return "";
    const d = new Date(weeks[i] + "T00:00:00"), e = new Date(weeks[i] + "T00:00:00");
    e.setDate(e.getDate() + 6);
    const mo = (x) => x.toLocaleString("en-US", { month: "short", day: "numeric" });
    return `${mo(d)} – ${mo(e)}`;
  };
  const root = document.createElement("div");
  root.className = "trend-chart";
  const tip = document.createElement("div");
  tip.className = "trend-tip"; tip.style.display = "none";
  root.appendChild(tip);

  function render(w) {
    if (!w) return;
    root.querySelectorAll("svg").forEach((s) => s.remove());
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${H}`);
    svg.setAttribute("class", "trend-svg");
    if (height) svg.style.height = H + "px";
    root.insertBefore(svg, tip);
    const pts = values.map((v, i) => ({ i, v })).filter((p) => p.v != null);
    if (pts.length < 2) return;
    const ys = pts.map((p) => p.v);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const span = maxY - minY || 1, lo = minY - span * 0.28, hi = maxY + span * 0.28;
    const n = values.length;
    const X = (i) => padX + (i / (n - 1)) * (w - 2 * padX);
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
    // faint scale grid (min / mid / max) + tiny axis labels
    for (const gv of [maxY, (minY + maxY) / 2, minY]) {
      const ln = document.createElementNS(ns, "line");
      ln.setAttribute("x1", padX); ln.setAttribute("x2", w - padX);
      ln.setAttribute("y1", Y(gv).toFixed(1)); ln.setAttribute("y2", Y(gv).toFixed(1));
      ln.setAttribute("class", "trend-grid"); svg.appendChild(ln);
    }
    for (const [v, dy] of [[maxY, -3], [minY, 9]]) {
      const t = document.createElementNS(ns, "text");
      t.setAttribute("x", padX); t.setAttribute("y", Y(v) + dy);
      t.setAttribute("class", "trend-axis"); t.textContent = fmt(v);
      svg.appendChild(t);
    }
    // smoothed line + area
    const P = pts.map((p) => [X(p.i), Y(p.v)]);
    let d = `M ${P[0][0].toFixed(1)} ${P[0][1].toFixed(1)} `;
    for (let k = 0; k < P.length - 1; k++) {
      const p0 = P[k - 1] || P[k], p1 = P[k], p2 = P[k + 1], p3 = P[k + 2] || P[k + 1];
      const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += `C ${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)} `;
    }
    const gid = "tg" + (_sparkId++);
    const grad = document.createElementNS(ns, "linearGradient");
    grad.setAttribute("id", gid);
    grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0"); grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
    grad.innerHTML = `<stop offset="0" stop-color="${color}" stop-opacity="0.20"/>`
      + `<stop offset="1" stop-color="${color}" stop-opacity="0"/>`;
    svg.appendChild(grad);
    const area = document.createElementNS(ns, "path");
    area.setAttribute("d", d + `L ${P[P.length - 1][0].toFixed(1)} ${H - padB} L ${P[0][0].toFixed(1)} ${H - padB} Z`);
    area.setAttribute("fill", `url(#${gid})`); svg.appendChild(area);
    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", d); line.setAttribute("fill", "none"); line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-linejoin", "round"); line.setAttribute("stroke-linecap", "round");
    svg.appendChild(line);
    // high (solid) & low (hollow) markers
    const hiP = pts.reduce((a, b) => (b.v > a.v ? b : a));
    const loP = pts.reduce((a, b) => (b.v < a.v ? b : a));
    const mkDot = (p, fill, stroke) => {
      const c = document.createElementNS(ns, "circle");
      c.setAttribute("cx", X(p.i)); c.setAttribute("cy", Y(p.v)); c.setAttribute("r", "3.4");
      c.setAttribute("fill", fill); c.setAttribute("stroke", stroke); c.setAttribute("stroke-width", "1.8");
      svg.appendChild(c);
    };
    mkDot(hiP, color, "#fff");
    mkDot(loP, "var(--card)", color);
    // hover: guide line + dot + overlay capture
    const guide = document.createElementNS(ns, "line");
    guide.setAttribute("class", "trend-guide");
    guide.setAttribute("y1", padT); guide.setAttribute("y2", H - padB);
    guide.style.display = "none"; svg.appendChild(guide);
    const hDot = document.createElementNS(ns, "circle");
    hDot.setAttribute("r", "3.6"); hDot.setAttribute("fill", color);
    hDot.setAttribute("stroke", "#fff"); hDot.setAttribute("stroke-width", "1.5");
    hDot.style.display = "none"; svg.appendChild(hDot);
    const ov = document.createElementNS(ns, "rect");
    ov.setAttribute("x", "0"); ov.setAttribute("y", "0");
    ov.setAttribute("width", w); ov.setAttribute("height", H);
    ov.setAttribute("fill", "transparent"); svg.appendChild(ov);
    const showAt = (clientX) => {
      const rect = svg.getBoundingClientRect();
      const mx = ((clientX - rect.left) / rect.width) * w;
      let best = pts[0], bd = Infinity;
      for (const p of pts) { const dd = Math.abs(X(p.i) - mx); if (dd < bd) { bd = dd; best = p; } }
      guide.setAttribute("x1", X(best.i)); guide.setAttribute("x2", X(best.i)); guide.style.display = "";
      hDot.setAttribute("cx", X(best.i)); hDot.setAttribute("cy", Y(best.v)); hDot.style.display = "";
      tip.innerHTML = `<b>${fmt(best.v)}</b><span>${weekLabel(best.i)}</span>`;
      tip.style.display = "block";
      const px = (X(best.i) / w) * rect.width;
      tip.style.left = Math.min(Math.max(px - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth) + "px";
      tip.style.top = Math.max((Y(best.v) / H) * rect.height - tip.offsetHeight - 8, 0) + "px";
    };
    const hide = () => { guide.style.display = "none"; hDot.style.display = "none"; tip.style.display = "none"; };
    ov.addEventListener("mousemove", (ev) => showAt(ev.clientX));
    ov.addEventListener("mouseleave", hide);
    ov.addEventListener("touchstart", (ev) => showAt(ev.touches[0].clientX), { passive: true });
    ov.addEventListener("touchmove", (ev) => { ev.preventDefault(); showAt(ev.touches[0].clientX); }, { passive: false });
    ov.addEventListener("touchend", hide);
  }

  const ro = new ResizeObserver((entries) => render(Math.round(entries[0].contentRect.width)));
  requestAnimationFrame(() => { render(root.clientWidth); ro.observe(root); });
  return root;
}

async function loadHomeYtd() {
  const now = new Date();
  const start = now.getFullYear() + "-01-01";
  const end = now.toISOString().slice(0, 10);
  const d = await (await fetch(`/api/scorecard?start_day=${start}&end_day=${end}&mode=sold`)).json();
  const rows = d.rows || [];
  const money = (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString());
  const pct = (v) => (v == null ? "–" : v.toFixed(1) + "%");
  // year-to-date counters on the title line — the Revenue tab's own totals
  fetch("/api/home_ytd").then((r) => r.json()).then((y) => {
    const hc = $("#home-counters");
    if (!hc || !y || y.revenue == null) return;
    const counter = (label, big, tip, color) => {
      const c = document.createElement("div"); c.className = "home-counter";
      c.style.borderLeftColor = color; c.title = tip;
      c.innerHTML = `<div class="hc-label">${label}</div><div class="hc-big" style="color:${color}">${big}</div>`;
      return c;
    };
    hc.replaceChildren(
      counter(`${y.year} revenue to date`, money(y.revenue),
        "Total contract value sold this year — the same total as the Revenue tab (every inside-sales seller, upsells included, renewals excluded)", "#0d9488"),
      counter(`${y.year} sales to date`, (y.sales || 0).toLocaleString(),
        "How many subscriptions make up that revenue", "#1e3659"));
  }).catch(() => { /* counters stay empty */ });
  const cols = [
    { key: "agent", label: "Agent", fmt: (r) => r.agent, num: false },
    { key: "rev_all", label: "YTD Revenue", fmt: (r) => money(r.rev_all), num: true },
    { key: "acv", label: "YTD ACV", fmt: (r) => money(r.acv), num: true },
    { key: "multi_total_pct", label: "Multiyear", fmt: (r) => pct(r.multi_total_pct), num: true },
    { key: "conversion_pct", label: "Conversion", fmt: (r) => pct(r.conversion_pct), num: true },
  ];
  const st = { key: "rev", asc: false };  // default: revenue high→low
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const t = document.createElement("table");

  function render() {
    const sorted = rows.slice().sort((a, b) => {
      const x = a[st.key], y = b[st.key];
      if (x == null) return 1;
      if (y == null) return -1;
      const c = typeof x === "string" ? String(x).localeCompare(String(y)) : x - y;
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
        else { st.key = col.key; st.asc = !col.num; }  // text asc, numbers high→low first
        render();
      });
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    const tb = document.createElement("tbody");
    for (const r of sorted) {
      const tr = document.createElement("tr");
      tr.innerHTML = cols.map((c) => `<td>${c.fmt(r)}</td>`).join("");
      tb.appendChild(tr);
    }
    t.replaceChildren(thead, tb);
  }
  render();
  wrap.appendChild(t);
  $("#home-ytd").replaceChildren(wrap);
}
