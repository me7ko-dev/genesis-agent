import * as Clipboard from 'expo-clipboard';
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  ActivityIndicator, Modal, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import {
  COMMANDS, explain, fmtDay, fmtTokens, fmtWhen, full, niceMax, toggleMessage,
  type CommandResult, type HistoryReport, type ModelsReport, type ProviderModels, type SheetView,
  type StatusReport, type UpdateReport, type UsageReport,
} from '../lib/commands';
import { mono, useTheme, type Theme } from '../lib/theme';

// The `/` command views of Genesis Desktop (desktop/src/renderer/components/
// Panels.tsx) for the phone: the same data from the same agent commands.

export type RunCommand = <T extends { ok: boolean; error?: string }>(name: string, arg?: Record<string, unknown>) => Promise<T>;

type Ctx = {
  run: RunCommand;
  onFlash: (text: string) => void;
  onOpen: (s: SheetView) => void;
  onClose: () => void;
  onPhone: boolean;
  /** Update Genesis on this phone (Termux: `genesis phone update`). */
  onSelfUpdate: () => void;
};

const TITLES: Record<SheetView['view'], string> = {
  usage: 'Разход', status: 'Състояние', models: 'Модели', history: 'История',
  update: 'Обновяване', help: 'Команди', text: '',
};

export function Sheet({ sheet, ...ctx }: Ctx & { sheet: SheetView | null }) {
  const theme = useTheme();
  const title = !sheet ? '' : sheet.view === 'text' ? sheet.title : TITLES[sheet.view];
  return (
    <Modal visible={!!sheet} animationType="slide" onRequestClose={ctx.onClose} statusBarTranslucent>
      <SafeAreaView style={[s.flex, { backgroundColor: theme.bg }]} edges={['top', 'left', 'right', 'bottom']}>
        <View style={[s.head, { borderColor: theme.border }]}>
          <Text style={[s.title, { color: theme.text }]}>{title}</Text>
          <Pressable accessibilityRole="button" accessibilityLabel="Затвори" onPress={ctx.onClose} hitSlop={12}>
            <Text style={[s.close, { color: theme.muted }]}>✕</Text>
          </Pressable>
        </View>
        {sheet ? (
          <ScrollView contentContainerStyle={s.body} keyboardShouldPersistTaps="handled">
            {sheet.view === 'usage' && <UsageView {...ctx} />}
            {sheet.view === 'status' && <StatusView {...ctx} />}
            {sheet.view === 'models' && <ModelsView tab={sheet.tab} {...ctx} />}
            {sheet.view === 'history' && <HistoryView {...ctx} />}
            {sheet.view === 'update' && <UpdateView {...ctx} />}
            {sheet.view === 'help' && <HelpView {...ctx} />}
            {sheet.view === 'text' && <TextView command={sheet.command} arg={sheet.arg} {...ctx} />}
          </ScrollView>
        ) : null}
      </SafeAreaView>
    </Modal>
  );
}

// ── plumbing ─────────────────────────────────────────────────────────────────

function useCommand<T extends CommandResult>(run: RunCommand, name: string, arg?: Record<string, unknown>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const key = JSON.stringify(arg ?? {});
  const load = useCallback(async () => {
    setError('');
    const r = await run<T>(name, JSON.parse(key) as Record<string, unknown>);
    if (r.ok) setData(r);
    else setError(explain(r.error));
  }, [run, name, key]);
  useEffect(() => { void load(); }, [load]);
  return { data, error, reload: load };
}

function Loading() {
  const theme = useTheme();
  return (
    <View style={s.center}>
      <ActivityIndicator color={theme.accent} />
      <Text style={{ color: theme.muted }}>Зареждам…</Text>
    </View>
  );
}

function Failure({ text, onRetry }: { text: string; onRetry?: () => void }) {
  const theme = useTheme();
  return (
    <View style={[s.card, { borderColor: theme.danger, backgroundColor: theme.surface }]}>
      <Text style={{ color: theme.text, lineHeight: 20 }}>⚠ {text}</Text>
      {onRetry ? <Btn label="Пак" onPress={onRetry} /> : null}
    </View>
  );
}

