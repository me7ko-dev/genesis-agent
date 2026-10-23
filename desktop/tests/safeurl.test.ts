/**
 * Адресите в отговора на модела (одит 2026-10-22): `//host` и `\\host\share`
 * ставаха file://host/... на file:// страницата — под Windows UNC и NTLM хеш.
 * npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resourceAllowed, styleAllowed } from '../src/renderer/lib/safeurl.ts';

test('a resource from model output loads only from data: or blob:', () => {
  for (const v of ['//evil/share/a.png', '\\\\evil\\share\\a.png', '/\\evil/a.png', ' //evil/a.png',
    'file://evil/share/a.png', 'https://evil/a.png', 'a.png']) {
    assert.equal(resourceAllowed('IMG', 'src', v), false, v);
    assert.equal(resourceAllowed('video', 'poster', v), false, v);
  }
  assert.equal(resourceAllowed('img', 'srcset', 'data:image/png;base64,AA 1x'), false);
  assert.equal(resourceAllowed('image', 'href', '//evil/a.png'), false);
  assert.equal(resourceAllowed('img', 'src', 'data:image/png;base64,AA=='), true);
  assert.equal(resourceAllowed('audio', 'src', 'blob:file:///x'), true);
  // <a> се отваря само при щракване, а main пуска навън само http(s).
  assert.equal(resourceAllowed('A', 'href', 'https://example.com'), true);
  assert.equal(resourceAllowed('img', 'alt', '//evil'), true);
});

test('style that can fetch an image is dropped', () => {
  assert.equal(styleAllowed('color: red'), true);
  assert.equal(styleAllowed('background: URL( //evil/x.png )'), false);
  assert.equal(styleAllowed('background: \\75 rl(//evil/x.png)'), false);
  assert.equal(styleAllowed('background-image: image-set("//evil/x.png" 1x)'), false);
});
