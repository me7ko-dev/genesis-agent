import {
  AlertTriangle, ArrowLeft, Check, CircleDollarSign, Copy, Cpu, History, Info, ListTodo, Loader2,
  RefreshCw, Search, Sparkles, SquareTerminal, X, Zap,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import type {
  CommandResult, HistoryReport, ModelsReport, ProviderModels, StatusReport, UpdateReport, UsageReport,
} from '../../shared/types';
import { fmtTokens, niceMax } from '../lib/format';
import { COMMANDS } from './Composer';

export { fmtTokens };

const api = window.genesis;

export type SheetView =
  | { view: 'usage' }
  | { view: 'status' }
  | { view: 'models'; tab: 'pick' | 'chain' }
  | { view: 'history' }
  | { view: 'update' }
  | { view: 'help' }
  | { view: 'text'; title: string; command: string; arg?: Record<string, unknown> };

// ── numbers ──────────────────────────────────────────────────────────────────

const full = (n: number) => n.toLocaleString('bg-BG');

function fmtDay(iso: string, long = false): string {
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString('bg-BG', long ? { weekday: 'short', day: 'numeric', month: 'long' } : { day: 'numeric', month: 'short' });
}

// ── plumbing ─────────────────────────────────────────────────────────────────

function useCommand<T extends CommandResult>(name: string, arg?: Record<string, unknown>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const key = JSON.stringify(arg ?? {});
  const load = useCallback(async () => {
    setError('');
    const r = await api.command<T>(name, JSON.parse(key) as Record<string, unknown>);
    if (r.ok) setData(r);
    else setError(explain(r.error));
  }, [name, key]);
  useEffect(() => { void load(); }, [load]);
  return { data, error, reload: load };
}

export function explain(error?: string): string {
  if (error === 'old_agent') {
    return 'Инсталираният genesis.exe е по-стара версия и не знае тази команда. Обнови Genesis или я пусни в терминала.';
  }
  if (error === 'busy') return 'Genesis работи по задача. Изчакай да свърши или я спри с Esc.';
  return error || 'Нещо се обърка.';
}

function Loading() {
  return <div className="sheet-loading"><Loader2 size={18} className="spin" /> Зареждам…</div>;
}

function Failure({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div className="sheet-error">
      <AlertTriangle size={16} /> <span>{text}</span>
      {onRetry && <button className="btn small" onClick={onRetry}><RefreshCw size={13} /> Пак</button>}
    </div>
  );
}

function Meter({ value, max, tone = 'accent' }: { value: number; max: number; tone?: 'accent' | 'warn' | 'error' }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="meter" role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
      <div className={`meter-fill ${tone}`} style={{ width: `${Math.max(pct, value > 0 ? 1.5 : 0)}%` }} />
    </div>
  );
}

// ── the sheet ────────────────────────────────────────────────────────────────

const TITLES: Record<SheetView['view'], { title: string; icon: ReactNode }> = {
  usage: { title: 'Разход', icon: <CircleDollarSign size={17} /> },
  status: { title: 'Състояние', icon: <Info size={17} /> },
  models: { title: 'Модели', icon: <Cpu size={17} /> },
  history: { title: 'История', icon: <History size={17} /> },
  update: { title: 'Обновяване', icon: <RefreshCw size={17} /> },
  help: { title: 'Команди', icon: <Sparkles size={17} /> },
  text: { title: '', icon: <ListTodo size={17} /> },
};

type SheetProps = {
  sheet: SheetView;
  onClose: () => void;
  onFlash: (text: string) => void;
  onOpen: (s: SheetView) => void;
  onTerminal: () => void;
};

export function Sheet({ sheet, onClose, onFlash, onOpen, onTerminal }: SheetProps) {
  const head = TITLES[sheet.view];
  const title = sheet.view === 'text' ? sheet.title : head.title;
  return (
    <div className="sheet-back" onMouseDown={onClose}>
      <div className={`sheet ${sheet.view}`} onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className="sheet-head">
          <div className="sheet-title">{head.icon} {title}</div>
          <button className="icon-btn" onClick={onClose} title="Затвори (Esc)"><X size={16} /></button>
        </div>
        <div className="sheet-body">
          {sheet.view === 'usage' && <UsageView onOpen={onOpen} />}
          {sheet.view === 'status' && <StatusView onFlash={onFlash} onOpen={onOpen} />}
          {sheet.view === 'models' && <ModelsView tab={sheet.tab} onFlash={onFlash} onClose={onClose} />}
          {sheet.view === 'history' && <HistoryView onFlash={onFlash} onClose={onClose} />}
          {sheet.view === 'update' && <UpdateView onTerminal={onTerminal} onFlash={onFlash} />}
          {sheet.view === 'help' && <HelpView />}
          {sheet.view === 'text' && <TextView command={sheet.command} arg={sheet.arg} />}
        </div>
      </div>
    </div>
  );
}

