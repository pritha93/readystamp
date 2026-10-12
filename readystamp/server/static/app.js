"use strict";
// ReadyStamp Lite front end. Plain JS, no build step; talks only to the local server.

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const ZONES = ["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"];
const ZONE_NAMES = {
  TL: "Top left", TC: "Top centre", TR: "Top right", ML: "Middle left", MC: "Middle centre",
  MR: "Middle right", BL: "Bottom left", BC: "Bottom centre", BR: "Bottom right",
};
const STYLE_KEYS = ["font", "size_pt", "bold", "color", "knockout", "page_rule"];
const SCREENS = ["load", "review", "number", "stamp", "output", "run"];

const S = {
  screen: "load",
  production: null,   // GET /api/production
  view: null,         // settings view from GET/PUT /api/settings
  settings: null,     // local, possibly unsaved, settings
  edits: 0,           // bumps on every local edit, so stale server replies don't overwrite newer edits
  zone: "BR",
  preview: { doc: 1, page: 1 },
  previewSeq: 0,
  pending: [],        // loose files waiting to upload: {file, path}
  jobId: null,
  job: null,
  saveTimer: null,
  saving: null,
};

class UserError extends Error {
  constructor(messages) { super(messages.join(" ")); this.messages = messages; }
}

async function api(method, url, body, allow = []) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch {
    throw new UserError(["ReadyStamp is not responding. Check that the readystamp window is still running."]);
  }
  const data = (res.headers.get("content-type") || "").includes("json") ? await res.json() : null;
  if (!res.ok && !allow.includes(res.status)) {
    throw new UserError((data && data.messages) || [`The request failed (${res.status}).`]);
  }
  return data;
}

function showMessages(messages, ok = false) {
  const box = $("#messages");
  box.className = "messages" + (ok ? " ok" : "");
  box.replaceChildren();
  if (!messages || !messages.length) { box.hidden = true; return; }
  if (messages.length === 1) box.textContent = messages[0];
  else {
    const ul = document.createElement("ul");
    for (const m of messages) ul.append(el("li", m));
    box.append(ul);
  }
  box.hidden = false;
}

function report(err) {
  if (err instanceof UserError) showMessages(err.messages);
  else { console.error(err); showMessages([`Something went wrong: ${err.message || err}`]); }
}

function el(tag, text, attrs = {}) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  return e;
}

function list(target, items) {
  const ul = typeof target === "string" ? $(target) : target;
  ul.replaceChildren(...(items || []).map((m) => el("li", m)));
}

function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((o, k) => o[k], obj)[last] = value;
}

function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

const naturalCompare = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }).compare;

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

function fieldErrors() { return (S.view && S.view.field_errors) || {}; }

function unlocked() {
  const p = S.production, v = S.view;
  const review = !!p;
  const number = review && !p.has_errors && p.documents.length > 0;
  const stamp = number && !!v && v.numbering.problems.length === 0 && !hasFieldErrors("numbering");
  const output = stamp && Object.keys(v.zones).length === 0 && !hasFieldErrors("zones") && !hasFieldErrors("margin");
  const run = (output && !!v.preflight && v.preflight.errors.length === 0 && Object.keys(fieldErrors()).length === 0) || !!S.jobId;
  return { load: true, review, number, stamp, output, run };
}

function hasFieldErrors(prefix) { return Object.keys(fieldErrors()).some((k) => k === prefix || k.startsWith(prefix + ".")); }

function renderSteps() {
  const open = unlocked();
  const current = SCREENS.indexOf(S.screen);
  for (const btn of $$(".step")) {
    const name = btn.dataset.screen;
    btn.disabled = !open[name];
    btn.classList.toggle("active", name === S.screen);
    btn.classList.toggle("done", SCREENS.indexOf(name) < current && open[name]);
  }
  const p = S.production;
  $("#prod-summary").textContent = p
    ? `${p.source_kind === "opt" ? "OPT production" : "Loose files"} · ${p.totals.documents} documents · ${p.totals.pages} pages`
    : "";
}

function go(screen) {
  if (!unlocked()[screen]) return;
  S.screen = screen;
  showMessages([]);
  for (const name of SCREENS) $(`#screen-${name}`).hidden = name !== screen;
  renderSteps();
  ({ review: renderReview, number: renderNumber, stamp: enterStamp, output: enterOutput, run: renderRun }[screen] || (() => {}))();
}

// ---------------------------------------------------------------------------
// Settings: local edits, debounced save, field errors
// ---------------------------------------------------------------------------

function editSetting(path, value) {
  setPath(S.settings, path, value);
  S.edits += 1;
  scheduleSave();
}

function scheduleSave() {
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(saveSettings, 300);
}

async function flushSave() {
  if (S.saveTimer) { clearTimeout(S.saveTimer); S.saveTimer = null; await saveSettings(); }
  if (S.saving) await S.saving;
}

