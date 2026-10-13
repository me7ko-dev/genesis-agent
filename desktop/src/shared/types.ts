/** What travels between the main process and the window (see preload.ts). */

/** One entry of the server's event log (genesis_agent/remote_server.py). */
export type GenesisEvent = {
  seq: number;
  ts: number;
  type: string;
  text?: string;
  name?: string;
  result?: string;
  clipped?: boolean;
  id?: string;
  operation?: string;
  reasons?: string[];
  allow?: boolean;
  note?: string;
  busy?: boolean;
  label?: string;
};

/** The agent's task list (the TODO tool), sent with every `events` reply. */
export type Todo = { content: string; status: 'pending' | 'in_progress' | 'completed' };

/** Plan mode and the task list — "state" in the server's `events` reply. */
export type AgentState = { plan: boolean; todos: Todo[] };

export type EventsBatch = {
  events: GenesisEvent[];
  last: number;
  reset: boolean;
  busy: boolean;
  epoch: string;
  /** The reply hit its size cap: ask again at once, from `last` (server 2026-10-11). */
  more?: boolean;
  /** The last accepted `mid`s of `send` (see Backend.send). */
  mids?: string[];
  state?: AgentState;
};

export type Phase =
  | 'idle'        // no workspace chosen yet
  | 'missing'     // genesis.exe not found
  | 'starting'
  | 'ready'
  | 'error';

export type BackendState = {
  phase: Phase;
  workspace: string;
  /** What the agent reports it actually works in (it may refuse ~ or C:\). */
  agentWorkspace?: string;
  /** The opened folder is home or a drive root — why the agent works elsewhere. */
  workspaceGuard?: boolean;
  model?: string;
  version?: string;
  exe?: string;
  error?: string;
  busy: boolean;
  /** The agent answers `/` commands itself (genesis_agent/desktop_commands.py). */
  commands?: boolean;
  /** Plan mode (/plan): the agent only reads and plans. */
  plan?: boolean;
  /** The agent's task list for the running turn. */
  todos?: Todo[];
};

export type Settings = {
  recent: string[];
  lastWorkspace?: string;
  sidebar: boolean;
  exe?: string;
};

export type SendResult = { ok: boolean; error?: string };

// ── `/` commands (genesis_agent/desktop_commands.py) ─────────────────────────

export type CommandResult = { ok: boolean; error?: string; [k: string]: unknown };

export type UsagePeriod = { calls: number; tokens: number; prompt: number; completion: number; cached: number };

export type UsageReport = CommandResult & {
  days: number;
  periods: { today: UsagePeriod; week: UsagePeriod; month: UsagePeriod; all: UsagePeriod };
  daily: { day: string; calls: number; tokens: number }[];
  providers: { provider: string; name: string; calls: number; tokens: number; week: number; today: number; free: boolean; subscription?: boolean }[];
  models: { provider: string; model: string; calls: number; tokens: number; free: boolean; subscription?: boolean }[];
  paid_tokens: number;
  quotas: { provider: string; name: string; limit: number; spent: number; left: number; label: string; period: string }[];
  since: string | null;
};

export type StatusReport = CommandResult & {
  version: string;
  provider: string;
  provider_name: string;
  model: string;
  free: boolean;
  /** Paid by subscription (Claude through the `claude` CLI), not per token. */
  subscription?: boolean;
  coding_mode: boolean;
  local_only: string | null;
  elapsed: string;
  session_input_tokens: number;
  session_output_tokens: number;
  context_used: number;
  context_window: number;
  messages: number;
  workspace: string;
};

export type ModelsReport = CommandResult & {
  chain: { provider: string; name: string; model: string; free: boolean; active: boolean; subscription?: boolean }[];
  providers: { provider: string; name: string; ready: boolean; hint: string; active: boolean; subscription?: boolean }[];
  current: { provider: string; model: string };
};

export type ProviderModels = CommandResult & {
  provider: string;
  subscription?: boolean;
  models: { model: string; free: boolean; active: boolean; subscription?: boolean }[];
};

export type HistoryReport = CommandResult & {
  sessions: { file: string; modified: number; messages: number; preview: string }[];
};

export type UpdateReport = CommandResult & {
  version: string;
  up_to_date: boolean | null;
  current: string | null;
  ref: string | null;
  latest: string | null;
  changes: string[];
  command: string | null;
};

export interface GenesisApi {
  state(): Promise<BackendState>;
  settings(): Promise<Settings>;
  events(): Promise<GenesisEvent[]>;
  send(text: string): Promise<SendResult>;
  stop(): Promise<SendResult>;
  clear(): Promise<SendResult>;
  confirm(id: string, allow: boolean): Promise<SendResult>;
  command<T extends CommandResult = CommandResult>(name: string, arg?: Record<string, unknown>): Promise<T>;
  openWorkspace(path?: string): Promise<BackendState>;
  forgetWorkspace(path: string): Promise<Settings>;
  restart(): Promise<BackendState>;
  logs(): Promise<string>;
  reveal(path: string): Promise<void>;
  openExternal(url: string): Promise<void>;
  openTerminal(): Promise<void>;
  setSidebar(open: boolean): Promise<void>;
  onState(cb: (s: BackendState) => void): () => void;
  onEvents(cb: (b: EventsBatch) => void): () => void;
  onLog(cb: (line: string) => void): () => void;
}
