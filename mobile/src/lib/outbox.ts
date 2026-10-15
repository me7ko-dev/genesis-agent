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

type Unsent = { text: string; attachments: string; mid: string; at: number };

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
   *  Хвърлената грешка минава нататък, а `mid` се пази за повторния опит.
   *
   *  `attachments` — кои снимки носи съобщението (идентификаторите им,
   *  2026-10-15): повторният опит е „същият“ само със същия текст И същите
   *  снимки. Иначе празен текст със снимка А, изгубен отговор, после празен
   *  текст със снимка Б получаваше стария `mid` и сървърът отговаряше с хода
   *  на А, без да види Б. */
  async send<T>(text: string, deliver: (text: string, mid: string) => Promise<T>, attachments = ''): Promise<T | null> {
    if (this.inFlight) return null;
    this.inFlight = true;
    const last = this.unsent;
    // Часовник, върнат назад, не е „в прозореца“ (вълна 5, 2026-10-11).
    const age = last === null ? -1 : this.now() - last.at;
    const retry = last !== null && last.text === text && last.attachments === attachments
      && age >= 0 && age < RETRY_WINDOW_MS;
    const mid = retry ? last.mid : this.newMid();
    // Прозорецът тече от последния опит: оператор, който натиска пак и пак
    // без връзка, не получава нов `mid` насред повторенията.
    this.unsent = { text, attachments, mid, at: this.now() };
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
   *  или `mid`-ът в списъка с приетите (`mids` на `events`) значи, че
   *  сървърът го е приел — следващото изпращане е нов ход.
   *
   *  Вълна 5, 2026-10-11: само по балончето reset страница без него (дълъг
   *  ход, докато телефонът е офлайн) и „/clear“ (няма балонче) оставяха
   *  стария `mid`, а нарочното повторение в прозореца се поглъщаше. */
  seen(events: readonly { type: string; text?: string }[], accepted?: readonly string[]): void {
    const pending = this.unsent;
    if (pending === null) return;
    if (accepted?.includes(pending.mid)) {
      this.unsent = null;
      return;
    }
    const text = pending.text.trim();
    // Само снимки, без текст: балончето носи въпроса на сървъра
    // (IMAGE_QUESTION), не празното — решават `mids` (2026-10-15).
    if (!text) return;
    if (events.some((e) => e.type === 'user' && (e.text ?? '').trim() === text)) this.unsent = null;
  }
}
