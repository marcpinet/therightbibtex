"use strict";
/* therightbibtex : interface. Vanilla JS, aucune dépendance. Le texte venant du
   serveur passe toujours par textContent (jamais innerHTML). */

// ------------------------------------------------------------------ outils
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style") el.style.cssText = v;   // via CSSOM : autorisé par la CSP, contrairement à l'attribut
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const LABELS = {
  crossref: "Crossref", doi: "doi.org", datacite: "DataCite", arxiv: "arXiv", s2: "Semantic Scholar",
  openalex: "OpenAlex", dblp: "DBLP", openreview: "OpenReview", europepmc: "Europe PMC", hal: "HAL",
  inspire: "INSPIRE-HEP", zbmath: "zbMATH", scholar: "Google Scholar", proceedings: "Official proceedings",
  acl: "ACL Anthology", web: "Web page", pdf: "PDF", cite: "Abbreviation (built-in)",
};
const SOURCE_ORDER = ["crossref", "dblp", "arxiv", "openreview", "s2", "openalex", "datacite", "europepmc", "hal",
  "inspire", "zbmath", "scholar", "doi", "proceedings", "acl", "web"];
const STATUS_WORD = { published: "published", preprint: "preprint", submitted: "submission", other: "other record" };

const store = {
  get(k, d) { try { const v = localStorage.getItem("cite." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("cite." + k, JSON.stringify(v)); } catch { /* navigation privée */ } },
};

class ApiError extends Error { constructor(msg, status, retryAfter) { super(msg); this.status = status; this.retryAfter = retryAfter; } }

async function api(method, url, body) {
  const opt = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opt); } catch { throw new ApiError("Server unreachable", 0); }
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const data = isJson ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) throw new ApiError((data && data.error) || `Error ${res.status}`, res.status, data && data.retry_after);
  return data;
}

function toast(msg, opts = {}) {
  const t = h("div", { class: "toast" + (opts.error ? " err" : "") }, h("span", { text: msg }));
  if (opts.action) {
    t.append(h("button", { text: opts.action, onclick: () => { t.remove(); opts.onAction(); } }));
  }
  $("#toasts").append(t);
  setTimeout(() => t.remove(), opts.duration || (opts.action ? 7000 : 3200));
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {
    const ta = h("textarea", { style: "position:fixed;left:-9999px" });
    ta.value = text; document.body.append(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

// Confirmation de copie : un coup de surligneur traverse le bouton.
function flashButton(btn, text) {
  const old = btn.textContent;
  btn.textContent = text; btn.classList.add("swiped");
  setTimeout(() => { btn.textContent = old; btn.classList.remove("swiped"); }, 1400);
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const fmtInt = (n) => new Intl.NumberFormat("en-US").format(n);

// ------------------------------------------------------------------ état
const state = {
  me: null, settings: {}, folders: [], counts: { inbox: 0, all: 0 },
  expanded: new Set(store.get("expanded", [])),
  view: "search", folderId: "all",
  search: null, batch: null, controller: null,
  favs: [], selected: new Set(), openFav: null, editFav: null,
  history: [],
};

// ------------------------------------------------------------------ démarrage
async function boot() {
  let me;
  try { me = await api("GET", "/api/me"); } catch (err) { toast(err.message, { error: true }); me = {}; }
  state.me = me; state.settings = me.settings || {};
  applyCardWidth();
  if (me.version) { $("#who").textContent = `v${me.version}`; $("#who").title = `therightbibtex ${me.version}`; }
  $("#home-path").textContent = me.home || "";
  $("#shell").hidden = false;
  $("#app").classList.remove("booting");
  await Promise.all([loadFolders(), loadHistory()]);
  applyHash();
  if (state.view === "search") $("#q").focus();
}

// ------------------------------------------------------------------ navigation
function applyHash() {
  const m = location.hash.match(/^#(?:folder|dossier)\/(all|inbox|\d+)$/);
  if (m) openFolder(m[1], false);
  else showSearchView(false);
}
window.addEventListener("hashchange", applyHash);

function closeDrawer() { $("#side").classList.remove("open"); $("#scrim").hidden = true; }
$("#menu-btn").addEventListener("click", () => { $("#side").classList.add("open"); $("#scrim").hidden = false; });
$("#scrim").addEventListener("click", closeDrawer);
$("#side-close").addEventListener("click", closeDrawer);

// Accueil : la barre de recherche seule, au milieu de la page.
function updateHome() {
  $("#main").classList.toggle("is-home", state.view === "search" && !state.search && !state.batch);
}

function showSearchView(push = true) {
  state.view = "search";
  $("#view-search").hidden = false; $("#view-folder").hidden = true;
  $("#nav-search").classList.add("active");
  if (push && location.hash) history.pushState(null, "", location.pathname);
  renderTree();
  updateHome();
  closeDrawer();
}
$("#nav-search").addEventListener("click", () => { showSearchView(); $("#q").focus(); });
$("#home-link").addEventListener("click", (e) => {
  e.preventDefault(); showSearchView();
  if (state.controller) state.controller.abort();
  state.search = null; state.batch = null; $("#q").value = ""; autosize(); renderSearch(); $("#q").focus();
});

// ------------------------------------------------------------------ historique
async function loadHistory() {
  try { state.history = (await api("GET", "/api/history")).history; } catch { state.history = []; }
  renderHistory();
}
function renderHistory() {
  const item = (it) => {
    const isPdf = it.kind === "pdf" && it.query.startsWith("PDF");
    return h("button", {
      title: it.title ? `${it.query}\n→ ${it.title}` : it.query, text: isPdf ? it.title || it.query : it.query,
      onclick: () => { const q = isPdf ? it.title : it.query; if (!q) return; $("#q").value = q; autosize(); showSearchView(); submitQuery(); },
    });
  };
  $("#history").replaceChildren(...state.history.slice(0, 8).map((it) => h("li", null, item(it))));
  $("#home-recent").replaceChildren(...state.history.slice(0, 5).map(item));
  $("#history-clear").hidden = !state.history.length;
}
$("#history-clear").addEventListener("click", async () => { await api("DELETE", "/api/history"); state.history = []; renderHistory(); });

// ------------------------------------------------------------------ saisie
const qEl = $("#q");
function autosize() {
  qEl.style.height = "auto";
  qEl.style.height = Math.min(qEl.scrollHeight + 2, 224) + "px";
  qEl.style.overflowY = qEl.scrollHeight > 224 ? "auto" : "hidden";   // barre de défilement seulement au-delà du maximum
}
qEl.addEventListener("input", () => { autosize(); $("#multiline-ask").hidden = true; });
qEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitQuery(); }
});
$("#omni").addEventListener("submit", (e) => { e.preventDefault(); submitQuery(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !$("#settings-dialog").open) {
    e.preventDefault(); showSearchView(); qEl.focus(); qEl.select();
  }
  if (e.key === "Escape") { closePop(); closeDrawer(); }
});

function countBibEntries(text) { return (text.match(/^\s*@\w+\s*[{(]/gm) || []).length; }

// Une référence copiée d'un PDF tient souvent sur plusieurs lignes : on ne
// propose « une recherche par ligne » que si les lignes ressemblent à des
// entrées distinctes (plusieurs années, liste numérotée, titres sans virgules).
function looksLikeOneReference(lines) {
  if (lines.length > 4 || lines.slice(1).some((l) => /^(\[\d+\]|\d+\.\s)/.test(l))) return false;
  const text = lines.join(" ");
  const years = (text.replace(/\b\d{4}\.\d{4,5}\b/g, " ").match(/\b(19|20)\d\d\b/g) || []).length;
  const commas = (text.match(/,/g) || []).length;
  const wrapped = lines.slice(0, -1).every((l, i) => /-$/.test(l) || /^[a-z(]/.test(lines[i + 1]) || !/[.?!"”»]$/.test(l));
  return years <= 1 && commas >= 2 && (years === 1 || wrapped);
}

function submitQuery(force) {
  const text = qEl.value.trim();
  if (!text) return;
  showSearchView();
  const entries = countBibEntries(text);
  if (entries >= 2) { startBatch(splitBib(text).map((t) => ({ text: t, label: bibLabel(t) }))); return; }
  const lines = text.split(/\n+/).map((l) => l.trim()).filter(Boolean);
  if (!force && lines.length >= 2 && !text.startsWith("@") && !looksLikeOneReference(lines)) {
    $("#multiline-ask").hidden = false;
    return;
  }
  $("#multiline-ask").hidden = true;
  startSearch({ text });
}
$("#ml-batch").addEventListener("click", () => {
  $("#multiline-ask").hidden = true;
  const lines = qEl.value.split(/\n+/).map((l) => l.trim()).filter(Boolean);
  startBatch(lines.slice(0, 100).map((l) => ({ text: l, label: l })));
});
$("#ml-single").addEventListener("click", () => { $("#multiline-ask").hidden = true; startSearch({ text: qEl.value.trim() }); });

function splitBib(text) {
  const out = []; const re = /@\w+\s*[{(]/g; let m; const starts = [];
  while ((m = re.exec(text))) starts.push(m.index);
  starts.forEach((s, i) => out.push(text.slice(s, starts[i + 1] ?? text.length).trim()));
  return out.filter((t) => !/^@(comment|string|preamble)/i.test(t));
}
function bibLabel(t) {
  const m = t.match(/title\s*=\s*[{"]+([^}"]+)/i);
  return m ? m[1] : t.slice(0, 80);
}

// PDF : bouton, glisser-déposer, coller
$("#pdf-btn").addEventListener("click", () => $("#pdf-input").click());
$("#pdf-input").addEventListener("change", (e) => { handleFiles([...e.target.files]); e.target.value = ""; });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files") || $("#shell").hidden) return;
  dragDepth++; $("#dropzone").hidden = false;
});
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#dropzone").hidden = true; });
window.addEventListener("dragover", (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) e.preventDefault(); });
window.addEventListener("drop", (e) => {
  if (!e.dataTransfer || !e.dataTransfer.files.length) return;
  e.preventDefault(); dragDepth = 0; $("#dropzone").hidden = true;
  handleFiles([...e.dataTransfer.files]);
});
document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])].filter(isPdf);
  if (files.length) { e.preventDefault(); handleFiles(files); }
});
function isPdf(f) { return f && (f.type === "application/pdf" || /\.pdf$/i.test(f.name)); }
function handleFiles(files) {
  const pdfs = files.filter(isPdf);
  if (!pdfs.length) { toast("Only PDF files are accepted", { error: true }); return; }
  showSearchView();
  if (pdfs.length === 1) startSearch({ file: pdfs[0] });
  else startBatch(pdfs.slice(0, 100).map((f) => ({ file: f, label: f.name })));
}

// ------------------------------------------------------------------ recherche (flux NDJSON)
async function streamSearch(params, onEvent, signal) {
  let res;
  if (params.file) {
    const fd = new FormData();
    fd.append("pdf", params.file, params.file.name);
    if (params.refresh) fd.append("refresh", "1");
    res = await fetch("/api/search", { method: "POST", body: fd, signal, credentials: "same-origin" });
  } else {
    res = await fetch("/api/search", {
      method: "POST", signal, credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q: params.text, refresh: !!params.refresh }),
    });
  }
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new ApiError(j.error || `Error ${res.status}`, res.status, j.retry_after);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (line.trim()) onEvent(JSON.parse(line));
    }
  }
}

