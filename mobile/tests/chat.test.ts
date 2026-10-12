import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  MAX_EVENTS, mergeEvents, reuseItems, thinkingLabel, todoSummary, toChatItems, trimEvents,
} from '../src/lib/chat.ts';
import { splitBlocks, splitInline } from '../src/lib/markdown.ts';
import { errorText, MAX_MESSAGE, messageLength, replyErrorText, trimDraft } from '../src/lib/messages.ts';
import { Outbox, RETRY_WINDOW_MS } from '../src/lib/outbox.ts';
import { ProtocolError, type GenesisEvent } from '../src/lib/protocol.ts';

const ev = (seq: number, type: string, extra: Partial<GenesisEvent> = {}): GenesisEvent =>
  ({ seq, ts: seq, type, ...extra });

test('events become chat items; busy/thinking do not', () => {
  const items = toChatItems([
    ev(1, 'user', { text: 'hi' }), ev(2, 'busy', { busy: true }), ev(3, 'thinking', { label: 'x' }),
    ev(4, 'tool', { name: 'RUN_CMD', result: 'ok' }), ev(5, 'assistant', { text: 'done' }),
    ev(6, 'warn', { text: 'careful' }),
  ]);
  assert.deepEqual(items.map((i) => i.kind), ['user', 'tool', 'assistant', 'note']);
});

test('a confirm card follows its answer', () => {
  const pending = toChatItems([ev(1, 'confirm', { id: 'a', operation: 'rm x', reasons: ['r'] })]);
  assert.equal(pending[0].kind === 'confirm' && pending[0].state, 'pending');
  const answered = toChatItems([
    ev(1, 'confirm', { id: 'a', operation: 'rm x', reasons: ['r'] }),
    ev(2, 'confirm_done', { id: 'a', allow: false, note: 'няма отговор — отказано' }),
  ]);
  assert.equal(answered[0].kind === 'confirm' && answered[0].state, 'denied');
});

test('"cleared" starts the chat over', () => {
  const items = toChatItems([ev(1, 'user', { text: 'old' }), ev(2, 'cleared'), ev(3, 'user', { text: 'new' })]);
  assert.deepEqual(items.map((i) => (i as { text: string }).text), ['new']);
});

test('merging skips what we already have and honours a reset', () => {
  const cur = [ev(1, 'user'), ev(2, 'busy')];
  assert.equal(mergeEvents(cur, [ev(2, 'busy'), ev(3, 'assistant')], false).length, 3);
  assert.equal(mergeEvents(cur, [], false), cur, 'no change → same array (no re-render)');
  assert.deepEqual(mergeEvents(cur, [ev(2, 'x'), ev(1, 'y')], true).map((e) => e.seq), [1, 2]);
});

test('the busy row shows the latest label of the running turn only', () => {
  assert.equal(thinkingLabel([ev(1, 'thinking', { label: 'old' }), ev(2, 'user'), ev(3, 'thinking', { label: 'Анализирам...' })]), 'Анализирам...');
  assert.equal(thinkingLabel([ev(1, 'thinking', { label: 'old' }), ev(2, 'user')]), 'Genesis мисли…');
});

test('sub-agent progress is a quiet note and names what the busy row shows', () => {
  const events = [ev(1, 'user', { text: 'прегледай' }), ev(2, 'thinking', { label: 'мисли' }),
    ev(3, 'progress', { text: '  ↳ reviewer: READ_FILE' })];
  const items = toChatItems(events);
  assert.equal(items[1].kind === 'note' && items[1].tone, 'progress');
  assert.equal(thinkingLabel(events), '↳ reviewer: READ_FILE');
});

test('the todo list in one line: done/all and the item in work', () => {
  assert.equal(todoSummary([
    { content: 'чети', status: 'completed' }, { content: 'пиши', status: 'in_progress' },
    { content: 'тествай', status: 'pending' },
  ]), '1/3 · пиши');
  assert.equal(todoSummary([{ content: 'а', status: 'completed' }]), '1/1');
  assert.equal(todoSummary([{ content: 'а', status: 'pending' }]), '0/1 · а');
});

