// Drives a running Genesis window for a live check, one action per call.
// Start the window with its own profile, so the check touches neither your
// settings nor a window you have open (the app allows one per profile):
//
//   npx electron . --remote-debugging-port=9333 --user-data-dir=<temp folder>
//
//   node scripts/cdp.mjs eval "<js expr>"          → prints the value
//   node scripts/cdp.mjs shot <name>               → <tmp>/genesis-desktop-shots/<name>.png
//   node scripts/cdp.mjs click "<text>" [selector] → clicks the first visible match
//   node scripts/cdp.mjs type "<text>" [noenter]   → types into the composer (and sends)
//   node scripts/cdp.mjs key <Key> [ctrl] [shift]  → a keydown where the focus is
//   node scripts/cdp.mjs wait "<js expr>" [sec]    → waits until truthy
//   node scripts/cdp.mjs idle [sec]                → waits for the turn to end (or a question)
//
// Git Bash turns a lone "/" into a Windows path: export MSYS_NO_PATHCONV=1 first.
// The log drawer holds the agent's output: do not screenshot it for others.
import { mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const port = process.env.GENESIS_CDP_PORT || '9333';
const [cmd, a1, a2, a3] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let page;
for (let i = 0; i < 30 && !page; i++) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    page = list.find((t) => t.type === 'page' && !t.url.startsWith('devtools'));
  } catch { /* not up yet */ }
  if (!page) await sleep(1000);
}
if (!page) { console.log('NO PAGE'); process.exit(2); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let id = 0;
const pending = new Map();
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const call = (method, params = {}) => new Promise((r) => {
  const n = ++id;
  pending.set(n, r);
  ws.send(JSON.stringify({ id: n, method, params }));
});
async function js(expr) {
  const r = await call('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) return { error: r.result.exceptionDetails.exception?.description ?? r.result.exceptionDetails.text };
  return r.result?.result?.value;
}

let out;
switch (cmd) {
  case 'eval':
    out = await js(a1);
    break;
  case 'shot': {
    const dir = join(tmpdir(), 'genesis-desktop-shots');
    mkdirSync(dir, { recursive: true });
    const r = await call('Page.captureScreenshot', { format: 'png' });
    writeFileSync(join(dir, `${a1}.png`), Buffer.from(r.result.data, 'base64'));
    out = join(dir, `${a1}.png`);
    break;
  }
  case 'click':
    out = await js(`(() => {
      const els = [...document.querySelectorAll(${JSON.stringify(a2 || 'button, a, [role=button]')})]
        .filter((x) => x.getClientRects().length);
      const hit = els.find((x) => x.textContent.trim().includes(${JSON.stringify(a1)}) || (x.title || '').includes(${JSON.stringify(a1)}));
      if (!hit) return 'MISSING';
      hit.click();
      return 'ok: ' + hit.textContent.trim().slice(0, 60);
    })()`);
    break;
  case 'type':
    out = await js(`(() => {
      const t = document.querySelector('textarea');
      if (!t) return 'NO TEXTAREA';
      if (t.disabled) return 'DISABLED';
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(t, ${JSON.stringify(a1 ?? '')});
      t.dispatchEvent(new Event('input', { bubbles: true }));
      ${a2 === 'noenter' ? '' : "t.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));"}
      return 'ok';
    })()`);
    break;
  case 'key':
    // Once, where the focus is: it bubbles to the window's own handler.
    out = await js(`(() => {
      (document.activeElement || document.body).dispatchEvent(new KeyboardEvent('keydown', {
        key: ${JSON.stringify(a1)}, ctrlKey: ${a2 === 'ctrl'}, shiftKey: ${a2 === 'shift' || a3 === 'shift'}, bubbles: true }));
      return 'ok';
    })()`);
    break;
  case 'wait': {
    const until = Date.now() + (Number(a2) || 60) * 1000;
    out = false;
    while (Date.now() < until) {
      out = await js(a1);
      if (out && !out.error) break;
      await sleep(500);
    }
    break;
  }
  case 'idle': {
    const until = Date.now() + (Number(a1) || 120) * 1000;
    out = 'TIMEOUT';
    await sleep(1500);
    while (Date.now() < until) {
      const s = await js(`({ busy: document.querySelector('.status-dot')?.className.includes('busy'),
                             asks: !!document.querySelector('.confirm-actions') })`);
      if (s?.asks) { out = 'CONFIRM'; break; }
      if (s && s.busy === false) { out = 'idle'; break; }
      await sleep(1000);
    }
    break;
  }
  default:
    out = `unknown command: ${cmd}`;
}
console.log(typeof out === 'string' ? out : JSON.stringify(out));
ws.close();