function newSearch(params) {
  return { params, sources: {}, query: null, works: [], final: false, done: false, error: "", current: null, chosen: {}, showSources: {}, showSusp: {}, keys: {} };
}

// Version affichée (celle qu'on copierait maintenant), pour repérer qu'une plus à jour la remplace.
function shownVersion(s) {
  const w = s.works.find((x) => x.id === s.current);
  return w ? { work: w, v: versionOf(w, s) } : null;
}
const STATUS_RANK = { published: 2, submitted: 1, preprint: 1 };
const sameTitle = (a, b) => a.toLowerCase().replace(/\W+/g, "") === b.toLowerCase().replace(/\W+/g, "");

function applyEvent(s, ev) {
  if (ev.type === "source") s.sources[ev.source] = ev;
  else if (ev.type === "query") s.query = ev.query;
  else if (ev.type === "results") {
    const before = shownVersion(s);
    s.works = ev.works || []; s.final = ev.final; s.elapsed = ev.elapsed;
    if (!s.works.find((w) => w.id === s.current)) s.current = s.works[0]?.id || null;
    const after = shownVersion(s);
    if (before && after && !s.chosen[after.work.id] && sameTitle(before.work.title, after.work.title)
        && (STATUS_RANK[after.v.status] || 0) > (STATUS_RANK[before.v.status] || 0)) {
      noteUpgrade(s, after);
    }
  } else if (ev.type === "done") { s.done = true; s.doneAt = Date.now(); s.elapsed = ev.elapsed; }
  else if (ev.type === "notice") (s.notices ||= []).push(ev.message);
  else if (ev.type === "error") s.error = ev.message;
}

// La prépublication affichée d'abord vient d'être remplacée par la version publiée :
// la fiche s'éclaire d'un coup de surligneur, et un BibTeX déjà copié est signalé périmé.
const UPGRADE_NOTE_MS = 8000;
function noteUpgrade(s, after) {
  s.upgrade = { workId: after.work.id, label: after.v.label, at: Date.now() };
  if (s !== state.search) return;
  setTimeout(() => { if (state.search === s) scheduleRender(); }, UPGRADE_NOTE_MS + 100);
  if (s.copied) {
    s.copied = false;
    toast(`Published version found (${after.v.label}): the BibTeX you copied is outdated`, {
      action: "Copy new", duration: 12000,
      onAction: async () => {
        const cur = shownVersion(s);
        if (cur && await copyText(bibOf(s, cur.v))) toast("Updated BibTeX copied");
      },
    });
  }
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderSearch(); });
}

async function startSearch(params) {
  if (state.controller) state.controller.abort();
  const ctrl = new AbortController();
  state.controller = ctrl;
  state.batch = null;
  const s = newSearch(params);
  state.search = s;
  renderSearch();
  try {
    await streamSearch(params, (ev) => { applyEvent(s, ev); if (state.search === s) scheduleRender(); }, ctrl.signal);
  } catch (err) {
    if (err.name === "AbortError") return;
    s.error = err.message;
  }
  s.done = true;
  if (state.search === s) { renderSearch(); loadHistory(); }
}

// ------------------------------------------------------------------ rendu : recherche
function renderSearch() {
  const s = state.search;
  const batch = state.batch;
  $("#empty").hidden = !!(s || batch);
  updateHome();
  if (batch) { renderBatch(); return; }
  if (!s) { $("#sources").replaceChildren(); $("#query-info").replaceChildren(); $("#results").replaceChildren(); return; }
  renderSources($("#sources"), s);
  renderQueryInfo($("#query-info"), s);
  const out = $("#results");
  const nodes = [];
  if (!s.done) nodes.push(h("div", { class: "progress-line", role: "progressbar", "aria-label": "Searching" }));
  if (s.error && !s.works.length) nodes.push(h("p", { class: "alert", text: s.error }));
  if (s.done && !s.works.length && !s.error) {
    nodes.push(h("p", { class: "verdict low", text: "No paper found. Try the exact title, a DOI or the arXiv id, or drop the PDF." }));
  }
  const work = s.works.find((w) => w.id === s.current);
  if (work) nodes.push(renderWork(work, s));
  const others = s.works.filter((w) => w.id !== s.current);
  if (others.length) {
    nodes.push(h("section", { class: "others" },
      h("h2", { text: work && s.current !== s.works[0].id ? "Other results" : "Other possible matches" }),
      h("ul", { class: "olist" }, others.map((w) => h("li", null, h("button", {
        onclick: () => { s.current = w.id; renderSearch(); window.scrollTo({ top: 0, behavior: "smooth" }); },
      },
        h("div", { class: "ot", text: w.title }),
        h("div", { class: "om" },
          `${w.authors.slice(0, 3).join(", ")}${w.authors.length > 3 ? " et al." : ""}`,
          w.year ? `, ${w.year}` : "",
          ...w.versions.filter((v) => v.id === w.recommended).map((v) => h("span", { class: "badge", text: v.label })),
        ),
      )))),
    ));
  }
  out.replaceChildren(...nodes);
}

// Une ligne de résumé ; le détail par source ne s'ouvre qu'à la demande.
function renderSources(el, s) {
  const names = Object.keys(s.sources).filter((n) => s.sources[n].state !== "skipped")
    .sort((a, b) => (SOURCE_ORDER.indexOf(a) + 99) % 99 - (SOURCE_ORDER.indexOf(b) + 99) % 99);
  if (!names.length) { el.replaceChildren(); return; }
  const found = names.filter((n) => s.sources[n].state === "ok").length;
  const summary = s.done ? `Found by ${found} of ${names.length} sources`
    : `Searching ${names.length} sources…`;
  const nodes = [h("button", { class: "summary", "aria-expanded": s.showStatus ? "true" : "false",
    text: `${summary} ${s.showStatus ? "▴" : "▾"}`, onclick: () => { s.showStatus = !s.showStatus; renderSearch(); } })];
  if (s.showStatus) {
    nodes.push(h("div", { class: "srcgrid" }, names.map((n) => {
      const st = s.sources[n];
      const tip = {
        ok: `${plural(st.count, "record", "records")} in ${st.ms} ms`, empty: "no results", running: "running…", pending: "waiting",
        blocked: st.message || "blocked", error: st.message || "error", timeout: st.message || "timed out",
      }[st.state] || st.state;
      return h("span", { class: `src-chip st-${st.state}`, title: `${LABELS[n] || n}: ${tip}` },
        h("span", { class: "dot" }), LABELS[n] || n, st.state === "ok" ? h("span", { class: "n", text: st.count }) : null);
    })));
  }
  el.replaceChildren(...nodes);
}

// Ce que le moteur a compris de la saisie, seulement quand ce n'est pas évident (PDF, lien, référence).
function renderQueryInfo(el, s) {
  const q = s.query;
  if (!q || (["title", "id"].includes(q.kind) && !(s.notices || []).length)) { el.replaceChildren(); return; }
  const bits = [];
  const kindLabel = { title: "title", reference: "reference", id: "identifier", bibtex: "BibTeX entry", pdf: "PDF", url: "link" }[q.kind] || q.kind;
  bits.push(`Read as ${kindLabel}`);
  const parts = [];
  if (q.kind === "pdf" || q.kind === "url" || q.kind === "reference") if (q.title) parts.push(["title", q.title]);
  if (q.doi) parts.push(["DOI", q.doi]);
  if (q.arxiv) parts.push(["arXiv", q.arxiv]);
  if (q.venue_hint) parts.push(["venue", q.venue_hint]);
  const nodes = [bits[0]];
  parts.forEach(([k, v]) => { nodes.push(`; ${k} `, h("code", { text: v })); });
  for (const n of s.notices || []) nodes.push(h("div", { text: n }));
  el.replaceChildren(...nodes);
}

function versionOf(work, s) {
  const id = s.chosen[work.id] || work.recommended;
  return work.versions.find((v) => v.id === id) || work.versions[0];
}

