// Genesis онлайн — входът, чатът и файловете. Без библиотеки; всичко, което
// идва от сървъра, се слага с textContent (никога innerHTML) — отговорите и
// имената на файлове идват от модела и от чужд код.
"use strict";

const $ = (id) => document.getElementById(id);
let current = null;       // id на отворения разговор
let polling = null;       // таймерът на текущия ход
let accountTimer = null;  // повторното четене на сметката след плащане
const params = new URLSearchParams(location.search);

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error(data.error || "нужен е вход"); }
  if (!r.ok) {
    const err = new Error(data.error || `грешка ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Адресът без ?invite/?paid — презареждане не повтаря регистрация или чакане.
function dropParams(...names) {
  for (const n of names) params.delete(n);
  const q = params.toString();
  history.replaceState(null, "", location.pathname + (q ? `?${q}` : ""));
}

function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
  const invite = params.get("invite");
  $("signup-form").hidden = !invite;
  $("login-form").hidden = Boolean(invite);
  if (invite) $("signup-form").elements.code.value = invite;
}

async function start() {
  try {
    const me = await api("/api/me");
    $("me").textContent = me.email;
    $("balance").textContent = me.balance !== undefined ? `· ${me.balance} кр.` : "";
    $("login").hidden = true;
    $("app").hidden = false;
    // Прочетено преди loadChats: отварянето на разговор чисти ?view.
    const wantAccount = params.get("view") === "account";
    const paid = params.get("paid") === "1";
    await loadChats();
    if (wantAccount) await showAccount(paid);
  } catch { showLogin(); }
}

$("signup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  $("signup-error").textContent = "";
  try {
    await api("/api/signup", { code: f.get("code"), email: f.get("email"), password: f.get("password") });
    ev.target.reset();
    dropParams("invite");
    await start();
  } catch (e) { $("signup-error").textContent = e.message; }
});

$("to-login").addEventListener("click", () => { dropParams("invite"); showLogin(); });

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
  showChatView();
  current = null;
  stopPolling();
  $("log").replaceChildren(el("p", "muted", "Нов разговор. Напиши задачата долу."));
  $("files").hidden = true;
  for (const li of $("chats").children) li.classList.remove("active");
  $("text").focus();
});

function stopPolling() { if (polling) { clearTimeout(polling); polling = null; } }

async function openChat(id) {
  showChatView();
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
    s.textContent = `готово за ${t.seconds} s${tok}${cost(t)}`;
  } else { s.classList.add("failed"); s.textContent = `не стана: ${t.error || "неизвестна грешка"}${cost(t)}`; }
}

function cost(t) { return t.cost ? ` · ${t.cost} кредита` : ""; }

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
  const { files, busy, truncated } = await api(`/api/chats/${current}/files`);
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
  // Списъкът спира на 1000 — казва го, zip-ът носи всички (2026-10-11).
  if (truncated) ul.append(el("li", "muted", `показани са първите ${files.length} — всички са в zip-а`));
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
    const msg = el("div", "msg status failed", e.message);
    // 402 — няма кредити: една стъпка до „Сметка“.
    if (e.status === 402) {
      const b = el("button", "link", " Към сметката");
      b.addEventListener("click", () => showAccount());
      msg.append(b);
    }
    $("log").append(msg);
  }
});

$("text").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) $("ask").requestSubmit();
});


// ── Сметка ──────────────────────────────────────────────────────────────────

const KINDS = { charge: "задача", purchase: "покупка", subscription: "абонамент",
                grant: "подарък", adjust: "корекция" };

function showChatView() {
  if (accountTimer) { clearTimeout(accountTimer); accountTimer = null; }
  $("account").hidden = true;
  $("chat-view").hidden = false;
  if (params.get("view")) dropParams("view", "paid");
}

async function showAccount(paid = false) {
  stopPolling();
  if (accountTimer) { clearTimeout(accountTimer); accountTimer = null; }
  $("chat-view").hidden = true;
  $("account").hidden = false;
  for (const li of $("chats").children) li.classList.remove("active");
  current = null;
  $("paid-note").hidden = !paid;
  // Webhook-ът от Stripe може да закъснее след връщането: 10 × 3 s.
  let tries = paid ? 10 : 0;
  const load = async () => {
    try { renderAccount(await api("/api/account")); $("account-error").textContent = ""; }
    catch (e) { $("account-error").textContent = e.message; }
    if (tries-- > 0) accountTimer = setTimeout(load, 3000);
    else accountTimer = null;
  };
  if (paid) dropParams("paid");
  await load();
}

function renderAccount(a) {
  $("billing-off").hidden = a.billing;
  $("billing-on").hidden = !a.billing;
  $("acc-balance").textContent = a.balance;
  $("balance").textContent = a.billing ? `· ${a.balance} кр.` : "";
  $("acc-month").textContent = `${a.month_spent} от ${a.monthly_cap} кредита`;
  $("acc-month-bar").max = Math.max(parseFloat(a.monthly_cap) || 1, 0.01);
  $("acc-month-bar").value = Math.max(parseFloat(a.month_spent) || 0, 0);
  $("acc-plan").textContent = a.plan ? `Абонамент: ${a.plan}` : "";
  $("acc-turns").textContent = `Задачи днес: ${a.turns_today} от ${a.turns_per_day}`;
  $("stripe-off").hidden = a.stripe || !a.billing;
  const offers = $("offers");
  offers.replaceChildren();
  if (a.stripe) {
    for (const p of a.packs) offers.append(offerButton(`${p.credits} кредита — ${p.price}`, { pack: p.id }));
    if (!a.plan) for (const p of a.plans) offers.append(offerButton(`Абонамент: ${p.credits} кредита на месец — ${p.price}`, { plan: p.id }));
  }
  const body = $("history").tBodies[0];
  body.replaceChildren();
  for (const h of a.history) {
    const tr = el("tr");
    const what = KINDS[h.kind] || h.kind;
    tr.append(el("td", "", new Date(h.at * 1000).toLocaleString("bg-BG")),
              el("td", "", h.turn_id ? `${what} #${h.turn_id}` : what),
              el("td", "num", h.credits));
    body.append(tr);
  }
  if (!a.history.length) {
    const tr = el("tr");
    const td = el("td", "muted", "още няма движения");
    td.colSpan = 3;
    tr.append(td);
    body.append(tr);
  }
}

function offerButton(label, body) {
  const b = el("button", "", label);
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const { url } = await api("/api/checkout", body);
      location.href = url;   // към Stripe Checkout (горно ниво — CSP form-action не го спира)
    } catch (e) { $("account-error").textContent = e.message; b.disabled = false; }
  });
  return b;
}

$("account-link").addEventListener("click", () => showAccount());

start();
