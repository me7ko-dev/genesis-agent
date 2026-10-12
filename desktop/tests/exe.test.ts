/**
 * Which genesis.exe the window runs.   npm test
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { exeCandidates } from '../src/main/exe.ts';

const home = join('C:', 'Users', 'u');
const local = join(home, 'AppData', 'Local');
const pipx = join(home, '.local', 'bin', 'genesis.exe');
const installDir = join(local, 'Programs', 'Genesis');
const installer = join(installDir, 'genesis.exe');

test('the genesis the terminal runs comes before the installer folder', () => {
  // 2026-10-12, this laptop: install.ps1 had left GENESIS_INSTALL_DIR on the
  // 24.09 build in Programs\Genesis; the terminal ran pipx's 12.10 one.
  const env = { LOCALAPPDATA: local, GENESIS_INSTALL_DIR: installDir };
  const list = exeCandidates({ env, home, onPath: [pipx, installer, ''] });
  assert.equal(list[0], pipx);
  assert.equal(list.indexOf(installer), 2);
});

test('without PATH: pipx folder first, then the installer folder', () => {
  assert.deepEqual(exeCandidates({ env: { LOCALAPPDATA: local }, home, onPath: [] }), [pipx, installer]);
  const bin = join('D:', 'pipx');
  assert.deepEqual(exeCandidates({ env: { LOCALAPPDATA: local, PIPX_BIN_DIR: bin }, home, onPath: [] })[0],
    join(bin, 'genesis.exe'));
});

test('the app setting and GENESIS_EXE win over everything', () => {
  const env = { LOCALAPPDATA: local, GENESIS_EXE: 'E:\\g.exe', GENESIS_INSTALL_DIR: 'F:\\gen' };
  const list = exeCandidates({ preferred: 'P:\\mine.exe', env, home, onPath: [pipx] });
  assert.deepEqual(list.slice(0, 3), ['P:\\mine.exe', 'E:\\g.exe', pipx]);
});

test('only an .exe from where.exe counts (not genesis.cmd or a blank line)', () => {
  const cmd = join(home, 'bin', 'genesis.cmd');
  const list = exeCandidates({ env: { LOCALAPPDATA: local }, home, onPath: [cmd, ` ${pipx}\r`] });
  assert.equal(list[0], pipx);
});
