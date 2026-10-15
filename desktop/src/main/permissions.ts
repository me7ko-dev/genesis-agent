/**
 * Какво може прозорецът да поиска от Chromium (2026-10-15, с микрофона).
 *
 * Без свой handler Electron разрешава ВСЯКО искане — камера, местоположение,
 * известия, екран. Прозорецът показва текст от модела (Markdown през
 * DOMPurify), затова му оставяме само нужното: микрофона (само звук, за
 * диктовката) и записа в клипборда (бутоните „Копирай“). Поставянето на
 * снимка (Ctrl+V) и пускането на файл не искат разрешение.
 *
 * Без Electron тук — за `npm test`.
 */

export type PermissionInfo = {
  /** Искане (setPermissionRequestHandler): какво точно иска getUserMedia. */
  mediaTypes?: string[];
  /** Проверка (setPermissionCheckHandler): 'audio' | 'video' | 'unknown'. */
  mediaType?: string;
};

const ALWAYS = new Set(['clipboard-sanitized-write']);

/** true — разреши; само за собствения прозорец (main.ts проверява кой пита). */
export function allowPermission(permission: string, info: PermissionInfo = {}): boolean {
  if (ALWAYS.has(permission)) return true;
  if (permission !== 'media') return false;
  if (info.mediaTypes !== undefined) {
    // Искане за камера (или камера + микрофон) — не: диктовката е само звук.
    return info.mediaTypes.length > 0 && info.mediaTypes.every((t) => t === 'audio');
  }
  // Проверка: 'unknown' е enumerateDevices / permissions.query без вид.
  return info.mediaType === 'audio' || info.mediaType === 'unknown' || info.mediaType === undefined;
}
