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
 */
export class Outbox {
  private inFlight = false;
  private unsent: { text: string; mid: string } | null = null;
  private readonly newMid: () => string;

  // Без „parameter property“: тестовете под Node само махат типовете.
  constructor(newMid: () => string) {
    this.newMid = newMid;
  }

  get sending(): boolean {
    return this.inFlight;
  }

  /** null — друго изпращане още е на път; това докосване не направи нищо.
   *  Хвърлената грешка минава нататък, а `mid` се пази за повторния опит. */
  async send<T>(text: string, deliver: (text: string, mid: string) => Promise<T>): Promise<T | null> {
    if (this.inFlight) return null;
    this.inFlight = true;
    const mid = this.unsent?.text === text ? this.unsent.mid : this.newMid();
    this.unsent = { text, mid };
    try {
      const reply = await deliver(text, mid);
      // Отговор има — сървърът е решил (прието, „busy“, „too_long“…).
      this.unsent = null;
      return reply;
    } finally {
      this.inFlight = false;
    }
  }
}
