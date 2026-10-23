"use strict";
// Runs the real app/static/app.js inside a tiny DOM + fetch stub so the
// review-queue UI behaviour can be checked without a browser.
// Assertions print "ok"/"FAIL" lines; any failure exits non-zero for pytest.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP = path.join(__dirname, "..", "app", "static", "app.js");
let failures = 0;
function check(ok, message) {
  console.log((ok ? "ok   " : "FAIL ") + message);
  if (!ok) failures += 1;
}

const viewport = { y: 0, scrollIntoView: [], focus: [] };
const registry = new Map();

class Element {
  constructor(key) {
    this.key = key;
    this._html = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.dataset = {};
    this.style = {};
    this.onclick = null;
    this.onsubmit = null;
  }
  set innerHTML(v) { this.writes = (this.writes || 0) + 1; this._html = String(v); this._cache = {}; }
  get innerHTML() { return this._html; }
  // Parsed buttons are cached per rendered markup so that handlers wired by
  // the app stay reachable when the test queries the same selector again.
  parsed(attr) {
    if (!this._cache) this._cache = {};
    return this._cache[attr] || (this._cache[attr] = dataButtons(this._html, attr));
  }
  after() {}
  insertBefore(child, before) {
    if (child.id) registry.set('#' + child.id, child);
    (this.insertions ||= []).push({ child, before });
  }
  appendChild() {}
  append() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  addEventListener() {}
  setAttribute(name, value) { this[name] = value; }
  querySelector() { return element("child"); }
  querySelectorAll() { return []; }
  focus() { viewport.focus.push(this.key); }
  scrollIntoView(opts) { viewport.scrollIntoView.push({ key: this.key, opts }); }
}

function element(key) {
  if (!registry.has(key)) registry.set(key, new Element(key));
  return registry.get(key);
}
const camel = (attr) => attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