test('markdown: fences and inline marks', () => {
  assert.deepEqual(splitBlocks('Виж:\n```python\nprint(1)\n```\nГотово'), [
    { code: false, text: 'Виж:' }, { code: true, text: 'print(1)' }, { code: false, text: 'Готово' },
  ]);
  assert.deepEqual(splitInline('a **b** `c` d'), [
    { t: 'a ' }, { t: 'b', bold: true }, { t: ' ' }, { t: 'c', code: true }, { t: ' d' },
  ]);
});

// ── одит 2026-10-11 ─────────────────────────────────────────────────────────

test('the phone keeps what the server keeps: the last 1000, nothing before "cleared"', () => {
  let events: GenesisEvent[] = [];
  for (let turn = 0; turn < 400; turn++) {
    const base = turn * 27;
    events = mergeEvents(events, Array.from({ length: 27 }, (_, i) =>
      ev(base + i + 1, i === 0 ? 'user' : 'tool', { text: `ход ${turn}`, name: 'RUN_CMD', result: 'x' })), false);
  }
  assert.equal(events.length, MAX_EVENTS, 'беше 10 800 — нищо не се махаше');
  assert.equal(events[events.length - 1].seq, 400 * 27);
  const end = 400 * 27;
  const cleared = mergeEvents(events, [ev(end + 1, 'cleared'), ev(end + 2, 'user', { text: 'ново' })], false);
  assert.deepEqual(cleared.map((e) => e.type), ['cleared', 'user'], 'преди "cleared" нищо не се показва');
  // След "cleared" следващото сливане знае докъде е стигнало.
  assert.equal(mergeEvents(cleared, [ev(end + 2, 'user')], false), cleared);
  assert.equal(trimEvents(cleared), cleared, 'нищо за махане → същият масив');
  assert.equal(mergeEvents([], Array.from({ length: 1500 }, (_, i) => ev(i + 1, 'info')), true).length, MAX_EVENTS);
});

test('an unchanged chat item stays the same object across polls', () => {
  const events = [ev(1, 'user', { text: 'hi' }), ev(2, 'tool', { name: 'RUN_CMD', result: 'ok' }),
    ev(3, 'confirm', { id: 'a', operation: 'rm x', reasons: ['r'] })];
  const before = reuseItems([], toChatItems(events));
  // Само „thinking“: нищо видимо не се променя → същият масив, същите обекти.
  const thinking = reuseItems(before, toChatItems([...events, ev(4, 'thinking', { label: 'x' })]));
  assert.equal(thinking, before);
  // Отговорът на потвърждението сменя само неговата карта.
  const done = ev(5, 'confirm_done', { id: 'a', allow: true });
  const answered = reuseItems(before, toChatItems([...events, done]));
  assert.notEqual(answered, before);
  assert.equal(answered[0], before[0]);
  assert.equal(answered[1], before[1]);
  assert.notEqual(answered[2], before[2]);
  assert.equal(answered[2].kind === 'confirm' && answered[2].state, 'allowed');
  // Нов елемент накрая: старите остават същите обекти.
  const more = reuseItems(answered, toChatItems([...events, done, ev(6, 'assistant', { text: 'готово' })]));
  assert.equal(more.length, 4);
  for (let i = 0; i < 3; i++) assert.equal(more[i], answered[i]);
});

test('the cards are memo and the confirm card gets a stable callback', () => {
  const cards = readFileSync(new URL('../src/components/Cards.tsx', import.meta.url), 'utf8');
  for (const name of ['UserBubble', 'AssistantMessage', 'ToolCard', 'Note', 'ConfirmCard']) {
    assert.match(cards, new RegExp(`export const ${name} = memo\\(`), name);
  }
  const chat = readFileSync(new URL('../src/app/chat.tsx', import.meta.url), 'utf8');
  assert.match(chat, /<ConfirmCard item=\{item\} onAnswer=\{g\.confirm\} \/>/);
});

