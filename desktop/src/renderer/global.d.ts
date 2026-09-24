import type { GenesisApi } from '../shared/types';

declare global {
  interface Window {
    genesis: GenesisApi;
  }
}
