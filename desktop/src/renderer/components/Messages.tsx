import {
  Activity,
  Brain, Check, ChevronRight, CircleHelp, File, FilePen, Folder, GitBranch, Globe, Image, Info,
  LoaderCircle, Pencil, RotateCw, Save, Search, ShieldAlert, Sparkles, Square, Terminal, TriangleAlert, Volume2, Wrench, X,
} from 'lucide-react';
import { memo, useEffect, useState, type MouseEvent } from 'react';
import { fmtBytes } from '../../shared/media';
import type { ImageRef } from '../../shared/types';
import { confirmChoices, diffLines, elapsed, toolHeader, toolLook, toolTitle, workSummary, type ChatItem } from '../lib/chat';
import { renderMarkdown } from '../lib/markdown';
import { useSearchOpen } from './Search';

const TOOL_ICONS = {
  file: File, edit: FilePen, save: Save, folder: Folder, search: Search, globe: Globe,
  terminal: Terminal, sparkles: Sparkles, brain: Brain, git: GitBranch, wrench: Wrench,
} as const;

/** Copy buttons inside rendered Markdown (the HTML is static, so delegate). */
function onMarkdownClick(e: MouseEvent<HTMLDivElement>): void {
  const target = e.target as HTMLElement;
  if (target.matches('[data-copy]')) {
    const code = target.closest('.code-block')?.querySelector('code')?.textContent ?? '';
    void navigator.clipboard.writeText(code);
    target.textContent = 'Копирано';
    setTimeout(() => { target.textContent = 'Копирай'; }, 1400);
    return;
  }
  const link = target.closest('a');
  if (link?.href) {
    e.preventDefault();
    void window.genesis.openExternal(link.href);
  }
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return <div className="md" onClick={onMarkdownClick} dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />;
});

function CopyButton({ text, title = 'Копирай отговора' }: { text: string; title?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="icon-btn subtle"
      title={title}
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1400);
      }}
    >
      {done ? <Check size={14} /> : <span className="copy-glyph">⧉</span>}
    </button>
  );
}

/** Изпратена картинка: името и размерът (байтовете не се връщат от агента). */
function ImageChip({ image }: { image: ImageRef }) {
  const size = image.width && image.height ? `${image.width}×${image.height}` : '';
  const title = [image.name, size, image.bytes ? fmtBytes(image.bytes) : '', image.mime].filter(Boolean).join(' · ');
  return (
    <span className="image-chip" title={title}>
      <Image size={13} /> <span className="image-chip-name">{image.name || 'картинка'}</span>
      {size && <span className="image-chip-size">{size}</span>}
    </span>
  );
}

/** Над толкова твоето съобщение се сгъва (поставен дълъг лог или файл). */
const LONG_LINES = 14;
const LONG_CHARS = 1200;

export function UserMessage({ text, images, onEdit }: { text: string; images?: ImageRef[]; onEdit?: (text: string) => void }) {
  const long = text.split('\n').length > LONG_LINES || text.length > LONG_CHARS;
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="msg user">
      {/* Вляво от балончето, при посочване — не добавя празен ред под него. */}
      {text && (
        <div className="msg-actions user-actions">
          <CopyButton text={text} title="Копирай съобщението" />
          {onEdit && (
            <button className="icon-btn subtle" title="Редактирай — връща текста в полето" onClick={() => onEdit(text)}>
              <Pencil size={13} />
            </button>
          )}
        </div>
      )}
      <div className="user-col">
        <div className={`bubble ${long && !expanded ? 'clamped' : ''}`}>
          {images && images.length > 0 && (
            <div className="bubble-images">{images.map((im, i) => <ImageChip key={i} image={im} />)}</div>
          )}
          {text}
        </div>
        {long && (
          <button className="more-btn" onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Сгъни' : `Покажи всичко (${text.split('\n').length} реда)`}
          </button>
        )}
      </div>
    </div>
  );
}

/** Бутонът „Прочети на глас“ на отговор: чете / зарежда / спира. */
export type Speech = { phase: 'idle' | 'loading' | 'playing'; onToggle: () => void };

