/**
 * What of `genesis serve`'s output reaches the window's log.   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { logLine } from '../src/main/logline.ts';

test('the pairing key never reaches the log', () => {
  // 2026-10-12, live check: the 2026-10-09 link format (`&k=`) showed the key in the log drawer.
  const key = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE';
  assert.equal(logLine(`Връзка: genesisremote://pair#u=http%3A%2F%2F127.0.0.1%3A62016&k=${key}&n=METKO`),
    'Връзка: genesisremote://pair#u=http%3A%2F%2F127.0.0.1%3A62016&k=•••&n=METKO');
  // The older `#k=` form and a key at the end of the line too.
  assert.equal(logLine(`http://192.168.1.5:8765/#k=${key}`), 'http://192.168.1.5:8765/#k=•••');
  assert.ok(!logLine(`…?u=x&k=${key}`)!.includes(key));
});

test('QR art is hidden, other lines stay as they are', () => {
  assert.equal(logLine('█▀▀▀▀▀█ ▄▀▄ █▀▀▀▀▀█'), null);
  assert.equal(logLine('   '), null);
  assert.equal(logLine('Работна папка: C:\\work'), 'Работна папка: C:\\work');
  assert.equal(logLine('kill=3 k=v'), 'kill=3 k=v');
});