function renderWork(work, s) {
  const v = versionOf(work, s);
  const root = h("article", { class: "work" });
  root.append(h("h1", { class: "work-title", text: work.title }));
  const authors = work.authors.length > 12 ? work.authors.slice(0, 10).join(", ") + ` and ${work.authors.length - 10} others` : work.authors.join(", ");
  root.append(h("p", { class: "work-meta" }, h("span", { class: "authors", text: authors })));
  const sub = h("div", { class: "work-sub" });
  if (work.citations) sub.append(h("span", { text: `Cited ${fmtInt(work.citations.count)} times (${work.citations.source})` }));
  const links = Object.entries(work.links || {});
  if (links.length) {
    sub.append(h("span", null, "View on ", ...links.flatMap(([label, url], i) =>
      [i ? ", " : "", h("a", { href: url, target: "_blank", rel: "noopener", text: label })])));
  }
  if (s.done && s === state.search) {
    sub.append(h("button", { class: "link tiny", text: "Search again without cache", onclick: () => startSearch({ ...s.params, refresh: true }) }));
  }
  root.append(sub);

  if (s === state.search && s.works[0]?.id === work.id && s.final) {
    const conf = work.confidence || "medium";
    const txt = {
      high: null,
      medium: "Several papers look like your search: check this is the right one (other results below).",
      low: "Uncertain match: no title really fits your search.",
    }[conf];
    if (txt) root.append(h("p", { class: `verdict ${conf}`, text: txt }));
  }
  if (work.retracted) root.append(h("p", { class: "alert", text: "Warning: a version of this paper was retracted or withdrawn. Check before citing it." }));
  for (const hint of work.hints || []) root.append(h("p", { class: "hint", text: hint }));

  // onglets de versions
  const normal = work.versions.filter((x) => !x.suspicious);
  const susp = work.versions.filter((x) => x.suspicious);
  const tabs = h("div", { class: "versions", role: "tablist" });
  const showSusp = s.showSusp[work.id];
  for (const x of [...normal, ...(showSusp ? susp : [])]) {
    const status = STATUS_WORD[x.status] + (x.id === work.recommended ? ", cite this" : "") + (x.suspicious ? ", suspicious" : "");
    tabs.append(h("button", {
      class: "vtab" + (x.id === v.id ? " active" : "") + (x.id === work.recommended ? " rec" : "") + (x.suspicious ? " susp" : ""),
      role: "tab", "aria-selected": x.id === v.id ? "true" : "false",
      title: x.suspicious ? x.suspicious_reason : plural(x.n_sources, "source", "sources"),
      onclick: () => { s.chosen[work.id] = x.id; rerender(s); },
    }, h("span", { class: "vlabel", text: x.label }), h("span", { class: "vstatus", text: status })));
  }
  if (susp.length && !showSusp) {
    tabs.append(h("button", {
      class: "vtab", title: "Likely copies or duplicate deposits",
      onclick: () => { s.showSusp[work.id] = true; rerender(s); },
    }, h("span", { class: "vlabel muted", text: `+${susp.length}` }), h("span", { class: "vstatus", text: "suspicious" })));
  }
  if (work.versions.length > 1 || susp.length) root.append(tabs);

  // Les premières notices (souvent la prépublication) s'affichent tout de suite ;
  // tant que la recherche continue, le BibTeX peut encore passer à une version plus à jour.
  const upgraded = s.upgrade && s.upgrade.workId === work.id && Date.now() - s.upgrade.at < UPGRADE_NOTE_MS;
  if (!s.done) {
    root.append(h("p", { class: "refining", role: "status" }, h("span", { class: "pen", "aria-hidden": "true" }),
      v.status === "published" ? "Checking the remaining sources…" : "Looking for a published version…"));
  } else if (upgraded) {
    root.append(h("p", { class: "refining found", role: "status" }, `Updated: ${s.upgrade.label} found`));
  }

  root.append(renderCard(work, v, s));
  return root;
}

function rerender(s) {
  if (state.batch) renderBatch(); else renderSearch();
}

