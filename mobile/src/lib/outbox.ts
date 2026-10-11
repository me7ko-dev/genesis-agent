/**
 * Изпращането на едно съобщение: по едно наведнъж и с идентификатор, който
 * преживява повторен опит.
 *
 * Одит 2026-10-11: „Изпрати“ проверяваше само `busy`, а то идва чак с
 * отговора — две докосвания през 150 ms пускаха /plan два пъти (вкл. и пак
 * изкл.). И отговор, изгубен по LAN, оставяше „няма връзка“; второто
 * натискане пускаше хода пак. Сега второто докосване, докато първото е на
 * път, не прави нищо, а повторният опит със СЪЩИЯ текст носи същия `mid` —
 * сървърът отговаря с първия отговор, без втори ход.
 *
 * Преглед 2026-10-11: `mid` се пазеше без край — по-късно, нарочно изпратено
 * „продължи“ или второ /plan (менюто вече предлага „Изпълни плана“) идваше
 * със стария `mid` и сървърът го поглъщаше: 1 ход вместо 2. Сега `mid` се
 * забравя, щом собственото балонче с този текст дойде в събитията (сървърът
 * го е приел), и най-късно RETRY_WINDOW_MS след последния опит.
 */
export const RETRY_WINDOW_MS = 2 * 60 * 1000;

type Unsent = { text: string; mid: string; at: number };

export class Outbox {
  private inFlight = false;
  private unsent: Unsent | null = null;
  private readonly newMid: () => string;
  private readonly now: () => number;

  // Без „parameter property“: тестовете под Node само махат типовете.
  constructor(newMid: () => string, now: () => number = Date.now) {
    this.newMid = newMid;
    this.now = now;
  }

  get sending(): boolean {
    return this.inFlight;
  }

  /** null — друго изпращане още е на път; това докосване не направи нищо.
   *  Хвърлената грешка минава нататък, а `mid` се пази за повторния опит. */
  async send<T>(text: string, deliver: (text: string, mid: string) => Promise<T>): Promise<T | null> {
    if (this.inFlight) return null;
    this.inFlight = true;
    const last = this.unsent;
    const retry = last !== null && last.text === text && this.now() - last.at < RETRY_WINDOW_MS;
    const mid = retry ? last.mid : this.newMid();
    // Прозорецът тече от последния опит: оператор, който натиска пак и пак
    // без връзка, не получава нов `mid` насред повторенията.
    this.unsent = { text, mid, at: this.now() };
    try {
      const reply = await deliver(text, mid);
      // Отговор има — сървърът е решил (прието, „busy“, „too_long“…).
      this.unsent = null;
      return reply;
    } finally {
      this.inFlight = false;
    }
  }

  /** Новите събития от сървъра: собственото балонче с неизпратения текст
   *  значи, че сървърът го е приел — следващото изпращане е нов ход. */
  seen(events: readonly { type: string; text?: string }[]): void {
    const pending = this.unsent;
    if (pending === null) return;
    const text = pending.text.trim();
    if (events.some((e) => e.type === 'user' && (e.text ?? '').trim() === text)) this.unsent = null;
  }
}
