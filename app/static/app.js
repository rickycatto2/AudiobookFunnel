const $ = (s) => document.querySelector(s),
  esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
let snapshot = { jobs: [] },
  current = null;
const fields = [
  "title",
  "author",
  "narrator",
  "year",
  "series",
  "series_number",
  "genre",
  "publisher",
  "copyright",
  "isbn",
  "asin",
  "language",
  "description",
  "cover_url",
];
const labels = {
  qbit_archive_enabled: "Archive finished downloads after seeding",
  qbit_source_path: "RAW folder as qBittorrent sees it",
  qbit_archive_path: "Processed folder as qBittorrent sees it",
  archive_path: "Processed folder inside Docker",
  qbit_enabled: "Submit torrent inbox to qBittorrent", qbit_url: "qBittorrent address",
  qbit_username: "Username", qbit_password: "Password", qbit_save_path: "qBittorrent save path",
  abs_enabled: "Scan Audiobookshelf after finalization", abs_url: "Audiobookshelf address",
  abs_token: "API token", abs_library_id: "Library ID", aac_bitrate: "AAC bitrate (kbps)",
  ffmpeg_threads: "Encoding threads", heavy_start: "Heavy processing starts (hour)",
  heavy_end: "Heavy processing ends (hour)", auto_approve: "Automatically approve strong matches",
  monitor_enabled: "Watch completed downloads", google_books_api_key: "Google Books API key (optional)",
  write_description: "Write desc.txt", write_reader: "Write reader.html", asin: "ASIN", isbn: "ISBN"
};
const label = (s) => labels[s] || s.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
function notice(text) {
  $("#notice").textContent = text;
  $("#notice").hidden = false;
  setTimeout(() => ($("#notice").hidden = true), 9000);
}
async function api(path, method = "GET", body) {
  const r = await fetch("/api" + path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok)
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
async function act(fn) {
  try {
    await fn();
  } catch (e) {
    notice(e.message);
  }
}
function screen(name) {
  for (const id of ["queue", "detail", "settings", "grouping", "archives"])
    $("#" + id).hidden = id !== name;
  window.scrollTo(0, 0);
}
function duration(s) {
  return s < 60
    ? `${Math.round(s)}s`
    : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
function image(url, cls = "") {
  return url && /^https:\/\//.test(url)
    ? `<img class="${cls}" src="${esc(url)}" alt="Book cover" referrerpolicy="no-referrer" loading="lazy">`
    : "";
}
async function queue() {
  snapshot = await api("/jobs");
  screen("queue");
  drawQueue();
  const h = await api("/health");
  const heartbeat = Number(h.runtime.worker_heartbeat || 0);
  $("#worker-status").textContent = heartbeat
    ? "Worker connected · " + new Date(heartbeat * 1000).toLocaleTimeString()
    : "Waiting for worker";
  const errors = Object.entries(h.runtime).filter(
    ([k, v]) => k.endsWith("_error") && v,
  );
  if (errors.length)
    notice(errors.map(([k, v]) => label(k) + ": " + v).join(" · "));
}
function drawQueue() {
  const states = ["REVIEW", "READY", "PROCESSING", "COMPLETE", "ALREADY_EXISTS", "DUPLICATE_CONFIRMED", "ERROR"];
  $("#counts").innerHTML = states
    .map(
      (s) =>
        `<div class="count"><span>${{ REVIEW: "Needs review", READY: "Ready / scheduled", PROCESSING: "Processing", COMPLETE: "In your library", ALREADY_EXISTS: "Already exists", DUPLICATE_CONFIRMED: "Confirmed duplicates", ERROR: "Needs attention" }[s]}</span><strong>${snapshot.jobs.filter((j) => j.status === s).length}</strong></div>`,
    )
    .join("");
  const filter = $("#filter").value;
  const jobs = snapshot.jobs.filter((j) => (filter ? j.status === filter : j.status !== "DISMISSED"));
  $("#books").innerHTML = jobs.length
    ? jobs
        .map(
          (j) =>
            `<article class="book"><div class="info"><h3>${esc(j.body.metadata.title || "Untitled book")}</h3><span>${esc(j.body.metadata.author || "Author needed")}</span><p class="muted">${j.body.files.length} file(s) · ${duration(j.body.embedded.duration)} · ${esc(j.body.metadata.narrator || "Narrator unknown")}</p></div><span class="badge ${j.status}">${j.status === "ALREADY_EXISTS" ? "Already exists" : j.status}</span><button data-open="${j.id}">${j.status === "COMPLETE" ? "View book" : "Open review"}</button></article>`,
        )
        .join("")
    : `<div class="empty"><h2>${filter ? "No books in this state" : "Your next listen starts here"}</h2><p>Set your folders in Settings, then check completed downloads. Each source package becomes one or more book jobs for inspection and review.</p><button id="empty-settings">Open Settings</button></div>`;
  $("#packages").innerHTML = snapshot.packages
    .filter((p) => filter === "DISMISSED" ? p.status === "DISMISSED" : p.status !== "INSPECTED" && p.status !== "DISMISSED")
    .map(
      (p) =>
        `<div class="${p.error ? "error" : "panel"}"><strong>${esc(p.status)}</strong> · ${esc(p.source)}${p.error ? `<p>${esc(p.error)}</p>${p.status === "DISMISSED" ? `<button data-package-action="restore" data-package-id="${p.id}">Restore error</button>` : `<button data-package-retry="${p.id}">Retry inspection</button><button data-package-action="dismiss" data-package-id="${p.id}">Dismiss error</button>`}` : ""}</div>`,
    )
    .join("");
  $("#scans").innerHTML = snapshot.scans.length
    ? `<details><summary>Audiobookshelf scan requests</summary>${snapshot.scans.map((s) => `<p>${esc(s.status)} ${esc(s.error || "Scan request accepted")}</p>`).join("")}</details>`
    : "";
  document
    .querySelectorAll("[data-open]")
    .forEach((b) => (b.onclick = () => act(() => detail(b.dataset.open))));
  document.querySelectorAll("[data-package-retry]").forEach(
    (b) =>
      (b.onclick = () =>
        act(async () => {
          await api(`/packages/${b.dataset.packageRetry}/retry`, "POST", {});
          await queue();
        })),
  );
  document.querySelectorAll("[data-package-action]").forEach(button => {
    button.onclick = () => act(async () => {
      await api(`/packages/${button.dataset.packageId}/${button.dataset.packageAction}`, "POST", {});
      notice("Queue updated. No files changed.");
      await queue();
    });
  });
  if ($("#empty-settings"))
    $("#empty-settings").onclick = () => act(showSettings);
}
async function detail(id) {
  current = await api("/jobs/" + id);
  const j = current,
    b = j.body,
    m = b.metadata,
    editable = ["REVIEW", "ERROR"].includes(j.status);
  screen("detail");
  $("#detail").innerHTML =
    `<button id="back">← Book queue</button><div class="heading"><div><p class="eyebrow">BOOK REVIEW</p><h1>${esc(m.title)}</h1><span class="badge ${j.status}">${j.status === "ALREADY_EXISTS" ? "Already exists" : j.status}</span></div><button id="group" ${editable ? "" : "disabled"}>Review package grouping</button></div>${j.error ? `<div class="error">${esc(j.error)}</div>` : ""}${b.lookup_error ? `<div class="warning">${esc(b.lookup_error)}</div>` : ""}${j.status === "ERROR" && b.publication ? '<button id="recover">Retry unchanged publication</button>' : ""}${j.output ? `<p class="preview">Published to ${esc(j.output)}</p>` : ""}<div class="split"><div><div class="panel"><h2>Source audio</h2><p>${duration(b.embedded.duration)} · ${mediaMode(b.files)}</p>${b.files.map((f, i) => `<div class="file"><strong>${i + 1}.</strong> ${esc(f.relative)}<br><span class="muted">${duration(f.duration)} · ${esc(f.codec)}</span>${f.warning ? `<div class="warning">${esc(f.warning)}</div>` : ""}</div>`).join("")}<p><label><input id="group-confirm" type="checkbox" ${b.grouping_confirmed ? "checked" : ""} ${editable ? "" : "disabled"}>These files are one book, in the correct order</label></p></div><div class="panel"><h2>Find the right edition</h2><label>Metadata source<select id="provider"><option>Audible</option><option>Google Books</option><option>Open Library</option></select></label><label>Title<input id="query" value="${esc(m.title)}"></label><label>Author<input id="search-author" value="${esc(m.author)}"></label><label>ASIN or Audible URL (optional)<input id="asin" placeholder="B0… or https://www.audible.com/pd/…"></label><div class="actions"><button id="search" ${editable ? "" : "disabled"}>Search</button><button id="embedded" ${editable ? "" : "disabled"}>Use existing metadata</button></div><div id="candidates">${b.candidates.map((c, i) => `<div class="candidate">${image(c.cover_url)}<h3>${esc(c.title)}</h3><p>${esc(c.author)}<br><small>${esc(c.narrator || "Narrator unavailable")} · ${c.duration ? duration(c.duration) : "Print-book metadata"}</small></p><strong>${c.confidence.total}% evidence score</strong><details><summary>Why this score?</summary><div class="signals">${c.confidence.signals.map((s) => `${esc(label(s.field))}: ${s.points}/${s.maximum} — ${esc(s.reason)}`).join("<br>")}</div></details><button data-candidate="${i}" ${editable ? "" : "disabled"}>Use this metadata</button></div>`).join("") || '<p class="muted">Search for candidates, paste an ASIN, or enter metadata yourself.</p>'}</div></div></div><div><div class="panel"><h2>Final metadata</h2><form id="metadata-form"><div class="field-grid">${fields.map((f) => `<label class="${["description", "cover_url"].includes(f) ? "wide" : ""}">${esc(label(f))}${f === "description" ? `<textarea name="${f}" ${editable ? "" : "disabled"}>${esc(m[f])}</textarea>` : `<input name="${f}" value="${esc(m[f])}" ${editable ? "" : "disabled"}>`}<small>${esc(b.provenance[f] || "Not set")}</small></label>`).join("")}</div><h3>Cover selection</h3><div class="cover-choice"><label>${b.files[0].cover ? `<img src="/api/jobs/${j.id}/embedded-cover" alt="Embedded artwork">` : ""}<input type="radio" name="cover" value="embedded" ${b.cover_choice === "embedded" ? "checked" : ""}>Embedded artwork</label><label>${image(m.cover_url)}<input type="radio" name="cover" value="provider" ${b.cover_choice === "provider" ? "checked" : ""}>Metadata cover</label><label><input type="radio" name="cover" value="none" ${b.cover_choice === "none" ? "checked" : ""}>No cover</label>${b.covers.map((p, i) => `<label><img src="/api/jobs/${j.id}/cover/${i}" alt="Local cover ${i + 1}"><input type="radio" name="cover" value="local:${i}" ${b.cover_choice === `local:${i}` ? "checked" : ""}>Local ${i + 1}</label>`).join("")}</div><div class="actions"><button type="button" id="preview">Preview final name</button><button type="submit" ${editable ? "" : "disabled"}>Save edits</button></div><p id="name-preview" class="preview" hidden></p></form><div class="actions">${editable ? '<button class="primary" id="approve">Save & approve</button><button id="now">Process now</button>' : j.status === "READY" ? '<button id="unqueue">Return to review</button>' : ""}</div><small>Approve follows the processing window. Process now overrides it for this book.</small></div><details class="panel"><summary>Activity history</summary>${j.events.map((e) => `<p><small>${new Date(e.created * 1000).toLocaleString()}</small><br>${esc(e.message)}</p>`).join("")}</details></div></div>`;
  const manual = document.createElement("div");
  manual.className = "panel";
  manual.innerHTML = `<h3>Add your own cover</h3><p>Use a direct image URL or upload a JPEG, PNG, or WebP image (up to 15 MB). The saved preview is the image that will be embedded.</p>${b.manual_cover ? `<label><img style="max-width:150px;max-height:180px" src="/api/jobs/${id}/manual-cover?v=${esc(b.manual_cover.filename)}" alt="Your saved cover"><input type="radio" name="cover" value="manual" ${b.cover_choice === "manual" ? "checked" : ""} ${editable ? "" : "disabled"}>Use my cover</label><p class="muted">${esc(b.provenance.cover || "Manual cover")}</p>` : ""}<label>Image URL<input id="manual-cover-url" type="url" placeholder="https://…/cover.jpg" ${editable ? "" : "disabled"}></label><button type="button" id="import-cover-url" ${editable ? "" : "disabled"}>Import cover from URL</button><label>Upload from computer<input id="manual-cover-file" type="file" accept="image/jpeg,image/png,image/webp" ${editable ? "" : "disabled"}></label><button type="button" id="upload-cover" ${editable ? "" : "disabled"}>Upload and use cover</button>`;
  $("#metadata-form .cover-choice").after(manual);
  async function importManual(payload) {
    await save();
    await api(`/jobs/${id}/manual-cover`, "POST", payload);
    notice("Cover saved and selected. Review the preview before approving.");
    await detail(id);
  }
  $("#import-cover-url").onclick = () => act(async () => {
    const url = $("#manual-cover-url").value.trim();
    if (!url) throw Error("Paste a direct image URL first");
    await importManual({url});
  });
  $("#upload-cover").onclick = () => act(async () => {
    const file = $("#manual-cover-file").files[0];
    if (!file) throw Error("Choose an image from your computer first");
    if (file.size > 15 * 1024 * 1024) throw Error("Cover exceeds 15 MB");
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(",")[1]);
      reader.onerror = () => reject(Error("Could not read this image"));
      reader.readAsDataURL(file);
    });
    await importManual({image: data});
  });
  const actions = document.createElement("div");
  actions.className = "panel";
  if (j.status === "REVIEW") {
    actions.innerHTML = `<h3>Automatic match check</h3><p>${j.automation.reasons.map(esc).join("<br>")}</p><button id="auto-match" ${j.automation.eligible ? "" : "disabled"}>Auto-match & queue</button><p class="muted">Uses the best unambiguous match and follows your processing schedule. Confirm grouping and save edits first.</p>`;
  } else if (["ALREADY_EXISTS", "DUPLICATE_CONFIRMED"].includes(j.status)) {
    actions.innerHTML = `<h3>Already exists in the library</h3><p>The destination folder is occupied. This may be a duplicate or a different edition with the same name. No files were overwritten or deleted.</p><button id="review-existing">Review metadata / naming</button>${j.status === "ALREADY_EXISTS" ? '<button id="confirm-duplicate">Keep library copy; allow download cleanup</button>' : '<p>Duplicate confirmed. Waiting for all books and seeding before archiving.</p>'}`;
  } else if (["ERROR", "DISMISSED"].includes(j.status)) {
    actions.innerHTML = `<button id="dismiss-error">${j.status === "ERROR" ? "Dismiss error" : "Restore error"}</button><p class="muted">Hides the stale error from the active queue. No files are deleted. Dismissed errors can be restored from the queue filter.</p>`;
  }
  $("#detail").insertBefore(actions, $("#detail .split"));
  if ($("#confirm-duplicate")) $("#confirm-duplicate").onclick = () => act(async () => {
    if (!confirm("Have you checked that the existing library recording is the one you want to keep? This allows the download to be archived after seeding. Deleting the archive remains a separate action.")) return;
    await api(`/jobs/${id}/confirm-duplicate`, "POST", {confirmed: true});
    await detail(id);
  });
  if ($("#review-existing")) $("#review-existing").onclick = () => act(async () => {
    await api(`/jobs/${id}/review-existing`, "POST", {});
    await detail(id);
  });
  if ($("#auto-match")) $("#auto-match").onclick = () => act(async () => {
    await save();
    await api(`/jobs/${id}/auto-match`, "POST", {});
    notice("Strong match queued for processing");
    await queue();
  });
  if ($("#dismiss-error")) $("#dismiss-error").onclick = () => act(async () => {
    await api(`/jobs/${id}/${j.status === "ERROR" ? "dismiss" : "restore"}`, "POST", {});
    notice("Queue updated. No files changed.");
    await queue();
  });
  if ($("#recover"))
    $("#recover").onclick = () =>
      act(async () => {
        await api(`/jobs/${id}/recover`, "POST", {});
        await queue();
      });
  $("#back").onclick = () => act(queue);
  $("#group").onclick = () => act(() => grouping(j.package_id));
  $("#metadata-form").onsubmit = (e) => {
    e.preventDefault();
    act(async () => {
      await save();
      notice("Edits saved");
      await detail(id);
    });
  };
  $("#search").onclick = () =>
    act(async () => {
      const button = $("#search");
      button.disabled = true;
      try {
        await save();
        await api(`/jobs/${id}/search`, "POST", {
          provider: $("#provider").value,
          query: $("#query").value,
          author: $("#search-author").value,
          asin: $("#asin").value,
        });
        await detail(id);
      } finally {
        button.disabled = false;
      }
    });
  $("#embedded").onclick = () =>
    act(async () => {
      await api(`/jobs/${id}/embedded`, "POST", {});
      await detail(id);
    });
  document.querySelectorAll("[data-candidate]").forEach(
    (btn) =>
      (btn.onclick = () =>
        act(async () => {
          await save();
          await api(`/jobs/${id}/select`, "POST", {
            index: Number(btn.dataset.candidate),
          });
          await detail(id);
        })),
  );
  $("#preview").onclick = () =>
    act(async () => {
      const p = await api("/preview-name", "POST", readMetadata());
      $("#name-preview").textContent = p.path;
      $("#name-preview").hidden = false;
    });
  if ($("#approve")) $("#approve").onclick = () => act(() => approve(false));
  if ($("#now")) $("#now").onclick = () => act(() => approve(true));
  if ($("#unqueue"))
    $("#unqueue").onclick = () =>
      act(async () => {
        await api(`/jobs/${id}/review`, "POST", {});
        await detail(id);
      });
}
function mediaMode(files) {
  return files.every((f) => f.codec === "aac") &&
    new Set(files.map((f) => [f.sample_rate, f.channels, f.profile].join("|")))
      .size === 1
    ? "AAC · stream copy planned"
    : "AAC conversion · processing window applies";
}
function readMetadata() {
  return Object.fromEntries(
    fields.map((f) => [f, $(`#metadata-form [name="${f}"]`).value]),
  );
}
async function save() {
  await api("/jobs/" + current.id, "PUT", {
    metadata: readMetadata(),
    cover_choice: $("#metadata-form [name=cover]:checked")?.value || "none",
    grouping_confirmed: $("#group-confirm").checked,
  });
}
async function approve(force) {
  await save();
  await api(`/jobs/${current.id}/approve`, "POST", { force_now: force });
  notice(
    force
      ? "Queued for processing now"
      : "Approved; the worker will follow your schedule",
  );
  await queue();
}
async function grouping(id) {
  snapshot = await api("/jobs");
  const jobs = snapshot.jobs.filter((j) => j.package_id === id);
  if (jobs.some((j) => !["REVIEW", "ERROR"].includes(j.status)))
    throw Error(
      "Return all queued books in this package to review before regrouping. Completed/processing packages cannot be regrouped.",
    );
  const excluded = await api(`/packages/${id}/excluded`);
  const rows = jobs
    .flatMap((j, i) =>
      j.body.files.map((f, n) => ({ f, group: i + 1, order: n + 1 })),
    )
    .concat(excluded.map((f, i) => ({ f, group: 0, order: i + 1 })));
  screen("grouping");
  $("#grouping").innerHTML =
    `<button id="group-back">← Queue</button><h1>One package. The right books.</h1><p>Give files the same group number to combine them. Use 0 to explicitly exclude samples or extras. Order controls the final audio sequence.</p><div class="warning">Saving grouping resets metadata edits for this package. Search and review each new book afterward.</div><div class="panel"><div class="group-row"><strong>Source file</strong><strong>Book group</strong><strong>Order</strong></div>${rows.map((r, i) => `<div class="group-row" data-row="${i}"><span class="path">${esc(r.f.relative)}</span><input aria-label="Book group for ${esc(r.f.relative)}" type="number" min="0" value="${r.group}" class="group-number"><input aria-label="Order for ${esc(r.f.relative)}" type="number" min="1" value="${r.order}" class="group-order"></div>`).join("")}<h3>Group titles</h3><p class="muted">One line per group: number = title. Add lines for any new groups.</p><textarea id="group-titles">${esc(jobs.map((j, i) => `${i + 1} = ${j.body.metadata.title}`).join("\n"))}</textarea><div class="actions"><button id="save-groups" class="primary">Confirm grouping & order</button></div></div>`;
  $("#group-back").onclick = () => act(queue);
  $("#save-groups").onclick = () =>
    act(async () => {
      const groups = new Map(),
        excluded = [];
      document.querySelectorAll("[data-row]").forEach((el) => {
        const n = Number(el.querySelector(".group-number").value),
          order = Number(el.querySelector(".group-order").value);
        if (
          !Number.isInteger(n) ||
          n < 0 ||
          !Number.isInteger(order) ||
          order < 1
        )
          throw Error("Use whole group and order numbers");
        const p = rows[Number(el.dataset.row)].f.path;
        if (n === 0) excluded.push(p);
        else {
          if (!groups.has(n)) groups.set(n, []);
          groups.get(n).push({ p, order });
        }
      });
      const titles = Object.fromEntries(
        $("#group-titles")
          .value.split("\n")
          .map((line) => {
            const i = line.indexOf("=");
            return [line.slice(0, i).trim(), line.slice(i + 1).trim()];
          }),
      );
      const payload = [...groups].map(([n, items]) => {
        if (!titles[n]) throw Error(`Enter a title for group ${n}`);
        if (new Set(items.map((x) => x.order)).size !== items.length)
          throw Error(`Group ${n} has duplicate order numbers`);
        return {
          title: titles[n],
          files: items.sort((a, b) => a.order - b.order).map((x) => x.p),
        };
      });
      await api(`/packages/${id}/regroup`, "POST", {
        groups: payload,
        excluded,
      });
      notice("Grouping saved. Review metadata for each book.");
      await queue();
    });
}
const settingGroups = [
  [
    "Folders & discovery",
    "Only completed downloads belong in the source folder. Monitoring starts disabled so you can review your paths first.",
    [
      "source_path",
      "work_path",
      "library_path",
      "torrent_path",
      "monitor_enabled",
    ],
  ],
  [
    "Metadata & confidence",
    "Exact title and author with a very close runtime can qualify without optional tags. Conflicts and competing editions still require review.",
    [
      "auto_approve",
      "confidence_threshold",
      "confidence_margin",
      "audible_region",
      "ignored_title_terms",
      "google_books_api_key",
    ],
  ],
  [
    "Final naming",
    "Use {title}, {author}, {series}, {series_number}, {year}, {year_prefix}, {year_suffix}, {series_suffix}. Empty series folders are omitted.",
    ["folder_template", "file_template", "write_description", "write_reader"],
  ],
  [
    "Processing schedule",
    "Heavy encodes start within this local-hour window; an active encode finishes. Equal start/end means all day. AAC remuxing can run anytime.",
    ["timezone", "heavy_start", "heavy_end", "aac_bitrate", "ffmpeg_threads"],
  ],
  [
    "qBittorrent",
    "Optional torrent inbox submission. Save path is the path qBittorrent sees. Password remains unchanged when left blank.",
    [
      "qbit_enabled",
      "qbit_url",
      "qbit_username",
      "qbit_password",
      "qbit_save_path",
    ],
  ],
  [
    "Finished download archive",
    "Moves are performed by qBittorrent after its total seeding-time or ratio limit is met and all books are resolved. RAW and processed paths must match the Docker SOURCE_PATH and ARCHIVE_PATH mounts. Inactive-time-only limits wait for manual attention. Turn off Automatic Torrent Management for torrents you want archived. Clearing an archive always requires a separate confirmation.",
    ["qbit_archive_enabled", "qbit_source_path", "qbit_archive_path", "archive_path"],
  ],
  [
    "Audiobookshelf",
    "Scan after a finalized batch. Failed scan requests retry independently. Token remains unchanged when left blank.",
    ["abs_enabled", "abs_url", "abs_token", "abs_library_id"],
  ],
];
async function showSettings() {
  const { values: v, roots } = await api("/settings");
  screen("settings");
  $("#settings-form").innerHTML =
    settingGroups
      .map(
        ([title, help, keys]) =>
          `<div class="panel setting-group"><h2>${title}</h2><p class="muted">${help}</p><div class="field-grid">${keys.map((k) => (Array.isArray(v[k]) ? `<label>${label(k)}<textarea name="${k}" rows="7">${esc(v[k].join("\n"))}</textarea><small>One literal phrase per line. Ignored in brackets or at title edges for searching and matching. Original titles are preserved. Leave empty to disable.</small></label>` : typeof v[k] === "boolean" ? `<label><input type="checkbox" name="${k}" ${v[k] ? "checked" : ""}>${label(k)}</label>` : `<label>${label(k)}<input name="${k}" type="${["qbit_password", "abs_token", "google_books_api_key"].includes(k) ? "password" : typeof v[k] === "number" ? "number" : "text"}" value="${esc(v[k])}" ${v[k + "_configured"] ? 'placeholder="Saved — leave blank to keep"' : ""}>${k.endsWith("_path") && roots[k.replace("_path", "").replace("torrent", "torrents")] ? `<small>Mounted root: ${esc(roots[k.replace("_path", "").replace("torrent", "torrents")])}</small>` : ""}</label>`)).join("")}</div></div>`,
      )
      .join("") +
    '<button class="primary" type="submit">Save Settings</button><button type="button" id="qbit-test">Test saved qBittorrent connection</button>';
  $("#qbit-test").onclick = () => act(async () => notice((await api("/qbit/test", "POST", {})).message));
  $("#settings-form").onsubmit = (e) => {
    e.preventDefault();
    act(async () => {
      const values = {};
      for (const input of $("#settings-form").querySelectorAll("input, textarea"))
        values[input.name] =
          input.name === "ignored_title_terms"
            ? input.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
            : input.type === "checkbox"
            ? input.checked
            : input.type === "number"
              ? Number(input.value)
              : input.value;
      await api("/settings", "PUT", values);
      notice("Settings saved");
    });
  };
}
$("#queue-nav").onclick = () => act(queue);
$("#settings-nav").onclick = () => act(showSettings);
$("#refresh").onclick = () => act(queue);
$("#filter").onchange = drawQueue;
$("#discover").onclick = () =>
  act(async () => {
    await api("/discover", "POST", {});
    notice("Completed sources queued for inspection");
    await queue();
  });