test('one send at a time; a retry of the same text keeps its id', async () => {
  let n = 0;
  const outbox = new Outbox(() => `mid-${++n}-xxxxxx`);
  const seen: string[] = [];
  let release!: () => void;
  const slow = (text: string, mid: string) => {
    seen.push(`${text}:${mid}`);
    return new Promise<{ ok: boolean }>((r) => { release = () => r({ ok: true }); });
  };
  const first = outbox.send('/plan', slow);
  assert.equal(outbox.sending, true);
  assert.equal(await outbox.send('/plan', slow), null, 'второто докосване не прави нищо');
  release();
  assert.deepEqual(await first, { ok: true });
  assert.equal(outbox.sending, false);
  assert.deepEqual(seen, ['/plan:mid-1-xxxxxx']);

  // Изгубен отговор: грешката минава нататък, повторният опит е със същия mid.
  const lost = async (): Promise<{ ok: boolean }> => { throw new Error('lost'); };
  await assert.rejects(outbox.send('пусни тестовете', lost));
  const ids: string[] = [];
  const ok = async (_: string, mid: string) => { ids.push(mid); return { ok: true }; };
  await outbox.send('пусни тестовете', ok);
  assert.deepEqual(ids, ['mid-2-xxxxxx']);
  // След отговор — нов mid, и за същия текст.
  await outbox.send('пусни тестовете', ok);
  assert.deepEqual(ids, ['mid-2-xxxxxx', 'mid-3-xxxxxx']);
  // Друг текст след изгубен отговор — нов mid.
  await assert.rejects(outbox.send('а', lost));
  await outbox.send('б', ok);
  assert.equal(ids[2], 'mid-5-xxxxxx');
});

test('a stale mid is forgotten: a later message with the same text is a new turn', async () => {
  // Преглед 2026-10-11: сървърът приема „продължи“, отговорът се губи; по-късно
  // операторът нарочно праща „продължи“ пак — със стария mid сървърът
  // (RemoteServer._sent) го поглъщаше: 1 ход вместо 2.
  let clock = 0;
  let n = 0;
  const box = new Outbox(() => `mid-${++n}-xxxxxx`, () => clock);
  const sent = new Map<string, { ok: boolean }>();
  let turns = 0;
  let drop = false;
  const server = async (_: string, mid: string) => {
    let reply = sent.get(mid);
    if (!reply) {
      turns += 1;
      reply = { ok: true };
      sent.set(mid, reply);
    }
    if (drop) throw new Error('lost');
    return reply;
  };

  // 1) Своето балонче дойде в събитията → следващото е нов ход.
  drop = true;
  await assert.rejects(box.send('продължи', server));
  drop = false;
  box.seen([{ type: 'assistant', text: 'продължи' }]);
  box.seen([{ type: 'user', text: 'продължи' }]);
  await box.send('продължи', server);
  assert.equal(turns, 2);

  // 2) Без събития (телефонът е без връзка) — след прозореца е нов ход.
  drop = true;
  await assert.rejects(box.send('/plan', server));
  drop = false;
  clock += RETRY_WINDOW_MS;
  await box.send('/plan', server);
  assert.equal(turns, 4);

  // 3) Повторен опит в прозореца, без своето балонче — пак същият mid.
  drop = true;
  await assert.rejects(box.send('да', server));
  clock += RETRY_WINDOW_MS - 1;
  await assert.rejects(box.send('да', server));
  clock += RETRY_WINDOW_MS - 1;   // прозорецът тече от последния опит
  drop = false;
  box.seen([{ type: 'user', text: 'друго' }]);
  await box.send('да', server);
  assert.equal(turns, 5);

  const hook = readFileSync(new URL('../src/lib/useGenesis.ts', import.meta.url), 'utf8');
  assert.match(hook, /outbox\.seen\(reply\.events[,)]/);
});

