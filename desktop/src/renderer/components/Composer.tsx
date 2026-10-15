import { ArrowUp, ImagePlus, LoaderCircle, Mic, Square, Volume2, VolumeX, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import {
  baseMime, clock, DEFAULT_LIMITS, imageMimeOf, MAX_RECORD_MS, micErrorText, pastedName, takeImages, voiceErrorText,
  type ImageAttachment, type MediaLimits,
} from '../../shared/media';
import { blobToBase64, MicRecorder, toAttachment, type Attachment } from '../lib/capture';

export type SlashCommand = { name: string; aliases?: string[]; args?: string; hint: string; group: string };

export const COMMANDS: SlashCommand[] = [
  { group: 'Разход', name: '/usage', aliases: ['/cost', '/разход'], hint: 'Колко е похарчено: токени, пари, квоти, по дни' },
  { group: 'Разход', name: '/status', aliases: ['/статус'], hint: 'Модел, контекст и тази сесия' },
  { group: 'Модели', name: '/model', aliases: ['/agent', '/модел'], hint: 'Избери доставчик и модел' },
  { group: 'Модели', name: '/models', hint: 'Веригата от резервни модели' },
  { group: 'Модели', name: '/maxcoding', aliases: ['/макскод'], hint: 'Вкл./изкл. най-силните безплатни модели за код' },
  { group: 'Модели', name: '/local_model_max', aliases: ['/локален_макс'], hint: 'Вкл./изкл. само локален qwen3:14b (мощен, бавен)' },
  { group: 'Модели', name: '/local_model_normal', aliases: ['/локален_нормал'], hint: 'Вкл./изкл. само локален qwen2.5-coder:7b (бърз)' },
  { group: 'Разговор', name: '/clear', aliases: ['/нов'], hint: 'Нов разговор' },
  { group: 'Разговор', name: '/history', aliases: ['/история'], hint: 'Стари разговори: прегледай и зареди' },
  { group: 'Разговор', name: '/stop', hint: 'Спри текущата задача' },
  { group: 'Разговор', name: '/plan', aliases: ['/план'], hint: 'Режим план: само чете и планира; /plan пак — изпълнява. /plan <задача> — план за нея' },
  { group: 'Разговор', name: '/undo', aliases: ['/назад'], hint: 'Връща файловете от последния ход (пита първо)' },
  { group: 'Разговор', name: '/rewind', args: '[номер]', hint: 'Назад до твое съобщение: реже разговора и връща файловете след него (пита първо)' },
  { group: 'Разговор', name: '/resume', args: '[номер]', hint: 'По-ранен разговор от тази папка: /resume показва, /resume <номер> зарежда' },
  { group: 'Разговор', name: '/compact', hint: 'Компресира историята на разговора' },
  { group: 'Разговор', name: '/context', hint: 'С какво е пълен контекстът' },
  { group: 'Разговор', name: '/todos', hint: 'Списъкът със задачи на агента' },
  { group: 'Работа', name: '/tasks', aliases: ['/задачи', '/state'], hint: 'Отворени нишки и решения' },
  { group: 'Работа', name: '/done', args: '<номер>', aliases: ['/готово'], hint: 'Затвори нишка като готова' },
  { group: 'Работа', name: '/drop', args: '<номер>', hint: 'Изхвърли нишка' },
  { group: 'Работа', name: '/skills', aliases: ['/умения'], hint: 'Уменията на Genesis' },
  { group: 'Работа', name: '/review', hint: 'Преглед на промените за грешки (/review <клон>)' },
  { group: 'Работа', name: '/bg', hint: 'Фоновите команди (dev сървъри) — състояние' },
  { group: 'Работа', name: '/agents', hint: 'Под-агентите (.genesis/agents/*.md)' },
  { group: 'Работа', name: '/memory', hint: 'Какво помни Genesis за тази папка' },
  { group: 'Работа', name: '/init', hint: 'Създай GENESIS.md за папката' },
  { group: 'Работа', name: '/commands', hint: 'Твоите команди (.genesis/commands)' },
  { group: 'Работа', name: '/hooks', hint: 'Куките (.genesis/hooks); доверяването е само в терминала' },
  { group: 'Работа', name: '/mcp', hint: 'MCP сървърите; login/trust са само в терминала' },
  { group: 'Работа', name: '/permissions', aliases: ['/разрешения'], hint: 'Командите без въпрос; add е само в терминала' },
  { group: 'Система', name: '/backup', hint: 'Архивирай работната папка в GENESIS_BACKUP_DIR' },
  { group: 'Система', name: '/export', aliases: ['/предай'], hint: 'Проектът за предаване: zip + отчет (тестове, как се пуска)' },
  { group: 'Система', name: '/update', aliases: ['/ъпдейт'], hint: 'Има ли нова версия на Genesis' },
  { group: 'Система', name: '/doctor', hint: 'Работи ли всичко: git, Python, ruff/pytest, ключове, MCP, Chromium (/doctor --models — и моделите)' },
  { group: 'Система', name: '/folder', aliases: ['/папка'], hint: 'Отвори друга папка' },
  { group: 'Система', name: '/terminal', hint: 'Genesis в терминала' },
  { group: 'Система', name: '/logs', hint: 'Дневникът на агента' },
  { group: 'Система', name: '/help', aliases: ['/помощ'], hint: 'Всички команди и клавиши' },
];

export function findCommand(word: string): SlashCommand | undefined {
  const w = word.toLowerCase();
  return COMMANDS.find((c) => c.name === w || c.aliases?.includes(w));
}

type Props = {
  busy: boolean;
  disabled: boolean;
  placeholder: string;
  onSend: (text: string, images: ImageAttachment[]) => Promise<boolean>;
  onStop: () => void;
  onCommand: (name: string, arg: string) => boolean;
  focusKey: number;
  /** Агентът приема картинки / говор ("features" на `status`, 2026-10-15). */
  images?: boolean;
  voice?: boolean;
  limits?: MediaLimits;
  /** „Чети отговорите на глас“ — пази се в settings.json. */
  speak?: boolean;
  onSpeak?: (on: boolean) => void;
  onFlash: (text: string) => void;
};

const HISTORY_MAX = 100;

type MicState = { phase: 'idle' } | { phase: 'recording'; ms: number } | { phase: 'transcribing' };

/** Файловете от пускане, поставяне или избор, които са картинки. */
function imageFiles(list: FileList | DataTransferItemList | null | undefined): File[] {
  if (!list) return [];
  const out: File[] = [];
  for (const x of Array.from(list as ArrayLike<File | DataTransferItem>)) {
    const file = x instanceof File ? x : x.kind === 'file' ? x.getAsFile() : null;
    if (file && imageMimeOf(file.type, file.name)) out.push(file);
  }
  return out;
}

function hasFiles(e: DragEvent): boolean {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files');
}

export function Composer({ busy, disabled, placeholder, onSend, onStop, onCommand, focusKey,
                           images: imagesOn = false, voice = false, limits = DEFAULT_LIMITS,
                           speak = false, onSpeak, onFlash }: Props) {
  const [text, setText] = useState('');
  const [menuIndex, setMenuIndex] = useState(0);
  const [attached, setAttached] = useState<Attachment[]>([]);
  const [reading, setReading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [mic, setMic] = useState<MicState>({ phase: 'idle' });
  const history = useRef<string[]>([]);
  const historyAt = useRef(-1);
  const area = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const recorder = useRef<MicRecorder | null>(null);
  // Колко са вече прикачени и колко се четат — за тавана при бързи поставяния.
  const taken = useRef(0);
  taken.current = attached.length + reading;

  useEffect(() => { area.current?.focus(); }, [focusKey, disabled]);

  // Grow with the text up to a third of the window.
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    const max = Math.round(window.innerHeight / 3);
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
    // A scrollbar only when the text really does not fit.
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden';
  }, [text]);

  const menu = useMemo(() => {
    if (!text.startsWith('/') || text.includes(' ') || text.includes('\n')) return [];
    const q = text.toLowerCase();
    return COMMANDS.filter((c) => c.name.startsWith(q) || c.aliases?.some((a) => a.startsWith(q)));
  }, [text]);

  // ── картинките (2026-10-15): бутон, Ctrl+V (снимка на екрана), пускане ──
  const addFiles = useCallback(async (files: File[], pasted = false) => {
    if (!files.length) return;
    if (!imagesOn) { onFlash('Този genesis не приема картинки — обнови го (/update).'); return; }
    const { take, dropped } = takeImages(taken.current, files, limits.images);
    if (dropped) onFlash(`Най-много ${limits.images} картинки в едно съобщение.`);
    if (!take.length) return;
    taken.current += take.length;
    setReading((n) => n + take.length);
    for (const file of take) {
      try {
        // Поставената снимка на екрана идва като „image.png“ — всички еднакви.
        const mime = imageMimeOf(file.type, file.name) ?? 'image/png';
        const name = pasted || !file.name || file.name === 'image.png' ? pastedName(mime) : file.name;
        const a = await toAttachment(file, name);
        setAttached((cur) => (cur.length >= limits.images ? cur : [...cur, a]));
      } catch (e) {
        onFlash((e as Error).message);
      } finally {
        setReading((n) => n - 1);
      }
    }
    area.current?.focus();
  }, [imagesOn, limits.images, onFlash]);

  useEffect(() => {
    // Поставяне навсякъде в прозореца: Win+Shift+S, после Ctrl+V.
    function onPaste(e: ClipboardEvent) {
      // Текст от Word/Excel носи и картинка на избраното — там се иска текстът.
      if (e.clipboardData?.getData('text/plain')) return;
      const files = imageFiles(e.clipboardData?.items);
      if (!files.length) return;
      e.preventDefault();
      void addFiles(files, true);
    }
    // Пускане върху прозореца (will-navigate в main.ts и без това не отваря файла).
    let depth = 0;
    function onEnter(e: DragEvent) { if (hasFiles(e)) { depth++; setDragging(true); } }
    function onLeave(e: DragEvent) { if (hasFiles(e) && --depth <= 0) { depth = 0; setDragging(false); } }
    function onOver(e: DragEvent) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    }
    function onDrop(e: DragEvent) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      setDragging(false);
      const all = e.dataTransfer?.files;
      const files = imageFiles(all);
      if (!files.length) { onFlash('Само картинки: PNG, JPEG, WebP или GIF.'); return; }
      if (all && files.length < all.length) onFlash('Пропуснах файловете, които не са картинки.');
      void addFiles(files);
    }
    window.addEventListener('paste', onPaste);
    window.addEventListener('dragenter', onEnter);
    window.addEventListener('dragleave', onLeave);
    window.addEventListener('dragover', onOver);
    window.addEventListener('drop', onDrop);
    return () => {
      window.removeEventListener('paste', onPaste);
      window.removeEventListener('dragenter', onEnter);
      window.removeEventListener('dragleave', onLeave);
      window.removeEventListener('dragover', onOver);
      window.removeEventListener('drop', onDrop);
    };
  }, [addFiles, onFlash]);

  // ── микрофонът (2026-10-15): запис → `transcribe` → текст в полето ──────
  const finishRecording = useCallback(async () => {
    const rec = recorder.current;
    recorder.current = null;
    if (!rec) return;
    setMic({ phase: 'transcribing' });
    try {
      const got = await rec.stop();
      if (!got || got.ms < 400) { onFlash('Записът е твърде кратък.'); return; }
      const r = await window.genesis.transcribe(baseMime(got.mime), await blobToBase64(got.blob));
      const heard = r.ok ? (r.text ?? '').trim() : '';
      if (!r.ok) { onFlash(voiceErrorText('transcribe', r)); return; }
      if (!heard) { onFlash('Не се чу нищо.'); return; }
      // Текстът е за редакция: не се праща сам (както на телефона).
      setText((cur) => (cur.trim() ? `${cur.replace(/\s+$/, '')} ${heard}` : heard));
      setTimeout(() => {
        const el = area.current;
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
      }, 0);
    } finally {
      setMic({ phase: 'idle' });
    }
  }, [onFlash]);

  const startRecording = useCallback(async () => {
    if (recorder.current) return;
    const rec = new MicRecorder();
    recorder.current = rec;
    try {
      await rec.start(() => { onFlash('2 минути — спрях записа.'); void finishRecording(); });
      setMic({ phase: 'recording', ms: 0 });
    } catch (e) {
      recorder.current = null;
      rec.cancel();
      onFlash(micErrorText((e as DOMException).name));
    }
  }, [finishRecording, onFlash]);

  const cancelRecording = useCallback(() => {
    recorder.current?.cancel();
    recorder.current = null;
    setMic({ phase: 'idle' });
  }, []);

  useEffect(() => {
    if (mic.phase !== 'recording') return;
    const t = setInterval(() => {
      setMic((m) => (m.phase === 'recording' ? { phase: 'recording', ms: recorder.current?.elapsed() ?? m.ms } : m));
    }, 250);
    return () => clearInterval(t);
  }, [mic.phase]);

  // Друга папка или затворен прозорец насред записа: микрофонът се освобождава.
  useEffect(() => () => { recorder.current?.cancel(); }, []);

  async function submit(raw: string) {
    const value = raw.trim();
    if ((!value && !attached.length) || disabled || reading) return;
    const [word, ...rest] = value.split(/\s+/);
    // С картинка „/plan …“ е текст към тях, не команда на прозореца.
    const cmd = attached.length ? undefined : findCommand(word);
    if (cmd && onCommand(cmd.name, rest.join(' '))) {
      setText('');
      return;
    }
    if (busy) return;
    const pictures = attached.map(({ mime, data, name }) => ({ mime, data, name }));
    if (await onSend(value, pictures)) {
      if (value) history.current = [value, ...history.current.filter((h) => h !== value)].slice(0, HISTORY_MAX);
      historyAt.current = -1;
      setText('');
      setAttached([]);
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (menu.length > 0) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const d = e.key === 'ArrowDown' ? 1 : -1;
        setMenuIndex((i) => (i + d + menu.length) % menu.length);
        return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault();
        const pick = menu[Math.min(menuIndex, menu.length - 1)];
        // A command that needs an argument waits for it.
        if (pick.args) setText(`${pick.name} `);
        else void submit(pick.name);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit(text);
      return;
    }
    // ↑ / ↓ on an empty (or recalled) box walks the history, like a shell.
    const el = e.currentTarget;
    if (e.key === 'ArrowUp' && el.selectionStart === 0 && history.current.length) {
      e.preventDefault();
      historyAt.current = Math.min(historyAt.current + 1, history.current.length - 1);
      setText(history.current[historyAt.current]);
    } else if (e.key === 'ArrowDown' && historyAt.current >= 0 && el.selectionEnd === text.length) {
      e.preventDefault();
      historyAt.current -= 1;
      setText(historyAt.current >= 0 ? history.current[historyAt.current] : '');
    }
  }

  const recording = mic.phase === 'recording';
  const full = attached.length + reading >= limits.images;

  return (
    <div className="composer-wrap">
      {menu.length > 0 && (
        <div className="slash-menu">
          {menu.map((c, i) => (
            <button
              key={c.name}
              className={i === Math.min(menuIndex, menu.length - 1) ? 'active' : ''}
              onMouseEnter={() => setMenuIndex(i)}
              onClick={() => { if (c.args) { setText(`${c.name} `); area.current?.focus(); } else void submit(c.name); }}
            >
              <span className="slash-name">{c.name}{c.args && <em> {c.args}</em>}</span>
              <span className="slash-hint">{c.hint}</span>
              {c.aliases && <span className="slash-alias">{c.aliases.join(' ')}</span>}
            </button>
          ))}
        </div>
      )}
      {dragging && imagesOn && (
        <div className="drop-hint"><ImagePlus size={18} /> Пусни картинката тук (до {limits.images})</div>
      )}
      {(attached.length > 0 || reading > 0) && (
        <div className="attachments">
          {attached.map((a) => (
            <div key={a.id} className="attachment" title={`${a.name} · ${a.width}×${a.height}`}>
              <img src={a.thumb} alt={a.name} />
              <button className="attachment-x" title="Махни"
                      onClick={() => setAttached((cur) => cur.filter((x) => x.id !== a.id))}>
                <X size={12} />
              </button>
            </div>
          ))}
          {Array.from({ length: reading }, (_, i) => (
            <div key={`r${i}`} className="attachment loading"><LoaderCircle size={16} className="spin" /></div>
          ))}
        </div>
      )}
      <div className={`composer ${disabled ? 'disabled' : ''} ${recording ? 'recording' : ''}`}>
        {imagesOn && (
          <>
            <button className="composer-tool" disabled={disabled || full} onClick={() => picker.current?.click()}
                    title={`Прикачи картинка — или Ctrl+V, или пусни файла върху прозореца (до ${limits.images})`}>
              <ImagePlus size={17} />
            </button>
            <input ref={picker} type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden
                   onChange={(e) => { const files = imageFiles(e.target.files); e.target.value = ''; void addFiles(files); }} />
          </>
        )}
        {recording && (
          <div className="recording-bar">
            <span className="rec-dot" /> Записвам… <span className="mono">{clock(mic.ms)}</span>
            <span className="muted">/ {clock(MAX_RECORD_MS)}</span>
          </div>
        )}
        <textarea
          ref={area}
          value={text}
          rows={1}
          hidden={recording}
          disabled={disabled}
          placeholder={mic.phase === 'transcribing' ? 'Разпознавам говора…' : placeholder}
          onChange={(e) => { setText(e.target.value); setMenuIndex(0); }}
          onKeyDown={onKey}
        />
        {voice && recording && (
          <button className="composer-tool" title="Откажи записа" onClick={cancelRecording}><X size={17} /></button>
        )}
        {voice && (
          <button className={`composer-tool mic ${mic.phase}`} disabled={disabled || mic.phase === 'transcribing'}
                  title={recording ? 'Спри и разпознай' : mic.phase === 'transcribing' ? 'Разпознавам…' : 'Диктувай (до 2 минути)'}
                  onClick={() => void (recording ? finishRecording() : startRecording())}>
            {mic.phase === 'transcribing' ? <LoaderCircle size={17} className="spin" />
              : recording ? <Square size={13} fill="currentColor" /> : <Mic size={17} />}
          </button>
        )}
        {busy ? (
          <button className="send stop" title="Спри (Esc)" onClick={onStop}>
            <Square size={13} fill="currentColor" />
          </button>
        ) : (
          <button className="send" title="Изпрати (Enter)"
                  disabled={disabled || reading > 0 || recording || (!text.trim() && !attached.length)}
                  onClick={() => void submit(text)}>
            <ArrowUp size={17} />
          </button>
        )}
      </div>
      <div className="composer-foot">
        <span>
          <kbd>Enter</kbd> изпраща · <kbd>Shift</kbd>+<kbd>Enter</kbd> нов ред · <kbd>/</kbd> команди
          {imagesOn && <> · <kbd>Ctrl</kbd>+<kbd>V</kbd> снимка</>}
        </span>
        {voice && onSpeak && (
          <button className={`foot-toggle ${speak ? 'on' : ''}`} onClick={() => onSpeak(!speak)}
                  title="Всеки завършен отговор се чете на глас">
            {speak ? <Volume2 size={13} /> : <VolumeX size={13} />} Чети отговорите на глас
          </button>
        )}
      </div>
    </div>
  );
}
