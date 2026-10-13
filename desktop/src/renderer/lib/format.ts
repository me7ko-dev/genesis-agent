/** 1234 → "1,2K", 2795326 → "2,80M": short enough for a tile, bg-BG decimals. */
export function fmtTokens(n: number): string {
  const f = (v: number, d: number) => v.toLocaleString('bg-BG', { minimumFractionDigits: d, maximumFractionDigits: d });
  if (n < 1000) return String(n);
  if (n < 999_500) return `${f(n / 1000, n < 9_950 ? 1 : 0)}K`;
  return `${f(n / 1_000_000, 2)}M`;
}

/** The chart's top gridline: the next 1, 2, 2.5 or 5 × 10ⁿ at or above `v`. */
export function niceMax(v: number): number {
  const exp = 10 ** Math.floor(Math.log10(Math.max(1, v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= v) return m * exp;
  return 10 * exp;
}
