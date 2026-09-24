import { ArrowDown, Cpu, Folder, PanelLeftClose, PanelLeftOpen, RotateCw, X, Zap } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { BackendState, CommandResult, GenesisEvent, Settings, UsageReport } from '../shared/types';
import { Composer } from './components/Composer';
import { AssistantMessage, ConfirmCard, Note, Thinking, ToolCard, UserMessage } from './components/Messages';
import { explain, fmtTokens, Sheet, toggleMessage, type SheetView } from './components/Panels';
import { EmptyChat, Failed, Logo, Missing, Starting, Welcome } from './components/Screens';
import { Sidebar } from './components/Sidebar';
import { mergeEvents, pendingConfirm, shortPath, toChatItems, turnInfo } from './lib/chat';

const api = window.genesis;

export function App() {
  const [state, setState] = useState<BackendState>({ phase: 'idle', workspace: '', busy: false });
  const [settings, setSettings] = useState<Settings>({ recent: [], sidebar: true });
  const [events, setEvents] = useState<GenesisEvent[]>([]);
  const [log, setLog] = useState('');
  const [logsOpen, setLogsOpen] = useState(false);
  const [sheet, setSheet] = useState<SheetView | null>(null);
  const [today, setToday] = useState<{ tokens: number; free: boolean } | null>(null);
  const [toast, setToast] = useState('');
  const [focusKey, setFocusKey] = useState(0);
  const [atBottom, setAtBottom] = useState(true);
  const scroller = useRef<HTMLDivElement>(null);

  // ── wiring to the main process ──────────────────────────────────────────
  useEffect(() => {
    void api.state().then(setState);
    void api.settings().then(setSettings);
    void api.events().then((e) => setEvents(e));
    void api.logs().then(setLog);
    const offs = [
      api.onState((s) => { setState(s); void api.settings().then(setSettings); }),
      api.onEvents((b) => setEvents((cur) => mergeEvents(cur, b.events, b.reset))),
      api.onLog((line) => setLog((cur) => {
        const next = cur ? `${cur}\n${line}` : line;
        return next.length > 200_000 ? next.slice(-150_000) : next;
      })),
    ];
    return () => offs.forEach((off) => off());
  }, []);

  const flash = useCallback((text: string) => {
    setToast(text);
    setTimeout(() => setToast((t) => (t === text ? '' : t)), 4000);
  }, []);

  const items = useMemo(() => toChatItems(events), [events]);
  const confirm = pendingConfirm(items);
  const turn = useMemo(() => turnInfo(events), [events]);
  const ready = state.phase === 'ready';

  // ── actions ─────────────────────────────────────────────────────────────
  const open = useCallback(async (path?: string) => {
    setEvents([]);
    await api.openWorkspace(path);
    setSettings(await api.settings());
  }, []);

  const newChat = useCallback(async () => {
    if (state.busy) { flash('Изчакай да свърши или натисни Esc, за да го спреш.'); return; }
    const r = await api.clear();
    if (!r.ok) flash('Не можах да започна нов разговор.');
    setFocusKey((k) => k + 1);
  }, [state.busy, flash]);

  const stop = useCallback(() => { void api.stop(); }, []);

  const answer = useCallback((id: string, allow: boolean) => {
    void api.confirm(id, allow).then(() => setFocusKey((k) => k + 1));
  }, []);

  const toggleSidebar = useCallback(() => {
    setSettings((s) => { void api.setSidebar(!s.sidebar); return { ...s, sidebar: !s.sidebar }; });
  }, []);

  const send = useCallback(async (text: string) => {
    const r = await api.send(text);
    if (!r.ok) flash(r.error === 'busy' ? 'Genesis още работи.' : `Не се изпрати: ${r.error}`);
    return r.ok;
  }, [flash]);

  // A command the agent answers and the app shows as a message, not a window.
  const quick = useCallback(async (name: string, arg: Record<string, unknown> = {}) => {
    const r = await api.command<CommandResult & { on?: boolean; model?: string; text?: string; dest?: string }>(name, arg);
    if (!r.ok) { flash(explain(r.error)); return; }
    if (name === 'backup') flash(`Архивът е готов в ${r.dest}.`);
    else if (r.text) flash(r.text);
    else flash(toggleMessage(name, r));
  }, [flash]);

  const command = useCallback((name: string, arg: string) => {
    // Everything but the window's own commands needs a genesis.exe that knows them.
    const local = ['/clear', '/stop', '/folder', '/terminal', '/logs', '/help'];
    if (!local.includes(name) && ready && !state.commands) {
      flash(explain('old_agent'));
      return true;
    }
    switch (name) {
      case '/clear': void newChat(); return true;
      case '/stop': stop(); return true;
      case '/folder': void open(); return true;
      case '/terminal': void api.openTerminal(); return true;
      case '/logs': setLogsOpen(true); return true;
      case '/help': setSheet({ view: 'help' }); return true;
      case '/usage': setSheet({ view: 'usage' }); return true;
      case '/status': setSheet({ view: 'status' }); return true;
      case '/model': setSheet({ view: 'models', tab: 'pick' }); return true;
      case '/models': setSheet({ view: 'models', tab: 'chain' }); return true;
      case '/history': setSheet({ view: 'history' }); return true;
      case '/update': setSheet({ view: 'update' }); return true;
      case '/skills': setSheet({ view: 'text', title: 'Умения', command: 'skills' }); return true;
      case '/tasks': setSheet({ view: 'text', title: 'Работа', command: 'tasks' }); return true;
      case '/maxcoding': void quick('maxcoding'); return true;
      case '/local_model_max': void quick('local_max'); return true;
      case '/local_model_normal': void quick('local_normal'); return true;
      case '/backup': void quick('backup'); return true;
      case '/done': void quick('done', { ids: arg }); return true;
      case '/drop': void quick('drop', { ids: arg }); return true;
      default: return false;
    }
  }, [newChat, stop, open, quick, flash, ready, state.commands]);

  // GENESIS_DESKTOP_SHEET (main.ts) → #sheet=usage: open that command once ready.
  useEffect(() => {
    const m = /sheet=([\w/]+)/.exec(window.location.hash);
    if (!m || !ready || !state.commands) return;
    window.location.hash = '';
    command(`/${m[1].replace(/^\//, '')}`, '');
  }, [ready, state.commands, command]);

  // Today's tokens in the title bar, fresh after every turn.
  useEffect(() => {
    if (!ready || state.busy || !state.commands) return;
    void api.command<UsageReport>('usage', { days: 7 }).then((r) => { if (r.ok) setToday({ tokens: r.periods.today.tokens, free: r.paid_tokens === 0 }); });
  }, [ready, state.busy, state.commands, sheet]);

  // ── keyboard ────────────────────────────────────────────────────────────
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (confirm && !e.ctrlKey && !e.altKey) {
        if (e.key === 'Enter') { e.preventDefault(); answer(confirm.id, true); return; }
        if (e.key === 'Escape') { e.preventDefault(); answer(confirm.id, false); return; }
      }
      if (e.key === 'Escape') {
        if (sheet) setSheet(null);
        else if (logsOpen) setLogsOpen(false);
        else if (state.busy) stop();
        return;
      }
      if (!e.ctrlKey) return;
      const k = e.key.toLowerCase();
      if (k === 'n') { e.preventDefault(); void newChat(); }
      else if (k === 'o') { e.preventDefault(); void open(); }
      else if (k === 'b') { e.preventDefault(); toggleSidebar(); }
      else if (k === 'l' && e.shiftKey) { e.preventDefault(); setLogsOpen((v) => !v); }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [confirm, answer, state.busy, stop, newChat, open, toggleSidebar, logsOpen, sheet]);

  // ── follow the conversation unless the user scrolled up ────────────────
  useEffect(() => {
    const el = scroller.current;
    if (el && atBottom) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [items.length, state.busy, turn.label, atBottom]);

  function onScroll() {
    const el = scroller.current;
    if (el) setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  }

  // ── layout ──────────────────────────────────────────────────────────────
  let main: React.ReactNode;
  if (state.phase === 'missing') main = <Missing onRetry={() => void api.restart().then(setState)} />;
  else if (state.phase === 'idle') main = <Welcome onOpen={open} recent={settings.recent} />;
  else if (state.phase === 'starting') main = <Starting workspace={state.workspace} log={log} />;
  else if (state.phase === 'error') {
    main = <Failed state={state} onRetry={() => void api.restart()} onLogs={() => setLogsOpen(true)} onOpen={() => void open()} />;
  } else {
    main = (
      <>
        <div className="chat" ref={scroller} onScroll={onScroll}>
          <div className="chat-inner">
            {items.length === 0 && !state.busy && (
              <EmptyChat workspace={state.agentWorkspace || state.workspace} onPick={(t) => void send(t)} />
            )}
            {items.map((it) => {
              switch (it.kind) {
                case 'user': return <UserMessage key={it.key} text={it.text} />;
                case 'assistant': return <AssistantMessage key={it.key} text={it.text} />;
                case 'tool': return <ToolCard key={it.key} item={it} />;
                case 'note': return <Note key={it.key} item={it} />;
                case 'confirm': return <ConfirmCard key={it.key} item={it} onAnswer={answer} />;
              }
            })}
            {state.busy && !confirm && <Thinking label={turn.label} since={turn.since} />}
          </div>
        </div>
        {!atBottom && (
          <button className="to-bottom" onClick={() => { setAtBottom(true); }} title="Към края">
            <ArrowDown size={16} />
          </button>
        )}
        <Composer
          busy={state.busy}
          disabled={!ready || !!confirm}
          placeholder={confirm ? 'Genesis чака отговор — Enter разрешава, Esc отказва'
            : state.busy ? 'Genesis работи… (Esc спира)' : 'Напиши задача за Genesis…'}
          onSend={send}
          onStop={stop}
          onCommand={command}
          focusKey={focusKey}
        />
      </>
    );
  }

  const differs = ready && state.agentWorkspace && state.agentWorkspace.toLowerCase() !== state.workspace.toLowerCase();

  return (
    <div className={`app ${settings.sidebar ? '' : 'no-sidebar'}`}>
      <header className="titlebar">
        <button className="icon-btn" onClick={toggleSidebar} title="Странична лента (Ctrl+B)">
          {settings.sidebar ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}
        </button>
        <div className="brand"><Logo size={20} /> Genesis</div>
        {state.workspace && (
          <button className="chip" title={state.agentWorkspace || state.workspace}
                  onClick={() => void api.reveal(state.agentWorkspace || state.workspace)}>
            <Folder size={13} /> {shortPath(state.agentWorkspace || state.workspace)}
          </button>
        )}
        {state.model && (
          <button className="chip model" title="Смени модела (/model)"
                  onClick={() => (state.commands ? setSheet({ view: 'models', tab: 'pick' }) : flash(explain('old_agent')))}>
            <Cpu size={13} /> {state.model}
          </button>
        )}
        {ready && state.commands && today !== null && (
          <button className="chip usage" title="Разход (/usage)" onClick={() => setSheet({ view: 'usage' })}>
            <Zap size={13} /> {fmtTokens(today.tokens)} днес{today.free && ' · $0'}
          </button>
        )}
        <span className={`status-dot ${state.phase} ${state.busy ? 'busy' : ''}`}
              title={state.busy ? 'Работи' : state.phase === 'ready' ? 'Свързан' : state.phase} />
        <div className="drag-fill" />
      </header>

      {settings.sidebar && (
        <Sidebar
          state={state}
          settings={settings}
          onOpen={(p) => void open(p)}
          onForget={(p) => void api.forgetWorkspace(p).then(setSettings)}
          onNewChat={() => void newChat()}
          onTerminal={() => void api.openTerminal()}
          onLogs={() => setLogsOpen(true)}
        />
      )}

      <main className="main">
        {differs && (
          <div className="banner">
            Genesis не работи направо в <b>{state.workspace}</b> (домашната папка и корена на диска са забранени) —
            работи в <b>{state.agentWorkspace}</b>.
          </div>
        )}
        {main}
      </main>

      {logsOpen && (
        <div className="drawer-back" onClick={() => setLogsOpen(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <b>Дневник на агента</b>
              <div className="row">
                <button className="btn small" onClick={() => void api.restart()}><RotateCw size={13} /> Рестарт</button>
                <button className="icon-btn" onClick={() => setLogsOpen(false)}><X size={16} /></button>
              </div>
            </div>
            <pre className="drawer-log">{log || '(празно)'}</pre>
          </div>
        </div>
      )}

      {sheet && (
        <Sheet sheet={sheet} onClose={() => { setSheet(null); setFocusKey((k) => k + 1); }} onFlash={flash}
               onOpen={setSheet} onTerminal={() => void api.openTerminal()} />
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