function Btn({ label, onPress, primary, disabled }: { label: string; onPress: () => void; primary?: boolean; disabled?: boolean }) {
  const theme = useTheme();
  return (
    <Pressable accessibilityRole="button" onPress={onPress} disabled={disabled}
      style={[s.btn, primary ? { backgroundColor: theme.accent } : { borderColor: theme.border, borderWidth: 1 },
        disabled ? { opacity: 0.5 } : null]}>
      <Text style={[s.btnText, { color: primary ? theme.accentText : theme.text }]}>{label}</Text>
    </Pressable>
  );
}

function Seg<T extends string | number>({ items, value, onChange }: { items: { key: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  const theme = useTheme();
  return (
    <View style={[s.seg, { backgroundColor: theme.surfaceAlt }]}>
      {items.map((it) => (
        <Pressable key={String(it.key)} onPress={() => onChange(it.key)}
          style={[s.segItem, it.key === value ? { backgroundColor: theme.surface } : null]}>
          <Text style={{ color: it.key === value ? theme.text : theme.muted, fontWeight: '600', fontSize: 13 }}>{it.label}</Text>
        </Pressable>
      ))}
    </View>
  );
}

function Meter({ value, max, warn }: { value: number; max: number; warn?: boolean }) {
  const theme = useTheme();
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <View style={[s.meter, { backgroundColor: theme.surfaceAlt }]}>
      <View style={[s.meterFill, { width: `${Math.max(pct, value > 0 ? 1.5 : 0)}%`, backgroundColor: warn ? theme.warn : theme.accent }]} />
    </View>
  );
}

function Badge({ free, on }: { free?: boolean; on?: boolean }) {
  const theme = useTheme();
  const [text, color] = on ? ['сега', theme.accent] : free ? ['FREE', theme.ok] : ['PAID', theme.warn];
  return <Text style={[s.badge, { color, borderColor: color }]}>{text}</Text>;
}

function Section({ title, sub, children }: { title: string; sub?: string; children: ReactNode }) {
  const theme = useTheme();
  return (
    <View style={s.section}>
      <Text style={[s.h3, { color: theme.text }]}>{title}{sub ? <Text style={{ color: theme.muted, fontWeight: '400' }}> · {sub}</Text> : null}</Text>
      {children}
    </View>
  );
}

function Card({ children, theme }: { children: ReactNode; theme: Theme }) {
  return <View style={[s.card, { backgroundColor: theme.surface, borderColor: theme.border }]}>{children}</View>;
}

// ── /usage ───────────────────────────────────────────────────────────────────

function UsageView({ run, onOpen }: Ctx) {
  const theme = useTheme();
  const [days, setDays] = useState<number>(30);
  const { data, error, reload } = useCommand<UsageReport>(run, 'usage', { days });
  const status = useCommand<StatusReport>(run, 'status');

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const p = data.periods;
  const range = p.month;
  const free = data.paid_tokens === 0;

  return (
    <View style={s.gap}>
      <View style={s.row}>
        <Seg items={[7, 30, 90].map((d) => ({ key: d, label: `${d} дни` }))} value={days} onChange={setDays} />
        <Pressable onPress={() => { void reload(); void status.reload(); }} hitSlop={10}>
          <Text style={{ color: theme.accent, fontSize: 20 }}>↻</Text>
        </Pressable>
      </View>

      <Card theme={theme}>
        <Text style={{ color: theme.muted }}>Похарчени пари</Text>
        <Text style={[s.money, { color: theme.text }]}>{free ? '$0.00' : 'виж сметката'}</Text>
        <Text style={{ color: free ? theme.ok : theme.warn, lineHeight: 19 }}>
          {free
            ? `✓ Всичките ${full(p.all.calls)} обръщения са към безплатни модели.`
            : `⚠ ${fmtTokens(data.paid_tokens)} токена са към платени модели. Цената им е в сметката при доставчика.`}
        </Text>
        <Text style={{ color: theme.muted, marginTop: 4 }}>
          ⚡ <Text style={{ color: theme.text, fontWeight: '700' }}>{fmtTokens(p.all.tokens)}</Text> токена общо
          {data.since ? ` · от ${fmtDay(data.since, true)}` : ''}
        </Text>
      </Card>

      <View style={s.tiles}>
        <Tile label="Днес" period={p.today} free={free} />
        <Tile label="7 дни" period={p.week} free={free} />
        {data.days > 7 ? <Tile label={`${data.days} дни`} period={range} free={free} /> : null}
        <Tile label="Общо" period={p.all} free={free} />
      </View>

      {data.quotas.length > 0 || status.data ? (
        <Section title="Колко остава">
          {data.quotas.map((q) => {
            const low = q.left / q.limit < 0.2;
            return (
              <View key={q.provider} style={s.quota}>
                <View style={s.rowBetween}>
                  <Text style={{ color: theme.text, fontWeight: '700' }}>{q.name}</Text>
                  <Text style={{ color: low ? theme.warn : theme.text }}>{low ? '⚠ ' : ''}остават <Text style={{ fontWeight: '700' }}>{fmtTokens(q.left)}</Text></Text>
                </View>
                <Meter value={q.spent} max={q.limit} warn={low} />
                <Text style={[s.fine, { color: theme.muted }]}>
                  {fmtTokens(q.spent)} от {fmtTokens(q.limit)} · безплатна квота {q.label}
                </Text>
              </View>
            );
          })}
          {status.data ? (
            <View style={s.quota}>
              <View style={s.rowBetween}>
                <Text style={{ color: theme.text, fontWeight: '700' }}>Контекст на разговора</Text>
                <Text style={{ color: theme.text }}>свободни <Text style={{ fontWeight: '700' }}>{fmtTokens(Math.max(0, status.data.context_window - status.data.context_used))}</Text></Text>
              </View>
              <Meter value={status.data.context_used} max={status.data.context_window}
                warn={status.data.context_used / status.data.context_window > 0.8} />
              <Text style={[s.fine, { color: theme.muted }]}>
                ~{fmtTokens(status.data.context_used)} от {fmtTokens(status.data.context_window)} токена · {status.data.model}
              </Text>
            </View>
          ) : null}
          <Text style={[s.fine, { color: theme.muted }]}>
            Доставчиците не казват колко остава. Числата са от дневника на Genesis спрямо обявените безплатни лимити.
          </Text>
        </Section>
      ) : null}

      <Section title="Токени по дни" sub={`последните ${data.days} дни`}>
        <DailyChart daily={data.daily} />
        <Text style={[s.fine, { color: theme.muted }]}>
          {fmtTokens(range.prompt)} изпратени, {fmtTokens(range.completion)} отговори
          {range.cached > 0 ? `, ${fmtTokens(range.cached)} от кеша` : ''} · {full(range.calls)} обръщения
        </Text>
      </Section>

      <Section title="По доставчик" sub="от началото">
        {data.providers.map((r) => {
          const top = Math.max(1, ...data.providers.map((x) => x.tokens));
          return (
            <View key={r.provider} style={s.hbar}>
              <View style={s.rowBetween}>
                <Text style={{ color: theme.text }}>{r.name} <Badge free={r.free} /></Text>
                <Text style={{ color: theme.text }}>
                  <Text style={{ fontWeight: '700' }}>{fmtTokens(r.tokens)}</Text>
                  <Text style={{ color: theme.muted }}> {p.all.tokens ? Math.round((r.tokens / p.all.tokens) * 100) : 0}%</Text>
                </Text>
              </View>
              <Meter value={r.tokens} max={top} />
            </View>
          );
        })}
      </Section>

      <Section title="Най-ползвани модели">
        {data.models.map((m) => (
          <View key={`${m.provider}/${m.model}`} style={[s.listRow, { borderColor: theme.border }]}>
            <Text style={[mono, { color: theme.text, fontSize: 12, flex: 1 }]} numberOfLines={2}>{m.model} <Badge free={m.free} /></Text>
            <Text style={{ color: theme.muted, fontSize: 12, textAlign: 'right' }}>
              {fmtTokens(m.tokens)}{'\n'}{full(m.calls)} обр.
            </Text>
          </View>
        ))}
      </Section>

      <View style={s.row}>
        <Btn label="Смени модела" onPress={() => onOpen({ view: 'models', tab: 'pick' })} />
        <Btn label="Състояние" onPress={() => onOpen({ view: 'status' })} />
      </View>
    </View>
  );
}

function Tile({ label, period, free }: { label: string; period: { calls: number; tokens: number }; free: boolean }) {
  const theme = useTheme();
  return (
    <View style={[s.tile, { backgroundColor: theme.surface, borderColor: theme.border }]}>
      <Text style={{ color: theme.muted, fontSize: 12 }}>{label}</Text>
      <Text style={[s.tileValue, { color: theme.text }]}>{fmtTokens(period.tokens)}</Text>
      <Text style={{ color: theme.muted, fontSize: 12 }}>{full(period.calls)} обр.{free ? ' · $0' : ''}</Text>
    </View>
  );
}

function DailyChart({ daily }: { daily: UsageReport['daily'] }) {
  const theme = useTheme();
  const [pick, setPick] = useState<number | null>(null);
  if (!daily.length) return null;
  const nice = niceMax(Math.max(1, ...daily.map((d) => d.tokens)));
  const h = pick === null ? null : daily[pick];
  return (
    <View>
      <Text style={[s.fine, { color: theme.muted }]}>
        {h ? `${fmtDay(h.day, true)}: ${full(h.tokens)} токена · ${full(h.calls)} обр.` : `Докосни ден · горе ${fmtTokens(nice)}`}
      </Text>
      <View style={[s.chart, { borderColor: theme.border }]}>
        {daily.map((d, i) => (
          <Pressable key={d.day} style={s.barSlot} onPress={() => setPick(i === pick ? null : i)}>
            <View style={[s.bar, {
              height: `${Math.max((d.tokens / nice) * 100, d.tokens > 0 ? 2 : 0)}%`,
              backgroundColor: i === pick ? theme.warn : theme.accent,
            }]} />
          </Pressable>
        ))}
      </View>
      <View style={s.rowBetween}>
        <Text style={[s.fine, { color: theme.muted }]}>{fmtDay(daily[0].day)}</Text>
        <Text style={[s.fine, { color: theme.muted }]}>днес</Text>
      </View>
    </View>
  );
}

// ── /status ──────────────────────────────────────────────────────────────────

function StatusView({ run, onFlash, onOpen, onPhone }: Ctx) {
  const theme = useTheme();
  const { data, error, reload } = useCommand<StatusReport>(run, 'status');
  const [busy, setBusy] = useState('');

  async function toggle(name: string) {
    setBusy(name);
    const r = await run<CommandResult & { on?: boolean; model?: string }>(name);
    setBusy('');
    if (!r.ok) { onFlash(explain(r.error)); return; }
    onFlash(toggleMessage(name, r));
    void reload();
  }

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const ctxPct = Math.round((data.context_used / Math.max(1, data.context_window)) * 100);
  const kv: [string, string][] = [
    ['Модел', data.model],
    ['Доставчик', data.local_only ? `Само локален: ${data.local_only}` : data.provider_name],
    ['Работна папка', data.workspace],
    ['Сесия', `${data.elapsed} · ${data.messages} съобщения`],
    ['Токени в сесията', `${fmtTokens(data.session_input_tokens)} изпратени · ${fmtTokens(data.session_output_tokens)} отговори`],
    ['Версия', `genesis-agent ${data.version}`],
  ];
  return (
    <View style={s.gap}>
      <Card theme={theme}>
        {kv.map(([k, v]) => (
          <View key={k} style={s.kv}>
            <Text style={{ color: theme.muted, fontSize: 13 }}>{k}</Text>
            <Text selectable style={[k === 'Модел' || k === 'Работна папка' ? mono : null, { color: theme.text, fontWeight: '600' }]}>
              {v}{k === 'Модел' ? '  ' : ''}{k === 'Модел' ? <Badge free={data.free} /> : null}
            </Text>
          </View>
        ))}
      </Card>
      <Card theme={theme}>
        <View style={s.rowBetween}>
          <Text style={{ color: theme.text, fontWeight: '700' }}>Контекст</Text>
          <Text style={{ color: theme.text }}>{ctxPct}% · свободни {fmtTokens(data.context_window - data.context_used)}</Text>
        </View>
        <Meter value={data.context_used} max={data.context_window} warn={ctxPct > 80} />
      </Card>
      <Section title="Режими">
        <Mode on={data.coding_mode} busy={busy === 'maxcoding'} onPress={() => void toggle('maxcoding')}
          title="Макс кодинг" text="Най-силните безплатни модели за код. По-бавно и харчи повече квота." />
        {onPhone ? null : (
          <>
            <Mode on={data.local_only === 'qwen3:14b'} busy={busy === 'local_max'} onPress={() => void toggle('local_max')}
              title="Локален MAX" text="Само qwen3:14b на компютъра, без облак." />
            <Mode on={data.local_only === 'qwen2.5-coder:7b'} busy={busy === 'local_normal'} onPress={() => void toggle('local_normal')}
              title="Локален бърз" text="Само qwen2.5-coder:7b на компютъра." />
          </>
        )}
      </Section>
      <View style={s.row}>
        <Btn label="Смени модела" onPress={() => onOpen({ view: 'models', tab: 'pick' })} />
        <Btn label="Разход" onPress={() => onOpen({ view: 'usage' })} />
      </View>
    </View>
  );
}

function Mode({ on, busy, title, text, onPress }: { on: boolean; busy: boolean; title: string; text: string; onPress: () => void }) {
  const theme = useTheme();
  return (
    <Pressable onPress={onPress} disabled={busy}
      style={[s.mode, { backgroundColor: theme.surface, borderColor: on ? theme.accent : theme.border }]}>
      <View style={s.flex}>
        <Text style={{ color: theme.text, fontWeight: '700' }}>{title}</Text>
        <Text style={{ color: theme.muted, fontSize: 13, lineHeight: 18 }}>{text}</Text>
      </View>
      {busy ? <ActivityIndicator color={theme.accent} /> : <Switch value={on} onValueChange={onPress} />}
    </Pressable>
  );
}

// ── /model, /models ──────────────────────────────────────────────────────────

function ModelsView({ tab: initial, run, onFlash, onClose }: Ctx & { tab: 'pick' | 'chain' }) {
  const theme = useTheme();
  const [tab, setTab] = useState(initial);
  const { data, error, reload } = useCommand<ModelsReport>(run, 'models');
  const [provider, setProvider] = useState<string | null>(null);

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <View style={s.gap}>
      <Text style={{ color: theme.muted }}>Сега: <Text style={[mono, { color: theme.text, fontWeight: '700' }]}>{data.current.provider}/{data.current.model}</Text></Text>
      <Seg items={[{ key: 'pick' as const, label: 'Избери модел' }, { key: 'chain' as const, label: `Верига (${data.chain.length})` }]}
        value={tab} onChange={(t) => { setTab(t); setProvider(null); }} />
      {tab === 'chain' ? (
        <>
          <Text style={[s.fine, { color: theme.muted }]}>Когато един модел откаже (квота, лимит), Genesis минава на следващия в този ред.</Text>
          {data.chain.map((c, i) => (
            <View key={`${c.provider}/${c.model}`} style={[s.listRow, { borderColor: c.active ? theme.accent : theme.border }]}>
              <Text style={{ color: theme.muted, width: 24 }}>{i + 1}.</Text>
              <View style={s.flex}>
                <Text style={[mono, { color: theme.text, fontSize: 12 }]}>{c.model}</Text>
                <Text style={{ color: theme.muted, fontSize: 12 }}>{c.name} <Badge free={c.free} />{c.active ? ' ' : ''}{c.active ? <Badge on /> : null}</Text>
              </View>
            </View>
          ))}
        </>
      ) : provider ? (
        <ProviderPicker provider={provider} name={data.providers.find((x) => x.provider === provider)?.name ?? provider}
          run={run} onBack={() => setProvider(null)} onFlash={onFlash}
          onPicked={(m) => { onFlash(`Моделът е сменен: ${m}`); onClose(); }} />
      ) : (
        data.providers.map((p) => (
          <Pressable key={p.provider} disabled={!p.ready} onPress={() => setProvider(p.provider)}
            style={[s.listRow, { borderColor: p.active ? theme.accent : theme.border, opacity: p.ready ? 1 : 0.55 }]}>
            <Text style={{ color: theme.text, fontWeight: '700', flex: 1 }}>{p.name}</Text>
            <Text style={{ color: p.ready ? theme.ok : theme.muted, fontSize: 12, flexShrink: 1, textAlign: 'right' }}>
              {p.ready ? (p.active ? 'ползва се' : 'готов ›') : p.hint}
            </Text>
          </Pressable>
        ))
      )}
    </View>
  );
}

