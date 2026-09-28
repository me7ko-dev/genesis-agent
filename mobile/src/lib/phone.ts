import { type Permission, PermissionsAndroid } from 'react-native';

import TermuxBridge from '../../modules/termux-bridge';
import type { Pairing } from './protocol';

// Genesis on the phone itself, no computer: the agent runs in Termux
// (`genesis phone`, genesis_agent/phone.py; installed by
// scripts/install-termux.sh) and this app talks to it on 127.0.0.1.

const TERMUX_FILES = '/data/data/com.termux/files';
const GENESIS_BIN = `${TERMUX_FILES}/usr/bin/genesis`;
const TERMUX_HOME = `${TERMUX_FILES}/home`;
const RUN_PERMISSION = 'com.termux.permission.RUN_COMMAND';

export const TERMUX_DOWNLOAD = 'https://f-droid.org/packages/com.termux/';

/** Android with the native module: the phone-only mode is offered. */
export const phoneModeAvailable = TermuxBridge != null;

// The branch this APK was built from (native.yml sets it), so a test build
// installs the matching agent. Release builds come from main.
const REF = process.env.EXPO_PUBLIC_GENESIS_REF || 'main';

/** What the user pastes into Termux, once. */
export const INSTALL_COMMAND =
  REF === 'main'
    ? 'curl -fsSL https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install-termux.sh | bash'
    : `curl -fsSL https://raw.githubusercontent.com/me7ko-dev/genesis-agent/refs/heads/${REF}/scripts/install-termux.sh | GENESIS_REF=${REF} bash`;

/** Paired with Genesis on this phone, not on a computer. */
export function isOnThisPhone(pairing: Pairing): boolean {
  return /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(pairing.base);
}

export function termuxInstalled(): boolean {
  try {
    return TermuxBridge?.isTermuxInstalled() ?? false;
  } catch {
    return false;
  }
}

export function openTermux(): boolean {
  try {
    return TermuxBridge?.openTermux() ?? false;
  } catch {
    return false;
  }
}

export type StartResult = 'started' | 'no-termux' | 'no-permission' | 'failed';

async function ensurePermission(): Promise<boolean> {
  if (!TermuxBridge) return false;
  if (TermuxBridge.hasRunPermission()) return true;
  const answer = await PermissionsAndroid.request(RUN_PERMISSION as Permission, {
    title: 'Genesis и Termux',
    message: 'За да пуска Genesis на телефона, приложението трябва да може да стартира команди в Termux.',
    buttonPositive: 'Добре',
  });
  return answer === PermissionsAndroid.RESULTS.GRANTED;
}

/**
 * `genesis phone <command>` in Termux. `start` and `stop` run in the
 * background; `pair` brings Termux to the front, which then opens this app paired
 * (Android lets only the app on screen open another one).
 */
export async function runGenesis(command: 'start' | 'stop' | 'pair'): Promise<StartResult> {
  if (!TermuxBridge) return 'failed';
  if (!termuxInstalled()) return 'no-termux';
  try {
    if (!(await ensurePermission())) return 'no-permission';
    const background = command !== 'pair';
    const ok = TermuxBridge.runCommand(GENESIS_BIN, ['phone', command], TERMUX_HOME, background);
    // Opened by this app (on screen now), Termux is surely in front — its
    // own service may not be allowed to bring it there from the background.
    if (ok && !background) TermuxBridge.openTermux();
    return ok ? 'started' : 'failed';
  } catch {
    return 'failed';
  }
}

export const START_ERROR: Record<Exclude<StartResult, 'started'>, string> = {
  'no-termux': 'Termux не е инсталиран. Инсталирай го от F-Droid и постави командата за Genesis в него.',
  'no-permission': 'Без разрешението „Изпълнение на команди в Termux“ приложението не може да пусне Genesis. Настройки → Приложения → Genesis Remote → Разрешения → Допълнителни разрешения.',
  failed: 'Termux не прие командата. Отвори Termux и постави командата за инсталиране още веднъж (тя разрешава на приложението да пуска Genesis).',
};
