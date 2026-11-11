/* Loads the page's script files, in the order index.html lists them, into a
   bare browser-like sandbox. Catches what checking each file alone cannot:
   the same name declared in two files (the page would refuse to load), and
   something that runs at load time needing a file that comes later.

   usage: node scripts/check_web_js.js        (run by the tests and on every push) */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "web");
const index = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const files = [...index.matchAll(/<script src="\/([\w./-]+\.js)(?:\?[^"]*)?"><\/script>/g)].map((m) => m[1]);
if (files.length < 3) { console.error("found no script list in index.html"); process.exit(1); }

const noop = () => {};
// stands in for any page element: every property and call just returns it again
const el = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => "" : el), apply: () => el, construct: () => el });
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: noop, clearInterval: noop, URLSearchParams, Intl, Date, Math, JSON,
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: () => el, createElementNS: () => el,
              addEventListener: noop, documentElement: el, body: el, getElementById: () => null, title: "" },
  location: { search: "", pathname: "/", href: "" }, history: { pushState: noop, replaceState: noop },
  localStorage: { getItem: () => null, setItem: noop }, fetch: () => new Promise(noop),
  ResizeObserver: function () { return { observe: noop }; }, navigator: { userAgent: "" },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);

let failed = false;
for (const f of files) {
  const file = path.join(WEB, f);
  if (!fs.existsSync(file)) { console.error(`index.html lists ${f}, which does not exist`); failed = true; continue; }
  if (path.basename(f) === "main.js") continue;      // main.js only calls init(), which needs a real page
  try {
    vm.runInContext(fs.readFileSync(file, "utf8"), ctx, { filename: f });
  } catch (e) {
    console.error(`${f}: ${e.name}: ${e.message}`);
    failed = true;
  }
}
// every script file in web/js must be listed in index.html, or it is never loaded
for (const f of fs.readdirSync(path.join(WEB, "js"))) {
  if (f.endsWith(".js") && !files.includes("js/" + f)) { console.error(`web/js/${f} is not listed in index.html`); failed = true; }
}
if (typeof vm.runInContext("typeof init", ctx) !== "string" || vm.runInContext("typeof init", ctx) !== "function") {
  console.error("init() is not defined after loading every file"); failed = true;
}
if (failed) process.exit(1);
console.log(`${files.length} script files load cleanly in order: ${files.join(", ")}`);
