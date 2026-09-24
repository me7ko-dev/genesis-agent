/** Just enough Markdown for what models write. Pure, so tests/ can run it. */

export type Block = { code: boolean; text: string };

/** ``` fences → code blocks; everything else → paragraphs. */
export function splitBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  const parts = text.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[\w+-]*\n/, '').replace(/\n$/, '');
      blocks.push({ code: true, text: body });
    } else if (part.trim()) {
      blocks.push({ code: false, text: part.replace(/^\n+|\n+$/g, '') });
    }
  });
  return blocks;
}

/** **bold** and `code` inside a line. */
export function splitInline(line: string): { t: string; bold?: boolean; code?: boolean }[] {
  const out: { t: string; bold?: boolean; code?: boolean }[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  for (const m of line.matchAll(re)) {
    if (m.index! > last) out.push({ t: line.slice(last, m.index) });
    const tok = m[0];
    out.push(tok.startsWith('**') ? { t: tok.slice(2, -2), bold: true } : { t: tok.slice(1, -1), code: true });
    last = m.index! + tok.length;
  }
  if (last < line.length) out.push({ t: line.slice(last) });
  return out;
}
