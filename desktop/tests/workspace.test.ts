/**
 * Why the agent may work elsewhere than the opened folder.   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { guardedFolder } from '../src/shared/workspace.ts';

test('home and drive roots are guarded, project folders are not', () => {
  const home = 'C:\\Users\\u';
  for (const p of ['C:\\', 'C:', 'd:/', 'C:\\Users\\u', 'c:/users/u/', '/']) assert.equal(guardedFolder(p, home), true, p);
  // 2026-10-12: a source-run Genesis worked in its own checkout and the window blamed this rule.
  for (const p of ['C:\\Users\\u\\Projects\\_test', 'C:\\Users', 'D:\\work\\app']) assert.equal(guardedFolder(p, home), false, p);
  assert.equal(guardedFolder('C:\\Users\\u\\x', ''), false);
});
