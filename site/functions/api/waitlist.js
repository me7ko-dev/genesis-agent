// Cloudflare Pages Function: /api/waitlist
//
// POST  — записва имейл (+ по желание „какво би автоматизирал") в KV `WAITLIST`.
//         Приема JSON (от страницата) и обикновен формуляр (без JavaScript →
//         пренасочва към /?ok=1). Същият имейл презаписва стария запис, не дублира.
// GET   — брой и списък на записаните; само с `Authorization: Bearer <ADMIN_TOKEN>`.
//
// Връзки в Cloudflare Pages → Settings → Functions: KV binding `WAITLIST`,
// променлива (secret) `ADMIN_TOKEN`.

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function readBody(request) {
  const ct = request.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return { data: await request.json().catch(() => ({})), isForm: false };
  }
  const form = await request.formData().catch(() => null);
  return { data: form ? Object.fromEntries(form) : {}, isForm: true };
}

export async function onRequestPost({ request, env }) {
  const { data, isForm } = await readBody(request);
  const done = (body, status = 200) =>
    isForm && status === 200 ? Response.redirect(new URL("/?ok=1", request.url).toString(), 303)
                             : json(body, status);

  // Скритото поле се попълва само от ботове: казваме „ок" и не пазим нищо.
  if (String(data.website || "").trim()) return done({ ok: true });

  const email = String(data.email || "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL_RE.test(email)) {
    return json({ ok: false, error: "Невалиден имейл." }, 400);
  }
  const need = String(data.need || "").trim().slice(0, 1000);
  const key = `email:${email}`;
  const repeat = (await env.WAITLIST.get(key)) !== null;
  await env.WAITLIST.put(key, JSON.stringify({
    email,
    need,
    at: new Date().toISOString(),
    country: (request.cf && request.cf.country) || "",
  }));
  return done({ ok: true, repeat });
}

export async function onRequestGet({ request, env }) {
  const auth = request.headers.get("authorization") || "";
  if (!env.ADMIN_TOKEN || auth !== `Bearer ${env.ADMIN_TOKEN}`) {
    return json({ ok: false, error: "forbidden" }, 403);
  }
  const entries = [];
  let cursor;
  do {
    const page = await env.WAITLIST.list({ prefix: "email:", cursor });
    for (const k of page.keys) {
      const v = await env.WAITLIST.get(k.name);
      if (v) entries.push(JSON.parse(v));
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return json({ ok: true, count: entries.length, entries });
}
