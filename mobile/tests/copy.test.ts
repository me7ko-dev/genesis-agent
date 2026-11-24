/**
 * „Копирай“ не бива да събаря чата (2026-11-23, преглед на PR #131):
 * expo-clipboard е нов нативен модул и на по-стар билд зареждането му хвърля.
 *   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { copyText } from '../src/lib/copy.ts';

const missing = () => Promise.reject(new Error("Cannot find native module 'ExpoClipboard'"));

test('no screen or component imports expo-clipboard at module load', () => {
  // Внасяне най-горе = грешка още при отваряне на чата на стар билд.
  for (const file of ['../src/components/Cards.tsx', '../src/app/chat.tsx', '../src/lib/copy.ts']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /^\s*import\b[^\n]*from\s+'expo-clipboard'/m, file);
  }
});

test('a build without the native module: copy says "no", it does not throw', async () => {
  assert.equal(await copyText('здравей', missing, undefined), false);
});

test('with the module the text reaches the clipboard', async () => {
  const seen: string[] = [];
  const ok = await copyText('pytest -q', async () => ({ setStringAsync: async (t) => { seen.push(t); return true; } }), undefined);
  assert.equal(ok, true);
  assert.deepEqual(seen, ['pytest -q']);
});

test('without the module the browser clipboard is the fallback', async () => {
  const seen: string[] = [];
  const web = { writeText: async (t: string) => { seen.push(t); } };
  assert.equal(await copyText('текст', missing, web), true);
  assert.deepEqual(seen, ['текст']);
  // И той отказва (няма разрешение) → пак само „не стана“.
  assert.equal(await copyText('текст', missing, { writeText: () => Promise.reject(new Error('denied')) }), false);
});
