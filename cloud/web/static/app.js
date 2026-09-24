// Genesis онлайн — входът, чатът и файловете. Без библиотеки; всичко, което
// идва от сървъра, се слага с textContent (никога innerHTML) — отговорите и
// имената на файлове идват от модела и от чужд код.
"use strict";

const $ = (id) => document.getElementById(id);
let current = null;       // id на отворения разговор
let polling = null;       // таймерът на текущия ход

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error(data.error || "нужен е вход"); }
  if (!r.ok) throw new Error(data.error || `грешка ${r.status}`);
  return data;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
}

async function start() {
  try {
    const me = await api("/api/me");
    $("me").textContent = me.email;
    $("login").hidden = true;
    $("app").hidden = false;
    await loadChats();
  } catch { showLogin(); }
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  $("login-error").textContent = "";
  try {
    await api("/api/login", { email: f.get("email"), password: f.get("password") });
    ev.target.reset();
    await start();
  } catch (e) { $("login-error").textContent = e.message; }
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", {}).catch(() => {});
  current = null;
  showLogin();
});

async function loadChats() {
  const { chats } = await api("/api/chats");
  const ul = $("chats");
  ul.replaceChildren();
  for (const c of chats) {
    const li = el("li", c.id === current ? "active" : "", c.title);
    li.dataset.id = c.id;
    li.addEventListener("click", () => openChat(c.id));
    ul.append(li);
  }
  if (!current && chats.length) await openChat(chats[0].id);
  if (!chats.length) { $("log").replaceChildren(el("p", "muted", "Напиши задача долу — започва нов разговор.")); $("files").hidden = true; }
}

$("new-chat").addEventListener("click", () => {
  current = null;
  stopPolling();
  $("log").replaceChildren(el("p", "muted", "Нов разговор. Напиши задачата долу."));
  $("files").hidden = true;
  for (const li of $("chats").children) li.classList.remove("active");
  $("text").focus();
});

function stopPolling() { if (polling) { clearTimeout(polling); polling = null; } }

async function openChat(id) {
  stopPolling();
  current = id;
  const chat = await api(`/api/chats/${id}`);
  for (const li of $("chats").children) li.classList.toggle("active", li.dataset.id === id);
  const log = $("log");
  log.replaceChildren();
  let running = null;
  for (const t of chat.turns) {
    const box = turnBox(t.text);
    log.append(box);
    for (const e of t.events) renderEvent(box, e);
    renderStatus(box, t);
    if (t.status === "queued" || t.status === "running") running = { id: t.id, box, seq: t.events.length ? t.events[t.events.length - 1].seq : 0 };
  }
  log.scrollTop = log.scrollHeight;
  setBusy(Boolean(running));
  if (running) poll(running.id, running.box, running.seq);
  await loadFiles();
}

function turnBox(text) {
  const box = el("div", "turn");
  box.append(el("div", "msg user", text));
  box.status = el("div", "msg status", "чака ред…");
  box.append(box.status);
  return box;
}

function renderEvent(box, e) {
  let node = null;
  if (e.kind === "assistant") node = el("div", "msg assistant", e.text);
  else if (e.kind === "tool") {
    node = el("div", "msg tool");
    const d = el("details");
    d.append(el("summary", "", `⚙ ${e.name}`), el("pre", "", e.result || ""));
    node.append(d);
  } else if (e.kind === "warn" || e.kind === "info") node = el("div", "msg tool", e.text);
  if (node) box.insertBefore(node, box.status);
}

function renderStatus(box, t) {
  const s = box.status;
  s.className = "msg status";
  if (t.status === "queued") s.textContent = "чака ред…";
  else if (t.status === "running") s.textContent = "работи…";
  else if (t.status === "ok") {
    s.classList.add("ok");
    const tok = t.tokens && t.tokens.total_tokens ? ` · ${t.tokens.total_tokens} токена` : "";
    s.textContent = `готово за ${t.seconds} s${tok}`;
  } else { s.classList.add("failed"); s.textContent = `не стана: ${t.error || "неизвестна грешка"}`; }
}

function setBusy(busy) {
  $("send").disabled = busy;
  $("send").textContent = busy ? "Работи…" : "Прати";
}

async function poll(turnId, box, seq) {
  let data;
  try { data = await api(`/api/turns/${turnId}/events?after=${seq}`); }
  catch { polling = setTimeout(() => poll(turnId, box, seq), 3000); return; }
  for (const e of data.events) { renderEvent(box, e); seq = e.seq; }
  renderStatus(box, data);
  const log = $("log");
  log.scrollTop = log.scrollHeight;
  if (data.status === "queued" || data.status === "running") {
    polling = setTimeout(() => poll(turnId, box, seq), 1000);
  } else {
    polling = null;
    setBusy(false);
    await loadFiles();
  }
}

async function loadFiles() {
  if (!current) { $("files").hidden = true; return; }
  const { files, busy } = await api(`/api/chats/${current}/files`);
  const ul = $("file-list");
  ul.replaceChildren();
  $("files").hidden = !files.length;
  $("zip").hidden = busy;
  $("zip").href = `/api/chats/${current}/files.zip`;
  for (const f of files) {
    const li = el("li");
    if (busy) li.append(el("span", "", f.path));
    else {
      const a = el("a", "", f.path);
      a.href = `/api/chats/${current}/files/${f.path.split("/").map(encodeURIComponent).join("/")}`;
      a.setAttribute("download", "");
      li.append(a);
    }
    li.append(el("span", "muted", ` · ${f.size < 1024 ? f.size + " B" : Math.round(f.size / 1024) + " KB"}`));
    ul.append(li);
  }
}

$("ask").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("text").value.trim();
  if (!text) return;
  setBusy(true);
  try {
    if (!current) {
      const { id } = await api("/api/chats", { title: text.slice(0, 60) });
      current = id;
      await loadChats();
      $("log").replaceChildren();
    }
    const { turn_id } = await api(`/api/chats/${current}/turns`, { text });
    $("text").value = "";
    const box = turnBox(text);
    $("log").append(box);
    poll(turn_id, box, 0);
  } catch (e) {
    setBusy(false);
    $("log").append(el("div", "msg status failed", e.message));
  }
});

$("text").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) $("ask").requestSubmit();
});

start();
