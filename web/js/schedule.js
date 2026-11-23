/* Brooks IS Dashboard - schedule.js
   Schedule tab (IS Managers): schedule builder, templates, timesheets, hours report, adherence, time off, agent records.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

/* ---------- IS Managers: Timesheets + Agents ---------- */
const TS = { data: null, range: null, open: null };

/* ---------- hours report (Timesheets tab): regular / weekly OT / PTO per agent ----------
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
  t.innerHTML = `<thead><tr><th>Agent</th><th>Regular hours</th><th>Weekly OT</th><th>PTO</th></tr></thead>`;
  const tb = document.createElement("tbody");
  if (!d.agents.length) {
    const tr = document.createElement("tr"); tr.innerHTML = `<td colspan="4">No hours or PTO in this range.</td>`; tb.appendChild(tr);
  }
  for (const r of d.agents) {
    const tr = document.createElement("tr");
    const nm = document.createElement("td"); nm.className = "nm"; nm.textContent = r.agent; tr.appendChild(nm);
    for (const k of ["regular_h", "weekly_ot_h", "pto_h"]) { const td = document.createElement("td"); td.textContent = f(r[k]); tr.appendChild(td); }
    tb.appendChild(tr);
  }
  if (d.agents.length) {
    const tr = document.createElement("tr"); tr.className = "tot";
    tr.innerHTML = `<td class="nm">Total</td>${["regular_h", "weekly_ot_h", "pto_h"].map((k) => `<td>${f(d.totals[k])}</td>`).join("")}`;
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  box.replaceChildren(ctl, t);
  noteInto("#hr-report", "Hours worked on the time clock, per agent, for the dates picked (it opens on the last closed pay period). " +
    "Weekly OT = hours past 40 in a Sunday–Saturday week; a long day on its own is not overtime. " +
    "Weeks are always counted from their Sunday, so a range that starts " +
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

/* ---------- time off: manager queue ----------
   Pending / approved / recent requests, each with Edit; "Add time off" enters
   one for an agent; "Look up an agent" lists everything they've had so older
   requests can be changed too. Add and Edit share timeOffEditor(). */
const TOQ = { agents: [], lookup: "" };

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
  const stamp = clockStamp;
  const row = (r, buttons) => {
    const el = document.createElement("div"); el.className = "to-row";
    const decided = r.decided_at ? `${r.status} ${stamp(r.decided_at)}${r.decided_by ? " by " + r.decided_by : ""} (Mountain time)` : "";
    el.innerHTML = `<span class="st ${escHtml(r.status)}" title="${escHtml(decided)}">${escHtml(r.status)}</span><b>${escHtml(r.agent)}</b><span>${fmtD(r.start_day)}${r.end_day !== r.start_day ? " – " + fmtD(r.end_day) : ""}</span>` +
      `<span>${escHtml(toKind(r))}</span>` + (r.balance != null ? `<span class="sub">balance ${r.balance.toFixed(2)} h</span>` : "") +
      (r.note ? `<span class="sub">“${escHtml(r.note)}”</span>` : "") + (r.decision_note ? `<span class="sub">manager: ${escHtml(r.decision_note)}</span>` : "") +
      (r.entered_by ? `<span class="sub" title="Entered by a manager (Mountain time)">entered by ${escHtml(r.entered_by)} ${stamp(r.requested_at)}</span>`
        : r.requested_at ? `<span class="sub" title="When the agent sent this request (Mountain time)">requested ${stamp(r.requested_at)}</span>` : "") +
      (r.edited_by ? `<span class="sub" title="Last changed by a manager (Mountain time)">edited by ${escHtml(r.edited_by)} ${stamp(r.edited_at)}</span>` : "");
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

// when something happened, in Mountain time: "Nov 3, 2:14 PM" (the year is added when it isn't this year)
function clockStamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const o = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: CLOCK_TZ };
  if (d.getFullYear() !== new Date().getFullYear()) o.year = "numeric";
  return d.toLocaleString("en-US", o);
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
  // who published (or, for a draft, created) the week and when
  const by = (who) => (who ? ` by ${who}` : "");
  const made = d.created_at ? `Week created ${clockStamp(d.created_at)}${by(d.created_by)} (Mountain time)` : "";
  if (d.published && d.published_at) {
    const s = document.createElement("span"); s.className = "sub";
    s.textContent = `${clockStamp(d.published_at)}${by(d.published_by)}`;
    s.title = `Published ${clockStamp(d.published_at)}${by(d.published_by)} (Mountain time).${made ? " " + made + "." : ""}`;
    acts.appendChild(s);
  } else if (d.exists && d.created_at) {
    const s = document.createElement("span"); s.className = "sub";
    s.textContent = `created ${clockStamp(d.created_at)}${by(d.created_by)}`;
    s.title = made + ".";
    acts.appendChild(s);
  }
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
      const ch = cell && a.changed && a.changed[k];      // the builder sends who last set each shift; templates don't
      if (ch) el.title = `Last changed ${clockStamp(ch.at)}${ch.by ? " by " + ch.by : ""} (Mountain time)`;
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
    ["weekly_ot_h", "Weekly OT", "hours past 40 in a Sun–Sat week"],
    ["weeks_over_40", "Weeks >40h", ""],
    ["flags", "Flags", "late, no clock-in, auto clock-outs, still clocked in"]];
  const rows = d.agents.map((a) => ({ ...a, _a: a }));
  const t = simpleTable(cols, rows, { fmt: { scheduled_h: h, worked_h: h, diff_h: (v) => (v > 0 ? "+" : "") + h(v), weekly_ot_h: h, flags: (v) => v || "" },
    sortable: true, defaultSort: "agent", hl: new Set(["worked_h"]), onName: (r) => showTsDetail(r._a),
    flag: (r, k) => (k === "diff_h" && r.diff_h != null ? (r.diff_h < -0.5 ? "sc-red" : r.diff_h > 0.5 ? "sc-yellow" : "") : "") });
  $("#ts-table").replaceChildren(t);
  noteInto("#ts-table", `${d.start} – ${d.through}. Scheduled hours come from the posted schedule sheet; clocked hours from the ` +
    `agent's own clock in/out on their page (server time, shown in Mountain). ${!d.auto || d.auto.enabled ? `A punch still open ${d.grace_min} minutes after the scheduled shift end is closed ` +
      `${d.auto && d.auto.stamp === "run" ? "at that moment" : "at the shift end"} and marked “auto” (see Clock rules below).` : `Auto clock-out is off (see Clock rules below).`} Overtime is weekly only: hours past 40 in a ` +
    `Sun–Sat week (a long day on its own is not overtime). Click an agent for their days; ` +
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
    "1 h per 40 worked for under two years of service and 1.5 per 40 after, full time = 30+ h/week (the Employment field overrides the " +
    "8-week average when set). The 80 h cap starts Jan 1, 2027: balances above 80 on Dec 31 are cut to 80 that day, then accrual stops at 80; until then nobody is capped. Only hours actually worked earn PTO — the time clock from Oct 26, 2026 (a fixed punch re-figures that day); scheduled hours never do. From the week of Nov 22, 2026, at most 40 worked hours a week (Sun–Sat, the overtime week) earn PTO — no PTO on overtime; earlier weeks are left as they were. " +
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