async function saveSettings() {
  S.saveTimer = null;
  const edits = S.edits;
  const numberingBefore = JSON.stringify(S.view && S.view.settings.numbering);
  const task = (async () => {
    const view = await api("PUT", "/api/settings", S.settings, [422]);
    S.view = view;
    if (view.ok && edits === S.edits) S.settings = structuredClone(view.settings);
    if (view.ok && JSON.stringify(view.settings.numbering) !== numberingBefore && S.production) {
      S.production = await api("GET", "/api/production");
    }
  })();
  S.saving = task;
  try { await task; } catch (err) { report(err); } finally { if (S.saving === task) S.saving = null; }
  renderSettingsDependents();
}

function renderSettingsDependents() {
  renderSteps();
  renderFieldErrors();
  if (S.screen === "number") renderNumber(false);
  if (S.screen === "stamp") { renderZoneGrid(); renderZoneErrors(); schedulePreview(); }
  if (S.screen === "output") renderOutput(false);
  if (S.screen === "run") renderRun();
  const general = Object.entries(fieldErrors()).filter(([k]) => !document.querySelector(`[data-setting="${k}"]`) && !k.startsWith("zones."));
  if (general.length) showMessages(general.flatMap(([, m]) => m));
  else if (!$("#messages").classList.contains("ok") && S.view && S.view.ok) showMessages([]);
}

function renderFieldErrors() {
  const errors = fieldErrors();
  for (const input of $$("[data-setting]")) {
    const msgs = errors[input.dataset.setting] || [];
    input.classList.toggle("invalid", msgs.length > 0);
    const label = input.closest(".field, .check");
    let note = label && label.querySelector(".error");
    if (msgs.length && label) {
      if (!note) { note = el("span", "", { class: "error" }); label.append(note); }
      note.textContent = msgs.join(" ");
    } else if (note) note.remove();
  }
}

function fillSettingInputs() {
  for (const input of $$("[data-setting]")) {
    if (input === document.activeElement) continue;
    const value = getPath(S.settings, input.dataset.setting);
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value ?? "";
  }
}

function readInput(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.type === "number") return input.value === "" ? input.value : Number(input.value);
  if (input.dataset.setting === "output.output_dir") return input.value.trim() || null;
  return input.value;
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

function confirmReplace() {
  return !S.production || confirm("Loading replaces the documents you have loaded now. Continue?");
}

async function afterLoad(production) {
  S.production = production;
  S.preview = { doc: 1, page: 1 };
  S.jobId = null;
  S.job = null;
  S.view = await api("GET", "/api/settings");
  S.settings = structuredClone(S.view.settings);
  go("review");
}

function addPending(items) {
  const seen = new Set(S.pending.map((p) => p.path));
  for (const item of items) if (!seen.has(item.path)) { S.pending.push(item); seen.add(item.path); }
  renderPending();
}

function renderPending() {
  $("#pending").hidden = S.pending.length === 0;
  $("#pending-count").textContent = `${S.pending.length} file${S.pending.length === 1 ? "" : "s"} ready to load`;
  list("#pending-list", S.pending.slice().sort((a, b) => naturalCompare(a.path, b.path)).map((p) => p.path));
}

async function filesFromDrop(dataTransfer) {
  // Entries must be taken synchronously, before the first await.
  const entries = [...dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dataTransfer.files].map((f) => ({ file: f, path: f.name }));
  const out = [];
  async function walk(entry, prefix) {
    if (entry.name.startsWith(".")) return;
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      }
    }
  }
  for (const entry of entries) await walk(entry, "");
  return out;
}

async function loadLoose() {
  if (!S.pending.length || !confirmReplace()) return;
  const form = new FormData();
  for (const { file, path } of S.pending) {
    form.append("files", file, file.name);
    form.append("paths", path);
  }
  const btn = $("#load-loose");
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const production = await api("POST", "/api/load/loose", form);
    S.pending = [];
    renderPending();
    await afterLoad(production);
  } catch (err) { report(err); } finally { btn.disabled = false; btn.textContent = "Load these files"; }
}

async function datChanged() {
  const path = $("#dat-path").value.trim();
  $("#dat-options").hidden = !path;
  $("#dat-info").textContent = "";
  if (!path) return;
  try {
    const info = await api("GET", `/api/dat/columns?${new URLSearchParams({
      path, delimiter: $("#dat-delimiter").value, qualifier: $("#dat-qualifier").value })}`);
    const select = $("#dat-key");
    const keep = select.value;
    select.replaceChildren(el("option", "Choose a column", { value: "" }),
      ...info.columns.map((c) => el("option", c, { value: c })));
    select.value = info.columns.includes(keep) ? keep : info.key || "";
    $("#dat-info").textContent = `${info.rows} row${info.rows === 1 ? "" : "s"}, ${info.columns.length} columns.`;
    showMessages([]);
  } catch (err) { report(err); }
}

