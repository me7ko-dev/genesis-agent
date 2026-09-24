import { FolderOpen, MessageSquarePlus, ScrollText, SquareTerminal, X, Folder } from 'lucide-react';
import type { BackendState, Settings } from '../../shared/types';
import { baseName } from '../lib/chat';

type Props = {
  state: BackendState;
  settings: Settings;
  onOpen: (path?: string) => void;
  onForget: (path: string) => void;
  onNewChat: () => void;
  onTerminal: () => void;
  onLogs: () => void;
};

export function Sidebar({ state, settings, onOpen, onForget, onNewChat, onTerminal, onLogs }: Props) {
  const current = state.workspace.toLowerCase();
  return (
    <aside className="sidebar">
      <div className="side-actions">
        <button className="side-btn primary" onClick={onNewChat} disabled={state.phase !== 'ready'}>
          <MessageSquarePlus size={16} /> Нов разговор <kbd>Ctrl+N</kbd>
        </button>
        <button className="side-btn" onClick={() => onOpen()}>
          <FolderOpen size={16} /> Отвори папка <kbd>Ctrl+O</kbd>
        </button>
      </div>

      <div className="side-section">Проекти</div>
      <nav className="projects">
        {settings.recent.length === 0 && <div className="side-empty">Още няма проекти</div>}
        {settings.recent.map((p) => (
          <div key={p} className={`project ${p.toLowerCase() === current ? 'active' : ''}`}>
            <button className="project-open" title={p} onClick={() => onOpen(p)}>
              <Folder size={15} />
              <span className="project-text">
                <span className="project-name">{baseName(p)}</span>
                <span className="project-path">{p}</span>
              </span>
            </button>
            <button className="project-forget" title="Махни от списъка" onClick={() => onForget(p)}>
              <X size={13} />
            </button>
          </div>
        ))}
      </nav>

      <div className="side-foot">
        <button className="side-link" onClick={onTerminal}><SquareTerminal size={15} /> Терминал</button>
        <button className="side-link" onClick={onLogs}><ScrollText size={15} /> Дневник</button>
        <div className="side-version">{state.version ? `genesis-agent ${state.version}` : ''}</div>
      </div>
    </aside>
  );
}