function bibOf(s, v) {
  const k = s.keys[v.id];
  return k ? v.bibtex.replace(/^(\s*@\w+\s*[{(]\s*)[^,\s]*/, (_m, p) => p + k) : v.bibtex;
}

function renderCard(work, v, s) {
  const wrap = h("div");
  const text = bibOf(s, v);
  const lines = text.split("\n");
  const head = lines[0] || "";
  const hm = head.match(/^@(\w+)\{(.*),$/);
  // Tant que la recherche continue, la notice est provisoire : grisée, avec un « ? »
  // surligné dans le coin ; elle reprend ses couleurs une fois toutes les sources lues.
  const settling = s.done && s.doneAt ? Date.now() - s.doneAt : Infinity;
  const card = h("div", { class: "bibcard" + (!s.done ? " provisional" : settling < 700 ? " settled" : ""),
    style: settling < 700 ? `--settle-delay:-${Math.round(settling)}ms` : null });
  if (!s.done) {
    card.append(h("span", { class: "provisional-mark", text: "?", role: "img",
      "aria-label": "Search still running: this BibTeX may still change",
      title: "Search still running: a more up-to-date version may replace this BibTeX" }));
  }
  const keySpan = h("span", { class: "key", text: hm ? hm[2] : "", title: "Click to edit the key" });
  keySpan.addEventListener("click", () => editKey(keySpan, s, v));
  card.append(h("div", { class: "head" },
    h("span", null, h("span", { class: "etype", text: hm ? `@${hm[1]}{` : head }), keySpan, ","),
    h("span", { class: "muted", text: v.kind && STATUS_WORD[v.status] ? STATUS_WORD[v.status] : "" })));
  const body = h("div", { class: "body" });
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    const m = line.match(/^(\s*)([A-Za-z_]+)(\s*=\s*)(.*)$/);
    if (m) {
      const src = v.prov?.[m[2]];
      const label = src ? (v.prov_labels?.[src] || LABELS[src] || src) : "";
      body.append(h("div", { class: "ln", title: label ? `${m[2]}: ${label}` : null, dataset: label ? { src: label } : {} },
        "  ", h("span", { class: "f", text: m[2] }), h("span", { class: "eq", text: m[3] }),
        ...valueNodes(m[4]),
        label ? h("span", { class: "mg", text: label }) : null));
    } else {
      body.append(h("div", { class: "ln last", text: line }));
    }
  }
  card.append(body);
  const fresh = s.upgrade && s.upgrade.workId === work.id ? Date.now() - s.upgrade.at : Infinity;
  // Délai négatif : l'animation reprend où elle en était si la fiche est redessinée entre-temps.
  wrap.append(h("div", { class: "card-wrap" + (fresh < 1600 ? " fresh" : ""),
    style: fresh < 1600 ? `--fresh-delay:-${Math.round(fresh)}ms` : null }, card,
    h("div", { class: "card-grip", role: "separator", "aria-orientation": "vertical", tabindex: "0",
      "aria-label": "Resize the BibTeX card", title: "Drag to widen or narrow, double-click to reset" })));

  // actions
  const copyBtn = h("button", { class: "btn primary copy", text: "Copy BibTeX" });
  copyBtn.addEventListener("click", async () => {
    if (await copyText(bibOf(s, v))) { flashButton(copyBtn, "Copied"); s.copied = !s.done; }
    else toast("Copy failed", { error: true });
  });
  const saveBtn = h("button", { class: "btn", text: "Save…" });
  saveBtn.addEventListener("click", () => openSavePop(saveBtn, () => bibOf(s, v), work.title));
  const nsrc = v.sources.filter((x) => x.bibtex).length;
  const srcBtn = h("button", { class: "btn ghost", text: s.showSources[v.id] ? "Hide sources" : `Sources (${nsrc})` });
  srcBtn.addEventListener("click", () => { s.showSources[v.id] = !s.showSources[v.id]; rerender(s); });
  // Ce qu'on colle dans l'article : \cite{clé}.
  const keyBtn = h("button", { class: "btn", text: "Copy \\cite{…}", title: "Copies the \\cite{key} command to paste into your paper" });
  keyBtn.addEventListener("click", async () => {
    const k = (bibOf(s, v).match(/^@\w+\{([^,]*)/) || [])[1] || "";
    if (await copyText(`\\cite{${k}}`)) { flashButton(keyBtn, "Copied"); s.copied = !s.done; }
  });
  const actions = h("div", { class: "card-actions" }, copyBtn, saveBtn, keyBtn, srcBtn, h("span", { class: "spacer" }));
  if (v.doi) actions.append(h("a", { class: "btn ghost small", href: `https://doi.org/${v.doi}`, target: "_blank", rel: "noopener", text: "DOI" }));
  if (v.arxiv) actions.append(h("a", { class: "btn ghost small", href: `https://arxiv.org/abs/${v.arxiv}`, target: "_blank", rel: "noopener", text: "arXiv" }));
  if (v.url && !v.url.includes("doi.org") && !v.url.includes("arxiv.org")) actions.append(h("a", { class: "btn ghost small", href: v.url, target: "_blank", rel: "noopener", text: "Page" }));
  wrap.append(actions);

  // infos de version, avis, désaccords
  const info = v.info || {};
  const notes = [];
  if (info.arxiv_version) notes.push(`Latest arXiv version: v${info.arxiv_version}${info.arxiv_updated ? ` (${info.arxiv_updated})` : ""}${info.arxiv_first ? `, first submitted ${info.arxiv_first}` : ""}.`);
  // (pas de redite quand l'avis en tête de fiche cite déjà ce commentaire)
  if (info.comment && !(work.hints || []).some((x) => x.includes(info.comment.slice(0, 60)))) notes.push(`Authors' comment: "${info.comment}"`);
  if (info.journal_ref) notes.push(`Journal reference declared on arXiv: "${info.journal_ref}"`);
  for (const n of v.notices || []) {
    notes.push(h("span", null, `${n.label}${n.date ? ` (${n.date})` : ""} reported by ${n.source}`,
      n.doi ? [": ", h("a", { href: `https://doi.org/${n.doi}`, target: "_blank", rel: "noopener", text: n.doi })] : "", "."));
  }
  if (v.suspicious) notes.push(`Suspicious record: ${v.suspicious_reason}.`);
  const conf = h("div", { class: "conflicts" });
  for (const n of notes) conf.append(h("div", { class: "c" }, n));
  for (const c of v.conflicts || []) {
    conf.append(h("div", { class: "c" }, h("b", { text: `Sources disagree on ${c.field}: ` }),
      c.values.map((x) => `${x.value} (${x.sources.join(", ")})`).join("; ")));
  }
  if (conf.childNodes.length) wrap.append(conf);

  if (s.showSources[v.id]) wrap.append(renderSourcesPanel(v));
  return wrap;
}

function valueNodes(raw) {
  // {valeur}, ou macro sans accolades (month = jun) ; virgule finale en gris
  const m = raw.match(/^(\{)(.*)(\})(,?)$/);
  if (!m) return [raw];
  return [h("span", { class: "br", text: "{" }), m[2], h("span", { class: "br", text: "}" }), m[4]];
}

function editKey(span, s, v) {
  const input = h("input", { class: "key-edit", value: span.textContent, "aria-label": "Citation key" });
  span.replaceWith(input);
  input.focus(); input.select();
  const done = (commit) => {
    if (commit) {
      const k = input.value.trim().replace(/[^A-Za-z0-9_:\-./+]/g, "");
      if (k) s.keys[v.id] = k;
    }
    rerender(s);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); });
  input.addEventListener("blur", () => done(true));
}

function renderSourcesPanel(v) {
  const panel = h("section", { class: "panel" }, h("h3", { text: "What each source says about this version" }));
  const ul = h("ul", { class: "srclist" });
  for (const x of v.sources) {
    const li = h("li");
    const row = h("div", { class: "srow" },
      h("span", { class: "sname", text: x.bibtex_label || x.label }),
      h("span", { class: "snote", text: [x.venue, x.year, x.status_note].filter(Boolean).join(", ") }));
    if (x.link) row.append(h("a", { href: x.link, target: "_blank", rel: "noopener", text: "view" }));
    if (x.bibtex) {
      const b = h("button", { class: "btn small ghost", text: "Copy" });
      b.addEventListener("click", async () => { if (await copyText(x.bibtex)) flashButton(b, "Copied"); });
      row.append(b);
    }
    li.append(row);
    if (x.bibtex) li.append(h("pre", { text: x.bibtex }));
    ul.append(li);
  }
  panel.append(ul);
  return panel;
}

// ------------------------------------------------------------------ lot (plusieurs PDF / lignes / entrées)
function startBatch(items, opts = {}) {
  if (state.controller) state.controller.abort();
  const ctrl = new AbortController();
  state.controller = ctrl;
  state.search = null;
  state.batch = { items: items.map((it, i) => ({ ...it, id: i, status: "queued", search: newSearch(it) })), open: null, ctrl,
    recheck: !!opts.recheck, waitUntil: 0 };
  renderSearch();
  runBatch(state.batch);
}

// Attente interruptible, avec un rafraîchissement par seconde (compte à rebours du lot).
function pause(ms, b) {
  return new Promise((resolve) => {
    const end = Date.now() + ms;
    const tick = () => {
      if (b.ctrl.signal.aborted || Date.now() >= end) { resolve(); return; }
      renderBatchSoon(b); setTimeout(tick, Math.min(1000, end - Date.now()));
    };
    tick();
  });
}

async function runBatch(b) {
  const queue = [...b.items];
  const worker = async () => {
    while (queue.length && state.batch === b && !b.ctrl.signal.aborted) {
      if (b.waitUntil > Date.now()) { await pause(b.waitUntil - Date.now(), b); continue; }
      const it = queue.shift();
      it.status = "running"; renderBatchSoon(b);
      try {
        await streamSearch(it.file ? { file: it.file } : { text: it.text },
          (ev) => { applyEvent(it.search, ev); renderBatchSoon(b); }, b.ctrl.signal);
        it.search.done = true;
        it.status = it.search.works.length ? "done" : "fail";
        if (!it.search.works.length) it.error = it.search.error || "no results";
      } catch (err) {
        if (err.name === "AbortError") return;
        if (err.status === 429) {
          // Limite de recherches : le serveur dit combien de temps attendre.
          it.status = "queued"; it.search = newSearch(it); queue.unshift(it);
          b.waitUntil = Math.max(b.waitUntil, Date.now() + 1000 * Math.max(2, err.retryAfter || 5));
        } else { it.status = "fail"; it.error = err.message; }
      }
      renderBatchSoon(b);
    }
  };
  await Promise.all([worker(), worker()]);
  if (state.batch === b) { renderBatch(); loadHistory(); }
}

let batchQueued = false;
function renderBatchSoon(b) {
  if (batchQueued || state.batch !== b) return;
  batchQueued = true;
  requestAnimationFrame(() => { batchQueued = false; if (state.batch === b) renderBatch(); });
}

function batchEntry(it) {
  const w = it.search.works[0];
  if (!w) return null;
  const v = versionOf(w, it.search);
  return { text: bibOf(it.search, v), key: (bibOf(it.search, v).match(/^@\w+\{([^,]*)/) || [])[1] || "", title: w.title };
}

function batchBibtex(b) {
  const used = {};
  return b.items.map(batchEntry).filter(Boolean).map((e) => {
    const n = used[e.key.toLowerCase()] = (used[e.key.toLowerCase()] || 0) + 1;
    return n > 1 ? e.text.replace(/^(\s*@\w+\s*[{(]\s*)[^,\s]*/, (_m, p) => p + e.key + String.fromCharCode(96 + n - 1)) : e.text;
  }).join("\n\n");
}

const withKey = (text, key) => (key ? text.replace(/^(\s*@\w+\s*[{(]\s*)[^,\s]*/, (_m, p) => p + key) : text);

// Revérification d'un dossier : le favori (une prépublication) a désormais une
// version publiée. On garde la clé du favori, déjà citée dans les articles.
function upgradeOf(it) {
  if (!it.fav || it.status !== "done") return null;
  const w = it.search.works[0];
  const v = versionOf(w, it.search);
  if (v.status !== "published" || w.confidence === "low") return null;
  return { text: withKey(bibOf(it.search, v), it.fav.cite_key), label: v.label, sure: w.confidence === "high" };
}

async function replaceFavs(list) {
  let n = 0;
  for (const [it, up] of list) {
    try { await api("PATCH", `/api/favorites/${it.fav.id}`, { bibtex: up.text }); it.replaced = true; n++; }
    catch (err) { toast(err.message, { error: true }); break; }
  }
  if (n) toast(`Replaced ${plural(n, "favorite", "favorites")} with the published version (key kept)`);
  if (state.batch) renderBatch();
}

function renderBatch() {
  const b = state.batch;
  $("#sources").replaceChildren(); $("#query-info").replaceChildren();
  const done = b.items.filter((i) => i.status === "done");
  const sure = done.filter((i) => i.search.works[0]?.confidence === "high").length;
  const fail = b.items.filter((i) => i.status === "fail").length;
  const pending = b.items.length - done.length - fail;
  const waiting = b.waitUntil > Date.now() && pending ? Math.ceil((b.waitUntil - Date.now()) / 1000) : 0;
  const upgrades = b.recheck ? b.items.map((it) => [it, upgradeOf(it)]).filter(([it, up]) => up && !it.replaced) : [];
  const copyAll = h("button", { class: "btn primary", text: "Copy all", disabled: !done.length });
  copyAll.addEventListener("click", async () => { if (await copyText(batchBibtex(b))) flashButton(copyAll, "Copied"); });
  const saveAll = h("button", { class: "btn", text: "Save all…", disabled: !done.length });
  saveAll.addEventListener("click", () => openSavePop(saveAll, null, null, async (folderId, folderName) => {
    let added = 0, dups = 0;
    for (const it of b.items) {
      const e = batchEntry(it);
      if (!e) continue;
      try {
        const r = await api("POST", "/api/favorites", { folder_id: folderId, bibtex: e.text });
        if (r.id) added++; else if (r.duplicate) dups++;
      } catch { /* continue */ }
    }
    await loadFolders();
    toast(`Saved ${plural(added, "record", "records")} to "${folderName}"${dups ? `, ${dups} already there` : ""}`);
  }));
  const stop = h("button", { class: "btn ghost", text: pending ? "Stop" : "Close batch" });
  stop.addEventListener("click", () => { b.ctrl.abort(); state.batch = null; renderSearch(); });
  const sureUps = upgrades.filter(([, up]) => up.sure);
  const replaceAll = sureUps.length ? h("button", { class: "btn primary", text: `Replace ${plural(sureUps.length, "favorite", "favorites")}`,
    title: "Replaces preprints with their published version (confident matches only), keeping their key",
    onclick: () => replaceFavs(sureUps) }) : null;
  let summary = `${plural(b.items.length, "search", "searches")}: ${sure} confident, ${done.length - sure} to check, ${fail} not found${pending ? `, ${pending} pending` : ""}`;
  if (b.recheck) summary += `; ${plural(upgrades.length, "published version", "published versions")} found`;
  if (waiting) summary += `; paused (60 searches per 10 min), resuming in ${waiting >= 60 ? `${Math.floor(waiting / 60)} min ` : ""}${waiting % 60} s`;
  const head = h("div", { class: "batch-head" }, h("span", { class: "grow", text: summary }),
    replaceAll, b.recheck ? null : copyAll, b.recheck ? null : saveAll, stop);
  const ul = h("ul", { class: "blist" });
  for (const it of b.items) {
    const w = it.search.works[0];
    const conf = w?.confidence;
    const st = it.status === "running" ? "running" : it.status === "fail" ? "fail" : it.status === "done" ? (conf === "high" ? "done" : "medium") : "";
    const entry = batchEntry(it);
    const up = b.recheck ? upgradeOf(it) : null;
    const tag = !b.recheck ? (entry ? entry.key : "") : it.replaced ? "replaced" : up ? `published: ${up.label}`
      : it.status === "done" ? "still a preprint" : "";
    const row = h("div", { class: "brow", role: "button", tabindex: "0", "aria-expanded": b.open === it.id ? "true" : "false" },
      h("span", { class: `st ${st}` }),
      h("div", null, h("div", { class: "bt", text: w ? w.title : (it.status === "fail" ? `Nothing found (${it.error || "?"})` : it.label) }),
        h("div", { class: "bq", text: it.fav ? `${it.fav.cite_key}: ${it.label}` : it.label })),
      h("span", { class: "bk" + (up && !it.replaced ? " up" : ""), text: tag }));
    const toggle = () => { b.open = b.open === it.id ? null : it.id; renderBatch(); };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    const li = h("li", null, row);
    if (b.open === it.id && w) {
      const s = it.search;
      const cur = s.works.find((x) => x.id === s.current) || w;
      const det = h("div", { class: "bdetail" });
      if (up && !it.replaced) {
        det.append(h("div", { class: "card-actions" },
          h("button", { class: "btn small primary", text: "Replace the favorite with this version",
            onclick: () => replaceFavs([[it, up]]) }),
          h("span", { class: "muted", text: `key kept: ${it.fav.cite_key}` })));
      }
      det.append(renderWork(cur, s));
      if (s.works.length > 1) {
        det.append(h("div", { class: "card-actions" }, h("span", { class: "muted", text: "Not the right one?" }),
          ...s.works.filter((x) => x.id !== cur.id).slice(0, 4).map((x) => h("button", {
            class: "btn small ghost", text: x.title.slice(0, 60) + (x.title.length > 60 ? "…" : ""),
            onclick: () => { s.current = x.id; s.works = [x, ...s.works.filter((y) => y.id !== x.id)]; renderBatch(); },
          }))));
      }
      li.append(det);
    }
    ul.append(li);
  }
  $("#results").replaceChildren(h("section", { class: "batch" }, head, ul));
}

// ------------------------------------------------------------------ enregistrer (choix du dossier)
let popEl = null;
function closePop() { if (popEl) { popEl.remove(); popEl = null; } }
document.addEventListener("mousedown", (e) => { if (popEl && !popEl.contains(e.target)) closePop(); });
window.addEventListener("resize", closePop);

function openPopAt(anchor, el) {
  closePop();
  popEl = el; el.classList.add("pop");
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth, hgt = el.offsetHeight;
  let left = Math.min(r.left, window.innerWidth - w - 8);
  let top = r.bottom + 4;
  if (top + hgt > window.innerHeight - 8) top = Math.max(8, r.top - hgt - 4);
  el.style.left = Math.max(8, left) + "px"; el.style.top = top + "px";
}

function flatFolders() {
  const byParent = {};
  for (const f of state.folders) (byParent[f.parent_id ?? "root"] ||= []).push(f);
  const out = [];
  const walk = (pid, depth, path) => {
    for (const f of byParent[pid] || []) {
      const p = path ? `${path} / ${f.name}` : f.name;
      out.push({ ...f, depth, path: p });
      walk(f.id, depth + 1, p);
    }
  };
  walk("root", 0, "");
  return out;
}

function openSavePop(anchor, getText, title, onPick) {
  const el = h("div", { role: "dialog", "aria-label": "Save to a folder" });
  const input = h("input", { placeholder: "Folder… or a new folder name", "aria-label": "Filter folders" });
  const list = h("div");
  el.append(input, list);
  const last = store.get("lastFolder", null);
  const pick = async (folderId, folderName) => {
    closePop();
    store.set("lastFolder", folderId);
    if (onPick) return onPick(folderId, folderName);
    await saveFavorite(folderId, folderName, getText());
  };
  const draw = () => {
    const term = input.value.trim().toLowerCase();
    const opts = [];
    const flat = flatFolders().filter((f) => !term || f.path.toLowerCase().includes(term));
    if (!term) {
      const lastF = state.folders.find((f) => f.id === last);
      if (lastF) opts.push(h("button", { class: "opt", onclick: () => pick(lastF.id, lastF.name) }, `Last used: ${lastF.name}`));
      opts.push(h("button", { class: "opt", onclick: () => pick(null, "Unfiled") }, "Unfiled"));
      if (opts.length) opts.push(h("div", { class: "sep" }));
    }
    for (const f of flat) {
      opts.push(h("button", { class: "opt", style: `padding-left:${0.5 + f.depth * 0.9}rem`, title: f.path, onclick: () => pick(f.id, f.name) },
        f.name, h("span", { class: "c", text: f.total })));
    }
    if (term && !flat.some((f) => f.name.toLowerCase() === term)) {
      opts.push(h("button", {
        class: "opt", onclick: async () => {
          try {
            const r = await api("POST", "/api/folders", { name: input.value.trim(), parent_id: null });
            await loadFolders();
            pick(r.id, input.value.trim());
          } catch (err) { toast(err.message, { error: true }); }
        },
      }, `Create "${input.value.trim()}" and save`));
    }
    list.replaceChildren(...opts);
  };
  input.addEventListener("input", draw);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); const first = $(".opt", list); if (first) first.click(); }
  });
  draw();
  openPopAt(anchor, el);
  input.focus();
}

async function saveFavorite(folderId, folderName, text, force = false) {
  try {
    const r = await api("POST", "/api/favorites", { folder_id: folderId, bibtex: text, force });
    if (r.duplicate) {
      toast(`Already in "${folderName}"`, { action: "Save anyway", onAction: () => saveFavorite(folderId, folderName, text, true) });
      return;
    }
    await loadFolders();
    toast(`Saved to "${folderName}"`, { action: "View", onAction: () => { location.hash = `#folder/${folderId ?? "inbox"}`; } });
  } catch (err) { toast(err.message, { error: true }); }
}

// ------------------------------------------------------------------ dossiers (barre latérale)
async function loadFolders() {
  try {
    const d = await api("GET", "/api/folders");
    state.folders = d.folders; state.counts = { inbox: d.inbox, all: d.all };
  } catch { /* garde l'état */ }
  renderTree();
  if (state.view === "folder") renderFolderHead();
}

const FOLDER_COLORS = ["yellow", "pink", "green", "blue", "orange", "violet"];
const COLOR_NAMES = { "": "No color", yellow: "Yellow", pink: "Pink", green: "Green", blue: "Blue", orange: "Orange", violet: "Violet" };

// Nom d'un dossier, surligné de sa couleur.
function folderLabel(f) {
  return h("span", { class: f.color ? `mark c-${f.color}` : null, text: f.name });
}

function swatches(current, onPick) {
  return h("div", { class: "swatches", role: "radiogroup", "aria-label": "Folder color" },
    ["", ...FOLDER_COLORS].map((c) => h("button", {
      type: "button", class: `swatch${c ? ` c-${c}` : ""}${c === current ? " on" : ""}`, role: "radio",
      "aria-checked": c === current ? "true" : "false", "aria-label": COLOR_NAMES[c], title: COLOR_NAMES[c], text: c ? "" : "∅",
      onclick: (e) => { e.preventDefault(); onPick(c); },
    })));
}

const folderById = (id) => state.folders.find((f) => f.id === id);
// fid est-il ancestorId lui-même ou l'un de ses sous-dossiers ?
function isInside(fid, ancestorId) {
  for (let cur = folderById(fid); cur; cur = folderById(cur.parent_id)) if (cur.id === ancestorId) return true;
  return false;
}

async function moveFolder(f, parentId) {
  if (parentId === f.parent_id || (parentId !== null && isInside(parentId, f.id))) return;
  try {
    await api("PATCH", `/api/folders/${f.id}`, { parent_id: parentId });
    if (parentId) { state.expanded.add(parentId); store.set("expanded", [...state.expanded]); }
    await loadFolders();
    toast(parentId ? `Moved "${f.name}" into "${folderById(parentId)?.name}"` : `Moved "${f.name}" to the top level`);
  } catch (err) { toast(err.message, { error: true }); }
}

let draggedFolder = null;   // dossier glissé dans l'arborescence

function renderTree() {
  const ul = $("#tree");
  const byParent = {};
  for (const f of state.folders) (byParent[f.parent_id ?? "root"] ||= []).push(f);
  const items = [];
  const active = (id) => state.view === "folder" && String(state.folderId) === String(id);
  const row = (id, name, count, depth, opts = {}) => {
    const f = opts.folder;
    const r = h("div", {
      class: "row" + (active(id) ? " active" : ""), style: `--depth:${depth}`, tabindex: "0", role: "button",
      dataset: { folder: String(id) }, draggable: f ? "true" : null,
    });
    if (opts.children) {
      const open = state.expanded.has(id);
      r.append(h("button", {
        class: "twisty", "aria-label": open ? "Collapse" : "Expand", text: open ? "▾" : "▸",
        onclick: (e) => { e.stopPropagation(); open ? state.expanded.delete(id) : state.expanded.add(id); store.set("expanded", [...state.expanded]); renderTree(); },
      }));
    } else r.append(h("span", { class: "twisty" }));
    const label = h("span", { class: "name", title: f ? "Double-click to rename" : null }, f ? folderLabel(f) : name);
    r.append(label, h("span", { class: "count", text: count || "" }));
    if (f) {
      r.append(h("span", { class: "tools" },
        h("button", { class: "icon-btn", text: "+", title: "New subfolder", "aria-label": `New subfolder in ${f.name}`,
          onclick: (e) => { e.stopPropagation(); newFolder(e.currentTarget, f); } }),
        h("button", { class: "icon-btn", text: "⋯", "aria-label": `Actions for ${f.name}`,
          onclick: (e) => { e.stopPropagation(); folderMenu(e.currentTarget, f); } })));
      label.addEventListener("dblclick", (e) => { e.stopPropagation(); renameInline(r, f); });
      r.addEventListener("dragstart", (e) => {
        draggedFolder = f; e.dataTransfer.setData("text/x-cite-folder", String(f.id)); e.dataTransfer.effectAllowed = "move";
        r.classList.add("dragging");
      });
      r.addEventListener("dragend", () => { draggedFolder = null; r.classList.remove("dragging"); });
    }
    r.addEventListener("click", () => { location.hash = `#folder/${id}`; });
    r.addEventListener("keydown", (e) => { if (e.key === "Enter") location.hash = `#folder/${id}`; });
    // On y dépose des favoris (partout sauf « Tous les favoris ») ou un dossier (dans un autre dossier).
    r.addEventListener("dragover", (e) => {
      const favs = id !== "all" && e.dataTransfer.types.includes("text/x-cite-favs");
      const folder = draggedFolder && f && !isInside(f.id, draggedFolder.id);
      if (favs || folder) { e.preventDefault(); r.classList.add("drop"); }
    });
    r.addEventListener("dragleave", () => r.classList.remove("drop"));
    r.addEventListener("drop", async (e) => {
      r.classList.remove("drop");
      const favs = e.dataTransfer.getData("text/x-cite-favs");
      if (favs && id !== "all") { e.preventDefault(); e.stopPropagation(); await moveFavs(JSON.parse(favs), id === "inbox" ? null : id, name); }
      else if (draggedFolder && f) { e.preventDefault(); e.stopPropagation(); await moveFolder(draggedFolder, f.id); }
    });
    return h("li", null, r);
  };
  items.push(row("all", "All favorites", state.counts.all, 0));
  items.push(row("inbox", "Unfiled", state.counts.inbox, 0));
  const walk = (pid, depth) => {
    for (const f of byParent[pid] || []) {
      const kids = byParent[f.id];
      items.push(row(f.id, f.name, f.total, depth, { folder: f, children: !!kids }));
      if (kids && state.expanded.has(f.id)) walk(f.id, depth + 1);
    }
  };
  walk("root", 0);
  ul.replaceChildren(...items);
  $("#nav-search").classList.toggle("active", state.view === "search");
}

// Glisser un dossier sur le titre « Favoris » le remet à la racine.
{
  const title = $("#folder-add").closest(".side-title");
  title.addEventListener("dragover", (e) => { if (draggedFolder && draggedFolder.parent_id !== null) { e.preventDefault(); title.classList.add("drop"); } });
  title.addEventListener("dragleave", () => title.classList.remove("drop"));
  title.addEventListener("drop", async (e) => { title.classList.remove("drop"); if (draggedFolder) { e.preventDefault(); await moveFolder(draggedFolder, null); } });
}

function renameInline(r, f) {
  const label = r.querySelector(".name");
  const input = h("input", { class: "rename", value: f.name, "aria-label": "Folder name", maxlength: "120" });
  label.replaceWith(input);
  input.focus(); input.select();
  let finished = false;
  const finish = async (commit) => {
    if (finished) return;
    finished = true;
    const name = input.value.trim();
    if (commit && name && name !== f.name) {
      try { await api("PATCH", `/api/folders/${f.id}`, { name }); } catch (err) { toast(err.message, { error: true }); }
    }
    await loadFolders();
  };
  input.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("blur", () => finish(true));
}

// Nouveau dossier, à la racine ou dans parent : un nom et une couleur (celle du parent par défaut).
function newFolder(anchor, parent = null) {
  let color = parent ? parent.color || "" : "";
  const input = h("input", { placeholder: parent ? `Subfolder of ${parent.name}` : "Folder name", "aria-label": "Folder name", maxlength: "120" });
  const picker = h("div");
  const drawPicker = () => picker.replaceChildren(swatches(color, (c) => { color = c; drawPicker(); input.focus(); }));
  drawPicker();
  const el = h("form", { "aria-label": parent ? `New subfolder in ${parent.name}` : "New folder" },
    input, h("div", { class: "swatch-label", text: "Color" }), picker,
    h("div", { class: "card-actions" }, h("button", { class: "btn small primary", type: "submit", text: "Create" }),
      h("button", { class: "btn small ghost", type: "button", text: "Cancel", onclick: () => closePop() })));
  el.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    closePop();
    try {
      const r = await api("POST", "/api/folders", { name, parent_id: parent ? parent.id : null, color });
      if (parent) { state.expanded.add(parent.id); store.set("expanded", [...state.expanded]); }
      await loadFolders();
      location.hash = `#folder/${r.id}`;
    } catch (err) { toast(err.message, { error: true }); }
  });
  openPopAt(anchor, el);
  input.focus();
}