async function loadOpt() {
  const opt = $("#opt-path").value.trim();
  if (!opt) { showMessages(["Choose the OPT file first."]); return; }
  if (!confirmReplace()) return;
  const dat = $("#dat-path").value.trim();
  const body = { opt_path: opt };
  if (dat) Object.assign(body, {
    dat_path: dat, dat_key: $("#dat-key").value || null,
    dat_delimiter: $("#dat-delimiter").value, dat_qualifier: $("#dat-qualifier").value,
  });
  const btn = $("#load-opt");
  btn.disabled = true;
  try { await afterLoad(await api("POST", "/api/load/opt", body)); } catch (err) { report(err); } finally { btn.disabled = false; }
}

// ---------------------------------------------------------------------------
// Server-side file browser
// ---------------------------------------------------------------------------

const browser = { kind: "opt", onPick: null, path: null, seq: 0 };

function openBrowser(kind, start, onPick) {
  Object.assign(browser, { kind, onPick });
  $("#browser-title").textContent = { opt: "Choose the OPT file", dat: "Choose the DAT file", dir: "Choose the output folder" }[kind];
  $("#browser-choose").hidden = kind !== "dir";
  $("#browser-list").replaceChildren();
  $("#browser").showModal();
  browse(start || null, true);
}

async function browse(path, fallbackHome = false) {
  const q = new URLSearchParams({ kind: browser.kind });
  if (path) q.set("path", path);
  const seq = ++browser.seq;
  let data;
  try { data = await api("GET", `/api/fs/list?${q}`); } catch (err) {
    if (seq !== browser.seq) return;
    if (fallbackHome && path) return browse(null);
    $("#browser-list").replaceChildren(el("li", err.messages ? err.messages.join(" ") : String(err)));
    return;
  }
  if (seq !== browser.seq) return;   // a newer listing was asked for meanwhile
  browser.path = data.path;
  $("#browser-path").value = data.path;
  const items = [];
  if (data.parent) { const li = el("li", "Up one folder", { class: "up" }); li.onclick = () => browse(data.parent); items.push(li); }
  for (const d of data.dirs) { const li = el("li", d, { class: "dir" }); li.onclick = () => browse(joinPath(data.path, d)); items.push(li); }
  for (const f of data.files) {
    const li = el("li", f, { class: "file" });
    li.onclick = () => { $("#browser").close(); browser.onPick(joinPath(data.path, f)); };
    items.push(li);
  }
  if (!data.dirs.length && !data.files.length) items.push(el("li", browser.kind === "dir" ? "No subfolders." : "No matching files here."));
  $("#browser-list").replaceChildren(...items);
}

function joinPath(dir, name) {
  const sep = dir.includes("\\") && !dir.includes("/") ? "\\" : "/";
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

function parentOf(path) {
  if (!path) return null;
  const i = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return i > 0 ? path.slice(0, i) : null;
}

// ---------------------------------------------------------------------------
// Review
// ---------------------------------------------------------------------------

function renderReview() {
  const p = S.production;
  if (!p) return;
  const t = p.totals;
  $("#totals").replaceChildren(...[["Documents", t.documents], ["Pages", t.pages], ["Colour pages", t.color_pages],
    ["Landscape pages", t.landscape_pages]].map(([label, n]) => { const d = el("div", label); d.prepend(el("strong", n.toLocaleString())); return d; }));
  const counts = { error: 0, warning: 0, info: 0 };
  for (const i of p.issues) counts[i.severity] += 1;
  for (const opt of $("#severity-filter").options) {
    if (opt.value !== "all") opt.textContent = `${opt.value[0].toUpperCase()}${opt.value.slice(1)}s (${counts[opt.value]})`;
    else opt.textContent = `All issues (${p.issues.length})`;
  }
  renderIssues();

  const opt = p.source_kind === "opt";
  for (const c of $$(".opt-only")) c.hidden = !opt;
  $("#sort-name").hidden = opt;
  $("#order-note").textContent = opt
    ? "The order comes from the OPT. You can remove documents" +
      (S.settings && S.settings.numbering.mode === "keep" ? "; with existing numbers kept, removing documents leaves gaps in the numbering." : ".")
    : "Drag rows to change the order. The order decides the numbering.";
  const hasErrors = p.documents.some((d) => d.status === "error");
  $("#remove-errors").hidden = !hasErrors;

  const rows = p.documents.map((d) => {
    const tr = el("tr", null, { "data-seq": d.seq, draggable: opt ? false : "true" });
    const box = el("input", null, { type: "checkbox", class: "doc-select", "aria-label": `Select ${d.source_name}` });
    const td0 = el("td"); td0.append(box);
    const status = el("td"); status.append(el("span", d.status === "ok" ? "OK" : d.status, { class: `badge ${d.status}` }));
    tr.append(td0, el("td", d.seq), el("td", d.source_name), el("td", d.page_count));
    if (opt) tr.append(el("td", d.original_bates || ""));
    tr.append(status);
    return tr;
  });
  $("#docs-table tbody").replaceChildren(...rows);
  if (!opt) enableRowDrag();
}

function renderIssues() {
  const filter = $("#severity-filter").value;
  const issues = S.production.issues.filter((i) => filter === "all" || i.severity === filter);
  $("#issues-table tbody").replaceChildren(...issues.map((i) => {
    const tr = el("tr");
    const sev = el("td"); sev.append(el("span", i.severity, { class: `badge ${i.severity}` }));
    tr.append(sev, el("td", i.doc_seq ?? ""), el("td", i.line ?? ""), el("td", i.message));
    return tr;
  }));
  $("#issues-table").hidden = issues.length === 0;
  $("#no-issues").hidden = issues.length !== 0;
  $("#no-issues").textContent = S.production.issues.length ? "No issues of this kind." : "No issues found.";
}

function enableRowDrag() {
  let dragged = null;
  for (const tr of $$("#docs-table tbody tr")) {
    tr.addEventListener("dragstart", (e) => { dragged = tr; tr.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
    tr.addEventListener("dragend", () => { tr.classList.remove("dragging"); $$(".drop-before").forEach((r) => r.classList.remove("drop-before")); });
    tr.addEventListener("dragover", (e) => {
      e.preventDefault();
      $$(".drop-before").forEach((r) => r.classList.remove("drop-before"));
      if (tr !== dragged) tr.classList.add("drop-before");
    });
    tr.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!dragged || dragged === tr) return;
      const order = S.production.documents.map((d) => d.seq);
      const from = Number(dragged.dataset.seq), to = Number(tr.dataset.seq);
      order.splice(order.indexOf(from), 1);
      order.splice(order.indexOf(to), 0, from);
      putOrder({ order });
    });
  }
}

