/* Brooks ISA Dashboard — single-page UI, no dependencies. */
"use strict";

/* data-source tags: g = Genesys dispositions (agent-entered),
   fr = FieldRoutes (actual accounts, objective), a = audit team's sheet */
const SRC_LEGEND = [
  ["src-g", "Genesys — what agents dispositioned"],
  ["src-fr", "FieldRoutes — actual accounts (verified)"],
  ["src-a", "Audit team — human-reviewed sheet"],
];

function legendBar() {
  const d = document.createElement("div");
  for (const [cls, text] of SRC_LEGEND) {
    const s = document.createElement("span");
    const dot = document.createElement("span");
    dot.className = "dot " + cls;
    s.append(dot, document.createTextNode(text));
    d.appendChild(s);
  }
  return d;
}

const COLS = [
  { key: "agent",           label: "Agent",  num: false },
  { key: "total_calls",     label: "Calls",  num: true, src: "g", tip: "Genesys: every call the agent handled on the ISA queues" },
  { key: "leads",           label: "Leads",  num: true, src: "g", tip: "Genesys: unique callers the agent dispositioned Closed / Follow Up / Unqualified" },
  { key: "qual_leads",      label: "Qual",   num: true, src: "g", tip: "Genesys: unique callers dispositioned Closed or Follow Up (plus any who converted)" },
  { key: "closed",          label: "Clsd",   num: true, src: "g", tip: "Genesys: calls the agent wrapped as Closed" },
  { key: "closed_pct",      label: "Cl%",    num: true, pct: true, src: "g", tip: "Genesys: share of sales-dispositioned calls wrapped Closed" },
  { key: "followup",        label: "FlUp",   num: true, src: "g", tip: "Genesys: calls wrapped Follow Up" },
  { key: "followup_pct",    label: "FU%",    num: true, pct: true, src: "g", tip: "Genesys: share wrapped Follow Up" },
  { key: "unqualified",     label: "Unq",    num: true, src: "g", tip: "Genesys: calls wrapped Unqualified" },
  { key: "unqualified_pct", label: "Uq%",    num: true, pct: true, src: "g", tip: "Genesys: share wrapped Unqualified" },
  { key: "sheet_conv_pct",  label: "Sheet%", num: true, pct: true, hl: true, src: "a",
    tip: "THE DEFAULT CONVERSION METRIC: audit sheet sales ÷ audit sheet qualified, exactly as the audit team keeps it" },
  { key: "conversions",     label: "Conv",   num: true, hl: true, src: "fr", tip: "FieldRoutes-VERIFIED: unique callers who became a real subscription within 14 days (any seller if pitched; own-name always)" },
  { key: "conv_qual_pct",   label: "Cv/Q%",  num: true, pct: true, hl: true, src: "fr", tip: "Verified conversions ÷ qualified leads (Genesys denominator)" },
  { key: "conv_leads_pct",  label: "Cv/L%",  num: true, pct: true, src: "fr", tip: "Verified conversions ÷ all unique leads incl. unqualified" },
  { key: "conv_calls_pct",  label: "Cv/C%",  num: true, pct: true, src: "fr", tip: "Verified conversions ÷ total calls taken" },
  { key: "honest_pct",      label: "Honest%", num: true, pct: true, hl: true, src: "a",
    tip: "GOLD STANDARD — audit + FieldRoutes: verified conversions ÷ (audit team's qualified + hidden + leaked). Only covers audited days; blank if the audit sheet doesn't cover this period." },
];

const TREND_METRICS = [
  ["sheet_pct", "Sheet conversion % (audit team — default)"],
  ["honest_pct", "Honest conversion % (gold standard)"],
  ["conv_qual_pct", "Conversion % of qualified leads"],
  ["conv_leads_pct", "Conversion % of all leads"],
  ["conv_calls_pct", "Conversion % of total calls"],
  ["closed_pct", "Closed disposition %"],
  ["unqualified_pct", "Unqualified disposition %"],
  ["conversions", "Conversions (count)"],
  ["qual_leads", "Qualified leads (count)"],
  ["leads", "Leads (count)"],
  ["total_calls", "Calls taken (count)"],
];

const PALETTE = ["#1e3659","#e05c4b","#1e8e5a","#8a56c9","#d98b1f","#0f8f8f",
  "#c94f8e","#5b6b7c","#7ba23f","#b0413e","#3b6fd4","#946b2d",
  "#2aa198","#d33682","#6c71c4","#859900","#cb4b16","#268bd2",
  "#a05a2c","#4e79a7","#c2452d","#59a14f"];

let DATA = null;
const state = {
  weekly:  { period: null, sort: "qual_leads", asc: false },
  monthly: { period: null, sort: "qual_leads", asc: false },
  wtd:     { sort: "qual_leads", asc: false },
  trends:  { metric: "conv_qual_pct", gran: "weekly", agents: new Set(["TEAM"]) },
};

const $ = (s) => document.querySelector(s);

// NOTE: init() is invoked at the very BOTTOM of this file — it must run
// after every top-level const has been evaluated.

// Publish the sticky header's live height so table headers can stick just
// below it (the header wraps differently by width and grows when the admin
// Sync button appears).
function syncHeaderHeight() {
  const h = document.querySelector("header");
  if (h) document.documentElement.style.setProperty("--header-h", h.offsetHeight + "px");
}

// Mobile: the tab bar collapses into the ☰ button.
function initNavMenu() {
  const toggle = $("#nav-toggle"), nav = $("#tabs");
  if (!toggle || !nav) return;
  const close = () => { nav.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); };
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  nav.addEventListener("click", (e) => { if (e.target.closest("button")) close(); });
  document.addEventListener("click", (e) => {
    if (!nav.contains(e.target) && e.target !== toggle) close();
  });
}

// Tag the primary data table in each big panel so it gets the frozen
// header-row + first-column treatment; re-tag when a panel re-renders.
function enableStickyGrids() {
  const ids = ["sc-table", "ps-table", "tm-table", "my-table", "cv-table", "mm-table",
               "av-table", "fl-table", "src-table"];
  const mark = (root) => root.querySelectorAll(".table-wrap").forEach((w) => {
    w.classList.add("sticky-grid");
    // Power Score leads with a rank "#" column, so freeze the 2nd column too
    // (the Sales Rep name) and offset it by the rank column's width.
    if (root.id === "ps-table") {
      w.classList.add("freeze-2");
      const c1 = w.querySelector("tbody td:first-child") || w.querySelector("thead th:first-child");
      if (c1) w.style.setProperty("--col1-w", c1.offsetWidth + "px");
    }
  });
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    mark(el);
    new MutationObserver(() => mark(el)).observe(el, { childList: true, subtree: true });
  }
}

/* ---------- access: what this login may see ----------
   The server refuses any data the login's permissions don't cover; this only
   keeps the page tidy by hiding the tabs and sub-tabs that would be empty. */
let ME = { user: null, admin: false, permissions: [], password_login: true, legacy: false };
const can = (k) => ME.permissions.includes(k);
const canAny = (prefix) => ME.permissions.some((k) => k === prefix || k.startsWith(prefix + "."));
const TAB_ACCESS = {
  home: () => can("home"),
  revenue: () => can("revenue"),
  agent: () => canAny("agent"),
  metrics: () => canAny("metrics"),
  managers: () => canAny("managers"),
  payroll: () => canAny("payroll"),
  sources: () => can("sources") || canAny("marketing"),
  settings: () => canAny("settings"),
};

async function loadMe() {
  try {
    const me = await (await fetch("/api/me")).json();
    ME = { ...ME, ...me, permissions: me.permissions || [] };
    ME.legacy = !ME.user;              // signed in with a shared password, not an account
  } catch (e) { /* keep the locked-down default */ }
}

// hide the buttons of a sub-tab strip the login can't use; keep one active
function restrictStrip(sel, attr, allowed) {
  const btns = [...document.querySelectorAll(sel + " button")];
  let firstOk = null;
  for (const b of btns) {
    const ok = !!allowed(b.dataset[attr]);
    b.style.display = ok ? "" : "none";
    if (ok && !firstOk) firstOk = b;
  }
  const active = btns.find((b) => b.classList.contains("active"));
  if (firstOk && (!active || active.style.display === "none")) {
    btns.forEach((b) => b.classList.toggle("active", b === firstOk));
  }
}

function applyAccess() {
  for (const b of document.querySelectorAll("#tabs button[data-tab]")) {
    let show = TAB_ACCESS[b.dataset.tab] ? TAB_ACCESS[b.dataset.tab]() : false;
    // shared-password logins keep the old "unlock with the admin password" Settings button
    if (b.dataset.tab === "settings" && ME.legacy && ME.password_login) show = true;
    b.style.display = show ? "" : "none";
  }
  // the old Marketing unlock button only exists for shared-password logins
  $("#mktg-unlock").style.display =
    ME.legacy && ME.password_login && !can("sources") ? "" : "none";
  restrictStrip("#agent-subtabs", "asub", (k) => can("agent." + k));
  restrictStrip("#mktg-subtabs", "mksub", (k) => (k === "sources" ? can("sources") : can("marketing." + k)));
  restrictStrip("#metrics-subtabs", "msub", (k) =>
    k === "phone" ? canAny("metrics.phone") : can("metrics." + k));
  restrictStrip("#phone-subtabs", "sub", (k) => can("metrics.phone." + k));
  restrictStrip("#payroll-subtabs", "psub", (k) => can("payroll." + k));
  restrictStrip("#managers-subtabs", "gsub", (k) => can("managers." + (k === "adherence" || k === "reports" ? "timesheets" : k === "templates" ? "schedule" : k)));
  restrictStrip("#settings-subtabs", "ssub", (k) => can("settings." + k));
}

function firstAllowedTab() {
  return ["home", "revenue", "agent", "metrics", "payroll", "sources", "settings"]
    .find((t) => TAB_ACCESS[t]());
}

async function init() {
  await loadMe();
  const previewAs = new URLSearchParams(location.search).get("as");
  if ((ME.user && ME.user.kind === "agent") || (previewAs && can("settings.users"))) {
    await initAgentPage(ME.user && ME.user.kind === "agent" ? "" : previewAs);
    return;
  }
  applyAccess();
  // ---- wire ALL controls immediately (no data needed) so the app is
  // interactive even while the first heavy /api/data build is running ----
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.addEventListener("click", () => showTab(b.dataset.tab)));
  document.querySelectorAll("#phone-subtabs button").forEach((b) =>
    b.addEventListener("click", () => showSubTab(b.dataset.sub)));
  document.querySelectorAll("#agent-subtabs button").forEach((b) =>
    b.addEventListener("click", () => showAgentSub(b.dataset.asub)));
  document.querySelectorAll("#metrics-subtabs button").forEach((b) =>
    b.addEventListener("click", () => showMetricsSub(b.dataset.msub)));
  initMarketing();
  initWho();
  initSync();
  loadConfig();
  initTabPickers();
  initNavMenu();
  enableStickyGrids();
  syncHeaderHeight();
  window.addEventListener("resize", syncHeaderHeight);
  for (const id of ["#phone-legend", "#my-legend", "#cv-legend", "#av-legend"]) {
    const el = document.querySelector(id);
    if (el) el.replaceChildren(...legendBar().childNodes);
  }
  $("#coverage").textContent = "loading data…";
  // the tab named in the address bar if this login may open it, else the first allowed one
  const allowedTab = (t) => !!TAB_ACCESS[t] && TAB_ACCESS[t]();
  const landing = Router.start(firstAllowedTab(), allowedTab, showTab);
  if (landing) showTab(landing).catch((e) => console.error(e)).then(() => Router.sync(true));
  else {
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    const box = $("#no-access");
    box.style.display = "block";
    box.replaceChildren(noData("Your account is set up, but no tabs are turned on for it yet. " +
      "Ask Dallin to switch on the ones you need."));
  }

  // ---- data-dependent views ----
  DATA = await (await fetch("/api/data")).json();
  const c = DATA.coverage;
  const updMt = new Date(DATA.generated_at).toLocaleString("en-US", {
    timeZone: "America/Denver", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true,
  });
  $("#coverage").textContent =
    `${c.calls.toLocaleString()} calls · ${c.subscriptions.toLocaleString()} subscriptions · ` +
    `${(c.first_call || "").slice(0, 10)} → ${(c.last_call || "").slice(0, 10)} · ` +
    `updated ${updMt} MT`;
  $("#foot-note").textContent = "";
  if (DATA.monthly) {                // agent rows only arrive with a Phone permission
    setupTrends();
    // opened straight on Phone -> Trends: the chart was waiting for this data
    if ($("#sub-trends").classList.contains("active")) renderTrend();
  }
}

function showSubTab(sub) {
  document.querySelectorAll("#phone-subtabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.sub === sub));
  document.querySelectorAll(".subpanel").forEach((p) =>
    p.classList.toggle("active", p.id === "sub-" + sub));
  if (sub === "trends") renderTrend();
  if (sub === "conv") ensureLoaded("ph");
  if (sub === "audit") ensureLoaded("au");
  if (sub === "leaked") ensureLoaded("lk");
}

/* pickers registry: every tab loads through its range picker */
const PK = {};

function ensureLoaded(key) {
  if (PK[key] && !PK[key].loaded) {
    PK[key].loaded = true;
    PK[key].fire();
  }
}

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

async function showTab(tab) {
  if (!TAB_ACCESS[tab] || !TAB_ACCESS[tab]()) {
    // shared-password logins can still unlock Settings + Sources with the
    // admin password (the page reloads with the wider access)
    if (ME.legacy && ME.password_login && (tab === "settings" || tab === "sources")) adminUnlock();
    return;
  }
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === "panel-" + tab));
  if (tab === "metrics") {
    showMetricsSub(document.querySelector("#metrics-subtabs button.active").dataset.msub);
  }
  if (tab === "agent") {
    showAgentSub(document.querySelector("#agent-subtabs button.active").dataset.asub);
  }
  if (tab === "sources") {
    const strip = $("#mktg-subtabs");
    if (!strip.dataset.bound) {
      strip.dataset.bound = "1";
      strip.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => showMktgSub(b.dataset.mksub)));
    }
    showMktgSub(document.querySelector("#mktg-subtabs button.active").dataset.mksub);
  }
  if (tab === "managers") {
    const strip = $("#managers-subtabs");
    if (!strip.dataset.bound) {
      strip.dataset.bound = "1";
      strip.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => showManagersSub(b.dataset.gsub)));
    }
    showManagersSub(document.querySelector("#managers-subtabs button.active").dataset.gsub);
  }
  if (tab === "payroll") {
    const strip = $("#payroll-subtabs");
    if (!strip.dataset.bound) {
      strip.dataset.bound = "1";
      strip.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => showPayrollSub(b.dataset.psub)));
    }
    showPayrollSub(document.querySelector("#payroll-subtabs button.active").dataset.psub);
  }
  if (tab === "settings") {
    const strip = $("#settings-subtabs");
    if (strip && !strip.dataset.bound) {          // wire the strip once, lazily
      strip.dataset.bound = "1";
      strip.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => showSettingsSub(b.dataset.ssub)));
    }
    showSettingsSub(document.querySelector("#settings-subtabs button.active").dataset.ssub);
  }
  if (tab === "revenue" && !REV.year) loadRevenue();
  if (tab === "home") ensureHome();
}

// Home's data is fetched the first time Home is shown (the page can now open
// on any tab, so that is no longer always at load).
let HOME_LOADED = false;
function ensureHome() {
  if (HOME_LOADED) return;
  HOME_LOADED = true;
  loadHome();
}

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

/* ---------- settings: subtabs + Software Management ---------- */
function showSettingsSub(sub) {
  document.querySelectorAll("#settings-subtabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.ssub === sub));
  document.querySelectorAll(".ssubpanel").forEach((p) =>
    p.classList.toggle("active", p.id === "ssub-" + sub));
  if (sub === "agents") loadSettings();
  if (sub === "users") loadUsers();
  if (sub === "software") loadSoftware();   // fresh check EVERY time it's opened — never polled
}

/* ---------- settings: Users (invite, permissions, disable) ---------- */
const US = { users: [], groups: [], presets: {}, agents: [] };

async function loadUsers() {
  const r = await fetch("/api/users");
  if (r.status === 403 || r.status === 401) {
    $("#us-table").replaceChildren(noData("Admin sign-in required."));
    return;
  }
  Object.assign(US, await r.json());
  const btn = $("#us-invite");
  if (!btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => openUserModal(null));
  }
  renderUsers();
}

function usAllKeys() { return US.groups.flatMap((g) => g.items.map((i) => i.key)); }

function usAccess(u) {
  if (u.owner) return "Owner — everything";
  if (u.kind === "agent") {
    const tabs = (US.agent_tabs || []).filter((t) => u.permissions.includes(t.key)).map((t) => t.label.split(" (")[0]);
    const all = (US.agent_tabs || []).length;
    return "Own page · " + (u.agent || "no agent linked") + (all && tabs.length < all ? ` · ${tabs.length ? tabs.join(", ") : "nothing"} only` : "");
  }
  const all = usAllKeys().length, n = u.permissions.length;
  if (!n) return "Nothing ticked yet";
  if (n === all) return "Everything";
  const groups = US.groups.filter((g) => g.items.some((i) => u.permissions.includes(i.key)))
    .map((g) => g.name);
  return `${n} of ${all} · ${groups.join(", ")}`;
}

function usWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? "—" : d.toLocaleString([], { month: "short", day: "numeric",
    year: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderUsers() {
  const wrap = document.createElement("div"); wrap.className = "table-wrap compact wide";
  const t = document.createElement("table");
  t.innerHTML = "<thead><tr><th>Name</th><th>Email</th><th>Type</th><th>Access</th>"
    + "<th>Status</th><th>Last sign-in</th><th></th></tr></thead>";
  const tb = document.createElement("tbody");
  const cell = (text, cls) => {
    const td = document.createElement("td");
    td.textContent = text; if (cls) td.className = cls; return td;
  };
  for (const u of US.users) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(u.name || "—", u.name ? "" : "dim"));
    tr.appendChild(cell(u.email));
    tr.appendChild(cell(u.owner ? "Owner" : u.kind === "agent" ? "Agent" : "Staff"));
    tr.appendChild(cell(usAccess(u)));
    const st = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = "us-status " + u.status;
    pill.textContent = u.status === "invited" ? "Invited — not signed in yet"
      : u.status[0].toUpperCase() + u.status.slice(1);
    st.appendChild(pill); tr.appendChild(st);
    tr.appendChild(cell(usWhen(u.last_login), u.last_login ? "" : "dim"));
    const td = document.createElement("td");
    const edit = document.createElement("button");
    edit.className = "nav-btn"; edit.textContent = "Edit";
    edit.addEventListener("click", () => openUserModal(u));
    td.appendChild(edit); tr.appendChild(td);
    tb.appendChild(tr);
  }
  t.appendChild(tb); wrap.appendChild(t);
  $("#us-table").replaceChildren(wrap);
}

async function usPost(url, body) {
  const r = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json().catch(() => ({ error: "HTTP " + r.status }));
}

function openUserModal(user) {
  closeModal();
  const isNew = !user;
  const u = user || { email: "", name: "", kind: "staff", agent: "", permissions: [] };
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay"; overlay.id = "agent-modal";
  overlay.innerHTML =
    `<div class="modal-card us-card">
      <div class="modal-head"><b>${isNew ? "Invite a user" : "Edit user"}</b>
        <span class="sub">${isNew
          ? "They get an email with the link. Signing in is their email plus a one-time code."
          : "Changes apply the next time they load a page."}</span></div>
      <div class="us-row2">
        <label class="modal-field">Email<input id="u-email" type="email" autocomplete="off"
          placeholder="name@brookspest.com"></label>
        <label class="modal-field">Name<input id="u-name" type="text" autocomplete="off"
          placeholder="First Last"></label>
        <label class="modal-field">Type
          <select id="u-kind"><option value="staff">Staff — tabs you choose below</option>
          <option value="agent">Agent — their own page only</option></select></label>
        <label class="modal-field" id="u-agent-box">This login belongs to
          <select id="u-agent"><option value="">— pick an agent —</option></select></label>
      </div>
      <div id="u-perms">
        <div class="us-presets"><b>Access</b><span class="modal-spacer"></span>Quick set:
          <span id="u-presets"></span></div>
        <div class="us-groups" id="u-groups"></div>
      </div>
      <p class="us-note" id="u-agent-note">Agents never see the tabs. They get one page with their
        own numbers, Power Score, and commission lookup for their own accounts. Untick a part of it to hide it from this agent.</p>
      <div class="us-groups" id="u-agent-tabs"></div>
      <div class="us-more" id="u-more"></div>
      <div class="modal-actions">
        <span class="modal-spacer"></span>
        <button class="nav-btn" id="u-cancel">Cancel</button>
        <button class="nav-btn primary" id="u-save">${isNew ? "Send invite" : "Save"}</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const q = (s) => overlay.querySelector(s);
  q("#u-email").value = u.email; q("#u-email").disabled = !isNew;
  q("#u-name").value = u.name || "";
  q("#u-kind").value = u.kind;
  for (const a of US.agents) {
    const o = document.createElement("option"); o.value = a; o.textContent = a;
    q("#u-agent").appendChild(o);
  }
  if (u.agent && !US.agents.includes(u.agent)) {
    const o = document.createElement("option"); o.value = u.agent; o.textContent = u.agent;
    q("#u-agent").appendChild(o);
  }
  q("#u-agent").value = u.agent || "";

  // permission checkboxes, grouped by tab; click a group name to tick/untick it all.
  // A user-manager who isn't the owner can only tick or untick what they hold
  // themselves: the rest is shown locked, and the server keeps it as it was.
  const grant = US.grantable ? new Set(US.grantable) : null;
  const locked = (k) => !!u.owner || (grant !== null && !grant.has(k));
  const boxes = {};
  for (const g of US.groups) {
    const card = document.createElement("div"); card.className = "us-group";
    const head = document.createElement("div");
    head.className = "us-group-name"; head.textContent = g.name;
    head.title = "Click to tick or untick this whole group";
    card.appendChild(head);
    for (const it of g.items) {
      const lab = document.createElement("label");
      const cb = document.createElement("input"); cb.type = "checkbox";
      cb.checked = u.permissions.includes(it.key); cb.disabled = locked(it.key);
      if (locked(it.key) && !u.owner) { lab.className = "us-locked"; lab.title = "You don't have this yourself, so you can't give or take it"; }
      boxes[it.key] = cb;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(it.label));
      card.appendChild(lab);
    }
    head.addEventListener("click", () => {
      if (u.owner) return;
      const open = g.items.filter((it) => !locked(it.key));
      const on = !open.every((it) => boxes[it.key].checked);
      open.forEach((it) => { boxes[it.key].checked = on; });
    });
    q("#u-groups").appendChild(card);
  }
  for (const [name, keys] of Object.entries(US.presets)) {
    const b = document.createElement("button");
    b.className = "nav-btn"; b.type = "button"; b.textContent = name; b.disabled = !!u.owner;
    b.addEventListener("click", () => {
      for (const k of Object.keys(boxes)) if (!locked(k)) boxes[k].checked = keys.includes(k);
    });
    q("#u-presets").appendChild(b);
  }
  if (grant !== null && !u.owner) {
    const n = document.createElement("div"); n.className = "us-note";
    n.textContent = "Greyed-out boxes are things you don't have yourself, so you can't hand them out or take them away.";
    q("#u-perms").appendChild(n);
  }
  // agent logins: which parts of their page they see
  const tabBoxes = {};
  const tabCard = document.createElement("div"); tabCard.className = "us-group";
  const tabHead = document.createElement("div"); tabHead.className = "us-group-name"; tabHead.textContent = "Their page";
  tabCard.appendChild(tabHead);
  for (const it of (US.agent_tabs || [])) {
    const lab = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox";
    cb.checked = isNew || u.kind !== "agent" ? true : u.permissions.includes(it.key);
    tabBoxes[it.key] = cb;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(it.label));
    tabCard.appendChild(lab);
  }
  q("#u-agent-tabs").appendChild(tabCard);
  const syncKind = () => {
    const agent = q("#u-kind").value === "agent";
    q("#u-perms").style.display = agent ? "none" : "";
    q("#u-agent-box").style.display = agent ? "" : "none";
    q("#u-agent-note").style.display = agent ? "" : "none";
    q("#u-agent-tabs").style.display = agent ? "" : "none";
  };
  q("#u-kind").addEventListener("change", syncKind);
  q("#u-kind").disabled = !!u.owner;
  syncKind();

  // extra actions on an existing, non-owner user
  if (!isNew) {
    const more = (label, action, confirmMsg, danger) => {
      const b = document.createElement("button");
      b.className = "nav-btn" + (danger ? " modal-remove" : ""); b.type = "button";
      b.textContent = label;
      b.addEventListener("click", async () => {
        if (confirmMsg && !confirm(confirmMsg)) return;
        b.disabled = true;
        const d = await usPost("/api/users/action", { id: u.id, action });
        if (d.ok) { closeModal(); await loadUsers(); toast("✓ " + label + " — done", "ok", 3000); }
        else { b.disabled = false; toast(d.error || "failed", "err", 6000); }
      });
      q("#u-more").appendChild(b);
    };
    if (u.kind === "agent" && u.agent) {
      const pv = document.createElement("a");
      pv.className = "nav-btn"; pv.textContent = "Preview their page";
      pv.href = "/?as=" + encodeURIComponent(u.agent); pv.target = "_blank";
      pv.style.textDecoration = "none";
      q("#u-more").appendChild(pv);
    }
    more("Resend invite email", "resend");
    if (u.sessions) more("Sign out everywhere", "signout");
    if (!u.owner) {
      if (u.status === "disabled") more("Enable", "enable");
      else more("Disable", "disable", `Disable ${u.email}? They are signed out immediately and can't sign back in.`, true);
      more("Remove", "remove", `Remove ${u.email} completely? Use Disable if they might come back.`, true);
    }
  }

  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
  q("#u-cancel").addEventListener("click", closeModal);
  q("#u-save").addEventListener("click", async () => {
    const btn = q("#u-save"); const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Saving…";
    const body = {
      email: q("#u-email").value, name: q("#u-name").value, kind: q("#u-kind").value,
      agent: q("#u-agent").value,
      permissions: Object.keys(boxes).filter((k) => boxes[k].checked),
      agent_tabs: Object.keys(tabBoxes).filter((k) => tabBoxes[k].checked),
    };
    if (!isNew) body.id = u.id;
    const d = await usPost("/api/users/save", body);
    if (!d.ok) {
      btn.disabled = false; btn.textContent = label;
      toast(d.error || "Save failed", "err", 6000);
      return;
    }
    closeModal(); await loadUsers();
    if (d.created && d.emailed === false)
      toast("User added, but the invite email failed: " + (d.email_error || ""), "err", 9000);
    else toast(d.created ? "✓ Invite sent" : "✓ Saved", "ok", 3000);
  });
}

/* ---------- the agent page: Renewal Calculator ----------
   Mirrors the "IS Renewal Calculator" Google sheet (Eligibility tab). The
   rules and the two pricing tables come from the sheet through
   /api/my/renewal; the math below is the sheet's formulas, cell for cell:
     B16 minimum by commission rule = ROUNDUP(existing / (1 - rate), 2)
     B17 minimum by subscription    = the Minimum Pricing cell, blank = "Not offered"
     B19 required minimum           = MAX(B16, B17)
     B20 set by                     = B17 >= B16 ? subscription pricing : commission rule
     B18 recommended start          = MAX(B19, the Recommended Start Prices cell)
     B21 required monthly increase  = B19 - existing */
const RC = { data: null, at: 0, built: false };

function rcRoundUp2(x) {
  // Sheets' ROUNDUP: away from zero; a hair of float noise (70/0.7) isn't a cent
  const y = Math.abs(x) * 100, r = Math.round(y);
  return Math.sign(x) * (Math.abs(y - r) < 1e-7 ? r : Math.ceil(y)) / 100;
}

function rcCell(table, sub, freq) {
  // INDEX/MATCH on a pricing table: number, or null for a blank / missing cell
  if (!table || !table.rows) return null;
  const row = table.rows.find((r) => r.name === sub);
  const c = (table.columns || []).indexOf(freq);
  if (!row || c < 0) return null;
  const v = row.values[c];
  return typeof v === "number" ? v : null;
}

function renewalCalc(p, existingRaw, sub, freq) {
  const rate = p.commission_rate;
  const t = String(existingRaw ?? "").trim();
  const existing = t === "" || !isFinite(Number(t)) ? "" : Number(t);
  const out = { existing };
  out.minRule = existing === "" ? "" : rcRoundUp2(existing / (1 - rate));
  if (!sub || !freq) out.minSub = "";
  else { const v = rcCell(p.minimum, sub, freq); out.minSub = v === null ? "Not offered" : v; }
  out.required = out.minRule === "" || out.minSub === "" ? ""
    : out.minSub === "Not offered" ? "Not offered - change frequency" : Math.max(out.minRule, out.minSub);
  out.setBy = out.minRule === "" || typeof out.minSub !== "number" ? ""
    : out.minSub >= out.minRule ? "Subscription minimum pricing" : `${rcPct(rate)} commission rule`;
  out.start = typeof out.required === "number" ? Math.max(out.required, rcCell(p.start, sub, freq) ?? 0) : "";
  out.increase = typeof out.required === "number" ? out.required - existing : "";
  return out;
}

function rcPct(rate) { return `${Math.round(rate * 1000) / 10}%`; }
function rcUsd(v) {
  return typeof v === "number"
    ? (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : (v || "");
}

async function loadRenewal(asQ) {
  if (RC.data && Date.now() - RC.at < 10 * 60 * 1000) return;
  if (!RC.data) $("#rc-calc").replaceChildren(noData("Loading…"));
  let d = null, r = null;
  try { r = await fetch(`/api/my/renewal?x=1${asQ || ""}`); d = await r.json(); } catch (e) { d = null; }
  if (!d || d.error || !r.ok) {
    if (!RC.data) $("#rc-calc").replaceChildren(noData((d && d.error) || "Couldn't load the renewal pricing right now."));
    return;
  }
  RC.data = d; RC.at = Date.now();
  renderRenewal(d);
}

function renderRenewal(p) {
  // the rules, straight from the sheet
  const rules = $("#rc-rules");
  rules.replaceChildren();
  if (p.rules && p.rules.length) {
    const h = document.createElement("div"); h.className = "rc-rules-head"; h.textContent = "Renewal Eligibility Rules";
    const ul = document.createElement("ul");
    for (const t of p.rules) { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); }
    rules.append(h, ul);
  }
  const src = `From the "${p.sheet || "IS Renewal Calculator"}" Google sheet (${p.tab || "Eligibility"} tab); ` +
    "edits there show up here within 10 minutes.";
  noteInto("#rc-calc", [p.how, src].filter(Boolean).join(" "), true);
  const stale = $("#rc-stale");
  stale.replaceChildren();
  if (p.stale) {
    const b = document.createElement("span"); b.className = "badge warn"; b.textContent = "Saved copy";
    b.appendChild(infoIcon("The pricing sheet couldn't be read just now, so this is the last copy the dashboard read from it."));
    stale.appendChild(b);
  }

  // the calculator: build the inputs once, keep what the agent typed on refresh
  const box = $("#rc-calc");
  if (!RC.built) {
    RC.built = true;
    box.replaceChildren();
    const inputs = document.createElement("div"); inputs.className = "rc-inputs";
    const field = (label, ctl) => {
      const l = document.createElement("label"); l.className = "rc-field";
      const s = document.createElement("span"); s.textContent = label;
      l.append(s, ctl); inputs.appendChild(l); return l;
    };
    const price = document.createElement("input");
    price.type = "number"; price.min = "0"; price.step = "0.01"; price.inputMode = "decimal";
    price.id = "rc-price"; price.placeholder = "0.00";
    const pw = document.createElement("span"); pw.className = "rc-money";
    const dollar = document.createElement("i"); dollar.textContent = "$";
    pw.append(dollar, price);
    field("Existing Monthly Price", pw);
    const sub = document.createElement("select"); sub.id = "rc-sub";
    field("New Subscription", sub);
    const freq = document.createElement("select"); freq.id = "rc-freq";
    field("Frequency", freq);
    const res = document.createElement("div"); res.className = "rc-results"; res.id = "rc-results";
    box.append(inputs, res);
    price.addEventListener("input", renewalUpdate);
    sub.addEventListener("change", () => { rcFreqOptions(); renewalUpdate(); });
    freq.addEventListener("change", renewalUpdate);
  }
  const subSel = $("#rc-sub"), keep = subSel.value;
  subSel.replaceChildren(new Option("Choose a subscription…", ""));
  for (const s of p.subscriptions) subSel.appendChild(new Option(s, s));
  subSel.value = p.subscriptions.includes(keep) ? keep : "";
  rcFreqOptions();
  renewalUpdate();
}

function rcFreqOptions() {
  // every frequency stays pickable (like the sheet); the ones this subscription
  // doesn't offer say so
  const p = RC.data, sel = $("#rc-freq"), keep = sel.value, sub = $("#rc-sub").value;
  sel.replaceChildren(new Option("Choose a frequency…", ""));
  for (const f of p.frequencies) {
    const off = sub && rcCell(p.minimum, sub, f) === null;
    sel.appendChild(new Option(off ? `${f} (not offered)` : f, f));
  }
  sel.value = p.frequencies.includes(keep) ? keep : "";
}

function renewalUpdate() {
  const p = RC.data;
  if (!p) return;
  const sub = $("#rc-sub").value, freq = $("#rc-freq").value;
  const c = renewalCalc(p, $("#rc-price").value, sub, freq);
  const res = $("#rc-results");
  res.replaceChildren();
  const line = (label, val, cls) => {
    const d = document.createElement("div"); d.className = "rc-line" + (cls ? " " + cls : "");
    const a = document.createElement("span"); a.textContent = label;
    const b = document.createElement("b"); b.textContent = val === "" ? "–" : rcUsd(val);
    d.append(a, b); res.appendChild(d); return d;
  };
  // agents see the outputs only (Dallin, 2026-10-19) — the in-between steps
  // (minimum by commission rule / by subscription pricing, set by) stay in renewalCalc
  line("Recommended Start Price", c.start, "rc-start");
  line("Required Minimum New Monthly Price", c.required,
    "rc-required" + (typeof c.required === "string" && c.required ? " rc-bad" : ""));
  line("Required Monthly Increase", c.increase);
  // the two tables, with the picked subscription / frequency lit up
  const table = (t, sel) => {
    if (!t || !t.rows || !t.rows.length) { $(sel).replaceChildren(noData("Not in the sheet.")); return; }
    const tb = document.createElement("table"); tb.className = "rc-table";
    const hr = document.createElement("tr");
    for (const h of ["Subscription", ...t.columns]) {
      const th = document.createElement("th"); th.textContent = h;
      if (h === freq) th.classList.add("rc-on");
      hr.appendChild(th);
    }
    const thead = document.createElement("thead"); thead.appendChild(hr);
    const tbody = document.createElement("tbody");
    for (const r of t.rows) {
      const tr = document.createElement("tr");
      if (r.name === sub) tr.classList.add("rc-on");
      const td0 = document.createElement("td"); td0.textContent = r.name; tr.appendChild(td0);
      r.values.forEach((v, i) => {
        const td = document.createElement("td");
        td.textContent = typeof v === "number" ? rcUsd(v) : "";
        if (r.name === sub && t.columns[i] === freq) td.classList.add("rc-hit");
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    tb.append(thead, tbody);
    $(sel).replaceChildren(tb);
    if (t.note) noteInto(sel, t.note, true);
  };
  table(p.minimum, "#rc-min");
  table(p.start, "#rc-start");
}

/* ---------- agent page: Payroll (own report + own lookup) ----------
   Both feeds work out WHO from the signed-in session, so an agent only ever
   gets their own pay lines and their own accounts. */
const mpMoney = (v) => (v < 0 ? "−$" : "$") + Math.abs(Number(v) || 0)
  .toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const mpDay = (iso, yr = true) => new Date(iso + "T12:00:00").toLocaleDateString("en-US",
  yr ? { month: "short", day: "numeric", year: "numeric" } : { month: "short", day: "numeric" });
const mpStart = (endIso) => {      // a pay period's Sunday, from its Saturday end
  const d = new Date(endIso + "T12:00:00"); d.setDate(d.getDate() - 13);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

async function loadMyPayroll(asQ) {
  const sel = $("#mp-period"), dl = $("#mp-dl"), tiles = $("#mp-tiles");
  noteInto("#mp-tiles", "Your commissions, deductions and payout for one pay period, from the " +
    "locked payroll statement. A pay period shows here once its payroll is final — the day " +
    "after payroll Monday. The CSV lists every line behind these totals.", true);
  noteInto("#mp-results", "Search only finds accounts tied to you: ones you sold, are split " +
    "on, hold an upsell credit on, or have been paid or deducted on. Pay history shows " +
    "finished payrolls only. Commission data syncs every morning.", true);
  const q = $("#mp-q"), go = $("#mp-go");
  if (!q.dataset.bound) {
    q.dataset.bound = "1";
    const run = () => myLookup(q.value, asQ);
    go.addEventListener("click", run);
    q.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  }
  if (sel.dataset.loaded) return;
  tiles.replaceChildren(noData("Loading…"));
  const r = await fetch(`/api/my/payroll/periods?x=1${asQ}`);
  const d = await r.json().catch(() => null);
  if (!r.ok || !d || d.error) {
    tiles.replaceChildren(noData((d && d.error) || "Couldn't load your reports right now."));
    return;
  }
  sel.dataset.loaded = "1";
  const periods = d.periods || [];
  sel.style.display = dl.style.display = periods.length ? "" : "none";
  if (!periods.length) {
    tiles.replaceChildren(noData("Your first commission report will show here once a pay period's payroll is final."));
    return;
  }
  sel.replaceChildren(...periods.map((p, i) => {
    const o = document.createElement("option");
    o.value = p.period_end;
    o.textContent = `${mpDay(p.start, false)} – ${mpDay(p.period_end)}` + (i === 0 ? " (latest)" : "");
    return o;
  }));
  const tile = (label, big, sub, color) => {
    const c = document.createElement("div"); c.className = "home-counter";
    c.style.borderLeftColor = color;
    const l = document.createElement("div"); l.className = "hc-label"; l.textContent = label;
    const b = document.createElement("div"); b.className = "hc-big"; b.style.color = color; b.textContent = big;
    const s = document.createElement("div"); s.className = "hc-sub"; s.textContent = sub;
    c.append(l, b, s);
    return c;
  };
  const show = () => {
    const p = periods.find((x) => x.period_end === sel.value) || periods[0];
    dl.href = `/api/my/payroll/report.csv?period=${p.period_end}${asQ}`;
    $("#mp-sub").textContent = `pay period ${mpDay(p.start, false)} – ${mpDay(p.period_end)}`;
    tiles.replaceChildren(
      tile("Total commission", mpMoney(p.commission), `${p.commission_lines} lines`, "#1d7a3d"),
      tile("Total deductions", mpMoney(p.deductions), `${p.deduction_lines} lines`, "#b3323c"),
      tile("Commission payout", mpMoney(p.payout), "commission minus deductions", "#1e3659"));
  };
  sel.addEventListener("change", show);
  show();
}

async function myLookup(q, asQ) {
  const box = $("#mp-results");
  q = (q || "").trim();
  if (!q) { box.replaceChildren(); return; }
  if (q.length < 2) { box.replaceChildren(noData("Type at least 2 characters.")); return; }
  box.replaceChildren(noData("Looking up…"));
  const r = await fetch(`/api/my/payroll/lookup?q=${encodeURIComponent(q)}${asQ}`);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) { box.replaceChildren(noData(d.error || "Couldn't look that up right now.")); return; }
  if (!(d.results || []).length) {
    box.replaceChildren(noData(`No accounts of yours match “${q}”.`));
    return;
  }
  const stateCls = (s) => ({
    vested: "cl-good", paid: "cl-good", excluded_renewal: "cl-good",
    excluded_moved: "cl-good", excluded_resigned: "cl-good",
    half_kept_fee_paid: "cl-warn", pending: "cl-warn", not_started: "cl-warn",
    clawed_back: "cl-bad", cancelled_unpaid: "cl-dim", excluded_prestart: "cl-dim",
  }[s] || "cl-dim");
  const money = (v) => v == null ? "—" : mpMoney(v);
  box.replaceChildren(...d.results.map((s) => {
    const card = document.createElement("div");
    card.className = "cl-card";
    const rows = (s.history || []).map((h) => `<tr>
        <td>${esc(`${mpStart(h.period_end)} – ${mpDay(h.period_end)}`)}</td>
        <td>${esc(h.label)}</td>
        <td class="num${h.amount < 0 ? " mp-neg" : ""}">${esc(mpMoney(h.amount))}</td>
        <td>${esc(h.text)}</td></tr>`).join("");
    // manual-sheet (before Oct 11, 2026) amounts are never shown, only that it was paid that way
    const manual = s.manual_paid
      ? `<div class="mp-nohist">Paid through the manual payroll sheet before Oct 11, 2026 — those amounts aren't shown here.</div>` : "";
    const hist = rows ? `<table class="mp-hist">
        <thead><tr><th>Pay period</th><th>Line</th><th class="num">Amount</th><th>What happened</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr><td colspan="2">Net from app payroll on this account</td>
          <td class="num${s.paid_to_you < 0 ? " mp-neg" : ""}">${esc(mpMoney(s.paid_to_you))}</td><td></td></tr></tfoot>
      </table>${manual}` : (manual || `<div class="mp-nohist">Nothing on a finished payroll for you on this account yet.</div>`);
    card.innerHTML = `
      <div class="cl-head">
        <b>${esc(s.customer || "?")}</b>
        <span class="cl-ids">acct ${esc(s.customer_id)} · sub ${esc(s.subscription_id)}</span>
        <span class="cl-state ${stateCls(s.state)}">${esc(s.state_label)}</span>
      </div>
      <div class="cl-grid">
        <span><i>You</i>${esc(s.role)}</span>
        <span><i>Plan</i>${esc(s.service_type || "—")}</span>
        <span><i>Contract value</i>${money(s.contract_value)}</span>
        <span><i>Rate / your commission</i>${s.rate != null ? esc(Math.round(s.rate * 100) + "%") : "—"} / ${money(s.your_commission)}</span>
        ${s.upsell_credit != null ? `<span><i>Your upsell credit</i>${money(s.upsell_credit)}</span>` : ""}
        <span><i>Sold</i>${esc(s.date_added ? mpDay(s.date_added) : "—")}</span>
        <span><i>First paid</i>${esc(s.first_paid ? mpDay(s.first_paid) : "not yet")}</span>
        <span><i>Services</i>${esc(s.qualifying ?? 0)} of 3</span>
        <span><i>Coverage</i>${money(s.coverage_paid)} of ${money(s.coverage_target)}</span>
        ${s.cancel_date ? `<span><i>Cancelled</i>${esc(mpDay(s.cancel_date))} (${esc(s.cancel_reason || "no reason")})</span>` : ""}
      </div>
      <div class="cl-why">${esc(s.why || "")}</div>
      ${hist}`;
    return card;
  }));
}

/* ---------- the agent page ("My Numbers") ----------
   What an agent login gets instead of the tabs. Everything here comes from
   the /api/my/* feeds, which only ever return the signed-in agent's own row
   (plus the team average and the Power Score ranking). `previewAs` is set
   when a user-manager opens /?as=Agent Name to see what that agent sees. */
async function initAgentPage(previewAs) {
  const asQ = previewAs ? "&as_agent=" + encodeURIComponent(previewAs) : "";
  for (const sel of ["#tabs", "#nav-toggle", ".sync-box"]) {
    const n = document.querySelector(sel); if (n) n.style.display = "none";
  }
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === "panel-me"));
  $("#coverage").textContent = "";
  initWho();
  await loadConfig();
  const who = previewAs || (ME.user && (ME.user.agent || ME.user.name)) || "";
  $("#me-title").textContent = who ? `${who} — My Numbers` : "My Numbers";
  // sub-tabs: My Numbers (this page) · Schedule · Payroll — only the ones
  // this agent's login is allowed (Settings -> Users); a missing one is
  // simply not there, and its data feeds refuse too
  let tabs = ["me.numbers", "me.schedule", "me.renewal", "me.payroll"];
  try { const t = await (await fetch(`/api/my/tabs?x=1${asQ}`)).json(); if (t && t.tabs) tabs = t.tabs; } catch (e) { /* keep all */ }
  const meStrip = $("#me-subtabs");
  const showMeSub = (b) => {
    meStrip.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".mesubpanel").forEach((p) => p.classList.toggle("active", p.id === "mesub-" + b.dataset.mesub));
    if (b.dataset.mesub === "schedule") { loadMySchedule(asQ, who); loadClock(asQ, !!previewAs); loadMyPto(asQ); loadMyTimeOff(asQ, !!previewAs); }
    if (b.dataset.mesub === "renewal") loadRenewal(asQ);
    if (b.dataset.mesub === "payroll") loadMyPayroll(asQ);
  };
  if (meStrip && !meStrip.dataset.bound) {
    meStrip.dataset.bound = "1";
    meStrip.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => showMeSub(b)));
  }
  restrictStrip("#me-subtabs", "mesub", (k) => tabs.includes("me." + k));
  Router.startAgentPage();              // open the sub-tab named in the address, if allowed
  const firstSub = meStrip.querySelector("button.active");
  if (!tabs.length) {
    $("#panel-me").querySelectorAll(".mesubpanel").forEach((p) => p.classList.remove("active"));
    $("#me-banner").replaceChildren(noData("Your login doesn't have any pages turned on yet — ask a manager."));
  } else if (firstSub && firstSub.dataset.mesub !== "numbers") { showMeSub(firstSub); }
  Router.sync(true);
  const canNumbers = tabs.includes("me.numbers");
  if (previewAs) {
    const b = document.createElement("div"); b.className = "me-banner";
    b.append(`Preview: this is exactly what ${previewAs} sees when they sign in. `);
    const back = document.createElement("a"); back.href = "/"; back.textContent = "Back to the dashboard";
    b.append(back);
    $("#me-banner").replaceChildren(b);
  }
  // year so far: revenue, sales, and their best day / week / month
  if (canNumbers) fetch(`/api/my/ytd?x=1${asQ}`).then((r) => r.json()).then((y) => {
    if (!y || y.error) return;
    const usd = (v) => "$" + Math.round(v || 0).toLocaleString();
    const day = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
    const tile = (label, big, sub, color) => {
      const c = document.createElement("div"); c.className = "home-counter";
      c.style.borderLeftColor = color;
      c.innerHTML = `<div class="hc-label">${label}</div><div class="hc-big" style="color:${color}">${big}</div>` +
        `<div class="hc-sub">${sub}</div>`;
      return c;
    };
    const best = (b, sub) => (b ? [usd(b.amount), sub(b)] : ["–", "none yet"]);
    const [bd, bds] = best(y.best_day, (b) => day(b.date));
    const [bw, bws] = best(y.best_week, (b) => b.label);
    const [bm, bms] = best(y.best_month, (b) => b.label);
    $("#me-counters").replaceChildren(
      tile(`${y.year} revenue to date`, usd(y.revenue), "contract value you've sold", "#0d9488"),
      tile(`${y.year} sales to date`, (y.sales || 0).toLocaleString(), "subscriptions sold", "#1e3659"),
      tile("Best day", bd, bds, "#7048ba"),
      tile("Best week", bw, bws, "#d98b1f"),
      tile("Best month", bm, bms, "#c0396f"));
  }).catch(() => { /* counters stay empty */ });

  const fail = (sel, r, d) => $(sel).replaceChildren(noData(
    (d && d.error) || (r.status === 403 ? "This login isn't linked to an agent yet — ask Dallin."
                                         : "Couldn't load this right now.")));

  // --- Power Score: current or last pay period only, serviced between ---
  const fmtD = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US",
    { month: "short", day: "numeric" });
  let psToken = 0;
  async function loadPower(period) {
    const token = ++psToken;
    $("#me-ps-table").replaceChildren(noData("Loading…"));
    $("#me-ps-scales").replaceChildren();
    const r = await fetch(`/api/my/power?period=${period}${asQ}`);
    const d = await r.json().catch(() => null);
    if (token !== psToken) return null;
    if (!r.ok || !d || !d.rows) { fail("#me-ps-table", r, d); return null; }
    $("#me-ps-sub").textContent = period === "current"
      ? `${fmtD(d.start)} – ${fmtD(d.period_end)} · projected: counts appointments still booked this period · where you'd rank if they all get serviced`
      : `${fmtD(d.start)} – ${fmtD(d.period_end)} · final`;
    if (!d.rows.length) { $("#me-ps-table").replaceChildren(noData("No Power Score data for this period yet.")); return d; }
    renderPowerScore(d, "serviced", "#me-ps-table", "#me-ps-scales", d.agent);
    if (!d.rows.some((x) => x.agent === d.agent)) {
      $("#me-ps-table").prepend(noData("You aren't part of the Power Score ranking — here is the team's."));
    }
    return d;
  }
  const strip = $("#me-ps-period");
  const mkP = (val, label) => {
    const b = document.createElement("button");
    b.textContent = label; b.className = val === "current" ? "on" : "";
    b.addEventListener("click", () => {
      strip.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      loadPower(val);
    });
    return b;
  };
  strip.replaceChildren(mkP("current", "Current pay period"), mkP("last", "Last pay period"));

  // --- my metrics vs the team: any dates, sold or serviced between ---
  let scToken = 0;
  const meMode = makeModeToggle($("#me-mode"), () => picker.fire(), "serviced");
  const picker = makeRangePicker($("#me-picker"), async (a, b) => {
    const token = ++scToken;
    $("#me-sc-table").replaceChildren(noData("Loading…"));
    const mode = meMode.get();
    const r = await fetch(`/api/my/scorecard?start_day=${a}&end_day=${b}&mode=${mode}${asQ}`);
    const d = await r.json().catch(() => null);
    if (token !== scToken) return;
    if (!r.ok || !d || !d.rows) { fail("#me-sc-table", r, d); $("#me-sc-tiers").replaceChildren(); return; }
    if (!d.rows.length) {
      $("#me-sc-table").replaceChildren(noData("No sales metrics are tracked for you on the IS #'s sheet."));
      $("#me-sc-tiers").replaceChildren();
      return;
    }
    d.teamRow = d.team;
    if (d.updated) {
      const when = new Date(d.updated).toLocaleString("en-US", { timeZone: "America/Denver",
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      $("#me-updated").textContent = `Data last updated ${when} MT`;
    }
    renderScorecard(d, mode, "#me-sc-table", "#me-sc-tiers");
    fetch(`/api/my/calls?start_day=${a}&end_day=${b}${asQ}`).then((r) => r.json()).then((c) => {
      if (token !== scToken || !c || c.error) return;
      for (const k of SC_CALL_KEYS) {
        d.rows[0][k] = (c.me || {})[k] ?? null;
        if (d.teamRow) d.teamRow[k] = (c.team || {})[k] ?? null;
      }
      renderScorecard(d, mode, "#me-sc-table", "#me-sc-tiers");
    }).catch(() => { /* leave the Genesys columns blank */ });
    fetch(`/api/my/mm?start_day=${a}&end_day=${b}${asQ}`).then((r) => r.json()).then((m) => {
      if (token !== scToken || !m || m.error) return;
      d.rows[0].mm = m.me ?? null;
      if (d.teamRow) d.teamRow.mm = m.team ?? null;
      renderScorecard(d, mode, "#me-sc-table", "#me-sc-tiers");
    }).catch(() => { /* leave MM blank */ });
    // VoiceOps quality fills its column in afterwards (it can take a while the
    // first time a date range is asked for)
    try {
      const q = await (await fetch(`/api/my/quality?start_day=${a}&end_day=${b}${asQ}`)).json();
      if (token !== scToken || !q) return;
      d.rows[0].quality_score = q.me ?? null;
      if (d.teamRow) d.teamRow.quality_score = q.team ?? null;
      renderScorecard(d, mode, "#me-sc-table", "#me-sc-tiers");
    } catch (e) { /* leave the column blank */ }
  }, "pay_this", { fullPayPeriod: true });

  // both sections start on the current pay period and load independently
  // (the Power Score waits on VoiceOps, which can be slow the first time)
  if (canNumbers) { picker.fire(); loadPower("current"); }
}

/* ---------- Payroll tab: lookup, rulebook, roster ---------- */
function showPayrollSub(sub) {
  document.querySelectorAll("#payroll-subtabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.psub === sub));
  document.querySelectorAll(".psubpanel").forEach((p) =>
    p.classList.toggle("active", p.id === "psub-" + sub));
  if (sub === "lookup") initLookup();
  if (sub === "adjust") initAdjust();
  if (sub === "rules") loadCommissionRules();
  if (sub === "roster") loadCommissions();
  if (sub === "downloads") loadDownloads();
}

/* ---------- Payroll -> Submit Adjustment ---------- */
const AJ = { meta: null, subs: [] };

// field spec per adjustment type: [key, label, kind]
const AJ_FIELDS = {
  split: [["agent1", "ISA rep 1", "inbound"],
          ["agent2", "ISA rep 2", "inbound"]],
  outbound: [["agent1", "Inbound ISA rep", "inbound"],
             ["agent2", "Outbound agent", "outbound"]],
  rescue: [["agent1", "ISA rep (gets 50%)", "inbound"]],
  upsell: [["agent1", "ISA rep", "inbound"],
           ["acv", "ACV change $", "number"]],
  tech_upsell: [["agent1", "Sales rep (paid 9% of the ACV change, 5% if commercial)", "inbound"],
                ["tech", "Technician name", "text"],
                ["branch", "Branch", "branch"],
                ["acv", "ACV change $", "number"]],
};

async function initAdjust() {
  if (!AJ.meta) {
    const r = await fetch("/api/commissions/intake/meta");
    if (r.status === 403 || r.status === 401) {
      $("#aj-account").replaceChildren(noData("You don't have access to submit adjustments."));
      return;
    }
    AJ.meta = await r.json().catch(() => null);
    if (!AJ.meta) return;
  }
  $("#aj-asof").textContent = AJ.meta.data_as_of
    ? "verifies against engine data synced " + AJ.meta.data_as_of : "";
  const typeSel = $("#aj-type");
  if (!typeSel.dataset.bound) {
    typeSel.dataset.bound = "1";
    typeSel.addEventListener("change", renderAjFields);
    $("#aj-check").addEventListener("click", ajCheckAccount);
    $("#aj-acct").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); ajCheckAccount(); }
    });
    $("#aj-acct").addEventListener("change", ajCheckAccount);
    $("#aj-submit").addEventListener("click", ajSubmit);
  }
  renderAjFields();
  loadAjRecent();
}

function ajSelect(id, options, placeholder) {
  const sel = document.createElement("select");
  sel.id = id;
  const ph = document.createElement("option");
  ph.value = ""; ph.textContent = placeholder || "— pick —";
  sel.appendChild(ph);
  for (const o of options) {
    const op = document.createElement("option");
    op.value = o; op.textContent = o;
    sel.appendChild(op);
  }
  return sel;
}

function renderAjFields() {
  const typ = $("#aj-type").value;
  const box = $("#aj-fields");
  box.replaceChildren(...AJ_FIELDS[typ].map(([key, label, kind]) => {
    const wrap = document.createElement("div");
    wrap.className = "aj-field";
    const lab = document.createElement("label");
    lab.textContent = label; lab.htmlFor = "aj-f-" + key;
    wrap.appendChild(lab);
    let el;
    if (kind === "inbound") el = ajSelect("aj-f-" + key, AJ.meta.inbound, "— pick the rep —");
    else if (kind === "outbound") el = ajSelect("aj-f-" + key, AJ.meta.outbound, "— pick the agent —");
    else if (kind === "branch") el = ajSelect("aj-f-" + key, AJ.meta.branches, "— branch —");
    else {
      el = document.createElement("input");
      el.id = "aj-f-" + key;
      el.autocomplete = "off";
      if (kind === "number") { el.type = "number"; el.min = "0"; el.step = "0.01"; }
    }
    wrap.appendChild(el);
    return wrap;
  }));
  $("#aj-result").replaceChildren();
}

async function ajCheckAccount() {
  const acct = $("#aj-acct").value.trim();
  const out = $("#aj-account");
  if (!acct) return;
  out.innerHTML = '<p class="sub">checking…</p>';
  const r = await fetch("/api/commissions/intake/account?acct=" + encodeURIComponent(acct));
  const d = await r.json().catch(() => ({ error: "check failed" }));
  if (d.error) { out.innerHTML = `<p class="aj-bad">${esc(d.error)}</p>`; return; }
  AJ.subs = d.subs || [];
  const subSel = $("#aj-sub");
  subSel.replaceChildren(new Option("auto (newest match)", ""));
  for (const s of AJ.subs) {
    subSel.appendChild(new Option(
      `${s.sub_id} — ${s.service_type || "?"} $${Math.round(s.cv || 0)} ` +
      `(${s.active ? "active" : "cancelled"}, sold ${s.sold}${s.rep ? " by " + s.rep : ""})`,
      s.sub_id));
  }
  if (!d.customer && !AJ.subs.length) {
    out.innerHTML = `<p class="aj-hold">Account ${esc(acct)} isn't in the engine database —
      not ISA-sold or newer than the last sync. An <b>upsell</b> submission will import it
      live from FieldRoutes; anything else holds until the next sync.</p>`;
    return;
  }
  const c = d.customer || {};
  let html = `<div class="aj-cust"><b>${esc(c.name || "(name unknown)")}</b>
    <span class="sub">acct ${esc(acct)}${c.branch ? " · " + esc(c.branch) : ""}
    ${c.status ? " · status " + esc(c.status) : ""} · card on file: ${c.card ? "yes" : "NO"}</span></div>`;
  html += AJ.subs.map((s) => `
    <div class="aj-sub ${s.active ? "" : "aj-sub-dead"}">
      <b>${esc(s.service_type || "?")}</b> $${Math.round(s.cv || 0)}
      <span class="sub">#${esc(s.sub_id)} · ${s.active ? "active" : "cancelled"} ·
      sold ${esc(s.sold || "?")}${s.rep ? " by " + esc(s.rep) : " (no ISA rep)"} ·
      state ${esc(s.state || "—")}</span>
      ${s.d2d ? `<div class="aj-flag">D2D-sold: ${esc(s.d2d)} — D2D rules apply automatically</div>` : ""}
      ${s.assignments.length ? `<div class="aj-flag">already assigned: ${esc(s.assignments.join("; "))}</div>` : ""}
      ${s.upsells ? `<div class="aj-flag">${s.upsells} upsell credit(s) already on this sub</div>` : ""}
    </div>`).join("");
  // branch auto-fill for tech referrals
  const br = $("#aj-f-branch");
  if (br && c.branch && !br.value) br.value = c.branch;
  out.innerHTML = html;
}

async function ajSubmit() {
  const typ = $("#aj-type").value;
  const body = { type: typ, account: $("#aj-acct").value.trim(),
                 sub_id: $("#aj-sub").value, note: $("#aj-note").value.trim() };
  for (const [key] of AJ_FIELDS[typ]) {
    const el = $("#aj-f-" + key);
    if (el) body[key] = el.value.trim();
  }
  const res = $("#aj-result");
  const btn = $("#aj-submit");
  btn.disabled = true;
  res.innerHTML = '<p class="sub">submitting…</p>';
  try {
    const r = await fetch("/api/commissions/intake", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!d.ok) {
      res.innerHTML = `<p class="aj-bad">${esc(d.error || "submit failed")}</p>`;
      return;
    }
    res.innerHTML = '<p class="sub">queued — verifying against PestRoutes data…</p>';
    let row = null;
    for (let i = 0; i < 10; i++) {
      await new Promise((ok) => setTimeout(ok, 1300));
      const lr = await fetch("/api/commissions/intake/list?limit=10");
      const ld = await lr.json().catch(() => ({ rows: [] }));
      row = (ld.rows || []).find((x) => x.id === d.id);
      if (row && row.status !== "NEW") break;
    }
    if (!row || row.status === "NEW") {
      res.innerHTML = `<p class="aj-hold">Queued as #${d.id}. The processor hasn't picked
        it up yet — it will book on the next pipeline run.</p>`;
    } else {
      const cls = row.status.startsWith("BOOKED") ? "aj-ok"
        : row.status === "ALREADY BOOKED" ? "aj-hold" : "aj-hold";
      res.innerHTML = `<p class="${cls}"><b>${esc(row.status)}</b> — ${esc(row.detail || "")}</p>`;
      if (row.status.startsWith("BOOKED")) {
        $("#aj-note").value = ""; $("#aj-acct").value = "";
        $("#aj-sub").replaceChildren(new Option("auto (newest match)", ""));
        $("#aj-account").innerHTML = "";
        renderAjFields();
        toast("✓ Booked", "ok", 2500);
      }
    }
    loadAjRecent();
  } finally {
    btn.disabled = false;
  }
}

async function loadAjRecent() {
  const r = await fetch("/api/commissions/intake/list?limit=25");
  const d = await r.json().catch(() => ({ rows: [] }));
  const box = $("#aj-recent");
  if (!(d.rows || []).length) {
    box.replaceChildren(noData("No dashboard submissions yet."));
    return;
  }
  const label = { split: "Split", outbound: "Outbound", rescue: "Rescue",
                  upsell: "Upsell", tech_upsell: "Tech referral" };
  box.innerHTML = `<table class="aj-table"><thead><tr>
    <th>When</th><th>By</th><th>Type</th><th>Account</th><th>Agents</th>
    <th>Est. $</th><th>Status</th><th>Detail</th><th></th></tr></thead><tbody>` +
    d.rows.map((x) => {
      const deleted = x.status === "DELETED";
      const cls = (x.status || "").startsWith("BOOKED") ? "aj-ok"
        : x.status === "HELD" ? "aj-hold" : "";
      const agents = [x.agent1, x.agent2, x.tech].filter(Boolean).join(" / ");
      return `<tr${deleted ? ' class="aj-deleted"' : ""}>
        <td>${esc((x.submitted_at || "").slice(0, 16))}</td>
        <td>${esc(x.submitted_by || "")}</td>
        <td>${esc(label[x.type] || x.type)}</td>
        <td>${esc(x.account)}</td><td>${esc(agents)}</td>
        <td>${x.est_amount != null ? "$" + x.est_amount.toFixed(2) : "—"}</td>
        <td class="${cls}">${esc(x.status || "")}</td>
        <td class="aj-detail">${esc(x.detail || "")}</td>
        <td>${deleted ? "" : `<button class="nav-btn" data-del="${x.id}">Delete</button>`}</td></tr>`;
    }).join("") + "</tbody></table>";
  for (const b of box.querySelectorAll("[data-del]")) {
    const x = d.rows.find((r) => String(r.id) === b.dataset.del);
    b.addEventListener("click", () => deleteAdjustment(x, label[x.type] || x.type, b));
  }
}

async function deleteAdjustment(x, what, btn) {
  const booked = (x.status || "").startsWith("BOOKED");
  const msg = booked
    ? `Delete this ${what} on account ${x.account}?\n\nWhat it booked is removed too. ` +
      "If it was already paid, the next payroll takes it back."
    : `Delete this ${what} submission for account ${x.account}?\n\n` +
      "Nothing was booked from it, so no money moves.";
  if (!confirm(msg)) return;
  btn.disabled = true;
  try {
    const r = await fetch("/api/commissions/intake/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: x.id }) });
    const d = await r.json().catch(() => ({}));
    if (d.error || !d.ok) toast(d.error || "Delete failed", "err", 6000);
    else toast(d.message || "Deleted", "ok", 7000);
  } finally {
    loadAjRecent();
  }
}

/* ---------- Payroll -> Downloads ---------- */
async function loadDownloads() {
  const r = await fetch("/api/commissions/reports");
  const d = await r.json().catch(() => ({}));
  if (d.error) { $("#dl-asof").textContent = d.error; return; }
  $("#dl-asof").textContent = d.data_as_of ? "engine data synced " + d.data_as_of : "";
  const psel = $("#dl-period");
  // periods before go-live were paid from the manual sheet: parallel runs
  const pre = new Set(d.pre_go_live || []);
  const opt = (p) => {
    const o = document.createElement("option");
    o.value = p;
    const [y, m, day] = p.split("-").map(Number);
    const end = new Date(y, m - 1, day), start = new Date(end);
    start.setDate(end.getDate() - 13);
    o.textContent = `${start.getMonth() + 1}/${start.getDate()} – ${m}/${day}/${y}` +
      (pre.has(p) ? " · PRE-GO-LIVE" : "");
    if (pre.has(p)) o.dataset.pre = "1";
    return o;
  };
  const group = (label, ps) => {
    const g = document.createElement("optgroup");
    g.label = label;
    g.append(...ps.map(opt));
    return g;
  };
  const live = (d.periods || []).filter((p) => !pre.has(p));
  const old = (d.periods || []).filter((p) => pre.has(p));
  const [ly, lm, ld] = (d.live_from || "").split("-").map(Number);
  psel.replaceChildren(...[
    live.length && group("Live — basis of pay", live),
    old.length && group(`Pre-go-live (before ${lm}/${ld}/${ly}) — parallel run, ` +
                        "not the basis of pay", old),
  ].filter(Boolean));
  const preNote = () => {
    $("#dl-period-note").hidden = !(psel.selectedOptions[0] || {}).dataset?.pre;
  };
  preNote();
  if (!psel.dataset.bound) { psel.dataset.bound = "1"; psel.addEventListener("change", preNote); }
  if (!(d.periods || []).length) {
    const o = document.createElement("option");
    o.textContent = "no payroll runs yet"; o.disabled = true;
    psel.replaceChildren(o);
  }
  const ysel = $("#dl-year");
  ysel.replaceChildren(...(d.d2d_years || []).map((y) => {
    const o = document.createElement("option");
    o.value = y; o.textContent = y;
    return o;
  }));
  const st = d.states || {};
  const pending = (st.pending || 0) + (st.not_started || 0);
  $("#dl-db-stats").textContent =
    `${pending.toLocaleString()} pending · ${(st.paid || 0).toLocaleString()} paid-unvested · ` +
    `${(st.vested || 0).toLocaleString()} vested`;
  const bind = (btn, fn) => {
    const b = $(btn);
    if (!b.dataset.bound) { b.dataset.bound = "1"; b.addEventListener("click", fn); }
  };
  bind("#dl-period-go", () => {
    if (psel.value) window.location = "/api/commissions/report.xlsx?period=" + psel.value;
  });
  bind("#dl-db-go", () => { window.location = "/api/commissions/database.xlsx"; });
  bind("#dl-year-go", () => {
    if (ysel.value) window.location = "/api/commissions/d2d.xlsx?year=" + ysel.value;
  });
  if (can("payroll.run")) loadRunCard();
}

/* ---------- the Lock & Run Payroll button ---------- */
let runPollTimer = null;

function fmtMD(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return `${m}/${d}`;
}

async function loadRunCard() {
  const card = $("#dl-run-card");
  const r = await fetch("/api/commissions/runpayroll/status");
  if (r.status === 403 || r.status === 401) return;
  const d = await r.json().catch(() => null);
  if (!d) return;
  card.style.display = "";
  const btn = $("#run-go"), state = $("#run-state");
  $("#run-period").textContent = `pay period ${fmtMD(d.period_start)} – ${fmtMD(d.period_end)}`;
  $("#run-pre").hidden = !d.pre_go_live;
  const run = d.run || {};
  clearTimeout(runPollTimer);
  btn.classList.remove("run-again");
  if (run.state === "running" && run.period_end === d.period_end) {
    btn.disabled = true;
    btn.textContent = "Running…";
    const STEP = { sync: "syncing PestRoutes (the long part)",
                   adjustments: "booking adjustments",
                   statement: "building the statement" };
    state.textContent = (STEP[run.step] || run.step) + ` · started ${run.started || ""}`;
    runPollTimer = setTimeout(loadRunCard, 7000);
  } else if (d.pre_go_live) {
    // paid from the manual sheet — never (re)run from here (server refuses too)
    btn.disabled = true;
    btn.textContent = "Pre-go-live — locked";
    const [ly, lm, ld] = d.live_from.split("-").map(Number);
    const lastDay = new Date(ly, lm - 1, ld + 13), unlock = new Date(ly, lm - 1, ld + 14);
    state.textContent = "paid from the manual sheet" +
      (d.statement_exists ? ` (parallel run ${d.ran_at})` : "") +
      ` — first live payroll ${lm}/${ld} – ${lastDay.getMonth() + 1}/${lastDay.getDate()}` +
      ` unlocks Sun ${unlock.getMonth() + 1}/${unlock.getDate()}`;
  } else if (run.state === "failed" && run.period_end === d.period_end && !d.statement_exists) {
    btn.disabled = false;
    btn.textContent = `Retry payroll ${fmtMD(d.period_end)}`;
    state.textContent = "last run failed — " + (run.error || "see payroll_run.log");
  } else if (!d.statement_exists) {
    btn.disabled = false;
    btn.textContent = `Run payroll ${fmtMD(d.period_start)} – ${fmtMD(d.period_end)}`;
    state.textContent = "period closed — ready to run";
  } else if (d.paid_locked) {
    // payroll Monday has passed: it was paid; later changes land next period
    btn.disabled = true;
    btn.textContent = `Paid ${fmtMD(d.payroll_monday)} — locked`;
    const [ny, nm, nd] = d.next_period_end.split("-").map(Number);
    const unlock = new Date(ny, nm - 1, nd + 1);
    state.textContent = `ran ${d.ran_at}` + (run.totals ? ` · ${run.totals}` : "") +
      ` — later changes land in the next payroll (unlocks Sun ${unlock.getMonth() + 1}/${unlock.getDate()})`;
  } else {
    // already run: grey until the next period closes, allow a re-run
    btn.disabled = false;
    btn.textContent = `Re-run ${fmtMD(d.period_start)} – ${fmtMD(d.period_end)}`;
    btn.classList.add("run-again");
    const [ny, nm, nd] = d.next_period_end.split("-").map(Number);
    const unlock = new Date(ny, nm - 1, nd + 1);   // the Sunday after the Saturday close
    state.textContent = `ran ${d.ran_at}` +
      (run.totals ? ` · ${run.totals}` : "") +
      ` — next payroll unlocks Sun ${unlock.getMonth() + 1}/${unlock.getDate()}`;
  }
  if (!btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", async () => {
      const again = btn.classList.contains("run-again");
      if (!confirm(
        (again ? "Re-run" : "Run") + ` payroll for ${$("#run-period").textContent.replace("pay period ", "")}?\n\n` +
        "This locks in every pending adjustment, pulls fresh PestRoutes data, " +
        "and rebuilds the statement. Takes 30–45 minutes." +
        (again ? "\n\nRe-running REPLACES this period's statement. Don't re-run once " +
                 "payroll has been entered from it — later changes belong in the next payroll."
               : ""))) return;
      btn.disabled = true;
      const resp = await fetch("/api/commissions/runpayroll", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}) });
      const res = await resp.json().catch(() => ({}));
      if (!res.ok) { toast(res.error || "could not start", "bad", 5000); }
      else toast("Payroll run started", "ok", 3000);
      setTimeout(loadRunCard, 1200);
    });
  }
}

const CR = { roster: [], sellers: [] };

async function loadCommissions() {
  const r = await fetch("/api/commissions/roster");
  if (r.status === 403 || r.status === 401) {
    $("#cr-table").replaceChildren(noData("You don't have access to the roster."));
    return;
  }
  const d = await r.json();
  CR.roster = d.roster || [];
  CR.sellers = d.sellers || [];
  bindCrSearch();
  renderCrTable();
}

function bindCrSearch() {
  const input = $("#cr-search"), results = $("#cr-search-results");
  if (input.dataset.bound) return;
  input.dataset.bound = "1";
  const render = () => {
    const q = input.value.trim().toLowerCase();
    const matches = CR.sellers
      .filter((s) => !q || s.name.toLowerCase().includes(q)).slice(0, 14);
    if (!matches.length) {
      results.innerHTML = `<div class="set-result-empty">${q ? "no matching sellers" : ""}</div>`;
      results.style.display = q ? "block" : "none";
      return;
    }
    results.replaceChildren(...matches.map((s) => {
      const row = document.createElement("div");
      row.className = "set-result cr-result";
      row.innerHTML = `<span>${s.name}</span><span class="cr-result-btns"></span>`;
      const btns = row.querySelector(".cr-result-btns");
      for (const role of ["inbound", "outbound"]) {
        const b = document.createElement("button");
        b.className = "nav-btn";
        b.textContent = "+ " + role;
        b.addEventListener("click", async () => {
          results.style.display = "none"; input.value = "";
          await saveCrEntry({ name: s.name, role });
        });
        btns.appendChild(b);
      }
      return row;
    }));
    results.style.display = "block";
  };
  input.addEventListener("input", render);
  input.addEventListener("focus", render);
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#psub-roster .set-search-box")) results.style.display = "none";
  });
}

async function saveCrEntry(body) {
  const r = await fetch("/api/commissions/roster", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (d.ok) { toast("✓ Saved", "ok", 2000); await loadCommissions(); }
  else toast(d.error || "save failed", "bad", 4000);
}

function renderCrTable() {
  const wrap = document.createElement("div"); wrap.className = "table-wrap compact narrow";
  const t = document.createElement("table");
  t.innerHTML = "<thead><tr><th>Agent</th><th>Role</th>" +
    "<th title='Inside-sales start date. Sales under this agent&#39;s PestRoutes logins dated earlier (e.g. retention work) are not commissioned.'>ISA start</th>" +
    "<th>Added</th><th></th></tr></thead>";
  const tb = document.createElement("tbody");
  const order = { inbound: 0, outbound: 1 };
  for (const a of CR.roster.slice().sort((x, y) =>
      (order[x.role] - order[y.role]) || x.name.localeCompare(y.name))) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${a.name}</td>`;
    const tdRole = document.createElement("td");
    const sel = document.createElement("select");
    sel.className = "cr-role";
    for (const role of ["inbound", "outbound"]) {
      const o = document.createElement("option");
      o.value = role; o.textContent = role[0].toUpperCase() + role.slice(1);
      if (role === a.role) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => saveCrEntry({ name: a.name, role: sel.value }));
    tdRole.appendChild(sel); tr.appendChild(tdRole);
    const tdStart = document.createElement("td");
    const sd = document.createElement("input");
    sd.type = "date"; sd.className = "cr-start"; sd.value = a.start_date || "";
    sd.title = "Leave blank if every sale under this agent counts";
    sd.addEventListener("change", () => saveCrEntry({ name: a.name, start_date: sd.value }));
    tdStart.appendChild(sd); tr.appendChild(tdStart);
    tr.insertAdjacentHTML("beforeend", `<td class="dim">${a.added || ""}</td>`);
    const td = document.createElement("td");
    const rm = document.createElement("button");
    rm.className = "nav-btn"; rm.textContent = "Remove";
    rm.addEventListener("click", () => {
      if (confirm(`Remove ${a.name} from the commission roster?`))
        saveCrEntry({ name: a.name, remove: true });
    });
    td.appendChild(rm); tr.appendChild(td);
    tb.appendChild(tr);
  }
  t.appendChild(tb); wrap.appendChild(t);
  if (!CR.roster.length)
    wrap.replaceChildren(noData("Nobody on the commission roster yet — search above to add agents."));
  $("#cr-table").replaceChildren(wrap);
}

async function loadCommissionRules() {
  const box = $("#cr-rules");
  const r = await fetch("/api/commissions/rules");
  const d = await r.json().catch(() => ({}));
  if (!d.markdown) {
    box.replaceChildren(noData(d.error || "rules unavailable"));
    $("#cr-rules-updated").textContent = "";
    return;
  }
  $("#cr-rules-updated").textContent =
    "COMMISSION_RULES.md · updated " + (d.updated || "").slice(0, 10);
  box.innerHTML = mdToHtml(d.markdown);
}

/* Minimal markdown renderer for the rulebook: headings, bold/italic/code,
   lists, tables, blockquotes, hr. Escapes HTML first — the file is ours but
   there is no reason to trust it with markup. */
function mdToHtml(md) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/⚠️|⚠/g, "<span class='rules-warn'>⚠</span>");
  // join soft-wrapped lines first so bold/italic spans that cross a wrap
  // still match: a plain-text line continues the previous paragraph/bullet
  // unless that previous line is a block construct of its own.
  const raw = md.split(/\r?\n/);
  const isBlock = (s) => /^(#{1,4})\s|^\s*---+\s*$|^\s*\||^\s*>/.test(s);
  const isListItem = (s) => /^\s*[-*]\s+/.test(s) || /^\s*\d+\.\s+/.test(s);
  const lines = [];
  for (const ln of raw) {
    const prev = lines[lines.length - 1];
    if (prev !== undefined && prev.trim() !== "" && !isBlock(prev)
        && ln.trim() !== "" && !isBlock(ln) && !isListItem(ln)) {
      lines[lines.length - 1] = prev + " " + ln.trim();
    } else {
      lines.push(ln);
    }
  }
  const out = [];
  let list = null, table = null, quote = false, para = [];
  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
  };
  const closeAll = () => {
    flushPara();
    if (list) { out.push(`</${list}>`); list = null; }
    if (table) { out.push("</tbody></table>"); table = null; }
    if (quote) { out.push("</blockquote>"); quote = false; }
  };
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    const h = ln.match(/^(#{1,4})\s+(.*)/);
    if (h) { closeAll(); out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); continue; }
    if (/^\s*---+\s*$/.test(ln)) { closeAll(); out.push("<hr>"); continue; }
    if (/^\s*\|/.test(ln)) {
      flushPara();
      const cells = ln.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;   // separator row
      if (!table) {
        if (list) { out.push(`</${list}>`); list = null; }
        table = true;
        out.push("<table><thead><tr>" + cells.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>");
      } else {
        out.push("<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>");
      }
      continue;
    }
    if (table) { out.push("</tbody></table>"); table = null; }
    const li = ln.match(/^\s*[-*]\s+(.*)/) || ln.match(/^\s*(\d+)\.\s+(.*)/);
    if (li) {
      flushPara();
      const kind = li.length === 3 ? "ol" : "ul";
      const text = li.length === 3 ? li[2] : li[1];
      if (list && list !== kind) { out.push(`</${list}>`); list = null; }
      if (!list) { out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(text)}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    if (/^\s*>/.test(ln)) {
      flushPara();
      if (!quote) { out.push("<blockquote>"); quote = true; }
      out.push(inline(ln.replace(/^\s*>\s?/, "")) + "<br>");
      continue;
    }
    if (quote) { out.push("</blockquote>"); quote = false; }
    if (ln.trim() === "") { flushPara(); continue; }
    para.push(ln.trim());
  }
  closeAll();
  return out.join("\n");
}

const SW = { data: null, open: new Set(), busy: false };

async function loadSoftware() {
  if (SW.busy) return;
  SW.busy = true;
  const box = $("#sw-apps");
  $("#sw-hosts").replaceChildren();
  $("#sw-checked").textContent = "";
  box.replaceChildren(noData("Checking servers… this takes a few seconds"));
  const btn = $("#sw-refresh");
  if (btn && !btn.dataset.bound) { btn.dataset.bound = "1"; btn.addEventListener("click", loadSoftware); }
  try {
    const r = await fetch("/api/software");
    if (r.status === 403 || r.status === 401) { box.replaceChildren(noData("Admin sign-in required.")); return; }
    SW.data = await r.json();
    renderSoftware(SW.data);
  } catch (e) {
    box.replaceChildren(noData("Could not reach the dashboard server: " + e));
  } finally {
    SW.busy = false;
  }
}

const SW_PILL = { ok: "OK", warn: "Warning", down: "Down", idle: "Idle" };

function swAge(ageH) {
  if (ageH == null) return "never";
  if (ageH < 0) {                              // FieldRoutes lets reps set a time ahead of now
    const h = -ageH;
    return "dated " + (h < 1 ? Math.round(h * 60) + " min" : h.toFixed(1) + "h") + " ahead";
  }
  if (ageH < 1) return Math.round(ageH * 60) + " min ago";
  if (ageH < 48) return ageH.toFixed(1) + "h ago";
  return (ageH / 24).toFixed(1) + " days ago";
}
function swWhen(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? String(iso).slice(0, 19)
    : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function swUptime(sec) {
  if (sec == null) return "–";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
  return d ? `${d}d ${h}h` : `${h}h`;
}
function swPill(status, text) {
  return `<span class="sw-pill ${status}">${text || SW_PILL[status] || status}</span>`;
}
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderSoftware(d) {
  $("#sw-checked").textContent = "checked " + new Date(d.generated_at)
    .toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

  // servers strip
  const hosts = document.createElement("div");
  hosts.className = "sw-host-row";
  for (const h of Object.values(d.hosts)) {
    const st = h.reachable ? "ok" : "down";
    const el = document.createElement("div");
    el.className = "sw-host " + st;
    el.innerHTML = `<div class="sw-host-name">${esc(h.name)} ${swPill(st, h.reachable ? "reachable" : "unreachable")}</div>`
      + `<div class="sw-host-sub">${esc(h.note)} · <a href="${esc(h.console_url)}" target="_blank" rel="noopener">${esc(h.ip)}</a></div>`
      + (h.reachable
        ? `<div class="sw-host-meta">up ${swUptime(h.uptime_s)} · disk ${h.disk_free_pct}% free · `
          + `${h.mem_avail_mb} MB free · firewall ${h.ufw_active === false ? '<b class="sw-bad">off</b>' : (h.ufw_active ? "on" : "?")}</div>`
        : `<div class="sw-host-meta sw-bad">${esc(h.error || "no response")}</div>`);
    hosts.appendChild(el);
  }
  $("#sw-hosts").replaceChildren(hosts);

  // app cards
  const list = document.createElement("div");
  list.className = "sw-list";
  for (const a of d.apps) {
    const card = document.createElement("div");
    card.className = "sw-card " + a.status + (SW.open.has(a.key) ? " open" : "");
    card.dataset.key = a.key;
    const head = document.createElement("div");
    head.className = "sw-card-head";
    head.innerHTML =
      `<div class="sw-card-title"><b>${esc(a.name)}</b> ${swPill(a.status, a.pill)}<span class="sw-chev">▾</span></div>`
      + `<div class="sw-card-headline">${esc(a.headline)}</div>`
      + `<div class="sw-card-summary">${esc(a.summary)}</div>`
      + `<div class="sw-card-sched">${esc(a.schedule)} · <span class="sw-host-tag">${esc(a.hostname)}</span></div>`;
    head.addEventListener("click", () => {
      SW.open.has(a.key) ? SW.open.delete(a.key) : SW.open.add(a.key);
      card.classList.toggle("open", SW.open.has(a.key));
    });
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "sw-card-body";
    let html = "";
    if (a.connections && a.connections.length) {
      html += `<h4>Data connections</h4><div class="table-wrap"><table class="sw-conn"><thead><tr>`
        + `<th>Connection</th><th>Status</th><th>Last verified data</th><th>Details</th></tr></thead><tbody>`;
      for (const c of a.connections) {
        html += `<tr><td>${esc(c.name)}</td><td>${swPill(c.status)}</td>`
          + `<td><b>${swAge(c.age_h)}</b><span class="sw-when">${swWhen(c.last)}</span></td>`
          + `<td class="sw-note">${esc(c.note || "")}</td></tr>`;
      }
      html += `</tbody></table></div>`;
    }
    if (a.unmapped) {                       // Sourcing App: calls whose dialed number its master sheet can't place
      const u = a.unmapped;
      const tip = `Answered inside-sales calls from the dashboard's own call log (refreshed by Sync; calls through `
        + `${swWhen(u.calls_through)}), dialed number checked against the sourcing app's master sheet `
        + `(${u.master_count} tracking numbers). The sourcing app drops these calls before they reach its database, `
        + `so the sales behind them get no phone source. Calls with no dialed number on record are left out.`;
      const pill = u.error ? swPill("warn", "unavailable") : swPill(u.calls ? "warn" : "ok", u.calls ? `${u.calls} calls` : "none");
      html += `<h4>Unmapped tracking numbers · last ${u.days} days ${pill} `
        + `<span class="info" tabindex="0" role="note" data-tip="${esc(tip)}">i</span></h4>`;
      if (u.error) {
        html += `<div class="sw-note">${esc(u.error)}</div>`;
      } else if (u.numbers.length) {
        const SHOW = 10;
        html += `<div class="table-wrap"><table class="sw-conn sw-unmapped"><thead><tr>`
          + `<th>Dialed number</th><th>Calls</th><th>Last call</th></tr></thead><tbody>`
          + u.numbers.map((n, i) => `<tr${i >= SHOW ? ' class="sw-more"' : ""}><td>${esc(n.display)}</td>`
            + `<td>${n.calls}</td><td>${swWhen(n.last)}</td></tr>`).join("")
          + `</tbody></table></div>`;
        if (u.numbers.length > SHOW) {
          html += `<button class="sw-showmore year-btn" type="button">Show all ${u.numbers.length} numbers</button>`;
        }
      } else {
        html += `<div class="sw-note">Every answered call in the last ${u.days} days dialed a mapped tracking number.</div>`;
      }
      const f = u.forms;
      if (f) {                                // dropped form leads, counted once a day by a Gmail pass
        const ftip = `Form-lead emails from the sourcing app's known lead senders that its provider rules could not `
          + `place on a source, so the lead was dropped. Counted once a day by a read-only Gmail pass `
          + `(${f.checked_days} of ${f.days} days counted, ${f.total_forms} lead emails seen). `
          + `Senders the sourcing app doesn't pull at all are not included.`;
        const fpill = (f.error && !f.checked_days) ? swPill("warn", "unavailable")
          : swPill(f.forms ? "warn" : "ok", f.forms ? `${f.forms} forms` : "none");
        html += `<h4>Unmapped forms · last ${f.days} days ${fpill} `
          + `<span class="info" tabindex="0" role="note" data-tip="${esc(ftip)}">i</span></h4>`
          + `<div class="sw-note">${f.error ? esc(f.error)
            : `${f.checked_days} of ${f.days} days counted · last pass ${swWhen(f.last_run)}`}</div>`;
      }
    }
    if (a.notes && a.notes.length) {
      html += `<h4>Notes</h4><ul class="sw-notes">` + a.notes.map((n) => `<li>${esc(n)}</li>`).join("") + `</ul>`;
    }
    if (a.recent && a.recent.length) {          // Missed Call Texter: the latest calls it handled
      const cls = (x) => (x === "sent" ? "ok" : /fail|unknown|error/.test(x) ? "bad" : x === "dry-run" ? "paused" : "");
      html += `<h4>Recent missed calls</h4><div class="table-wrap"><table class="sw-conn sw-recent"><thead><tr>`
        + `<th>When</th><th>Caller</th><th>What happened</th><th>Outcome</th><th>Details</th></tr></thead><tbody>`
        + a.recent.map((r) => `<tr><td>${swWhen(r.when)}</td><td>${esc(r.phone)}</td>`
          + `<td>${esc(r.kind)}${r.after_hours ? ` <span class="sw-when">after hours</span>` : ""}</td>`
          + `<td><span class="sw-out ${cls(r.action)}">${esc(r.outcome)}</span></td><td class="sw-note">${esc(r.detail)}</td></tr>`).join("")
        + `</tbody></table></div>`;
    }
    const facts = Object.entries(a.facts || {}).filter(([, v]) => v != null && v !== "" && typeof v !== "object");
    if (facts.length) {
      html += `<h4>Details</h4><dl class="sw-facts">` + facts.map(([k, v]) =>
        `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(/^\d{4}-\d{2}-\d{2}T/.test(String(v)) ? swWhen(v) : v)}</dd>`).join("") + `</dl>`;
    }
    html += `<h4>Server</h4><div class="sw-server">${esc(a.hostname)} · ${esc(a.host)} · `
      + `<a href="${esc((d.hosts[a.host] || {}).console_url || "#")}" target="_blank" rel="noopener">open in DigitalOcean</a></div>`;
    if (a.actions && a.actions.length) {
      html += `<h4>Actions</h4><div class="sw-actions">` + a.actions.map((x) =>
        `<button class="sw-action" data-action="${esc(x.key)}" data-confirm="${esc(x.confirm)}">${esc(x.label)}</button>`).join("")
        + `</div><div class="sw-result" data-for="${esc(a.key)}"></div>`;
    } else if (a.key === "isdash") {
      html += `<h4>Actions</h4><div class="sw-server">Use the <b>⟳ Sync</b> button in the header.</div>`;
    }
    body.innerHTML = html;
    const more = body.querySelector(".sw-showmore");
    if (more) more.addEventListener("click", (e) => {
      e.stopPropagation();   // don't toggle the card
      const all = body.querySelector(".sw-unmapped").classList.toggle("show-all");
      more.textContent = all ? "Show fewer" : `Show all ${a.unmapped.numbers.length} numbers`;
    });
    body.querySelectorAll(".sw-action").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();   // don't toggle the card
        runSoftwareAction(btn, body.querySelector(".sw-result"));
      }));
    card.appendChild(body);
    list.appendChild(card);
  }
  $("#sw-apps").replaceChildren(list);
}

async function runSoftwareAction(btn, resultEl) {
  const action = btn.dataset.action, label = btn.textContent;
  if (!window.confirm(btn.dataset.confirm || `Run "${label}"?`)) return;
  btn.disabled = true; btn.textContent = "Running…";
  resultEl.className = "sw-result"; resultEl.textContent = "";
  try {
    const r = await fetch("/api/software/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    let res = {};
    try { res = await r.json(); } catch (e) { /* non-JSON body */ }
    const msg = res.message || res.error || (r.ok ? "done" : `HTTP ${r.status}`);
    if (r.ok && res.ok) {
      resultEl.className = "sw-result ok";
      resultEl.textContent = "✓ " + msg;
      toast(`${label}: ${msg}`, "ok", 6000);
      setTimeout(() => { if (!SW.busy) loadSoftware(); }, 5000);   // show the new state
    } else {
      resultEl.className = "sw-result bad";
      resultEl.textContent = "✗ " + msg;
      toast(`${label} failed: ${msg}`, "err", 8000);
    }
  } catch (e) {
    resultEl.className = "sw-result bad";
    resultEl.textContent = "✗ could not reach the dashboard server: " + e;
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

/* ---------- settings (agent roster config) ---------- */
const SET = { agents: [], sellers: [] };

async function loadSettings() {
  const d = await (await fetch("/api/settings")).json();
  SET.agents = d.agents || [];
  SET.sellers = d.sellers || [];
  bindSetSearch();
  renderSetTable();
  loadConfigEditors();
}

function bindSetSearch() {
  const input = $("#set-search"), results = $("#set-search-results");
  if (!input.dataset.bound) {
    input.dataset.bound = "1";
    input.addEventListener("input", () => renderSetResults(input.value));
    input.addEventListener("focus", () => renderSetResults(input.value));
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".set-search-box")) results.style.display = "none";
    });
  }
}

function renderSetResults(q) {
  const results = $("#set-search-results");
  const tracked = new Set(SET.agents.map((a) => a.name));
  q = q.trim().toLowerCase();
  const matches = SET.sellers
    .filter((s) => !tracked.has(s.name) && (!q || s.name.toLowerCase().includes(q)))
    .slice(0, 14);
  if (!matches.length) {
    results.innerHTML = `<div class="set-result-empty">${q ? "no matching sellers" : ""}</div>`;
    results.style.display = q ? "block" : "none";
    return;
  }
  results.replaceChildren(...matches.map((s) => {
    const row = document.createElement("button");
    row.className = "set-result";
    row.innerHTML = `<span>${s.name}</span><span class="set-result-rev">${revMoney(s.revenue)} · ${s.fr_count} offices</span>`;
    row.addEventListener("click", () => {
      results.style.display = "none"; $("#set-search").value = "";
      openAgentModal({ name: s.name, fr_count: s.fr_count, revenue: s.revenue }, true);
    });
    return row;
  }));
  results.style.display = "block";
}

function renderSetTable() {
  const yn = (v) => (v ? "✓" : "—");
  const teamLabel = (t) => (t === "josh" ? "Josh" : t === "kai" ? "Kai" : "—");
  const wrap = document.createElement("div"); wrap.className = "table-wrap compact";
  const t = document.createElement("table");
  t.innerHTML = "<thead><tr><th>Agent</th><th>FR IDs</th><th>Active</th>"
    + "<th>Power Score</th><th>Team</th><th></th></tr></thead>";
  const tb = document.createElement("tbody");
  for (const a of SET.agents.slice().sort((x, y) => x.name.localeCompare(y.name))) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${a.name}</td><td class="${a.fr_ids ? "" : "flag-bad"}">${a.fr_ids || "⚠ 0"}</td>`
      + `<td class="${a.active ? "" : "dim"}">${yn(a.active)}</td>`
      + `<td class="${a.powerscore ? "" : "dim"}">${yn(a.powerscore)}</td>`
      + `<td>${teamLabel(a.team)}</td>`;
    const td = document.createElement("td");
    const edit = document.createElement("button");
    edit.className = "nav-btn"; edit.textContent = "Edit";
    edit.addEventListener("click", () => openAgentModal(a, false));
    td.appendChild(edit); tr.appendChild(td);
    tb.appendChild(tr);
  }
  t.appendChild(tb); wrap.appendChild(t);
  $("#set-table").replaceChildren(wrap);
}

function closeModal() { const m = $("#agent-modal"); if (m) m.remove(); }

function openAgentModal(agent, isNew) {
  closeModal();
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay"; overlay.id = "agent-modal";
  const sub = isNew ? `${revMoney(agent.revenue)} lifetime · ${agent.fr_count} office IDs`
    : `${agent.fr_ids} office IDs`;
  overlay.innerHTML =
    `<div class="modal-card">
      <div class="modal-head"><b>${isNew ? "Add" : "Edit"} ${agent.name}</b>
        <span class="sub">${sub}</span></div>
      <label class="modal-field">Team
        <select id="m-team"><option value="">— none —</option>
          <option value="josh">Josh</option><option value="kai">Kai</option></select></label>
      <label class="modal-check"><input type="checkbox" id="m-active">
        <span>Active <span class="sub">— shows on IS #'s &amp; Team Metrics</span></span></label>
      <label class="modal-check"><input type="checkbox" id="m-power">
        <span>Power Score <span class="sub">— include in lead-distribution ranking</span></span></label>
      <div class="modal-actions">
        ${isNew ? "" : '<button class="nav-btn modal-remove" id="m-remove">Remove agent</button>'}
        <span class="modal-spacer"></span>
        <button class="nav-btn" id="m-cancel">Cancel</button>
        <button class="nav-btn primary" id="m-save">Save</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector("#m-team").value = agent.team || "";
  overlay.querySelector("#m-active").checked = isNew ? true : !!agent.active;
  overlay.querySelector("#m-power").checked = isNew ? false : !!agent.powerscore;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
  overlay.querySelector("#m-cancel").addEventListener("click", closeModal);
  overlay.querySelector("#m-save").addEventListener("click", async () => {
    const body = {
      team: overlay.querySelector("#m-team").value,
      active: overlay.querySelector("#m-active").checked,
      powerscore: overlay.querySelector("#m-power").checked,
    };
    const btn = overlay.querySelector("#m-save"); btn.disabled = true; btn.textContent = "Saving…";
    if (isNew) {
      await fetch("/api/settings/add", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: agent.name, ...body }) });
    } else {
      await postAgent({ id: agent.id, ...body });
    }
    closeModal(); await loadSettings(); toast("✓ Saved", "ok", 2500);
  });
  if (!isNew) {
    overlay.querySelector("#m-remove").addEventListener("click", async () => {
      await postAgent({ id: agent.id, scorecard: false });
      closeModal(); await loadSettings(); toast("Agent removed", "ok", 2500);
    });
  }
}

/* ---------- editable tiers + power-score weights ---------- */
const TIER_METRICS = [
  ["conversion_pct", "Conversion %", "%"],
  ["call_grade", "Call Grade", "%"],
  ["acv", "ACV", "$"],
  ["multi_total_pct", "Total Multi-Yr %", "%"],
  ["multi24_pct", "Multi-yr 24 %", "%"],
  ["monthly_pct", "Monthly Billing %", "%"],
  ["sts_pct", "Sold → Serviced %", "%"],
  ["aip", "AIP", "$"],
  ["signed_pct", "Signed Agr %", "%"],
  ["quality_score", "Quality Score", ""],
  ["ans_missed", "Ans:Missed ratio", ""],
  ["avg_acw_s", "Avg ACW (seconds · lower is better)", ""],
  ["mm", "MM — Master Metric (Tier 1 = goal; yellow between minimum and goal)", ""],
];
function cfgNum(value, attrs) {
  const i = document.createElement("input");
  i.type = "number"; i.step = "any"; i.className = "cfg-num";
  i.value = (value === null || value === undefined) ? "" : value;
  for (const k in attrs) i.dataset[k] = attrs[k];
  return i;
}
function cfgBtn(label, primary, fn) {
  const b = document.createElement("button");
  b.className = "nav-btn" + (primary ? " primary" : "");
  b.textContent = label;
  b.addEventListener("click", fn);
  return b;
}

async function loadConfigEditors() {
  const host = $("#set-config");
  if (!host) return;
  await loadConfig();               // refresh CONFIG from the server
  PSE = null;                       // the power score editor restarts from what's saved
  host.replaceChildren(buildTierEditor(), buildPowerEditor());
}

function buildTierEditor() {
  const tiers = CONFIG.scorecard_tiers || {};
  const box = document.createElement("div");
  box.className = "cfg-block";
  const h = document.createElement("h3");
  h.innerHTML = "Scorecard Tiers <span class='sub'>green ≥ Tier 1, yellow ≥ Tier 2, red below — under Minimum shows red text. " +
    "Leave a row blank for no colouring. Avg ACW works the other way round: green at or under Tier 1, and “Minimum” is the most allowed.</span>";
  box.appendChild(h);
  const wrap = document.createElement("div"); wrap.className = "cfg-card cfg-tiers";
  const t = document.createElement("table"); t.className = "cfg-table";
  t.innerHTML = "<thead><tr><th>Metric</th><th>Tier 1</th><th>Tier 2</th><th>Minimum</th></tr></thead>";
  const tb = document.createElement("tbody");
  for (const [key, label, unit] of TIER_METRICS) {
    const band = tiers[key] || {};
    const tr = document.createElement("tr");
    const td0 = document.createElement("td"); td0.textContent = label; tr.appendChild(td0);
    for (const bk of ["t1", "t2", "min"]) {
      const td = document.createElement("td");
      if (key === "mm" && bk === "t2") { td.textContent = "—"; td.className = "dim"; td.title = "MM has a goal and a minimum only"; tr.appendChild(td); continue; }
      const pre = document.createElement("span"); pre.className = "cfg-unit";
      pre.textContent = unit === "$" ? "$" : "";
      td.append(pre, cfgNum(band[bk], { tier: key, band: bk }));
      if (unit === "%") { const s = document.createElement("span"); s.className = "cfg-unit"; s.textContent = "%"; td.append(s); }
      tr.appendChild(td);
    }
    tb.appendChild(tr);
  }
  t.appendChild(tb); wrap.appendChild(t); box.appendChild(wrap);
  const bar = document.createElement("div"); bar.className = "cfg-actions";
  const save = cfgBtn("Save tiers", true, async () => {
    const out = {};
    box.querySelectorAll("input.cfg-num").forEach((i) => {
      if (i.value === "") return;
      (out[i.dataset.tier] = out[i.dataset.tier] || {})[i.dataset.band] = parseFloat(i.value);
    });
    await saveConfig({ scorecard_tiers: out }, save);
  });
  bar.append(save, cfgBtn("Reset to defaults", false, () => resetConfig("scorecard_tiers")));
  box.appendChild(bar);
  return box;
}

// The Power Score editor works on a draft copy (PSE) of the saved rules: three
// lists — points earned (a ladder per metric), penalties, rank bonuses — each
// with add / remove. Any IS #'s metric can be used anywhere. Save sends v2.
let PSE = null;
function psEditorRerender() {
  const old = document.getElementById("ps-editor");
  if (old) old.replaceWith(buildPowerEditor());
}
function psUnit(k) {
  const m = psMetric(k);
  return m.fmt === "pct" ? "%" : (m.fmt === "money0" || m.fmt === "money2") ? "$" : m.fmt === "ms" ? "sec" : "";
}
// metrics used by more than one rule (each would show as its own column)
function psDupes(cfg) {
  const seen = new Set(), dup = new Set();
  for (const g of ["points", "penalties", "bonuses"]) {
    for (const x of cfg[g] || []) { if (seen.has(x.metric)) dup.add(x.metric); seen.add(x.metric); }
  }
  return [...dup];
}
function psMetricSelect(val, onChange, taken = new Set()) {
  const sel = document.createElement("select"); sel.className = "cfg-sel";
  for (const k of psMetricKeys()) {
    const o = new Option(psLabel(k) + (k !== val && taken.has(k) ? " (already used)" : ""), k);
    o.disabled = k !== val && taken.has(k);
    sel.append(o);
  }
  sel.value = val;
  sel.addEventListener("change", () => onChange(sel.value));
  return sel;
}
function buildPowerEditor() {
  if (!PSE) PSE = JSON.parse(JSON.stringify(psCfg()));
  for (const g of ["points", "penalties", "bonuses"]) PSE[g] = PSE[g] || [];
  const box = document.createElement("div");
  box.className = "cfg-block"; box.id = "ps-editor";
  const h = document.createElement("h3");
  h.innerHTML = "Power Score Weights <span class='sub'>points earned, penalties and bonuses that add up to the Power Score — add or remove any IS #'s metric</span>";
  h.appendChild(infoIcon("Each metric can be used in only one rule, so it shows as one column on the Power Score. " +
    "For points plus a penalty on the same metric, use a points ladder and give its last row negative points " +
    "(e.g. at least 75 → 0, at least 0 → -2)."));
  box.appendChild(h);
  const allRules = () => [...PSE.points, ...PSE.penalties, ...PSE.bonuses];
  const taken = new Set(allRules().map((x) => x.metric));
  const dupes = psDupes(PSE);
  if (dupes.length) {
    const w = document.createElement("div"); w.className = "cfg-warn";
    w.textContent = `${dupes.map(psLabel).join(", ")} ${dupes.length > 1 ? "are" : "is"} used by more than one rule, ` +
      "so it shows up more than once on the Power Score. Remove the extra rule (the red ✕ cards below) and save.";
    box.appendChild(w);
  }
  const xBtn = (title, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = "cfg-x"; b.textContent = "✕"; b.title = title; b.addEventListener("click", fn); return b; };
  const addBtn = (label, fn) => { const b = document.createElement("button"); b.type = "button"; b.className = "cfg-add"; b.textContent = label; b.addEventListener("click", fn); return b; };
  const num = (val, onInput, ph) => {
    const i = document.createElement("input"); i.type = "number"; i.step = "any"; i.className = "cfg-num";
    i.value = val == null ? "" : val; if (ph) i.placeholder = ph;
    i.addEventListener("input", () => onInput(i.value === "" ? null : parseFloat(i.value)));
    return i;
  };
  const unused = () => psMetricKeys().find((k) => !taken.has(k)) || null;
  const full = () => { toast("Every metric already has a rule — remove one first", "err", 4000); };

  // 1) points earned
  const lab1 = document.createElement("div"); lab1.className = "cfg-group-label";
  lab1.textContent = "Points earned — the best threshold a rep clears wins";
  box.appendChild(lab1);
  const grid = document.createElement("div"); grid.className = "cfg-ps-grid";
  PSE.points.forEach((p, i) => {
    const lower = psMetric(p.metric).lowerBetter, unit = psUnit(p.metric);
    const card = document.createElement("div"); card.className = "cfg-card cfg-ps-card" + (dupes.includes(p.metric) ? " cfg-dup" : "");
    const head = document.createElement("div"); head.className = "cfg-ps-head";
    head.append(psMetricSelect(p.metric, (v) => { p.metric = v; psEditorRerender(); }, taken),
      xBtn("Remove this metric from points earned", () => { PSE.points.splice(i, 1); psEditorRerender(); }));
    card.appendChild(head);
    const tbl = document.createElement("table"); tbl.className = "cfg-table";
    tbl.innerHTML = `<thead><tr><th>${lower ? "At most" : "At least"} ${unit}</th><th>Points</th><th></th></tr></thead>`;
    const tb = document.createElement("tbody");
    p.scale = p.scale && p.scale.length ? p.scale : [[null, null]];
    p.scale.forEach((pair, r) => {
      const tr = document.createElement("tr");
      const a = document.createElement("td"); a.appendChild(num(pair[0], (v) => { pair[0] = v; }));
      const b = document.createElement("td"); b.appendChild(num(pair[1], (v) => { pair[1] = v; }));
      const x = document.createElement("td"); x.appendChild(xBtn("Remove this row", () => { p.scale.splice(r, 1); psEditorRerender(); }));
      tr.append(a, b, x); tb.appendChild(tr);
    });
    tbl.appendChild(tb); card.appendChild(tbl);
    card.appendChild(addBtn("+ row", () => { p.scale.push([null, null]); psEditorRerender(); }));
    grid.appendChild(card);
  });
  grid.appendChild(addBtn("+ Add a metric", () => {
    const m = unused(); if (!m) { full(); return; }
    PSE.points.push({ metric: m, scale: [[null, null]] }); psEditorRerender();
  }));
  box.appendChild(grid);

  // 2) penalties  3) bonuses
  const lab2 = document.createElement("div"); lab2.className = "cfg-group-label";
  lab2.textContent = "Penalties and bonuses";
  box.appendChild(lab2);
  const extra = document.createElement("div"); extra.className = "cfg-rules";
  const card = (title, metric, onRemove, ...parts) => {
    const d = document.createElement("div"); d.className = "cfg-card cfg-rule" + (dupes.includes(metric) ? " cfg-dup" : "");
    const top = document.createElement("div"); top.className = "cfg-ps-head";
    const b = document.createElement("b"); b.textContent = title;
    top.append(b, xBtn("Remove", onRemove));
    const line = document.createElement("div"); line.className = "cfg-rule-line"; line.append(...parts);
    d.append(top, line); return d;
  };
  PSE.penalties.forEach((p, i) => {
    const lower = psMetric(p.metric).lowerBetter, unit = psUnit(p.metric);
    extra.appendChild(card("Penalty", p.metric, () => { PSE.penalties.splice(i, 1); psEditorRerender(); },
      num(p.penalty, (v) => { p.penalty = v; }, "-2"), " points if ",
      psMetricSelect(p.metric, (v) => { p.metric = v; psEditorRerender(); }, taken),
      lower ? " is above " : " is below ", num(p.threshold, (v) => { p.threshold = v; }), unit ? " " + unit : ""));
  });
  PSE.bonuses.forEach((bn, i) => {
    const pts = document.createElement("input"); pts.type = "text"; pts.className = "cfg-num cfg-wide";
    pts.value = (bn.points || []).join(", "); pts.placeholder = "3, 2, 1";
    pts.addEventListener("input", () => {
      bn.points = pts.value.split(/[\s,;]+/).map(parseFloat).filter((n) => Number.isFinite(n) && n >= 0);
    });
    const note = document.createElement("span"); note.className = "sub"; note.textContent = "comma-separated, by rank; everyone else gets 0";
    extra.appendChild(card("Rank bonus", bn.metric, () => { PSE.bonuses.splice(i, 1); psEditorRerender(); },
      "Points for 1st, 2nd, 3rd… in ", psMetricSelect(bn.metric, (v) => { bn.metric = v; psEditorRerender(); }, taken), " : ",
      pts, document.createElement("br"), note));
  });
  const adds = document.createElement("div"); adds.className = "cfg-adds";
  adds.append(addBtn("+ Add a penalty", () => {
    const m = unused(); if (!m) { full(); return; }
    PSE.penalties.push({ metric: m, threshold: null, penalty: -2 }); psEditorRerender();
  }),
    addBtn("+ Add a rank bonus", () => {
      const m = taken.has("rev") ? unused() : "rev"; if (!m) { full(); return; }
      PSE.bonuses.push({ metric: m, points: [3, 2, 1] }); psEditorRerender();
    }));
  extra.appendChild(adds);
  box.appendChild(extra);

  const bar = document.createElement("div"); bar.className = "cfg-actions";
  const saveBtn = cfgBtn("Save power score", true, async () => {
    const ok = (v) => v != null && Number.isFinite(v);
    const power = {
      version: 2,
      points: PSE.points.map((p) => ({ metric: p.metric, scale: (p.scale || []).filter((x) => ok(x[0]) && ok(x[1])) }))
        .filter((p) => p.scale.length),
      penalties: PSE.penalties.filter((p) => ok(p.threshold) && ok(p.penalty)),
      bonuses: PSE.bonuses.filter((b) => (b.points || []).length),
    };
    const d = psDupes(power);
    if (d.length) { toast(`${d.map(psLabel).join(", ")} is in more than one rule — remove the extra one first`, "err", 6000); return; }
    await saveConfig({ power_score: power }, saveBtn);
  });
  bar.append(saveBtn, cfgBtn("Reset to defaults", false, () => {
    if (confirm("Reset every Power Score rule to the defaults?")) resetConfig("power_score");
  }));
  box.appendChild(bar);
  return box;
}

async function saveConfig(body, btn) {
  const old = btn.textContent; btn.disabled = true; btn.textContent = "Saving…";
  try {
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) { toast("Save failed: " + (j.error || r.status), "err", 6000); }
    else { applyConfig(j); toast("✓ Saved", "ok", 3000); await loadConfigEditors(); refreshAgentViews(); }
  } catch (e) { toast("Save failed: " + e, "err", 6000); }
  btn.disabled = false; btn.textContent = old;
}
async function resetConfig(key) {
  const r = await fetch("/api/config/reset", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key }),
  });
  const j = await r.json();
  if (r.ok) { applyConfig(j); toast("Reset to defaults", "ok", 3000); await loadConfigEditors(); refreshAgentViews(); }
  else { toast("Reset failed", "err", 5000); }
}
function applyConfig(j) {
  if (j && j.power_score) CONFIG.power_score = j.power_score;
  if (j && j.scorecard_tiers) CONFIG.scorecard_tiers = j.scorecard_tiers;
}
function refreshAgentViews() {
  for (const k of ["sc", "ps"]) if (PK[k] && PK[k].loaded) PK[k].fire();
}

async function postAgent(body) {
  await fetch("/api/settings/agent", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // bust caches so other tabs reflect the change
  fetchRange.cache = {}; AV.cache = {}; SRC.cache = {};
}

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  return td;
}
function toggleCell(on, onChange) {
  const td = document.createElement("td");
  const cb = document.createElement("input");
  cb.type = "checkbox"; cb.checked = on;
  cb.addEventListener("change", () => onChange(cb.checked));
  td.appendChild(cb);
  return td;
}
function teamCell(val, onChange) {
  const td = document.createElement("td");
  const sel = document.createElement("select");
  for (const [v, label] of [["", "—"], ["josh", "Josh"], ["kai", "Kai"]])
    sel.append(new Option(label, v));
  sel.value = val || "";
  sel.addEventListener("change", () => onChange(sel.value));
  td.appendChild(sel);
  return td;
}

function showMetricsSub(sub) {
  document.querySelectorAll("#metrics-subtabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.msub === sub));
  document.querySelectorAll(".msubpanel").forEach((p) =>
    p.classList.toggle("active", p.id === "msub-" + sub));
  if (sub === "team") { ensureLoaded("tm"); loadTeamOverview(); }
  if (sub === "phone") {
    showSubTab(document.querySelector("#phone-subtabs button.active").dataset.sub);
  }
  if (sub === "forms") ensureLoaded("fl");
  if (sub === "multiyear") ensureLoaded("my");
  if (sub === "acv") ensureLoaded("cv");
}

function showAgentSub(sub) {
  document.querySelectorAll("#agent-subtabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.asub === sub));
  document.querySelectorAll(".asubpanel").forEach((p) =>
    p.classList.toggle("active", p.id === "asub-" + sub));
  if (sub === "power") ensureLoaded("ps");
  if (sub === "scorecard") ensureLoaded("sc");
  if (sub === "compare") ensureLoaded("av");
  if (sub === "master") ensureLoaded("mm");
  if (sub === "inbound") ensureLoaded("ib");
}

/* ---------- Payroll: Commission Lookup ---------- */
function initLookup() {
  const input = $("#cl-q"), go = $("#cl-go");
  if (input.dataset.bound) { input.focus(); return; }
  input.dataset.bound = "1";
  const run = () => doLookup(input.value);
  go.addEventListener("click", run);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  input.focus();
}

async function doLookup(q) {
  const box = $("#cl-results");
  q = (q || "").trim();
  if (!q) { box.replaceChildren(); return; }
  box.replaceChildren(noData("Looking up…"));
  const r = await fetch("/api/commissions/lookup?q=" + encodeURIComponent(q));
  const d = await r.json().catch(() => ({}));
  if (d.error) { box.replaceChildren(noData(d.error)); return; }
  $("#cl-asof").textContent = d.data_as_of ? "engine data synced " + d.data_as_of : "";
  if (!(d.results || []).length) {
    box.replaceChildren(noData(`Nothing found for “${q}” — try the PestRoutes account number.`));
    return;
  }
  const money = (v) => v == null ? "—" :
    "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const stateCls = (s) => ({
    vested: "cl-good", paid: "cl-good", excluded_renewal: "cl-good",
    excluded_moved: "cl-good", excluded_resigned: "cl-good",
    half_kept_fee_paid: "cl-warn", pending: "cl-warn", not_started: "cl-warn",
    clawed_back: "cl-bad", cancelled_unpaid: "cl-dim", excluded_prestart: "cl-dim",
  }[s] || "cl-dim");
  const label = (s) => (s || "no status").replace(/_/g, " ");
  box.replaceChildren(...d.results.map((s) => {
    const card = document.createElement("div");
    card.className = "cl-card";
    const splits = (s.splits || []).map((a) =>
      `${a.rep} ${Math.round(a.share * 100)}% (${a.type})`).join(" · ");
    card.innerHTML = `
      <div class="cl-head">
        <b>${s.customer || "?"}</b>
        <span class="cl-ids">acct ${s.customer_id} · sub ${s.subscription_id}</span>
        <span class="cl-state ${stateCls(s.state)}">${label(s.state)}</span>
      </div>
      <div class="cl-grid">
        <span><i>Rep</i>${s.rep || "—"}${splits ? " · split: " + splits : ""}</span>
        <span><i>Plan</i>${s.service_type || "—"} · ${s.source || "no source"}</span>
        <span><i>Contract value</i>${money(s.contract_value)}</span>
        <span><i>Rate / commission</i>${s.rate != null ? Math.round(s.rate * 100) + "%" : "—"} / ${money(s.expected)}</span>
        <span><i>Sold</i>${s.date_added || "—"}</span>
        <span><i>First paid</i>${s.first_paid || "not yet"}</span>
        <span><i>Services</i>${s.qualifying ?? 0} of 3</span>
        <span><i>Coverage</i>${money(s.coverage_paid)} of ${money(s.coverage_target)}</span>
        ${s.cancel_date ? `<span><i>Cancelled</i>${s.cancel_date} (${s.cancel_reason || "no reason"})</span>` : ""}
        <span><i>Card on file</i>${s.card_on_file ? "yes" : "no"}</span>
      </div>
      <div class="cl-why">${s.why || ""}</div>
      ${s.flags ? `<div class="cl-flags">flags: ${s.flags}</div>` : ""}`;
    return card;
  }));
}

/* ---------- sync ---------- */
function initSync() {
  const btn = $("#sync-btn"), pill = $("#sync-status");
  const box = document.querySelector(".sync-box");
  if (box && can("sync")) box.style.display = "";   // needs the Run Sync permission;
                                     // non-admins are cooldown-limited server-side
  let timer = null;
  async function poll() {
    const s = await (await fetch("/api/sync/status")).json();
    if (s.running) {
      btn.disabled = true;
      pill.textContent = "Syncing — " + (s.step || "starting") + "…";
      return;
    }
    clearInterval(timer); timer = null;
    btn.disabled = false;
    if (s.error) {
      pill.textContent = "";
      toast("Sync failed: " + s.error.slice(0, 120), "err", 9000);
    } else if (s.finished) {
      pill.textContent = "";
      const notes = (s.steps || []).map((x) => `${x.name}: ${x.note || "ok"}`);
      toast("✓ Sync complete — data is up to date", "ok", 6000);
      console.log("sync summary:", notes);
      setTimeout(() => window.location.reload(), 2500);
    }
  }
  btn.addEventListener("click", async () => {
    const r = await (await fetch("/api/sync", { method: "POST" })).json();
    if (r.error === "cooldown") {
      toast(r.message || "Synced recently — try again soon.", "err", 6000);
      return;                       // button stays enabled; nothing started
    }
    if (!r.started) { toast("A sync is already running", "err", 4000); }
    btn.disabled = true;
    pill.textContent = "Syncing…";
    timer = setInterval(poll, 2500);
  });
  // resume display if a sync is already running when the page opens
  fetch("/api/sync/status").then((r) => r.json()).then((s) => {
    if (s.running) { btn.disabled = true; timer = setInterval(poll, 2500); }
  });
}

function toast(msg, cls, ms) {
  const t = document.createElement("div");
  t.className = "toast " + cls;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

/* ---------- explore (custom ranges + CSV drill-downs) ---------- */
function fmtDay(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
    "-" + String(d.getDate()).padStart(2, "0");
}

// 14-day Sun–Sat pay periods, anchored the same way as the commissions app
// (the Saturday two days before the anchor payroll Monday ends a period).
// offset 0 = the period containing today, -1 = the one before.
function payPeriod(offset) {
  const [y, m, d] = (ME.payroll_anchor_monday || "2026-08-31").split("-").map(Number);
  const end0 = new Date(y, m - 1, d - 2);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const n = Math.ceil(Math.round((today - end0) / 864e5) / 14) + offset;
  const end = new Date(end0); end.setDate(end0.getDate() + 14 * n);
  const start = new Date(end); start.setDate(end.getDate() - 13);
  return [start, end];
}

function presetRange(p, opts = {}) {
  const now = new Date();
  const day = (offset) => { const d = new Date(now); d.setDate(d.getDate() + offset); return d; };
  const sunday = (d) => { const x = new Date(d); x.setDate(x.getDate() - x.getDay()); return x; };
  switch (p) {
    // the IS #'s sheet (and the agent's copy of it) runs to the period's last
    // day, so pending appointments still ahead count like they will if they're
    // serviced; everywhere else stops at today (Dallin, 2026-10-16)
    case "pay_this": { const [s, e] = payPeriod(0); return [s, opts.fullPayPeriod || e <= now ? e : now]; }
    case "pay_last": return payPeriod(-1);
    case "today": return [now, now];
    case "yesterday": return [day(-1), day(-1)];
    case "this_week": return [sunday(now), now];
    case "ytd": return [new Date(now.getFullYear(), 0, 1), now];
    case "last_week": { const s = sunday(day(-7)); const e = new Date(s); e.setDate(e.getDate() + 6); return [s, e]; }
    case "this_month": return [new Date(now.getFullYear(), now.getMonth(), 1), now];
    case "last_month": return [new Date(now.getFullYear(), now.getMonth() - 1, 1),
                               new Date(now.getFullYear(), now.getMonth(), 0)];
    case "t7": return [day(-7), now];
    case "t90": return [day(-90), now];
    default: return [day(-30), now];  // t30
  }
}

/* Reusable range picker: mounts preset + date inputs + Apply into `mount`,
   calls onApply(startDay, endDay). Used by every tab. */
function makeRangePicker(mount, onApply, defaultPreset, opts = {}) {
  const preset = document.createElement("select");
  for (const [v, label] of [["pay_this", "This pay period"],
      ["pay_last", "Last pay period"], ["today", "Today"],
      ["yesterday", "Yesterday"], ["this_week", "This week (Sun–today)"],
      ["last_week", "Last week (Sun–Sat)"], ["this_month", "This month"],
      ["last_month", "Last month"], ["ytd", "Year to date"], ["t7", "Trailing 7 days"],
      ["t30", "Trailing 30 days"], ["t90", "Trailing 90 days"],
      ["custom", "Custom…"]]) {
    preset.append(new Option(label, v));
  }
  preset.value = defaultPreset || "t30";
  const si = document.createElement("input"); si.type = "date";
  const ei = document.createElement("input"); ei.type = "date";
  const go = document.createElement("button");
  go.className = "nav-btn"; go.textContent = "Apply";
  const setDates = () => {
    if (preset.value && preset.value !== "custom") {
      const [a, b] = presetRange(preset.value, opts);
      si.value = fmtDay(a); ei.value = fmtDay(b);
    }
  };
  const fire = () => { if (si.value && ei.value && ei.value >= si.value) onApply(si.value, ei.value); };
  preset.addEventListener("change", () => { setDates(); fire(); });
  si.addEventListener("change", () => { preset.value = "custom"; });
  ei.addEventListener("change", () => { preset.value = "custom"; });
  go.addEventListener("click", fire);
  setDates();
  mount.append(preset, si, ei, go);
  const set = (a, b) => { preset.value = "custom"; si.value = a; ei.value = b; };
  return { fire, set, dates: () => [si.value, ei.value] };
}

/* two-option segmented toggle: Sold Between / Serviced Between */
function makeModeToggle(mount, onChange, initial = "sold") {
  let mode = initial;
  const mk = (val, label, tip) => {
    const b = document.createElement("button");
    b.textContent = label; b.title = tip;
    b.className = val === mode ? "on" : "";
    b.addEventListener("click", () => {
      if (mode === val) return;
      mode = val;
      mount.querySelectorAll("button").forEach((x) =>
        x.classList.toggle("on", x === b));
      onChange(mode);
    });
    return b;
  };
  mount.replaceChildren(
    mk("sold", "Sold Between", "Subscriptions whose sale date is in range"),
    mk("serviced", "Serviced Between",
       "Subscriptions whose initial appointment is scheduled/serviced in range"));
  return { get: () => mode };
}

async function fetchRange(start, end) {
  const key = start + ".." + end;
  fetchRange.cache = fetchRange.cache || {};
  if (!fetchRange.cache[key]) {
    fetchRange.cache[key] = await (await fetch(`/api/range?start=${start}&end=${end}`)).json();
  }
  return fetchRange.cache[key];
}

/* ---------- ⓘ notes ----------
   Where data comes from (and how often it refreshes, when that isn't "every
   load") lives behind a small info icon, never as text on the page. noteEl()
   makes a right-aligned icon line to drop under a table; noteInto() puts the
   icon in the nearest header above a container instead. */
function infoIcon(text) {
  const i = document.createElement("span");
  i.className = "info"; i.textContent = "i"; i.tabIndex = 0; i.setAttribute("role", "note");
  i.dataset.tip = text;
  return i;
}
function noteEl(text) {
  const line = document.createElement("div"); line.className = "info-line";
  line.appendChild(infoIcon(text));
  return line;
}
function setTip(el, text) { const i = el.querySelector(".info") || el; i.dataset.tip = text; }
function noteInto(containerSel, text, keep = false) {
  const box = $(containerSel);
  if (!box) return;
  if (!keep) box.replaceChildren();
  let head = null;
  for (let p = box.previousElementSibling; p && !head; p = p.previousElementSibling) {
    if (p.matches(".panel-head, h3.section-title, h2")) head = p;
  }
  const target = head ? (head.matches(".panel-head") ? head.querySelector("h2") || head : head) : box;
  let icon = target.querySelector(":scope > .info");
  if (!icon) { icon = infoIcon(text); target.appendChild(icon); }
  icon.dataset.tip = text;
}
// the notes written into the page itself (Payroll, Settings…) become icons too
document.querySelectorAll("p.home-note").forEach((p) => {
  p.replaceWith(noteEl(p.textContent.replace(/\s+/g, " ").trim()));
});

function rangeCaption(d) {
  return noteEl(`${d.start} – ${d.end} (Pacific days, inclusive). Audit sheet ` +
    `covers through ${d.audit_covered_through || "—"}; Honest% uses only days ` +
    `both systems cover. Conversions inside the last 14 days are still maturing.`);
}

/* ---------- per-tab custom ranges ---------- */
function initTabPickers() {
  // Inbound Phone Conversion -> Conversion subtab
  PK.ph = makeRangePicker($("#ph-picker"), async (a, b) => {
    if (!a) return;
    $("#ph-custom-table").replaceChildren(noData("Loading…"));
    const d = await fetchRange(a, b);
    const rows = d.rows.map((r) => ({
      agent: r.agent === "TEAM" ? "__TOTAL__" : r.agent,
      total_calls: r.calls, leads: r.leads, qual_leads: r.qual_leads,
      closed: r.closed, closed_pct: r.closed_pct ?? 0,
      followup: r.followup, followup_pct: r.followup_pct ?? 0,
      unqualified: r.unqualified, unqualified_pct: r.unqualified_pct ?? 0,
      conversions: r.conversions, conv_qual_pct: r.conv_qual_pct ?? 0,
      conv_leads_pct: r.conv_leads_pct ?? 0, conv_calls_pct: r.conv_calls_pct ?? 0,
      sheet_conv_pct: r.sheet_conv_pct, honest_pct: r.honest_pct,
    }));
    const st = { sort: "qual_leads", asc: false };
    const rerender = () => $("#ph-custom-table").replaceChildren(
      buildTable(rows, st, (k) => { toggleSort(st, k); rerender(); }),
      rangeCaption(d));
    rerender();
  }, "this_week");

  // Form Conversion — team funnel for the selected dates
  // ⬇ CSV export of the NET leads behind whatever range the picker shows
  if (!$("#fl-export")) {
    const xb = document.createElement("button");
    xb.id = "fl-export"; xb.className = "nav-btn";
    xb.textContent = "⬇ Export net leads (CSV)";
    xb.title = "Download every net form lead in the selected range — deduped, " +
      "already-customers excluded — with phone/email, source, and sold status.";
    xb.addEventListener("click", () => {
      const [a, b] = PK.fl.dates();
      if (a && b) window.location = `/api/formleads_export?start_day=${a}&end_day=${b}`;
    });
    $("#fl-picker").after(xb);
  }
  PK.fl = makeRangePicker($("#fl-picker"), async (a, b) => {
    $("#fl-note").textContent =
      "All form submissions (Gmail + Meta + LSA) → deduped to unique leads " +
      "(14-day window) → minus contacts who already had a live subscription when " +
      "the form arrived → net leads. Converted = now holds an active " +
      "subscription (pending or serviced). Re-checks on every Sync.";
    $("#fl-table").replaceChildren(noData("Loading…"));
    const d = await fetchRange(a, b);
    const f = d.forms || {};
    const cols = [
      ["forms", "Total Forms", "Every raw form submission across Gmail, Meta, and LSA"],
      ["deduped", "Deduped", "Unique leads after collapsing duplicate submissions from the same contact within 14 days of their last submission"],
      ["existing", "Already Customers", "Contacts who already had a LIVE subscription (pending or serviced, not cancelled) at the moment the form arrived — excluded"],
      ["net", "Net Leads", "Deduped − already customers (the conversion denominator)"],
      ["converted", "Converted", "Net leads who now hold an active subscription with at least a pending appointment"],
      ["conv_pct", "Conv%", "Converted ÷ net leads"],
    ];
    $("#fl-table").replaceChildren(
      simpleTable(cols, [f], { fmt: { conv_pct: (v) => (v ?? 0).toFixed(1) },
                               hl: new Set(["converted", "conv_pct"]) }),
      rangeCaption(d));
  });

  // Multiyear + Contract Value — per-agent tables for the selected dates
  PK.my = makeRangePicker($("#my-picker"), async (a, b) => {
    $("#my-table").replaceChildren(noData("Loading…"));
    const d = await fetchRange(a, b);
    renderMyRows(rangeSubsRows(d), rangeCaption(d));
  });
  PK.cv = makeRangePicker($("#cv-picker"), async (a, b) => {
    $("#cv-table").replaceChildren(noData("Loading…"));
    const d = await fetchRange(a, b);
    renderCvRows(rangeSubsRows(d), rangeCaption(d));
  });

  // Master Metric — Genesys calls + scheduled hours (+ Podium once connected)
  let mmToken = 0;
  PK.mm = makeRangePicker($("#mm-picker"), async (a, b) => {
    const token = ++mmToken;
    $("#mm-table").replaceChildren(noData("Loading… (reading the hours sheet and Genesys)"));
    $("#mm-notes").replaceChildren(); $("#mm-foot").replaceChildren();
    let d = null;
    try { d = await (await fetch(`/api/mastermetric?start_day=${a}&end_day=${b}`)).json(); }
    catch (e) { d = { error: "Couldn't reach the server." }; }
    if (token !== mmToken) return;
    if (!d || d.error || !d.rows) { $("#mm-table").replaceChildren(noData((d && d.error) || "No data.")); return; }
    renderMasterMetric(d);
  }, "pay_this");

  // Inbound vs Other — the call matcher's own-call sales vs everything else
  let ibToken = 0;
  PK.ib = makeRangePicker($("#ib-picker"), async (a, b) => {
    const token = ++ibToken;
    $("#ib-table").replaceChildren(noData("Loading…"));
    $("#ib-notes").replaceChildren(); $("#ib-foot").replaceChildren();
    let d = null;
    try { d = await (await fetch(`/api/inbound_split?start_day=${a}&end_day=${b}`)).json(); }
    catch (e) { d = { error: "Couldn't reach the server." }; }
    if (token !== ibToken) return;
    if (!d || d.error || !d.rows) { $("#ib-table").replaceChildren(noData((d && d.error) || "No data.")); return; }
    renderInboundSplit(d);
  }, "pay_this");

  // Agent View — same picker drives the comparison window
  PK.av = makeRangePicker($("#av-picker"), async (a, b) => {
    AV.range = [a, b];
    await loadAgentView();
  }, "pay_this");

  // Agent Scorecard — with sold/serviced mode toggle
// {agent: quality_score} for a range; empty on any failure so callers degrade
// to "no penalty / –" rather than blocking. Cache-first server-side.
async function fetchQualityByAgent(a, b) {
  try {
    const q = await (await fetch(`/api/quality?start_day=${a}&end_day=${b}`)).json();
    const out = {};
    for (const [name, v] of Object.entries((q && q.agents) || {})) out[name] = v.quality;
    return out;
  } catch (e) { return {}; }
}

let _scQualToken = 0;
async function loadScorecardQuality(d, a, b, mode) {
  const token = ++_scQualToken;
  let q = null;
  try { q = await (await fetch(`/api/quality?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { return; }
  if (token !== _scQualToken || !q || !q.agents) return;   // user changed range
  for (const r of d.rows) {
    const hit = q.agents[r.agent];
    r.quality_score = hit ? hit.quality : null;
  }
  if (d.team && q.teams) d.team.quality_score = q.teams.total ?? null;
  renderScorecard(d, mode);
}

  document.addEventListener("click", (e) => {
    for (const id of ["#sc-agents", "#sc-cols"]) {
      const m = $(id);
      if (m && m.open && !m.contains(e.target)) m.open = false;
    }
  });
  const scMode = makeModeToggle($("#sc-mode"), () => PK.sc.fire());
  PK.sc = makeRangePicker($("#sc-picker"), async (a, b) => {
    $("#sc-table").replaceChildren(noData("Loading…"));
    const d = await (await fetch(
      `/api/scorecard?start_day=${a}&end_day=${b}&mode=${scMode.get()}`)).json();
    renderScorecard(d, scMode.get());
    loadScorecardQuality(d, a, b, scMode.get());  // VoiceOps fills its column in
    loadScorecardCalls(d, a, b, scMode.get());    // Genesys fills its four columns in
    loadScorecardMM(d, a, b, scMode.get());       // Master Metric fills MM in
  }, "pay_this", { fullPayPeriod: true });

let _scCallsToken = 0;
async function loadScorecardCalls(d, a, b, mode) {
  const token = ++_scCallsToken;
  let c = null;
  try { c = await (await fetch(`/api/scorecard_calls?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { return; }
  if (token !== _scCallsToken || !c || !c.agents) return;   // user changed range
  for (const r of d.rows) {
    const hit = c.agents[r.agent] || {};
    for (const k of SC_CALL_KEYS) r[k] = hit[k] ?? null;
  }
  if (d.team && c.team) for (const k of SC_CALL_KEYS) d.team[k] = c.team[k] ?? null;
  renderScorecard(d, mode);
}

let _scMMToken = 0;
async function loadScorecardMM(d, a, b, mode) {
  const token = ++_scMMToken;
  let c = null;
  try { c = await (await fetch(`/api/scorecard_mm?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { return; }
  if (token !== _scMMToken || !c || !c.agents) return;     // user changed range
  for (const r of d.rows) r.mm = c.agents[r.agent] ?? null;
  if (d.team) d.team.mm = c.team ?? null;
  renderScorecard(d, mode);
}

let _tmQualToken = 0;
async function loadTeamQuality(d, a, b) {
  const token = ++_tmQualToken;
  let q = null;
  try { q = await (await fetch(`/api/quality?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { return; }
  if (token !== _tmQualToken || !q || !q.teams) return;   // user changed range
  for (const t of ["total", "josh", "kai"]) {
    if (d[t]) d[t].quality_score = q.teams[t] ?? null;
  }
  renderTeamMetrics(d);
}

  // Team Metrics — sold/serviced toggle + range picker
  const tmMode = makeModeToggle($("#tm-mode"), () => PK.tm.fire());
  PK.tm = makeRangePicker($("#tm-picker"), async (a, b) => {
    $("#tm-table").replaceChildren(noData("Loading…"));
    const d = await (await fetch(
      `/api/teammetrics?start_day=${a}&end_day=${b}&mode=${tmMode.get()}`)).json();
    renderTeamMetrics(d);
    loadTeamQuality(d, a, b);  // VoiceOps fills the Quality Score row in
  }, "last_week");

  // Power Score — same toggle
  const psMode = makeModeToggle($("#ps-mode"), () => PK.ps.fire());
  PK.ps = makeRangePicker($("#ps-picker"), async (a, b) => {
    $("#ps-table").replaceChildren(noData("Loading…"));
    // the total depends on every rule's metric, so wait for all the feeds it needs
    const need = psNeeds();
    const j = (u) => fetch(u).then((r) => r.json()).catch(() => null);
    const [d, qual, calls, mm] = await Promise.all([
      fetch(`/api/scorecard?start_day=${a}&end_day=${b}&mode=${psMode.get()}`).then((r) => r.json()),
      fetchQualityByAgent(a, b),
      SC_CALL_KEYS.some((k) => need.has(k)) ? j(`/api/scorecard_calls?start_day=${a}&end_day=${b}`) : null,
      need.has("mm") ? j(`/api/scorecard_mm?start_day=${a}&end_day=${b}`) : null,
    ]);
    for (const r of d.rows) {
      r.quality_score = qual[r.agent] ?? null;
      if (calls && calls.agents) for (const k of SC_CALL_KEYS) r[k] = (calls.agents[r.agent] || {})[k] ?? null;
      if (mm && mm.agents) r.mm = mm.agents[r.agent] ?? null;
    }
    renderPowerScore(d, psMode.get());
  }, "pay_this", { fullPayPeriod: true });
  if (!$("#ps-export")) {
    const xb = document.createElement("button");
    xb.id = "ps-export"; xb.className = "nav-btn";
    xb.textContent = "⬇ Download Excel";
    xb.title = "Excel workbook: tab 1 = this table exactly as shown; tab 2 = every PestRoutes account " +
      "behind it for this date range and Sold/Serviced mode, with every field we store.";
    xb.addEventListener("click", () => exportPowerScore(xb));
    $("#ps-picker").after(xb);
  }

  // Audit vs System — computed for any range from the same engine
  PK.au = makeRangePicker($("#au-picker"), async (a, b) => {
    $("#audit-body").replaceChildren(noData("Loading…"));
    const d = await fetchRange(a, b);
    renderAuditRange(d);
  });

  // Leaked Leads
  PK.lk = makeRangePicker($("#lk-picker"), async (a, b) => {
    $("#lk-summary").replaceChildren(noData("Loading…"));
    $("#lk-detail").replaceChildren();
    const d = await (await fetch(
      `/api/leaked?start_day=${a}&end_day=${b}`)).json();
    renderLeaked(d);
  });

  // Sources (marketing)
  PK.src = makeRangePicker($("#src-picker"), async (a, b) => {
    SRC.range = [a, b];
    await loadSources();
  });
  // IS Managers: Timesheets
  PK.ts = makeRangePicker($("#ts-picker"), (a, b) => loadTimesheet(a, b), "pay_this");
  PK.adh = makeRangePicker($("#adh-picker"), (a, b) => loadAdherence(a, b), "pay_this");
  // Marketing: By Source (Lead Volume and Spend & ROAS have fixed windows)
  PK.mb = makeRangePicker($("#mb-picker"), (a, b) => loadMk("bysource", a, b));
  // Marketing: Lead Volume -> Leads by hour (the year-over-year charts above it have fixed windows)
  PK.mhr = makeRangePicker($("#mhr-picker"), (a, b) => loadMkHourly(a, b), "t7");

}

function renderAuditRange(d) {
  const p = (v) => v == null ? "–" : v.toFixed(1);
  const rows = d.rows.map((r) => {
    const denom = Math.max((r.audit_qual || 0) + (r.hidden || 0) + (r.leaked || 0),
                           r.conversions || 0);
    return {
      agent: r.agent === "TEAM" ? "TEAM TOTAL" : r.agent,
      audit_qualified: r.audit_qual, sys_qualified: r.qual_leads,
      dispo_accuracy: r.audit_qual ? Math.round(1000 * r.qual_leads / r.audit_qual) / 10 : null,
      audit_sales: r.audit_sales, sys_conversions: r.conversions,
      hidden: r.hidden, leaked: r.leaked,
      audit_conv_pct: r.audit_qual ? Math.round(1000 * r.audit_sales / r.audit_qual) / 10 : null,
      adj_conv_pct: r.audit_qual ? Math.round(1000 * (r.conversions || 0) / denom) / 10 : null,
      sys_unqualified: r.unqualified,
    };
  }).sort((x, y) => (x.dispo_accuracy ?? 999) - (y.dispo_accuracy ?? 999));
  const cols = [
    ["agent", "Agent"],
    ["audit_qualified", "Audit Qual", "Qualified calls per the audit team's sheet"],
    ["sys_qualified", "System Qual", "Unique qualified callers per Genesys dispositions + conversions"],
    ["dispo_accuracy", "Dispo Acc%", "System qualified ÷ audited qualified — under 100 suggests the agent marks real leads unqualified"],
    ["audit_sales", "Audit Sales", "Sales per the audit team's sheet"],
    ["sys_conversions", "System Conv", "Unique callers who became customers (FieldRoutes-verified)"],
    ["hidden", "Hidden", "Bought from the agent, never dispositioned Closed/Follow Up"],
    ["leaked", "Leaked", "Marked unqualified, bought from a DIFFERENT seller within 14 days"],
    ["audit_conv_pct", "Audit Cv%", "Audit sales ÷ audit qualified (the audit team's own rate)"],
    ["adj_conv_pct", "Honest%", "GOLD STANDARD: verified conversions ÷ (audited qualified + hidden + leaked)"],
    ["sys_unqualified", "Sys Unq Calls", "Calls the agent dispositioned unqualified"],
  ];
  $("#audit-body").replaceChildren(simpleTable(cols, rows, {
    hl: new Set(["adj_conv_pct"]),
    fmt: { dispo_accuracy: p, audit_conv_pct: p, adj_conv_pct: p },
    flag: (r, key) => key === "dispo_accuracy" && r.dispo_accuracy != null
      ? (r.dispo_accuracy < 80 ? "flag-bad" : "flag-ok") : null,
    sortable: true, defaultSort: "audit_qualified",
    pin: (r) => r.agent === "TEAM TOTAL",
  }), rangeCaption(d));
}

function renderLeaked(d) {
  $("#lk-summary").replaceChildren(simpleTable(
    [["agent", "Agent"],
     ["leaked", "Leaked (unqualified)", "Callers this agent marked unqualified who bought from a different seller within 14 days"],
     ["cc_leaked", "Leaked (other/current-cust)", "Callers with other dispositions (transfers, no wrap-up) who bought elsewhere — often current-customer upsells"],
     ["hidden", "Hidden conversions", "Callers who bought from THIS agent without any Closed/Follow Up disposition"]],
    d.summary, { sortable: true, defaultSort: "leaked",
                 hl: new Set(["leaked"]) }));
  $("#lk-detail").replaceChildren(simpleTable(
    [["call_date", "Call"], ["agent", "Agent"], ["phone", "Phone"],
     ["wrapup", "Wrap-up"], ["kind", "Type"], ["sold_by", "Sold By"],
     ["sub_added", "Sub Added"]],
    d.rows, {}));
}

/* ---------- power score ---------- */
// Weights come from /api/config (editable in Settings). These defaults are the
// fallback until that fetch lands, and the shape the editor writes back.
// Rules come from /api/config (editable in Settings). v2 shape: points earned
// (a ladder per metric), penalties and rank bonuses, each on any IS #'s metric.
const PS_DEFAULT = {
  version: 2,
  points: [
    { metric: "conversion_pct", scale: [[75, 8], [70, 6], [66, 4], [63, 2], [55, 1]] },
    { metric: "acv", scale: [[760, 5], [740, 4], [720, 3], [700, 2], [675, 1]] },
    { metric: "multi_total_pct", scale: [[70, 5], [65, 4], [60, 3], [57.5, 2], [53.75, 1]] },
    { metric: "apay_pct", scale: [[95, 5], [90, 4], [88, 3], [85, 2], [80, 1]] },
  ],
  penalties: [
    { metric: "quality_score", threshold: 67, penalty: -2 },
    { metric: "multi24_pct", threshold: 15, penalty: -2 },
    { metric: "monthly_pct", threshold: 60, penalty: -2 },
  ],
  bonuses: [{ metric: "rev", points: [3, 2, 1] }],
};
// live config, populated by loadConfig(); tiers arrive via /api/scorecard too.
let PS_LAST = null;   // the last rendered Power Score (range, mode, rows, columns) — for the Excel export
const CONFIG = { power_score: PS_DEFAULT, scorecard_tiers: null };
function psCfg() { const c = CONFIG.power_score; return (c && c.version === 2) ? c : PS_DEFAULT; }
const PS_LOWER = new Set(["avg_acw_s", "calls_missed"]);       // lower is better: "at most" / penalty "above"
const PS_LABEL = { rev: "Revenue", multi24_pct: "Multi-yr 24", conversion_pct: "Conversion", multi_total_pct: "Total Multi-Yr" };
function psMetric(key) {
  const c = SC_COLS.find((x) => x.key === key) || { key, label: key, fmt: "num2" };
  return { ...c, lowerBetter: PS_LOWER.has(key) || !!c.lowerBetter };
}
function psLabel(key) { return PS_LABEL[key] || psMetric(key).label; }
function psMetricKeys() { return SC_COLS.filter((c) => c.key !== "agent").map((c) => c.key); }
// every metric a rule uses — the Power Score tab fetches the slow feeds (calls, MM) only when needed
function psNeeds() {
  const cfg = psCfg();
  return new Set(["points", "penalties", "bonuses"].flatMap((g) => (cfg[g] || []).map((x) => x.metric)));
}
function psThr(key, v) {
  const m = psMetric(key), pre = m.lowerBetter ? "≤ " : "";
  if (m.fmt === "pct") return pre + v + "%";
  if (m.fmt === "money0" || m.fmt === "money2") return pre + "$" + v;
  if (m.fmt === "ms") return pre + scFmt(v, "ms");
  return pre + v;
}

// Metrics are rounded to the nearest whole number (.5 rounds up) before they
// are scored, like the old IS #'s sheet: 59.5% counts as 60%, $759.50 as $760
// (managers, 2026-10-26). Two-decimal ratios (Ans:Missed, MM) are scored as-is.
const PS_ROUND_FMTS = new Set(["pct", "money0", "money2", "num1", "ms", "int"]);
function psRound(key, v) {
  if (v == null || !PS_ROUND_FMTS.has(psMetric(key).fmt)) return v;
  return Math.round(v);
}

function scaleScore(scale, v, lower) {
  if (v == null) return 0;
  for (const [t, p] of scale) if (lower ? v <= t : v >= t) return p;
  return 0;
}

function computePowerScores(rows) {
  const cfg = psCfg();
  // rank bonuses: 1st gets points[0], 2nd points[1]…; ties share the higher rank;
  // a rep with no value (or 0 on a higher-is-better metric) never places
  const bonusFns = (cfg.bonuses || []).map((b) => {
    const lower = psMetric(b.metric).lowerBetter;
    const ok = (v) => v != null && (lower || v > 0);
    const ranked = rows.map((r) => r[b.metric]).filter(ok).sort((x, y) => (lower ? x - y : y - x));
    return (v) => (ok(v) ? b.points[ranked.indexOf(v)] || 0 : 0);
  });
  return rows.map((r) => {
    const s = {};
    (cfg.points || []).forEach((p, i) => { s["p" + i] = scaleScore(p.scale, psRound(p.metric, r[p.metric]), psMetric(p.metric).lowerBetter); });
    (cfg.penalties || []).forEach((p, i) => {
      const v = psRound(p.metric, r[p.metric]), lower = psMetric(p.metric).lowerBetter;
      // no data (unmapped rep, no graded calls, no recurring subs) -> no penalty
      s["n" + i] = (v != null && (lower ? v > p.threshold : v < p.threshold)) ? p.penalty : 0;
    });
    bonusFns.forEach((f, i) => { s["b" + i] = f(r[cfg.bonuses[i].metric]); });
    s.total = Object.values(s).reduce((a, b) => a + b, 0);
    return { ...r, s };
  }).sort((a, b) => b.s.total - a.s.total);
}

async function loadConfig() {
  try {
    const c = await (await fetch("/api/config")).json();
    if (c && c.power_score) CONFIG.power_score = c.power_score;
    if (c && c.scorecard_tiers) CONFIG.scorecard_tiers = c.scorecard_tiers;
  } catch (e) { /* keep defaults */ }
}

// Excel export: tab 1 = the rendered table (sent verbatim so it matches the
// screen exactly); tab 2 = the accounts behind it, built server-side.
async function exportPowerScore(btn) {
  if (!PS_LAST) { toast("Load the Power Score first", "err", 4000); return; }
  const { start, end, mode, rows, mets } = PS_LAST;
  const columns = ["#", "Sales Rep"], formats = ["", ""];
  for (const [, , label, fmt] of mets) { columns.push(label, label + " score"); formats.push(fmt, "score"); }
  columns.push("Power Score"); formats.push("score");
  const data = rows.map((r, i) => {
    const out = [i + 1, r.agent];
    for (const [vk, sk] of mets) out.push(r[vk] ?? null, r.s[sk] ?? 0);
    out.push(r.s.total);
    return out;
  });
  const old = btn.textContent; btn.disabled = true; btn.textContent = "Preparing…";
  try {
    const r = await fetch("/api/powerscore_export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_day: start, end_day: end, mode, columns, formats, rows: data }),
    });
    if (!r.ok) {
      let e = {}; try { e = await r.json(); } catch (_) { /* not JSON */ }
      toast("Export failed: " + (e.error || ("HTTP " + r.status)), "err", 6000);
      return;
    }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url; a.download = `PowerScore_${mode}_${start}_${end}.xlsx`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) {
    toast("Export failed: " + e, "err", 6000);
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

function renderPowerScore(d, mode, tableSel = "#ps-table", scalesSel = "#ps-scales", meName = "") {
  // outbound reps (Seyj/Micah) appear on the scorecard but not the power score
  const eligible = d.rows.filter((r) => r.powerscore);
  const rows = computePowerScores(eligible);
  const cfg = psCfg();
  // metric columns (value + score pairs), one per rule: points, then penalties, then bonuses
  const METS = [
    ...(cfg.points || []).map((p, i) => [p.metric, "p" + i, psLabel(p.metric), psMetric(p.metric).fmt]),
    ...(cfg.penalties || []).map((p, i) => [p.metric, "n" + i, psLabel(p.metric), psMetric(p.metric).fmt]),
    ...(cfg.bonuses || []).map((b, i) => [b.metric, "b" + i, psLabel(b.metric), psMetric(b.metric).fmt]),
  ];
  if (!meName) PS_LAST = { start: d.start, end: d.end, mode, rows, mets: METS };
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const t = document.createElement("table");
  t.className = "ps-table";
  let head = "<thead><tr><th>#</th><th>Sales Rep</th>";
  for (const [, , label] of METS) head += `<th>${label}</th><th class="ps-sc">Score</th>`;
  head += '<th class="ps-total">POWER<br>SCORE</th></tr></thead>';
  let body = "<tbody>";
  rows.forEach((r, i) => {
    body += `<tr${meName && r.agent === meName ? ' class="me-row"' : ""}><td class="dim">${i + 1}</td><td>${r.agent}</td>`;
    for (const [vk, sk, , fmt] of METS) {
      body += `<td>${scFmt(r[vk], fmt)}</td><td class="ps-sc">${fmtScore(r.s[sk])}</td>`;
    }
    body += `<td class="ps-total">${r.s.total.toFixed(1)}</td></tr>`;
  });
  body += "</tbody>";
  t.innerHTML = head + body;
  wrap.appendChild(t);

  // scales reference
  const ref = document.createElement("div");
  ref.className = "ps-scales-box";
  ref.innerHTML =
    (cfg.points || []).map((p) => `<div class="ps-scale"><b>${psLabel(p.metric)}</b><table><tbody>` +
      p.scale.map(([t, pts]) => `<tr><td>${psThr(p.metric, t)}</td><td>${pts}</td></tr>`).join("") + `</tbody></table></div>`).join("") +
    (cfg.penalties || []).map((p) => `<div class="ps-scale"><b>${psLabel(p.metric)}</b><div class="ps-note">${p.penalty} if ` +
      `${psMetric(p.metric).lowerBetter ? "above" : "below"} ${psThr(p.metric, p.threshold).replace("≤ ", "")}, else 0</div></div>`).join("") +
    (cfg.bonuses || []).map((b) => `<div class="ps-scale"><b>${psLabel(b.metric)}</b><div class="ps-note">by rank in the range: ` +
      `${b.points.map((p, i) => `#${i + 1} = ${p}`).join(", ")}; everyone else 0</div></div>`).join("");

  const nMetrics = METS.length;
  const maxTotal = (cfg.points || []).reduce((a, p) => a + Math.max(0, ...p.scale.map((x) => x[1])), 0) +
    (cfg.bonuses || []).reduce((a, b) => a + Math.max(0, ...b.points), 0);
  const bonusTxt = (cfg.bonuses || []).map((b) => `the top ${b.points.length} by ${psLabel(b.metric)} get ${b.points.join("/")} points`).join("; ");
  const cap = rangeCaption(d);
  const modeTxt = mode === "serviced" ? "initial-service-date" : "sale-date";
  setTip(cap, `${d.start} – ${d.end} (${modeTxt} in range). Total = ${nMetrics} ` +
    `metrics (max ${maxTotal}). ${bonusTxt ? "Rank bonus: " + bonusTxt + ". " : ""}` +
    `Each metric is rounded to the nearest whole number before scoring (.5 rounds up — 59.5% counts as 60%); ` +
    `Ans:Missed and MM are scored at two decimals. Ordered highest to lowest for lead distribution. ` +
    `Excludes mosquito-upsell subs; Quality Score is VoiceOps AI grading of calls in the range. ` +
    (meName ? "" : `Weights are editable in Settings.`));
  $(tableSel).replaceChildren(wrap);
  if (meName) {
    // phone layout (agent page): rank / rep / score; tap a rep for the breakdown
    wrap.classList.add("me-wide");
    const sw = document.createElement("div"); sw.className = "table-wrap me-stack";
    const stt = document.createElement("table");
    stt.innerHTML = "<thead><tr><th>#</th><th style='text-align:left'>Sales Rep</th><th>Power Score</th></tr></thead>";
    const stb = document.createElement("tbody");
    rows.forEach((r, i) => {
      const isMe = r.agent === meName;
      const tr = document.createElement("tr");
      tr.className = "me-rank" + (isMe ? " me-row open" : "");
      const c1 = document.createElement("td"); c1.className = "dim"; c1.textContent = i + 1;
      const c2 = document.createElement("td"); c2.textContent = r.agent;
      const c3 = document.createElement("td"); c3.textContent = r.s.total.toFixed(1);
      tr.append(c1, c2, c3);
      const det = document.createElement("tr"); det.className = "me-detail";
      det.style.display = isMe ? "" : "none";
      const dc = document.createElement("td"); dc.colSpan = 3;
      const grid = document.createElement("div"); grid.className = "me-detail-grid";
      for (const [vk, sk, label, fmt] of METS) {
        const a = document.createElement("span"); a.textContent = label;
        const b = document.createElement("b"); b.textContent = scFmt(r[vk], fmt);
        const c = document.createElement("i"); const sc = r.s[sk];
        c.textContent = (sc > 0 ? "+" : "") + (sc % 1 === 0 ? sc : sc.toFixed(1)) + " pts";
        if (sc < 0) c.className = "neg";
        grid.append(a, b, c);
      }
      dc.appendChild(grid); det.appendChild(dc);
      tr.addEventListener("click", () => {
        const open = det.style.display === "none";
        det.style.display = open ? "" : "none";
        tr.classList.toggle("open", open);
      });
      stb.append(tr, det);
    });
    stt.appendChild(stb); sw.appendChild(stt);
    $(tableSel).appendChild(sw);
  }
  $(scalesSel).replaceChildren(ref, cap);
}

function fmtScore(v) {
  if (!v) return v === 0 ? "0" : "–";
  const cls = v < 0 ? "neg" : "";
  return `<span class="${cls}">${v % 1 === 0 ? v : v.toFixed(1)}</span>`;
}

function scaleTable(title, scale, unit) {
  const rows = scale.map(([t, p]) =>
    `<tr><td>${unit === "$" ? "$" + t : t + "%"}</td><td>${p}</td></tr>`).join("");
  return `<div class="ps-scale"><b>${title}</b><table><tbody>${rows}</tbody></table></div>`;
}

/* ---------- Agent Metrics: Master Metric ---------- */
const MM_COLS = [
  { key: "agent", label: "Agent" },
  { key: "avail_ob_rate", label: "Available Outbound Rate", fmt: "n2", podium: true,
    tip: "Total OBs ÷ Available Hours" },
  { key: "mm", label: "MM", fmt: "n2", podium: true, cls: "mm-key",
    tip: "Master Metric = Calls/Hr + OB/Hr" },
  { key: "ans_missed", label: "Ans:Missed", fmt: "n2",
    tip: "Answered ÷ Alert-No Answer (Genesys)" },
  { key: "inbound_util", label: "Inbound Utilization", fmt: "pct",
    tip: "Total Talk ÷ Hours" },
  { key: "answered", label: "Answered", fmt: "int", tip: "Genesys: inbound calls answered" },
  { key: "missed", label: "Alert - No Answer", fmt: "int",
    tip: "Genesys: calls that rang this agent and weren't picked up" },
  { key: "hours", label: "Hours", fmt: "n2", tip: "Hours worked: the dashboard time clock from Oct 26, 2026 (today live); Homebase hours Oct 5–25; scheduled hours before Oct 5" },
  { key: "avail_hours", label: "Available Hours", fmt: "n2", tip: "Hours − Total Talk" },
  { key: "total_obs", label: "Total OBs", fmt: "int", podium: true,
    tip: "Podium conversations + Podium calls made" },
  { key: "calls_hr", label: "Calls/Hr", fmt: "n2", tip: "Answered ÷ Hours" },
  { key: "ob_hr", label: "OB/Hr", fmt: "n2", podium: true, tip: "Total OBs ÷ Hours" },
  { key: "conversations", label: "Total Conversations", fmt: "int", podium: true, tip: "Podium" },
  { key: "calls_made", label: "Total Calls Made", fmt: "int", podium: true, tip: "Podium" },
  { key: "avg_talk_s", label: "Avg Talk", fmt: "ms", tip: "Genesys: average talk time per inbound call" },
  { key: "avg_acw_s", label: "Avg ACW", fmt: "ms", tip: "Genesys: average after-call work" },
  { key: "total_talk_h", label: "Total Talk (hrs)", fmt: "n2", tip: "Genesys: inbound talk time, in hours" },
];

function mmFmt(v, fmt) {
  if (v == null) return "–";
  if (fmt === "pct") return v.toFixed(2) + "%";
  if (fmt === "n2") return v.toFixed(2);
  if (fmt === "int") return Math.round(v).toLocaleString();
  if (fmt === "ms") return `${Math.floor(v / 60)}:${String(Math.round(v % 60)).padStart(2, "0")}`;
  return v;
}

function renderMasterMetric(d) {
  const notes = [];
  const pending = d.pending || [];
  if (!d.podium_calls) notes.push("Podium isn't connected yet, so the outbound columns (Total OBs, OB/Hr, " +
    "Conversations, Calls Made), MM and Available Outbound Rate are blank for now.");
  else if (pending.includes("conversations")) notes.push("Total Calls Made is live from Podium. Total " +
    "Conversations is collected each morning for the day before and none is loaded for these dates yet, " +
    "so Total OBs, OB/Hr, MM and Available Outbound Rate are blank.");
  notes.push("Hours are hours worked on the dashboard time clock from Oct 26, 2026 (today counts live). " +
    "Oct 5–25 keep the Homebase hours and days before Oct 5 the scheduled hours they had.");
  for (const n of d.notes || []) notes.push(n);
  $("#mm-notes").replaceChildren();          // notes stay off the tab (Dallin, 2026-10-08)

  const st = { key: pending.includes("mm") ? "calls_hr" : "mm", asc: false };
  const wrap = document.createElement("div"); wrap.className = "table-wrap";
  const table = document.createElement("table");
  const draw = () => {
    const rows = d.rows.slice().sort((x, y) => {
      const a = x[st.key], b = y[st.key];
      if (a == null) return 1; if (b == null) return -1;
      const c = typeof a === "string" ? a.localeCompare(b) : a - b;
      return st.asc ? c : -c;
    });
    const thead = document.createElement("thead"); const hr = document.createElement("tr");
    for (const col of MM_COLS) {
      const th = document.createElement("th");
      th.textContent = col.label; if (col.tip) th.title = col.tip;
      if (pending.includes(col.key)) th.classList.add("mm-pending");
      if (st.key === col.key) th.classList.add("sorted", ...(st.asc ? ["asc"] : []));
      th.addEventListener("click", () => {
        if (st.key === col.key) st.asc = !st.asc;
        else { st.key = col.key; st.asc = col.key === "agent"; }
        draw();
      });
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    const tbody = document.createElement("tbody");
    const line = (r, cls) => {
      const tr = document.createElement("tr"); if (cls) tr.className = cls;
      for (const col of MM_COLS) {
        const td = document.createElement("td");
        td.textContent = col.key === "agent" ? r.agent : mmFmt(r[col.key], col.fmt);
        if (col.cls) td.classList.add(col.cls);
        if (pending.includes(col.key)) td.classList.add("mm-pending");
        tr.appendChild(td);
      }
      return tr;
    };
    for (const r of rows) tbody.appendChild(line(r));
    if (d.team) tbody.appendChild(line(d.team, "total-row"));
    table.replaceChildren(thead, tbody);
  };
  draw();
  wrap.appendChild(table);
  $("#mm-table").replaceChildren(d.rows.length ? wrap : noData("No calls or hours in this range."));
  noteInto("#mm-foot", `${d.start} – ${d.through} (Pacific days` +
    (d.through !== d.end ? `; the range runs to ${d.end} but nothing after today is counted` : "") +
    `). Genesys calls and talk time, live. Hours: the dashboard time clock from Oct 26, 2026 (today live; frozen Homebase hours Oct 5–25 and scheduled hours ` +
    `before Oct 5; outbound reps 8h Mon–Fri). Podium calls made: live; Podium conversations: collected each morning for the ` +
    `day before. Calls/Hr = Answered ÷ Hours · Inbound Utilization = Total Talk ÷ Hours · Available Hours = Hours − Total Talk · ` +
    `MM = Calls/Hr + OB/Hr. ` + (d.notes || []).join(" "));
}

/* ---------- Agent Metrics -> Inbound vs Other ---------- */
function renderInboundSplit(d) {
  const money = (v) => (v == null ? "–" : "$" + Math.round(v).toLocaleString());
  const pct = (v) => (v == null ? "–" : v.toFixed(1) + "%");
  const int = (v) => (v == null ? "–" : Math.round(v).toLocaleString());
  const cols = [
    ["agent", "Agent", ""],
    ["total", "Total Revenue", "Contract value of every serviced + pending sale dated in the range — upsells, mosquito and renewals all included"],
    ["inbound", "From Inbound Calls", "Revenue from sales tied to an inbound call this agent took"],
    ["other", "Other", "Total − inbound: outbound, referrals, chats, and inbound sales the matcher couldn't tie to a call"],
    ["pct_inbound", "% Inbound", "Inbound ÷ Total"],
    ["n_subs", "Sales", "Number of sales in Total"],
    ["n_inbound", "Inbound Sales", "Sales tied to the agent's own inbound call"],
    ["n_other", "Other Sales", "Sales − Inbound Sales"],
  ];
  const rows = d.rows.slice();
  if (d.team) rows.push({ ...d.team, _team: true });
  $("#ib-table").replaceChildren(simpleTable(cols, rows, {
    fmt: { total: money, inbound: money, other: money, pct_inbound: pct, n_subs: int, n_inbound: int, n_other: int },
    hl: new Set(["pct_inbound"]), sortable: true, defaultSort: "total", pin: (r) => r._team,
  }));
  const notes = [
    "How a sale is tied to an inbound call: the caller's phone number is the account's phone, the sale is in " +
    "that agent's name, and it was added within 14 days of the call (the dashboard's call matcher, re-run on " +
    "every sync). The earliest qualifying call gets the credit. This is a floor — a customer who called from " +
    "one number and signed up under another isn't matched — so “Other” is a ceiling.",
    ...(d.notes || []),
  ];
  $("#ib-notes").replaceChildren();
  noteInto("#ib-foot", `${d.start} – ${d.end} · sales by date sold · tracked agents, credited by FieldRoutes employee id` +
    (d.calls_from ? ` · call history on file from ${d.calls_from}` : "") + ". " + notes.join(" "));
}

/* ---------- Team Overview cards (whole team / Josh / Kai) ----------
   The same card layout as Home: label | last 30 days | last 7 days, with the
   13-week trend underneath. One fetch covers all three teams; the toggle just
   re-draws. The any-dates table below it is the original Team Metrics view. */
const TO = { data: null, loading: false };
const TO_METRICS = [
  // the Agent Metrics (IS #'s) sheet, in the same order, per team
  { key: "phone_conversion", name: "Conversion", sub: "audit team's sales ÷ qualified", kind: "pct", color: "#2563eb" },
  { key: "acv", name: "ACV", sub: "avg contract value per counted subscription", kind: "money", color: "#d98b1f" },
  { key: "multiyear_pct", name: "Total Multi-Year", sub: "18mo+ agreements", kind: "pct", color: "#7048ba" },
  { key: "multi24_pct", name: "Multi-Yr 24mo", sub: "24mo+ agreements", kind: "pct", color: "#9333ea" },
  { key: "total_revenue", name: "Revenue", sub: "contract value sold · upsells in, renewals out", kind: "money", color: "#0d9488" },
  { key: "total_sales", name: "Total Sales", sub: "subscriptions sold · upsells in, renewals out", kind: "count", color: "#1e3659" },
  { key: "outbound_sales", name: "Outbound Sales", sub: "of Total Sales, not tied to an inbound call the rep took", kind: "count", color: "#b45309" },
  { key: "sts_pct", name: "Sold → Serviced", sub: "serviced ÷ (serviced + cancelled + no-show)", kind: "pct", color: "#5b6b7c" },
  { key: "apay_pct", name: "APAY", sub: "customers with a card on file", kind: "pct", color: "#0891b2" },
  { key: "multi18_pct", name: "Multi-Yr 18mo", sub: "18-to-23-month agreements", kind: "pct", color: "#a855f7" },
  { key: "avg_initial", name: "AIP", sub: "avg first-service price", kind: "money", color: "#c0396f" },
  { key: "signed_doc_pct", name: "Signed Agr %", sub: "signed contracts per counted subscription", kind: "pct", color: "#b3323c" },
  { key: "quality_score", name: "Quality Score", sub: "VoiceOps AI grading of calls", kind: "num", color: "#0e7490" },
  { key: "monthly_billing_pct", name: "Monthly Billing %", sub: "recurring subs billed monthly", kind: "pct", color: "#65a30d" },
  { key: "calls_taken", name: "Calls Taken", sub: "Genesys: inbound calls answered", kind: "count", color: "#4f46e5" },
  { key: "calls_missed", name: "Calls Missed", sub: "Genesys: rang and weren't picked up", kind: "count", color: "#dc2626", lowerBetter: true },
  { key: "ans_missed", name: "Ans:Missed", sub: "calls answered for every one missed", kind: "num", color: "#0f766e" },
  { key: "avg_talk_s", name: "Avg Talk", sub: "Genesys: average talk time per inbound call", kind: "time", color: "#475569" },
  { key: "avg_acw_s", name: "Avg ACW", sub: "Genesys: average after-call work", kind: "time", color: "#b45309", lowerBetter: true },
];

async function loadTeamOverview() {
  if (TO.data || TO.loading) { if (TO.data) renderTeamOverview(); return; }
  TO.loading = true;
  $("#to-cards").replaceChildren(noData("Loading the team overview…"));
  try {
    const d = await (await fetch("/api/team_overview")).json();
    if (d && d.teams) TO.data = d;
    else $("#to-cards").replaceChildren(noData((d && d.error) || "Couldn't load the team overview."));
  } catch (e) {
    $("#to-cards").replaceChildren(noData("Couldn't load the team overview."));
  }
  TO.loading = false;
  if (TO.data) renderTeamOverview();
}

// Josh's team on the left, Kai's on the right, one metric per row so the two
// can be compared straight across. Whole-team-only metrics (forms, all leads)
// live in the any-dates table underneath.
function renderTeamOverview() {
  const d = TO.data;
  const fmt = (m, v) => v == null ? "–"
    : m.kind === "pct" ? v.toFixed(1) + "%"
    : m.kind === "money" ? "$" + Math.round(v).toLocaleString()
    : m.kind === "count" ? Math.round(v).toLocaleString()
    : m.kind === "time" ? `${Math.floor(v / 60)}:${String(Math.round(v % 60)).padStart(2, "0")}`
    : v.toFixed(1);
  const card = (m, teamKey, teamLabel) => {
    const t = d.teams[teamKey] || {}, x = t[m.key] || {};
    const win = (cur, prev) => ({
      v: x[cur], prev: x[prev], range: d.ranges[cur], prev_range: d.ranges[prev],
      sales: (t._sales || {})[cur] });
    const relative = m.kind === "money" || m.kind === "count" || m.kind === "time";   // % change vs point change
    const row = metricRow({
      key: m.key, name: m.name, sub: m.sub, color: m.color, lowerBetter: m.lowerBetter,
      d30: win("d30", "p30"), d7: win("d7", "p7"),
      val: (w) => fmt(m, w.v),
      delta: (w) => (w.v == null || w.prev == null) ? null
        : relative ? (w.prev ? ((w.v - w.prev) / w.prev) * 100 : null) : w.v - w.prev,
      unit: relative ? "%" : " pts",
      detail: (w) => (m.key === "total_sales" || w.sales == null) ? "" : `${w.sales.toLocaleString()} sales`,
      chartKind: m.kind === "pct" ? true : m.kind === "money" ? false
        : m.kind === "time" ? "time" : m.kind === "num" ? "num" : "count",
    }, x.series || [], d.weeks || []);
    const tag = document.createElement("span");
    tag.className = "to-tag to-" + teamKey; tag.textContent = teamLabel;
    row.querySelector(".metric-name b").prepend(tag);
    return row;
  };
  const head = (label, cls) => {
    const h = document.createElement("div"); h.className = "to-colhead " + cls; h.textContent = label; return h;
  };
  const out = [head("Josh's team", "to-josh"), head("Kai's team", "to-kai")];
  for (const m of TO_METRICS) {
    out.push(card(m, "josh", "Josh"), card(m, "kai", "Kai"));
  }
  $("#to-cards").replaceChildren(...out);
}

/* ---------- team metrics (Total / Josh / Kai) ---------- */
function renderTeamMetrics(d) {
  const pct = (v) => v == null ? "–" : v.toFixed(1) + "%";
  const money0 = (v) => v == null ? "–" : "$" + Math.round(v).toLocaleString();
  const money2 = (v) => v == null ? "–" : "$" + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const num = (v) => v == null ? "–" : v.toLocaleString();
  const num1 = (v) => v == null ? "–" : v.toFixed(1);
  const mmss = (sec) => sec == null ? "–" :
    Math.floor(sec / 60) + ":" + String(Math.round(sec % 60)).padStart(2, "0");

  // section header rows + metric rows. teamOnly rows only fill the Total column.
  const SECTIONS = [
    ["Leads & Closing", [
      ["phone_conversion", "Phone Conversion", pct, false, "Audit team's sheet: sales ÷ qualified"],
      ["form_conversion", "Podium (Form) Conversion", pct, true, "Form leads (by date in) that became a subscription"],
      ["total_leads_conversion", "Total Leads Conversion", pct, true, "Audit + form sales ÷ audit qualified + net form leads"],
      ["total_revenue", "Total Revenue", money0, false, "Sum of contract value, counted subs"],
      ["total_sales", "Total Sales", num, false, "Count of counted subscriptions sold"],
    ]],
    ["Sales Quality (excl. upsells)", [
      ["multiyear_pct", "Multiyear %", pct, false, "18-month+ agreements"],
      ["multi24_pct", "24 Months %", pct, false, "24-month+ agreements"],
      ["acv", "Av Contract Value", money2, false, "Average contract value"],
      ["avg_initial", "Av Initial Price", money2, false, "Average first-service price"],
      ["apay_pct", "Apay", pct, false, "% of counted subs whose customer has a card on file"],
      ["monthly_billing_pct", "Monthly Billing %", pct, false, "% of recurring subs that bill monthly"],
      ["signed_doc_pct", "Signed Doc %", pct, false, "Signed agreement — approximated by contractAdded (see note)"],
    ]],
    ["Call Quality (VoiceOps)", [
      ["quality_score", "Quality Score", num1, false,
       "VoiceOps AI grading of inside-sales calls (0-100), call-weighted across "
       + "the agents mapped in Settings. Strict Sunday-Saturday Pacific days."],
    ]],
  ];
  const PHONE = [
    ["service_level", "Service Level — 80% @ 30s", pct],
    ["avg_acw_sec", "Average ACW", mmss],
    ["answer_pct", "Answer %", pct],
    ["abandon_pct", "Average Abandon", pct],
  ];

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const t = document.createElement("table");
  t.className = "tm-table";
  let html = "<thead><tr><th>Metric</th><th>Total Team</th>" +
    "<th>Josh's Team</th><th>Kai's Team</th></tr></thead><tbody>";
  const cell = (fmt, v) => `<td>${fmt(v)}</td>`;
  for (const [section, rows] of SECTIONS) {
    html += `<tr class="tm-section"><td colspan="4">${section}</td></tr>`;
    for (const [key, label, fmt, teamOnly, tip] of rows) {
      html += `<tr><td title="${tip}">${label}</td>`;
      html += cell(fmt, d.total[key]);
      html += teamOnly ? '<td class="dim">—</td><td class="dim">—</td>'
        : cell(fmt, d.josh[key]) + cell(fmt, d.kai[key]);
      html += "</tr>";
    }
  }
  // phone metrics — total only
  const pm = d.total.phone_metrics || {};
  html += '<tr class="tm-section"><td colspan="4">Phone Metrics (from Genesys — both inside-sales queues)</td></tr>';
  for (const [key, label, fmt] of PHONE) {
    html += `<tr><td>${label}</td><td>${pm.error ? "–" : fmt(pm[key])}</td>` +
      '<td class="dim">—</td><td class="dim">—</td></tr>';
  }
  html += "</tbody>";
  t.innerHTML = html;
  wrap.appendChild(t);

  $("#tm-table").replaceChildren(wrap);
  noteInto("#tm-table", `${d.start} – ${d.end}. FieldRoutes sales (serviced-or-pending initial, by sale date; retention ignored), ` +
    `the audit sheet's conversion, form leads bucketed by when they came in, Genesys queue stats. Podium / Total-Leads / Phone ` +
    `metrics are team-total only. Signed Doc % is approximate — the exact PestRoutes field isn't exposed via the API yet.`, true);
}

/* ---------- agent scorecard (the pay-period sheet) ---------- */
const SC_COLS = [
  { key: "agent", label: "Sales Rep", grp: "" },
  { key: "conversion_pct", label: "Conversion %", grp: "tier", fmt: "pct", tier: true,
    tip: "Audit sheet: sales ÷ qualified calls" },
  { key: "acv", label: "ACV", grp: "tier", fmt: "money0", tier: true,
    tip: "Average contract value of counted subscriptions" },
  { key: "multi_total_pct", label: "Total Multi-Year", grp: "tier", fmt: "pct", tier: true,
    tip: "18-month+ agreements as % of counted subs (18mo + 24mo buckets)" },
  { key: "multi24_pct", label: "Multi-Yr % 24mo", grp: "tier", fmt: "pct", tier: true,
    tip: "24-month+ agreements as % of counted subs" },
  { key: "rev", label: "Pending / Serviced Rev", grp: "other", fmt: "money0",
    tip: "Total contract value of serviced + pending subscriptions" },
  { key: "subs", label: "Total Sales", grp: "other", fmt: "int",
    tip: "How many subscriptions that revenue is — serviced + pending, upsells and mosquito upsells excluded" },
  { key: "outbound_subs", label: "Outbound Sales", grp: "other", fmt: "int",
    tip: "Of Total Sales, the ones NOT tied to an inbound call this agent took (a sale is tied to a call when " +
      "the caller's number is the account's phone, it's sold in the agent's name, and it was added within 14 days " +
      "of the call). Sales where the customer called from a different number land here too, so this is a ceiling." },
  { key: "sts_pct", label: "Sold → Serviced", grp: "other", fmt: "pct", tier: true,
    tip: "Of this agent's sales whose first service was decided: % that were actually serviced = "
       + "Serviced ÷ (Serviced + Cancelled + No Show). Pending and never-scheduled sales are left out." },
  { key: "apay_pct", label: "APAY", grp: "other", fmt: "pct", apay: true,
    tip: "% of counted subs whose customer has a card on file (green ≥ 90%)" },
  { key: "multi18_pct", label: "Multi-Yr % 18mo", grp: "other", fmt: "pct",
    tip: "18-to-23-month agreements as % of counted subs" },
  { key: "aip", label: "AIP", grp: "other", fmt: "money2", tier: true,
    tip: "Average initial service price of counted subs" },
  { key: "signed_pct", label: "Signed Agr %", grp: "other", fmt: "pct", tier: true,
    tip: "% of counted subs with a signed (COMPLETED) agreement in PestRoutes" },
  { key: "quality_score", label: "Quality Score", grp: "other", fmt: "num1", tier: true,
    tip: "VoiceOps AI grading of this agent's calls in the selected range (0-100). "
       + "Blank when VoiceOps graded no calls for them in the period." },
  { key: "monthly_pct", label: "Monthly Billing %", grp: "other", fmt: "pct", tier: true,
    tip: "% of recurring counted subs billed monthly (billing freq 30, or default billing on a 30-day service freq); one-time services excluded" },
  { key: "calls_taken", label: "Calls Taken", grp: "other", fmt: "int",
    tip: "Genesys: inbound calls this agent answered in the selected dates" },
  { key: "calls_missed", label: "Calls Missed", grp: "other", fmt: "int",
    tip: "Genesys: calls that rang this agent and weren't picked up (Alert - No Answer)" },
  { key: "ans_missed", label: "Ans:Missed", grp: "other", fmt: "num2", tier: true,
    tip: "Calls answered for every one missed (Calls Taken ÷ Calls Missed). Blank when none were missed." },
  { key: "avg_talk_s", label: "Avg Talk", grp: "other", fmt: "ms",
    tip: "Genesys: average talk time per inbound call" },
  { key: "avg_acw_s", label: "Avg ACW", grp: "other", fmt: "ms", tier: true, lowerBetter: true,
    tip: "Genesys: average after-call work per call (lower is better)" },
  { key: "mm", label: "MM", grp: "other", fmt: "num2", tier: true, goalMin: true,
    tip: "Master Metric = Calls/Hr + OB/Hr, the same number as the Master Metric tab" },
];

// What an agent should aim for in a column. Metrics the Power Score pays on a
// scale show the number that earns the most points; the two penalty-only
// Power Score metrics (Multi-yr 24, Quality) and everything else show the
// Tier 1 goal, falling back to the penalty line. null = nothing to aim at.
function scGoal(col, tiers) {
  // the best ladder threshold if the metric earns Power Score points, else
  // Tier 1, else a Power Score penalty line; null = nothing to aim at
  const ps = psCfg(), lower = psMetric(col.key).lowerBetter;
  const p = (ps.points || []).find((x) => x.metric === col.key);
  if (p && p.scale.length) { const ts = p.scale.map((x) => x[0]); return lower ? Math.min(...ts) : Math.max(...ts); }
  const t = tiers[col.key];
  if (t && t.t1 != null) return t.t1;
  const pen = (ps.penalties || []).find((x) => x.metric === col.key);
  return pen ? pen.threshold : null;
}

// the Genesys columns that arrive after the sheet renders (shared by the
// managers' sheet and the agent page, so it lives at file level)
const SC_CALL_KEYS = ["calls_taken", "calls_missed", "ans_missed", "avg_talk_s", "avg_acw_s"];

// a tier threshold, shown the way its column is
function tierFmt(col, v) {
  if (v == null) return "";
  if (col.fmt === "money0" || col.fmt === "money2") return "$" + v;
  if (col.fmt === "pct") return v + "%";
  if (col.fmt === "ms") return (col.lowerBetter ? "≤ " : "") + scFmt(v, "ms");
  return String(v);
}

function scFmt(v, fmt) {
  if (v == null) return "–";
  if (fmt === "pct") return v.toFixed(1) + "%";
  if (fmt === "num1") return v.toFixed(1);
  if (fmt === "num2") return v.toFixed(2);
  if (fmt === "int") return (Math.round(v * 10) / 10).toLocaleString();
  if (fmt === "ms") return `${Math.floor(v / 60)}:${String(Math.round(v % 60)).padStart(2, "0")}`;
  if (fmt === "money0") return "$" + Math.round(v).toLocaleString();
  if (fmt === "money2") return "$" + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return v;
}

function scColor(col, v, tiers) {
  if (v == null) return "";
  if (col.apay) return v >= 90 ? "sc-green" : "sc-red";
  if (col.tier && tiers[col.key]) {
    const t = tiers[col.key];
    const has = (x) => x != null;
    if (!has(t.t1) && !has(t.t2) && !has(t.min)) return "";      // nothing set in Settings
    const meets = (x) => (col.lowerBetter ? v <= x : v >= x);
    if (col.goalMin) {          // goal + minimum only: green at the goal, yellow from the minimum, red under
      if (has(t.t1) && meets(t.t1)) return "sc-green";
      if (has(t.min)) return meets(t.min) ? "sc-yellow" : "sc-red sc-below-min";
      return has(t.t1) ? "sc-yellow" : "";
    }
    if (has(t.t1) && meets(t.t1)) return "sc-green";
    if (has(t.t2) && meets(t.t2)) return "sc-yellow";
    // past the minimum: red TEXT on top of the red cell
    if (has(t.min) && !meets(t.min)) return "sc-red sc-below-min";
    return has(t.t1) || has(t.t2) ? "sc-red" : "";
  }
  return "";
}

/* IS #'s filters: which agents and which metric columns to show. Remembered
   in this browser. They only apply to the managers' sheet — the agent page
   always shows its own full row. */
const SC_FILTER = { agentsOff: new Set(), colsOff: new Set(), last: null };
// the "Daily metrics" shortcut: the columns checked day to day (Dallin, 2026-10-02)
const SC_DAILY = ["conversion_pct", "acv", "multi_total_pct", "rev", "subs", "outbound_subs", "apay_pct",
                  "calls_taken", "calls_missed", "ans_missed", "avg_talk_s", "avg_acw_s"];
try {
  const saved = JSON.parse(localStorage.getItem("is_sc_filter") || "{}");
  SC_FILTER.agentsOff = new Set(saved.agentsOff || []);
  SC_FILTER.colsOff = new Set(saved.colsOff || []);
} catch (e) { /* start with everything shown */ }

function scFilterSave() {
  try {
    localStorage.setItem("is_sc_filter", JSON.stringify({
      agentsOff: [...SC_FILTER.agentsOff], colsOff: [...SC_FILTER.colsOff] }));
  } catch (e) { /* private mode: just not remembered */ }
  if (SC_FILTER.last) renderScorecard(SC_FILTER.last.d, SC_FILTER.last.mode);
}

// a click anywhere outside an open dropdown menu closes it
document.addEventListener("click", (ev) => {
  document.querySelectorAll("details.src-picker[open]").forEach((d) => { if (!d.contains(ev.target)) d.open = false; });
});

/* ---------- saved views (per signed-in user, kept on the server) ----------
   prefsGet/prefsPut talk to /api/me/prefs. savedViewsBlock() builds the
   "Saved views" section at the top of a menu: one button per view to apply
   it, ✕ to delete, and "Save current as…". The caller says how to capture
   the current state and how to apply a saved one. */
const PREFS = { cache: {} };
async function prefsGet(key) {
  if (key in PREFS.cache) return PREFS.cache[key];
  try {
    const r = await (await fetch(`/api/me/prefs?key=${key}`)).json();
    PREFS.cache[key] = (r && r.value) || [];
  } catch (e) { PREFS.cache[key] = []; }
  return PREFS.cache[key];
}
async function prefsPut(key, value) {
  PREFS.cache[key] = value;
  try {
    const r = await fetch("/api/me/prefs", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }) });
    if (!r.ok) toast("Couldn't save the view (sign in with your account to use saved views).", "warn");
  } catch (e) { toast("Couldn't save the view.", "warn"); }
}

function savedViewsBlock(key, { capture, apply, rerender }) {
  const box = document.createElement("div"); box.className = "saved-views";
  const title = document.createElement("div"); title.className = "sv-title"; title.textContent = "Saved views";
  box.appendChild(title);
  const views = PREFS.cache[key];
  if (views === undefined) {                       // first open: fetch, then redraw the menu
    const p = document.createElement("div"); p.className = "dim"; p.textContent = "Loading…";
    box.appendChild(p);
    prefsGet(key).then(() => rerender());
    return box;
  }
  if (!views.length) {
    const p = document.createElement("div"); p.className = "dim"; p.textContent = "None yet.";
    box.appendChild(p);
  }
  for (const v of views) {
    const row = document.createElement("div"); row.className = "sv-row";
    const b = document.createElement("button"); b.type = "button"; b.className = "sv-apply"; b.textContent = v.name;
    b.addEventListener("click", () => { apply(v); rerender(); });
    const del = document.createElement("button"); del.type = "button"; del.className = "sv-del"; del.textContent = "✕";
    del.title = "Delete this view";
    del.addEventListener("click", async () => {
      if (!confirm(`Delete the saved view “${v.name}”?`)) return;
      await prefsPut(key, views.filter((x) => x.name !== v.name));
      rerender();
    });
    row.append(b, del);
    box.appendChild(row);
  }
  const save = document.createElement("button"); save.type = "button"; save.className = "sv-save";
  save.textContent = "＋ Save current as…";
  save.addEventListener("click", async () => {
    const name = (prompt("Name for this view:") || "").trim();
    if (!name) return;
    const next = views.filter((x) => x.name !== name).concat([{ name, ...capture() }]);
    await prefsPut(key, next);
    rerender();
  });
  box.appendChild(save);
  return box;
}

function scFilterMenus(d) {
  const menu = (bodySel, sumSel, word, items, off, shortcuts) => {
    const body = $(bodySel);
    if (!body) return;
    const acts = document.createElement("div"); acts.className = "picker-actions";
    for (const [label, fn] of shortcuts) {
      const b = document.createElement("button"); b.type = "button"; b.textContent = label;
      b.addEventListener("click", () => { fn(); scFilterSave(); });
      acts.appendChild(b);
    }
    const nodes = [acts];
    for (const [key, label] of items) {
      const lab = document.createElement("label");
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !off.has(key);
      cb.addEventListener("change", () => { cb.checked ? off.delete(key) : off.add(key); scFilterSave(); });
      lab.append(cb, document.createTextNode(label));
      nodes.push(lab);
    }
    body.replaceChildren(...nodes);
    const shown = items.filter(([k]) => !off.has(k)).length;
    $(sumSel).querySelector("summary").textContent =
      shown === items.length ? word : `${word} (${shown} of ${items.length})`;
  };
  const names = d.rows.map((r) => r.agent).sort();
  const only = (pred) => () => {
    SC_FILTER.agentsOff = new Set(d.rows.filter((r) => !pred(r)).map((r) => r.agent));
  };
  menu("#sc-agents-body", "#sc-agents", "Agents", names.map((n) => [n, n]), SC_FILTER.agentsOff, [
    ["All", only(() => true)], ["Josh's team", only((r) => r.team === "josh")],
    ["Kai's team", only((r) => r.team === "kai")], ["None", only(() => false)]]);
  const cols = SC_COLS.filter((c) => c.key !== "agent");
  menu("#sc-cols-body", "#sc-cols", "Metrics", cols.map((c) => [c.key, c.label]), SC_FILTER.colsOff, [
    ["All", () => { SC_FILTER.colsOff = new Set(); }],
    ["Daily metrics", () => { SC_FILTER.colsOff = new Set(cols.filter((c) => !SC_DAILY.includes(c.key)).map((c) => c.key)); }],
    ["None", () => { SC_FILTER.colsOff = new Set(cols.map((c) => c.key)); }]]);
  // a saved view = the metrics shown AND the agents shown, under a name
  $("#sc-cols-body").prepend(savedViewsBlock("sc_views", {
    capture: () => ({ colsOff: [...SC_FILTER.colsOff], agentsOff: [...SC_FILTER.agentsOff] }),
    apply: (v) => { SC_FILTER.colsOff = new Set(v.colsOff || []); SC_FILTER.agentsOff = new Set(v.agentsOff || []); },
    rerender: () => scFilterSave(),
  }));
}

const SC_TEAM = { open: false };     // agent page: is the team row expanded
function renderScorecard(d, mode, tableSel = "#sc-table", tiersSel = "#sc-tiers") {
  const tiers = d.tiers || {};
  const filtered = tableSel === "#sc-table";       // the managers' sheet
  if (filtered) { SC_FILTER.last = { d, mode }; scFilterMenus(d); }
  const SC_SHOWN = filtered
    ? SC_COLS.filter((c) => c.key === "agent" || !SC_FILTER.colsOff.has(c.key)) : SC_COLS;
  const rowsShown = filtered ? d.rows.filter((r) => !SC_FILTER.agentsOff.has(r.agent)) : d.rows;
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  // grouped header row
  const thead = document.createElement("thead");
  const g = document.createElement("tr");
  const tierN = SC_SHOWN.filter((c) => c.grp === "tier").length;
  const otherN = SC_SHOWN.filter((c) => c.grp === "other").length;
  g.appendChild(document.createElement("th"));
  if (tierN) {
    const gt = document.createElement("th"); gt.className = "grp"; gt.colSpan = tierN;
    gt.textContent = "Tier Metrics"; g.appendChild(gt);
  }
  if (otherN) {
    const go = document.createElement("th"); go.className = "grp"; go.colSpan = otherN;
    go.textContent = "Other Metrics"; g.appendChild(go);
  }
  thead.appendChild(g);
  // column header row (sortable)
  const st = SC_SHOWN.some((c) => c.key === "conversion_pct")
    ? { key: "conversion_pct", asc: false } : { key: "agent", asc: true };
  function render() {
    const rows = rowsShown.slice().sort((x, y) => {
      const a = x[st.key], b = y[st.key];
      if (a == null) return 1; if (b == null) return -1;
      const c = typeof a === "string" ? a.localeCompare(b) : a - b;
      return st.asc ? c : -c;
    });
    const hr = document.createElement("tr");
    for (const col of SC_SHOWN) {
      const th = document.createElement("th");
      th.textContent = col.label;
      if (col.tip) th.title = col.tip;
      if (st.key === col.key) th.classList.add("sorted", ...(st.asc ? ["asc"] : []));
      th.addEventListener("click", () => {
        if (st.key === col.key) st.asc = !st.asc;
        else { st.key = col.key; st.asc = col.key === "agent"; }
        render();
      });
      hr.appendChild(th);
    }
    const tbody = document.createElement("tbody");
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const col of SC_SHOWN) {
        const td = document.createElement("td");
        td.textContent = col.key === "agent" ? r.agent : scFmt(r[col.key], col.fmt);
        const cls = scColor(col, r[col.key], tiers);
        if (cls) td.classList.add(...cls.split(" "));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    if (d.teamRow) {                    // agent page: the team, pinned underneath
      const tr = document.createElement("tr");
      tr.className = "sc-team";
      const everyone = (d.everyone || []).filter((r) => r.agent);
      const canOpen = everyone.length > 1;
      for (const col of SC_SHOWN) {
        const td = document.createElement("td");
        td.textContent = col.key === "agent" ? d.teamRow.agent : scFmt(d.teamRow[col.key], col.fmt);
        const cls = scColor(col, d.teamRow[col.key], tiers);
        if (cls) td.classList.add(...cls.split(" "));
        if (col.key === "agent" && canOpen) {
          td.classList.add("sc-team-toggle");
          td.innerHTML = `<span class="caret">${SC_TEAM.open ? "▾" : "▸"}</span> ${d.teamRow.agent} <span class="sub">${SC_TEAM.open ? "hide" : "see everyone"}</span>`;
          td.title = SC_TEAM.open ? "Hide the individual rows" : "Show every agent's numbers for these dates";
        }
        tr.appendChild(td);
      }
      if (canOpen) tr.addEventListener("click", () => { SC_TEAM.open = !SC_TEAM.open; render(); });
      tbody.appendChild(tr);
      if (canOpen && SC_TEAM.open) {
        // every agent's sheet metrics, best revenue first; the viewer's own
        // row (with their call and quality columns) highlighted
        const me = d.rows[0] || {};
        for (const r0 of everyone) {
          const r = r0.agent === d.agent ? { ...r0, ...me } : r0;
          const er = document.createElement("tr");
          er.className = "sc-all" + (r.agent === d.agent ? " sc-you" : "");
          for (const col of SC_SHOWN) {
            const td = document.createElement("td");
            td.textContent = col.key === "agent" ? r.agent + (r.agent === d.agent ? " (you)" : "") : scFmt(r[col.key], col.fmt);
            const cls = scColor(col, r[col.key], tiers);
            if (cls) td.classList.add(...cls.split(" "));
            er.appendChild(td);
          }
          tbody.appendChild(er);
        }
      }
      // …followed by what to aim for and the floor, in the same columns
      for (const [cls, label, pick] of [
        ["sc-goal", "Max Power Score / Goal", (col) => scGoal(col, tiers)],
        ["sc-minrow", "Minimum", (col) => (tiers[col.key] || {}).min ?? null]]) {
        const r2 = document.createElement("tr"); r2.className = cls;
        for (const col of SC_SHOWN) {
          const td = document.createElement("td");
          td.textContent = col.key === "agent" ? label : (tierFmt(col, pick(col)) || "–");
          r2.appendChild(td);
        }
        tbody.appendChild(r2);
      }
    }
    table.replaceChildren(thead);
    thead.replaceChildren(hr);          // no "Tier Metrics / Other Metrics" band (2026-10-02)
    table.appendChild(tbody);
    if (filtered) {
      // managers' sheet: what to aim for and the floor, as a separate block
      // under the agents, in the same (filtered) columns
      const tfoot = document.createElement("tfoot");
      const gap = document.createElement("tr"); gap.className = "sc-gap";
      const gtd = document.createElement("td"); gtd.colSpan = SC_SHOWN.length; gap.appendChild(gtd);
      tfoot.appendChild(gap);
      if (d.team) {               // the whole team pooled (same row the agent page shows), coloured like a rep
        const tr = document.createElement("tr"); tr.className = "sc-teamavg";
        for (const col of SC_SHOWN) {
          const td = document.createElement("td");
          td.textContent = col.key === "agent" ? "Team average" : scFmt(d.team[col.key], col.fmt);
          const cls = col.key === "agent" ? "" : scColor(col, d.team[col.key], tiers);
          if (cls) td.classList.add(...cls.split(" "));
          tr.appendChild(td);
        }
        tr.title = "The whole team pooled together (every agent's sales and calls added up), whatever agents are shown";
        tfoot.appendChild(tr);
      }
      for (const [cls, label, pick] of [
        ["sc-goal", "Max Power Score / Goal", (col) => scGoal(col, tiers)],
        ["sc-minrow", "Minimum", (col) => (tiers[col.key] || {}).min ?? null]]) {
        const r2 = document.createElement("tr"); r2.className = cls;
        for (const col of SC_SHOWN) {
          const td = document.createElement("td");
          td.textContent = col.key === "agent" ? label : (tierFmt(col, pick(col)) || "–");
          r2.appendChild(td);
        }
        tfoot.appendChild(r2);
      }
      table.appendChild(tfoot);
    }
  }
  render();
  wrap.appendChild(table);

  // tier legend
  const legend = document.createElement("div");
  legend.className = "table-wrap";
  const lt = document.createElement("table");
  const lcols = SC_SHOWN.filter((c) => c.tier && tiers[c.key] &&
    ["t1", "t2", "min"].some((k) => tiers[c.key][k] != null));
  lt.innerHTML = `<thead><tr><th>${d.teamRow ? "" : "Tier"}</th>` +
    lcols.map((c) => `<th>${c.label}</th>`).join("") + "</tr></thead>";
  const lb = document.createElement("tbody");
  // the agent page lists only the minimums; managers see all three bands
  const bands = [["t1", "t1", "Tier 1"], ["t2", "t2", "Tier 2"], ["min", "tmin", "Minimums"]]
    .filter(([row]) => !d.teamRow || row === "min");
  for (const [row, cls, lbl] of bands) {
    const tr = document.createElement("tr"); tr.className = cls;
    tr.innerHTML = `<td>${lbl}</td>` + lcols.map((c) => {
      const v = (tiers[c.key] || {})[row];
      return `<td>${tierFmt(c, v)}</td>`;
    }).join("");
    lb.appendChild(tr);
  }
  lt.appendChild(lb);
  legend.appendChild(lt);

  $(tableSel).replaceChildren(wrap);
  if (d.teamRow) {
    // phone layout (agent page): one metric per line — You / Team / Minimum
    wrap.classList.add("me-wide");
    const me = d.rows[0] || {};
    const sw = document.createElement("div"); sw.className = "table-wrap me-stack";
    const stt = document.createElement("table");
    stt.innerHTML = "<thead><tr><th>Metric</th><th>You</th><th>Team</th><th>Goal</th><th>Min</th></tr></thead>";
    const stb = document.createElement("tbody");
    for (const col of SC_SHOWN) {
      if (col.key === "agent") continue;
      const tr = document.createElement("tr");
      const name = document.createElement("td"); name.textContent = col.label;
      const mine = document.createElement("td"); mine.textContent = scFmt(me[col.key], col.fmt);
      const cls = scColor(col, me[col.key], tiers);
      if (cls) mine.classList.add(...cls.split(" "));
      const team = document.createElement("td"); team.textContent = scFmt(d.teamRow[col.key], col.fmt);
      const goal = document.createElement("td"); goal.className = "me-goal";
      goal.textContent = tierFmt(col, scGoal(col, tiers)) || "–";
      const min = document.createElement("td"); min.className = "me-min";
      const mv = col.tier && tiers[col.key] ? tiers[col.key].min : null;
      min.textContent = tierFmt(col, mv) || "–";
      tr.append(name, mine, team, goal, min);
      stb.appendChild(tr);
    }
    stt.appendChild(stb); sw.appendChild(stt);
    $(tableSel).appendChild(sw);
  }
  const cap = rangeCaption(d);
  const modeTxt = mode === "serviced"
    ? "Counting subs whose INITIAL SERVICE DATE is in range (serviced/scheduled between)."
    : "Counting subs whose SALE DATE is in range (sold between).";
  setTip(cap, `${d.start} – ${d.end}. ${modeTxt} Excludes mosquito-upsell-source ` +
    `subscriptions. Conversion = audit sheet sales ÷ qualified.` +
    (d.teamRow ? " Team average = the whole team's sales pooled together; its revenue is the average per rep." : ""));
  // the tier legend table is gone: goals + minimums are rows of the sheet now;
  // the agent page carries no caption at all (its header already says what's excluded)
  $(tiersSel).replaceChildren(...(d.teamRow ? [] : [cap]));
}

function rangeSubsRows(d) {
  return d.rows.map((r) => ({
    agent: r.agent === "TEAM" ? "TEAM TOTAL" : r.agent,
    subs: r.subs, pending: r.pending,
    lt12: r.lt12, lt12_pct: r.lt12_pct ?? 0,
    m12: r.m12, m12_pct: r.m12_pct ?? 0,
    m18: r.m18, m18_pct: r.m18_pct ?? 0,
    m24: r.m24, m24_pct: r.m24_pct ?? 0,
    multiyear_pct: r.multiyear_pct ?? 0,
    acv: r.acv ?? 0, init_avg: r.init_avg ?? 0, cv_total: r.cv_total ?? 0,
  }));
}

/* ---------- Marketing tab: sub-tabs, Lead Volume, By Source, Spend & ROAS ---------- */
const MK = {};
function showMktgSub(sub) {
  document.querySelectorAll("#mktg-subtabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mksub === sub));
  document.querySelectorAll(".mksubpanel").forEach((p) =>
    p.classList.toggle("active", p.id === "mksub-" + sub));
  if (sub === "sources") { ensureLoaded("src"); initSrevLevel(); if (!SREV.year) loadRevenue(undefined, SREV); }
  if (sub === "volume") { loadMkHistory("volume"); loadMkHistory("forms"); ensureLoaded("mhr"); }
  if (sub === "bysource") ensureLoaded("mb");
  if (sub === "spend") loadSpendWindows();
}

const countFmt = (v) => (v == null ? "–" : Math.round(v).toLocaleString());
const pctFmt = (v) => (v == null ? "–" : v.toFixed(1) + "%");
// the Revenue tab's line chart, re-used for weekly counts
function weekChartState(sel) {
  return { chart: null, hidden: new Set(), colorOf: {}, el: { chart: sel },
    axisFmt: countFmt, valFmt: countFmt, yFloor: 10, labelEvery: 2 };
}
const MB = weekChartState("#mb-chart");
const MK_PAL = ["#1e3659", "#6fa8c4", "#d98b1f", "#1e8e5a", "#7048ba", "#b3323c"];
const mkCard = (label, big, sub) => `<div class="rev-card"><div class="rev-card-label">${label}</div>`
  + `<div class="rev-card-amt">${big}</div><div class="rev-card-sub">${sub || ""}</div></div>`;
const weekLabel = (w) => new Date(w + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
function drawWeekChart(S, weeks, seriesObj) {
  const agents = Object.entries(seriesObj).map(([name, vals]) =>
    ({ agent: name, months: vals, total: vals.reduce((a, b) => a + b, 0) }));
  S.chart = { year: "", months: weeks.map(weekLabel), agents };
  S.hidden = new Set(); S.colorOf = {};
  agents.forEach((a, i) => { S.colorOf[a.agent] = MK_PAL[i % MK_PAL.length]; });
  renderRevChart(S);
}

async function loadMk(kind, a, b) {
  const map = { bysource: ["/api/marketing/bysource", "#mb-table", renderBySource] };
  const [url, tableSel, render] = map[kind];
  MK[kind] = (MK[kind] || 0) + 1;
  const token = MK[kind];
  $(tableSel).replaceChildren(noData("Loading…"));
  let d = null;
  try { d = await (await fetch(`${url}?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { d = { error: "Couldn't reach the server." }; }
  if (token !== MK[kind]) return;
  if (!d || d.error) { $(tableSel).replaceChildren(noData((d && d.error) || "No data.")); return; }
  render(d);
}

function renderBySource(d) {
  const sel = $("#mb-source");
  const prev = MB.pick || "__all";
  sel.replaceChildren(new Option("All sources", "__all"),
    ...d.rows.map((r) => new Option(`${r.source} (${countFmt(r.total)})`, r.source)));
  sel.value = [...sel.options].some((o) => o.value === prev) ? prev : "__all";
  const draw = () => {
    MB.pick = sel.value;
    const s = sel.value === "__all" ? d.all : (d.series[sel.value] || { calls: [], forms: [] });
    drawWeekChart(MB, d.weeks, { Total: s.calls.map((c, i) => c + (s.forms[i] || 0)), Calls: s.calls, Forms: s.forms });
  };
  sel.onchange = draw;
  draw();
  MB.data = d;
  bySourceMenu();
  bySourceTable();
  noteInto("#mb-foot", `${d.start} – ${d.through} vs ${d.prev_start} – ${d.prev_end}. Calls are the team's handled calls ` +
    `matched to a source by the tracking number dialled (“(no tracking number)” = a number the sourcing sheet doesn't list); ` +
    `forms are net new leads by the source on the submission. Grouping and channels follow the Sources table. ` +
    `The Total row adds up the sources shown after the Sources filter.`);
}

// the Sources menu over the range table: a check-list of channels and one of
// sources; unticked names are remembered for the session (MB.chanOff / MB.srcOff)
MB.chanOff = new Set(); MB.srcOff = new Set();
try {
  const v = JSON.parse(localStorage.getItem("is_mb_view") || "null");
  if (v) { MB.chanOff = new Set(v.chanOff || []); MB.srcOff = new Set(v.srcOff || []); }
} catch (e) { /* fine */ }
function mbRemember() {
  try { localStorage.setItem("is_mb_view", JSON.stringify({ chanOff: [...MB.chanOff], srcOff: [...MB.srcOff] })); } catch (e) { /* fine */ }
}
function bySourceRows() {
  const d = MB.data;
  if (!d) return [];
  return d.rows.filter((r) => !MB.srcOff.has(r.source) && !MB.chanOff.has(r.channel || "Other"));
}
function bySourceSummary() {
  const d = MB.data;
  if (!d) return;
  const n = d.rows.length, shown = bySourceRows().length;
  $("#mb-src").querySelector("summary").textContent = shown === n ? "Sources" : `Sources (${shown} of ${n})`;
}
function bySourceMenu() {
  const d = MB.data, body = $("#mb-src-body");
  const names = d.rows.map((r) => r.source);
  const chans = [...new Set(d.rows.map((r) => r.channel || "Other"))].sort();
  const boxes = [];
  const sync = () => {
    for (const { cb, kind, key } of boxes) cb.checked = !(kind === "chan" ? MB.chanOff : MB.srcOff).has(key);
    mbRemember(); bySourceSummary(); bySourceTable();
  };
  const check = (kind, key, label) => {
    const lab = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox";
    cb.checked = !(kind === "chan" ? MB.chanOff : MB.srcOff).has(key);
    cb.addEventListener("change", () => {
      const off = kind === "chan" ? MB.chanOff : MB.srcOff;
      cb.checked ? off.delete(key) : off.add(key);
      sync();
    });
    boxes.push({ cb, kind, key });
    lab.append(cb, document.createTextNode(" " + label));
    return lab;
  };
  const title = (txt, top) => {
    const el = document.createElement("div"); el.className = "sv-title"; el.textContent = txt;
    if (top) el.style.marginTop = "8px";
    return el;
  };
  const shortcuts = (pairs) => {
    const acts = document.createElement("div"); acts.className = "picker-actions";
    for (const [label, fn] of pairs) {
      const b = document.createElement("button"); b.type = "button"; b.textContent = label;
      b.addEventListener("click", (e) => { e.preventDefault(); fn(); sync(); });
      acts.appendChild(b);
    }
    return acts;
  };
  // a saved view = the channels and sources ticked, under a name (per signed-in user)
  const views = savedViewsBlock("mb_views", {
    capture: () => ({ chanOff: [...MB.chanOff], srcOff: [...MB.srcOff] }),
    apply: (v) => { MB.chanOff = new Set(v.chanOff || []); MB.srcOff = new Set(v.srcOff || []); mbRemember(); },
    rerender: () => { bySourceMenu(); bySourceTable(); },
  });
  body.replaceChildren(
    views,
    title("Channel"),
    shortcuts([["All", () => { MB.chanOff = new Set(); }], ["None", () => { MB.chanOff = new Set(chans); }]]),
    ...chans.map((c) => check("chan", c, c)),
    title("Sources", true),
    shortcuts([["All", () => { MB.srcOff = new Set(); }], ["None", () => { MB.srcOff = new Set(names); }]]),
    ...names.map((n) => check("src", n, `${n} (${countFmt(d.rows.find((r) => r.source === n).total)})`)));
  bySourceSummary();
}
function bySourceTable() {
  const d = MB.data;
  if (!d) return;
  const rows = bySourceRows();
  const sum = (k) => rows.reduce((a, r) => a + (r[k] || 0), 0);
  const tot = { source: "Total", channel: "", calls: sum("calls"), forms: sum("forms"),
    total: sum("total"), prev_total: sum("prev_total"), __total: true };
  tot.delta_pct = tot.prev_total ? Math.round(1000 * (tot.total - tot.prev_total) / tot.prev_total) / 10 : null;
  const cols = [["source", "Source", "lead-source group, as on the Sources table"], ["channel", "Channel", ""],
    ["calls", "Calls", "calls handled by the team, attributed by the number dialled"],
    ["forms", "Forms", "net new form leads naming this source"], ["total", "Total", "calls + forms"],
    ["prev_total", "Prior period", `${d.prev_start} – ${d.prev_end}`], ["delta_pct", "Change", "total vs the prior period"]];
  const delta = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(1) + "%");
  if (!rows.length) { $("#mb-table").replaceChildren(noData("No sources picked.")); return; }
  $("#mb-table").replaceChildren(simpleTable(cols, [...rows, tot], {
    fmt: { calls: countFmt, forms: countFmt, total: countFmt, prev_total: countFmt, delta_pct: delta },
    hl: new Set(["total"]), sortable: true, defaultSort: "total", pin: (r) => !!r.__total,
    flag: (r, k) => (k === "delta_pct" && r.delta_pct != null ? (r.delta_pct >= 0 ? "sc-green" : "sc-red") : "") }));
}

/* ---------- Lead Volume: calls + forms year over year ----------
   One line per year across 52 Sun–Sat weeks. Each week shows the dashboard's
   count (Genesys / the form-lead feed) when it has one, otherwise the number
   typed on the manual tracker sheet — one combined line, no source labels. */
const MH = {
  volume: { url: "/api/marketing/volume_history", S: Object.assign(weekChartState("#mvh-chart"), { labelEvery: 4 }), data: null, loading: false },
  forms: { url: "/api/marketing/forms_history", S: Object.assign(weekChartState("#mfh-chart"), { labelEvery: 4 }), data: null, loading: false },
};
const YEAR_PAL = ["#1e3659", "#6fa8c4", "#d98b1f", "#1e8e5a", "#7048ba", "#b3323c", "#0e7490"];

async function loadMkHistory(kind) {
  const H = MH[kind];
  if (H.data || H.loading) { if (H.data) renderMkHistory(kind); return; }
  H.loading = true;
  $(H.S.el.chart).replaceChildren(noData("Loading…"));
  try {
    const d = await (await fetch(H.url)).json();
    if (d && d.rows) H.data = d;
    else $(H.S.el.chart).replaceChildren(noData((d && d.error) || "Couldn't load the history."));
  } catch (e) { $(H.S.el.chart).replaceChildren(noData("Couldn't load the history.")); }
  H.loading = false;
  if (H.data) renderMkHistory(kind);
}

function renderMkHistory(kind) {
  const H = MH[kind], d = H.data, S = H.S;
  const agents = d.rows.filter((r) => r.combined.some((v) => v)).map((r) =>
    ({ agent: String(r.year), months: r.combined.map((v) => (v == null ? 0 : v)), total: r.combined_total || 0 }));
  S.chart = { year: "", months: d.weeks, agents };
  S.hidden = new Set(); S.colorOf = {};
  agents.slice().reverse().forEach((a, i) => { S.colorOf[a.agent] = YEAR_PAL[i % YEAR_PAL.length]; });
  renderRevChart(S);
  if (kind === "forms") {
    noteInto("#mvh-foot", `Weeks run Sunday–Saturday; week 1 is the week containing January 1, and ${d.current_year} runs to week ${d.current_week}. ` +
      `Calls = calls offered to the Inside Sales queues (Genesys, from April 2023), forms = form leads; where the dashboard has no ` +
      `count for a week the number typed on the Brooks Pest Weekly Data sheet is used. Click a year in the legend to hide or show it.`);
  }
}

/* ---------- Lead Volume: leads by hour of the day (Mountain) ----------
   Calls offered + net new form leads per hour for a picked range, against the
   same number of days right before it. Numbers are the average per day for
   that hour (one day = that day's counts). The table lists the open hours,
   then After hours, Open hours and All day roll-ups. */
const MHR = Object.assign(weekChartState("#mhr-chart"), { yFloor: 4, labelEvery: 2, tok: 0, data: null });
const hrFmt = (v) => (v == null ? "–" : (Math.round(v * 10) / 10).toLocaleString(undefined, { maximumFractionDigits: 1 }));
const hrDelta = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(1) + "%");
const hrPct = (now, was) => (now != null && was ? Math.round(1000 * (now - was) / was) / 10 : null);
const hrDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
const hrSpan = (a, b) => (a === b ? hrDay(a) : `${hrDay(a)} – ${hrDay(b)}`);

async function loadMkHourly(a, b) {
  const tok = ++MHR.tok;
  $("#mhr-table").replaceChildren(noData("Loading…"));
  let d = null;
  try { d = await (await fetch(`/api/marketing/hourly?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { d = { error: "Couldn't reach the server." }; }
  if (tok !== MHR.tok) return;
  if (!d || d.error || !d.hours) {
    $("#mhr-cards").replaceChildren(); $("#mhr-chart").replaceChildren();
    $("#mhr-table").replaceChildren(noData((d && d.error) || "No data."));
    return;
  }
  MHR.data = d;
  renderMkHourly(d);
}

function renderMkHourly(d) {
  const one = d.days <= 1 && d.prev_days <= 1;
  $("#mhr-sub").textContent = "Mountain time · " + (one ? "leads in each hour" : "average per day for each hour")
    + ` · ${hrSpan(d.start, d.through)} vs ${hrSpan(d.prev_start, d.prev_end)}`;
  // cards: the three roll-ups, total leads with the change on the period before
  const card = (label, g) => {
    const ch = hrPct(g.cur.avg_total, g.prev.avg_total);
    const cls = ch == null ? "" : (ch >= 0 ? "up" : "down");
    return mkCard(label, hrFmt(g.cur.avg_total),
      `${hrFmt(g.cur.avg_calls)} calls · ${hrFmt(g.cur.avg_forms)} forms`
      + `<br><span class="mhr-chg ${cls}">${hrDelta(ch)}</span> vs ${hrFmt(g.prev.avg_total)} before`);
  };
  const h12 = (h) => `${(h % 12) || 12} ${h % 24 < 12 ? "AM" : "PM"}`;
  const openLbl = `${h12(d.open_hour)} – ${h12(d.close_hour)}`, afterLbl = `${h12(d.close_hour)} – ${h12(d.open_hour)}`;
  const per = one ? "" : " · per day";
  $("#mhr-cards").innerHTML = card(`Open hours (${openLbl})${per}`, d.open)
    + card(`After hours (${afterLbl})${per}`, d.after) + card(`Whole day${per}`, d.all);

  // chart: all 24 hours, midnight to midnight
  const short = (h) => `${(h % 12) || 12}${h < 12 ? "a" : "p"}`;
  const pick = (side, k) => d.hours.map((x) => x[side]["avg_" + k] || 0);
  const series = { Total: pick("cur", "total"), Calls: pick("cur", "calls"), Forms: pick("cur", "forms"),
    "Total, period before": pick("prev", "total") };
  const keep = MHR.chart ? new Set(MHR.hidden) : new Set();
  MHR.valFmt = hrFmt; MHR.axisFmt = hrFmt;
  MHR.chart = { year: "", months: d.hours.map((x) => short(x.hour)),
    agents: Object.entries(series).map(([name, vals]) => ({ agent: name, months: vals, total: vals.reduce((a, b) => a + b, 0) })) };
  MHR.hidden = keep; MHR.colorOf = {};
  MHR.chart.agents.forEach((a, i) => { MHR.colorOf[a.agent] = ["#1e3659", "#6fa8c4", "#1e8e5a", "#b8c4d0"][i]; });
  renderRevChart(MHR);

  // table: open hours one by one, then the roll-ups
  const row = (label, g, extra) => {
    const r = { label, ...extra };
    for (const k of ["calls", "forms", "total"]) {
      r[k] = g.cur["avg_" + k]; r["p_" + k] = g.prev["avg_" + k];
      r["d_" + k] = hrPct(g.cur["avg_" + k], g.prev["avg_" + k]);
    }
    return r;
  };
  const rows = d.hours.filter((x) => x.open).map((x) => row(x.label, x));
  rows.push(row(`After hours (${afterLbl})`, d.after, { __total: true }),
            row(`Open hours (${openLbl})`, d.open, { __total: true }),
            row("Whole day", d.all, { __total: true }));
  const prevTip = hrSpan(d.prev_start, d.prev_end), nowTip = hrSpan(d.start, d.through);
  const trio = (k) => [[k, "Now", nowTip], ["p_" + k, "Before", prevTip], ["d_" + k, "Change", "now vs the period before"]];
  const cols = [["label", "Hour (Mountain)", ""], ...trio("calls"), ...trio("forms"), ...trio("total")];
  const fmt = {};
  for (const k of ["calls", "forms", "total"]) { fmt[k] = hrFmt; fmt["p_" + k] = hrFmt; fmt["d_" + k] = hrDelta; }
  const t = simpleTable(cols, rows, {
    fmt, hl: new Set(["total"]), pin: (r) => !!r.__total,
    groups: [{ label: "", span: 1 }, { label: "Calls", span: 3 }, { label: "Forms", span: 3 }, { label: "Total leads", span: 3 }],
    flag: (r, k) => (k.startsWith("d_") && r[k] != null ? (r[k] >= 0 ? "sc-green" : "sc-red") : "") });
  t.classList.add("mhr-table");
  $("#mhr-table").replaceChildren(t);
  const head = $("#mhr-sub").parentElement;
  let icon = head.querySelector(":scope > .info");
  if (!icon) { icon = infoIcon(""); head.appendChild(icon); }
  setTip(icon, `Hours are Mountain time. Calls = calls offered to the Inside Sales queues (Genesys); forms = net new form leads ` +
    `(existing customers left out), by the time the form came in. ` + (one ? "" : `Each number is the average per day for that hour across the range. `) +
    `“Before” is the same number of days right before the range (${prevTip}). The hour still in progress is left out. ` +
    `The phone system only sends calls to the queues while the team is open, so after-hours leads are forms. ` +
    `The chart shows all 24 hours, midnight to midnight.`);
}

/* ---------- Spend & ROAS: the spend sheet's layout ----------
   Rows = the sheet's Lead Source lines. Three windows (last spend week,
   trailing 4, trailing 12) × the metrics switched on in Columns (ROAS, CPL,
   CPA by default; Spend, Revenue, Form leads, Call volume optional). The
   Sources menu hides lines; the "Lead sources" total follows what's shown.
   Sales and revenue are always the dashboard's (Pending + Serviced sales by
   sale date). A saved view = columns + sources. */
const MS = Object.assign(weekChartState("#ms-chart"), {
  data: null, loading: false,
  axisFmt: (v) => "$" + Math.round(v / 1000) + "k", valFmt: (v) => "$" + Math.round(v).toLocaleString(), yFloor: 1000,
  labelEvery: 4,
  cols: { channel: true, roas: true, cpl: true, cpa: true, spend: false, revenue: false, forms: false, calls: false },
  srcOff: new Set(),
  chanOff: new Set(),          // channels (Paid / Organic / Affiliate / Generic / Other) hidden
  // the weekly chart: any mix of types and/or single sources (empty = every line incl. overhead)
  chartChan: new Set(), chartSrc: new Set(), chartOverhead: false,
  // the chart's date range: null = year to date (from spend_windows); else {chart, rows, start, end}
  chartView: null, chartPicker: null, chartTok: 0,
});
// CPA % = spend ÷ revenue (the sheet's "ROI" column) rides the same chart on a right-hand % axis
MS.right = { keys: new Set(["CPA %"]), axisFmt: (v) => Math.round(v) + "%", valFmt: (v) => v.toFixed(1) + "%",
             ticks: [35, 40, 45, 50, 55] };     // hard gridlines every 5 points so CPA % reads at a glance
const MS_WINDOWS = [["w1", "Last week"], ["w4", "Trailing 4 weeks"], ["w12", "Trailing 12 weeks"]];
const MS_METRICS = [["roas", "ROAS"], ["cpl", "CPL"], ["cpa", "CPA"], ["spend", "Spend"], ["revenue", "Revenue"],
  ["forms", "Form leads"], ["calls", "Call volume"]];
try {
  const saved = JSON.parse(localStorage.getItem("is_ms_view") || "{}");
  if (saved.cols) Object.assign(MS.cols, saved.cols);
  MS.srcOff = new Set(saved.srcOff || []);
  MS.chanOff = new Set(saved.chanOff || []);
  MS.chartChan = new Set(saved.chartChan || []); MS.chartSrc = new Set(saved.chartSrc || []); MS.chartOverhead = !!saved.chartOverhead;
} catch (e) { /* defaults */ }
function msSave() {
  try { localStorage.setItem("is_ms_view", JSON.stringify({ cols: MS.cols, srcOff: [...MS.srcOff], chanOff: [...MS.chanOff],
    chartChan: [...MS.chartChan], chartSrc: [...MS.chartSrc], chartOverhead: MS.chartOverhead })); } catch (e) { /* fine */ }
  spendMenus();
  if (MS.data) renderSpendWindows(MS.data);
}

async function loadSpendWindows() {
  if (MS.data || MS.loading) { if (MS.data) renderSpendWindows(MS.data); return; }
  MS.loading = true;
  $("#ms-table").replaceChildren(noData("Reading the spend sheet…"));
  let d = null;
  try { d = await (await fetch("/api/marketing/spend_windows")).json(); }
  catch (e) { d = { error: "Couldn't reach the server." }; }
  MS.loading = false;
  if (!d || d.error || !d.rows) { $("#ms-table").replaceChildren(noData((d && d.error) || "No data.")); return; }
  MS.data = d;
  if (!MS.chartPicker) MS.chartPicker = makeRangePicker($("#ms-chart-range"), (a, b) => loadSpendChart(a, b), "ytd");
  spendMenus();
  renderSpendWindows(d);
}

// the weekly chart for a picked date range; year to date reuses what the page already has
async function loadSpendChart(a, b) {
  const tok = ++MS.chartTok;
  const now = new Date();
  if (a === fmtDay(new Date(now.getFullYear(), 0, 1)) && b === fmtDay(now)) {
    MS.chartView = null;
    if (MS.data) renderSpendWindows(MS.data);
    return;
  }
  $("#ms-chart").replaceChildren(noData("Loading…")); $("#ms-ytd").replaceChildren();
  let c = null;
  try { c = await (await fetch(`/api/marketing/spend_chart?start_day=${a}&end_day=${b}`)).json(); }
  catch (e) { c = { error: "Couldn't reach the server." }; }
  if (tok !== MS.chartTok) return;
  if (!c || c.error || !c.chart) {
    MS.chartView = null;
    $("#ms-chart").replaceChildren(noData((c && c.error) || "No complete weeks in that range."));
    $("#ms-ytd").replaceChildren();
    return;
  }
  MS.chartView = { chart: c.chart, rows: c.rows || [], start: a, end: b };
  if (MS.data) spendChart(MS.data);
}

// the Sources and Columns menus (rebuilt whenever the view changes)
function spendMenus() {
  const views = savedViewsBlock("ms_views", {
    capture: () => ({ cols: { ...MS.cols }, srcOff: [...MS.srcOff], chanOff: [...MS.chanOff],
                      chartChan: [...MS.chartChan], chartSrc: [...MS.chartSrc], chartOverhead: MS.chartOverhead }),
    apply: (v) => { Object.assign(MS.cols, v.cols || {}); MS.srcOff = new Set(v.srcOff || []); MS.chanOff = new Set(v.chanOff || []);
                    MS.chartChan = new Set(v.chartChan || []); MS.chartSrc = new Set(v.chartSrc || []); MS.chartOverhead = !!v.chartOverhead; },
    rerender: msSave,
  });
  const check = (key, label, on, onChange) => {
    const lab = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = on;
    cb.addEventListener("change", () => { onChange(cb.checked); msSave(); });
    lab.append(cb, document.createTextNode(" " + label));
    return lab;
  };
  const shortcuts = (pairs) => {
    const acts = document.createElement("div"); acts.className = "picker-actions";
    for (const [label, fn] of pairs) {
      const b = document.createElement("button"); b.type = "button"; b.textContent = label;
      b.addEventListener("click", () => { fn(); msSave(); });
      acts.appendChild(b);
    }
    return acts;
  };
  // columns
  const cbody = $("#ms-cols-body");
  cbody.replaceChildren(views,
    shortcuts([["All", () => { MS.cols.channel = true; for (const [k] of MS_METRICS) MS.cols[k] = true; }],
               ["Defaults", () => { MS.cols.channel = true; for (const [k] of MS_METRICS) MS.cols[k] = ["roas", "cpl", "cpa"].includes(k); }],
               ["None", () => { MS.cols.channel = false; for (const [k] of MS_METRICS) MS.cols[k] = false; }]]),
    check("channel", "Channel (Paid / Organic / Affiliate / Generic)", !!MS.cols.channel, (on) => { MS.cols.channel = on; }),
    ...MS_METRICS.map(([k, label]) => check(k, label + " (last week, 4 wk, 12 wk)", !!MS.cols[k], (on) => { MS.cols[k] = on; })));
  const onCols = MS_METRICS.filter(([k]) => MS.cols[k]).length + (MS.cols.channel ? 1 : 0);
  $("#ms-cols").querySelector("summary").textContent = onCols === MS_METRICS.length + 1 ? "Columns" : `Columns (${onCols} of ${MS_METRICS.length + 1})`;
  // sources
  const rowsAll = MS.data ? MS.data.rows : [];
  const names = rowsAll.map((r) => r.source);
  const chans = [...new Set(rowsAll.map((r) => r.channel || "Other"))].sort();
  const sbody = $("#ms-src-body");
  const chanTitle = document.createElement("div"); chanTitle.className = "sv-title"; chanTitle.textContent = "Type of source";
  const srcTitle = document.createElement("div"); srcTitle.className = "sv-title"; srcTitle.textContent = "Sources";
  srcTitle.style.marginTop = "8px";
  sbody.replaceChildren(
    chanTitle,
    shortcuts([["All", () => { MS.chanOff = new Set(); }], ["None", () => { MS.chanOff = new Set(chans); }]]),
    ...chans.map((c) => check(c, c, !MS.chanOff.has(c), (on) => { on ? MS.chanOff.delete(c) : MS.chanOff.add(c); })),
    srcTitle,
    shortcuts([["All", () => { MS.srcOff = new Set(); }], ["None", () => { MS.srcOff = new Set(names); }]]),
    ...names.map((n) => check(n, n, !MS.srcOff.has(n), (on) => { on ? MS.srcOff.delete(n) : MS.srcOff.add(n); })));
  const shown = rowsAll.filter((r) => !MS.srcOff.has(r.source) && !MS.chanOff.has(r.channel || "Other")).length;
  $("#ms-src").querySelector("summary").textContent = shown === names.length ? "Sources" : `Sources (${shown} of ${names.length})`;
}

function renderSpendWindows(d) {
  // a negative spend is a net credit (Baton pays us for referrals we send): the
  // money came in free, so ROAS reads "Free", cost per lead / sale $0, and the
  // credit nets against total spend
  const FREE = 9999;
  const usd = (v) => (v == null ? "–" : (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString());
  const usd2 = (v) => (v == null ? "–" : (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const x = (v) => (v == null ? "–" : v >= FREE ? "Free" : v.toFixed(2) + "×");
  const fmtOf = { roas: x, cpl: usd2, cpa: usd2, spend: usd, revenue: usd, forms: countFmt, calls: countFmt };
  const tipOf = { roas: "revenue ÷ spend", cpl: "spend ÷ (calls + form leads)", cpa: "spend ÷ sales (cost per sale)",
    spend: "from the spend sheet", revenue: "contract value of Pending + Serviced sales with this source, by sale date",
    forms: "net new form leads", calls: "calls handled, by tracking number" };
  const metrics = MS_METRICS.filter(([k]) => MS.cols[k]);
  const pick = (w) => {
    if (!w) return {};
    const free = (w.spend || 0) <= 0 && (w.revenue || 0) > 0;
    return { roas: free ? FREE : w.roas, cpl: free ? 0 : w.cpl, cpa: free ? 0 : w.cps,
      spend: w.spend, revenue: w.revenue, forms: w.forms, calls: w.calls };
  };
  const cols = [["source", "Source", "the spend sheet's Lead Source line"]];
  if (MS.cols.channel) cols.push(["channel", "Channel", "Paid / Organic / Affiliate Marketing / Generic, as on the Sources table"]);
  const groups = [{ label: "", span: cols.length }];
  const fmt = {};
  for (const [wk, wlabel] of MS_WINDOWS) {
    const w = d.windows[wk] || {};
    if (metrics.length) groups.push({ label: `${wlabel} · ${w.start || ""} – ${w.end || ""}`, span: metrics.length, cls: "grp-pr" });
    for (const [m, label] of metrics) { cols.push([`${wk}_${m}`, label, tipOf[m]]); fmt[`${wk}_${m}`] = fmtOf[m]; }
  }
  const flat = (base, byWin) => {
    const o = { ...base };
    for (const [wk] of MS_WINDOWS) {
      const v = pick(byWin[wk]);
      for (const [m] of MS_METRICS) o[`${wk}_${m}`] = v[m];
      o[`${wk}_spendsort`] = (byWin[wk] || {}).spend || 0;
    }
    return o;
  };
  const shownRows = d.rows.filter((r) => !MS.srcOff.has(r.source) && !MS.chanOff.has(r.channel || "Other"));
  const rows = shownRows.map((r) => flat({ source: r.matched ? r.source : r.source + " (not matched)", channel: r.channel }, r));
  // "Lead sources shown" counts whatever rows are visible: the sheet's lines plus the no-spend sources
  // "Lead sources" total follows the sources shown; the blended total is everything on the sheet
  const sum = (key, f) => shownRows.reduce((acc, r) => acc + ((r[key] || {})[f] || 0), 0);
  const ratio = (a, b) => (b ? a / b : null);
  const subtotal = {};
  for (const [wk] of MS_WINDOWS) {
    const spend = sum(wk, "spend"), leads = sum(wk, "leads"), sales = sum(wk, "sales"), revenue = sum(wk, "revenue");
    subtotal[wk] = { spend, leads, sales, revenue, calls: sum(wk, "calls"), forms: sum(wk, "forms"),
      roas: ratio(revenue, spend), cpl: ratio(spend, leads), cps: ratio(spend, sales) };
  }
  rows.push(flat({ source: shownRows.length === d.rows.length ? "Lead sources" : `Lead sources shown (${shownRows.length} of ${d.rows.length})`, channel: "", _tot: true }, subtotal));
  rows.push(flat({ source: "All spend incl. overhead vs all revenue", channel: "", _tot: true }, d.totals.blended));
  $("#ms-table").replaceChildren(simpleTable(cols, rows, {
    groups, fmt, sortable: true, defaultSort: "w12_spendsort", pin: (r) => r._tot,
    hl: new Set(MS_WINDOWS.map(([wk]) => `${wk}_roas`)),
    // ROAS: 2.5× and up green, 2× to 2.5× yellow, under 2× red (Dallin, 2026-10-08)
    flag: (r, k) => (k.endsWith("_roas") && r[k] != null ? (r[k] >= 2.5 ? "sc-green" : r[k] >= 2 ? "sc-yellow" : "sc-red") : "") }));
  noteInto("#ms-foot", `Spend is read from the sheet's ${d.tabs.join(", ")} tab every 10 minutes (latest week with spend ends ${d.latest_week}). ` +
    `Sales and revenue are the dashboard's: office-staff Pending + Serviced sales whose source maps to the line, by sale date ` +
    `(upsells, mosquito and renewals included; a pending sale that later cancels drops out). Leads = calls by tracking number ` +
    `+ net form leads. Overhead lines (payroll, fees, tooling) only count in the bottom total. A negative spend is a net ` +
    `credit (e.g. Baton paying for referrals we send): that revenue shows as “Free” and the credit nets against total spend. ` +
    `Sources with no spend line (referrals, renewals, upsells, in-house campaigns) are listed too, with $0 spend and “Free” ROAS; ` +
    `the totals and the chart's Revenue line count ALL revenue, the same total as the Revenue tab. ` +
    `ROAS colours: 2.5× and up green, 2× to 2.5× yellow, under 2× red. ` +
    `On the weekly chart, a week cut by the start of the dates (Dec 28 – Jan 3 for year to date) counts only its share of ` +
    `that week's spend, by days, since the sheet's spend is weekly. ` + (d.notes || []).join(" "));
  if (d.chart || MS.chartView) spendChart(d);
}

// the weekly chart: three lines (spend, revenue, CPA %) summing whatever mix of
// types of source and/or single sources is picked; nothing picked = every line
// on the sheet (overhead included) vs all revenue. Year-to-date counters follow.
function spendChart(d) {
  const view = MS.chartView;                 // a picked date range, or null = year to date
  const chart = view ? view.chart : d.chart;
  const bySrc = chart.by_source || {};
  const names = Object.keys(bySrc);
  const chanOf = {}; for (const r of (view ? view.rows : (chart.rows || [])).concat(d.rows)) chanOf[r.source] = r.channel || "Other";
  const chans = [...new Set(names.map((s) => chanOf[s] || "Other"))].sort();
  const all = chart.series["Spend (sheet)"] || [], allRev = chart.series["Revenue (dashboard)"] || [];
  const n = chart.weeks.length;
  MS.labelEvery = n <= 13 ? 1 : n <= 26 ? 2 : 4;
  // overhead spend per week = every line on the sheet minus the source lines
  const overhead = all.map((v, i) => v - names.reduce((acc, s) => acc + (bySrc[s].spend[i] || 0), 0));
  const picked = new Set([...names.filter((s) => MS.chartChan.has(chanOf[s])), ...[...MS.chartSrc].filter((s) => names.includes(s))]);
  const everything = !picked.size && !MS.chartOverhead;
  let spend, rev, label;
  if (everything) {
    spend = all; rev = allRev; label = "every line incl. overhead vs all revenue";
  } else {
    spend = Array.from({ length: n }, (_, i) => [...picked].reduce((acc, s) => acc + (bySrc[s].spend[i] || 0), 0) + (MS.chartOverhead ? overhead[i] : 0));
    rev = Array.from({ length: n }, (_, i) => [...picked].reduce((acc, s) => acc + (bySrc[s].revenue[i] || 0), 0));
    const parts = [...MS.chartChan].filter((c) => chans.includes(c)).concat([...MS.chartSrc].filter((s) => names.includes(s)));
    if (MS.chartOverhead) parts.push("overhead spend");
    label = parts.join(" + ") || "nothing picked";
  }
  const pct = spend.map((s, i) => (rev[i] ? Math.round((s / rev[i]) * 1000) / 10 : 0));
  drawWeekChart(MS, chart.weeks, { Spend: spend, Revenue: rev, "CPA %": pct });
  const tSpend = spend.reduce((a, b) => a + b, 0), tRev = rev.reduce((a, b) => a + b, 0);
  const usd = (v) => (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString();
  const yr = new Date().getFullYear();
  const lastW = chart.weeks[chart.weeks.length - 1];
  const lastEnd = (chart.week_ends && chart.week_ends[chart.week_ends.length - 1]) ||
    (lastW ? new Date(new Date(lastW + "T12:00:00").getTime() + 6 * 864e5).toISOString().slice(0, 10) : "");
  const through = lastEnd ? weekLabel(lastEnd) : "";
  const fmtFull = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  const span = view ? `${fmtFull(chart.weeks[0])} – ${fmtFull(lastEnd)} (complete weeks)` : `Jan 1 – ${through} (complete weeks)`;
  $("#ms-chart-sub").textContent = (view ? `${fmtFull(chart.weeks[0])} – ${fmtFull(lastEnd)}` : "this year") +
    " · spend and revenue on the left, CPA % on the right";
  $("#ms-ytd").innerHTML = `<div class="rev-cards">`
    + mkCard(view ? "Spend" : `${yr} spend to date`, usd(tSpend), `${label} · ${span}`)
    + mkCard(view ? "Revenue" : `${yr} revenue to date`, usd(tRev), `${label} · ${span}`)
    + mkCard(view ? "CPA % over these weeks" : "CPA % on the year", tRev ? (100 * tSpend / tRev).toFixed(1) + "%" : "–", `spend ÷ revenue · ${span}`)
    + `</div>`;
  // the picker: types of source, overhead, then single sources
  const body = $("#ms-chart-pick-body");
  const check = (label2, on, onChange) => {
    const lab = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = on;
    cb.addEventListener("change", () => { onChange(cb.checked); msSave(); });
    lab.append(cb, document.createTextNode(" " + label2));
    return lab;
  };
  const title = (t, top) => { const el = document.createElement("div"); el.className = "sv-title"; el.textContent = t; if (top) el.style.marginTop = "8px"; return el; };
  const acts = document.createElement("div"); acts.className = "picker-actions";
  const everyBtn = document.createElement("button"); everyBtn.type = "button"; everyBtn.textContent = "Everything";
  everyBtn.addEventListener("click", () => { MS.chartChan = new Set(); MS.chartSrc = new Set(); MS.chartOverhead = false; msSave(); });
  acts.appendChild(everyBtn);
  body.replaceChildren(acts, title("Type of source"),
    ...chans.map((c) => check(c, MS.chartChan.has(c), (on) => { on ? MS.chartChan.add(c) : MS.chartChan.delete(c); })),
    check("Overhead spend (payroll, fees, tooling)", MS.chartOverhead, (on) => { MS.chartOverhead = on; }),
    title("Single sources", true),
    ...names.map((s) => check(s, MS.chartSrc.has(s), (on) => { on ? MS.chartSrc.add(s) : MS.chartSrc.delete(s); })));
  $("#ms-chart-pick").querySelector("summary").textContent = everything ? "Chart: everything" : `Chart: ${label.length > 48 ? label.slice(0, 45) + "…" : label}`;
}

/* ---------- agent page: Schedule ----------
   A rolling calendar: Monday–Sunday columns, one row per week from last
   week through the last posted week, the month running down the left
   edge, the day number in every cell. Green = working, yellow = off,
   purple = PTO / unpaid, grey = not posted yet, today outlined. Click a
   day for who's on. */
async function loadMySchedule(asQ, who) {
  const box = $("#me-sched");
  box.replaceChildren(noData("Reading the schedule…"));
  let d = null;
  try { d = await (await fetch(`/api/my/schedule?x=1${asQ}`)).json(); }
  catch (e) { d = { error: "Couldn't reach the server." }; }
  if (!d || d.error || !d.weeks) { box.replaceChildren(noData((d && d.error) || "No schedule found.")); return; }
  if (who) $("#me-sched-title").textContent = `${who} — Schedule`;
  if (!d.weeks.some((w) => w.on_sheet)) {
    box.replaceChildren(noData("You're not on the posted schedule yet. Ask your manager if that's not right."));
    return;
  }
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // a week belongs to the month its Thursday is in (so the month label follows the majority of its days)
  const monthOf = (w) => { const t = new Date(w.start + "T12:00:00"); t.setDate(t.getDate() + 3); return `${t.getFullYear()}-${t.getMonth()}`; };
  const table = document.createElement("table"); table.className = "cal";
  table.innerHTML = `<thead><tr><th class="cal-m"></th>${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((x) => `<th>${x}</th>`).join("")}<th class="cal-hrs">Hrs</th></tr></thead>`;
  const tb = document.createElement("tbody");
  const groups = [];
  for (const w of d.weeks) {
    const k = monthOf(w);
    if (groups.length && groups[groups.length - 1].key === k) groups[groups.length - 1].weeks.push(w);
    else groups.push({ key: k, weeks: [w] });
  }
  for (const g of groups) {
    g.weeks.forEach((w, i) => {
      const tr = document.createElement("tr");
      if (i === 0) {
        const m = document.createElement("td"); m.className = "cal-m"; m.rowSpan = g.weeks.length;
        const [y, mo] = g.key.split("-").map(Number);
        m.textContent = `${MONTHS[mo]} ${y}`;
        tr.appendChild(m);
      }
      for (const day of w.days) {
        const dt = new Date(day.date + "T12:00:00");
        const td = document.createElement("td");
        const kind = !w.on_sheet ? "none" : day.off ? "pto" : day.hours ? "work" : "off";
        td.className = "cal-day " + kind + (day.today ? " today" : "") + (day.past && !day.today ? " past" : "");
        const shift = kind === "none" ? "" : day.off ? day.shift : day.hours ? day.shift : "Off";
        td.innerHTML = `<div class="cal-num">${dt.getDate() === 1 ? MONTHS[dt.getMonth()] + " " + dt.getDate() : dt.getDate()}</div>` +
          `<div class="cal-shift">${shift}</div>` + (day.hours ? `<div class="cal-h">${day.hours}h</div>` : "");
        td.title = "Who's on this day";
        td.addEventListener("click", () => openDayRoster(day.date, asQ, who));
        tr.appendChild(td);
      }
      const hrs = document.createElement("td"); hrs.className = "cal-hrs";
      hrs.innerHTML = w.on_sheet ? `${w.hours}${w.current ? `<span class="sub">this wk</span>` : ""}` : `<span class="sub">—</span>`;
      tr.appendChild(hrs);
      tb.appendChild(tr);
    });
  }
  table.appendChild(tb);
  const legend = document.createElement("div"); legend.className = "cal-legend";
  legend.innerHTML = `<span class="work">Working</span><span class="off">Off</span><span class="pto">PTO / unpaid</span><span class="none">Not posted yet</span>`;
  const wrap = document.createElement("div"); wrap.className = "cal-wrap";
  wrap.append(legend, table);
  box.replaceChildren(wrap);
  noteInto("#me-sched", `Posted by your managers (the IS Scheduling sheet until a week is published from the dashboard), re-read every 10 minutes` +
    (d.sheet_name ? ` · listed there as “${d.sheet_name}”` : "") + `. The calendar grows a week at a time as new weeks are posted. Click a day to see who's on.`, true);
}

// who's on a given day: every agent's shift, earliest start first, then who's off
async function openDayRoster(iso, asQ, who) {
  let ov = $("#day-roster-overlay");
  if (!ov) {
    ov = document.createElement("div"); ov.id = "day-roster-overlay"; ov.className = "modal-overlay";
    ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
    document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { ov.remove(); document.removeEventListener("keydown", esc); } });
    document.body.appendChild(ov);
  }
  const card = document.createElement("div"); card.className = "modal-card day-roster";
  const label = new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
  card.innerHTML = `<div class="modal-head" style="display:flex;justify-content:space-between;align-items:flex-start">` +
    `<div><b>${label}</b><span class="sub">who's on, earliest start first</span></div><button class="sd-close" title="Close">×</button></div>` +
    `<div class="dr-body">${noData("Loading…").outerHTML}</div>`;
  card.querySelector(".sd-close").addEventListener("click", () => ov.remove());
  ov.replaceChildren(card);
  let d = null;
  try { d = await (await fetch(`/api/my/schedule_day?day=${iso}${asQ}`)).json(); }
  catch (e) { d = { error: "Couldn't reach the server." }; }
  const body = card.querySelector(".dr-body");
  if (!d || d.error) { body.replaceChildren(noData((d && d.error) || "No data.")); return; }
  if (!d.posted) { body.replaceChildren(noData("That week isn't posted on the schedule yet.")); return; }
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const rows = d.on.map((r) => `<tr${who && r.name === who ? ' class="me"' : ""}><td>${esc(r.name)}</td><td>${esc(r.shift)}</td><td>${r.hours}h</td></tr>`).join("");
  body.innerHTML = (rows ? `<table>${rows}</table>` : noData("Nobody is scheduled that day.").outerHTML) +
    (d.off.length ? `<div class="dr-off"><b>Off:</b> ${d.off.map((r) => esc(r.name)).join(", ")}</div>` : "");
}

/* ---------- time clock (agent page) ---------- */
const CK = { timer: null };
async function loadClock(asQ, preview) {
  const box = $("#me-clock");
  if (!box) return;
  let d = null;
  try { d = await (await fetch(`/api/my/clock?x=1${asQ}`)).json(); } catch (e) { d = null; }
  if (!d || d.error) { box.replaceChildren(noData((d && d.error) || "Couldn't load the clock.")); return; }
  renderClock(d, asQ, preview);
  clearTimeout(CK.timer);
  CK.timer = setTimeout(() => { if ($("#mesub-schedule").classList.contains("active")) loadClock(asQ, preview); }, 60000);
}
function renderClock(d, asQ, preview) {
  const box = $("#me-clock");
  const t = clockTime;
  const on = !!d.open;
  box.className = "clock-card" + (on ? " on" : "");
  const btn = document.createElement("button");
  btn.className = "ck-btn" + (on ? " out" : "");
  btn.textContent = on ? "Clock out" : "Clock in";
  const g = !on && d.guard ? d.guard : null;
  btn.disabled = !!preview || !!g;
  btn.title = preview ? "Preview only — agents clock in from their own login" : g ? g.blocked : "";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const r = await fetch(on ? "/api/my/clock/out" : "/api/my/clock/in", { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) toast(j.error || "That didn't work.", "warn");
    loadClock(asQ, preview);
  });
  const st = document.createElement("div");
  const shift = d.shift ? `Today's shift ${d.shift.cell} (${t(d.shift.start)} – ${t(d.shift.end)})` : "No shift posted for today";
  st.innerHTML = `<div class="ck-status"><span class="ck-dot"></span>${on ? "Clocked in since " + t(d.open.in_at) : "Clocked out"}</div>` +
    `<div class="ck-sub">${shift}${d.shift && on && (!d.auto || d.auto.enabled) ? ` · you'll be clocked out automatically ${d.grace_min} min after the shift ends` : ""}</div>` +
    (g ? `<div class="ck-guard">${g.blocked}</div>` : "");
  if (g && g.allowed_from) {
    // the button frees itself when the window opens, without a reload
    const ms = new Date(g.allowed_from).getTime() - Date.now();
    if (ms > 0 && ms < 6 * 3600e3) { clearTimeout(CK.guardTimer); CK.guardTimer = setTimeout(() => loadClock(asQ, preview), ms + 1500); }
  }
  const p = document.createElement("div"); p.className = "ck-punches";
  p.innerHTML = `<div><b>${d.worked_today_h}h</b> today</div>` +
    d.today_punches.map((x) => `<div>${t(x.in_at)} – ${x.out_at ? t(x.out_at) : "…"}${x.out_kind === "auto" ? " (auto)" : ""}</div>`).join("");
  box.replaceChildren(btn, st, p);
}

/* ---------- PTO (agent page) ---------- */
async function loadMyPto(asQ) {
  const box = $("#me-pto");
  if (!box) return;
  let d = null;
  try { d = await (await fetch(`/api/my/pto?x=1${asQ}`)).json(); } catch (e) { d = null; }
  if (!d || d.error) { box.replaceChildren(); return; }
  const fmtD = (iso) => (iso ? new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "–");
  box.innerHTML = `<div><div class="pto-lbl">PTO balance</div><div class="pto-big">${d.balance.toFixed(2)}<span style="font-size:14px;color:var(--muted)"> h</span></div></div>` +
    `<div><div class="pto-lbl">This year</div><div style="font-size:14px"><b>+${d.ytd_accrued.toFixed(2)}</b> earned · <b>${d.ytd_used.toFixed(2)}</b> used</div>` +
    `<div class="pto-lbl" style="margin-top:6px">Start date</div><div style="font-size:14px">${fmtD(d.start_date)}</div></div>` +
    `<div class="pto-detail"><span class="pto-status ${d.status === "accruing" ? "accruing" : d.status === "probation" ? "probation" : d.status === "at cap" ? "cap" : ""}">${d.status}</span><br>${d.detail} ` +
    `${new Date() >= new Date("2027-01-01T00:00:00") ? `Balance is capped at ${d.cap} hours.` : `The ${d.cap}-hour cap starts Jan 1, 2027.`}</div>`;
  noteInto("#me-pto", "PTO per company policy 4-09: full-time agents (30+ hours a week) earn 2 hours per 80 worked in their first two years and 3 per 80 after, " +
    "starting 180 days after their start date. From Jan 1, 2027 the balance is capped at 80 hours (anything above 80 that day is cut to 80); until then there's no cap. Only hours actually worked earn PTO: the dashboard time clock from Oct 26, 2026 (each finished day). " +
    "Balances were carried over from Homebase as of Oct 25, 2026.", true);
}

/* ---------- IS Managers: Timesheets + Agents ---------- */
const TS = { data: null, range: null, open: null };
/* ---------- hours report (Timesheets tab): regular / daily OT / weekly OT / PTO per agent ----------
   For payroll: pick dates (defaults to the last closed pay period), check it on screen, download CSV. */
const HR = { start: null, end: null };
async function loadHoursReport(start, end) {
  const box = $("#hr-report");
  if (!box) return;
  const q = start && end ? `?start_day=${start}&end_day=${end}` : "";
  box.replaceChildren(noData("Loading…"));
  let d = null, err = null;
  try { const r = await fetch("/api/managers/hours_report" + q); d = await r.json(); if (!r.ok) { err = d.error; d = null; } } catch (e) { d = null; }
  const ctl = document.createElement("div"); ctl.className = "to-form hr-ctl";
  ctl.innerHTML = `<label>From <input type="date" id="hrr-from"></label><label>To <input type="date" id="hrr-to"></label>` +
    `<button id="hrr-go">Show</button><a class="year-btn" id="hrr-csv" href="#">&#x2913; Download CSV</a><span id="hrr-warn"></span>`;
  ctl.querySelector("#hrr-from").value = (d && d.start) || start || "";
  ctl.querySelector("#hrr-to").value = (d && d.end) || end || "";
  const go = () => {
    const a = ctl.querySelector("#hrr-from").value, b = ctl.querySelector("#hrr-to").value;
    if (!a || !b) { toast("Pick both dates.", "warn"); return; }
    loadHoursReport(a, b);
  };
  ctl.querySelector("#hrr-go").addEventListener("click", go);
  const csv = ctl.querySelector("#hrr-csv");
  csv.addEventListener("click", (e) => {
    const a = ctl.querySelector("#hrr-from").value, b = ctl.querySelector("#hrr-to").value;
    if (!a || !b) { e.preventDefault(); toast("Pick both dates.", "warn"); return; }
    csv.href = `/api/managers/hours_report.csv?start_day=${a}&end_day=${b}`;
  });
  if (!d) { box.replaceChildren(ctl, noData(err || "Couldn't load the report.")); return; }
  HR.start = d.start; HR.end = d.end;
  if (d.open_punches.length) {
    const w = ctl.querySelector("#hrr-warn"); w.className = "badge warn";
    const names = [...new Set(d.open_punches.map((p) => p.agent))];
    w.textContent = `${d.open_punches.length} punch${d.open_punches.length === 1 ? "" : "es"} still open`;
    w.title = `Still clocked in (counted up to now): ${names.join(", ")}. Close them on the Timesheets table before running payroll.`;
  }
  const f = (v) => (v ? v.toFixed(2) : "–");
  const t = document.createElement("table"); t.className = "tp-table hrs-rep-table";
  t.innerHTML = `<thead><tr><th>Agent</th><th>Regular hours</th><th>Daily OT</th><th>Weekly OT</th><th>PTO</th></tr></thead>`;
  const tb = document.createElement("tbody");
  if (!d.agents.length) {
    const tr = document.createElement("tr"); tr.innerHTML = `<td colspan="5">No hours or PTO in this range.</td>`; tb.appendChild(tr);
  }
  for (const r of d.agents) {
    const tr = document.createElement("tr");
    const nm = document.createElement("td"); nm.className = "nm"; nm.textContent = r.agent; tr.appendChild(nm);
    for (const k of ["regular_h", "daily_ot_h", "weekly_ot_h", "pto_h"]) { const td = document.createElement("td"); td.textContent = f(r[k]); tr.appendChild(td); }
    tb.appendChild(tr);
  }
  if (d.agents.length) {
    const tr = document.createElement("tr"); tr.className = "tot";
    tr.innerHTML = `<td class="nm">Total</td>${["regular_h", "daily_ot_h", "weekly_ot_h", "pto_h"].map((k) => `<td>${f(d.totals[k])}</td>`).join("")}`;
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  box.replaceChildren(ctl, t);
  noteInto("#hr-report", "Hours worked on the time clock, per agent, for the dates picked (it opens on the last closed pay period). " +
    "Daily OT = hours over 8 in a day; Weekly OT = hours past 40 in a Sunday–Saturday week. An hour is never counted twice: once a " +
    "week passes 40, the rest of that week is weekly OT, not daily. Weeks are always counted from their Sunday, so a range that starts " +
    "mid-week still includes the hours worked earlier that week when deciding overtime. Regular = everything else. PTO = approved PTO " +
    "taken on those days (the paid part of a PTO + unpaid request; unpaid time off isn't included). The CSV has the same columns plus a total row. " +
    "Matches the Timesheets tab for the same dates.", true);
}

function showManagersSub(sub) {
  document.querySelectorAll("#managers-subtabs button").forEach((b) => b.classList.toggle("active", b.dataset.gsub === sub));
  document.querySelectorAll(".gsubpanel").forEach((p) => p.classList.toggle("active", p.id === "gsub-" + sub));
  if (sub === "timesheets") { ensureLoaded("ts"); loadClockRules(); }
  if (sub === "reports") loadHoursReport(HR.start, HR.end);
  if (sub === "adherence") { ensureLoaded("adh"); }
  if (sub === "agents") loadHr();
  if (sub === "schedule") { loadBuilder(); loadLive(); startLiveTimer(); }
  if (sub === "templates") loadTemplates();
  if (sub === "timeoff") loadTimeOffQueue();
}

/* ---------- time off: agent request form + list ---------- */
async function loadMyTimeOff(asQ, preview) {
  const box = $("#me-timeoff");
  if (!box) return;
  let d = null;
  try { d = await (await fetch(`/api/my/timeoff?x=1${asQ}`)).json(); } catch (e) { d = null; }
  if (!d || d.error) { box.replaceChildren(); return; }
  const fmtD = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const form = document.createElement("div"); form.className = "to-form";
  const today = new Date().toISOString().slice(0, 10);
  form.innerHTML = `<label>From <input type="date" id="to-start" value="${today}"></label><label>To <input type="date" id="to-end" value="${today}"></label>` +
    `<label>Type <select id="to-kind"><option value="pto">PTO</option><option value="unpaid">Unpaid time off</option></select></label>` +
    `<label>PTO hours <input type="number" id="to-hours" step="0.25" placeholder="auto" style="width:80px" title="Leave blank to use your scheduled hours on those days"></label>` +
    `<label>Note <input type="text" id="to-note" placeholder="optional" style="width:200px"></label><button id="to-send" ${preview ? "disabled" : ""}>Request</button>`;
  const send = async () => {
    const b = form.querySelector("#to-send"); b.disabled = true;
    const r = await fetch("/api/my/timeoff", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start: form.querySelector("#to-start").value, end: form.querySelector("#to-end").value, kind: form.querySelector("#to-kind").value,
        hours: form.querySelector("#to-hours").value, note: form.querySelector("#to-note").value }) });
    const j = await r.json().catch(() => ({}));
    b.disabled = false;
    if (!r.ok) { toast(j.error || "Couldn't send that.", "warn"); return; }
    if (j.message) alert(j.message);
    toast(`Request sent${j.hours ? ` for ${j.hours} PTO hours` : ""}${j.unpaid_hours ? ` (${j.unpaid_hours} h unpaid)` : ""}.`, "ok");
    loadMyTimeOff(asQ, preview); loadMyPto(asQ);
  };
  form.querySelector("#to-send").addEventListener("click", send);
  const prev = document.createElement("div"); prev.className = "to-preview";
  let prevTimer = null;
  const refreshPreview = async () => {
    const a = form.querySelector("#to-start").value, b = form.querySelector("#to-end").value || a;
    if (!a) { prev.replaceChildren(); return; }
    let p = null;
    try { p = await (await fetch(`/api/my/timeoff/preview?start=${a}&end=${b}${asQ}`)).json(); } catch (e) { p = null; }
    if (!p || p.error) { prev.innerHTML = `<span class="sub">${(p && p.error) || "Couldn't check that range."}</span>`; return; }
    const pj = p.projected;
    const fmtD2 = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
    const earn = pj.accrual_ahead > 0 ? ` (${pj.balance.toFixed(2)} now − ${pj.pending.toFixed(2)} pending + ${pj.accrual_ahead.toFixed(2)} you should earn by then)` :
      pj.pending > 0 ? ` (${pj.balance.toFixed(2)} now − ${pj.pending.toFixed(2)} already pending)` : "";
    const hrs = p.posted ? `<b>${p.scheduled_h.toFixed(2)} h</b> scheduled over ${p.days.length} day${p.days.length === 1 ? "" : "s"}` :
      `nothing posted for those days yet — a blank request counts <b>${p.default_h.toFixed(0)} h</b> (8 per weekday)`;
    const short = pj.projected < (p.posted ? p.scheduled_h : p.default_h) - 0.005;
    const offs = p.days.filter((x) => x.others.length);
    const who = offs.length ? `<div class="to-others">Also off: ` + offs.map((x) => `<span><b>${fmtD2(x.day)}</b> ${x.others.map((o) => o.name.split(" ")[0] + (o.status === "pending" ? " <i>(pending)</i>" : "")).join(", ")}</span>`).join("") + `</div>` :
      `<div class="to-others sub">Nobody else has time off on those days.</div>`;
    prev.innerHTML = `<div>Projected PTO on ${fmtD2(a)}: <b class="${short ? "warn" : "ok"}">${pj.projected.toFixed(2)} h</b><span class="sub">${earn}</span></div>` +
      `<div>${hrs}${short ? ` <span class="warn">· more than you'll have, so part would be unpaid</span>` : ""}</div>` + who;
  };
  for (const id of ["#to-start", "#to-end"]) form.querySelector(id).addEventListener("change", () => { clearTimeout(prevTimer); prevTimer = setTimeout(refreshPreview, 200); });
  refreshPreview();
  const list = document.createElement("div"); list.className = "to-list";
  if (!d.requests.length) list.textContent = "No requests yet.";
  for (const r of d.requests) {
    const row = document.createElement("div"); row.className = "to-row";
    row.innerHTML = `<span class="st ${r.status}">${r.status}</span><b>${fmtD(r.start_day)}${r.end_day !== r.start_day ? " – " + fmtD(r.end_day) : ""}</b>` +
      `<span>${toKind(r)}</span><span class="sub">${r.note || ""}</span>` +
      (r.decision_note ? `<span class="sub">manager: ${r.decision_note}</span>` : "");
    if (r.status === "pending" && !preview) {
      const c = document.createElement("button"); c.textContent = "Cancel";
      c.addEventListener("click", async () => {
        const rr = await fetch("/api/my/timeoff/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: r.id }) });
        if (rr.ok) loadMyTimeOff(asQ, preview); else toast("Couldn't cancel.", "warn");
      });
      row.appendChild(c);
    }
    list.appendChild(row);
  }
  const h = document.createElement("h4"); h.textContent = `Time off · ${d.pto.available.toFixed(2)} h PTO available`;
  box.replaceChildren(h, form, prev, list);
  noteInto("#me-timeoff", "Leave PTO hours blank and the request uses your scheduled hours for those days (8 per weekday when nothing is posted). " +
    "A manager approves or denies it; approved PTO comes off your balance day by day and shows as PTO on your schedule. " +
    "The projected balance counts what you should earn between now and the first day off at your recent pace, less any request still pending; " +
    "if the request needs more than that, the rest goes in as unpaid.", true);
}

const toKind = (r) => (r.kind === "mixed" ? `${r.hours} h PTO + ${r.unpaid_hours} h unpaid` : r.kind === "pto" ? `${r.hours} h PTO` : `${r.unpaid_hours || ""} h unpaid`.trim());

/* ---------- time off: manager queue ----------
   Pending / approved / recent requests, each with Edit; "Add time off" enters
   one for an agent; "Look up an agent" lists everything they've had so older
   requests can be changed too. Add and Edit share timeOffEditor(). */
const TOQ = { agents: [], lookup: "" };
const escHtml = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
async function loadTimeOffQueue() {
  const fmtD = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  let d = null;
  const q = TOQ.lookup ? `?agent=${encodeURIComponent(TOQ.lookup)}` : "";
  try { d = await (await fetch("/api/managers/timeoff" + q)).json(); } catch (e) { d = null; }
  if (!d || !d.pending) { $("#to-pending").replaceChildren(noData("Couldn't load.")); return; }
  TOQ.agents = d.agents || [];
  const add = $("#to-add");
  if (add && !add.dataset.bound) { add.dataset.bound = "1"; add.addEventListener("click", () => timeOffEditor(null)); }
  const post = async (path, body) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.error || "That didn't work.", "warn"); return false; }
    return j;
  };
  const row = (r, buttons) => {
    const el = document.createElement("div"); el.className = "to-row";
    el.innerHTML = `<span class="st ${escHtml(r.status)}">${escHtml(r.status)}</span><b>${escHtml(r.agent)}</b><span>${fmtD(r.start_day)}${r.end_day !== r.start_day ? " – " + fmtD(r.end_day) : ""}</span>` +
      `<span>${escHtml(toKind(r))}</span>` + (r.balance != null ? `<span class="sub">balance ${r.balance.toFixed(2)} h</span>` : "") +
      (r.note ? `<span class="sub">“${escHtml(r.note)}”</span>` : "") + (r.decision_note ? `<span class="sub">manager: ${escHtml(r.decision_note)}</span>` : "") +
      (r.entered_by ? `<span class="sub" title="Entered by a manager">entered by ${escHtml(r.entered_by)}</span>` : "") +
      (r.edited_by ? `<span class="sub" title="${escHtml(r.edited_at || "")}">edited by ${escHtml(r.edited_by)}</span>` : "");
    for (const b of buttons) el.appendChild(b);
    return el;
  };
  const btn = (label, cls, fn) => { const b = document.createElement("button"); b.className = cls; b.textContent = label; b.addEventListener("click", fn); return b; };
  const edit = (r) => btn("Edit", "", () => timeOffEditor(r));
  const pend = document.createElement("div"); pend.className = "to-list";
  if (!d.pending.length) pend.textContent = "Nothing waiting.";
  for (const r of d.pending) {
    const note = document.createElement("input"); note.placeholder = "note to the agent (optional)";
    pend.appendChild(row(r, [note,
      btn("Approve", "ok", async () => { if (await post("/api/managers/timeoff/decide", { id: r.id, approve: true, note: note.value })) { toast("Approved.", "ok"); loadTimeOffQueue(); } }),
      btn("Deny", "no", async () => { if (await post("/api/managers/timeoff/decide", { id: r.id, approve: false, note: note.value })) { toast("Denied.", "ok"); loadTimeOffQueue(); } }),
      edit(r)]));
  }
  $("#to-pending").replaceChildren(pend);
  const appr = document.createElement("div"); appr.className = "to-list";
  if (!d.approved.length) appr.textContent = "Nothing approved in this window.";
  for (const r of d.approved) {
    appr.appendChild(row(r, [edit(r), btn("Revoke", "no", async () => {
      if (!confirm(`Revoke ${r.agent}'s approved time off? The PTO hours go back on their balance.`)) return;
      if (await post("/api/managers/timeoff/revoke", { id: r.id })) loadTimeOffQueue();
    })]));
  }
  $("#to-approved").replaceChildren(appr);
  const rec = document.createElement("div"); rec.className = "to-list";
  if (!d.recent.length) rec.textContent = "None in the last 30 days.";
  for (const r of d.recent) rec.appendChild(row(r, [edit(r)]));
  $("#to-recent").replaceChildren(rec);
  // look up one agent: every request they've had
  const lk = document.createElement("div");
  const pick = document.createElement("div"); pick.className = "to-form";
  const sel = document.createElement("select");
  sel.append(new Option("Pick an agent…", ""));
  for (const a of TOQ.agents) sel.append(new Option(a, a));
  sel.value = TOQ.lookup;
  sel.addEventListener("change", () => { TOQ.lookup = sel.value; loadTimeOffQueue(); });
  pick.appendChild(sel);
  if (d.history_agent) {
    const s = document.createElement("span"); s.className = "sub"; s.style.alignSelf = "center";
    s.textContent = `PTO balance ${d.history_balance.toFixed(2)} h · ${d.history.length} request${d.history.length === 1 ? "" : "s"}`;
    pick.appendChild(s);
  }
  lk.appendChild(pick);
  if (d.history_agent) {
    const hl = document.createElement("div"); hl.className = "to-list";
    if (!d.history.length) hl.textContent = "No requests yet.";
    for (const r of d.history) hl.appendChild(row(r, [edit(r)]));
    lk.appendChild(hl);
  }
  $("#to-lookup").replaceChildren(lk);
  noteInto("#to-pending", "Approving a PTO request takes the hours off the agent's balance day by day (split by their scheduled hours, or evenly across " +
    "weekdays when nothing is posted) and marks those days PTO on every schedule view, including the builder's coverage. Denying leaves the balance alone. " +
    "Revoke undoes an approval. “Add time off” enters a request for an agent, and Edit changes any request — dates, type, the exact PTO and unpaid hours, " +
    "notes and status; approved PTO is re-taken from the balance with the new numbers. A manager entry isn't held to the agent's PTO balance " +
    "(it can go below zero; you'll get a warning). Nothing is sent by email yet — agents see the change on their page.", true);
}

// Add (r = null) or edit a time-off request with full control.
function timeOffEditor(r) {
  document.querySelectorAll("#to-editor").forEach((x) => x.remove());
  const isNew = !r;
  const today = clockLocal(new Date().toISOString()).slice(0, 10);
  const ov = document.createElement("div"); ov.className = "modal-overlay"; ov.id = "to-editor";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div"); card.className = "modal-card to-card";
  const status0 = isNew ? "approved" : r.status === "cancelled" ? "approved" : r.status;
  card.innerHTML =
    `<div class="modal-head"><b>${isNew ? "Add time off" : "Edit time off"}</b><span class="sub">${isNew ? "Entered by you on the agent's behalf" :
      `${escHtml(r.agent)} · ${escHtml(r.status)}${r.status === "cancelled" ? " (saving brings it back)" : ""}`}</span></div>` +
    (isNew ? `<label class="modal-field">Agent <select id="te-agent"><option value="">Pick an agent…</option>${TOQ.agents.map((a) => `<option>${escHtml(a)}</option>`).join("")}</select></label>` : "") +
    `<div class="te-row"><label class="modal-field">From <input type="date" id="te-start"></label><label class="modal-field">To <input type="date" id="te-end"></label></div>` +
    `<div class="te-row"><label class="modal-field">Type <select id="te-kind"><option value="pto">PTO</option><option value="unpaid">Unpaid</option><option value="mixed">PTO + unpaid</option></select></label>` +
    `<label class="modal-field" id="te-h-wrap">PTO hours <input type="number" step="0.25" min="0" id="te-h" placeholder="scheduled hours"></label>` +
    `<label class="modal-field" id="te-u-wrap">Unpaid hours <input type="number" step="0.25" min="0" id="te-u" placeholder="scheduled hours"></label></div>` +
    `<div class="te-hint" id="te-hint"></div>` +
    `<label class="modal-field">Status <select id="te-status"><option value="approved">Approved</option><option value="pending">Pending (waiting on a decision)</option><option value="denied">Denied</option></select></label>` +
    `<label class="modal-field">Agent's note <input type="text" id="te-note" placeholder="optional"></label>` +
    `<label class="modal-field">Manager note <input type="text" id="te-dnote" placeholder="optional — the agent sees this"></label>` +
    `<div class="modal-actions">${!isNew && r.status !== "cancelled" ? `<button class="year-btn modal-remove" id="te-remove">Remove request</button>` : ""}` +
    `<span class="modal-spacer"></span><button class="year-btn" id="te-cancel">Cancel</button><button class="year-btn active" id="te-save">Save</button></div>`;
  ov.appendChild(card); document.body.appendChild(ov);
  const $c = (s) => card.querySelector(s);
  $c("#te-start").value = isNew ? today : r.start_day;
  $c("#te-end").value = isNew ? today : r.end_day;
  $c("#te-kind").value = isNew ? "pto" : r.kind;
  if (!isNew) {
    if (r.hours != null) $c("#te-h").value = r.hours;
    if (r.unpaid_hours != null) $c("#te-u").value = r.unpaid_hours;
    $c("#te-note").value = r.note || "";
    $c("#te-dnote").value = r.decision_note || "";
  }
  $c("#te-status").value = status0;
  const agent = () => (isNew ? $c("#te-agent").value : r.agent);
  const syncKind = () => {
    const k = $c("#te-kind").value;
    $c("#te-h-wrap").style.display = k === "unpaid" ? "none" : "";
    $c("#te-u-wrap").style.display = k === "pto" ? "none" : "";
    const ph = k === "mixed" ? "required" : "scheduled hours";
    $c("#te-h").placeholder = ph; $c("#te-u").placeholder = ph;
  };
  syncKind();
  // helper line: scheduled hours on those days, projected PTO balance, who else is off
  let seq = 0;
  const hint = async () => {
    const a = agent(), s = $c("#te-start").value, e = $c("#te-end").value || s;
    const box = $c("#te-hint");
    if (!a || !s) { box.textContent = ""; return; }
    const my = ++seq;
    let p = null;
    try { const res = await fetch(`/api/managers/timeoff/preview?agent=${encodeURIComponent(a)}&start=${s}&end=${e}`); p = res.ok ? await res.json() : null; } catch (x) { p = null; }
    if (my !== seq) return;
    if (!p) { box.textContent = ""; return; }
    const others = [...new Set(p.days.flatMap((x) => x.others.filter((o) => o.name !== a).map((o) => o.name + (o.status === "pending" ? " (pending)" : ""))))];
    // an approved or pending request's own PTO is already taken off the projection: add it back
    const own = !isNew && (r.status === "approved" || r.status === "pending") && r.hours ? r.hours : 0;
    box.textContent = `${p.posted ? `Scheduled ${p.scheduled_h} h on those days` : `No shifts posted — default ${p.default_h} h (8 per weekday)`}` +
      ` · PTO balance by ${new Date(s + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}` +
      `${own ? " not counting this request" : ""}: ${(p.projected.projected + own).toFixed(2)} h` +
      (others.length ? ` · Also off: ${others.join(", ")}` : "");
  };
  for (const id of ["#te-start", "#te-end"]) $c(id).addEventListener("change", () => {
    if (id === "#te-start" && $c("#te-end").value < $c("#te-start").value) $c("#te-end").value = $c("#te-start").value;
    hint();
  });
  if (isNew) $c("#te-agent").addEventListener("change", hint);
  $c("#te-kind").addEventListener("change", syncKind);
  hint();
  $c("#te-cancel").addEventListener("click", () => ov.remove());
  const post = async (path, body) => {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) { toast(j.error || "That didn't work.", "warn"); return null; }
    return j;
  };
  if ($c("#te-remove")) $c("#te-remove").addEventListener("click", async () => {
    if (!confirm(`Remove ${r.agent}'s request? Any PTO it took goes back on their balance.`)) return;
    if (await post("/api/managers/timeoff/revoke", { id: r.id, note: $c("#te-dnote").value || "removed by a manager" })) { ov.remove(); toast("Removed.", "ok"); loadTimeOffQueue(); }
  });
  $c("#te-save").addEventListener("click", async () => {
    const k = $c("#te-kind").value;
    const body = { id: isNew ? null : r.id, agent: agent(), start: $c("#te-start").value, end: $c("#te-end").value || $c("#te-start").value,
      kind: k, hours: k === "unpaid" ? null : $c("#te-h").value, unpaid_hours: k === "pto" ? null : $c("#te-u").value,
      status: $c("#te-status").value, note: $c("#te-note").value, decision_note: $c("#te-dnote").value };
    if (k === "unpaid") body.hours = null;
    const res = await post("/api/managers/timeoff/save", body);
    if (!res) return;
    ov.remove();
    toast(res.warning ? `Saved. ${res.warning}` : `Saved — PTO balance now ${res.balance.toFixed(2)} h.`, res.warning ? "warn" : "ok");
    loadTimeOffQueue();
  });
}

/* ---------- the IS team's clock ----------
   Shift times, punches and timesheets are Mountain time (the server's
   CLOCK_TZ), whatever zone the viewer's computer is set to. clockLocal() fills
   a datetime-local box in Mountain; the server reads a typed time as Mountain. */
const CLOCK_TZ = "America/Denver";
function clockTime(iso) {
  return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: CLOCK_TZ });
}
function clockLocal(iso) {
  if (!iso) return "";
  const parts = {};
  for (const x of new Intl.DateTimeFormat("en-US", { timeZone: CLOCK_TZ, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso))) parts[x.type] = x.value;
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

/* ---------- schedule builder ----------
   Week grid (agents × Mon–Sun). Click a cell to set a shift from presets or
   type one in the sheet's notation; drag a shift onto another cell to move
   it (hold Alt/Option to copy). The coverage strip is recalculated from the
   server after every change. Publish makes the week what agents see and what
   the time clock uses; until then the sheet's block stands. */
const SB = { monday: null, data: null, sort: { key: "agent", asc: true } };
const isoDay = (d) => d.toISOString().slice(0, 10);
function sbMonday(offsetWeeks) {
  const d = new Date(); d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7) + 7 * offsetWeeks);
  return isoDay(d);
}
async function loadBuilder(monday) {
  SB.monday = monday || SB.monday || sbMonday(0);
  $("#sb-grid").replaceChildren(noData("Loading…"));
  let d = null;
  try { d = await (await fetch(`/api/managers/schedule?monday=${SB.monday}`)).json(); } catch (e) { d = null; }
  if (!d || d.error) { $("#sb-grid").replaceChildren(noData((d && d.error) || "Couldn't load.")); return; }
  SB.data = d;
  renderBuilder(d);
  loadSaturdays();
}
async function sbPost(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.error || "That didn't work.", "warn"); return null; }
  return j;
}
function renderBuilder(d) {
  const fmt = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
  // week nav + status
  const ctl = $("#sb-controls"); ctl.replaceChildren();
  const nav = document.createElement("span"); nav.className = "sb-week";
  const prev = document.createElement("button"); prev.textContent = "◀"; prev.addEventListener("click", () => loadBuilder(isoDay(new Date(new Date(d.monday + "T12:00:00").getTime() - 7 * 864e5))));
  const next = document.createElement("button"); next.textContent = "▶"; next.addEventListener("click", () => loadBuilder(isoDay(new Date(new Date(d.monday + "T12:00:00").getTime() + 7 * 864e5))));
  const today = document.createElement("button"); today.textContent = "This week"; today.addEventListener("click", () => loadBuilder(sbMonday(0)));
  nav.append(prev, document.createTextNode(`${fmt(d.monday)} – ${fmt(d.sunday)}`), next, today);
  ctl.appendChild(nav);
  // actions
  const acts = $("#sb-actions"); acts.replaceChildren();
  const pill = document.createElement("span");
  pill.className = "pill " + (d.published ? "pub" : d.exists ? "" : "none");
  pill.textContent = d.published ? "Published" : d.exists ? "Draft — agents can't see this week until you publish it" : "Not created — agents see the sheet";
  acts.appendChild(pill);
  const btn = (label, fn, primary) => { const b = document.createElement("button"); b.className = "year-btn" + (primary ? " active" : ""); b.textContent = label; b.addEventListener("click", fn); acts.appendChild(b); return b; };
  if (!d.exists) {
    if (d.prev_week_exists) btn("Create from last week", () => createWeek("last"), true);
    if (d.sheet_has_week) { const tb = btn("Template week", () => createWeek("sheet"), !d.prev_week_exists); tb.title = "This week's block from the IS Scheduling sheet"; }
    for (const t of d.templates) btn(`Template: ${t.name}`, () => createWeek("template", t.id));
    btn("Blank week", () => createWeek("blank"));
  } else {
    btn(d.published ? "Unpublish" : "Publish to agents", async () => {
      if (d.published && !confirm("Unpublish this week? Agents go back to seeing the sheet's version.")) return;
      if (await sbPost("/api/managers/schedule/publish", { monday: d.monday, on: !d.published })) loadBuilder();
    }, !d.published);
    const st = btn("Save as template…", () => { TP.prefillWeek = d.monday; showManagersSub("templates"); });
    st.title = "Start a new template from this week on the Templates tab";
    btn("Delete week", async () => {
      if (!confirm("Delete every shift in this week? Agents will see the sheet's version again.")) return;
      if (await sbPost("/api/managers/schedule/week", { action: "delete", monday: d.monday })) loadBuilder();
    });
  }
  async function createWeek(source, template_id) {
    const r = await sbPost("/api/managers/schedule/week", { action: "create", monday: d.monday, source, template_id });
    if (r) { toast(`Week created with ${r.shifts} shifts${r.skipped_time_off ? ` (${r.skipped_time_off} skipped — approved time off)` : ""}.`, "ok"); loadBuilder(); }
  }
  // grid
  const days = [...Array(7)].map((_, k) => isoDay(new Date(new Date(d.monday + "T12:00:00").getTime() + k * 864e5)));
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const longDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  const grid = shiftGrid({
    id: "sb", rows: d.agents, editable: d.exists, sort: SB.sort, rerender: () => renderBuilder(d),
    heads: days.map((x, k) => `${dow[k]}<br><span class="sub">${fmt(x)}</span>`),
    onCell: (ev, a, k, cell) => openShiftEditor(ev, {
      agent: a.agent, sub: longDay(days[k]), cell, presets: d.presets, done: () => loadBuilder(),
      save: async (c) => !!(await sbPost("/api/managers/schedule/shift", { agent: a.agent, day: days[k], cell: c })),
      weekdays: async (c) => {
        for (let j = 0; j < 5; j++) if (!(await sbPost("/api/managers/schedule/shift", { agent: a.agent, day: days[j], cell: c }))) return false;
        return true;
      },
    }),
    onDrop: async (from, a, k, copy) => {
      if (await sbPost("/api/managers/schedule/move", { agent: from.agent, day: days[from.k], to_agent: a.agent, to_day: days[k], copy })) loadBuilder();
    },
  });
  $("#sb-grid").replaceChildren(grid);
  noteInto("#sb-grid", `Weeks run Monday–Sunday. Click a cell to set or clear a shift (presets, or type it like the sheet: 9-5, 9-4:30, ` +
    `8-11, 1:30-7). Drag a shift onto another cell to move it; hold Alt to copy. “Publish” makes this week what agents see on their ` +
    `Schedule tab and what the time clock uses for auto clock-out; an unpublished week leaves the sheet's block in place.`, true);
  // coverage
  $("#sb-cover").replaceChildren(coverTable(d.coverage, d.targets));
  renderTargets(d);
}

/* one week grid (agents × Mon–Sun), shared by the schedule builder and the
   Templates editor. o: { id, rows, heads[7] (html), editable, sort {key, asc},
   rerender(), onCell(ev, row, k, cell), onDrop(from {agent, k}, row, k, copy),
   nameExtra(row, td) } */
function shiftGrid(o) {
  const wrap = document.createElement("div"); wrap.className = "table-wrap";
  const table = document.createElement("table"); table.className = "sb-table" + (o.editable ? "" : " locked");
  // sortable: Agent (name), a day (earliest start first, shorter shift first on a tie, unscheduled last), Hours (most first)
  const sk = o.sort;
  const sortTitle = (k) => (k === "agent" ? "Sort by name" : k === "hours" ? "Sort by total hours" : "Sort by this day's start time");
  const headCell = (key, label) => `<th data-sort="${key}" class="${sk.key === key ? "sorted" : ""}" title="${sortTitle(key)}">${label}</th>`;
  table.innerHTML = `<thead><tr>${headCell("agent", "Agent")}${o.heads.map((h, k) => headCell(String(k), h)).join("")}${headCell("hours", "Hrs")}</tr></thead>`;
  table.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (sk.key === key) sk.asc = !sk.asc; else { sk.key = key; sk.asc = key !== "hours"; }
    o.rerender();
  }));
  const rows = o.rows.slice().sort((x, y) => {
    let c = 0;
    if (sk.key === "agent") c = x.agent.localeCompare(y.agent);
    else if (sk.key === "hours") c = x.hours - y.hours;
    else {
      const k = Number(sk.key);
      const xs = x.starts[k], ys = y.starts[k];
      if (xs == null && ys == null) c = x.agent.localeCompare(y.agent);
      else if (xs == null) return 1; else if (ys == null) return -1;
      else c = (xs - ys) || (x.lengths[k] - y.lengths[k]) || x.agent.localeCompare(y.agent);
    }
    return sk.asc ? c : -c;
  });
  const tb = document.createElement("tbody");
  for (const a of rows) {
    const tr = document.createElement("tr");
    const name = document.createElement("td"); name.textContent = a.agent; name.title = a.agent;
    if (o.nameExtra) o.nameExtra(a, name);
    tr.appendChild(name);
    a.cells.forEach((cell, k) => {
      const td = document.createElement("td");
      const el = document.createElement("span");
      const offKind = a.off && a.off[k];
      el.className = "sb-cell " + (offKind ? "pto" : cell ? "on" : "off");
      el.textContent = offKind || cell || "–";
      if (offKind) { el.title = `Approved ${offKind === "PTO" ? "PTO" : "unpaid time off"} — can't be scheduled`; td.appendChild(el); tr.appendChild(td); return; }
      if (o.editable) {
        el.addEventListener("click", (ev) => o.onCell(ev, a, k, cell));
        if (cell) {
          el.draggable = true;
          el.addEventListener("dragstart", (ev) => { ev.dataTransfer.setData("text/plain", JSON.stringify({ g: o.id, agent: a.agent, k })); });
        }
        td.addEventListener("dragover", (ev) => { ev.preventDefault(); el.classList.add("drop"); });
        td.addEventListener("dragleave", () => el.classList.remove("drop"));
        td.addEventListener("drop", async (ev) => {
          ev.preventDefault(); el.classList.remove("drop");
          let from = null; try { from = JSON.parse(ev.dataTransfer.getData("text/plain")); } catch (e) { return; }
          if (!from || from.g !== o.id || (from.agent === a.agent && from.k === k)) return;
          o.onDrop(from, a, k, ev.altKey);
        });
      }
      td.appendChild(el); tr.appendChild(td);
    });
    const hrs = document.createElement("td"); hrs.className = "hrs"; hrs.textContent = a.hours ? a.hours + "h" : ""; tr.appendChild(hrs);
    tb.appendChild(tr);
  }
  table.appendChild(tb); wrap.appendChild(table);
  return wrap;
}

// people on per hour per day, colored against the yellow/green targets
function coverTable(coverage, targets) {
  const tg = targets || {};
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const cls = (hour, c) => { const t = tg[String(hour)] || { yellow: 8, green: 12 }; return c === 0 ? "c0" : c < t.yellow ? "c1" : c < t.green ? "c2" : "c3"; };
  const ct = document.createElement("table"); ct.className = "cover-table";
  ct.innerHTML = `<thead><tr><th></th>${dow.map((x) => `<th>${x}</th>`).join("")}</tr></thead>` +
    `<tbody>${coverage.map((r) => `<tr><td>${r.label}</td>${r.counts.map((c) => `<td class="${cls(r.hour, c)}">${c ? c : ""}</td>`).join("")}</tr>`).join("")}</tbody>`;
  return ct;
}

// coverage targets: per hour, the count that turns a cell yellow and the count that turns it green
function renderTargets(d) {
  const box = $("#sb-targets");
  const tg = d.targets || {};
  const t = document.createElement("table"); t.className = "sb-targets";
  t.innerHTML = `<thead><tr><th>Hour</th><th>Yellow at</th><th>Green at</th></tr></thead>`;
  const tb = document.createElement("tbody");
  const inputs = {};
  for (const r of d.coverage) {
    const v = tg[String(r.hour)] || { yellow: 8, green: 12 };
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.label}</td><td><input type="number" step="0.5" min="0" value="${v.yellow}"></td><td><input type="number" step="0.5" min="0" value="${v.green}"></td>`;
    const [y, g] = tr.querySelectorAll("input");
    inputs[r.hour] = { y, g };
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  const acts = document.createElement("div"); acts.className = "sb-targets-actions";
  acts.innerHTML = `<span class="sub">Set every hour at once:</span> yellow <input type="number" step="0.5" min="0" id="tg-ally" style="width:56px"> green <input type="number" step="0.5" min="0" id="tg-allg" style="width:56px"> <button class="year-btn" id="tg-apply">Apply to all</button> <button class="year-btn active" id="tg-save">Save targets</button>`;
  acts.querySelector("#tg-apply").addEventListener("click", () => {
    const y = acts.querySelector("#tg-ally").value, g = acts.querySelector("#tg-allg").value;
    for (const k in inputs) { if (y !== "") inputs[k].y.value = y; if (g !== "") inputs[k].g.value = g; }
  });
  acts.querySelector("#tg-save").addEventListener("click", async () => {
    const targets = {}; for (const k in inputs) targets[k] = { yellow: inputs[k].y.value, green: inputs[k].g.value };
    if (await sbPost("/api/managers/schedule/targets", { targets })) { toast("Targets saved.", "ok"); loadBuilder(); }
  });
  box.replaceChildren(t, acts);
}

/* the little shift picker that opens under a grid cell.
   o: { agent, sub (day label), cell, presets, save(cell) -> bool, weekdays(cell) -> bool, done() } */
function openShiftEditor(ev, o) {
  document.querySelectorAll(".sb-editor").forEach((x) => x.remove());
  const ed = document.createElement("div"); ed.className = "sb-editor";
  const pre = document.createElement("div"); pre.className = "presets";
  const input = document.createElement("input"); input.type = "text"; input.value = o.cell || ""; input.placeholder = "e.g. 9-5 or 8-11, 1:30-7";
  for (const p of o.presets) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = p; if (p === o.cell) b.className = "cur";
    b.addEventListener("click", () => { input.value = p; save(); });
    pre.appendChild(b);
  }
  const head = document.createElement("div");
  const hb = document.createElement("b"); hb.textContent = o.agent;
  const hs = document.createElement("span"); hs.className = "sub"; hs.textContent = " " + o.sub;
  head.append(hb, hs);
  const row = document.createElement("div"); row.className = "row";
  const clear = document.createElement("button"); clear.type = "button"; clear.textContent = "Off";
  clear.addEventListener("click", () => { input.value = ""; save(); });
  const all = document.createElement("button"); all.type = "button"; all.textContent = "Mon–Fri";
  all.title = "Set this shift on every weekday for this agent";
  all.addEventListener("click", async () => { await o.weekdays(input.value); ed.remove(); o.done(); });
  const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "Cancel"; cancel.addEventListener("click", () => ed.remove());
  const ok = document.createElement("button"); ok.type = "button"; ok.className = "primary"; ok.textContent = "Save"; ok.addEventListener("click", save);
  row.append(clear, all, cancel, ok);
  const hint = document.createElement("div"); hint.className = "hint"; hint.textContent = "Times read like the sheet: 1-6 means 1 PM to 6 PM; 9-5 means 9 AM to 5 PM.";
  ed.append(head, pre, input, row, hint);
  document.body.appendChild(ed);
  const r = ev.currentTarget.getBoundingClientRect();
  ed.style.left = Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - 320) + "px";
  ed.style.top = (r.bottom + window.scrollY + 4) + "px";
  input.focus();
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") ed.remove(); });
  setTimeout(() => document.addEventListener("click", function off(e) { if (!ed.contains(e.target)) { ed.remove(); document.removeEventListener("click", off); } }), 0);
  async function save() {
    if (await o.save(input.value)) { ed.remove(); o.done(); }
  }
}

/* ---------- on the clock now (Schedule tab, under Coverage) ----------
   Who is clocked in, who should be and isn't, and who is clocked in outside
   their shift — refreshed every minute while the Schedule tab is open. */
const LV = { timer: null };
async function loadLive() {
  const box = $("#sb-live");
  if (!box) return;
  let d = null;
  try { const r = await fetch("/api/managers/live"); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  if (!d) { box.replaceChildren(noData("Couldn't load.")); return; }
  $("#lv-asof").textContent = "as of " + d.now_label + " MT";
  const wrap = document.createElement("div"); wrap.className = "lv-box";
  const group = (cls, title, items, empty, detail) => {
    const g = document.createElement("div"); g.className = "lv-group " + cls;
    const h = document.createElement("div"); h.className = "lv-h";
    h.innerHTML = `<span>${title}</span><b>${items.length}</b>`;
    g.appendChild(h);
    if (!items.length) {
      if (!empty) return null;
      const e = document.createElement("div"); e.className = "lv-empty"; e.textContent = empty; g.appendChild(e);
      return g;
    }
    for (const x of items) {
      const line = document.createElement("div"); line.className = "lv-row";
      const n = document.createElement("span"); n.className = "lv-name"; n.textContent = x.agent; n.title = x.agent;
      const s = document.createElement("span"); s.className = "lv-sub"; s.textContent = detail(x);
      line.append(n, s); g.appendChild(line);
    }
    return g;
  };
  const late = (x) => `since ${x.since_label} · ${x.late_min} min`;
  const parts = [
    group("lv-miss", "Should be on, not clocked in", d.missing, d.posted ? "Everyone scheduled right now is in." : "No schedule posted for today.", late),
    group("lv-on", "Clocked in", d.on, "Nobody is clocked in.", (x) => `in ${x.in_label}`),
    group("lv-extra", "Clocked in, not scheduled now", d.extra, null, (x) => `in ${x.in_label} · ${x.shift ? "shift " + x.shift : "not on today"}`),
  ].filter(Boolean);
  wrap.append(...parts);
  box.replaceChildren(wrap);
  noteInto("#sb-live", `Live from the time clock, checked every minute while this tab is open. Times are Mountain, the same clock the schedule cells use. “Scheduled” is today's posted shift ` +
    `(the published week here, or the sheet's block when the week isn't published), with approved time off taken out. Clocking in up to ` +
    `${d.early_ok_min} minutes early, or staying up to ${d.grace_min} minutes after the shift ends, still counts as on shift.`, true);
}
function startLiveTimer() {
  if (LV.timer) return;
  LV.timer = setInterval(() => {
    const p = $("#gsub-schedule");
    if (p && p.classList.contains("active") && p.offsetParent !== null && !document.hidden) loadLive();
  }, 60000);
}

/* ---------- Saturdays tracker (Schedule tab, under the grid) ----------
   Who worked which Saturday, so managers can pull whoever has gone longest.
   Counts come from published builder weeks (and the old sheet's history);
   a click on a past Saturday corrects it. Nothing is auto-assigned. */
// sort: null = the rotation order from the server; "agent" | "last" | "weeks" | "recent" | "c<column index>"
const SATS = { data: null, sort: null, asc: true };
async function loadSaturdays() {
  const box = $("#sb-sats");
  if (!box) return;
  let d = null;
  try { const r = await fetch("/api/managers/saturdays"); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  if (!d) { box.replaceChildren(noData("Couldn't load.")); return; }
  SATS.data = d;
  renderSaturdays();
}
function renderSaturdays() {
  const box = $("#sb-sats");
  const d = SATS.data;
  const md = (iso) => { const x = new Date(iso + "T12:00:00"); return `${x.getMonth() + 1}/${x.getDate()}`; };
  const longD = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  const wrap = document.createElement("div"); wrap.className = "table-wrap";
  const table = document.createElement("table"); table.className = "sat-table";
  // click a header to sort (again to flip); a Saturday's column puts whoever worked it on top
  const sk = SATS.sort;
  const th = (key, label, title, cls = "") =>
    `<th data-sort="${key}" class="${cls}${sk === key ? " sorted" + (SATS.asc ? " asc" : "") : ""}" title="${title}. Click to sort.">${label}</th>`;
  const thCols = d.columns.map((c, i) => th("c" + i, md(c), i >= d.future_from ? `Upcoming ${longD(c)} — from a published week` : longD(c), i >= d.future_from ? "fut" : "")).join("");
  table.innerHTML = `<thead><tr>${th("agent", "Agent", "Name")}${th("last", "Last", "The most recent Saturday worked")}${th("weeks", "Weeks", "Saturdays since then")}` +
    `${th("recent", `Last ${d.window_weeks}`, `Saturdays worked in the last ${d.window_weeks}`)}${thCols}<th title="Works every Saturday — kept out of the rotation order">Every Sat</th></tr></thead>`;
  table.querySelectorAll("th[data-sort]").forEach((h) => h.addEventListener("click", () => {
    const key = h.dataset.sort;
    if (SATS.sort === key) SATS.asc = !SATS.asc; else { SATS.sort = key; SATS.asc = true; }
    renderSaturdays();
  }));
  const tb = document.createElement("tbody");
  const srcLabel = { schedule: "published schedule", sheet: "old Saturdays sheet", manual: "manager correction" };
  const addRow = (r) => {
    const tr = document.createElement("tr"); if (r.regular) tr.className = "reg";
    const nm = document.createElement("td"); nm.className = "nm"; nm.textContent = r.agent; nm.title = r.agent; tr.appendChild(nm);
    const last = document.createElement("td");
    last.textContent = r.last ? md(r.last) + (r.last.slice(0, 4) !== d.today.slice(0, 4) ? "/" + r.last.slice(2, 4) : "") : "never";
    if (!r.last) last.className = "never"; tr.appendChild(last);
    const ws = document.createElement("td"); ws.textContent = r.weeks_since == null ? "–" : r.weeks_since; tr.appendChild(ws);
    const rc = document.createElement("td"); rc.textContent = r.recent || ""; tr.appendChild(rc);
    r.cells.forEach((c, i) => {
      const td = document.createElement("td");
      const fut = i >= d.future_from;
      const worked = !!(c && c.worked);
      const s = document.createElement("span");
      s.className = "sat-c" + (worked ? " w " + c.source : c ? " x" : "") + (fut ? " fut" : "");
      s.textContent = worked ? "✓" : c ? "✕" : "";
      const what = worked ? `Worked${c.cell ? " " + c.cell : ""} (${srcLabel[c.source] || c.source})` : c ? "Marked as not worked (manager correction)" : "Not worked";
      td.title = `${r.agent} · ${longD(d.columns[i])}\n${fut ? "Scheduled" + (c && c.cell ? " " + c.cell : "") + " (published week)" : what}`;
      if (!fut) {
        td.className = "click";
        td.addEventListener("click", async () => {
          const next = !worked;
          if (!confirm(`${next ? "Mark" : "Unmark"} ${r.agent} as having worked ${longD(d.columns[i])}?`)) return;
          const base = !!(c && (c.source === "schedule" || c.scheduled));
          const send = next === base && !(c && c.source === "sheet") ? null : next;
          const res = await sbPost("/api/managers/saturdays", { action: "mark", agent: r.agent, day: d.columns[i], worked: send });
          if (res) loadSaturdays();
        });
      }
      td.appendChild(s); tr.appendChild(td);
    });
    const rg = document.createElement("td");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = r.regular;
    cb.addEventListener("change", async () => {
      if (await sbPost("/api/managers/saturdays", { action: "regular", agent: r.agent, on: cb.checked })) loadSaturdays(); else cb.checked = !cb.checked;
    });
    rg.appendChild(cb); tr.appendChild(rg);
    tb.appendChild(tr);
  };
  // first click: name A–Z, oldest "Last" first (never on top), most weeks since first, fewest in the window first,
  // or whoever worked that Saturday first. Ties keep the rotation order.
  const pos = new Map(d.rows.map((r, i) => [r.agent, i]));
  const by = {
    agent: (a, b) => a.agent.localeCompare(b.agent),
    last: (a, b) => (a.last || "").localeCompare(b.last || ""),
    weeks: (a, b) => (b.weeks_since ?? 1e9) - (a.weeks_since ?? 1e9),
    recent: (a, b) => a.recent - b.recent,
  };
  const cmp = (a, b) => {
    let c = 0;
    if (sk && sk[0] === "c") { const i = Number(sk.slice(1)); const w = (r) => (r.cells[i] && r.cells[i].worked ? 0 : 1); c = w(a) - w(b); }
    else if (sk && by[sk]) c = by[sk](a, b);
    if (!SATS.asc) c = -c;
    return c || pos.get(a.agent) - pos.get(b.agent);
  };
  const rot = d.rows.filter((r) => !r.regular).sort(cmp), regs = d.rows.filter((r) => r.regular).sort(cmp);
  rot.forEach(addRow);
  if (regs.length) {
    const sep = document.createElement("tr"); sep.className = "sep";
    sep.innerHTML = `<td colspan="${5 + d.columns.length}">Every-Saturday staff</td>`;
    tb.appendChild(sep);
    regs.forEach(addRow);
  }
  table.appendChild(tb); wrap.appendChild(table);
  box.replaceChildren(wrap);
  noteInto("#sb-sats", `A Saturday counts as soon as it's in a week published here; upcoming ones show faded on the right. ` +
    `${d.history_from ? `History before the builder comes from the old Saturdays sheet (from ${md(d.history_from)}/${d.history_from.slice(2, 4)}). ` : ""}` +
    `Agents with an upcoming published Saturday sit below those without one. Click a column heading to sort by it (click again to flip); a Saturday's heading puts everyone who worked it on top. Click a past Saturday's box to correct it. ` +
    `Tick “Every Sat” for staff who work every Saturday; they're listed separately and left out of the rotation order. Nobody is added to a schedule automatically.`, true);
}

/* ---------- Templates (IS Managers -> Templates) ----------
   Reusable weeks with no dates. Create one (blank, from a builder week, or a
   copy of another), rename it, edit its shifts on the same grid as the
   builder; the builder's "Template: …" buttons start a week from it. */
const TP = { open: null, list: [], sort: { key: "agent", asc: true }, prefillWeek: null };
const tpPost = (body) => sbPost("/api/managers/templates", body);
async function loadTemplates(keepForm) {
  let d = null;
  try { const r = await fetch("/api/managers/templates"); d = r.ok ? await r.json() : null; } catch (e) { d = null; }
  if (!d || !d.templates) { $("#tp-list").replaceChildren(noData("Couldn't load.")); return; }
  TP.list = d.templates;
  if (!keepForm || TP.prefillWeek) renderTpNew();
  renderTpList();
  if (TP.open && !TP.list.some((t) => t.id === TP.open)) TP.open = null;
  if (TP.open) loadTemplate(TP.open); else $("#tp-editor").replaceChildren();
}
function renderTpNew() {
  const box = $("#tp-new");
  const f = document.createElement("div"); f.className = "tp-new";
  f.innerHTML = `<b>New template</b><input type="text" id="tpn-name" placeholder="name, e.g. Summer week" style="width:200px">` +
    `<select id="tpn-src"><option value="blank">Start blank</option><option value="week">Copy a week from the builder</option>` +
    (TP.list.length ? `<option value="copy">Copy another template</option>` : "") + `</select>` +
    `<input type="date" id="tpn-week" title="Any day in the week to copy" style="display:none">` +
    `<select id="tpn-from" style="display:none">${TP.list.map((t) => `<option value="${t.id}"></option>`).join("")}</select>` +
    `<button class="year-btn active" id="tpn-go">Create</button>`;
  f.querySelectorAll("#tpn-from option").forEach((o, i) => { o.textContent = TP.list[i].name; });
  const src = f.querySelector("#tpn-src"), wk = f.querySelector("#tpn-week"), from = f.querySelector("#tpn-from"), name = f.querySelector("#tpn-name");
  const sync = () => { wk.style.display = src.value === "week" ? "" : "none"; from.style.display = src.value === "copy" ? "" : "none"; };
  src.addEventListener("change", sync);
  wk.value = TP.prefillWeek || SB.monday || sbMonday(0);
  if (TP.prefillWeek) { src.value = "week"; TP.prefillWeek = null; setTimeout(() => name.focus(), 0); }
  sync();
  const go = async () => {
    const body = { action: "create", name: name.value, source: src.value };
    if (src.value === "week") body.monday = wk.value;
    if (src.value === "copy") body.from_id = Number(from.value);
    const r = await tpPost(body);
    if (r) { toast("Template created.", "ok"); TP.open = r.id; loadTemplates(); }
  };
  f.querySelector("#tpn-go").addEventListener("click", go);
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  box.replaceChildren(f);
}
function renderTpList() {
  const box = $("#tp-list");
  if (!TP.list.length) { box.replaceChildren(noData("No templates yet — make one above.")); return; }
  const when = (iso) => (iso ? new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "");
  const t = document.createElement("table"); t.className = "tp-table";
  t.innerHTML = `<thead><tr><th>Template</th><th>Agents</th><th>Shifts</th><th>Hours / week</th><th>Last edited</th><th></th></tr></thead>`;
  const tb = document.createElement("tbody");
  const b = (label, fn, cls) => { const x = document.createElement("button"); x.className = "year-btn" + (cls ? " " + cls : ""); x.textContent = label; x.addEventListener("click", (e) => { e.stopPropagation(); fn(); }); return x; };
  for (const x of TP.list) {
    const tr = document.createElement("tr"); if (x.id === TP.open) tr.className = "open";
    const cells = [x.name, x.agents, x.shifts, x.hours, `${when(x.updated_at)}${x.updated_by ? " · " + x.updated_by : ""}`];
    for (const v of cells) { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); }
    tr.firstChild.className = "nm";
    const acts = document.createElement("td"); acts.className = "acts";
    acts.append(
      b(x.id === TP.open ? "Editing" : "Edit", () => { TP.open = x.id; renderTpList(); loadTemplate(x.id); }, x.id === TP.open ? "active" : ""),
      b("Rename", () => renameTemplate(x)),
      b("Duplicate", async () => {
        const name = (prompt("Name for the copy:", `${x.name} (copy)`) || "").trim(); if (!name) return;
        const r = await tpPost({ action: "create", name, source: "copy", from_id: x.id });
        if (r) { toast("Copied.", "ok"); TP.open = r.id; loadTemplates(true); }
      }),
      b("Delete", async () => {
        if (!confirm(`Delete template “${x.name}”? Weeks already built from it are not affected.`)) return;
        if (await tpPost({ action: "delete", id: x.id })) { if (TP.open === x.id) TP.open = null; loadTemplates(true); }
      }));
    tr.appendChild(acts);
    tr.addEventListener("click", () => { TP.open = x.id; renderTpList(); loadTemplate(x.id); });
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  box.replaceChildren(t);
}
async function renameTemplate(x) {
  const name = (prompt("Rename template:", x.name) || "").trim();
  if (!name || name === x.name) return;
  if (await tpPost({ action: "rename", id: x.id, name })) { toast("Renamed.", "ok"); loadTemplates(true); }
}
async function loadTemplate(id) {
  let t = null;
  try { const r = await fetch(`/api/managers/templates/one?id=${id}`); t = r.ok ? await r.json() : null; } catch (e) { t = null; }
  if (!t || t.error) { $("#tp-editor").replaceChildren(noData("Couldn't load that template.")); return; }
  renderTemplate(t);
}
function renderTemplate(t) {
  const box = $("#tp-editor");
  const head = document.createElement("div"); head.className = "tp-head";
  const h = document.createElement("h3"); h.className = "section-title"; h.textContent = `Editing “${t.name}”`;
  const close = document.createElement("button"); close.className = "year-btn"; close.textContent = "Close";
  close.addEventListener("click", () => { TP.open = null; renderTpList(); box.replaceChildren(); });
  const ren = document.createElement("button"); ren.className = "year-btn"; ren.textContent = "Rename";
  ren.addEventListener("click", () => renameTemplate(t));
  head.append(h, ren, close);
  const refresh = () => { loadTemplates(true); };
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const dowLong = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const grid = shiftGrid({
    id: "tp", rows: t.agents, editable: true, sort: TP.sort, rerender: () => renderTemplate(t), heads: dow,
    onCell: (ev, a, k, cell) => openShiftEditor(ev, {
      agent: a.agent, sub: dowLong[k], cell, presets: t.presets, done: refresh,
      save: async (c) => !!(await tpPost({ action: "set", id: t.id, agent: a.agent, dows: [k], cell: c })),
      weekdays: async (c) => !!(await tpPost({ action: "set", id: t.id, agent: a.agent, dows: [0, 1, 2, 3, 4], cell: c })),
    }),
    onDrop: async (from, a, k, copy) => {
      if (await tpPost({ action: "move", id: t.id, agent: from.agent, dow: from.k, to_agent: a.agent, to_dow: k, copy })) refresh();
    },
    nameExtra: (a, td) => {
      if (a.on_roster) return;
      td.classList.add("sb-gone"); td.title = `${a.agent} — not on the roster any more`;
      const x = document.createElement("button"); x.className = "sb-x"; x.textContent = "×"; x.title = "Take them out of this template";
      x.addEventListener("click", async () => {
        if (!confirm(`Take ${a.agent} out of “${t.name}”?`)) return;
        if (await tpPost({ action: "clear_agent", id: t.id, agent: a.agent })) refresh();
      });
      td.prepend(x);
    },
  });
  const layout = document.createElement("div"); layout.className = "sb-layout";
  const g = document.createElement("div"); g.id = "tp-grid"; g.appendChild(grid);
  const side = document.createElement("div"); side.className = "tp-side";
  const sh = document.createElement("h3"); sh.className = "section-title"; sh.innerHTML = `Coverage <span class="sub">people on, per hour</span>`;
  side.append(sh, coverTable(t.coverage, t.targets));
  layout.append(g, side);
  box.replaceChildren(head, layout);
  h.appendChild(infoIcon("Edit it like the schedule builder: click a cell to set or clear a shift, drag to move (hold Alt to copy). " +
    "Changes save as you go. Using a template (the “Template: …” buttons on the Schedule tab when a week isn't created yet) copies it " +
    "into that week; later edits to the template don't change weeks already built. Approved time off still wins when a week is built."));
}
/* ---------- clock-in rules (Timesheets tab) ----------
   Two switches a manager can turn on: hold the clock-in button until N
   minutes before the shift, and hold it on days with no shift. Either can
   be lifted for one agent for one day (an exception). */
async function loadClockRules() {
  const box = $("#ts-rules");
  if (!box) return;
  let d = null;
  try { d = await (await fetch("/api/managers/clock_rules")).json(); } catch (e) { d = null; }
  if (!d || !d.rules) { box.replaceChildren(noData("Couldn't load the rules.")); return; }
  const post = async (path, body) => {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.error || "That didn't work.", "warn"); return null; }
    return j;
  };
  const wrap = document.createElement("div"); wrap.className = "ts-rules";
  const rules = document.createElement("div"); rules.className = "ts-rule-row";
  rules.innerHTML = `<label><input type="checkbox" id="cr-early" ${d.rules.early_min > 0 ? "checked" : ""}> Hold the clock-in button until</label>` +
    `<input type="number" id="cr-min" min="1" max="240" step="1" value="${d.rules.early_min || 15}" style="width:64px"> <span>minutes before the shift starts</span>` +
    `<label style="margin-left:18px"><input type="checkbox" id="cr-unsched" ${d.rules.block_unscheduled ? "checked" : ""}> Hold it on days with no shift posted</label>` +
    `<button class="year-btn active" id="cr-save">Save rules</button>` +
    `<span class="sub" id="cr-state">${d.rules.early_min > 0 || d.rules.block_unscheduled ? "" : "Both rules are off: agents can clock in any time."}</span>`;
  rules.querySelector("#cr-save").addEventListener("click", async () => {
    const early = rules.querySelector("#cr-early").checked ? rules.querySelector("#cr-min").value : 0;
    if (await post("/api/managers/clock_rules", { early_min: early, block_unscheduled: rules.querySelector("#cr-unsched").checked })) { toast("Rules saved.", "ok"); loadClockRules(); }
  });
  // auto clock-out: on/off, minutes after the shift, when the clock-out is recorded, the no-shift limit
  const au = d.auto || { enabled: true, grace_min: 10, stamp: "shift_end", no_shift_h: 14 };
  const auto = document.createElement("div"); auto.className = "ts-rule-row";
  auto.innerHTML = `<label><input type="checkbox" id="ao-on" ${au.enabled ? "checked" : ""}> Auto clock-out</label>` +
    `<input type="number" id="ao-min" min="0" max="600" step="1" value="${au.grace_min}" style="width:64px"> <span>minutes after the shift ends; record the clock-out at</span>` +
    `<select id="ao-stamp"><option value="shift_end">the shift end</option><option value="run">that time</option></select>` +
    `<span style="margin-left:12px">With no shift posted, close it after</span><input type="number" id="ao-cap" min="0" max="24" step="0.5" value="${au.no_shift_h}" style="width:64px"> <span>hours (0 = never)</span>` +
    `<button class="year-btn active" id="ao-save">Save</button>` +
    `<span class="sub">${au.enabled ? "" : "Off: punches stay open until the agent or a manager clocks them out."}${au.updated_by ? ` Last changed by ${au.updated_by}.` : ""}</span>`;
  auto.querySelector("#ao-stamp").value = au.stamp;
  auto.querySelector("#ao-save").addEventListener("click", async () => {
    const body = { enabled: auto.querySelector("#ao-on").checked, grace_min: auto.querySelector("#ao-min").value,
      stamp: auto.querySelector("#ao-stamp").value, no_shift_h: auto.querySelector("#ao-cap").value };
    if (await post("/api/managers/auto_clockout", body)) { toast("Auto clock-out saved.", "ok"); loadClockRules(); }
  });
  const exc = document.createElement("div"); exc.className = "ts-rule-row";
  const today = new Date(); today.setHours(12); const iso = today.toISOString().slice(0, 10);
  exc.innerHTML = `<span><b>Allow an exception</b></span><select id="co-agent">${d.agents.map((a) => `<option>${a}</option>`).join("")}</select>` +
    `<input type="date" id="co-day" value="${iso}"><input type="text" id="co-note" placeholder="note (optional)" style="width:180px"><button class="year-btn" id="co-add">Allow</button>`;
  exc.querySelector("#co-add").addEventListener("click", async () => {
    if (await post("/api/managers/clock_override", { agent: exc.querySelector("#co-agent").value, day: exc.querySelector("#co-day").value, note: exc.querySelector("#co-note").value })) { toast("Exception added.", "ok"); loadClockRules(); }
  });
  const list = document.createElement("div"); list.className = "to-list";
  if (!d.overrides.length) list.innerHTML = `<span class="sub">No exceptions from today on.</span>`;
  for (const o of d.overrides) {
    const row = document.createElement("div"); row.className = "to-row";
    row.innerHTML = `<b>${o.agent}</b><span>${new Date(o.day + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}</span>` +
      `<span class="sub">${o.note || ""}</span><span class="sub">by ${o.created_by || ""}</span>`;
    const rm = document.createElement("button"); rm.className = "no"; rm.textContent = "Remove";
    rm.addEventListener("click", async () => { if (await post("/api/managers/clock_override", { agent: o.agent, day: o.day, remove: true })) loadClockRules(); });
    row.appendChild(rm); list.appendChild(row);
  }
  wrap.append(rules, auto, exc, list);
  box.replaceChildren(wrap);
  noteInto("#ts-rules", "With the first rule on, an agent's Clock in button stays off until that many minutes before their posted shift (the card " +
    "says when it opens). With the second, it stays off on a day they have no shift — but never when the week isn't posted at all. An " +
    "exception lets one agent clock in on one day regardless of both rules. Clocking out is never held back. " +
    "Auto clock-out closes a punch an agent forgot to end: that many minutes after their posted shift ends, recorded either at the shift end " +
    "(they're paid to the end of the shift) or at the moment it runs. A punch on a day with no posted shift is closed after the hour limit " +
    "(0 = never). Turned off, open punches stay open until someone clocks them out. Changes apply to punches still open.", true);
}

/* ---------- adherence (IS Managers -> Adherence) ---------- */
const ADH = { data: null, range: null, open: null };
async function loadAdherence(a, b) {
  ADH.range = [a, b];
  $("#adh-table").replaceChildren(noData("Loading…")); $("#adh-detail").replaceChildren();
  let d = null;
  try { d = await (await fetch(`/api/managers/adherence?start_day=${a}&end_day=${b}`)).json(); } catch (e) { d = null; }
  if (!d || d.error) { $("#adh-table").replaceChildren(noData((d && d.error) || "No data.")); return; }
  ADH.data = d;
  renderAdherence(d);
}
function renderAdherence(d) {
  const h = (v) => (v == null ? "–" : v.toFixed(2));
  const pct = (v) => (v == null ? "–" : v.toFixed(1) + "%");
  const cols = [["agent", "Agent", ""], ["adherence_pct", "Adherence", "hours clocked inside the posted shift ÷ scheduled hours"],
    ["on_time_pct", "On time", "scheduled days with neither a late arrival nor a no-show"],
    ["sched_days", "Sched. days", "scheduled days up to today"], ["sched_h", "Sched. hours", ""], ["in_window_h", "In shift", "hours clocked inside the shift window"],
    ["late", "Late", `clock-ins more than ${d.late_min} min after the start`], ["avg_late_min", "Avg late (min)", ""],
    ["early_out", "Early outs", `clock-outs by hand more than ${d.early_out_min} min before the end`], ["no_show", "No-shows", "scheduled, never clocked in"],
    ["unscheduled_days", "Unscheduled", "days clocked with no shift posted"]];
  const rows = d.agents.map((a) => ({ ...a, _a: a }));
  const t = simpleTable(cols, rows, { fmt: { adherence_pct: pct, on_time_pct: pct, sched_h: h, in_window_h: h, avg_late_min: (v) => (v ? v.toFixed(0) : "–") },
    sortable: true, defaultSort: "agent", hl: new Set(["adherence_pct"]), onName: (r) => showAdhDetail(r._a),
    flag: (r, k) => (k === "adherence_pct" && r.adherence_pct != null ? (r.adherence_pct < 85 ? "sc-red" : r.adherence_pct < 95 ? "sc-yellow" : "sc-green") :
      k === "on_time_pct" && r.on_time_pct != null ? (r.on_time_pct < 80 ? "sc-red" : r.on_time_pct < 95 ? "sc-yellow" : "sc-green") : "") });
  $("#adh-table").replaceChildren(t);
  noteInto("#adh-table", `${d.start} – ${d.through}. Built from the same punches and posted shifts as the timesheet; only scheduled days up to today ` +
    `count, and a shift still ahead today isn't judged yet. Adherence = hours clocked inside the shift window ÷ scheduled hours (red under 85%, ` +
    `yellow under 95%). On time = scheduled days with no late arrival (${d.late_min}+ min) and no no-show. Early outs only count clock-outs by hand, ` +
    `never automatic ones. Approved time off isn't a scheduled day. Click an agent for the list of days behind the numbers.`, true);
  if (ADH.open) { const a = d.agents.find((x) => x.agent === ADH.open); if (a) showAdhDetail(a); }
}
function showAdhDetail(a) {
  ADH.open = a.agent;
  const box = $("#adh-detail");
  const head = document.createElement("div"); head.className = "ts-detail-head";
  head.innerHTML = `<h3 style="margin:0">${a.agent} <span class="sub">${a.exception_count} exception${a.exception_count === 1 ? "" : "s"} over ${a.sched_days} scheduled day${a.sched_days === 1 ? "" : "s"}</span></h3>`;
  const list = document.createElement("div"); list.className = "adh-list";
  if (!a.exceptions.length) list.innerHTML = `<span class="sub">Every scheduled day was worked on time.</span>`;
  for (const e of a.exceptions) {
    const row = document.createElement("div"); row.className = "to-row";
    row.innerHTML = `<b>${new Date(e.date + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}</b>` +
      `<span class="ts-flag${/no-show/.test(e.kind) ? " bad" : ""}">${e.kind}</span><span>${e.detail}</span>`;
    list.appendChild(row);
  }
  box.replaceChildren(head, list);
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadTimesheet(a, b) {
  TS.range = [a, b];
  $("#ts-table").replaceChildren(noData("Loading…")); $("#ts-detail").replaceChildren();
  $("#ts-csv").href = `/api/managers/timesheet.csv?start_day=${a}&end_day=${b}`;
  let d = null;
  try { d = await (await fetch(`/api/managers/timesheet?start_day=${a}&end_day=${b}`)).json(); } catch (e) { d = null; }
  if (!d || d.error) { $("#ts-table").replaceChildren(noData((d && d.error) || "No data.")); return; }
  TS.data = d;
  renderTimesheet(d);
}
function renderTimesheet(d) {
  const h = (v) => (v == null ? "–" : v.toFixed(2));
  const cols = [["agent", "Agent", ""], ["scheduled_h", "Scheduled", "hours on the posted schedule"],
    ["worked_h", "Clocked", "hours between clock in and out"], ["diff_h", "Difference", "clocked − scheduled"],
    ["daily_ot_h", "Daily OT", "hours over 8 in a day (only hours inside the week's first 40)"],
    ["weekly_ot_h", "Weekly OT", "hours past 40 in a Sun–Sat week — never also counted as daily"],
    ["days_over_8", "Days >8h", ""], ["weeks_over_40", "Weeks >40h", ""],
    ["flags", "Flags", "late, no clock-in, auto clock-outs, still clocked in, daily OT"]];
  const rows = d.agents.map((a) => ({ ...a, _a: a }));
  const t = simpleTable(cols, rows, { fmt: { scheduled_h: h, worked_h: h, diff_h: (v) => (v > 0 ? "+" : "") + h(v), daily_ot_h: h, weekly_ot_h: h, flags: (v) => v || "" },
    sortable: true, defaultSort: "agent", hl: new Set(["worked_h"]), onName: (r) => showTsDetail(r._a),
    flag: (r, k) => (k === "diff_h" && r.diff_h != null ? (r.diff_h < -0.5 ? "sc-red" : r.diff_h > 0.5 ? "sc-yellow" : "") : "") });
  $("#ts-table").replaceChildren(t);
  noteInto("#ts-table", `${d.start} – ${d.through}. Scheduled hours come from the posted schedule sheet; clocked hours from the ` +
    `agent's own clock in/out on their page (server time, shown in Mountain). ${!d.auto || d.auto.enabled ? `A punch still open ${d.grace_min} minutes after the scheduled shift end is closed ` +
      `${d.auto && d.auto.stamp === "run" ? "at that moment" : "at the shift end"} and marked “auto” (see Clock rules below).` : `Auto clock-out is off (see Clock rules below).`} Overtime: daily = hours over 8 in a day, weekly = hours past 40 in a ` +
    `Sun–Sat week; once a week passes 40 everything after counts as weekly only, so nothing is counted twice. Click an agent for their days; ` +
    `change any time directly and hit Save, add a missed punch on the empty line, ✕ deletes one. Every change is logged; the note is optional.`, true);
  if (TS.open) { const a = d.agents.find((x) => x.agent === TS.open); if (a) showTsDetail(a); }
}
function showTsDetail(a) {
  TS.open = a.agent;
  const box = $("#ts-detail");
  const t = (iso) => (iso ? clockTime(iso) : "…");
  const head = document.createElement("div"); head.className = "ts-detail-head";
  head.innerHTML = `<h3 style="margin:0">${a.agent} <span class="sub">${a.worked_h.toFixed(2)}h clocked · ${a.scheduled_h.toFixed(2)}h scheduled</span></h3>`;
  const list = document.createElement("div");
  const local = clockLocal;
  const send = async (body) => {
    const r = await fetch("/api/managers/punch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.error || "Couldn't save.", "warn"); return; }
    toast("Saved.", "ok"); if (TS.range) loadTimesheet(...TS.range);
  };
  // each punch is two time boxes you can change directly; a note is optional
  const punchRow = (p, agent, day) => {
    const line = document.createElement("div"); line.className = "ts-punch";
    const inp = document.createElement("input"); inp.type = "datetime-local"; inp.value = p ? local(p.in_at) : `${day}T09:00`;
    const out = document.createElement("input"); out.type = "datetime-local"; out.value = p ? local(p.out_at) : "";
    const note = document.createElement("input"); note.type = "text"; note.placeholder = "note (optional)"; note.style.width = "150px";
    const save = document.createElement("button"); save.textContent = p ? "Save" : "Add";
    save.addEventListener("click", () => send({ id: p ? p.id : null, agent, in_at: inp.value || null, out_at: out.value || null, reason: note.value }));
    line.append(inp, document.createTextNode("–"), out, note, save);
    if (p) {
      const tag = document.createElement("span"); tag.className = "sub";
      tag.textContent = `${p.hours != null ? p.hours + "h" : "open"}${p.out_kind && p.out_kind !== "manual" ? " · " + p.out_kind : ""}`;
      const del = document.createElement("button"); del.textContent = "✕"; del.title = "Delete this punch";
      del.addEventListener("click", () => { if (confirm("Delete this punch?")) send({ id: p.id, delete: true, reason: note.value }); });
      line.append(tag, del);
    }
    return line;
  };
  for (const day of a.days) {
    const row = document.createElement("div"); row.className = "ts-day";
    const flags = day.flags.map((f) => `<span class="ts-flag${/no clock-in|still/.test(f) ? " bad" : ""}">${f}</span>`).join("");
    const punches = document.createElement("div");
    for (const p of day.punches) punches.appendChild(punchRow(p, a.agent, day.date));
    const addLine = punchRow(null, a.agent, day.date);
    addLine.style.opacity = ".7";
    punches.appendChild(addLine);
    row.innerHTML = `<div><b>${new Date(day.date + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}</b>` +
      `<div class="sub">${day.shift || "not scheduled"}</div></div><div>${day.worked_h.toFixed(2)}h / ${day.scheduled_h.toFixed(2)}h<div>${flags}</div></div>`;
    row.appendChild(punches);
    list.appendChild(row);
  }
  box.replaceChildren(head, list);
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
// (the old modal editor, kept for reference; the timesheet now edits inline)
function punchModal({ agent, punch }) {
  const ov = document.createElement("div"); ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const local = clockLocal;
  const card = document.createElement("div"); card.className = "modal-card";
  card.innerHTML = `<div class="modal-head"><b>${punch ? "Edit punch" : "Add a punch"}</b><span class="sub">${agent}</span></div>` +
    `<label class="modal-field">Clock in <input type="datetime-local" id="pm-in" value="${local(punch && punch.in_at)}"></label>` +
    `<label class="modal-field">Clock out <input type="datetime-local" id="pm-out" value="${local(punch && punch.out_at)}"></label>` +
    `<label class="modal-field">Reason <input type="text" id="pm-reason" placeholder="why this is being changed"></label>` +
    `<div style="display:flex;gap:8px;justify-content:flex-end"><button class="year-btn" id="pm-cancel">Cancel</button>` +
    (punch ? `<button class="year-btn" id="pm-del">Delete</button>` : "") + `<button class="year-btn active" id="pm-save">Save</button></div>`;
  ov.appendChild(card); document.body.appendChild(ov);
  card.querySelector("#pm-cancel").addEventListener("click", () => ov.remove());
  const send = async (body) => {
    const r = await fetch("/api/managers/punch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.error || "Couldn't save.", "warn"); return; }
    ov.remove(); toast("Saved.", "ok"); if (TS.range) loadTimesheet(...TS.range);
  };
  card.querySelector("#pm-save").addEventListener("click", () => send({
    id: punch ? punch.id : null, agent, in_at: card.querySelector("#pm-in").value || null,
    out_at: card.querySelector("#pm-out").value || null, reason: card.querySelector("#pm-reason").value }));
  const del = card.querySelector("#pm-del");
  if (del) del.addEventListener("click", () => { if (confirm("Delete this punch?")) send({ id: punch.id, delete: true, reason: card.querySelector("#pm-reason").value }); });
}
async function loadHr() {
  const box = $("#hr-table");
  box.replaceChildren(noData("Loading…"));
  let d = null;
  try { d = await (await fetch("/api/managers/agents")).json(); } catch (e) { d = null; }
  if (!d || !d.agents) { box.replaceChildren(noData("Couldn't load.")); return; }
  let ptoBy = {};
  try { const p = await (await fetch("/api/managers/pto")).json(); for (const x of (p.agents || [])) ptoBy[x.agent] = x; } catch (e) { /* no PTO columns */ }
  const wrap = document.createElement("div"); wrap.className = "table-wrap hr-table";
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>Agent</th><th>Start date</th><th>Employment</th><th>Hours / week</th><th>State</th><th>Notes</th><th></th>` +
    `<th>PTO balance</th><th>PTO status</th><th></th></tr></thead>`;
  const tb = document.createElement("tbody");
  for (const a of d.agents) {
    const tr = document.createElement("tr");
    const p = ptoBy[a.agent];
    tr.innerHTML = `<td>${a.agent}</td><td><input class="d" type="date" value="${a.start_date || ""}"></td>` +
      `<td><select><option value="">–</option><option value="full_time"${a.employment_type === "full_time" ? " selected" : ""}>Full time</option>` +
      `<option value="part_time"${a.employment_type === "part_time" ? " selected" : ""}>Part time</option></select></td>` +
      `<td><input class="n" type="number" step="0.5" value="${a.weekly_hours ?? ""}"></td>` +
      `<td><input class="s" type="text" maxlength="2" value="${a.work_state || ""}" placeholder="CA"></td>` +
      `<td><input type="text" value="${(a.notes || "").replace(/"/g, "&quot;")}" style="width:220px"></td><td><button>Save</button></td>` +
      `<td class="pto-bal">${p ? p.balance.toFixed(2) + " h" : "–"}</td><td title="${p ? p.detail : ""}">${p ? p.status : ""}</td>` +
      `<td><button class="pto-adj" type="button">Adjust</button> <button class="pto-led" type="button">Ledger</button></td>`;
    tr.querySelector(".pto-adj").addEventListener("click", () => ptoAdjustModal(a.agent, p));
    tr.querySelector(".pto-led").addEventListener("click", () => ptoLedgerModal(a.agent));
    const [sd, et, wh, ws, nt] = tr.querySelectorAll("input, select");
    const btn = tr.querySelector("td:nth-child(7) button");
    btn.addEventListener("click", async () => {
      const r = await fetch("/api/managers/agents", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: a.agent, start_date: sd.value, employment_type: et.value, weekly_hours: wh.value, work_state: ws.value.toUpperCase(), notes: nt.value }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { toast(j.error || "Couldn't save.", "warn"); return; }
      btn.textContent = "Saved"; btn.className = "saved"; setTimeout(() => { btn.textContent = "Save"; btn.className = ""; }, 2000);
    });
    tb.appendChild(tr);
  }
  table.appendChild(tb); wrap.appendChild(table);
  box.replaceChildren(wrap);
  noteInto("#hr-table", "Start date, employment type and hours drive PTO (policy 4-09): accrual starts 180 days after the start date, " +
    "2 h per 80 worked for under two years of service and 3 per 80 after, full time = 30+ h/week (the Employment field overrides the " +
    "8-week average when set). The 80 h cap starts Jan 1, 2027: balances above 80 on Dec 31 are cut to 80 that day, then accrual stops at 80; until then nobody is capped. Only hours actually worked earn PTO — the time clock from Oct 26, 2026 (a fixed punch re-figures that day); scheduled hours never do. " +
    "Balances were set from Homebase as of Oct 25, 2026. Adjust adds or removes hours with a note; Ledger shows every entry.", true);
}
function ptoAdjustModal(agent, p) {
  const ov = document.createElement("div"); ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div"); card.className = "modal-card";
  card.innerHTML = `<div class="modal-head"><b>Adjust PTO</b><span class="sub">${agent} · balance ${p ? p.balance.toFixed(2) : "–"} h</span></div>` +
    `<label class="modal-field">Hours (negative takes hours away) <input type="number" step="0.25" id="pa-h"></label>` +
    `<label class="modal-field">Kind <select id="pa-kind"><option value="adjust">Adjustment</option><option value="use">PTO used</option></select></label>` +
    `<label class="modal-field">Day <input type="date" id="pa-day" value="${new Date().toISOString().slice(0, 10)}"></label>` +
    `<label class="modal-field">Note <input type="text" id="pa-note" placeholder="why"></label>` +
    `<div style="display:flex;gap:8px;justify-content:flex-end"><button class="year-btn" id="pa-cancel">Cancel</button><button class="year-btn active" id="pa-save">Save</button></div>`;
  ov.appendChild(card); document.body.appendChild(ov);
  card.querySelector("#pa-cancel").addEventListener("click", () => ov.remove());
  card.querySelector("#pa-save").addEventListener("click", async () => {
    let hours = parseFloat(card.querySelector("#pa-h").value);
    const kind = card.querySelector("#pa-kind").value;
    if (kind === "use" && hours > 0) hours = -hours;          // a use always lowers the balance
    const r = await fetch("/api/managers/pto/adjust", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent, hours, kind, day: card.querySelector("#pa-day").value, note: card.querySelector("#pa-note").value }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.error || "Couldn't save.", "warn"); return; }
    ov.remove(); toast(`Saved — balance now ${j.balance.toFixed(2)} h.`, "ok"); loadHr();
  });
}
async function ptoLedgerModal(agent) {
  const ov = document.createElement("div"); ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div"); card.className = "modal-card day-roster";
  card.innerHTML = `<div class="modal-head" style="display:flex;justify-content:space-between"><div><b>PTO ledger</b><span class="sub">${agent}</span></div><button class="sd-close">×</button></div><div class="dr-body">Loading…</div>`;
  card.querySelector(".sd-close").addEventListener("click", () => ov.remove());
  ov.appendChild(card); document.body.appendChild(ov);
  let d = null;
  try { d = await (await fetch(`/api/managers/pto/ledger?agent=${encodeURIComponent(agent)}`)).json(); } catch (e) { d = null; }
  const body = card.querySelector(".dr-body");
  if (!d || !d.rows) { body.textContent = "Couldn't load."; return; }
  let bal = d.rows.reduce((a, r) => a + r.hours, 0);
  const esc = (s) => String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const lines = d.rows.map((r) => { const line = `<tr><td>${r.day}</td><td>${r.kind}</td><td class="n">${r.hours > 0 ? "+" : ""}${r.hours.toFixed(2)}</td><td class="n">${bal.toFixed(2)}</td><td>${esc(r.note)}</td></tr>`; bal -= r.hours; return line; });
  body.innerHTML = d.rows.length ? `<table class="pto-ledger"><tr><th>Day</th><th>Kind</th><th>Hours</th><th>Balance</th><th>Note</th></tr>${lines.join("")}</table>` : "No entries yet.";
}

/* ---------- marketing (admin) unlock + sources ---------- */
const SRC = {
  cache: {}, range: null,
  cats: { both: true, phone: true, form: true, none: true },
  picked: null,      // null = all sources; otherwise a Set of names
  pickerKey: null,   // dataset the picker was built for
};

// every source lands in exactly ONE lead-type bucket
function srcCategory(r) {
  const ph = r.leads > 0, fo = r.form_net > 0;
  if (ph && fo) return "both";
  if (ph) return "phone";
  if (fo) return "form";
  return "none";
}

const SRC_CATS = [
  ["phone", "Has phone leads"], ["form", "Has form leads"],
  ["both", "Has both"], ["none", "No leads"],
];

/* Shared-password logins only (until PASSWORD_LOGIN is switched off): the
   admin password widens access, enforced server-side via the is_admin cookie. */
const ADMIN = { ok: false };


// footer: who is signed in (accounts) + a sign-out link for everyone
async function initWho() {
  let me = null;
  try { me = await (await fetch("/api/me")).json(); } catch (e) { return; }
  const box = $("#who");
  if (!box) return;
  const out = document.createElement("a");
  out.href = "#";
  out.textContent = "Sign out";
  out.addEventListener("click", async (ev) => {
    ev.preventDefault();
    await fetch("/api/auth/logout", { method: "POST" });
    location.href = "/";
  });
  box.replaceChildren(
    document.createTextNode(me && me.user ? `Signed in as ${me.user.email} · ` : ""), out);
}

function initMarketing() {
  ADMIN.ok = ME.admin;
  $("#mktg-unlock").addEventListener("click", () => adminUnlock());
}

// prompt for the admin password and set the cookie; returns true on success
async function adminUnlock() {
  if (ADMIN.ok) return true;
  const pw = prompt("Admin password (Settings & Sources):");
  if (!pw) return false;
  const r = await fetch("/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }),
  });
  if (!r.ok) { alert("Wrong password."); return false; }
  location.reload();                    // come back with the admin tabs showing
  return true;
}

async function loadSources() {
  if (!SRC.range) return;
  const ck = SRC.range.join("..");
  if (!SRC.cache[ck]) {
    const r = await (await fetch(
      `/api/sources?start_day=${SRC.range[0]}&end_day=${SRC.range[1]}`)).json();
    if (r.error) {
      ADMIN.ok = false;
      $("#src-table").replaceChildren(emptyBox("Locked — enter the admin password."));
      return;
    }
    SRC.cache[ck] = r;
  }
  buildSourceFilters(ck);
  renderSourcesTable(ck);
}

function buildSourceFilters(ck) {
  // category chips (bound once)
  const catBox = $("#src-cats");
  if (!catBox.childElementCount) {
    for (const [key, label] of SRC_CATS) {
      const lab = document.createElement("label");
      lab.className = SRC.cats[key] ? "on" : "";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = SRC.cats[key];
      cb.addEventListener("change", () => {
        SRC.cats[key] = cb.checked;
        lab.classList.toggle("on", cb.checked);
        if (SRC.range) renderSourcesTable(SRC.range.join(".."));
      });
      lab.append(cb, document.createTextNode(label));
      catBox.appendChild(lab);
    }
  }
  // source check-list (rebuilt when the dataset changes)
  if (SRC.pickerKey === ck) return;
  SRC.pickerKey = ck;
  SRC.picked = null;
  const body = $("#src-picker-body");
  body.replaceChildren();
  const actions = document.createElement("div");
  actions.className = "picker-actions";
  const names = SRC.cache[ck].rows.map((r) => r.source);
  const mk = (txt, fn) => {
    const b = document.createElement("button");
    b.textContent = txt;
    b.addEventListener("click", (e) => { e.preventDefault(); fn(); });
    return b;
  };
  actions.append(
    mk("All", () => { SRC.picked = null;
      body.querySelectorAll("input").forEach((c) => c.checked = true);
      renderSourcesTable(ck); }),
    mk("None", () => { SRC.picked = new Set();
      body.querySelectorAll("input").forEach((c) => c.checked = false);
      renderSourcesTable(ck); }));
  body.appendChild(actions);
  for (const name of names) {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.value = name;
    cb.addEventListener("change", () => {
      if (SRC.picked === null) SRC.picked = new Set(names);
      cb.checked ? SRC.picked.add(name) : SRC.picked.delete(name);
      if (SRC.picked.size === names.length) SRC.picked = null;
      renderSourcesTable(ck);
    });
    lab.append(cb, document.createTextNode(name));
    body.appendChild(lab);
  }
}

function renderSourcesTable(ck) {
  const data = SRC.cache[ck];
  if (!data) return;
  let rows = data.rows.filter((r) => SRC.cats[srcCategory(r)]);
  if (SRC.picked !== null) rows = rows.filter((r) => SRC.picked.has(r.source));
  $("#src-list").querySelector("summary").textContent =
    SRC.picked === null ? "Sources (all)" : `Sources (${SRC.picked.size})`;
  const money = (v) => v == null ? "–" : "$" + v.toLocaleString(undefined, {maximumFractionDigits: 0});
  const p = (v) => v == null ? "–" : v.toFixed(1);
  const cols = [
    ["source", "Source", "Lead provider / channel (from the sourcing master sheet)"],
    // 📞 Genesys — phone dispositions only
    ["leads", "Ph Leads", "GENESYS: unique callers dispositioned on this source's tracking numbers (Closed / Follow Up / Unqualified)"],
    ["qual", "Qual", "GENESYS: unique callers dispositioned Closed or Follow Up"],
    ["qual_pct", "Qual%", "Qualified as a % of phone leads"],
    ["closed", "Closed", "GENESYS: unique callers the agent dispositioned as 'Closed' (a claimed close — not the PestRoutes-verified sale)"],
    ["close_pct", "Close%", "Closed as a % of qualified callers"],
    // 📝 Forms
    ["form_net", "Total Forms", "FORMS: net form leads from this source (existing customers excluded)"],
    ["form_conv", "Forms Conv", "FORMS: form leads that became a FieldRoutes subscription within 90 days"],
    ["form_pct", "Form Cv%", "Forms converted as a % of total forms"],
    // 🗂 PestRoutes — Pending + Serviced accounts only
    ["subs_sold", "Subs Sold", "PESTROUTES: subscriptions sold with this source label — any seller, counted by sale date. Only Pending + Serviced statuses (never-scheduled and cancelled excluded)"],
    ["sts_pct", "Sold→Svc%", "PESTROUTES: of subs whose first service was decided, the % that got serviced = Serviced ÷ (Serviced + Cancelled + No Show). Pending and never-scheduled excluded"],
    ["pending", "Pending", "PESTROUTES: of those, subscriptions still awaiting their first service"],
    ["multiyear_pct", "MY%", "PESTROUTES: 18-month+ agreements as a % of Pending + Serviced subs"],
    ["acv", "ACV", "PESTROUTES: average contract value of Pending + Serviced subs"],
    ["init_avg", "Avg Init", "PESTROUTES: average first-service price of Pending + Serviced subs"],
    ["apay_pct", "APAY", "PESTROUTES: % of Pending + Serviced subs with a payment method on file"],
  ];
  const groups = [
    { label: "", span: 1 },
    { label: "📞 Genesys — phone dispositions", span: 5, cls: "grp-genesys" },
    { label: "📝 Forms", span: 3, cls: "grp-forms" },
    { label: "🗂 PestRoutes — accounts (Pending + Serviced)", span: 7, cls: "grp-pr" },
  ];
  $("#src-table").replaceChildren(simpleTable(cols, rows, {
    groups, expandable: true, onName: (r) => openSourceDetail(r.source),
    hl: new Set(["close_pct", "acv"]),
    fmt: { qual_pct: p, close_pct: p, form_pct: p, sts_pct: p,
           multiyear_pct: p, acv: money, init_avg: money, apay_pct: p },
    sortable: true, defaultSort: "leads",
  }));
}

/* ---------- source detail overlay (click a source name) ---------- */
function sdEsc(e) { if (e.key === "Escape") closeSourceDetail(); }
function closeSourceDetail() {
  const ov = $("#src-detail-overlay");
  if (ov) ov.style.display = "none";
  document.removeEventListener("keydown", sdEsc);
}

async function openSourceDetail(name) {
  let ov = $("#src-detail-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "src-detail-overlay";
    ov.className = "sd-overlay";
    ov.addEventListener("click", (e) => { if (e.target === ov) closeSourceDetail(); });
    document.body.appendChild(ov);
  }
  ov.replaceChildren(el("div", "sd-card", el("div", "sd-body sd-loading", `Loading ${name}…`)));
  ov.style.display = "flex";
  document.addEventListener("keydown", sdEsc);
  let d;
  try {
    d = await (await fetch(`/api/source_detail?source=${encodeURIComponent(name)}`)).json();
  } catch (e) { d = { error: "network" }; }
  if (d.error) {
    ov.querySelector(".sd-body").textContent = "Couldn't load this source (" + d.error + ").";
    return;
  }
  renderSourceDetail(ov, d);
}

function el(tag, cls, content) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (typeof content === "string") e.innerHTML = content;
  else if (content) e.appendChild(content);
  return e;
}

function sdDay(iso) {
  return new Date(iso + "T00:00:00").toLocaleString("en-US", { month: "short", day: "numeric" });
}

function renderSourceDetail(ov, d) {
  const s = d.series, w = d.weeks;
  const card = el("div", "sd-card");
  const head = el("div", "sd-head",
    `<div><h2>${d.source}</h2><span class="sd-sub">Charts show the trailing 90 days, weekly (Sun–Sat)</span></div>`);
  const close = el("button", "sd-close", "×");
  close.addEventListener("click", closeSourceDetail);
  head.appendChild(close);
  card.appendChild(head);

  const body = el("div", "sd-body");

  // A — Genesys dispositions, with its OWN date-range picker, + quality charts
  const secHead = el("div", "sd-dispo-head");
  secHead.appendChild(el("h3", "sd-section-inline", "📞 Genesys dispositions"));
  const pickMount = el("div", "controls sd-dispo-picker");
  secHead.appendChild(pickMount);
  body.appendChild(secHead);

  const cardsBox = el("div", "sd-dispo-cards");   // Closed + Follow Up (re-render on range)
  const rightBox = el("div", "sd-dispo-right");   // Unqualified (re-render on range)
  const updateDispo = (dispo) => {
    const grand = (dispo.closed?.total || 0) + (dispo.followup?.total || 0) + (dispo.unqualified?.total || 0);
    cardsBox.replaceChildren(dispoCard("closed", "Closed", dispo.closed, grand),
                             dispoCard("followup", "Follow Up", dispo.followup, grand));
    rightBox.replaceChildren(dispoCard("unqualified", "Unqualified", dispo.unqualified, grand));
  };
  updateDispo(d.dispositions);

  const chartsBox = el("div", "sd-dispo-charts");  // trailing-90d quality trends (static)
  chartsBox.append(
    detailChart("% Qualified (of callers)", s.qual_pct, w, "#1a6bb0", true),
    detailChart("Conversion % (of qualified)", s.close_pct, w, "#3a8a4d", true),
  );

  const left = el("div", "sd-dispo-left");
  left.append(cardsBox, chartsBox);
  const layout = el("div", "sd-dispo-layout");
  layout.append(left, rightBox);
  body.appendChild(layout);

  makeRangePicker(pickMount, async (a, b) => {
    try {
      const r = await (await fetch(
        `/api/source_dispositions?source=${encodeURIComponent(d.source)}&start_day=${a}&end_day=${b}`)).json();
      if (!r.error) updateDispo(r.dispositions);
    } catch (e) { /* leave current */ }
  }, "last_week");

  // B — lead volume (unique callers + forms)
  const b = [];
  if (d.has_calls) {
    b.push(detailChart("Unique callers", s.total_calls, w, "#1e3659", "count"));
    b.push(detailChart("Qualified callers (Closed/Follow-Up)", s.qual_calls, w, "#3a8a4d", "count"));
  }
  if (d.has_forms) b.push(detailChart("Form leads", s.forms, w, "#8a5cc0", "count"));
  if (d.has_calls && d.has_forms)
    b.push(detailChart("Total qualified sales leads (qual callers + forms)", s.qual_leads, w, "#1e3659", "count"));
  if (b.length) {
    body.appendChild(el("h3", "sd-section", "Lead volume — trailing 90 days (unique callers &amp; form leads)"));
    body.appendChild(chartGrid(b));
  }

  // C — PestRoutes economics (sold + serviced combined into one ratio)
  body.appendChild(el("h3", "sd-section", "🗂 PestRoutes — accounts (Pending + Serviced)"));
  body.appendChild(chartGrid([
    detailChart("Revenue", s.revenue, w, "#b06a17", false),
    detailChart("Multiyear %", s.multiyear_pct, w, "#b06a17", true),
    detailChart("ACV", s.acv, w, "#b06a17", false),
    detailChart("Avg initial", s.avg_init, w, "#b06a17", false),
    detailChart("Sold → Serviced %", s.sts_pct, w, "#b06a17", true),
  ]));

  card.appendChild(body);
  ov.replaceChildren(card);
}

function chartGrid(cards) {
  const g = el("div", "sd-grid");
  cards.forEach((c) => g.appendChild(c));
  return g;
}

function detailChart(title, values, weeks, color, kind) {
  const card = el("div", "sd-chart");
  const h = el("div", "sd-chart-title", `<span>${title}</span>`);
  const latest = [...values].reverse().find((v) => v != null);
  const txt = latest == null ? "–"
    : kind === true ? latest.toFixed(1) + "%"
    : kind === "count" ? Math.round(latest).toLocaleString()
    : "$" + Math.round(latest).toLocaleString();
  h.appendChild(el("span", "sd-chart-val", txt));
  card.appendChild(h);
  card.appendChild(trendChart(values, weeks, color, kind));
  return card;
}

function dispoCard(key, label, cat, grand) {
  cat = cat || { total: 0, items: [] };
  // every % is a share of the grand total (all dispositions), so the three
  // category totals add to 100% and each item is its slice of the same total
  const pctOf = (n) => (grand ? ` <span class="sd-dispo-pct">${(100 * n / grand).toFixed(1)}%</span>` : "");
  const col = el("div", "sd-dispo-cat",
    el("div", "sd-dispo-h sd-dispo-" + key, `${label} <b>${cat.total.toLocaleString()}${pctOf(cat.total)}</b>`));
  if (!cat.items.length) col.appendChild(el("div", "sd-dispo-empty", "none in range"));
  else for (const it of cat.items)
    col.appendChild(el("div", "sd-dispo-row", `<span>${it.name}</span><b>${it.count.toLocaleString()}${pctOf(it.count)}</b>`));
  return col;
}

/* ---------- agent view ---------- */
const AV = { cache: {}, range: null, picks: [null, null, null], ready: false };

function avKey() {
  return AV.range ? AV.range.join("..") : "";
}

async function loadAgentView() {
  if (!AV.range) return;
  const key = avKey();
  if (!AV.cache[key]) {
    AV.cache[key] = await (await fetch(
      `/api/agentview?start_day=${AV.range[0]}&end_day=${AV.range[1]}`)).json();
  }
  const data = AV.cache[key];
  if (!AV.ready) {
    AV.ready = true;
    AV.picks = [data.agents[0] || null, null, null];
    for (const i of [0, 1, 2]) {
      const sel = $("#av-a" + (i + 1));
      sel.append(new Option("— agent —", ""));
      for (const a of data.agents) sel.append(new Option(a, a));
      sel.value = AV.picks[i] || "";
      sel.addEventListener("change", () => { AV.picks[i] = sel.value || null; renderAgentView(); });
    }
  }
  renderAgentView();
}

const AV_SECTIONS = [
  ["Inbound Phone", [
    ["calls", "Calls taken", "n"],
    ["leads", "Unique leads", "n"],
    ["qual_leads", "Qualified leads", "n"],
    ["conversions", "Conversions", "n"],
    ["conv_qual_pct", "Conv % of qualified", "pct+"],
    ["conv_leads_pct", "Conv % of all leads", "pct+"],
    ["conv_calls_pct", "Conv % of calls", "pct+"],
    ["closed_pct", "Closed dispo %", "pct"],
    ["followup_pct", "Follow-up dispo %", "pct"],
    ["unqualified_pct", "Unqualified dispo %", "pct"],
  ]],
  ["Subscriptions Sold", [
    ["subs", "Counted subscriptions", "n"],
    ["multiyear_pct", "Multiyear % (18mo+)", "pct+"],
    ["acv", "Avg contract value", "$+"],
    ["init_avg", "Avg initial price", "$+"],
    ["cv_total", "Total contract value", "$"],
    ["sts_pct", "Sold → serviced %", "pct+"],
    ["sts_pending", "…undecided (pending)", "n"],
  ]],
  ["Audit", [
    ["audit_qual", "Audited qualified calls", "n"],
    ["audit_sales", "Audited sales", "n"],
    ["sheet_conv_pct", "Sheet conversion %", "pct+"],
    ["hidden", "Hidden conversions", "n"],
    ["leaked", "Leaked leads", "n"],
    ["true_conv_pct", "Honest conv % (adjusted)", "pct+"],
  ]],
];

const AV_TIPS = {
  calls: "Every call handled on the ISA queues, any wrap-up",
  leads: "Unique callers dispositioned Closed / Follow Up / Unqualified (or converted)",
  qual_leads: "Unique callers dispositioned Closed or Follow Up, or who converted",
  conversions: "Unique callers who became a customer within 14 days (any seller if pitched; own-name sales always)",
  conv_qual_pct: "Conversions ÷ qualified leads",
  conv_leads_pct: "Conversions ÷ all unique leads (incl. unqualified)",
  conv_calls_pct: "Conversions ÷ total calls taken",
  closed_pct: "Share of sales-dispositioned calls wrapped Closed",
  followup_pct: "Share wrapped Follow Up",
  unqualified_pct: "Share wrapped Unqualified",
  subs: "Subscriptions in the agent's own name, serviced or pending, mosquito-upsell excluded",
  pending: "Counted subscriptions still waiting on their first service",
  multiyear_pct: "18-month+ agreements as % of counted subscriptions",
  acv: "Average contract value of counted subscriptions",
  init_avg: "Average price of the first service on counted subscriptions",
  cv_total: "Total contract value of counted subscriptions",
  sts_pct: "Sold → serviced: of subs that got an initial appointment SCHEDULED and are decided, % whose first service happened; pending and never-scheduled excluded",
  sts_pending: "Subscriptions sold in the window still awaiting their first service",
  sheet_conv_pct: "THE default conversion metric: audit sheet sales ÷ audit sheet qualified, verbatim",
  audit_qual: "Qualified calls per the audit team's sheet",
  audit_sales: "Sales per the audit team's sheet",
  hidden: "Conversions whose pitch never made the audit base",
  leaked: "Unqualified-marked callers who bought elsewhere within 14 days",
  true_conv_pct: "GOLD STANDARD: verified conversions ÷ (audited qualified + hidden + leaked), audit-covered days only",
};

function avFmt(v, kind) {
  if (v == null) return "–";
  if (kind.startsWith("pct")) return v.toFixed(1) + "%";
  if (kind.startsWith("$")) return "$" + v.toLocaleString(undefined, {maximumFractionDigits: 0});
  return v.toLocaleString();
}

function renderAgentView() {
  const data = AV.cache[avKey()];
  if (!data) return;
  const picks = AV.picks.filter(Boolean);
  const team = data.stats.TEAM || {};
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "av-compare";          // compact + centred (managers found the full-width layout hard to read)
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const label of ["Metric", ...picks, "TEAM"]) {
    const th = document.createElement("th");
    th.textContent = label;
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const [section, metrics] of AV_SECTIONS) {
    const sr = document.createElement("tr");
    sr.className = "metric-section";
    const sc = document.createElement("td");
    sc.colSpan = picks.length + 2;
    sc.textContent = section;
    sr.appendChild(sc);
    tbody.appendChild(sr);
    for (const [key, label, kind] of metrics) {
      const tr = document.createElement("tr");
      const tl = document.createElement("td");
      tl.textContent = label;
      if (AV_TIPS[key]) tl.title = AV_TIPS[key];
      tr.appendChild(tl);
      for (const agent of picks) {
        const v = data.stats[agent]?.[key];
        const td = document.createElement("td");
        td.textContent = avFmt(v, kind);
        if (kind.endsWith("+") && v != null && team[key] != null) {
          const better = v >= team[key];
          const d = document.createElement("small");
          d.className = "delta-inline " + (better ? "up" : "down");
          const diff = v - team[key];
          d.textContent = " " + (diff >= 0 ? "+" : "") +
            (kind.startsWith("$") ? "$" + Math.round(diff).toLocaleString() : diff.toFixed(1));
          td.appendChild(d);
        }
        tr.appendChild(td);
      }
      const tt = document.createElement("td");
      tt.textContent = avFmt(team[key], kind);
      tt.className = "dim";
      tr.appendChild(tt);
      tbody.appendChild(tr);
    }
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  $("#av-table").replaceChildren(wrap);
}

/* ---------- multiyear + contract value ---------- */
function renderMyRows(rows, caption) {
  const cols = [
    ["agent", "Agent"],
    ["subs", "Subs", "Subscriptions sold in the agent's own name (soldBy), first service serviced or pending; MOSQUITO UPSELL CAMPAIGN excluded"],
    ["pending", "Pend", "Counted subscriptions whose first service hasn't happened yet — they fall off later if it never does"],
    ["lt12", "<12mo", "Agreements under 12 months — includes one-time treatments (no contract)"], ["lt12_pct", "<12%", "% of counted subs under 12 months"],
    ["m12", "12mo", "12-17 month agreements"], ["m12_pct", "12%", "% of counted subs at 12-17 months"],
    ["m18", "18mo", "18-23 month agreements"], ["m18_pct", "18%", "% of counted subs at 18-23 months"],
    ["m24", "24mo+", "24 month and longer agreements"], ["m24_pct", "24%+", "% of counted subs at 24+ months"],
    ["multiyear_pct", "MY%", "Multiyear rate: 18-month-or-longer agreements as a % of counted subscriptions"],
  ];
  const table = simpleTable(cols, rows, {
    hl: new Set(["multiyear_pct"]),
    fmt: Object.fromEntries(["lt12_pct","m12_pct","m18_pct","m24_pct","multiyear_pct"]
      .map((k) => [k, (v) => (v ?? 0).toFixed(1)])),
    sortable: true, defaultSort: "multiyear_pct", pin: (r) => r.agent === "TEAM TOTAL",
  });
  $("#my-table").replaceChildren(...(caption ? [table, caption] : [table]));
}

function renderCvRows(rows, caption) {
  const money = (v) => "$" + (v ?? 0).toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
  const cols = [
    ["agent", "Agent"],
    ["subs", "Subs", "Subscriptions sold in the agent's own name (soldBy), first service serviced or pending; MOSQUITO UPSELL CAMPAIGN excluded"],
    ["pending", "Pend", "Counted subscriptions whose first service hasn't happened yet"],
    ["acv", "Avg Contract Value", "Average FieldRoutes contractValue across the agent's counted subscriptions"],
    ["init_avg", "Avg Initial Price", "Average price of the FIRST service on the agent's counted subscriptions"],
    ["cv_total", "Total Contract Value", "Sum of contract value across counted subscriptions"],
  ];
  const table = simpleTable(cols, rows, {
    hl: new Set(["acv"]),
    fmt: { acv: money, cv_total: money, init_avg: money },
    sortable: true, defaultSort: "acv", pin: (r) => r.agent === "TEAM TOTAL",
  });
  $("#cv-table").replaceChildren(...(caption ? [table, caption] : [table]));
}

/* generic small table; opts.sortable adds click-to-sort headers,
   opts.pin(r) rows stay pinned at the bottom (e.g. TEAM TOTAL) */
function simpleTable(cols, rows, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const st = { key: opts.defaultSort || null, asc: false };
  const openSet = new Set();   // expandable groups left open across re-sorts

  function render() {
    let body = rows, pinned = [];
    if (opts.pin) {
      pinned = rows.filter(opts.pin);
      body = rows.filter((r) => !opts.pin(r));
    }
    if (opts.sortable && st.key) {
      body = body.slice().sort((a, b) => {
        const va = a[st.key], vb = b[st.key];
        const cmp = typeof va === "string" ? va.localeCompare(vb) : (va ?? -1) - (vb ?? -1);
        return st.asc ? cmp : -cmp;
      });
    }
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    // optional grouped header: thick divider before each group after the first
    const startIdx = new Set();
    if (opts.groups) {
      let i = 0;
      for (const g of opts.groups) { if (i > 0) startIdx.add(i); i += g.span; }
      const gr = document.createElement("tr");
      gr.className = "group-row";
      let gi = 0;
      for (const g of opts.groups) {
        const th = document.createElement("th");
        th.textContent = g.label || "";
        th.colSpan = g.span;
        if (g.cls) th.classList.add(g.cls);
        if (gi > 0) th.classList.add("grp-start");
        gr.appendChild(th);
        gi += g.span;
      }
      thead.appendChild(gr);
    }
    const hr = document.createElement("tr");
    cols.forEach(([key, label, tip], j) => {
      const th = document.createElement("th");
      th.textContent = label;
      if (tip) th.title = tip;
      if (startIdx.has(j)) th.classList.add("grp-start");
      if (opts.sortable) {
        if (st.key === key) { th.classList.add("sorted"); if (st.asc) th.classList.add("asc"); }
        th.addEventListener("click", () => {
          if (st.key === key) st.asc = !st.asc;
          else { st.key = key; st.asc = key === "agent"; }
          render();
        });
      }
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    const cellText = (r, key) => {
      const v = r[key];
      return opts.fmt?.[key] ? opts.fmt[key](v)
        : typeof v === "number" ? v.toLocaleString() : (v ?? "");
    };
    const sortKids = (arr) => {
      if (!(opts.sortable && st.key)) return arr;
      return arr.slice().sort((a, b) => {
        const va = a[st.key], vb = b[st.key];
        const c = typeof va === "string" ? va.localeCompare(vb) : (va ?? -1) - (vb ?? -1);
        return st.asc ? c : -c;
      });
    };
    // opts.onName(row) makes the first-column name a clickable link (drill-in)
    const makeName = (r, text) => {
      if (!opts.onName) return document.createTextNode(text);
      const a = document.createElement("span");
      a.className = "src-name-link";
      a.textContent = text;
      a.addEventListener("click", (e) => { e.stopPropagation(); opts.onName(r); });
      return a;
    };
    let gid = 0;
    for (const r of [...body, ...pinned]) {
      const tr = document.createElement("tr");
      if (opts.pin?.(r)) tr.className = "total-row";
      const kids = opts.expandable && r.children?.length ? r.children : null;
      const pid = kids ? "sg" + (gid++) : null;
      const isOpen = kids && openSet.has(r.source);
      cols.forEach(([key], j) => {
        const td = document.createElement("td");
        if (j === 0 && kids) {
          const arrow = document.createElement("span");
          arrow.className = "src-toggle";
          arrow.textContent = isOpen ? "▼" : "▶";
          const toggle = (e) => {
            e.stopPropagation();
            const nowOpen = !tr.classList.contains("open");
            tr.classList.toggle("open", nowOpen);
            arrow.textContent = nowOpen ? "▼" : "▶";
            nowOpen ? openSet.add(r.source) : openSet.delete(r.source);
            tbody.querySelectorAll(`tr[data-parent="${pid}"]`).forEach((c) => { c.hidden = !nowOpen; });
          };
          arrow.addEventListener("click", toggle);
          td.append(arrow, document.createTextNode(" "), makeName(r, cellText(r, key)));
        } else if (j === 0) {
          td.append(makeName(r, cellText(r, key)));
        } else {
          td.textContent = cellText(r, key);
        }
        if (opts.hl?.has(key)) td.classList.add("hl");
        if (startIdx.has(j)) td.classList.add("grp-start");
        const fl = opts.flag?.(r, key);
        if (fl) td.classList.add(fl);
        tr.appendChild(td);
      });
      if (isOpen) tr.classList.add("open");
      tbody.appendChild(tr);
      if (kids) {
        for (const c of sortKids(kids)) {
          const ctr = document.createElement("tr");
          ctr.className = "src-child";
          ctr.dataset.parent = pid;
          ctr.hidden = !isOpen;
          cols.forEach(([key], j) => {
            const td = document.createElement("td");
            if (j === 0) { td.classList.add("src-child-name"); td.append(makeName(c, cellText(c, key))); }
            else td.textContent = cellText(c, key);
            if (opts.hl?.has(key)) td.classList.add("hl");
            if (startIdx.has(j)) td.classList.add("grp-start");
            ctr.appendChild(td);
          });
          tbody.appendChild(ctr);
        }
      }
    }
    table.appendChild(tbody);
    wrap.replaceChildren(table);
  }
  render();
  return wrap;
}

function emptyBox(msg) {
  const d = document.createElement("div");
  d.className = "empty-box";
  d.textContent = msg;
  return d;
}

function periods(gran) {
  return [...new Set(DATA[gran].map((r) => r.period))].sort().reverse();
}
function rowsFor(gran, period) {
  return DATA[gran].filter((r) => r.period === period);
}

/* ---------- tables ---------- */
function buildTable(rows, st, onSort) {
  const agents = rows.filter((r) => r.agent !== "__TOTAL__");
  const total = rows.find((r) => r.agent === "__TOTAL__");
  agents.sort((a, b) => {
    const va = a[st.sort], vb = b[st.sort];
    const cmp = typeof va === "string" ? va.localeCompare(vb) : va - vb;
    return st.asc ? cmp : -cmp;
  });

  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const c of COLS) {
    const th = document.createElement("th");
    if (c.src) {
      const dot = document.createElement("span");
      dot.className = "dot src-" + c.src;
      th.appendChild(dot);
    }
    th.appendChild(document.createTextNode(c.label));
    if (c.tip) th.title = c.tip;
    if (st.sort === c.key) th.classList.add("sorted", ...(st.asc ? ["asc"] : []));
    th.addEventListener("click", () => onSort(c.key));
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const r of agents) tbody.appendChild(renderRow(r));
  if (total) {
    const tr = renderRow({ ...total, agent: "TEAM TOTAL" });
    tr.className = "total-row";
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function renderRow(r) {
  const tr = document.createElement("tr");
  for (const c of COLS) {
    const td = document.createElement("td");
    let v = r[c.key];
    if (c.pct) {
      td.textContent = v == null ? "–" : v.toFixed(1);
      if (!v) td.classList.add("dim");
    }
    else td.textContent = typeof v === "number" ? v.toLocaleString() : v;
    if (c.hl) td.classList.add("hl");
    tr.appendChild(td);
  }
  return tr;
}

function toggleSort(st, key) {
  if (st.sort === key) st.asc = !st.asc;
  else { st.sort = key; st.asc = key === "agent"; }
}

function noData(msg) {
  const d = document.createElement("p");
  d.textContent = msg;
  d.style.color = "var(--muted)";
  return d;
}

/* ---------- trends ---------- */
function setupTrends() {
  const msel = $("#tr-metric");
  for (const [k, label] of TREND_METRICS) {
    const o = document.createElement("option");
    o.value = k; o.textContent = label;
    msel.appendChild(o);
  }
  msel.addEventListener("change", () => { state.trends.metric = msel.value; renderTrend(); });
  $("#tr-gran").addEventListener("change", (e) => { state.trends.gran = e.target.value; renderTrend(); });

  const agents = [...new Set(DATA.monthly.map((r) => r.agent))]
    .filter((a) => a !== "__TOTAL__").sort();
  const box = $("#tr-agents");
  for (const [i, name] of [["TEAM", "TEAM (total)"], ...agents.map((a) => [a, a])].entries()) {
    const [val, label] = name;
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = val;
    cb.checked = state.trends.agents.has(val);
    cb.addEventListener("change", () => {
      cb.checked ? state.trends.agents.add(val) : state.trends.agents.delete(val);
      renderTrend();
    });
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = colorFor(val);
    lab.append(cb, sw, document.createTextNode(label));
    box.appendChild(lab);
  }
}

function colorFor(name) {
  if (name === "TEAM") return "#1b2733";
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

function renderTrend() {
  const { metric, gran } = state.trends;
  if (!DATA || !DATA[gran]) return;   // data still loading (or no Phone permission): init() draws it when it lands
  const ps = periods(gran).slice().reverse(); // oldest -> newest
  const series = [];
  for (const who of state.trends.agents) {
    const pts = ps.map((p) => {
      const r = DATA[gran].find((x) => x.period === p &&
        x.agent === (who === "TEAM" ? "__TOTAL__" : who));
      return r ? r[metric] : null;
    });
    series.push({ who, pts });
  }
  $("#tr-chart").replaceChildren(svgChart(ps, series, metric));
}

function svgChart(ps, series, metric) {
  const W = 900, H = 420, M = { t: 16, r: 130, b: 58, l: 46 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const vals = series.flatMap((s) => s.pts).filter((v) => v != null);
  const ymax = Math.max(10, ...vals) * 1.08;
  const isPct = metric.endsWith("_pct");
  const x = (i) => M.l + (ps.length < 2 ? iw / 2 : (i / (ps.length - 1)) * iw);
  const y = (v) => M.t + ih - (v / ymax) * ih;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  for (let g = 0; g <= 4; g++) {
    const v = (ymax / 4) * g;
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", M.l); line.setAttribute("x2", W - M.r);
    line.setAttribute("y1", y(v)); line.setAttribute("y2", y(v));
    line.setAttribute("class", "gridline");
    svg.appendChild(line);
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", M.l - 6); t.setAttribute("y", y(v) + 3);
    t.setAttribute("text-anchor", "end"); t.setAttribute("class", "axis-label");
    t.textContent = isPct ? v.toFixed(0) + "%" : Math.round(v).toLocaleString();
    svg.appendChild(t);
  }
  const stepN = Math.ceil(ps.length / 12);
  ps.forEach((p, i) => {
    if (i % stepN !== 0 && i !== ps.length - 1) return;
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", x(i)); t.setAttribute("y", H - M.b + 26);
    t.setAttribute("text-anchor", "end"); t.setAttribute("class", "axis-label");
    t.setAttribute("transform", `rotate(-35 ${x(i)} ${H - M.b + 26})`);
    t.textContent = p.slice(0, 10);
    svg.appendChild(t);
  });

  series.forEach((s, si) => {
    const color = colorFor(s.who);
    let d = "", started = false;
    s.pts.forEach((v, i) => {
      if (v == null) { started = false; return; }
      d += (started ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " ";
      started = true;
    });
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", s.who === "TEAM" ? 3 : 2);
    svg.appendChild(path);
    s.pts.forEach((v, i) => {
      if (v == null) return;
      const c = document.createElementNS(ns, "circle");
      c.setAttribute("cx", x(i)); c.setAttribute("cy", y(v)); c.setAttribute("r", 2.6);
      c.setAttribute("fill", color);
      const ti = document.createElementNS(ns, "title");
      ti.textContent = `${s.who} — ${ps[i]}: ${isPct ? v.toFixed(1) + "%" : v.toLocaleString()}`;
      c.appendChild(ti);
      svg.appendChild(c);
    });
    const ly = M.t + 14 + si * 16;
    const lt = document.createElementNS(ns, "text");
    lt.setAttribute("x", W - M.r + 10); lt.setAttribute("y", ly);
    lt.setAttribute("fill", color); lt.setAttribute("font-size", "11");
    lt.setAttribute("font-weight", s.who === "TEAM" ? "700" : "400");
    lt.textContent = s.who;
    svg.appendChild(lt);
  });
  return svg;
}

init();