function promptPop(anchor, label, initial = "") {
  return new Promise((resolve) => {
    const el = h("form", { "aria-label": label });
    const input = h("input", { value: initial, placeholder: label, "aria-label": label, maxlength: "120" });
    el.append(input, h("div", { class: "card-actions" }, h("button", { class: "btn small primary", type: "submit", text: "OK" }),
      h("button", { class: "btn small ghost", type: "button", text: "Cancel", onclick: () => { closePop(); resolve(null); } })));
    el.addEventListener("submit", (e) => { e.preventDefault(); const v = input.value.trim(); closePop(); resolve(v || null); });
    openPopAt(anchor, el);
    input.focus(); input.select();
  });
}

$("#folder-add").addEventListener("click", (e) => newFolder(e.currentTarget));

function folderMenu(anchor, f) {
  const el = h("div", { role: "menu" });
  el.append(
    h("div", { class: "swatch-label", text: "Color" }),
    swatches(f.color || "", async (c) => {
      closePop();
      try { await api("PATCH", `/api/folders/${f.id}`, { color: c }); await loadFolders(); } catch (err) { toast(err.message, { error: true }); }
    }),
    h("div", { class: "sep" }),
    h("button", { class: "opt", text: "New subfolder", onclick: () => newFolder(anchor, f) }),
    h("button", { class: "opt", text: "Rename", onclick: async () => {
      const name = await promptPop(anchor, "New name", f.name);
      if (!name || name === f.name) return;
      try { await api("PATCH", `/api/folders/${f.id}`, { name }); await loadFolders(); } catch (err) { toast(err.message, { error: true }); }
    } }),
    h("button", { class: "opt", text: "Move to…", onclick: () => moveFolderPop(anchor, f) }),
    h("div", { class: "sep" }),
    h("button", { class: "opt danger", text: "Delete", onclick: () => { closePop(); deleteFolder(f); } }),
  );
  openPopAt(anchor, el);
}

