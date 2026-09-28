// The API keys from the computer: `genesis keys qr` (genesis_agent/keys_transfer.py)
// shows genesisremote://keys?gq=…&gq2=…&ol=… — one short code per key.
// Same table as keys_transfer.SHORT.

const LONG: Record<string, string> = {
  ol: 'OLLAMA_API_KEY', gq: 'GROQ_API_KEY', or: 'OPENROUTER_API_KEY',
  cb: 'CEREBRAS_API_KEY', nv: 'NVIDIA_API_KEY', gm: 'GEMINI_API_KEY',
  hf: 'HF_TOKEN', sn: 'SAMBANOVA_API_KEY', tg: 'TOGETHER_API_KEY',
  co: 'COHERE_API_KEY', oa: 'OPENAI_API_KEY', ds: 'DEEPSEEK_API_KEY',
  an: 'ANTHROPIC_API_KEY', tt: 'GENESIS_TELEGRAM_TOKEN', tc: 'GENESIS_TELEGRAM_CHAT_ID',
};

const KEYS_LINK = /^genesisremote:\/\/keys\?/i;
const VALUE = /^[\x21-\x7e]{8,1024}$/;

export function isKeysLink(raw: string): boolean {
  return KEYS_LINK.test(raw.trim());
}

/** gq → GROQ_API_KEY, ol3 → OLLAMA_API_KEY_3; '' for anything else. */
export function longName(code: string): string {
  const m = /^([a-z]{2})(\d{0,2})$/.exec(code);
  if (!m || !LONG[m[1]]) return '';
  const n = m[2] ? Number(m[2]) : 1;
  if (m[2] && (n < 2 || n > 10)) return '';
  return LONG[m[1]] + (m[2] ? `_${m[2]}` : '');
}

/** Short codes and values (the link's query, or expo-router's params) → named keys. */
export function keysFromParams(params: Record<string, unknown>): Record<string, string> {
  const keys: Record<string, string> = {};
  for (const [code, value] of Object.entries(params)) {
    const name = longName(code);
    const v = Array.isArray(value) ? value[0] : value;
    if (name && typeof v === 'string' && VALUE.test(v.trim())) keys[name] = v.trim();
  }
  return keys;
}

export function parseKeysLink(raw: string): Record<string, string> | null {
  const text = raw.trim();
  if (!isKeysLink(text)) return null;
  const params: Record<string, string> = {};
  for (const part of text.slice(text.indexOf('?') + 1).split('&')) {
    const eq = part.indexOf('=');
    if (eq <= 0) continue;
    try {
      params[decodeURIComponent(part.slice(0, eq))] = decodeURIComponent(part.slice(eq + 1).replace(/\+/g, ' '));
    } catch {
      return null;
    }
  }
  const keys = keysFromParams(params);
  return Object.keys(keys).length ? keys : null;
}

export function maskKey(value: string): string {
  return value.length > 12 ? `${value.slice(0, 4)}…${value.slice(-3)}` : '…';
}