async function putOrder(body) {
  try {
    S.production = await api("PUT", "/api/production/order", body);
    S.view = await api("GET", "/api/settings");
    S.preview = { doc: 1, page: 1 };
    showMessages([]);
  } catch (err) { report(err); }
  renderReview();
  renderSteps();
}

function removeDocs(seqs) {
  if (!seqs.length) { showMessages(["Select the documents to remove first."]); return; }
  let question = `Remove ${seqs.length} document${seqs.length === 1 ? "" : "s"} from this production? The files themselves are not touched.`;
  if (S.production.source_kind === "opt" && S.settings.numbering.mode === "keep") {
    question += " Existing numbers are kept, so this leaves gaps in the numbering.";
  }
  if (confirm(question)) putOrder({ remove: seqs });
}

// ---------------------------------------------------------------------------
// Number
// ---------------------------------------------------------------------------

function renderNumber(fill = true) {
  if (!S.settings) return;
  const isOpt = S.production && S.production.source_kind === "opt";
  const mode = S.settings.numbering.mode;
  for (const r of $$('input[name="num-mode"]')) { r.checked = r.value === mode; }
  const keep = $('input[name="num-mode"][value="keep"]');
  keep.disabled = !isOpt && mode !== "keep";
  $("#keep-label").classList.toggle("disabled", keep.disabled);
  $("#keep-label").title = isOpt ? "" : "Only an OPT production has existing numbers to keep.";
  for (const input of $$("#renumber-fields input")) input.disabled = mode === "keep";
  if (fill) fillSettingInputs();
  renderFieldErrors();
  list("#numbering-problems", S.view.numbering.problems);
  const sample = S.view.numbering.sample;
  const box = $("#numbering-sample");
  if (!sample) { box.replaceChildren(el("p", "Fix the problems above to see the numbers.", { class: "muted" })); return; }
  const table = el("table", null, { class: "sample" });
  const row = (label, a, b) => { const tr = el("tr"); tr.append(el("td", label), el("td", a === b ? a : `${a} – ${b}`)); table.append(tr); };
  row("Production", sample.first, sample.last);
  sample.documents.forEach(([a, b], i) => row(`Document ${i + 1}`, a, b));
  box.replaceChildren(table);
}

// ---------------------------------------------------------------------------
// Stamp: zones and preview
// ---------------------------------------------------------------------------

function enterStamp() {
  renderZoneGrid();
  renderZoneEditor();
  for (const r of $$('input[name="margin-mode"]')) r.checked = r.value === S.settings.margin.mode;
  $("#band-field").hidden = S.settings.margin.mode !== "add_band";
  fillSettingInputs();
  renderFieldErrors();
  const picker = $("#token-picker");
  picker.replaceChildren(el("option", "Insert a token…", { value: "" }), ...S.view.tokens.map((t) => el("option", `{${t}}`, { value: t })));
  clampPreview();
  loadPreview();
}

