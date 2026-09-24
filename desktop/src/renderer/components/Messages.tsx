import {
  Brain, Check, ChevronRight, CircleHelp, File, FilePen, Folder, GitBranch, Globe, Info,
  Save, Search, ShieldAlert, Sparkles, Terminal, TriangleAlert, Wrench, X,
} from 'lucide-react';
import { memo, useEffect, useState, type MouseEvent } from 'react';
import { elapsed, toolHeader, toolLook, type ChatItem } from '../lib/chat';
import { renderMarkdown } from '../lib/markdown';

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

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="icon-btn subtle"
      title="Копирай отговора"
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

export function UserMessage({ text }: { text: string }) {
  return (
    <div className="msg user">
      <div className="bubble">{text}</div>
    </div>
  );
}

export function AssistantMessage({ text }: { text: string }) {
  return (
    <div className="msg assistant">
      <div className="avatar"><span className="logo-dot" /></div>
      <div className="body">
        <Markdown text={text} />
        <div className="msg-actions"><CopyButton text={text} /></div>
      </div>
    </div>
  );
}

export function ToolCard({ item }: { item: Extract<ChatItem, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false);
  const look = toolLook(item.name);
  const Icon = TOOL_ICONS[look.icon as keyof typeof TOOL_ICONS] ?? Wrench;
  const head = toolHeader(item.name, item.result);
  return (
    <div className={`tool ${open ? 'open' : ''} ${head.failed ? 'failed' : ''}`}>
      <button className="tool-head" onClick={() => setOpen(!open)}>
        <ChevronRight size={14} className="chev" />
        <Icon size={14} className="tool-icon" />
        <span className="tool-name">{item.name}</span>
        <span className="tool-sum" title={head.target}>{head.target}</span>
        {head.extra && <span className="tool-extra">{head.extra}</span>}
      </button>
      {open && (
        <pre className="tool-body">
          {item.result}
          {item.clipped && <span className="clipped">{'\n'}… (съкратено — пълният изход е в терминала)</span>}
        </pre>
      )}
    </div>
  );
}

const NOTE_ICON = { info: Info, warn: TriangleAlert, error: TriangleAlert, asked: CircleHelp } as const;

export function Note({ item }: { item: Extract<ChatItem, { kind: 'note' }> }) {
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
      <span>{item.text}</span>
    </div>
  );
}

export function ConfirmCard({ item, onAnswer }: {
  item: Extract<ChatItem, { kind: 'confirm' }>;
  onAnswer: (id: string, allow: boolean) => void;
}) {
  const pending = item.state === 'pending';
  return (
    <div className={`confirm ${item.state}`}>
      <div className="confirm-title">
        <ShieldAlert size={16} />
        {pending ? 'Genesis иска разрешение' : item.state === 'allowed' ? 'Разрешено' : 'Отказано'}
        {item.note && <span className="confirm-note">{item.note}</span>}
      </div>
      <pre className="confirm-op">{item.operation}</pre>
      {item.reasons.length > 0 && (
        <ul className="confirm-reasons">{item.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      )}
      {pending && (
        <div className="confirm-actions">
          <button className="btn primary" onClick={() => onAnswer(item.id, true)}>
            <Check size={14} /> Разреши <kbd>Enter</kbd>
          </button>
          <button className="btn" onClick={() => onAnswer(item.id, false)}>
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
