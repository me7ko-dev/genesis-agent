/**
 * Микрофонът и високоговорителят (2026-10-15) — expo-audio (SDK 57; expo-av
 * вече го няма в SDK 57). Записът става текст на компютъра (`transcribe`),
 * отговорът — mp3 от компютъра (`speak`); тук е само звукът.
 */
import {
  createAudioPlayer, RecordingPresets, requestRecordingPermissionsAsync, setAudioModeAsync,
  useAudioRecorder, useAudioRecorderState, type AudioPlayer, type RecordingOptions,
} from 'expo-audio';
import { File, Paths } from 'expo-file-system';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Platform } from 'react-native';

import { MAX_RECORDING_MS, recordingMime, stripDataUrl, webMicAvailable } from './media';

/** Говор, не музика: моно и 64 kbps AAC — 2 минути ≈ 1 MB вместо 2 MB при
 *  HIGH_QUALITY (стерео, 128 kbps); Whisper и без това слуша 16 kHz моно.
 *  Браузърът: webm/opus, а където го няма (Safari) — каквото MediaRecorder
 *  даде; mime-ът се чете от записа (media.recordingMime). */
const SPEECH: RecordingOptions = {
  ...RecordingPresets.HIGH_QUALITY,
  numberOfChannels: 1,
  bitRate: 64000,
  web: { mimeType: 'audio/webm', bitsPerSecond: 64000 },
};

/** Има ли микрофон тук: на телефона — да; в браузъра — само в защитен
 *  контекст (https или localhost). */
export function micAvailable(): boolean {
  if (Platform.OS !== 'web') return true;
  const g = globalThis as { isSecureContext?: boolean; navigator?: { mediaDevices?: { getUserMedia?: unknown } }; MediaRecorder?: unknown };
  return webMicAvailable({
    isSecureContext: g.isSecureContext,
    hasGetUserMedia: typeof g.navigator?.mediaDevices?.getUserMedia === 'function',
    hasMediaRecorder: typeof g.MediaRecorder === 'function',
  });
}

export type Recording = { mime: string; data: string };

/**
 * Запис с бутон: start() → stop() дава base64 и mime; cancel() изхвърля.
 * Спира сам на 2 минути (таванът на сървъра). Докато записва, възпроизвеждането
 * е спряно (stopPlayback), иначе микрофонът чува собствения отговор.
 */
export function useRecorder(onFull: () => void) {
  const recorder = useAudioRecorder(SPEECH);
  const state = useAudioRecorderState(recorder, 250);
  const [recording, setRecording] = useState(false);
  const busy = useRef(false);

  // Таванът: състоянието идва на 250 ms — записът е ≤ 120.25 s, а сървърът
  // приема до 121 (voice.MAX_AUDIO_SECONDS + 1).
  const full = recording && state.durationMillis >= MAX_RECORDING_MS;
  const onFullRef = useRef(onFull);
  onFullRef.current = onFull;
  useEffect(() => {
    if (full) onFullRef.current();
  }, [full]);

  const start = useCallback(async (): Promise<string | null> => {
    if (busy.current || recording) return null;
    busy.current = true;
    try {
      stopPlayback();
      const permission = await requestRecordingPermissionsAsync();
      if (!permission.granted) return 'Няма достъп до микрофона — разреши го в настройките на телефона.';
      // iOS: allowsRecording превключва сесията в PlayAndRecord — само докато
      // записваме, иначе отговорите вървят тихо през слушалката.
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      await recorder.prepareToRecordAsync();
      recorder.record();
      setRecording(true);
      return null;
    } catch (e) {
      console.warn('genesis: record', e instanceof Error ? e.message : String(e));
      await setAudioModeAsync({ allowsRecording: false }).catch(() => undefined);
      return 'Записът не можа да започне.';
    } finally {
      busy.current = false;
    }
  }, [recorder, recording]);

  const finish = useCallback(async (keep: boolean): Promise<Recording | null> => {
    if (busy.current || !recording) return null;
    busy.current = true;
    try {
      await recorder.stop();
      setRecording(false);
      await setAudioModeAsync({ allowsRecording: false }).catch(() => undefined);
      const uri = recorder.uri;
      if (!keep || !uri) return null;
      return await readRecording(uri);
    } finally {
      setRecording(false);
      busy.current = false;
    }
  }, [recorder, recording]);

  return {
    recording,
    durationMs: recording ? state.durationMillis : 0,
    start,
    stop: useCallback(() => finish(true), [finish]),
    cancel: useCallback(async () => {
      await finish(false);
    }, [finish]),
  };
}

/** Записът като base64. На телефона — файлът от кеша (и после се трие);
 *  в браузъра — blob: адресът от MediaRecorder. */
async function readRecording(uri: string): Promise<Recording> {
  if (Platform.OS === 'web') {
    const blob = await (await fetch(uri)).blob();
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error ?? new Error('read failed'));
      reader.onload = () => resolve(String(reader.result ?? ''));
      reader.readAsDataURL(blob);
    });
    URL.revokeObjectURL(uri);
    return { mime: recordingMime('web', blob.type), data: stripDataUrl(data) };
  }
  const file = new File(uri);
  const data = await file.base64();
  try {
    file.delete();
  } catch {
    // кешът се чисти и от системата
  }
  return { mime: recordingMime(Platform.OS), data };
}

// ── възпроизвеждане ─────────────────────────────────────────────────────────

let player: AudioPlayer | null = null;
let playerFile: File | null = null;
let playingKey: string | null = null;
const listeners = new Set<(key: string | null) => void>();

function setPlaying(key: string | null) {
  playingKey = key;
  for (const l of listeners) l(key);
}

/** Спира и освобождава текущия отговор, ако има такъв. */
export function stopPlayback(): void {
  const p = player;
  const f = playerFile;
  player = null;
  playerFile = null;
  if (p) {
    try {
      p.pause();
      p.remove();
    } catch {
      // вече освободен
    }
  }
  if (f) {
    try {
      f.delete();
    } catch {
      // кешът се чисти и от системата
    }
  }
  if (playingKey !== null) setPlaying(null);
}

/** mp3 от `speak` → звук. Един наведнъж: нов отговор спира предишния.
 *  На телефона — през файл в кеша (плейърът чете адрес, не base64); в
 *  браузъра — data: адрес. */
export async function playAudio(key: string, mime: string, data: string): Promise<void> {
  stopPlayback();
  await setAudioModeAsync({ playsInSilentMode: true, allowsRecording: false }).catch(() => undefined);
  let source: string;
  if (Platform.OS === 'web') {
    source = `data:${mime || 'audio/mpeg'};base64,${data}`;
  } else {
    const file = new File(Paths.cache, `genesis-reply-${Date.now()}.mp3`);
    file.create({ overwrite: true });
    file.write(data, { encoding: 'base64' });
    playerFile = file;
    source = file.uri;
  }
  const p = createAudioPlayer(source);
  player = p;
  setPlaying(key);
  p.addListener('playbackStatusUpdate', (status) => {
    if (status.didJustFinish && player === p) stopPlayback();
  });
  p.play();
}

/** Кой отговор звучи сега (ключът на елемента) — за бутона ▶/■. */
export function usePlayingKey(): string | null {
  const [key, setKey] = useState<string | null>(playingKey);
  useEffect(() => {
    listeners.add(setKey);
    return () => {
      listeners.delete(setKey);
    };
  }, []);
  return key;
}