// ── /usage ───────────────────────────────────────────────────────────────────

const RANGES = [7, 30, 90] as const;

function UsageView({ onOpen }: { onOpen: (s: SheetView) => void }) {
  const [days, setDays] = useState<number>(30);
  const { data, error, reload } = useCommand<UsageReport>('usage', { days });
  const status = useCommand<StatusReport>('status');

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const p = data.periods;
  // `month` is the chosen range (7, 30 or 90 days), whatever its length.
  const range = p.month;
  const free = data.paid_tokens === 0;

  return (
    <div className="usage">
      <div className="range-row">
        <div className="seg">
          {RANGES.map((d) => (
            <button key={d} className={d === days ? 'on' : ''} onClick={() => setDays(d)}>{d} дни</button>
          ))}
        </div>
        <button className="icon-btn subtle" title="Опресни" onClick={() => { void reload(); void status.reload(); }}>
          <RefreshCw size={14} />
        </button>
      </div>

      <div className="money">
        <div>
          <div className="money-label">Похарчени пари</div>
          <div className="money-value">{free ? <>$0<span>.00</span></> : <span>виж сметката</span>}</div>
          <div className="money-note">
            {data.paid_tokens === 0
              ? <><Check size={13} /> Всичките {full(p.all.calls)} обръщения са към безплатни модели.</>
              : <><AlertTriangle size={13} /> {fmtTokens(data.paid_tokens)} токена са към платени модели. Цената им е в сметката при доставчика.</>}
          </div>
        </div>
        <div className="money-side">
          <Zap size={15} />
          <div><b>{fmtTokens(p.all.tokens)}</b> токена общо</div>
          {data.since && <div className="muted">от {fmtDay(data.since, true)}</div>}
        </div>
      </div>

      <div className="tiles">
        <Tile label="Днес" period={p.today} free={free} />
        <Tile label="7 дни" period={p.week} free={free} />
        {data.days > 7 && <Tile label={`${data.days} дни`} period={range} free={free} />}
        <Tile label="Общо" period={p.all} free={free} />
      </div>

      {(data.quotas.length > 0 || status.data) && (
        <section>
          <h3>Колко остава</h3>
          {data.quotas.map((q) => {
            const low = q.left / q.limit < 0.2;
            return (
              <div className="quota" key={q.provider}>
                <div className="quota-row">
                  <span><b>{q.name}</b> <span className="muted">безплатна квота {q.label}</span></span>
                  <span className={low ? 'warn-text' : ''}>
                    {low && <AlertTriangle size={13} />} остават <b>{fmtTokens(q.left)}</b>
                  </span>
                </div>
                <Meter value={q.spent} max={q.limit} tone={low ? 'warn' : 'accent'} />
                <div className="quota-foot muted">
                  Изхарчени {fmtTokens(q.spent)} от {fmtTokens(q.limit)} за последните {q.period === 'ден' ? '24 часа (днес)' : '7 дни'}
                </div>
              </div>
            );
          })}
          {status.data && (
            <div className="quota">
              <div className="quota-row">
                <span><b>Контекст на разговора</b> <span className="muted">{status.data.model}</span></span>
                <span>свободни <b>{fmtTokens(Math.max(0, status.data.context_window - status.data.context_used))}</b></span>
              </div>
              <Meter value={status.data.context_used} max={status.data.context_window}
                     tone={status.data.context_used / status.data.context_window > 0.8 ? 'warn' : 'accent'} />
              <div className="quota-foot muted">
                ~{fmtTokens(status.data.context_used)} от {fmtTokens(status.data.context_window)} токена са заети от този разговор
              </div>
            </div>
          )}
          <p className="fine">
            Доставчиците не казват колко остава от квотата. Числата са сметнати от дневника на Genesis спрямо
            обявените безплатни лимити.
          </p>
        </section>
      )}

      <section>
        <h3>Токени по дни <span className="muted">· последните {data.days} дни</span></h3>
        <DailyChart daily={data.daily} />
        <div className="split muted">
          За {data.days} дни: {fmtTokens(range.prompt)} изпратени, {fmtTokens(range.completion)} отговори
          {range.cached > 0 && <>, {fmtTokens(range.cached)} от кеша</>} · {full(range.calls)} обръщения
        </div>
      </section>

      <section>
        <h3>По доставчик <span className="muted">· от началото</span></h3>
        <ProviderBars rows={data.providers} total={p.all.tokens} />
      </section>

      <section>
        <h3>Най-ползвани модели</h3>
        <table className="data">
          <thead><tr><th>Модел</th><th>Доставчик</th><th className="num">Обръщения</th><th className="num">Токени</th></tr></thead>
          <tbody>
            {data.models.map((m) => (
              <tr key={`${m.provider}/${m.model}`}>
                <td className="mono">{m.model} {m.free ? <span className="badge free">FREE</span> : <span className="badge paid">PAID</span>}</td>
                <td>{data.providers.find((x) => x.provider === m.provider)?.name ?? m.provider}</td>
                <td className="num">{full(m.calls)}</td>
                <td className="num">{fmtTokens(m.tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="sheet-actions">
        <button className="btn small" onClick={() => onOpen({ view: 'models', tab: 'pick' })}><Cpu size={13} /> Смени модела</button>
        <button className="btn small" onClick={() => onOpen({ view: 'status' })}><Info size={13} /> Състояние</button>
      </div>
    </div>
  );
}

function Tile({ label, period, free }: { label: string; period: { calls: number; tokens: number }; free: boolean }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{fmtTokens(period.tokens)}</div>
      <div className="tile-sub">{full(period.calls)} обръщения{free && ' · $0'}</div>
    </div>
  );
}

function DailyChart({ daily }: { daily: UsageReport['daily'] }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(1, ...daily.map((d) => d.tokens));
  const nice = niceMax(max);
  const h = hover === null ? null : daily[hover];
  const mid = Math.floor(daily.length / 2);
  return (
    <div className="chart">
      <div className="chart-y">
        <span>{fmtTokens(nice)}</span><span>{fmtTokens(nice / 2)}</span><span>0</span>
      </div>
      <div className="chart-plot" onMouseLeave={() => setHover(null)}>
        <div className="grid-line" style={{ bottom: '100%' }} />
        <div className="grid-line" style={{ bottom: '50%' }} />
        <div className="bars">
          {daily.map((d, i) => (
            <div key={d.day} className={`bar-slot ${hover === i ? 'hover' : ''}`} onMouseEnter={() => setHover(i)}>
              <div className={`bar ${d.tokens === 0 ? 'zero' : ''}`} style={{ height: `${(d.tokens / nice) * 100}%` }} />
            </div>
          ))}
        </div>
        {h && hover !== null && (
          <div className="chart-tip" style={{ left: `${((hover + 0.5) / daily.length) * 100}%` }}>
            <div className="tip-day">{fmtDay(h.day, true)}</div>
            <div><b>{full(h.tokens)}</b> токена</div>
            <div className="muted">{full(h.calls)} обръщения</div>
          </div>
        )}
        <div className="chart-x">
          <span>{fmtDay(daily[0].day)}</span>
          <span>{fmtDay(daily[mid].day)}</span>
          <span>днес</span>
        </div>
      </div>
    </div>
  );
}

function ProviderBars({ rows, total }: { rows: UsageReport['providers']; total: number }) {
  const top = Math.max(1, ...rows.map((r) => r.tokens));
  return (
    <div className="hbars">
      {rows.map((r) => (
        <div className="hbar" key={r.provider} title={`${r.name}: ${full(r.tokens)} токена, ${full(r.calls)} обръщения`}>
          <div className="hbar-name">{r.name} {r.free ? <span className="badge free">FREE</span> : <span className="badge paid">PAID</span>}</div>
          <div className="hbar-track"><div className="hbar-fill" style={{ width: `${(r.tokens / top) * 100}%` }} /></div>
          <div className="hbar-val"><b>{fmtTokens(r.tokens)}</b> <span className="muted">{total ? Math.round((r.tokens / total) * 100) : 0}%</span></div>
        </div>
      ))}
    </div>
  );
}

// ── /status ──────────────────────────────────────────────────────────────────

function StatusView({ onFlash, onOpen }: { onFlash: (t: string) => void; onOpen: (s: SheetView) => void }) {
  const { data, error, reload } = useCommand<StatusReport>('status');
  const [busy, setBusy] = useState('');

  async function toggle(name: string) {
    setBusy(name);
    const r = await api.command<CommandResult & { on?: boolean; model?: string }>(name);
    setBusy('');
    if (!r.ok) { onFlash(explain(r.error)); return; }
    onFlash(toggleMessage(name, r));
    void reload();
  }

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const ctxPct = Math.round((data.context_used / data.context_window) * 100);
  return (
    <div className="status">
      <div className="kv">
        <span>Модел</span><b className="mono">{data.model} {data.free ? <span className="badge free">FREE</span> : <span className="badge paid">PAID</span>}</b>
        <span>Доставчик</span><b>{data.local_only ? `Само локален: ${data.local_only}` : data.provider_name}</b>
        <span>Работна папка</span><b className="mono small">{data.workspace}</b>
        <span>Сесия</span><b>{data.elapsed} · {data.messages} съобщения</b>
        <span>Токени в сесията</span><b>{fmtTokens(data.session_input_tokens)} изпратени · {fmtTokens(data.session_output_tokens)} отговори</b>
        <span>Версия</span><b>genesis-agent {data.version}</b>
      </div>
      <div className="quota">
        <div className="quota-row"><b>Контекст</b><span>{ctxPct}% · свободни <b>{fmtTokens(data.context_window - data.context_used)}</b></span></div>
        <Meter value={data.context_used} max={data.context_window} tone={ctxPct > 80 ? 'warn' : 'accent'} />
      </div>
      <h3>Режими</h3>
      <div className="modes">
        <Mode on={data.coding_mode} busy={busy === 'maxcoding'} onClick={() => void toggle('maxcoding')}
              title="Макс кодинг" text="Най-силните безплатни модели за код. По-бавно и харчи повече квота." />
        <Mode on={data.local_only === 'qwen3:14b'} busy={busy === 'local_max'} onClick={() => void toggle('local_max')}
              title="Локален MAX" text="Само qwen3:14b на този компютър, без облак. Мощен, но бавен." />
        <Mode on={data.local_only === 'qwen2.5-coder:7b'} busy={busy === 'local_normal'} onClick={() => void toggle('local_normal')}
              title="Локален бърз" text="Само qwen2.5-coder:7b на този компютър. Лек и бърз." />
      </div>
      <div className="sheet-actions">
        <button className="btn small" onClick={() => onOpen({ view: 'models', tab: 'pick' })}><Cpu size={13} /> Смени модела</button>
        <button className="btn small" onClick={() => onOpen({ view: 'usage' })}><CircleDollarSign size={13} /> Разход</button>
      </div>
    </div>
  );
}

export function toggleMessage(name: string, r: { on?: boolean; model?: string | null }): string {
  if (name === 'maxcoding') return r.on ? 'Кодинг режимът е включен: най-силните безплатни модели за код.' : 'Кодинг режимът е изключен.';
  return r.on ? `Локален режим: само ${r.model}, облакът не се ползва.` : 'Локалният режим е изключен, обратно към облака.';
}

function Mode({ on, busy, title, text, onClick }: { on: boolean; busy: boolean; title: string; text: string; onClick: () => void }) {
  return (
    <button className={`mode ${on ? 'on' : ''}`} onClick={onClick} disabled={busy}>
      <span className="switch" aria-hidden><span /></span>
      <span><b>{title}</b><span className="muted">{text}</span></span>
    </button>
  );
}

// ── /model, /models ──────────────────────────────────────────────────────────

function ModelsView({ tab: initial, onFlash, onClose }: { tab: 'pick' | 'chain'; onFlash: (t: string) => void; onClose: () => void }) {
  const [tab, setTab] = useState(initial);
  const { data, error, reload } = useCommand<ModelsReport>('models');
  const [provider, setProvider] = useState<string | null>(null);

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <div className="models">
      <div className="current">
        <span className="muted">Сега:</span> <b className="mono">{data.current.provider}/{data.current.model}</b>
      </div>
      <div className="range-row">
        <div className="seg">
          <button className={tab === 'pick' ? 'on' : ''} onClick={() => setTab('pick')}>Избери модел</button>
          <button className={tab === 'chain' ? 'on' : ''} onClick={() => setTab('chain')}>Резервна верига ({data.chain.length})</button>
        </div>
      </div>
      {tab === 'chain' && (
        <>
          <p className="fine">Когато един модел откаже (квота, лимит), Genesis минава на следващия в този ред.</p>
          <ol className="chain">
            {data.chain.map((c) => (
              <li key={`${c.provider}/${c.model}`} className={c.active ? 'active' : ''}>
                <span className="muted">{c.name}</span> <span className="mono">{c.model}</span>
                {c.free ? <span className="badge free">FREE</span> : <span className="badge paid">PAID</span>}
                {c.active && <span className="badge on">сега</span>}
              </li>
            ))}
          </ol>
        </>
      )}
      {tab === 'pick' && !provider && (
        <div className="providers">
          {data.providers.map((p) => (
            <button key={p.provider} className={`prov ${p.active ? 'active' : ''}`} disabled={!p.ready}
                    title={p.ready ? '' : p.hint} onClick={() => setProvider(p.provider)}>
              <b>{p.name}</b>
              <span className={p.ready ? 'ok-text' : 'muted'}>{p.ready ? (p.active ? 'ползва се' : 'готов') : p.hint}</span>
            </button>
          ))}
        </div>
      )}
      {tab === 'pick' && provider && (
        <ProviderPicker provider={provider} name={data.providers.find((p) => p.provider === provider)?.name ?? provider}
                        onBack={() => setProvider(null)}
                        onPicked={(m) => { onFlash(`Моделът е сменен: ${m}`); onClose(); }}
                        onFlash={onFlash} />
      )}
    </div>
  );
}

function ProviderPicker({ provider, name, onBack, onPicked, onFlash }: {
  provider: string; name: string; onBack: () => void; onPicked: (m: string) => void; onFlash: (t: string) => void;
}) {
  const { data, error, reload } = useCommand<ProviderModels>('provider_models', { provider });
  const [q, setQ] = useState('');
  const [freeOnly, setFreeOnly] = useState(false);
  const list = useMemo(() => (data?.models ?? []).filter((m) =>
    m.model.toLowerCase().includes(q.toLowerCase()) && (!freeOnly || m.free)), [data, q, freeOnly]);

  async function pick(model: string) {
    const r = await api.command<CommandResult & { note?: string }>('set_model', { provider, model });
    if (!r.ok) { onFlash(explain(r.error)); return; }
    onPicked(r.note ? `${model}. ${r.note}` : model);
  }

  return (
    <div className="picker">
      <div className="picker-head">
        <button className="btn small" onClick={onBack}><ArrowLeft size={13} /> Доставчици</button>
        <b>{name}</b>
      </div>
      {error && <Failure text={error} onRetry={reload} />}
      {!error && !data && <Loading />}
      {data && (
        <>
          <div className="search">
            <Search size={14} />
            <input autoFocus placeholder={`Търси сред ${data.models.length} модела…`} value={q} onChange={(e) => setQ(e.target.value)} />
            <label><input type="checkbox" checked={freeOnly} onChange={(e) => setFreeOnly(e.target.checked)} /> само FREE</label>
          </div>
          <div className="model-list">
            {list.map((m) => (
              <button key={m.model} className={m.active ? 'active' : ''} onClick={() => void pick(m.model)}>
                <span className="mono">{m.model}</span>
                {m.free ? <span className="badge free">FREE</span> : <span className="badge paid">PAID</span>}
                {m.active && <span className="badge on">сега</span>}
              </button>
            ))}
            {list.length === 0 && <div className="muted pad">Нищо не съвпада.</div>}
          </div>
        </>
      )}
    </div>
  );
}

// ── /history ─────────────────────────────────────────────────────────────────

function HistoryView({ onFlash, onClose }: { onFlash: (t: string) => void; onClose: () => void }) {
  const { data, error, reload } = useCommand<HistoryReport>('history');
  const [q, setQ] = useState('');

  async function load(file: string) {
    const r = await api.command<CommandResult & { messages?: number }>('load_history', { file });
    if (!r.ok) { onFlash(explain(r.error)); return; }
    onFlash(`Разговорът е зареден (${r.messages} съобщения). Можеш да продължиш оттам.`);
    onClose();
  }

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const list = data.sessions.filter((s) => s.preview.toLowerCase().includes(q.toLowerCase()));
  return (
    <div className="history">
      <div className="search">
        <Search size={14} />
        <input autoFocus placeholder="Търси в разговорите…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {list.length === 0 && <div className="muted pad">Няма запазени разговори.</div>}
      <div className="sessions">
        {list.map((s) => (
          <button key={s.file} className="session" onClick={() => void load(s.file)}>
            <span className="session-preview">{s.preview || '(без текст)'}</span>
            <span className="muted">
              {new Date(s.modified).toLocaleString('bg-BG', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}
              {' · '}{s.messages} съобщения
            </span>
          </button>
        ))}
      </div>
      <p className="fine">Зареденият разговор заменя текущия. Новите съобщения се пазят в отделен файл.</p>
    </div>
  );
}

// ── /update ──────────────────────────────────────────────────────────────────

function UpdateView({ onTerminal, onFlash }: { onTerminal: () => void; onFlash: (t: string) => void }) {
  const { data, error, reload } = useCommand<UpdateReport>('update');
  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <div className="update">
      <div className="kv">
        <span>Версия</span><b>genesis-agent {data.version}</b>
        <span>Сглобено от</span><b className="mono">{data.current ?? 'не е от git'}{data.ref ? ` (${data.ref})` : ''}</b>
      </div>
      {data.current === null && <p>Това копие не е инсталирано от GitHub, затова няма с какво да се сравни.</p>}
      {data.current !== null && data.up_to_date === null && <Failure text="Не можах да питам GitHub (мрежа или лимит)." onRetry={reload} />}
      {data.up_to_date === true && <p className="ok-text"><Check size={14} /> Това е най-новата версия.</p>}
      {data.up_to_date === false && (
        <>
          <p><b>Има по-нова версия:</b> <span className="mono">{data.latest}</span></p>
          {data.changes.length > 0 && <ul className="changes">{data.changes.map((c) => <li key={c}>{c}</li>)}</ul>}
        </>
      )}
      {data.command && data.up_to_date !== true && (
        <>
          <p className="fine">Обновяването сменя genesis.exe, затова се пуска извън приложението. Затвори Genesis и изпълни в PowerShell:</p>
          <div className="cmd-line">
            <code>{data.command}</code>
            <button className="icon-btn subtle" title="Копирай" onClick={() => { void navigator.clipboard.writeText(data.command ?? ''); onFlash('Командата е копирана.'); }}>
              <Copy size={14} />
            </button>
          </div>
          <button className="btn small" onClick={onTerminal}><SquareTerminal size={13} /> Отвори терминал</button>
        </>
      )}
    </div>
  );
}

// ── /help ────────────────────────────────────────────────────────────────────

const KEYS: [string, string][] = [
  ['Ctrl+N', 'Нов разговор'], ['Ctrl+O', 'Отвори папка'], ['Ctrl+B', 'Странична лента'],
  ['Ctrl+Shift+L', 'Дневник'], ['Esc', 'Спри задачата / затвори'], ['↑ ↓', 'Предишни съобщения'],
  ['Enter', 'Изпрати'], ['Shift+Enter', 'Нов ред'],
];

function HelpView() {
  const groups = [...new Set(COMMANDS.map((c) => c.group))];
  return (
    <div className="help">
      {groups.map((g) => (
        <section key={g}>
          <h3>{g}</h3>
          <div className="help-grid">
            {COMMANDS.filter((c) => c.group === g).map((c) => (
              <div className="help-row" key={c.name}>
                <span className="slash-name">{c.name}{c.args && <em> {c.args}</em>}</span>
                <span>{c.hint}{c.aliases && <span className="muted"> · {c.aliases.join(' ')}</span>}</span>
              </div>
            ))}
          </div>
        </section>
      ))}
      <section>
        <h3>Клавиши</h3>
        <div className="help-grid">
          {KEYS.map(([k, t]) => <div className="help-row" key={k}><kbd>{k}</kbd><span>{t}</span></div>)}
        </div>
      </section>
    </div>
  );
}

// ── /skills, /tasks, /done, /drop ────────────────────────────────────────────

function TextView({ command, arg }: { command: string; arg?: Record<string, unknown> }) {
  const { data, error, reload } = useCommand<CommandResult & { text?: string; stats?: Record<string, number> }>(command, arg);
  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const s = data.stats;
  return (
    <div className="textview">
      {s && (
        <div className="chips">
          <span className="badge on">{s.open} отворени</span>
          <span className="badge paid">{s.blocked} блокирани</span>
          <span className="badge free">{s.done} готови</span>
          <span className="badge">{s.decisions} решения</span>
        </div>
      )}
      <pre>{data.text?.trim() || 'Още нищо не е записано.'}</pre>
    </div>
  );
}