function SpeakButton({ speech }: { speech: Speech }) {
  const title = speech.phase === 'idle' ? 'Прочети на глас' : speech.phase === 'loading' ? 'Озвучавам… (спри)' : 'Спри четенето';
  return (
    <button className={`icon-btn subtle ${speech.phase !== 'idle' ? 'on' : ''}`} title={title} onClick={speech.onToggle}>
      {speech.phase === 'idle' ? <Volume2 size={14} />
        : speech.phase === 'loading' ? <LoaderCircle size={14} className="spin" /> : <Square size={11} fill="currentColor" />}
    </button>
  );
}

export function AssistantMessage({ text, speech, took, last }: { text: string; speech?: Speech; took?: number; last?: boolean }) {
  return (
    <div className={`msg assistant ${last ? 'last' : ''}`}>
      <div className="avatar"><span className="logo-dot" /></div>
      <div className="body">
        <Markdown text={text} />
        <div className={`msg-actions ${speech && speech.phase !== 'idle' ? 'shown' : ''}`}>
          <CopyButton text={text} />
          {speech && <SpeakButton speech={speech} />}
          {took !== undefined && took >= 1000 && <span className="took" title="Колко продължи ходът">за {elapsed(took)}</span>}
        </div>
      </div>
    </div>
  );
}

/** Изходът на инструмента; промените (EDIT_FILE) — зелено/червено като в git. */
function ToolBody({ item }: { item: Extract<ChatItem, { kind: 'tool' }> }) {
  const diff = diffLines(item.result);
  return (
    <pre className="tool-body">
      {diff ? diff.map((l, i) => <span key={i} className={`dl ${l.kind}`}>{l.text}{'\n'}</span>) : item.result}
      {item.clipped && <span className="clipped">{'\n'}… (съкратено — пълният изход е в терминала)</span>}
    </pre>
  );
}

export function ToolCard({ item, workspace }: { item: Extract<ChatItem, { kind: 'tool' }>; workspace?: string }) {
  const [manual, setOpen] = useState(false);
  // Търсенето (Ctrl+F) отваря картата, в чийто изход е съвпадението.
  const found = useSearchOpen().tools.has(item.key);
  const open = manual || found;
  const look = toolLook(item.name);
  const Icon = TOOL_ICONS[look.icon as keyof typeof TOOL_ICONS] ?? Wrench;
  const head = toolHeader(item.name, item.result, workspace);
  return (
    <div className={`tool ${open ? 'open' : ''} ${head.failed ? 'failed' : ''}`}>
      <button className="tool-head" onClick={() => setOpen(!open)}>
        <ChevronRight size={14} className="chev" />
        <Icon size={14} className="tool-icon" />
        <span className="tool-name" title={item.name}>{toolTitle(item.name)}</span>
        <span className="tool-sum" title={head.target}>{head.target}</span>
        {head.extra && <span className="tool-extra">{head.extra}</span>}
        {head.failed && <span className="tool-bad">грешка</span>}
      </button>
      {open && <ToolBody item={item} />}
    </div>
  );
}

const NOTE_ICON = { info: Info, warn: TriangleAlert, error: TriangleAlert, asked: CircleHelp, progress: Activity } as const;

export function Note({ item, onRetry }: { item: Extract<ChatItem, { kind: 'note' }>; onRetry?: () => void }) {
  const Icon = NOTE_ICON[item.tone];
  if (item.tone === 'asked') {
    return (
      <div className="note asked">
        <div className="note-title"><Icon size={15} /> Genesis пита</div>
        <Markdown text={item.text} />
      </div>
    );
  }
  return (
    <div className={`note ${item.tone}`}>
      <Icon size={14} />
      <span className="note-text">{item.text}</span>
      {onRetry && (
        <button className="btn small retry" onClick={onRetry} title="Праща същото съобщение пак">
          <RotateCw size={13} /> Опитай пак
        </button>
      )}
    </div>
  );
}

/**
 * Стъпките на един ход в една сгъваема група (2026-11-20). Докато ходът върви,
 * е отворена и се пълни на живо; щом свърши — сгъва се до един ред.
 */