function zoneProblems(z) {
  const msgs = [...((S.view && S.view.zones[z]) || [])];
  for (const [k, m] of Object.entries(fieldErrors())) if (k.startsWith(`zones.${z}`)) msgs.push(...m);
  return msgs;
}

function renderZoneGrid() {
  const grid = $("#zone-grid");
  grid.replaceChildren(...ZONES.map((z) => {
    const zs = S.settings.zones[z];
    const b = el("button", null, { class: "zone", type: "button", title: ZONE_NAMES[z], "aria-pressed": z === S.zone ? "true" : "false" });
    b.classList.toggle("enabled", zs.enabled);
    b.classList.toggle("selected", z === S.zone);
    b.classList.toggle("bad", zoneProblems(z).length > 0);
    b.append(el("span", z, { class: "zid" }), el("span", zs.enabled ? zs.template.split("\n")[0] || "(empty)" : "off", { class: "ztext" }));
    b.onclick = () => { S.zone = z; renderZoneGrid(); renderZoneEditor(); };
    return b;
  }));
}

function renderZoneEditor() {
  const zs = S.settings.zones[S.zone];
  $("#zone-title").textContent = `${ZONE_NAMES[S.zone]} (${S.zone})`;
  for (const input of $$("[data-zone]")) {
    const v = zs[input.dataset.zone];
    if (input.type === "checkbox") input.checked = !!v;
    else if (input.type === "color") input.value = v.toLowerCase();
    else input.value = String(v);
  }
  $("[data-mc-only]").hidden = S.zone !== "MC";
  renderZoneErrors();
}

function renderZoneErrors() {
  list("#zone-errors", zoneProblems(S.zone));
  $('[data-zone="template"]').classList.toggle("invalid", zoneProblems(S.zone).length > 0);
}

function editZone(input) {
  const key = input.dataset.zone;
  let value;
  if (input.type === "checkbox") value = input.checked;
  else if (key === "size_pt") value = input.value === "" ? "" : Number(input.value);
  else if (key === "rotation") value = Number(input.value);
  else value = input.value;
  S.settings.zones[S.zone][key] = value;
  // Typing into a zone's text turns it on; clearing it does not turn it off.
  if (key === "template" && value && !S.settings.zones[S.zone].enabled) {
    S.settings.zones[S.zone].enabled = true;
    $('[data-zone="enabled"]').checked = true;
  }
  S.edits += 1;
  renderZoneGrid();
  scheduleSave();
}

function insertToken(token) {
  const ta = $('[data-zone="template"]');
  const text = `{${token}}`;
  const start = ta.selectionStart ?? ta.value.length, end = ta.selectionEnd ?? ta.value.length;
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  ta.focus();
  ta.selectionStart = ta.selectionEnd = start + text.length;
  editZone(ta);
}

function copyStyleToAll() {
  const src = S.settings.zones[S.zone];
  for (const z of ZONES) if (z !== S.zone) for (const k of STYLE_KEYS) S.settings.zones[z][k] = src[k];
  S.edits += 1;
  renderZoneGrid();
  scheduleSave();
  showMessages([`Copied the font, size, bold, colour, white box and page rule of ${S.zone} to every zone.`], true);
}

function docs() { return S.production ? S.production.documents : []; }
function currentDoc() { return docs().find((d) => d.seq === S.preview.doc); }

function clampPreview() {
  const all = docs();
  if (!all.length) return;
  if (!currentDoc()) S.preview = { doc: all[0].seq, page: 1 };
  const d = currentDoc();
  S.preview.page = Math.min(Math.max(1, S.preview.page), Math.max(1, d.page_count));
}

let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(loadPreview, 50); }

async function loadPreview() {
  clampPreview();
  const d = currentDoc();
  const img = $("#preview-img"), errBox = $("#preview-error");
  if (!d) return;
  const pg = d.pages[S.preview.page - 1];
  const bates = pg && (pg.bates || pg.original_bates);
  $("#preview-label").textContent = `Document ${d.seq} of ${docs().length} · page ${S.preview.page} of ${d.page_count}` + (bates ? ` · ${bates}` : "");
  $("#preview-note").textContent = S.settings.output.format === "tiff"
    ? "Shown as it will be written to TIFF (black-and-white pages are bi-level)."
    : "Shown as it will be written to PDF.";
  $("#prev-doc").disabled = d.seq === docs()[0].seq;
  $("#next-doc").disabled = d.seq === docs()[docs().length - 1].seq;
  $("#prev-page").disabled = S.preview.page <= 1 && d.seq === docs()[0].seq;
  $("#next-page").disabled = S.preview.page >= d.page_count && d.seq === docs()[docs().length - 1].seq;
  if (!d.page_count) { img.hidden = true; errBox.hidden = false; errBox.textContent = "This document has no pages that can be shown."; return; }
  const seq = ++S.previewSeq;
  img.classList.add("loading");
  try {
    const res = await fetch(`/api/preview?doc=${d.seq}&page=${S.preview.page}&dpi=100&v=${seq}`);
    if (seq !== S.previewSeq) return;
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new UserError((data && data.messages) || ["The preview could not be made."]);
    }
    const url = URL.createObjectURL(await res.blob());
    if (seq !== S.previewSeq) { URL.revokeObjectURL(url); return; }
    if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
    img.src = url;
    img.hidden = false;
    errBox.hidden = true;
  } catch (err) {
    if (seq !== S.previewSeq) return;
    img.hidden = true;
    errBox.hidden = false;
    errBox.textContent = err.messages ? err.messages.join(" ") : String(err);
  } finally { if (seq === S.previewSeq) img.classList.remove("loading"); }
}