function ProviderPicker({ provider, name, run, onBack, onPicked, onFlash }: {
  provider: string; name: string; run: RunCommand; onBack: () => void; onPicked: (m: string) => void; onFlash: (t: string) => void;
}) {
  const theme = useTheme();
  const { data, error, reload } = useCommand<ProviderModels>(run, 'provider_models', { provider });
  const [q, setQ] = useState('');
  const [freeOnly, setFreeOnly] = useState(false);
  const list = useMemo(() => (data?.models ?? []).filter((m) =>
    m.model.toLowerCase().includes(q.toLowerCase()) && (!freeOnly || m.free)), [data, q, freeOnly]);

  async function pick(model: string) {
    const r = await run<CommandResult & { note?: string }>('set_model', { provider, model });
    if (!r.ok) { onFlash(explain(r.error)); return; }
    onPicked(r.note ? `${model}. ${r.note}` : model);
  }

  return (
    <View style={s.gap}>
      <View style={s.row}>
        <Btn label="‹ Доставчици" onPress={onBack} />
        <Text style={{ color: theme.text, fontWeight: '700' }}>{name}</Text>
      </View>
      {error ? <Failure text={error} onRetry={reload} /> : null}
      {!error && !data ? <Loading /> : null}
      {data ? (
        <>
          <TextInput value={q} onChangeText={setQ} placeholder={`Търси сред ${data.models.length} модела…`}
            placeholderTextColor={theme.muted} autoCapitalize="none" autoCorrect={false}
            style={[s.input, { color: theme.text, borderColor: theme.border, backgroundColor: theme.surface }]} />
          <View style={s.row}>
            <Switch value={freeOnly} onValueChange={setFreeOnly} />
            <Text style={{ color: theme.text }}>само FREE</Text>
          </View>
          {list.slice(0, 200).map((m) => (
            <Pressable key={m.model} onPress={() => void pick(m.model)}
              style={[s.listRow, { borderColor: m.active ? theme.accent : theme.border }]}>
              <Text style={[mono, { color: theme.text, fontSize: 12, flex: 1 }]}>{m.model}</Text>
              <Badge free={m.free} />
              {m.active ? <Badge on /> : null}
            </Pressable>
          ))}
          {list.length === 0 ? <Text style={{ color: theme.muted }}>Нищо не съвпада.</Text> : null}
          {list.length > 200 ? <Text style={{ color: theme.muted }}>…и още {list.length - 200}. Потърси по име.</Text> : null}
        </>
      ) : null}
    </View>
  );
}