export function WorkGroup({ items, live, workspace }: { items: ChatItem[]; live: boolean; workspace?: string }) {
  const [manual, setManual] = useState<boolean | null>(null);
  // Търсенето (Ctrl+F) държи отворена група със съвпадение (ключът на реда — chat.buildRows).
  const found = useSearchOpen().groups.has(`w${items[0]?.key}`);
  const open = found || (manual ?? live);
  const sum = workSummary(items, workspace);
  const icons = [...new Set(items.filter((i) => i.kind === 'tool').map((i) => toolLook((i as { name: string }).name).icon))].slice(0, 6);
  const parts = [
    sum.steps ? `${sum.steps} ${sum.steps === 1 ? 'стъпка' : 'стъпки'}` : `${items.length} бележки`,
    sum.changed.length ? `промени ${sum.changed.slice(0, 3).join(', ')}${sum.changed.length > 3 ? ` и още ${sum.changed.length - 3}` : ''}` : '',
  ].filter(Boolean);
  return (
    <div className={`work ${open ? 'open' : ''} ${live ? 'live' : ''}`}>
      <button className="work-head" onClick={() => setManual(!open)} title={open ? 'Сгъни стъпките' : 'Покажи стъпките'}>
        <ChevronRight size={14} className="chev" />
        {live ? <LoaderCircle size={14} className="spin work-mark" /> : <Wrench size={14} className="work-mark" />}
        <span className="work-title">{live ? 'Работи' : 'Работа'}</span>
        <span className="work-sum">{parts.join(' · ')}</span>
        {sum.failed > 0 && <span className="work-failed" title="Неуспешни стъпки (често е нормално — напр. тест преди поправката)">✗ {sum.failed}</span>}
        <span className="work-icons">
          {icons.map((ic) => { const I = TOOL_ICONS[ic as keyof typeof TOOL_ICONS] ?? Wrench; return <I key={ic} size={12} />; })}
        </span>
      </button>
      {open && (
        <div className="work-body">
          {items.map((it) => {
            if (it.kind === 'tool') return <ToolCard key={it.key} item={it} workspace={workspace} />;
            if (it.kind === 'note') return <Note key={it.key} item={it} />;
            if (it.kind === 'assistant') return <div key={it.key} className="interim"><Markdown text={it.text} /></div>;
            return null;
          })}
        </div>
      )}
    </div>
  );
}

export function ConfirmCard({ item, onAnswer }: {
  item: Extract<ChatItem, { kind: 'confirm' }>;
  onAnswer: (id: string, allow: boolean, session?: boolean) => void;
}) {
  // One click answers: the buttons go away at once (the agent keeps the first
  // answer too — RemoteSession.answer), so a double click cannot flip it.
  const [sent, setSent] = useState(false);
  const pending = item.state === 'pending' && !sent;
  const choices = confirmChoices(item);
  const answer = (allow: boolean, session = false) => {
    if (sent) return;
    setSent(true);
    onAnswer(item.id, allow, session);
  };
  return (
    <div className={`confirm ${item.state}`}>
      <div className="confirm-title">
        <ShieldAlert size={16} />
        {item.state === 'pending' ? (sent ? 'Изпратено…' : 'Genesis иска разрешение')
          : item.state === 'allowed' ? (item.remembered ? 'Разрешено за тази сесия' : 'Разрешено') : 'Отказано'}
        {item.note && <span className="confirm-note">{item.note}</span>}
      </div>
      <pre className="confirm-op">{item.operation}</pre>
      {item.reasons.length > 0 && (
        <ul className="confirm-reasons">{item.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      )}
      {pending && (
        <div className="confirm-actions">
          <button className="btn primary" onClick={() => answer(true)}>
            <Check size={14} /> Разреши <kbd>Enter</kbd>
          </button>
          {choices.includes('session') && (
            <button className="btn" title={`Без въпрос до края на сесията: ${item.sessionRule}`}
                    onClick={() => answer(true, true)}>
              <Check size={14} /> За тази сесия
            </button>
          )}
          <button className="btn" onClick={() => answer(false)}>
            <X size={14} /> Откажи <kbd>Esc</kbd>
          </button>
        </div>
      )}
    </div>
  );
}

export function Thinking({ label, since }: { label: string; since: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="thinking">
      <span className="orbit"><i /><i /><i /></span>
      <span className="shimmer">{label}</span>
      <span className="muted">· {elapsed(now - since)} · Esc спира</span>
    </div>
  );
}
