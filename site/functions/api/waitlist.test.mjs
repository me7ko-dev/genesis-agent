// node --test site/functions/api/waitlist.test.mjs — без мрежа и без Cloudflare: KV е речник в паметта.
import { test } from "node:test";
import assert from "node:assert/strict";
import { onRequestGet, onRequestPost } from "./waitlist.js";

function fakeKV() {
  const m = new Map();
  return {
    m,
    get: async (k) => (m.has(k) ? m.get(k) : null),
    put: async (k, v) => { m.set(k, v); },
    list: async ({ prefix }) => ({
      keys: [...m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })),
      list_complete: true,
    }),
  };
}

const URL_ = "https://genesis.metko.uk/api/waitlist";
const post = (body, env) => onRequestPost({
  request: new Request(URL_, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  }),
  env,
});

test("a valid email is stored, lowercased, with the need text", async () => {
  const env = { WAITLIST: fakeKV() };
  const r = await post({ email: " Ivan@Primer.BG ", need: "фактури → Excel" }, env);
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { ok: true, repeat: false });
  const saved = JSON.parse(env.WAITLIST.m.get("email:ivan@primer.bg"));
  assert.equal(saved.need, "фактури → Excel");
});

test("the same email twice updates one record, not two", async () => {
  const env = { WAITLIST: fakeKV() };
  await post({ email: "a@b.bg", need: "1" }, env);
  const r = await post({ email: "a@b.bg", need: "2" }, env);
  assert.equal((await r.json()).repeat, true);
  assert.equal(env.WAITLIST.m.size, 1);
  assert.equal(JSON.parse(env.WAITLIST.m.get("email:a@b.bg")).need, "2");
});

test("an invalid email is refused and nothing is stored", async () => {
  const env = { WAITLIST: fakeKV() };
  for (const email of ["", "no-at-sign", "a@b", "x".repeat(250) + "@b.bg"]) {
    const r = await post({ email }, env);
    assert.equal(r.status, 400, email);
  }
  assert.equal(env.WAITLIST.m.size, 0);
});

test("the honeypot field makes it a silent no-op", async () => {
  const env = { WAITLIST: fakeKV() };
  const r = await post({ email: "bot@spam.io", website: "http://spam" }, env);
  assert.equal(r.status, 200);
  assert.equal(env.WAITLIST.m.size, 0);
});

test("the need text is capped at 1000 characters", async () => {
  const env = { WAITLIST: fakeKV() };
  await post({ email: "c@d.bg", need: "я".repeat(5000) }, env);
  assert.equal(JSON.parse(env.WAITLIST.m.get("email:c@d.bg")).need.length, 1000);
});

test("a plain form post (no JavaScript) redirects back to the page", async () => {
  const env = { WAITLIST: fakeKV() };
  const body = new URLSearchParams({ email: "e@f.bg", need: "" });
  const r = await onRequestPost({ request: new Request(URL_, { method: "POST", body }), env });
  assert.equal(r.status, 303);
  assert.equal(r.headers.get("location"), "https://genesis.metko.uk/?ok=1");
  assert.equal(env.WAITLIST.m.size, 1);
});

test("the list needs the admin token", async () => {
  const env = { WAITLIST: fakeKV(), ADMIN_TOKEN: "s3cret" };
  await post({ email: "g@h.bg" }, env);
  const get = (auth) => onRequestGet({
    request: new Request(URL_, { headers: auth ? { authorization: auth } : {} }), env,
  });
  assert.equal((await get()).status, 403);
  assert.equal((await get("Bearer wrong")).status, 403);
  const ok = await get("Bearer s3cret");
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).count, 1);
});

test("without an ADMIN_TOKEN configured the list is closed to everyone", async () => {
  const env = { WAITLIST: fakeKV() };
  const r = await onRequestGet({ request: new Request(URL_, { headers: { authorization: "Bearer " } }), env });
  assert.equal(r.status, 403);
});
