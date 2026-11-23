/**
 * Ходът като едно цяло, на телефона (2026-11-23). Същите случаи като
 * desktop/tests/turns.test.ts (проба на живо от 20.11): ако двете копия на
 * логиката се разминат, един от двата файла пада.
 *   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { toChatItems, type ChatItem } from '../src/lib/chat.ts';
import type { GenesisEvent } from '../src/lib/protocol.ts';
import {
  buildRows, diffLines, reuseRows, stepLabel, toolHeader, toolTitle, workSummary, workTitle, type Row,
} from '../src/lib/turns.ts';

const WS = 'C:\\Users\\u\\Projects\\_genesis-ui-test';

// Истинските събития от пробата на живо (съкратени).
const LIVE: GenesisEvent[] = [
  { seq: 1, ts: 1_000, type: 'user', text: 'Намери и поправи бъговете' },
  { seq: 2, ts: 1_001, type: 'busy', busy: true },
  { seq: 5, ts: 13_000, type: 'tool', name: 'REPO_MAP', result: `[REPO_MAP: ${WS}]\nезик: python` },
  { seq: 7, ts: 19_000, type: 'tool', name: 'READ_FILE', result: `[READ_FILE: ${WS}\\calc.py]\ndef add(a, b):` },
  { seq: 10, ts: 35_000, type: 'assistant', text: 'Намерих бъгът.' },
  { seq: 12, ts: 36_000, type: 'tool', name: 'EDIT_FILE', result: `[EDIT_FILE: ${WS}\\calc.py] ✓ ${WS}\\calc.py — 1 замяна(и), +1/-1 реда\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-    return a - b\n+    return a + b` },
  { seq: 15, ts: 47_000, type: 'tool', name: 'RUN_CMD', result: `[RUN_CMD: cd ${WS} && python -m pytest -v]  (rc=0)\n2 passed` },
  { seq: 17, ts: 93_000, type: 'assistant', text: 'Готово. Двата теста минават.' },
  { seq: 18, ts: 93_100, type: 'warn', text: 'Написа код — казвам му да го пробва.' },
  { seq: 25, ts: 126_000, type: 'tool', name: 'приемни тестове', result: 'приемни тестове: заявката не дава интерфейс за тест — пропуснати' },
  { seq: 26, ts: 126_100, type: 'busy', busy: false },
];

const shape = (rows: Row[]) => rows.map((r) => (r.kind === 'work' ? `work:${r.items.length}` : r.item.kind));

test('a finished turn: the steps fold into groups, every answer stays in sight', () => {
  const rows = buildRows(toChatItems(LIVE), false);
  // 7 карти на телефона бяха 3–4 екрана; сега са два реда и двата отговора.
  assert.deepEqual(shape(rows), ['user', 'work:2', 'assistant', 'work:4', 'assistant']);
  const work = rows[3];
  assert.ok(work.kind === 'work' && !work.live);
  // Проверката след крайния отговор влиза в групата преди него — отговорът остава последен.
  assert.equal(work.kind === 'work' && work.items[work.items.length - 1].kind, 'tool');
  const final = rows[4];
  assert.equal(final.kind === 'item' && final.item.kind === 'assistant' && final.item.text, 'Готово. Двата теста минават.');
});

test('every row has a unique key for the list', () => {
  const rows = buildRows(toChatItems(LIVE), false);
  assert.equal(new Set(rows.map((r) => r.key)).size, rows.length);
});

test('a running turn: only the last group is live (open), the earlier one folds', () => {
  const rows = buildRows(toChatItems(LIVE.slice(0, 7)), true);
  assert.deepEqual(rows.map((r) => (r.kind === 'work' ? `work:${r.items.length}:${r.live}` : r.item.kind)),
                   ['user', 'work:2:false', 'assistant', 'work:2:true']);
});

test('a confirmation, a question and an error stay outside the group', () => {
  const events: GenesisEvent[] = [
    { seq: 1, ts: 1, type: 'user', text: 'изтрий build' },
    { seq: 2, ts: 2, type: 'tool', name: 'READ_FILE', result: '[READ_FILE: a.py]\nx' },
    { seq: 3, ts: 3, type: 'tool', name: 'LIST_DIR', result: '[LIST_DIR: .]\nbuild/' },
    { seq: 4, ts: 4, type: 'confirm', id: 'c1', operation: 'rm -rf build', reasons: [] },
    { seq: 5, ts: 5, type: 'tool', name: 'RUN_CMD', result: '[RUN_CMD: rm -rf build]  (rc=0)' },
    { seq: 6, ts: 6, type: 'asked', text: 'И dist ли?' },
    { seq: 7, ts: 7, type: 'error', text: 'RuntimeError: моделът не отговори' },
  ];
  const rows = buildRows(toChatItems(events), false);
  assert.deepEqual(shape(rows), ['user', 'work:2', 'confirm', 'tool', 'note', 'note']);
  // Ходът свърши с грешка, без отговор → „Опитай пак“ праща същото.
  const err = rows[5];
  assert.equal(err.kind === 'item' && err.retry, 'изтрий build');
  // Докато върви — не.
  assert.equal(buildRows(toChatItems(events), true).some((r) => r.kind === 'item' && r.retry), false);
});

test('only the last turn offers a retry, and not after an answer', () => {
  const events: GenesisEvent[] = [
    { seq: 1, ts: 1, type: 'user', text: 'първо' },
    { seq: 2, ts: 2, type: 'error', text: 'грешка' },
    { seq: 3, ts: 3, type: 'user', text: 'второ' },
    { seq: 4, ts: 4, type: 'assistant', text: 'ок' },
  ];
  assert.equal(buildRows(toChatItems(events), false).some((r) => r.kind === 'item' && r.retry), false);
});

test('a group of one is the thing itself, without a frame', () => {
  const events: GenesisEvent[] = [
    { seq: 1, ts: 1, type: 'user', text: 'виж' },
    { seq: 2, ts: 2, type: 'tool', name: 'READ_FILE', result: '[READ_FILE: a.py]\nx' },
    { seq: 3, ts: 3, type: 'assistant', text: 'Ето.' },
  ];
  assert.deepEqual(shape(buildRows(toChatItems(events), false)), ['user', 'tool', 'assistant']);
});

test('a command answered with notes only is never folded away', () => {
  // 2026-11-20, на живо: отговорът на /resume (два info реда) се скри зад „Работа · 2 бележки“.
  const events: GenesisEvent[] = [
    { seq: 1, ts: 1, type: 'user', text: '/resume' },
    { seq: 3, ts: 2, type: 'info', text: 'Разговори в …:\n  1. …' },
    { seq: 4, ts: 3, type: 'info', text: 'Избери с /resume <номер>.' },
  ];
  assert.deepEqual(shape(buildRows(toChatItems(events), false)), ['user', 'note', 'note']);
  assert.deepEqual(shape(buildRows(toChatItems(events), true)), ['user', 'note', 'note']);
});

test('the history before the first message of the window still shows', () => {
  const events: GenesisEvent[] = [{ seq: 1, ts: 1, type: 'info', text: 'Зареден разговор' }, { seq: 2, ts: 2, type: 'assistant', text: 'здрасти' }];
  assert.deepEqual(shape(buildRows(toChatItems(events), false)), ['note', 'assistant']);
});

test('the work summary: steps, changed files, failures — and the folded title', () => {
  const tools = toChatItems(LIVE).filter((i) => i.kind === 'tool');
  const failed: ChatItem = { kind: 'tool', key: 'x', name: 'RUN_CMD', result: '[RUN_CMD: pytest]  (rc=1)\nFAILED', clipped: false };
  const s = workSummary([...tools, failed], WS);
  assert.equal(s.steps, 6);
  assert.deepEqual(s.changed, ['calc.py']);
  assert.equal(s.failed, 1);
  assert.equal(workTitle([...tools, failed], WS), '6 стъпки · промени calc.py');
  assert.equal(workTitle([failed], WS), '1 стъпка');
});

test('RUN_CMD cards drop the "cd <the open folder> &&" prefix', () => {
  const r = `[RUN_CMD: cd ${WS} && python -m pytest -v]  (rc=0)\nok`;
  assert.deepEqual(toolHeader('RUN_CMD', r, WS), { target: 'python -m pytest -v', extra: '', failed: false });
  assert.equal(toolHeader('RUN_CMD', `[RUN_CMD: cd "${WS}" ; npm test]  (rc=0)`, WS).target, 'npm test');
  // Подпапка — остава, но относителна.
  assert.equal(toolHeader('RUN_CMD', `[RUN_CMD: cd /d ${WS}\\src && dir]  (rc=0)`, WS).target, 'cd /d src && dir');
  // Компютърът може да е Linux/macOS — същото с наклонени черти.
  assert.equal(toolHeader('RUN_CMD', '[RUN_CMD: cd /home/u/proj && make]  (rc=0)', '/home/u/proj').target, 'make');
  // Без позната папка (още няма status) — командата остава цяла.
  assert.equal(toolHeader('RUN_CMD', r).target, `cd ${WS} && python -m pytest -v`);
});

test('a multi-line command keeps its first line and its exit code', () => {
  const r = `[RUN_CMD: cd ${WS} && python -c "\nfrom calc import add\nprint([add(1, 2)])\n"]  (rc=1)\nTraceback`;
  const h = toolHeader('RUN_CMD', r, WS);
  assert.equal(h.target, 'python -c " …');
  assert.equal(h.failed, true);
  assert.equal(toolHeader('RUN_CMD', `[RUN_CMD: python -c "\nprint([1])\n"]  (rc=0)\n[1]`).failed, false);
});

test('paths inside the open folder are shown relative to it', () => {
  assert.equal(toolHeader('READ_FILE', `[READ_FILE: ${WS}\\src\\calc.py] (редове 1–6 от 6)\nx`, WS).target, 'src\\calc.py');
  assert.equal(toolHeader('READ_FILE', `[READ_FILE: ${WS}\\src\\calc.py] (редове 1–6 от 6)\nx`, WS).extra, '(редове 1–6 от 6)');
});

test('a result that starts with the tool name does not repeat it', () => {
  assert.equal(toolHeader('приемни тестове', 'приемни тестове: заявката не дава интерфейс — пропуснати').target,
               'заявката не дава интерфейс — пропуснати');
});

test('tool titles in Bulgarian, unknown names as they are', () => {
  assert.equal(toolTitle('READ_FILE'), 'Чете');
  assert.equal(toolTitle('RUN_CMD'), 'Изпълнява');
  assert.equal(toolTitle('EDIT_FILE'), 'Редактира');
  assert.equal(toolTitle('TASK_UPDATE'), 'TASK_UPDATE');
  assert.equal(toolTitle('приемни тестове'), 'приемни тестове');
});

test('the busy label of a running tool reads like the card', () => {
  assert.equal(stepLabel(`RUN_CMD: cd ${WS} && pytest -q`, WS), 'Изпълнява: pytest -q');
  assert.equal(stepLabel(`READ_FILE: ${WS}\\a.py`, WS), 'Чете: a.py');
  assert.equal(stepLabel('Genesis мисли…', WS), 'Genesis мисли…');
});

test('a diff in a result is marked line by line', () => {
  const lines = diffLines('[EDIT_FILE: a.py] ✓\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n z');
  assert.deepEqual(lines?.map((l) => l.kind), ['plain', 'meta', 'meta', 'hunk', 'del', 'add', 'plain']);
  assert.equal(diffLines('[RUN_CMD: ls]\n-rw-r--r-- a\n+ x'), null);
});

test('reuseRows keeps the old row objects where nothing changed', () => {
  // Без това всяко допитване рисува пак целия списък (memo на картите пада).
  const items = toChatItems(LIVE);
  const before = buildRows(items, false);
  const again = reuseRows(before, buildRows(items, false));
  assert.ok(again.every((r, i) => r === before[i]));
  // Нова стъпка в хода: старите редове остават, сменя се само засегнатият.
  const more = [...items, { kind: 'user', key: '30', text: 'още', images: [] } as ChatItem];
  const next = reuseRows(before, buildRows(more, true));
  assert.equal(next.length, before.length + 1);
  assert.ok(before.every((r, i) => next[i] === r));
  // Групата тръгва на живо → нов обект.
  const live = reuseRows(buildRows(items.slice(0, 3), false), buildRows(items.slice(0, 3), true));
  assert.equal(live[1].kind === 'work' && live[1].live, true);
});
