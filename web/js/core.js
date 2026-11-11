/* Brooks IS Dashboard - core.js
   Shared pieces every tab uses: constants, sign-in/permissions, the tab switcher, date-range pickers, tables, notes, saved views, Sync.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
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

/* pickers registry: every tab loads through its range picker */
const PK = {};

function ensureLoaded(key) {
  if (PK[key] && !PK[key].loaded) {
    PK[key].loaded = true;
    PK[key].fire();
  }
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

function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function closeModal() { const m = $("#agent-modal"); if (m) m.remove(); }

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

const countFmt = (v) => (v == null ? "–" : Math.round(v).toLocaleString());

const pctFmt = (v) => (v == null ? "–" : v.toFixed(1) + "%");

const mkCard = (label, big, sub) => `<div class="rev-card"><div class="rev-card-label">${label}</div>`
  + `<div class="rev-card-amt">${big}</div><div class="rev-card-sub">${sub || ""}</div></div>`;

const escHtml = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

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
