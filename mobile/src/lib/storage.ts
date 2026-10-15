import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

import type { Pairing } from './protocol';

const KEY = 'genesis.pairing.v1';

/**
 * The pairing holds the key that lets this phone run commands on the
 * computer: the Keychain on iOS, the Keystore-backed store on Android. The
 * web build (served by `genesis serve` itself) has only localStorage.
 */
export async function loadPairing(): Promise<Pairing | null> {
  try {
    const raw = Platform.OS === 'web' ? globalThis.localStorage?.getItem(KEY) : await SecureStore.getItemAsync(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Pairing;
    return parsed.base && parsed.key ? parsed : null;
  } catch {
    return null;
  }
}

export async function savePairing(pairing: Pairing): Promise<void> {
  const raw = JSON.stringify(pairing);
  if (Platform.OS === 'web') {
    globalThis.localStorage?.setItem(KEY, raw);
    return;
  }
  await SecureStore.setItemAsync(KEY, raw, { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY });
}

export async function clearPairing(): Promise<void> {
  try {
    if (Platform.OS === 'web') globalThis.localStorage?.removeItem(KEY);
    else await SecureStore.deleteItemAsync(KEY);
  } catch {
    // nothing stored — nothing to remove
  }
}

// ── настройки на приложението (2026-10-15) ──────────────────────────────────
// Не са тайна, но SecureStore е единственото хранилище, което приложението
// вече има на телефона — заради един флаг не добавяме още модул.

const PREFS = 'genesis.prefs.v1';

export type Prefs = {
  /** „Чети отговорите на глас“ — всеки нов отговор се изговаря, щом ходът свърши. */
  speakReplies: boolean;
};

export const DEFAULT_PREFS: Prefs = { speakReplies: false };

export async function loadPrefs(): Promise<Prefs> {
  try {
    const raw = Platform.OS === 'web' ? globalThis.localStorage?.getItem(PREFS) : await SecureStore.getItemAsync(PREFS);
    if (!raw) return DEFAULT_PREFS;
    const parsed = JSON.parse(raw) as Partial<Prefs>;
    return { speakReplies: parsed.speakReplies === true };
  } catch {
    return DEFAULT_PREFS;
  }
}

export async function savePrefs(prefs: Prefs): Promise<void> {
  try {
    const raw = JSON.stringify(prefs);
    if (Platform.OS === 'web') globalThis.localStorage?.setItem(PREFS, raw);
    else await SecureStore.setItemAsync(PREFS, raw);
  } catch {
    // не се записа — настройката важи до затварянето
  }
}
