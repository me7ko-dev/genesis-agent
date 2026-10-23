/**
 * Кои адреси в Markdown/HTML от модела могат да се заредят сами (одит
 * 2026-10-22). Прозорецът е file:// страница: `//host/x.png` и
 * `\\host\share\x.png` ставаха file://host/share/x.png, DOMPurify ги пускаше,
 * а 'self' в CSP — също. Под Electron Chromium наистина поиска
 * file://evil.example/share/a.png (и .mp4 от <video>); под Windows това е UNC —
 * SMB към чуждия хост и NTLM хешът на потребителя, без нито едно щракване,
 * само от показан отговор (prompt injection в прочетена страница стига).
 *
 * Тук няма DOM — само чисти функции, за `npm test` и pytest.
 */

/** Атрибути, по които браузърът тегли сам (без щракване). */
const RESOURCE_ATTRS = new Set(['src', 'srcset', 'poster', 'background', 'xlink:href', 'href']);

/** Само картинките и звукът на самия прозорец: data: и blob:. */
function localOnly(value: string): boolean {
  // Chromium маха водещи интервали и контролни знаци преди да разбере адреса.
  const v = value.replace(/^[\x00-\x20]+/, '').toLowerCase();
  return v.startsWith('data:') || v.startsWith('blob:');
}

/**
 * Може ли атрибутът да остане. <a href> се отваря само при щракване и main
 * пуска навън само http(s) (setWindowOpenHandler/will-navigate), затова
 * остава; всеки друг адрес за ресурс — само data:/blob:. srcset изобщо не:
 * в него има запетаи и в data: адресите, а моделът няма нужда от него.
 */
export function resourceAllowed(tag: string, name: string, value: string): boolean {
  const attr = name.toLowerCase();
  if (!RESOURCE_ATTRS.has(attr)) return true;
  if (attr === 'href' && tag.toLowerCase() === 'a') return true;
  if (attr === 'srcset') return false;
  return localOnly(value);
}

/** style="" с url()/image-set() или CSS екраниране (`\75 rl(`) тегли картинка. */
export function styleAllowed(value: string): boolean {
  return !/url\s*\(|image|\\|@import/i.test(value);
}
