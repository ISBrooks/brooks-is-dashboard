/* Brooks IS Dashboard - agent-page.js
   The agent page (what an agent login sees, and the /?as=Name preview): My Numbers, Schedule, clock, PTO, time off, Renewal Calculator, Payroll.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

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