function step(deltaPage, deltaDoc) {
  const all = docs();
  let i = all.findIndex((d) => d.seq === S.preview.doc);
  if (deltaDoc) {
    i = Math.min(Math.max(0, i + deltaDoc), all.length - 1);
    S.preview = { doc: all[i].seq, page: 1 };
  } else {
    let page = S.preview.page + deltaPage;
    if (page < 1 && i > 0) { i -= 1; page = Math.max(1, all[i].page_count); }
    else if (page > all[i].page_count && i < all.length - 1) { i += 1; page = 1; }
    S.preview = { doc: all[i].seq, page };
  }
  loadPreview();
}

async function jumpBates() {
  const bates = $("#jump-bates").value.trim();
  if (!bates) return;
  try {
    const at = await api("GET", `/api/locate?${new URLSearchParams({ bates })}`);
    S.preview = { doc: at.doc, page: at.page };
    showMessages([]);
    loadPreview();
  } catch (err) { report(err); }
}

function jumpTo(kind) {
  const at = S.production.jumps[kind];
  if (!at) { showMessages([`There are no ${kind === "first_portrait" ? "portrait" : "landscape"} pages.`]); return; }
  S.preview = { doc: at[0], page: at[1] };
  showMessages([]);
  loadPreview();
}

// ---------------------------------------------------------------------------
// Output and presets
// ---------------------------------------------------------------------------

function enterOutput() {
  renderOutput(true);
  refreshPresets();
}

function renderOutput(fill = true) {
  if (!S.settings) return;
  for (const r of $$('input[name="out-format"]')) r.checked = r.value === S.settings.output.format;
  $("#tiff-options").hidden = S.settings.output.format !== "tiff";
  if (fill) fillSettingInputs();
  renderFieldErrors();
  const pre = S.view.preflight;
  $("#estimate").textContent = pre && pre.estimated_bytes ? `Estimated output size: ${formatBytes(pre.estimated_bytes)}.` : "";
  list("#preflight-errors", pre ? pre.errors : []);
  list("#preflight-warnings", pre ? pre.warnings : []);
}

async function refreshPresets() {
  try {
    const { presets } = await api("GET", "/api/presets");
    const select = $("#preset-list");
    select.replaceChildren(...(presets.length ? presets.map((p) => el("option", p, { value: p })) : [el("option", "No saved presets", { value: "" })]));
    $("#preset-load").disabled = !presets.length;
  } catch (err) { report(err); }
}

async function savePreset() {
  const name = $("#preset-name").value.trim();
  if (!name) { showMessages(["Give the preset a name."]); return; }
  await flushSave();
  if (Object.keys(fieldErrors()).length) { showMessages(["Fix the settings marked in red before saving a preset."]); return; }
  try {
    const { presets } = await api("GET", "/api/presets");
    if (presets.includes(name) && !confirm(`Replace the preset "${name}"?`)) return;
    await api("POST", "/api/presets", { name });
    await refreshPresets();
    $("#preset-list").value = name;
    showMessages([`Saved the preset "${name}".`], true);
  } catch (err) { report(err); }
}

