/* Brooks IS Dashboard - team-metrics.js
   Team Metrics tab: Team Overview, Phone (Conversion / Trends / Audit / Leaked Leads), Form Conversion, Multiyear, Contract Value.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

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

function periods(gran) {
  return [...new Set(DATA[gran].map((r) => r.period))].sort().reverse();
}

function rowsFor(gran, period) {
  return DATA[gran].filter((r) => r.period === period);
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
