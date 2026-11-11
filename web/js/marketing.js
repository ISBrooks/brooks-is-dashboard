/* Brooks IS Dashboard - marketing.js
   Marketing tab: Spend & ROAS, Sales Data, Lead Volume, Volume By Source, and the source detail overlay.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

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

// the Revenue tab's line chart, re-used for weekly counts
function weekChartState(sel) {
  return { chart: null, hidden: new Set(), colorOf: {}, el: { chart: sel },
    axisFmt: countFmt, valFmt: countFmt, yFloor: 10, labelEvery: 2 };
}

const MB = weekChartState("#mb-chart");

const MK_PAL = ["#1e3659", "#6fa8c4", "#d98b1f", "#1e8e5a", "#7048ba", "#b3323c"];

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
MB.chanOff = new Set();

MB.srcOff = new Set();

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

function initMarketing() {
  ADMIN.ok = ME.admin;
  $("#mktg-unlock").addEventListener("click", () => adminUnlock());
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
