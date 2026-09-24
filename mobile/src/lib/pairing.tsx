import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import type { Pairing } from './protocol';
import { clearPairing, loadPairing, savePairing } from './storage';

type PairingState = {
  pairing: Pairing | null;
  loaded: boolean;
  pair: (p: Pairing) => Promise<void>;
  unpair: () => Promise<void>;
};

const Ctx = createContext<PairingState | null>(null);

export function PairingProvider({ children }: { children: ReactNode }) {
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    loadPairing().then((p) => {
      setPairing(p);
      setLoaded(true);
    });
  }, []);

  const pair = useCallback(async (p: Pairing) => {
    await savePairing(p);
    setPairing(p);
  }, []);

  const unpair = useCallback(async () => {
    await clearPairing();
    setPairing(null);
  }, []);

  const value = useMemo(() => ({ pairing, loaded, pair, unpair }), [pairing, loaded, pair, unpair]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePairing(): PairingState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('usePairing outside PairingProvider');
  return ctx;
}
