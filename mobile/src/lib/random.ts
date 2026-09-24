import { getRandomBytes } from 'expo-crypto';

/** Nonces and request ids — from the platform's CSPRNG, never Math.random. */
export const random = (n: number): Uint8Array => getRandomBytes(n);