function moveFolderPop(anchor, f) {
  const el = h("div");
  const forbidden = new Set([f.id]);
  let grew = true;
  while (grew) { grew = false; for (const x of state.folders) if (forbidden.has(x.parent_id) && !forbidden.has(x.id)) { forbidden.add(x.id); grew = true; } }
  el.append(h("button", { class: "opt", text: "Top level", onclick: () => doMove(null) }));
  for (const x of flatFolders()) {
    if (forbidden.has(x.id)) continue;
    el.append(h("button", { class: "opt", style: `padding-left:${0.5 + x.depth * 0.9}rem`, text: x.name, onclick: () => doMove(x.id) }));
  }
  async function doMove(pid) {
    closePop();
    try { await api("PATCH", `/api/folders/${f.id}`, { parent_id: pid }); if (pid) state.expanded.add(pid); await loadFolders(); }
    catch (err) { toast(err.message, { error: true }); }
  }
  openPopAt(anchor, el);
}

async function deleteFolder(f) {
  try {
    const r = await api("DELETE", `/api/folders/${f.id}`);
    if (String(state.folderId) === String(f.id)) location.hash = "#folder/all";
    await loadFolders();
    if (state.view === "folder") loadFavs();
    toast(`Deleted folder "${f.name}"`, { action: "Undo", onAction: () => undo(r.undo) });
  } catch (err) { toast(err.message, { error: true }); }
}

async function undo(token) {
  try { await api("POST", "/api/undo", { token }); await loadFolders(); if (state.view === "folder") loadFavs(); toast("Restored"); }
  catch (err) { toast(err.message, { error: true }); }
}

// ------------------------------------------------------------------ vue dossier
function folderName(id) {
  if (id === "all") return "All favorites";
  if (id === "inbox") return "Unfiled";
  return state.folders.find((f) => String(f.id) === String(id))?.name || "Folder";
}

function openFolder(id, push = true) {
  state.view = "folder";
  state.folderId = id;
  state.selected.clear(); state.openFav = null; state.editFav = null;
  $("#view-search").hidden = true; $("#view-folder").hidden = false;
  if (push) location.hash = `#folder/${id}`;
  $("#fav-filter").value = "";
  renderTree(); renderFolderHead(); loadFavs(); updateHome();
  closeDrawer();
}

function renderFolderHead() {
  const id = state.folderId;
  const crumbs = $("#crumbs");
  const nodes = [];
  if (id !== "all" && id !== "inbox") {
    const chain = [];
    let cur = state.folders.find((f) => String(f.id) === String(id));
    while (cur) { chain.unshift(cur); cur = state.folders.find((f) => f.id === cur.parent_id); }
    nodes.push(h("button", { text: "Favorites", onclick: () => { location.hash = "#folder/all"; } }), h("span", { class: "sep", text: "/" }));
    chain.forEach((f, i) => {
      if (i === chain.length - 1) nodes.push(h("span", { class: "cur" }, folderLabel(f)));
      else nodes.push(h("button", { text: f.name, onclick: () => { location.hash = `#folder/${f.id}`; } }), h("span", { class: "sep", text: "/" }));
    });
  } else nodes.push(h("span", { class: "cur", text: folderName(id) }));
  crumbs.replaceChildren(...nodes);

  const isFolder = id !== "all" && id !== "inbox";
  const recursive = $("#fav-recursive").checked ? 1 : 0;
  const f = isFolder ? state.folders.find((x) => String(x.id) === String(id)) : null;
  const acts = [
    h("button", { class: "btn small", text: "Export ▾", onclick: (e) => exportMenu(e.currentTarget, id, recursive) }),
    h("button", { class: "btn small ghost", text: "Published versions?", onclick: () => recheckFolder(),
      title: "Searches again for each preprint in this list and offers the published version when there is one" }),
    h("button", { class: "btn small ghost", text: "Import", title: "Import a .bib file", onclick: () => importBib(id) }),
  ];
  if (f) acts.push(h("button", { class: "btn small ghost", text: "⋯", "aria-label": "Folder actions", onclick: (e) => folderMenu(e.currentTarget, f) }));
  $("#folder-actions").replaceChildren(...acts);
  $("#fav-recursive").closest("label").hidden = !isFolder;

  // Sous-dossiers du dossier ouvert (ou dossiers de premier niveau dans « Tous les favoris »).
  const kids = id === "inbox" ? [] : state.folders.filter((x) => (f ? x.parent_id === f.id : id === "all" && x.parent_id === null));
  const strip = $("#subfolders");
  strip.replaceChildren(...kids.map((k) => h("button", { onclick: () => { location.hash = `#folder/${k.id}`; } },
    h("span", { class: `sw${k.color ? ` c-${k.color}` : ""}` }), k.name, h("span", { class: "n", text: k.total || "" }))));
  if (f) strip.append(h("button", { class: "add", text: "+ Subfolder", onclick: (e) => newFolder(e.currentTarget, f) }));
  strip.hidden = !strip.childNodes.length;
}

function exportMenu(anchor, id, recursive) {
  const q = `folder=${id}&recursive=${recursive}`;
  openPopAt(anchor, h("div", { role: "menu" },
    h("a", { class: "opt", href: `/api/export?${q}`, text: ".bib file", onclick: () => closePop() }),
    h("a", { class: "opt", href: `/api/export?${q}&format=csl`, text: "CSL-JSON (Zotero, Pandoc)", onclick: () => closePop() }),
    h("button", { class: "opt", text: "Copy all BibTeX", onclick: async () => {
      closePop();
      const txt = await api("GET", `/api/export?${q}`);
      if (await copyText(txt)) toast("Folder BibTeX copied");
    } })));
}

let filterTimer = null;
$("#fav-filter").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadFavs, 200); });
$("#fav-recursive").addEventListener("change", () => { renderFolderHead(); loadFavs(); });

async function loadFavs() {
  const id = state.folderId;
  const q = $("#fav-filter").value.trim();
  const recursive = $("#fav-recursive").checked ? 1 : 0;
  try {
    const d = await api("GET", `/api/favorites?folder=${id}&q=${encodeURIComponent(q)}&recursive=${recursive}`);
    if (state.folderId !== id) return;
    state.favs = d.favorites; state.favsTruncated = !!d.truncated;
  } catch (err) { toast(err.message, { error: true }); state.favs = []; state.favsTruncated = false; }
  renderFavs();
}

// Prépublication : identifiant arXiv, revue « arXiv preprint... », dépôt de prépublications.
function isPreprintFav(f) {
  return !!f.arxiv || f.entry_type === "unpublished"
    || /\b(arxiv|corr|preprint|biorxiv|medrxiv|ssrn|research square|techrxiv|openreview)\b/i.test(f.venue || "");
}

function recheckFolder() {
  const favs = state.favs.filter(isPreprintFav);
  if (!favs.length) { toast("No preprint in this list"); return; }
  showSearchView();
  qEl.value = ""; autosize();
  startBatch(favs.slice(0, 200).map((f) => ({ text: f.bibtex, label: f.title || f.cite_key, fav: f })), { recheck: true });
}