act(queue);
setInterval(() => {
  if (!$("#queue").hidden) act(queue);
}, 15000);

async function showArchives() {
  const data = await api("/archives");
  screen("archives");
  const names = {WAITING: "Waiting for seeding", BLOCKED: "Needs attention", MOVING: "Moving / verifying", ARCHIVED: "Ready to clear", DELETE_REQUESTED: "Clear requested", DELETING: "Clearing / verifying", CLEARED: "Cleared"};
  $("#archive-list").innerHTML = `<p>${data.enabled ? "Checks run about once a minute when the worker is free. Clearing deletes only this archived torrent's files and its qBittorrent entry." : "Archive integration is disabled. Configure it in Settings to begin."}</p>` + data.items.map(a => `<article class="panel"><h3>${esc(a.name)}</h3><strong>${esc(names[a.status] || a.status)}</strong>${a.body.destination ? `<p>${esc(a.body.destination)}</p>` : ""}${a.error ? `<p class="warning">${esc(a.error)}</p>` : ""}${a.status === "ARCHIVED" && !a.error ? `<button data-clear="${esc(a.hash)}" ${data.enabled ? "" : "disabled"}>Clear archived download</button>` : ""}</article>`).join("");
  document.querySelectorAll("[data-clear]").forEach(button => button.onclick = () => act(async () => {
    const item = data.items.find(a => a.hash === button.dataset.clear);
    if (!confirm(`Permanently delete the archived download "${item.name}" and remove its qBittorrent entry? Your Audiobookshelf copy will be kept.`)) return;
    await api(`/archives/${item.hash}/clear`, "POST", {confirmed: true});
    await showArchives();
  }));
}
$("#archives-nav").onclick = () => act(showArchives);
$("#archives-refresh").onclick = () => act(showArchives);
