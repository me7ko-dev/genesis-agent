// „Копирай“ (2026-11-23). expo-clipboard е нативен модул, добавен днес: на
// билд, правен преди това (старо APK с нов JS), самото му зареждане хвърля
// грешка. Затова НЕ се внася най-горе в Cards.tsx — тогава целият чат не се
// отваряше (преглед на PR #131). Зарежда се чак при докосване, в try/catch.

type ClipboardModule = { setStringAsync(text: string): Promise<boolean> };
type WebClipboard = { writeText(text: string): Promise<void> };

const expoClipboard = (): Promise<ClipboardModule> => import('expo-clipboard');

function webClipboard(): WebClipboard | undefined {
  const nav = (globalThis as { navigator?: { clipboard?: WebClipboard } }).navigator;
  return nav?.clipboard && typeof nav.clipboard.writeText === 'function' ? nav.clipboard : undefined;
}

/**
 * Слага текста в клипборда. Никога не хвърля: false = не стана (старият билд
 * няма модула) и бутонът го казва, вместо приложението да падне.
 */
export async function copyText(
  text: string,
  load: () => Promise<ClipboardModule> = expoClipboard,
  web: WebClipboard | undefined = webClipboard(),
): Promise<boolean> {
  try {
    const clipboard = await load();
    if (await clipboard.setStringAsync(text)) return true;
  } catch (e) {
    console.warn('genesis: copy', e instanceof Error ? e.message : String(e));
  }
  // В браузъра (expo web) има и вграден клипборд.
  try {
    if (web) {
      await web.writeText(text);
      return true;
    }
  } catch (e) {
    console.warn('genesis: copy (web)', e instanceof Error ? e.message : String(e));
  }
  return false;
}
