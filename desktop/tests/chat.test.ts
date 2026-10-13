/**
 * The chat helpers for the parts of the protocol that came with Genesis
 * 2026-10-11 (progress of a sub-agent, the task list).   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { relativeTo, toChatItems, todoSummary, toolHeader, turnInfo } from '../src/renderer/lib/chat.ts';

test('a sub-agent step is a note and names the running turn', () => {
  const events = [
    { seq: 1, ts: 1000, type: 'user', text: 'направи го' },
    { seq: 2, ts: 1001, type: 'thinking', label: 'Мисли…' },
    { seq: 3, ts: 1002, type: 'progress', text: '  под-агент: пуска тестовете  ' },
  ];
  const items = toChatItems(events);
  assert.equal(items.length, 2);
  assert.deepEqual(items[1], { kind: 'note', key: '3', tone: 'progress', text: '  под-агент: пуска тестовете  ', ts: 1002 });
  assert.deepEqual(turnInfo(events), { label: 'под-агент: пуска тестовете', since: 1000 });
  // The last label wins — a later "thinking" replaces the sub-agent's step.
  assert.equal(turnInfo([...events, { seq: 4, ts: 1003, type: 'thinking', label: 'Пише' }]).label, 'Пише');
});

test('the task list in one line', () => {
  assert.equal(todoSummary([]), '0/0');
  assert.equal(todoSummary([
    { content: 'прочети', status: 'completed' },
    { content: 'пусни тестовете', status: 'in_progress' },
    { content: 'качи', status: 'pending' },
  ]), '1/3 · пусни тестовете');
  assert.equal(todoSummary([{ content: 'качи', status: 'pending' }]), '0/1 · качи');
});

test('tool cards show paths relative to the open folder', () => {
  // 2026-10-12, live check: an EDIT_FILE card showed C:\Users\…\calc.py twice, cut off.
  const ws = 'C:\\Users\\u\\Projects\\_genesis-app-test';
  const edit = `[EDIT_FILE: ${ws}\\calc.py] ✓ ${ws}\\calc.py — 1 замяна(и), +1/-1 реда`;
  assert.deepEqual(toolHeader('EDIT_FILE', edit, ws), { target: 'calc.py', extra: 'calc.py — 1 замяна(и), +1/-1 реда', failed: false });
  // The folder itself reads as its name; case and slashes do not matter; outside paths stay whole.
  assert.equal(toolHeader('REPO_MAP', `[REPO_MAP: ${ws}]`, ws).target, '_genesis-app-test');
  assert.equal(relativeTo('c:/users/u/projects/_genesis-app-test/src/a.py', `${ws}\\`), 'src/a.py');
  assert.equal(relativeTo('D:\\other\\a.py', ws), 'D:\\other\\a.py');
  assert.equal(relativeTo(`${ws}-old\\a.py`, ws), `${ws}-old\\a.py`);
  assert.equal(toolHeader('READ_FILE', `[READ_FILE: ${ws}\\a.py]`).target, `${ws}\\a.py`);
});

test('a result that starts with its own tag does not repeat it', () => {
  // 2026-10-12, live check: „TASK_UPDATE [TASK_UPDATE] ✓ Нишка #20 обновена.“
  assert.equal(toolHeader('TASK_UPDATE', '[TASK_UPDATE] ✓ Нишка #20 обновена.').target, '✓ Нишка #20 обновена.');
  assert.equal(toolHeader('TASK_UPDATE', '[DONE] друго').target, '[DONE] друго');
});