test('errors reach the operator in Bulgarian', () => {
  assert.match(replyErrorText('too_long'), /твърде дълго/);
  assert.match(replyErrorText('busy'), /още работи/);
  assert.match(replyErrorText('unknown_op'), /по-стар/);
  assert.match(replyErrorText('empty'), /празно/);
  assert.equal(replyErrorText('toString'), 'Компютърът отказа заявката.');
  assert.equal(replyErrorText(undefined), 'Компютърът отказа заявката.');
  assert.match(errorText(new ProtocolError('server error 413', 'server', 413)), /твърде голямо/);
  assert.match(errorText(new ProtocolError('too many failed attempts — wait a minute', 'network', 429)), /изчакай минута/);
  assert.match(errorText(new ProtocolError('cannot reach http://x', 'network')), /Няма връзка/);
  assert.match(errorText(new ProtocolError('the reply could not be verified', 'protocol')), /проверен/);
  for (const kind of ['network', 'unauthorized', 'clock', 'protocol', 'server'] as const) {
    assert.doesNotMatch(errorText(new ProtocolError('english', kind, 500)), /[a-z]{4,}/, kind);
  }
  assert.equal(errorText(new TypeError('x')), 'Неочаквана грешка в приложението.');
  // useGenesis вече не показва нито `reply.error ?? …`, нито e.message.
  const hook = readFileSync(new URL('../src/lib/useGenesis.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(hook, /reply\.error \?\? /);
  assert.doesNotMatch(hook, /setError\(e instanceof Error \? e\.message/);
});

test('a draft is trimmed the way the server strips it; the length is counted like the server', () => {
  assert.equal(trimDraft('\x1c'), '');
  assert.equal(trimDraft(' \x85здравей\x1f\n'), 'здравей');
  assert.equal(trimDraft('a\x1cb'), 'a\x1cb');
  assert.equal(messageLength('👋'.repeat(3)), 3);
  assert.equal(messageLength('я'.repeat(MAX_MESSAGE)), MAX_MESSAGE);
});

test('"Нов разговор" is not offered while a turn runs, and its "busy" is said', () => {
  const chat = readFileSync(new URL('../src/app/chat.tsx', import.meta.url), 'utf8');
  const guard = chat.indexOf('{g.busy ? null : (');
  const item = chat.indexOf('label="Нов разговор"');
  assert.ok(guard > 0 && item > guard, '„Нов разговор“ е под проверката за busy');
  const hook = readFileSync(new URL('../src/lib/useGenesis.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(hook, /const clear = useCallback\(\(\) => act\(\(\) => client\.clear\(\)\)/);
  assert.match(hook, /client\.clear\(\)\);\s+if \(reply && !reply\.ok\) setError\(replyErrorText\(reply\.error\)\)/);
});

test('a deliberate repeat after the server took the send is never swallowed by a stale mid', async () => {
  // Вълна 5, 2026-10-11: сървърът (RemoteServer._sent) пази приетите mid без
  // край, телефонът ги забравяше само по своето балонче или след прозореца.
  // Измерено със сървър като _sent: (1) reset страница от най-новите събития
  // без балончето (дълъг ход офлайн) и (2) „/clear“, което няма балонче —
  // нарочното повторение в прозореца минаваше със стария mid и сървърът го
  // поглъщаше; (3) часовник, върнат назад, удължаваше прозореца без край.
  let clock = 1_000_000;
  let n = 0;
  const box = new Outbox(() => `mid-${++n}-xxxxxx`, () => clock);
  const accepted = new Set<string>();
  let turns = 0;
  let drop = false;
  const server = async (_: string, mid: string) => {
    if (!accepted.has(mid)) {
      turns += 1;
      accepted.add(mid);
    }
    if (drop) throw new Error('lost');
    return { ok: true };
  };

  // (1) Отговорът се губи, после идва reset страница без балончето, но с
  // резултата на хода — и списъкът с приетите mid.
  drop = true;
  await assert.rejects(box.send('продължи', server));
  drop = false;
  box.seen([{ type: 'assistant', text: 'готово' }], [...accepted]);
  await box.send('продължи', server);
  assert.equal(turns, 2);

  // (2) „/clear“ няма балонче — пак списъкът решава.
  drop = true;
  await assert.rejects(box.send('/clear', server));
  drop = false;
  box.seen([{ type: 'cleared' }], [...accepted]);
  await box.send('/clear', server);
  assert.equal(turns, 4);

  // (3) Часовникът се връща назад с час: това не е „в прозореца“.
  drop = true;
  await assert.rejects(box.send('да', server));
  drop = false;
  clock -= 60 * 60 * 1000;
  await box.send('да', server);
  assert.equal(turns, 6);

  // Повторен опит, който сървърът НЕ е приел, пази своя mid (без нов ход).
  const offline = async (): Promise<{ ok: boolean }> => { throw new Error('offline'); };
  await assert.rejects(box.send('пусни', offline));
  box.seen([], [...accepted]);
  const ids: string[] = [];
  await box.send('пусни', async (_t, mid) => { ids.push(mid); return { ok: true }; });
  assert.deepEqual(ids, [`mid-${n}-xxxxxx`]);

  const hook = readFileSync(new URL('../src/lib/useGenesis.ts', import.meta.url), 'utf8');
  assert.match(hook, /outbox\.seen\(reply\.events, reply\.mids\)/);
});