// Just enough markup parsing to recover buttons carrying data-* attributes,
// which is how the app wires its list and count actions.
function dataButtons(html, attr) {
  const out = [];
  const re = /<button\b([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const attrs = m[1];
    const hit = attrs.match(new RegExp('\\bdata-' + attr + '="([^"]*)"'));
    if (!hit) continue;
    const el = new Element("[data-" + attr + "]");
    el.dataset[camel(attr)] = hit[1];
    el.disabled = /\sdisabled(\s|=|$)/.test(" " + attrs);
    out.push(el);
  }
  return out;
}

const document = {
  querySelector: (sel) => sel === 'dialog[open]' ? (element('#playlist-dialog').open ? element('#playlist-dialog') : null) : element(sel),
  querySelectorAll: (sel) => {
    const m = /^\[data-([a-z-]+)\]$/.exec(sel);
    if (!m) return [];
    const found = [];
    for (const el of registry.values()) found.push(...el.parsed(m[1]));
    return found;
  },
  createElement: (tag) => new Element(tag),
};
const window = {
  scrollTo: (x, y) => { viewport.y = y; },
  get scrollY() { return viewport.y; },
};

const metadata = {
  title: "Five Found Dead", author: "Sulari Gentill", narrator: "", year: "",
  series: "", series_number: "", genre: "", publisher: "", copyright: "",
  isbn: "", asin: "", language: "", description: "", cover_url: "",
};
const provenance = Object.fromEntries(Object.keys(metadata).map((k) => [k, "Embedded"]));
const file = {
  relative: "five-found-dead/part1.m4b", duration: 28218, codec: "aac",
  sample_rate: 44100, channels: 2, profile: "LC", cover: true, warning: "",
};
const job = (id, status, title) => ({
  id, package_id: "pkg-1", status, error: "", output: "",
  events: [{ created: 1700000000, message: "Inspected" }],
  automation: { eligible: false, reasons: ["Grouping is not confirmed yet"] },
  approval_warnings: ['Edition conflicts: language <check> & runtime'],
  body: {
    metadata: { ...metadata, title },
    provenance,
    files: [file],
    embedded: { duration: 28218 },
    grouping_confirmed: false,
    lookup_error: "",
    cover_choice: "embedded",
    covers: [],
    manual_cover: null,
    candidates: [{
      title, author: "Sulari Gentill", narrator: "Katherine Littrell", duration: 28200,
      cover_url: "",
      confidence: { total: 95, signals: [{ field: "title", points: 40, maximum: 40, reason: "Exact title" }] },
    }],
  },
});
const reviewJob = job("j1", "REVIEW", "Five Found Dead");
const readyJob = job("j2", "READY", "A Ready Book");
const snapshot = { jobs: [reviewJob, readyJob], packages: [], scans: [] };

const calls = [];
let pendingPlaylists = null, pendingLookup = null;
function route(method, url) {
  if (url.startsWith('/api/playlists')) return pendingPlaylists || { playlists: [{ id: 'p1', name: 'TBR' }] };
  if (url.split('?')[0].endsWith('/library-item')) return pendingLookup || { libraryItemId: 'book-id' };
  if (url === "/api/jobs" && method === "GET") return snapshot;
  if (url === "/api/health") return { runtime: { worker_heartbeat: 0 } };
  if (url === "/api/archives") return { enabled: true, items: [{ hash: 'archive-id', name: 'Fixture', status: 'ARCHIVED', body: {} }] };
  if (/^\/api\/jobs\/[^/]+$/.test(url)) return reviewJob;
  if (/^\/api\/jobs\/j1\/select$/.test(url)) return reviewJob;
  return {};
}
async function fetch(url, opts = {}) {
  const method = opts.method || "GET";
  calls.push({ method, url, body: opts.body ? JSON.parse(opts.body) : undefined });
  const data = await route(method, url);
  return { ok: true, status: 200, json: async () => data };
}

const sandbox = {
  document, window, fetch, console,
  setTimeout: () => 0,
  setInterval: () => 0,
  // Any confirm() means an implicit prompt slipped into a normal flow.
  confirm: () => { throw new Error("unexpected confirm() in this flow"); },
};
vm.createContext(sandbox);
const source = fs.readFileSync(APP, "utf8");
vm.runInContext(source + "\n;globalThis.__hooks = { drawQueue, detail, queue, queuePollAllowed, queueFingerprint, showPlaylist, save, reviewManualFields, showArchives };", sandbox, { filename: "app.js" });
const hooks = sandbox.__hooks;
const settle = () => new Promise((r) => setTimeout(r, 5));

(async () => {
  await settle(); // let the load-time queue() finish before asserting
  await hooks.queue();
  await settle();

  // 1. Needs review is a real button carrying the correct count.
  const counts = element("#counts").innerHTML;
  check(/<button type="button" class="count count-filter" data-count-filter="REVIEW" aria-label="Needs review: 1/.test(counts),
    "needs-review count renders as an accessible button with its total");
  check(/data-count-filter="REVIEW"[^]*?<strong>1<\/strong><\/button>/.test(counts),
    "needs-review count shows the number of REVIEW jobs");
  check((counts.match(/<div class="count">/g) || []).length === 6,
    "the other six count boxes stay plain divs");
  check(counts.includes("Needs review"), "needs-review label is unchanged");

  // 2. Activating it selects the REVIEW filter and jumps to the listing.
  const countsButton = document.querySelectorAll("[data-count-filter]").find((b) => b.dataset.countFilter === "REVIEW");
  check(!!countsButton && !countsButton.disabled, "count button is enabled and focusable");
  countsButton.onclick();
  check(element("#filter").value === "REVIEW", "count button selects the REVIEW queue filter");
  check(element("#books").innerHTML.includes("Five Found Dead") && !element("#books").innerHTML.includes("A Ready Book"),
    "listing shows only REVIEW books after activating the count");
  check(viewport.scrollIntoView.some((s) => s.key === "#books"), "viewport moves to the filtered listing");

  // 3. Opening a book from the queue still starts at the top.
  viewport.y = 640;
  await hooks.detail("j1");
  check(element("#detail").innerHTML.includes('id="approve-top"'), "review detail offers a top-level Save & approve button");
  check(viewport.y === 0, "opening a book from the queue scrolls to the top on purpose");
  check(!element('#approval-warnings').hidden && element('#approval-warnings').innerHTML.includes('language &lt;check&gt; &amp; runtime'), 'GET review warnings are visible inline and escaped');
  check(element('#detail').insertions.some(i => i.child.id === 'approval-warnings' && i.before.key === '#detail .actions'), 'warning panel is next to Save & approve controls');
  reviewJob.approval_warnings = ['Updated warning after save'];
  await hooks.save();
  check(element('#approval-warnings').innerHTML.includes('Updated warning after save'), 'save refreshes inline approval warnings');

  // 4. Choosing a candidate keeps the reader near their place.
  const candidate = document.querySelectorAll("[data-candidate]")[0];
  check(!!candidate && !candidate.disabled, "candidate selection button is enabled");
  viewport.y = 1200;
  await candidate.onclick();
  check(calls.some((c) => c.method === "POST" && c.url === "/api/jobs/j1/select"), "candidate selection saves the edition");
  check(viewport.y === 1200, "scroll position is preserved after 'Use this metadata'");

  // 5. Approving stays an explicit action through the existing endpoint.
  const approveCalls = () => calls.filter((c) => c.method === "POST" && c.url === "/api/jobs/j1/approve");
  check(approveCalls().length === 0, "nothing is approved implicitly while reviewing");
  const approveTop = element("#approve-top");
  check(typeof approveTop.onclick === "function", "top Save & approve button is wired up");
  await approveTop.onclick();
  check(approveCalls().length === 1 && approveCalls()[0].body.force_now === false,
    "top Save & approve reuses the existing approve endpoint without forcing an encode");
  check(approveCalls()[0].body.force_now !== true, "no implicit immediate processing override");
  check(!element('#notice').textContent.includes('unexpected confirm'), 'Save & approve with warnings never calls confirm');
  reviewJob.approval_warnings = [];
  await hooks.detail('j1');
  check(element('#approval-warnings').hidden, 'empty warning panel is hidden');
  await element('#approve').onclick();
  check(approveCalls().length === 2, 'lower Save & approve also posts without confirmation');

  // 6. Polls compare rendered fields and preserve the viewport on real updates.
  await hooks.queue();
  viewport.y = 900;
  const writes = element('#books').writes;
  await hooks.queue(true);
  check(element('#books').writes === writes && viewport.y === 900, 'unchanged poll neither redraws nor scrolls');
  readyJob.updated = 123;
  await hooks.queue(true);
  check(element('#books').writes === writes, 'unrendered timestamp changes do not redraw');
  readyJob.status = 'PROCESSING';
  await hooks.queue(true);
  check(element('#books').writes === writes + 1 && viewport.y === 900, 'changed poll redraws and preserves scroll');
  element('#playlist-dialog').open = true;
  check(!hooks.queuePollAllowed(), 'timer skips an open playlist dialog');
  readyJob.status = 'COMPLETE';
  await hooks.queue(true);
  check(element('#books').writes === writes + 1, 'in-flight poll cannot redraw under a dialog');
  element('#playlist-dialog').open = false;
  await hooks.detail('j1');
  element('#metadata-form [name="title"]').value = 'Typing';
  check(!hooks.queuePollAllowed(), 'timer skips metadata detail');
  await hooks.queue(true);
  check(!element('#detail').hidden && element('#metadata-form [name="title"]').value === 'Typing', 'in-flight poll leaves metadata edits alone');
  await hooks.queue();
  check(viewport.y === 0, 'navigating from detail to queue still starts at the top');

  let finishPlaylists, finishLookup;
  pendingPlaylists = new Promise(resolve => { finishPlaylists = resolve; });
  pendingLookup = new Promise(resolve => { finishLookup = resolve; });
  const opening = hooks.showPlaylist(reviewJob);
  check(element('#playlist-dialog').open, 'cold-cache playlist dialog opens before requests finish');
  check(element('#playlist-status').textContent.includes('Loading playlists'), 'cold-cache dialog shows an honest loading status');
  check(calls.some(c => c.url.endsWith('/library-item')), 'book lookup starts without waiting for playlists');
  finishPlaylists({ playlists: [{ id: 'p1', name: 'TBR' }] });
  await settle();
  check(element('#playlist-status').textContent.includes('Looking up published book'), 'slow library scan has a distinct status');
  finishLookup({ libraryItemId: 'book-id' });
  await opening;
  check(element('#playlist-status').textContent === 'Book found. Choose a playlist.', 'dialog reports completion');
  pendingPlaylists = pendingLookup = null;
  await element('#playlist-retry').onclick();
  check(calls.some(c => c.url === '/api/playlists?refresh=1'), 'retry explicitly bypasses the playlist cache');
  check(calls.some(c => c.url.endsWith('/library-item?refresh=1')), 'retry explicitly bypasses the resolution cache');
  element('#playlist-close').onclick();
  check(!element('#playlist-dialog').open, 'dialog remains closable');

  const confirmations = [];
  sandbox.confirm = message => { confirmations.push(message); return false; };
  hooks.reviewManualFields({provenance: {asin: 'Manual'}, metadata: {asin: 'B0FM6GST96'}});
  check(confirmations.at(-1).includes('Keep your manual asin'), 'manual-field override confirmation remains');
  reviewJob.status = 'ALREADY_EXISTS';
  await hooks.detail('j1');
  await element('#confirm-duplicate').onclick();
  check(confirmations.at(-1).includes('existing library recording'), 'duplicate confirmation remains');
  check(!calls.some(c => c.url.endsWith('/confirm-duplicate')), 'canceling duplicate confirmation prevents action');
  await hooks.showArchives();
  await document.querySelectorAll('[data-clear]')[0].onclick();
  check(confirmations.at(-1).includes('Permanently delete'), 'archive deletion confirmation remains');
  check(!calls.some(c => c.url.endsWith('/clear')), 'canceling archive deletion prevents action');

  console.log(failures ? `\n${failures} UI check(s) failed` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
