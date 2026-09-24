import { ArrowUp, Square } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';

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
  { group: 'Работа', name: '/tasks', aliases: ['/задачи', '/state'], hint: 'Отворени нишки и решения' },
  { group: 'Работа', name: '/done', args: '<номер>', aliases: ['/готово'], hint: 'Затвори нишка като готова' },
  { group: 'Работа', name: '/drop', args: '<номер>', hint: 'Изхвърли нишка' },
  { group: 'Работа', name: '/skills', aliases: ['/умения'], hint: 'Уменията на Genesis' },
  { group: 'Система', name: '/backup', hint: 'Архивирай работната папка в GENESIS_BACKUP_DIR' },
  { group: 'Система', name: '/update', aliases: ['/ъпдейт'], hint: 'Има ли нова версия на Genesis' },
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
  onSend: (text: string) => Promise<boolean>;
  onStop: () => void;
  onCommand: (name: string, arg: string) => boolean;
  focusKey: number;
};

const HISTORY_MAX = 100;

export function Composer({ busy, disabled, placeholder, onSend, onStop, onCommand, focusKey }: Props) {
  const [text, setText] = useState('');
  const [menuIndex, setMenuIndex] = useState(0);
  const history = useRef<string[]>([]);
  const historyAt = useRef(-1);
  const area = useRef<HTMLTextAreaElement>(null);

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

  async function submit(raw: string) {
    const value = raw.trim();
    if (!value || disabled) return;
    const [word, ...rest] = value.split(/\s+/);
    const cmd = findCommand(word);
    if (cmd && onCommand(cmd.name, rest.join(' '))) {
      setText('');
      return;
    }
    if (busy) return;
    if (await onSend(value)) {
      history.current = [value, ...history.current.filter((h) => h !== value)].slice(0, HISTORY_MAX);
      historyAt.current = -1;
      setText('');
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
      <div className={`composer ${disabled ? 'disabled' : ''}`}>
        <textarea
          ref={area}
          value={text}
          rows={1}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => { setText(e.target.value); setMenuIndex(0); }}
          onKeyDown={onKey}
        />
        {busy ? (
          <button className="send stop" title="Спри (Esc)" onClick={onStop}>
            <Square size={13} fill="currentColor" />
          </button>
        ) : (
          <button className="send" title="Изпрати (Enter)" disabled={disabled || !text.trim()} onClick={() => void submit(text)}>
            <ArrowUp size={17} />
          </button>
        )}
      </div>
      <div className="composer-foot">
        <span><kbd>Enter</kbd> изпраща · <kbd>Shift</kbd>+<kbd>Enter</kbd> нов ред · <kbd>/</kbd> команди</span>
      </div>
    </div>
  );
}