// ── /history ─────────────────────────────────────────────────────────────────

function HistoryView({ run, onFlash, onClose }: Ctx) {
  const theme = useTheme();
  const { data, error, reload } = useCommand<HistoryReport>(run, 'history');
  const [q, setQ] = useState('');

  async function load(file: string) {
    const r = await run<CommandResult & { messages?: number }>('load_history', { file });
    if (!r.ok) { onFlash(explain(r.error)); return; }
    onFlash(`Разговорът е зареден (${r.messages} съобщения). Можеш да продължиш оттам.`);
    onClose();
  }

  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const list = data.sessions.filter((x) => x.preview.toLowerCase().includes(q.toLowerCase()));
  return (
    <View style={s.gap}>
      <TextInput value={q} onChangeText={setQ} placeholder="Търси в разговорите…" placeholderTextColor={theme.muted}
        style={[s.input, { color: theme.text, borderColor: theme.border, backgroundColor: theme.surface }]} />
      {list.length === 0 ? <Text style={{ color: theme.muted }}>Няма запазени разговори.</Text> : null}
      {list.map((x) => (
        <Pressable key={x.file} onPress={() => void load(x.file)}
          style={[s.card, { backgroundColor: theme.surface, borderColor: theme.border }]}>
          <Text style={{ color: theme.text }} numberOfLines={2}>{x.preview || '(без текст)'}</Text>
          <Text style={{ color: theme.muted, fontSize: 12 }}>{fmtWhen(x.modified)} · {x.messages} съобщения</Text>
        </Pressable>
      ))}
      <Text style={[s.fine, { color: theme.muted }]}>Зареденият разговор заменя текущия. Новите съобщения се пазят в отделен файл.</Text>
    </View>
  );
}