function renderFavs() {
  const ul = $("#favs");
  if (!state.favs.length) {
    const q = $("#fav-filter").value.trim();
    ul.replaceChildren(h("li", { class: "empty-folder", text: q ? "No favorite matches this filter." :
      "Empty folder. Save results here from a search, import a .bib file, or drag favorites in from another folder." }));
    renderBulk();
    return;
  }
  const keyCount = {};
  for (const f of state.favs) if (f.cite_key) keyCount[f.cite_key.toLowerCase()] = (keyCount[f.cite_key.toLowerCase()] || 0) + 1;
  ul.replaceChildren(...state.favs.map((f) => {
    const li = h("li", { class: "fav", draggable: "true" });
    li.addEventListener("dragstart", (e) => {
      const ids = state.selected.has(f.id) ? [...state.selected] : [f.id];
      e.dataTransfer.setData("text/x-cite-favs", JSON.stringify(ids));
      e.dataTransfer.effectAllowed = "move";
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    const cb = h("input", { type: "checkbox", "aria-label": "Select", checked: state.selected.has(f.id) });
    cb.addEventListener("change", () => { cb.checked ? state.selected.add(f.id) : state.selected.delete(f.id); renderBulk(); });
    const dup = f.cite_key && keyCount[f.cite_key.toLowerCase()] > 1;
    const main = h("div", { class: "fmain", role: "button", tabindex: "0", "aria-expanded": state.openFav === f.id ? "true" : "false" },
      h("div", { class: "fkey" + (dup ? " dup" : ""), text: f.cite_key + (dup ? "  (duplicate key, suffixed on export)" : "") }),
      h("div", { class: "ftitle", text: f.title || "(untitled)" }),
      h("div", { class: "fmeta" }, [f.authors.split("; ").slice(0, 3).join(", ") + (f.authors.split("; ").length > 3 ? " et al." : ""), f.venue, f.year].filter(Boolean).join(", "),
        // Hors de son dossier (« Tous les favoris », sous-dossiers inclus), le favori montre où il est rangé.
        f.folder_id && String(f.folder_id) !== String(state.folderId) && folderById(f.folder_id)
          ? h("span", { class: "ffolder" }, folderLabel(folderById(f.folder_id))) : null));
    const toggle = () => { state.openFav = state.openFav === f.id ? null : f.id; state.editFav = null; renderFavs(); };
    main.addEventListener("click", toggle);
    main.addEventListener("keydown", (e) => { if (e.key === "Enter") toggle(); });
    const copy = h("button", { class: "btn small ghost", text: "Copy" });
    copy.addEventListener("click", async () => { if (await copyText(f.bibtex)) flashButton(copy, "Copied"); });
    const more = h("button", { class: "icon-btn", text: "⋯", "aria-label": "Actions" });
    more.addEventListener("click", () => favMenu(more, f));
    li.append(h("div", { class: "frow" }, cb, main, h("div", { class: "fact" }, copy, more)));
    if (state.openFav === f.id) li.append(renderFavDetail(f));
    return li;
  }));
  if (state.favsTruncated) {
    ul.append(h("li", { class: "empty-folder", text: `Showing only the ${fmtInt(state.favs.length)} most recent favorites: narrow down with the filter. Exports include everything.` }));
  }
  renderBulk();
}

function renderFavDetail(f) {
  const det = h("div", { class: "fdetail" });
  if (state.editFav === f.id) {
    const ta = h("textarea", { spellcheck: "false", "aria-label": "BibTeX" });
    ta.value = f.bibtex;
    const note = h("input", { class: "fnote", placeholder: "Personal note (optional)", value: f.note || "" });
    const save = h("button", { class: "btn small primary", text: "Save changes" });
    save.addEventListener("click", async () => {
      try { await api("PATCH", `/api/favorites/${f.id}`, { bibtex: ta.value, note: note.value }); state.editFav = null; await loadFavs(); toast("Updated"); }
      catch (err) { toast(err.message, { error: true }); }
    });
    det.append(ta, note, h("div", { class: "bar" }, save, h("button", { class: "btn small ghost", text: "Cancel", onclick: () => { state.editFav = null; renderFavs(); } })));
    setTimeout(() => ta.focus(), 0);
  } else {
    det.append(staticCard(f.bibtex));
    if (f.note) det.append(h("p", { class: "muted", text: f.note }));
    const bar = h("div", { class: "bar" },
      h("button", { class: "btn small", text: "Edit", onclick: () => { state.editFav = f.id; renderFavs(); } }),
      h("button", { class: "btn small ghost", text: "Check online again", title: "Search again with this entry to see if there is a newer version", onclick: () => { qEl.value = f.bibtex; autosize(); showSearchView(); startSearch({ text: f.bibtex }); } }));
    det.append(bar);
  }
  return det;
}

function staticCard(text) {
  const lines = text.trim().split("\n");
  const card = h("div", { class: "bibcard" });
  card.append(h("div", { class: "head" }, h("span", { class: "etype", text: lines[0] })));
  const body = h("div", { class: "body" });
  for (const line of lines.slice(1)) {
    const m = line.match(/^(\s*)([A-Za-z_]+)(\s*=\s*)(.*)$/);
    if (m) body.append(h("div", { class: "ln" }, "  ", h("span", { class: "f", text: m[2] }), h("span", { class: "eq", text: m[3] }), ...valueNodes(m[4])));
    else body.append(h("div", { class: "ln last", text: line }));
  }
  card.append(body);
  return card;
}

function favMenu(anchor, f) {
  const el = h("div", { role: "menu" },
    h("button", { class: "opt", text: "Edit", onclick: () => { closePop(); state.openFav = f.id; state.editFav = f.id; renderFavs(); } }),
    h("button", { class: "opt", text: "Move to…", onclick: () => movePop(anchor, [f.id]) }),
    h("button", { class: "opt", text: "Copy key", onclick: async () => { closePop(); if (await copyText(f.cite_key)) toast("Key copied"); } }),
    h("div", { class: "sep" }),
    h("button", { class: "opt danger", text: "Delete", onclick: () => { closePop(); deleteFavs([f.id]); } }),
  );
  openPopAt(anchor, el);
}

function movePop(anchor, ids) {
  openSavePop(anchor, null, null, (folderId, name) => moveFavs(ids, folderId, name));
}

async function moveFavs(ids, folderId, name) {
  try {
    await api("POST", "/api/favorites/bulk", { action: "move", ids, folder_id: folderId });
    state.selected.clear();
    await loadFolders(); if (state.view === "folder") await loadFavs();
    toast(`Moved ${plural(ids.length, "favorite", "favorites")} to "${name}"`);
  } catch (err) { toast(err.message, { error: true }); }
}

async function deleteFavs(ids) {
  try {
    const r = await api("POST", "/api/favorites/bulk", { action: "delete", ids });
    state.selected.clear();
    await loadFolders(); await loadFavs();
    toast(`Deleted ${plural(ids.length, "favorite", "favorites")}`, { action: "Undo", onAction: () => undo(r.undo) });
  } catch (err) { toast(err.message, { error: true }); }
}

function renderBulk() {
  const bar = $("#bulkbar");
  const n = state.selected.size;
  if (!n) { bar.hidden = true; return; }
  bar.hidden = false;
  const ids = [...state.selected];
  const move = h("button", { class: "btn small", text: "Move…" });
  move.addEventListener("click", () => movePop(move, ids));
  const copy = h("button", { class: "btn small", text: "Copy" });
  copy.addEventListener("click", async () => {
    const txt = await api("GET", `/api/export?ids=${ids.join(",")}`);
    if (await copyText(txt)) flashButton(copy, "Copied");
  });
  bar.replaceChildren(
    h("span", { class: "grow", text: `${n} selected` }),
    copy, h("a", { class: "btn small", href: `/api/export?ids=${ids.join(",")}`, text: "Export .bib" }),
    h("a", { class: "btn small ghost", href: `/api/export?ids=${ids.join(",")}&format=csl`, text: "CSL-JSON" }), move,
    h("button", { class: "btn small danger", text: "Delete", onclick: () => deleteFavs(ids) }),
    h("button", { class: "btn small ghost", text: "Select all", onclick: () => { state.favs.forEach((f) => state.selected.add(f.id)); renderFavs(); } }),
    h("button", { class: "btn small ghost", text: "None", onclick: () => { state.selected.clear(); renderFavs(); } }),
  );
}

function importBib(id) {
  const input = h("input", { type: "file", accept: ".bib,.txt,text/plain", style: "display:none" });
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    const text = await file.text();
    try {
      const r = await api("POST", "/api/import", { folder_id: id === "all" ? null : id, bibtex: text });
      await loadFolders(); await loadFavs();
      toast(`Imported ${r.added} of ${plural(r.total, "entry", "entries")}${r.duplicates ? `, ${r.duplicates} already there` : ""}`);
    } catch (err) { toast(err.message, { error: true }); }
  });
  document.body.append(input);
  input.click();
  setTimeout(() => input.remove(), 60000);
}

// ------------------------------------------------------------------ réglages
const dlg = $("#settings-dialog");
const settingsForm = $("#settings-form");

$("#open-settings").addEventListener("click", () => {
  const st = state.settings;
  for (const el of settingsForm.elements) {
    if (!el.name || !(el.name in st)) continue;
    if (el.type === "checkbox") el.checked = !!st[el.name];
    else el.value = st[el.name];
  }
  $("#settings-status").textContent = "";
  syncTheme();
  dlg.showModal();
  closeDrawer();
  loadKeys();
});
$("#settings-close").addEventListener("click", () => dlg.close());

// Un clic dans le vide autour du panneau le ferme. Il faut appuyer et relâcher dehors :
// sélectionner le texte d'un champ en finissant sur le fond ne ferme rien.
const outsideDialog = (e) => {
  const r = dlg.getBoundingClientRect();
  return e.target === dlg && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom);
};
let pressedOutside = false;
dlg.addEventListener("pointerdown", (e) => { pressedOutside = outsideDialog(e); });
dlg.addEventListener("click", (e) => { if (pressedOutside && outsideDialog(e)) dlg.close(); pressedOutside = false; });

// Les réglages BibTeX s'enregistrent à chaque changement, et les résultats affichés sont refaits.
let saveTimer = null, savedTimer = null;
function readSettings() {
  const data = {};
  for (const el of settingsForm.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") data[el.name] = el.checked;
    else if (el.type === "number") data[el.name] = parseInt(el.value, 10) || 100;
    else data[el.name] = el.value;
  }
  return data;
}
async function saveSettings() {
  clearTimeout(saveTimer); saveTimer = null;
  const status = $("#settings-status");
  try {
    state.settings = await api("PUT", "/api/settings", readSettings());
    status.textContent = "Saved";
    clearTimeout(savedTimer); savedTimer = setTimeout(() => { status.textContent = ""; }, 1800);
    await rerenderBibtex();
  } catch (err) { status.textContent = err.message; }
}
settingsForm.addEventListener("change", (e) => {
  if (!e.target.name) return;   // clés d'API et mot de passe ont leurs propres boutons
  clearTimeout(saveTimer); saveTimer = setTimeout(saveSettings, 200);
});
settingsForm.addEventListener("submit", (e) => e.preventDefault());
dlg.addEventListener("close", () => { if (saveTimer) saveSettings(); });

