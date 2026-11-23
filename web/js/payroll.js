/* Brooks IS Dashboard - payroll.js
   Payroll tab: Commission Lookup, Submit Adjustment, Payroll Rules, Commission Roster, Downloads, Lock & run payroll.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

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
      (run.week_totals ? ` · ${run.week_totals}` : "") +
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
      (run.week_totals ? ` · ${run.week_totals}` : "") +
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
