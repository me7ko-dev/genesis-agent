import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { BusyAfterSend, losesConnection } from './busy';
import { mergeEvents, reuseItems, thinkingLabel, toChatItems, type ChatItem } from './chat';
import { checkImages, hasFeature, limitsFrom } from './media';
import { errorText, MAX_MESSAGE, messageLength, replyErrorText, sendErrorText, voiceErrorText } from './messages';
import { Outbox } from './outbox';
import {
  b64urlEncode, GenesisClient, ProtocolError, type GenesisEvent, type OutgoingImage, type Pairing, type PhoneState,
  type StatusReply,
} from './protocol';
import { random } from './random';

export type Connection = 'connecting' | 'online' | 'offline' | 'unauthorized' | 'clock';

const LONG_POLL_S = 20;
const NO_STATE: PhoneState = { plan: false, todos: [] };
const BACKOFF_MS = [1000, 2000, 5000, 10000];

/**
 * One live link to `genesis serve`: status, the event stream (long-poll,
 * resumes from the last event after the phone was offline or asleep), and
 * the actions. Polling pauses while the app is in the background.
 */
export function useGenesis(pairing: Pairing) {
  const client = useMemo(() => new GenesisClient(pairing, random), [pairing]);
  // Едно изпращане наведнъж, `mid` за повторния опит (lib/outbox.ts).
  const outbox = useMemo(() => new Outbox(() => b64urlEncode(random(12))), [client]);
  // busy от `send` само без допитване междувременно (lib/busy.ts, 2026-10-27).
  const busyGate = useMemo(() => new BusyAfterSend(), [client]);
  const [connection, setConnection] = useState<Connection>('connecting');
  const [status, setStatus] = useState<StatusReply | null>(null);
  const [events, setEvents] = useState<GenesisEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [phone, setPhone] = useState<PhoneState>(NO_STATE);
  const [error, setError] = useState('');
  const [active, setActive] = useState(AppState.currentState === 'active');
  const lastSeq = useRef(0);
  const epoch = useRef('');

  useEffect(() => {
    const sub = AppState.addEventListener('change', (s) => setActive(s === 'active'));
    return () => sub.remove();
  }, []);

  const fail = useCallback((e: unknown): Connection => {
    const kind = e instanceof ProtocolError ? e.kind : 'network';
    const next: Connection = kind === 'unauthorized' || kind === 'protocol' ? 'unauthorized'
      : kind === 'clock' ? 'clock' : 'offline';
    setConnection(next);
    return next;
  }, []);

  useEffect(() => {
    if (!active) return undefined;
    let stopped = false;
    let attempt = 0;
    let needStatus = true;
    let first = true;

    const loop = async () => {
      while (!stopped) {
        try {
          if (needStatus) {
            const s = await client.status();
            if (s.key_id !== client.keyId()) throw new ProtocolError('another computer', 'unauthorized');
            if (stopped) return;
            setStatus(s);
            setConnection('online');
            needStatus = false;
            first = true;
          }
          // Right after (re)connecting: what we missed, at once. Then wait.
          const reply = await client.events(lastSeq.current, first ? 0 : LONG_POLL_S);
          // Над тавана сървърът праща най-старите и "more": дотегляме веднага,
          // без дълго чакане, докато дупката се запълни (одит 2026-10-11).
          first = !!reply.more;
          if (stopped) return;
          // A restarted server numbers its events from 1 again: what it just
          // sent was counted from our old seq, so start over from 0 (audit
          // 2026-10-07: the new run's first events were skipped).
          if (epoch.current !== '' && reply.epoch !== epoch.current) {
            epoch.current = reply.epoch;
            lastSeq.current = 0;
            setEvents([]);
            setPhone(NO_STATE);
            first = true;
            continue;
          }
          const reset = reply.reset;
          epoch.current = reply.epoch;
          lastSeq.current = reply.last;
          setEvents((cur) => mergeEvents(cur, reply.events, reset));
          // Своето балонче дойде → изпращането е прието, следващото със
          // същия текст е нов ход, не повторен опит (преглед 2026-10-11).
          outbox.seen(reply.events, reply.mids);
          busyGate.polled();
          setBusy(reply.busy);
          if (reply.state) setPhone(reply.state);
          setConnection('online');
          attempt = 0;
        } catch (e) {
          if (stopped) return;
          needStatus = true;
          if (fail(e) === 'unauthorized') return;
          await sleep(BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]);
          attempt += 1;
        }
      }
    };
    loop();
    return () => {
      stopped = true;
    };
  }, [client, outbox, busyGate, active, fail]);

  const act = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    setError('');
    const noPoll = busyGate.since();
    try {
      return await fn();
    } catch (e) {
      // Отговор на сървъра (413, 429 "busy" на голям плик, 500) значи, че
      // компютърът е тук — не „няма връзка“: точката ставаше червена и
      // „Изпрати“ стоеше изключено до следващото допитване (2026-10-15).
      // Същото, ако допитване е минало, докато заявката чакаше (2026-10-27).
      if (losesConnection(e, !noPoll())) fail(e);
      // На екрана — на български; суровото съобщение е за дневника (одит
      // 2026-10-11: „cannot reach http://…“ и „server error 413“ стигаха
      // до оператора дословно).
      console.warn('genesis:', e instanceof Error ? e.message : String(e));
      setError(errorText(e));
      return null;
    }
  }, [fail, busyGate]);

  const features = useMemo(() => ({
    voice: hasFeature(status, 'voice'),
    images: hasFeature(status, 'images'),
  }), [status]);
  const limits = useMemo(() => limitsFrom(status), [status]);

  // `images` — снимките над полето (2026-10-15). Проверката на сървъра се прави
  // и тук: 10 MiB не пътуват, за да чуем „не“. Повторният опит носи същия `mid`
  // само със същите снимки (outbox, `attachments`).
  const send = useCallback(async (text: string, images: readonly OutgoingImage[] = []) => {
    if (outbox.sending) return false;
    if (messageLength(text) > MAX_MESSAGE) {
      setError(replyErrorText('too_long'));
      return false;
    }
    if (images.length) {
      const problem = features.images ? checkImages(images, limits, text) : 'unknown_op';
      if (problem) {
        setError(problem === 'unknown_op' ? 'Genesis на компютъра не приема снимки — обнови го.' : replyErrorText(problem));
        return false;
      }
    }
    setSending(true);
    const allowBusy = busyGate.sending();
    try {
      const reply = await act(() => outbox.send(text, (t, mid) => client.send(t, mid, images),
                                                images.map((i) => i.id).join(',')));
      if (reply && !reply.ok) {
        setError(sendErrorText(reply));
        return false;
      }
      // Бърз ход (/todos, /plan) може да е свършил преди този отговор — тогава
      // допитването вече е казало busy = false и то важи (одит 2026-10-27).
      if (reply && allowBusy()) setBusy(true);
      return !!reply;
    } finally {
      setSending(false);
    }
  }, [act, client, outbox, busyGate, features.images, limits]);

  // Гласът (2026-10-15): говор → текст за полето (НЕ се праща сам) и
  // отговор → mp3. null — грешката вече е на екрана.
  const transcribe = useCallback(async (mime: string, data: string): Promise<string | null> => {
    const reply = await act(() => client.transcribe(mime, data));
    if (!reply) return null;
    if (!reply.ok || typeof reply.text !== 'string') {
      setError(voiceErrorText('transcribe', reply));
      return null;
    }
    if (!reply.text.trim()) {
      setError(voiceErrorText('transcribe', { error: 'empty' }));
      return null;
    }
    return reply.text;
  }, [act, client]);

  const speak = useCallback(async (text: string): Promise<{ mime: string; data: string } | null> => {
    const reply = await act(() => client.speak(text));
    if (!reply) return null;
    if (!reply.ok || typeof reply.data !== 'string' || !reply.data) {
      setError(voiceErrorText('speak', reply));
      return null;
    }
    return { mime: reply.mime || 'audio/mpeg', data: reply.data };
  }, [act, client]);

  const confirm = useCallback(async (id: string, allow: boolean, session = false) => {
    const reply = await act(() => client.confirm(id, allow, session));
    // ok:false — already answered, timed out or stopped: confirm_done shows
    // what happened, nothing to retry.
    if (reply && !reply.ok) setError('Това потвърждение вече е отговорено или изтече.');
    return reply !== null;
  }, [act, client]);
  const stop = useCallback(() => act(() => client.stop()), [act, client]);
  // „busy“ се казва, не се гълта: „Нов разговор“ насред хода не правеше нищо
  // и мълчеше (одит 2026-10-11).
  const clear = useCallback(async () => {
    const reply = await act(() => client.clear());
    if (reply && !reply.ok) setError(replyErrorText(reply.error));
    return !!reply?.ok;
  }, [act, client]);

  // Непроменените елементи остават същите обекти (chat.reuseItems).
  const shown = useRef<ChatItem[]>([]);
  const items = useMemo(() => {
    shown.current = reuseItems(shown.current, toChatItems(events));
    return shown.current;
  }, [events]);
  const label = useMemo(() => thinkingLabel(events), [events]);

  return { connection, status, items, busy, sending, label, error, setError, send, confirm, stop, clear,
           transcribe, speak, features, limits, plan: phone.plan, todos: phone.todos };
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}