// Clés d'API (config.json) : le serveur ne renvoie jamais que leurs 4 derniers caractères.
const KEY_SERVICES = {
  serpapi: { label: "SerpAPI", help: "Google Scholar without captchas, 250 free searches a month" },
  s2: { label: "Semantic Scholar", help: "Faster, steadier Semantic Scholar lookups" },
  openalex: { label: "OpenAlex", help: "Own daily budget instead of the one shared by your network" },
};

async function loadKeys() {
  const box = $("#keys-list");
  box.replaceChildren(h("p", { class: "muted", text: "Loading…" }));
  try { renderKeys(await api("GET", "/api/keys")); }
  catch (err) { box.replaceChildren(h("p", { class: "muted", text: err.message })); }
}

// Plusieurs clés par service, essayées dans l'ordre : si l'une est refusée ou
// épuisée, la suivante prend le relais.
function renderKeys(data) {
  const all = data.keys || {};
  $("#keys-list").replaceChildren(...Object.entries(KEY_SERVICES).map(([service, meta]) => {
    const keys = all[service] || [];
    const msg = h("span", { class: "muted" });
    const lines = keys.map((k, i) => {
      const bits = [`Key ${k.hint}`];
      if (k.quota !== undefined && k.quota !== null) bits.push(`${fmtInt(k.quota)} searches left this month`);
      if (k.budget !== undefined && k.budget !== null) bits.push(`$${k.budget.toFixed(2)} left today`);
      if (k.status) bits.push(k.status);
      return h("li", { class: "keyline" + (/rejected/.test(k.status || "") ? " bad" : "") },
        keys.length > 1 ? h("span", { class: "keyrank", text: `${i + 1}.` }) : null,
        h("span", { class: "keystate", text: bits.join(" · ") }),
        h("button", { type: "button", class: "link tiny", text: "Remove", "aria-label": `Remove ${meta.label} key ${k.hint}`,
          onclick: async () => {
            try { renderKeys(await api("DELETE", `/api/keys/${service}/${k.id}`)); toast(`${meta.label} key removed`); }
            catch (err) { msg.textContent = err.message; }
          } }));
    });
    let edit = null;
    if (keys.length < (data.max || 5)) {
      const input = h("input", { type: "password", autocomplete: "off", spellcheck: "false", "aria-label": `New ${meta.label} key`,
        placeholder: keys.length ? "Add a backup key" : "Paste your key" });
      const add = h("button", { type: "button", class: "btn small", text: "Add" });
      add.addEventListener("click", async () => {
        if (!input.value.trim()) return;
        add.disabled = true; msg.textContent = "Checking…";
        try { renderKeys(await api("POST", `/api/keys/${service}`, { value: input.value.trim() })); toast(`${meta.label} key added`); }
        catch (err) { msg.textContent = err.message; add.disabled = false; }
      });
      // Entrée enregistre la clé au lieu de soumettre le formulaire.
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add.click(); } });
      edit = h("div", { class: "keyedit" }, input, add, msg);
    }
    return h("div", { class: "keyrow" },
      h("div", { class: "keyhead" }, h("b", { text: meta.label }), h("span", { class: "muted", text: meta.help })),
      lines.length ? h("ol", { class: "keylist" }, lines) : null,
      keys.length > 1 ? h("p", { class: "keynote muted", text: "Tried in this order: if one fails, the next takes over." }) : null,
      edit || msg);
  }));
}

// ------------------------------------------------------------------ thème
// Choix propre à l'appareil : « system » suit le système, « light » et « dark » l'imposent.
// prefs.js applique le choix enregistré avant le premier affichage.
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function themeChoice() { const t = store.get("theme", null); return t === "light" || t === "dark" ? t : "system"; }
function isDark() { const c = themeChoice(); return c === "system" ? darkQuery.matches : c === "dark"; }
function setTheme(choice) {
  store.set("theme", choice === "system" ? null : choice);
  if (choice === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
  syncTheme();
}
function syncTheme() {
  const c = themeChoice();
  for (const b of $$("#theme-choice [data-choice]")) b.setAttribute("aria-checked", String(b.dataset.choice === c));
  for (const m of $$('meta[name="theme-color"]')) {
    const dark = c === "system" ? m.media.includes("dark") : c === "dark";
    m.content = dark ? "#17181d" : "#fafaf7";
  }
  $("#theme-toggle").title = isDark() ? "Switch to light theme" : "Switch to dark theme";
}
$("#theme-toggle").addEventListener("click", () => setTheme(isDark() ? "light" : "dark"));
$("#theme-choice").addEventListener("click", (e) => { const b = e.target.closest("[data-choice]"); if (b) setTheme(b.dataset.choice); });
darkQuery.addEventListener("change", syncTheme);
syncTheme();

// ------------------------------------------------------------------ largeur de la barre latérale
const SIDE_MIN = 200, SIDE_MAX = 480;
const resizer = $("#side-resize");
const sideWidth = () => Math.round($("#side").getBoundingClientRect().width);
function setSideWidth(w, save = true) {
  const root = document.documentElement;
  if (w === null) root.style.removeProperty("--side-w");
  else root.style.setProperty("--side-w", Math.round(Math.min(SIDE_MAX, innerWidth * 0.5, Math.max(SIDE_MIN, w))) + "px");
  resizer.setAttribute("aria-valuenow", sideWidth());
  if (save) store.set("sideW", w === null ? null : sideWidth());
}
resizer.setAttribute("aria-valuemin", SIDE_MIN);
resizer.setAttribute("aria-valuemax", SIDE_MAX);
resizer.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  resizer.setPointerCapture(e.pointerId);
  const left = $("#side").getBoundingClientRect().left;
  document.body.classList.add("resizing");
  const move = (ev) => setSideWidth(ev.clientX - left, false);
  const end = () => {
    resizer.removeEventListener("pointermove", move);
    resizer.removeEventListener("pointerup", end);
    resizer.removeEventListener("pointercancel", end);
    document.body.classList.remove("resizing");
    store.set("sideW", sideWidth());
  };
  resizer.addEventListener("pointermove", move);
  resizer.addEventListener("pointerup", end);
  resizer.addEventListener("pointercancel", end);
});
resizer.addEventListener("dblclick", () => setSideWidth(null));
resizer.addEventListener("keydown", (e) => {
  const step = e.shiftKey ? 48 : 16;
  if (e.key === "ArrowLeft") { e.preventDefault(); setSideWidth(sideWidth() - step); }
  if (e.key === "ArrowRight") { e.preventDefault(); setSideWidth(sideWidth() + step); }
});

async function rerenderBibtex() {
  const searches = state.batch ? state.batch.items.map((i) => i.search) : state.search ? [state.search] : [];
  const versions = [];
  for (const s of searches) for (const w of s.works) for (const v of w.versions) versions.push(v);
  if (!versions.length) return;
  for (let i = 0; i < versions.length; i += 200) {
    const chunk = versions.slice(i, i + 200);
    const r = await api("POST", "/api/render", { specs: chunk.map((v) => v.spec), settings: state.settings });
    r.results.forEach((res, j) => { if (res) Object.assign(chunk[j], { bibtex: res.bibtex, key: res.key, prov: res.prov }); });
  }
  for (const s of searches) s.keys = {};
  if (state.batch) renderBatch(); else renderSearch();
}

// ------------------------------------------------------------------ largeur de la notice
// Sur grand écran seulement (au-delà de 1100 px, là où les sources ont leur marge) : la
// notice s'étire ou se resserre par sa poignée, et la largeur est gardée dans
// config.json. En dessous (tablette, téléphone), la mise en page ne change pas.
const CARD_MIN = 480;
const wideScreen = matchMedia("(min-width: 1101px)");

function applyCardWidth() {
  const w = state.settings.card_width || 0;
  if (w) document.documentElement.style.setProperty("--card-w", w + "px");
  else document.documentElement.style.removeProperty("--card-w");
}

async function saveCardWidth(w) {
  try { state.settings = await api("PUT", "/api/settings", { card_width: w }); }
  catch (err) { toast(err.message, { error: true }); }
}

// Largeur possible : jusqu'au bord droit de la zone principale, marge comprise.
function cardMax(wrap) {
  const main = $("#main");
  const right = main.getBoundingClientRect().right - (parseFloat(getComputedStyle(main).paddingRight) || 0);
  return Math.max(CARD_MIN, Math.floor(right - wrap.getBoundingClientRect().left));
}

function setCardWidth(wrap, w, save) {
  w = Math.round(Math.min(cardMax(wrap), Math.max(CARD_MIN, w)));
  document.documentElement.style.setProperty("--card-w", w + "px");
  if (save) { state.settings.card_width = w; saveCardWidth(w); }
  return w;
}

// Un seul écouteur sur les résultats : la fiche est redessinée à chaque réponse d'une
// source, la poignée avec elle ; le glissé suit la fenêtre, pas l'élément.
$("#results").addEventListener("pointerdown", (e) => {
  const grip = e.target.closest(".card-grip");
  if (!grip || e.button !== 0 || !wideScreen.matches) return;
  e.preventDefault();
  const wrap = grip.closest(".card-wrap");
  const left = wrap.getBoundingClientRect().left;
  let w = wrap.getBoundingClientRect().width;
  document.body.classList.add("resizing-card");
  const move = (ev) => { w = setCardWidth(wrap, ev.clientX - left, false); };
  const end = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    document.body.classList.remove("resizing-card");
    setCardWidth(wrap, w, true);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", end);
  window.addEventListener("pointercancel", end);
});
$("#results").addEventListener("dblclick", (e) => {
  if (!e.target.closest(".card-grip")) return;
  state.settings.card_width = 0;
  applyCardWidth();
  saveCardWidth(0);
});
$("#results").addEventListener("keydown", (e) => {
  const grip = e.target.closest(".card-grip");
  if (!grip || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
  e.preventDefault();
  const wrap = grip.closest(".card-wrap");
  const step = (e.shiftKey ? 96 : 32) * (e.key === "ArrowLeft" ? -1 : 1);
  setCardWidth(wrap, wrap.getBoundingClientRect().width + step, true);
});

boot();
