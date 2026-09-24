import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { mergeEvents, thinkingLabel, toChatItems } from './chat';
import { GenesisClient, ProtocolError, type GenesisEvent, type Pairing, type StatusReply } from './protocol';
import { random } from './random';

export type Connection = 'connecting' | 'online' | 'offline' | 'unauthorized' | 'clock';

const LONG_POLL_S = 20;
const BACKOFF_MS = [1000, 2000, 5000, 10000];

/**
 * One live link to `genesis serve`: status, the event stream (long-poll,
 * resumes from the last event after the phone was offline or asleep), and
 * the actions. Polling pauses while the app is in the background.
 */
export function useGenesis(pairing: Pairing) {
  const client = useMemo(() => new GenesisClient(pairing, random), [pairing]);
  const [connection, setConnection] = useState<Connection>('connecting');
  const [status, setStatus] = useState<StatusReply | null>(null);
  const [events, setEvents] = useState<GenesisEvent[]>([]);
  const [busy, setBusy] = useState(false);
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
          first = false;
          if (stopped) return;
          // A restarted server numbers its events from 1 again.
          const reset = reply.reset || (epoch.current !== '' && reply.epoch !== epoch.current);
          epoch.current = reply.epoch;
          lastSeq.current = reply.last;
          setEvents((cur) => mergeEvents(cur, reply.events, reset));
          setBusy(reply.busy);
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
  }, [client, active, fail]);

  const act = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    setError('');
    try {
      return await fn();
    } catch (e) {
      fail(e);
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [fail]);

  const send = useCallback(async (text: string) => {
    const reply = await act(() => client.send(text));
    if (reply && !reply.ok) {
      setError(reply.error === 'busy' ? 'Genesis още работи по предишното.' : reply.error ?? 'грешка');
      return false;
    }
    if (reply) setBusy(true);
    return !!reply;
  }, [act, client]);

  const confirm = useCallback((id: string, allow: boolean) => act(() => client.confirm(id, allow)), [act, client]);
  const stop = useCallback(() => act(() => client.stop()), [act, client]);
  const clear = useCallback(() => act(() => client.clear()), [act, client]);

  const items = useMemo(() => toChatItems(events), [events]);
  const label = useMemo(() => thinkingLabel(events), [events]);

  return { connection, status, items, busy, label, error, send, confirm, stop, clear };
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}
