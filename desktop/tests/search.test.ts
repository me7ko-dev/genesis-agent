/**
 * Търсене в чата, Ctrl+F (2026-11-23): след сгъваемите групи от 20.11
 * стъпките на хода не са в страницата, докато групата е затворена — търсенето
 * трябва само да я отвори, да оцвети съвпаденията и да брои „3 от 12“.
 *   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildRows, toChatItems } from '../src/renderer/lib/chat.ts';
import { counter, findAll, fold, locate, searchOpen, step } from '../src/renderer/lib/search.ts';

const WS = 'C:\\Users\\u\\Projects\\demo';

// Два хода: първият със сгъната група (3 стъпки), вторият — само отговор.
const EVENTS = [
  { seq: 1, ts: 1_000, type: 'user', text: 'Поправи бъга в calc.py' },
  { seq: 2, ts: 2_000, type: 'tool', name: 'READ_FILE', result: `[READ_FILE: ${WS}\\calc.py]\ndef add(a, b):\n    return a - b` },
  { seq: 3, ts: 3_000, type: 'tool', name: 'EDIT_FILE', result: `[EDIT_FILE: ${WS}\\calc.py] ✓\n--- a/calc.py\n+++ b/calc.py\n@@\n-    return a - b\n+    return a + b` },
  { seq: 4, ts: 4_000, type: 'tool', name: 'RUN_CMD', result: '[RUN_CMD: pytest -q] (rc=0)\n3 passed in 0.02s' },
  { seq: 5, ts: 5_000, type: 'assistant', text: 'Готово: **събирането** вече е вярно.' },
  { seq: 6, ts: 6_000, type: 'user', text: 'Благодаря' },
  { seq: 7, ts: 7_000, type: 'assistant', text: 'Моля.' },
];
const rows = () => buildRows(toChatItems(EVENTS as never), false);

test('findAll: без значение главни/малки, и на кирилица, без застъпване', () => {
  assert.deepEqual(findAll('Готово. ГОТОВО е, готово!', 'готово'), [0, 8, 18]);
  assert.deepEqual(findAll('aaaa', 'aa'), [0, 2]);
  assert.deepEqual(findAll('Pytest и pytest', 'PYTEST'), [0, 9]);
});

test('findAll: празна заявка или само интервали не намира нищо', () => {
  assert.deepEqual(findAll('a b c', ''), []);
  assert.deepEqual(findAll('a b c', '   '), []);
});

test('findAll: интервалите в заявката се пазят („a b“ не е „ab“)', () => {
  assert.deepEqual(findAll('ab a b', 'a b'), [3]);
});

test('fold пази дължината — отместванията след „İ“ остават верни', () => {
  assert.equal(fold('İx').length, 2);
  assert.deepEqual(findAll('İstanbul test', 'test'), [9]);
});

test('locate: съвпадение в един текстов възел', () => {
  assert.deepEqual(locate([['няма'], ['пусни pytest сега']], 'pytest'), [
    { block: 1, startNode: 0, startOffset: 6, endNode: 0, endOffset: 12 },
  ]);
});

test('locate: съвпадение през няколко възела (оцветен код, **удебелено**)', () => {
  // <span>ret</span><span>urn a</span><span> + b</span>
  assert.deepEqual(locate([['ret', 'urn a', ' + b']], 'return a +'), [
    { block: 0, startNode: 0, startOffset: 0, endNode: 2, endOffset: 2 },
  ]);
});

test('locate: краят остава във възела, където свършва думата', () => {
  // „Готово“ + „ е“ → обхватът не влиза в следващия възел.
  assert.deepEqual(locate([['Готово', ' е']], 'готово'), [
    { block: 0, startNode: 0, startOffset: 0, endNode: 0, endOffset: 6 },
  ]);
});

test('locate: между два абзаца не се търси', () => {
  assert.deepEqual(locate([['край'], ['начало']], 'крайначало'), []);
});

test('locate: всички съвпадения, по реда им в чата', () => {
  const hits = locate([['a b a'], ['x'], ['A']], 'a');
  assert.deepEqual(hits.map((h) => [h.block, h.startOffset]), [[0, 0], [0, 4], [2, 0]]);
});

test('searchOpen: съвпадение в сгъната група я отваря, заедно с картата', () => {
  const r = rows();
  const work = r.find((x) => x.kind === 'work');
  assert.ok(work && work.kind === 'work', 'ходът има група „Работа“');
  const open = searchOpen(r, '3 passed');
  assert.deepEqual(open.groups, [work.key]);
  assert.deepEqual(open.tools, ['4']);
});

test('ключът на групата е „w“ + ключа на първата ѝ стъпка (WorkGroup разчита на това)', () => {
  const work = rows().find((x) => x.kind === 'work');
  assert.ok(work && work.kind === 'work');
  assert.equal(work.key, `w${work.items[0].key}`);
});

test('searchOpen: отваря всяка карта със съвпадение в изхода', () => {
  const open = searchOpen(rows(), 'RETURN A');
  assert.equal(open.groups.length, 1);
  assert.deepEqual(open.tools, ['2', '3']);
});

test('searchOpen: съвпадение само в отговорите не отваря нищо', () => {
  assert.deepEqual(searchOpen(rows(), 'Моля'), { groups: [], tools: [] });
  assert.deepEqual(searchOpen(rows(), 'няма такова'), { groups: [], tools: [] });
});

test('searchOpen: празна заявка не отваря нищо', () => {
  assert.deepEqual(searchOpen(rows(), ' '), { groups: [], tools: [] });
});

test('searchOpen: самотна карта извън група също се отваря', () => {
  const r = buildRows(toChatItems([
    { seq: 1, ts: 1, type: 'user', text: 'виж' },
    { seq: 2, ts: 2, type: 'tool', name: 'RUN_CMD', result: '[RUN_CMD: dir] (rc=0)\nтайният ред' },
    { seq: 3, ts: 3, type: 'assistant', text: 'ето' },
  ] as never), false);
  assert.ok(r.every((x) => x.kind === 'item'));
  assert.deepEqual(searchOpen(r, 'тайният'), { groups: [], tools: ['2'] });
});

test('searchOpen: бележка в групата — без Markdown знаците', () => {
  const r = buildRows(toChatItems([
    { seq: 1, ts: 1, type: 'user', text: 'x' },
    { seq: 2, ts: 2, type: 'tool', name: 'RUN_CMD', result: '[RUN_CMD: a] (rc=0)' },
    { seq: 3, ts: 3, type: 'info', text: 'бележка с **важна** дума' },
    { seq: 4, ts: 4, type: 'tool', name: 'RUN_CMD', result: '[RUN_CMD: b] (rc=0)' },
    { seq: 5, ts: 5, type: 'assistant', text: 'край' },
  ] as never), false);
  const open = searchOpen(r, 'с важна дума');
  assert.equal(open.groups.length, 1);
  assert.deepEqual(open.tools, []);
});

test('step: следващо/предишно в кръг', () => {
  assert.equal(step(0, 3, 1), 1);
  assert.equal(step(2, 3, 1), 0);
  assert.equal(step(0, 3, -1), 2);
  assert.equal(step(5, 0, 1), 0);
});

test('counter: „3 от 12“, „Няма“, празно без заявка', () => {
  assert.equal(counter(2, 12, 'x'), '3 от 12');
  assert.equal(counter(0, 0, 'x'), 'Няма');
  assert.equal(counter(0, 0, ''), '');
  assert.equal(counter(9, 4, 'x'), '4 от 4');
});