async function loadPreset() {
  const name = $("#preset-list").value;
  if (!name) return;
  try {
    const { settings } = await api("GET", `/api/presets/${encodeURIComponent(name)}`);
    settings.output.output_dir = S.settings.output.output_dir;
    S.settings = settings;
    S.edits += 1;
    await saveSettings();
    fillSettingInputs();
    renderOutput(true);
    if (S.view.ok) showMessages([`Loaded the preset "${name}". The output folder was left as it was.`], true);
  } catch (err) { report(err); }
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------

const STATUS_TEXT = {
  queued: "Starting…", running: "Running…",
  completed: "Run complete.", completed_with_failures: "Run complete, but some documents failed.",
  cancelled: "Run cancelled. Documents finished before the cancel are in the output folder.",
  blocked: "The run could not start.", failed: "The run stopped unexpectedly.",
};

function renderRun() {
  const o = S.settings && S.settings.output;
  const p = S.production;
  if (o && p) {
    const box = $("#run-summary");
    const t = el("table", null, { class: "sample" });
    for (const [k, v] of [["Documents", p.totals.documents], ["Pages", p.totals.pages],
      ["Format", o.format === "pdf" ? "PDF, one per document" : "TIFF, one image per page"],
      ["Output folder", o.output_dir || "not chosen"], ["Volume", o.volume_name]]) {
      const tr = el("tr"); tr.append(el("td", k), el("td", String(v))); t.append(tr);
    }
    box.replaceChildren(t);
  }
  const job = S.job;
  const running = job && (job.status === "queued" || job.status === "running");
  const ready = S.view && S.view.preflight && S.view.preflight.errors.length === 0;
  $("#run-start").disabled = running || !ready;
  $("#run-start").textContent = job && !running ? "Start another run" : "Start run";
  if (!running && !ready && S.view && S.view.preflight) {
    if (job) $("#run-start").title = S.view.preflight.errors.join(" ");
  }
  $("#run-progress").hidden = !running;
  $("#run-result").hidden = !job || running;
  if (!job) return;
  const pct = job.pages_total ? Math.round((100 * job.pages_done) / job.pages_total) : 0;
  $("#progress-bar").style.width = `${pct}%`;
  $("#progress-text").textContent = `${job.pages_done.toLocaleString()} of ${job.pages_total.toLocaleString()} pages` +
    (job.current_document ? ` · document ${job.current_document}` : "") + ` · ${formatElapsed(job.elapsed_seconds)}`;
  if (running) return;

  const good = job.status === "completed" && job.qc_passed;
  const status = $("#run-status");
  status.className = `status ${good ? "pass" : "fail"}`;
  status.textContent = STATUS_TEXT[job.status] + (job.qc_passed === false ? " QC found problems." : job.qc_passed ? " QC passed." : "") +
    ` (${formatElapsed(job.elapsed_seconds)})`;
  const fail = $("#run-failures");
  fail.replaceChildren();
  if (job.problems.length) { const ul = el("ul", null, { class: "problems" }); list(ul, job.problems); fail.append(ul); }
  if (job.failures.length) {
    fail.append(el("h2", `Failed documents (${job.failures.length})`));
    const ul = el("ul", null, { class: "problems" });
    list(ul, job.failures.map((f) => `Document ${f.document}: ${f.message}`));
    fail.append(ul);
  }
  $("#qc-table").hidden = !job.qc.length;
  $("#qc-table tbody").replaceChildren(...job.qc.map((c) => {
    const tr = el("tr");
    const r = el("td"); r.append(el("span", c.passed ? "Pass" : "Fail", { class: `badge ${c.passed ? "pass" : "fail"}` }));
    const details = el("td", c.summary);
    if (c.details.length) { const ul = el("ul"); list(ul, c.details); details.append(ul); }
    tr.append(el("td", c.check), r, details);
    return tr;
  }));
  $("#open-output").hidden = !job.output_dir;
  $("#open-qc").hidden = !job.qc_report;
  $("#open-qc").href = `/api/jobs/${job.id}/qc`;
  $("#open-note").textContent = job.output_dir ? `Output: ${job.output_dir}` : "";
}

function formatElapsed(s) {
  s = Math.round(s || 0);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

async function startRun() {
  $("#run-status").textContent = "";
  $("#run-result").hidden = true;
  await flushSave();
  try {
    const { id, warnings } = await api("POST", "/api/jobs");
    S.jobId = id;
    $("#run-cancel").disabled = false;
    S.job = await api("GET", `/api/jobs/${id}`);
    showMessages(warnings.length ? warnings : []);
    renderRun();
    poll();
  } catch (err) { report(err); }
}

async function poll() {
  if (!S.jobId) return;
  try { S.job = await api("GET", `/api/jobs/${S.jobId}`); } catch (err) { report(err); }
  const running = S.job && (S.job.status === "queued" || S.job.status === "running");
  if (running) setTimeout(poll, 500);
  else {
    // The output folder now holds this run, so the next run needs a different one.
    S.view = await api("GET", "/api/settings").catch(() => S.view);
    renderSteps();
  }
  if (S.screen === "run") renderRun();
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function wire() {
  for (const btn of $$(".step")) btn.onclick = () => go(btn.dataset.screen);

  for (const tab of $$(".tab")) tab.onclick = () => {
    for (const t of $$(".tab")) t.classList.toggle("active", t === tab);
    for (const p of $$(".tab-panel")) p.hidden = p.dataset.panel !== tab.dataset.tab;
    showMessages([]);
  };

  const dz = $("#dropzone");
  for (const ev of ["dragenter", "dragover"]) dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", async (e) => {
    e.preventDefault();
    dz.classList.remove("over");
    try { addPending(await filesFromDrop(e.dataTransfer)); } catch (err) { report(err); }
  });
  // Dropping outside the drop zone must not navigate away from the app.
  for (const ev of ["dragover", "drop"]) window.addEventListener(ev, (e) => e.preventDefault());
  $("#pick-files").onchange = (e) => { addPending([...e.target.files].map((f) => ({ file: f, path: f.name }))); e.target.value = ""; };
  $("#pick-folder").onchange = (e) => {
    addPending([...e.target.files].filter((f) => !f.name.startsWith(".")).map((f) => ({ file: f, path: f.webkitRelativePath || f.name })));
    e.target.value = "";
  };
  $("#clear-pending").onclick = () => { S.pending = []; renderPending(); };
  $("#load-loose").onclick = loadLoose;

  for (const btn of $$("[data-browse]")) btn.onclick = (e) => {
    e.preventDefault();
    const kind = btn.dataset.browse;
    if (kind === "dir") {
      const current = S.settings.output.output_dir;
      openBrowser("dir", current || null, null);
      return;
    }
    const target = $(`#${btn.dataset.target}`);
    const start = parentOf(target.value.trim()) || parentOf($("#opt-path").value.trim());
    openBrowser(kind, start, (path) => { target.value = path; if (kind === "dat") datChanged(); });
  };
  $("#browser-go").onclick = () => browse($("#browser-path").value.trim());
  $("#browser-path").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); browse(e.target.value.trim()); } });
  $("#browser-choose").onclick = () => {
    $("#browser").close();
    $('[data-setting="output.output_dir"]').value = browser.path;
    editSetting("output.output_dir", browser.path);
  };
  $("#dat-path").addEventListener("change", datChanged);
  $("#dat-delimiter").onchange = datChanged;
  $("#dat-qualifier").onchange = datChanged;
  $("#load-opt").onclick = loadOpt;

  $("#severity-filter").onchange = renderIssues;
  $("#sort-name").onclick = () => {
    const order = docs().slice().sort((a, b) => naturalCompare(a.source_name, b.source_name)).map((d) => d.seq);
    putOrder({ order });
  };
  $("#remove-selected").onclick = () => removeDocs($$(".doc-select").filter((c) => c.checked).map((c) => Number(c.closest("tr").dataset.seq)));
  $("#remove-errors").onclick = () => removeDocs(docs().filter((d) => d.status === "error").map((d) => d.seq));

  for (const input of $$("[data-setting]")) {
    input.addEventListener(input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input", () => editSetting(input.dataset.setting, readInput(input)));
  }
  for (const r of $$('input[name="num-mode"]')) r.onchange = () => { editSetting("numbering.mode", r.value); renderNumber(false); };
  for (const r of $$('input[name="margin-mode"]')) r.onchange = () => { editSetting("margin.mode", r.value); $("#band-field").hidden = r.value !== "add_band"; };
  for (const r of $$('input[name="out-format"]')) r.onchange = () => { editSetting("output.format", r.value); renderOutput(false); };

  for (const input of $$("[data-zone]")) {
    input.addEventListener(input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input", () => editZone(input));
  }
  $("#token-picker").onchange = (e) => { if (e.target.value) insertToken(e.target.value); e.target.value = ""; };
  $("#copy-all").onclick = copyStyleToAll;

  $("#prev-doc").onclick = () => step(0, -1);
  $("#next-doc").onclick = () => step(0, 1);
  $("#prev-page").onclick = () => step(-1, 0);
  $("#next-page").onclick = () => step(1, 0);
  $("#jump-go").onclick = jumpBates;
  $("#jump-bates").addEventListener("keydown", (e) => { if (e.key === "Enter") jumpBates(); });
  $("#jump-portrait").onclick = () => jumpTo("first_portrait");
  $("#jump-landscape").onclick = () => jumpTo("first_landscape");

  $("#preset-save").onclick = savePreset;
  $("#preset-load").onclick = loadPreset;

  $("#run-start").onclick = startRun;
  $("#run-cancel").onclick = async () => {
    try { await api("POST", `/api/jobs/${S.jobId}/cancel`); $("#run-cancel").disabled = true; } catch (err) { report(err); }
  };
  $("#open-output").onclick = async () => {
    try {
      const r = await api("POST", `/api/jobs/${S.jobId}/open`);
      if (!r.opened) showMessages([`The folder could not be opened automatically. It is at ${r.path}`]);
    } catch (err) { report(err); }
  };
}

async function init() {
  wire();
  try {
    S.view = await api("GET", "/api/settings");
    S.settings = structuredClone(S.view.settings);
    S.production = await api("GET", "/api/production", undefined, [409]);
    if (S.production && S.production.messages) S.production = null;
  } catch (err) { report(err); }
  go(S.production ? "review" : "load");
}

init();
