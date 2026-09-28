import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  findCommand, fmtDay, fmtTokens, full, matchCommands, niceMax, parseSlash, visibleCommands,
} from '../src/lib/commands.ts';

test('`/` parses into the command and its argument', () => {
  assert.deepEqual(parseSlash('/usage'), ['/usage', '']);
  assert.deepEqual(parseSlash('  /done 3 4 '), ['/done', '3 4']);
  assert.equal(parseSlash('здравей /usage'), null);
});

test('aliases, in Bulgarian too, find the command', () => {
  assert.equal(findCommand('/разход', true)?.name, '/usage');
  assert.equal(findCommand('/COST', true)?.name, '/usage');
  assert.equal(findCommand('/nope', true), undefined);
});

test('the phone has no local-Ollama modes; the computer does', () => {
  assert.equal(findCommand('/local_model_max', true), undefined);
  assert.equal(findCommand('/local_model_max', false)?.name, '/local_model_max');
  assert.ok(visibleCommands(true).length < visibleCommands(false).length);
});

test('the `/` menu narrows while typing and closes after a space', () => {
  assert.ok(matchCommands('/', true).length > 10);
  assert.deepEqual(matchCommands('/mod', true).map((c) => c.name), ['/model', '/models']);
  assert.deepEqual(matchCommands('/done 3', true), []);
  assert.deepEqual(matchCommands('hello', true), []);
});

test('numbers read like the desktop app, without Intl', () => {
  assert.equal(fmtTokens(950), '950');
  assert.equal(fmtTokens(1234), '1,2K');
  assert.equal(fmtTokens(82_400), '82K');
  assert.equal(fmtTokens(2_795_326), '2,80M');
  assert.equal(full(1_234_567), '1 234 567');
  assert.equal(niceMax(730), 1000);
  assert.equal(niceMax(180_000), 200_000);
  assert.equal(fmtDay('2026-09-28'), '28 сеп');
  assert.equal(fmtDay('2026-09-28', true), 'пн, 28 сеп');
});
