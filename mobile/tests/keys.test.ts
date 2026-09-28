import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isKeysLink, keysFromParams, longName, maskKey, parseKeysLink } from '../src/lib/keys.ts';

// Made by genesis_agent/keys_transfer.encode_link (Python) — both sides must agree.
const LINK =
  'genesisremote://keys?gq=gsk_A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2' +
  '&gq2=gsk_Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8' +
  '&ol3=0123456789abcdef0123456789abcdef.AbCd%2BEfGh%2FIjKl&or=sk-or-v1-0f0f0f0f0f0f0f0f';

test('the link from `genesis keys qr` becomes named keys', () => {
  assert.equal(isKeysLink(LINK), true);
  assert.deepEqual(parseKeysLink(LINK), {
    GROQ_API_KEY: 'gsk_A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2A1b2',
    GROQ_API_KEY_2: 'gsk_Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8Z9y8',
    OLLAMA_API_KEY_3: '0123456789abcdef0123456789abcdef.AbCd+EfGh/IjKl',
    OPENROUTER_API_KEY: 'sk-or-v1-0f0f0f0f0f0f0f0f',
  });
});

test('short codes: known providers, numbers 2..10 only', () => {
  assert.equal(longName('gq'), 'GROQ_API_KEY');
  assert.equal(longName('ol10'), 'OLLAMA_API_KEY_10');
  assert.equal(longName('gq1'), '');
  assert.equal(longName('gq11'), '');
  assert.equal(longName('zz'), '');
  assert.equal(longName('PATH'), '');
});

test('anything else is not a keys link', () => {
  assert.equal(parseKeysLink('http://192.168.1.5:8765/#k=abc'), null);
  assert.equal(parseKeysLink('genesisremote://pair?u=x'), null);
  assert.equal(parseKeysLink('genesisremote://keys?zz=0123456789abcdef'), null);
});

test('expo-router params (deep link) and bad values', () => {
  assert.deepEqual(keysFromParams({ gq: 'gsk_0123456789', nv: ['nvapi-0123456789'], x: 'y', or: 'has space in it' }),
    { GROQ_API_KEY: 'gsk_0123456789', NVIDIA_API_KEY: 'nvapi-0123456789' });
  assert.equal(maskKey('gsk_A1b2A1b2A1b2'), 'gsk_…1b2');
});