// ── /update ──────────────────────────────────────────────────────────────────

function UpdateView({ run, onFlash, onSelfUpdate }: Ctx) {
  const theme = useTheme();
  const { data, error, reload } = useCommand<UpdateReport>(run, 'update');
  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  return (
    <View style={s.gap}>
      <Card theme={theme}>
        <Text style={{ color: theme.muted }}>Версия</Text>
        <Text style={{ color: theme.text, fontWeight: '700' }}>genesis-agent {data.version}</Text>
        <Text style={{ color: theme.muted, marginTop: 6 }}>Сглобено от</Text>
        <Text style={[mono, { color: theme.text }]}>{data.current ?? 'не е от git'}{data.ref ? ` (${data.ref})` : ''}</Text>
      </Card>
      {data.current === null ? <Text style={{ color: theme.text }}>Това копие не е инсталирано от GitHub, затова няма с какво да се сравни.</Text> : null}
      {data.current !== null && data.up_to_date === null ? <Failure text="Не можах да питам GitHub (мрежа или лимит)." onRetry={reload} /> : null}
      {data.up_to_date === true ? <Text style={{ color: theme.ok, fontWeight: '700' }}>✓ Това е най-новата версия.</Text> : null}
      {data.up_to_date === false ? (
        <View style={s.gap}>
          <Text style={{ color: theme.text }}><Text style={{ fontWeight: '700' }}>Има по-нова версия:</Text> <Text style={mono}>{data.latest}</Text></Text>
          {data.changes.map((c) => <Text key={c} style={{ color: theme.text }}>• {c}</Text>)}
        </View>
      ) : null}
      {data.self_update && data.up_to_date !== true ? (
        <>
          <Btn primary label="Обнови Genesis сега" onPress={onSelfUpdate} />
          <Text style={[s.fine, { color: theme.muted }]}>Genesis се сваля от GitHub и се пуска отново (1–3 минути). Приложението се свързва само.</Text>
        </>
      ) : data.command && data.up_to_date !== true ? (
        <>
          <Text style={[s.fine, { color: theme.muted }]}>Обновяването се пуска на компютъра:</Text>
          <Text selectable style={[mono, s.code, { color: theme.text, backgroundColor: theme.code }]}>{data.command}</Text>
          <Btn label="Копирай" onPress={() => { void Clipboard.setStringAsync(data.command ?? ''); onFlash('Командата е копирана.'); }} />
        </>
      ) : null}
    </View>
  );
}

