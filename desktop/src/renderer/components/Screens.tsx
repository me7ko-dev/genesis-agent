import { Download, FolderOpen, RotateCw, ScrollText, TriangleAlert, Zap } from 'lucide-react';
import type { BackendState } from '../../shared/types';
import { baseName } from '../lib/chat';

const INSTALL = 'irm https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install.ps1 | iex';

export function Logo({ size = 56 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" className="logo" aria-hidden>
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#5eead4" />
          <stop offset="0.55" stopColor="#60a5fa" />
          <stop offset="1" stopColor="#c084fc" />
        </linearGradient>
      </defs>
      <circle cx="32" cy="32" r="26" fill="none" stroke="url(#lg)" strokeWidth="4" opacity="0.35" />
      <path d="M44 22a14 14 0 1 0 2 12H33" fill="none" stroke="url(#lg)" strokeWidth="5" strokeLinecap="round" />
      <circle cx="46" cy="18" r="3.5" fill="#5eead4" />
    </svg>
  );
}

export function Welcome({ onOpen, recent }: { onOpen: (p?: string) => void; recent: string[] }) {
  return (
    <div className="screen">
      <Logo size={72} />
      <h1>Genesis</h1>
      <p className="lead">Автономен агент за код, който работи в папката на проекта ти.</p>
      <button className="btn primary big" onClick={() => onOpen()}>
        <FolderOpen size={17} /> Отвори папка
      </button>
      {recent.length > 0 && (
        <div className="recent-grid">
          {recent.slice(0, 4).map((p) => (
            <button key={p} className="recent-card" onClick={() => onOpen(p)} title={p}>
              <span className="recent-name">{baseName(p)}</span>
              <span className="recent-path">{p}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Starting({ workspace, log }: { workspace: string; log: string }) {
  const last = log.split('\n').filter((l) => l.trim()).slice(-1)[0] ?? '';
  return (
    <div className="screen">
      <div className="boot"><Logo size={64} /></div>
      <h2>Стартирам Genesis…</h2>
      <p className="muted">{workspace}</p>
      <p className="boot-line">{last}</p>
    </div>
  );
}

export function Missing({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="screen">
      <Download size={40} className="accent" />
      <h2>Genesis Agent не е инсталиран</h2>
      <p className="lead">Приложението е прозорец към <code>genesis.exe</code>. Инсталирай го с един ред в PowerShell:</p>
      <div className="cmd-box">
        <code>{INSTALL}</code>
        <button className="btn" onClick={() => void navigator.clipboard.writeText(INSTALL)}>Копирай</button>
      </div>
      <button className="btn primary" onClick={onRetry}><RotateCw size={15} /> Провери пак</button>
    </div>
  );
}

export function Failed({ state, onRetry, onLogs, onOpen }: {
  state: BackendState; onRetry: () => void; onLogs: () => void; onOpen: () => void;
}) {
  const noKeys = /api.?key|ключ|setup/i.test(state.error ?? '');
  return (
    <div className="screen">
      <TriangleAlert size={40} className="warn-icon" />
      <h2>Genesis спря</h2>
      <p className="lead error-text">{state.error}</p>
      {noKeys && (
        <p className="muted"><Zap size={14} /> Ако няма API ключ: отвори <b>Терминал</b> и напиши <code>genesis setup</code>.</p>
      )}
      <div className="row">
        <button className="btn primary" onClick={onRetry}><RotateCw size={15} /> Пусни пак</button>
        <button className="btn" onClick={onLogs}><ScrollText size={15} /> Дневник</button>
        <button className="btn" onClick={onOpen}><FolderOpen size={15} /> Друга папка</button>
      </div>
    </div>
  );
}

export function EmptyChat({ onPick, workspace }: { onPick: (t: string) => void; workspace: string }) {
  const ideas = [
    'Разгледай проекта и ми кажи как е устроен',
    'Намери и поправи бъговете в тестовете',
    'Направи README за този проект',
    'Какво можеш да правиш?',
  ];
  return (
    <div className="empty-chat">
      <Logo size={52} />
      <h2>С какво да помогна в <span className="grad">{baseName(workspace)}</span>?</h2>
      <div className="ideas">
        {ideas.map((t) => <button key={t} className="idea" onClick={() => onPick(t)}>{t}</button>)}
      </div>
    </div>
  );
}
