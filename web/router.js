/* Addresses for the dashboard's tabs.

   Every tab and sub-tab has its own address (/marketing/spend-roas,
   /team-metrics/phone/trends, ...), so a refresh stays put, Back and Forward
   move between the tabs you visited, and a link opens the same view for
   someone else (if their login is allowed to see it).

   The page is still one page: this file only translates between the address
   bar and which tab buttons are active. Showing a tab is still done by
   showTab() and the show...Sub() functions in app.js.

   To add a tab or sub-tab: give it a line here. The first value is the
   button's data-* key in index.html, the second is the word used in the
   address. Addresses get bookmarked - don't rename one without adding the old
   word to ALIASES below. The server must also know each top-level word:
   _APP_PAGES in src/isadash/webapp.py. */
"use strict";

const Router = (() => {
  const SITE = "Brooks IS Dashboard";

  // tab key -> address word, the sub-tab strip it owns, and its sub-tabs
  const TABS = {
    home:     { slug: "", title: "Home" },
    revenue:  { slug: "revenue", title: "Revenue" },
    agent:    { slug: "agent-metrics", title: "Agent Metrics", strip: "#agent-subtabs", attr: "asub",
                subs: { scorecard: "is-numbers", power: "power-score", compare: "compare-agents",
                        master: "master-metric", inbound: "inbound-vs-other" } },
    metrics:  { slug: "team-metrics", title: "Team Metrics", strip: "#metrics-subtabs", attr: "msub",
                subs: { team: "overview", phone: "phone", forms: "form-conversion",
                        multiyear: "multiyear", acv: "contract-value" },
                nested: { under: "phone", strip: "#phone-subtabs", attr: "sub",
                          subs: { conv: "conversion", trends: "trends", audit: "audit", leaked: "leaked-leads" } } },
    managers: { slug: "schedule", title: "Schedule", strip: "#managers-subtabs", attr: "gsub",
                subs: { schedule: "builder", templates: "templates", timesheets: "timesheets",
                        reports: "hours-report", adherence: "adherence", timeoff: "time-off", agents: "agents" } },
    payroll:  { slug: "payroll", title: "Payroll", strip: "#payroll-subtabs", attr: "psub",
                subs: { lookup: "commission-lookup", adjust: "submit-adjustment", rules: "rules",
                        roster: "roster", downloads: "downloads" } },
    sources:  { slug: "marketing", title: "Marketing", strip: "#mktg-subtabs", attr: "mksub",
                subs: { spend: "spend-roas", sources: "sales-data", volume: "lead-volume",
                        bysource: "volume-by-source" } },
    settings: { slug: "settings", title: "Settings", strip: "#settings-subtabs", attr: "ssub",
                subs: { agents: "agents", software: "software", users: "users" } },
  };
  // the agent page (what an agent login sees, and the /?as=Name preview)
  const ME = { slug: "my", strip: "#me-subtabs", attr: "mesub",
               subs: { numbers: "numbers", schedule: "schedule", renewal: "renewal-calculator", payroll: "payroll" } };
  // old address word -> current one, so a renamed address keeps working
  const ALIASES = {};

  const clean = (w) => ALIASES[w] || w;
  const keyFor = (subs, word) => Object.keys(subs).find((k) => subs[k] === clean(word));
  const buttons = (strip) => [...document.querySelectorAll(strip + " button")];
  const usable = (b) => b && b.style.display !== "none";
  const active = (strip) => buttons(strip).find((b) => b.classList.contains("active") && usable(b));

  function segments(path) {
    return path.split("/").filter(Boolean).map((s) => { try { return decodeURIComponent(s); } catch (e) { return s; } });
  }

  // Mark the sub-tab named in the address as the active one (if this login may
  // use it); showTab() then opens whichever sub-tab is active.
  function choose(def, word) {
    if (!def.strip || !word) return null;
    const key = keyFor(def.subs, word);
    const btn = key && buttons(def.strip).find((b) => b.dataset[def.attr] === key);
    if (!usable(btn)) return null;
    buttons(def.strip).forEach((b) => b.classList.toggle("active", b === btn));
    return key;
  }

  /* The tab an address names, with its sub-tabs made active. null if the
     address isn't a tab or this login can't open it. */
  function apply(path, allowed) {
    const seg = segments(path);
    const tab = seg.length ? Object.keys(TABS).find((t) => TABS[t].slug === clean(seg[0])) : "home";
    if (!tab || !allowed(tab)) return null;
    const def = TABS[tab];
    const sub = choose(def, seg[1]);
    if (def.nested && sub === def.nested.under) choose(def.nested, seg[2]);
    return tab;
  }

  function subPath(def) {
    if (!def.strip) return "";
    const b = active(def.strip);
    if (!b) return "";
    const key = b.dataset[def.attr];
    let out = "/" + def.subs[key];
    if (def.nested && key === def.nested.under) out += subPath(def.nested);
    return out;
  }

  function subTitle(def) {
    const b = def.strip && active(def.strip);
    if (!b) return [];
    const mine = [b.textContent.trim()];
    return def.nested && b.dataset[def.attr] === def.nested.under ? mine.concat(subTitle(def.nested)) : mine;
  }

  const onAgentPage = () => {
    const p = document.getElementById("panel-me");
    return !!p && p.classList.contains("active");
  };

  /* The address and title for what is on screen right now. */
  function current() {
    if (onAgentPage()) {
      return { path: "/" + ME.slug + subPath(ME), title: subTitle(ME).concat(SITE).join(" · ") };
    }
    const btn = document.querySelector("#tabs button.active[data-tab]");
    const tab = btn ? btn.dataset.tab : "home";
    const def = TABS[tab] || TABS.home;
    const path = def.slug ? "/" + def.slug + subPath(def) : "/";
    // "Schedule · Schedule" reads oddly: drop a sub-tab named the same as its tab
    const subs = subTitle(def).filter((t, i) => !(i === 0 && t === def.title));
    return { path, title: [def.title].concat(subs, SITE).join(" · ") };
  }

  /* Make the address bar match the screen. A new history entry is added only
     when the address actually changed (so Back steps through tabs, not clicks). */
  function sync(replace) {
    const { path, title } = current();
    document.title = title;
    if (path === location.pathname) return;
    const url = path + location.search;          // keeps ?as=Name on the agent-page preview
    if (replace) history.replaceState(null, "", url);
    else history.pushState(null, "", url);
  }

  /* Called once at load. Returns the tab to open: the one in the address if
     this login can open it, otherwise `fallback`. */
  function start(fallback, allowed, show) {
    const tab = apply(location.pathname, allowed) || fallback;
    // after any tab or sub-tab click (the buttons' own handlers run first)
    document.addEventListener("click", (e) => {
      if (e.target.closest("#tabs button[data-tab], nav.subtabs button")) setTimeout(() => sync(false), 0);
    });
    window.addEventListener("popstate", () => {
      const t = apply(location.pathname, allowed);
      if (t) Promise.resolve(show(t)).then(() => { document.title = current().title; });
    });
    return tab;
  }

  /* Agent page: pick the sub-tab named in the address before the page builds,
     and keep the address in step with later clicks. */
  function startAgentPage() {
    const seg = segments(location.pathname);
    if (clean(seg[0] || "") === ME.slug) choose(ME, seg[1]);
    document.addEventListener("click", (e) => {
      if (e.target.closest("#me-subtabs button")) setTimeout(() => sync(false), 0);
    });
    window.addEventListener("popstate", () => {
      const s = segments(location.pathname);
      const key = clean(s[0] || "") === ME.slug ? keyFor(ME.subs, s[1] || "") : "numbers";
      const btn = buttons(ME.strip).find((b) => b.dataset[ME.attr] === (key || "numbers"));
      if (usable(btn) && !btn.classList.contains("active")) btn.click();
    });
  }

  return { start, startAgentPage, sync, current, apply };
})();