// ── /help ────────────────────────────────────────────────────────────────────

function HelpView({ onPhone }: Ctx) {
  const theme = useTheme();
  const cmds = COMMANDS.filter((c) => !(onPhone && c.pcOnly));
  const groups = [...new Set(cmds.map((c) => c.group))];
  return (
    <View style={s.gap}>
      <Text style={{ color: theme.muted }}>Напиши „/“ в полето за съобщение — излиза списък.</Text>
      {groups.map((g) => (
        <Section key={g} title={g}>
          {cmds.filter((c) => c.group === g).map((c) => (
            <View key={c.name} style={s.helpRow}>
              <Text style={[mono, { color: theme.accent, fontWeight: '700' }]}>{c.name}{c.args ? ` ${c.args}` : ''}</Text>
              <Text style={{ color: theme.text, lineHeight: 19 }}>
                {c.hint}{c.aliases ? <Text style={{ color: theme.muted }}> · {c.aliases.join(' ')}</Text> : null}
              </Text>
            </View>
          ))}
        </Section>
      ))}
    </View>
  );
}

// ── /skills, /tasks ──────────────────────────────────────────────────────────

function TextView({ command, arg, run }: Ctx & { command: string; arg?: Record<string, unknown> }) {
  const theme = useTheme();
  const { data, error, reload } = useCommand<CommandResult & { text?: string; stats?: Record<string, number> }>(run, command, arg);
  if (error) return <Failure text={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const st = data.stats;
  return (
    <View style={s.gap}>
      {st ? (
        <Text style={{ color: theme.muted }}>
          {st.open} отворени · {st.blocked} блокирани · {st.done} готови · {st.decisions} решения
        </Text>
      ) : null}
      <Text selectable style={[mono, s.code, { color: theme.text, backgroundColor: theme.code }]}>
        {data.text?.trim() || 'Още нищо не е записано.'}
      </Text>
    </View>
  );
}

const s = StyleSheet.create({
  flex: { flex: 1 },
  head: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 18, paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth },
  title: { fontSize: 20, fontWeight: '800' },
  close: { fontSize: 22, fontWeight: '600' },
  body: { padding: 16, paddingBottom: 40, maxWidth: 640, width: '100%', alignSelf: 'center' },
  gap: { gap: 12 },
  center: { alignItems: 'center', gap: 8, paddingVertical: 40 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
  rowBetween: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8 },
  card: { borderRadius: 14, borderWidth: StyleSheet.hairlineWidth, padding: 14, gap: 6 },
  money: { fontSize: 34, fontWeight: '800' },
  tiles: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 },
  tile: { flexGrow: 1, flexBasis: '45%', borderRadius: 12, borderWidth: StyleSheet.hairlineWidth, padding: 12, gap: 2 },
  tileValue: { fontSize: 22, fontWeight: '800' },
  section: { gap: 10, marginTop: 6 },
  h3: { fontSize: 16, fontWeight: '700' },
  quota: { gap: 6 },
  meter: { height: 8, borderRadius: 4, overflow: 'hidden' },
  meterFill: { height: '100%', borderRadius: 4 },
  fine: { fontSize: 12, lineHeight: 17 },
  chart: { height: 120, flexDirection: 'row', alignItems: 'flex-end', gap: 2, borderBottomWidth: 1, marginVertical: 6 },
  barSlot: { flex: 1, height: '100%', justifyContent: 'flex-end' },
  bar: { width: '100%', borderTopLeftRadius: 2, borderTopRightRadius: 2 },
  hbar: { gap: 5 },
  listRow: { flexDirection: 'row', alignItems: 'center', gap: 8, borderWidth: 1, borderRadius: 10, padding: 10 },
  badge: { fontSize: 10, fontWeight: '800', borderWidth: 1, borderRadius: 4, paddingHorizontal: 4, overflow: 'hidden' },
  kv: { paddingVertical: 4, gap: 1 },
  mode: { flexDirection: 'row', alignItems: 'center', gap: 12, borderWidth: 1, borderRadius: 12, padding: 12 },
  seg: { flexDirection: 'row', borderRadius: 10, padding: 3, gap: 3 },
  segItem: { paddingHorizontal: 12, paddingVertical: 7, borderRadius: 8 },
  btn: { borderRadius: 10, paddingHorizontal: 14, paddingVertical: 10, alignItems: 'center' },
  btnText: { fontWeight: '700', fontSize: 14 },
  input: { borderWidth: 1, borderRadius: 10, padding: 10, fontSize: 15 },
  code: { fontSize: 12, lineHeight: 17, padding: 10, borderRadius: 8, overflow: 'hidden' },
  helpRow: { gap: 2, paddingVertical: 2 },
});
