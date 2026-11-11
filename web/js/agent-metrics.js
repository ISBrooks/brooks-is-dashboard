/* Brooks IS Dashboard - agent-metrics.js
   Agent Metrics tab: IS #'s, Power Score, Compare Agents, Master Metric, Inbound vs Other.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

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
