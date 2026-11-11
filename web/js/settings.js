/* Brooks IS Dashboard - settings.js
   Settings tab: agent roster, tiers and Power Score editors, Software Management, Users.

   One of the page's script files (see index.html for the load order). They
   share one global scope, exactly as when this was a single app.js: a function
   or constant defined in any file can be used from any other. */
"use strict";

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
