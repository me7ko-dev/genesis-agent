/**
 * Runs the agent: `genesis serve` on 127.0.0.1 in the chosen folder, and
 * turns its long-poll event log into a stream for the window.
 *
 * The desktop app carries no agent of its own — it is a window onto the same
 * genesis.exe the terminal runs, so both always have the same skills,
 * memory, keys and sandbox rules.
 */
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type { BackendState, CommandResult, EventsBatch, GenesisEvent, SendResult } from '../shared/types';
import { b64d, GenesisClient, keyId, ProtocolError } from './protocol';

const START_TIMEOUT_MS = 90_000;
const LOG_LINES = 2000;
const EVENT_LOG = 3000;

export function genesisHome(): string {
  return process.env.GENESIS_HOME || join(homedir(), '.genesis');
}

/** Where genesis.exe is: the app's own setting, then the installer's places, then PATH. */
export async function findExe(preferred?: string): Promise<string | undefined> {
  const local = process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local');
  const candidates = [
    preferred,
    process.env.GENESIS_EXE,
    process.env.GENESIS_INSTALL_DIR && join(process.env.GENESIS_INSTALL_DIR, 'genesis.exe'),
    join(local, 'Programs', 'Genesis', 'genesis.exe'),
  ];
  for (const c of candidates) if (c && existsSync(c)) return c;
  return new Promise((resolve) => {
    execFile('where.exe', ['genesis'], { windowsHide: true }, (err, out) => {
      const first = err ? '' : String(out).split(/\r?\n/).find((l) => l.toLowerCase().endsWith('.exe'));
      resolve(first || undefined);
    });
  });
}

const bindCache = new Map<string, { mtime: number; ok: boolean }>();

/** Newer builds take `--bind 127.0.0.1`; older ones listen on every interface. */
function supportsBind(exe: string): Promise<boolean> {
  const mtime = statSync(exe).mtimeMs;
  const hit = bindCache.get(exe);
  if (hit && hit.mtime === mtime) return Promise.resolve(hit.ok);
  return new Promise((resolve) => {
    execFile(exe, ['serve', '--help'], { windowsHide: true, timeout: 30_000, env: childEnv() }, (_e, out) => {
      const ok = String(out).includes('--bind');
      bindCache.set(exe, { mtime, ok });
      resolve(ok);
    });
  });
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      srv.close(() => resolve(port));
    });
  });
}

function childEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    NO_COLOR: '1',
    TERM: 'dumb',
    COLUMNS: '120',
  };
}

function readKey(): Buffer | undefined {
  try {
    const data = JSON.parse(readFileSync(join(genesisHome(), 'remote.json'), 'utf8')) as { key?: string };
    const key = data.key ? b64d(data.key) : undefined;
    return key && key.length === 32 ? key : undefined;
  } catch {
    return undefined;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const QR_LINE = /^[\s█▀▄]+$/;
const PROTOCOL_FAILURES = 3;

function commandLineOf(pid: number): Promise<string> {
  return new Promise((resolve) => {
    execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`],
      { windowsHide: true, timeout: 15_000 }, (_e, out) => resolve(String(out ?? '').trim()));
  });
}

/**
 * A crashed or killed window leaves its `genesis serve` behind (Windows does
 * not end children with their parent). The next start ends it — but only if
 * that pid is still exactly the command we started, never a `genesis serve`
 * the user runs for the phone.
 */
export async function reapOrphan(pidFile: string): Promise<void> {
  let rec: { pid?: number; port?: number } = {};
  try {
    rec = JSON.parse(readFileSync(pidFile, 'utf8')) as typeof rec;
  } catch {
    return;
  }
  if (!rec.pid || !rec.port) return;
  const cmd = await commandLineOf(rec.pid);
  if (/genesis(\.exe)?"?\s+serve\b/i.test(cmd) && cmd.includes(`--port ${rec.port}`)) {
    await new Promise<void>((resolve) => {
      execFile('taskkill.exe', ['/pid', String(rec.pid), '/T', '/F'], { windowsHide: true }, () => resolve());
    });
  }
  try { rmSync(pidFile); } catch { /* gone already */ }
}

export class Backend extends EventEmitter {
  /** Where the running agent's pid is kept, for reapOrphan after a crash. */
  pidFile?: string;
  private proc?: ChildProcess;
  private client?: GenesisClient;
  private generation = 0;
  private after = 0;
  private epoch = '';
  private log: string[] = [];
  private eventLog: GenesisEvent[] = [];
  state: BackendState = { phase: 'idle', workspace: '', busy: false };

  // ── lifecycle ─────────────────────────────────────────────────────────────

  async start(workspace: string, preferredExe?: string): Promise<BackendState> {
    await this.shutdown();
    const gen = ++this.generation;
    this.after = 0;
    this.epoch = '';
    this.eventLog = [];
    this.emit('events', { events: [], last: 0, reset: true, busy: false, epoch: '' } satisfies EventsBatch);

    const exe = await findExe(preferredExe);
    if (!exe) return this.set({ phase: 'missing', workspace, busy: false, error: undefined });
    this.set({ phase: 'starting', workspace, exe, busy: false, error: undefined,
               model: undefined, agentWorkspace: undefined });

    try {
      const port = await freePort();
      const args = ['serve', '--port', String(port)];
      if (await supportsBind(exe)) args.push('--bind', '127.0.0.1');
      this.line(`▶ ${exe} ${args.join(' ')}   (в ${workspace})`);
      const proc = spawn(exe, args, { cwd: workspace, env: childEnv(), windowsHide: true });
      this.proc = proc;
      if (this.pidFile && proc.pid) {
        try { writeFileSync(this.pidFile, JSON.stringify({ pid: proc.pid, port })); } catch { /* best effort */ }
      }
      let exited: string | undefined;
      proc.stdout?.on('data', (d: Buffer) => this.chunk(d));
      proc.stderr?.on('data', (d: Buffer) => this.chunk(d));
      proc.on('exit', (code, signal) => {
        exited = `genesis спря (код ${code ?? signal})`;
        this.line(`■ ${exited}`);
        if (gen === this.generation && this.state.phase !== 'idle') {
          this.set({ ...this.state, phase: 'error', busy: false, error: exited });
        }
      });
      proc.on('error', (e) => { exited = e.message; this.line(`✖ ${e.message}`); });

      const client = await this.connect(`http://127.0.0.1:${port}`, () => exited, gen);
      if (gen !== this.generation) return this.state;
      this.client = client;
      await this.refreshStatus();
      this.set({ ...this.state, phase: 'ready' });
      void this.pump(gen);
    } catch (e) {
      if (gen === this.generation) {
        await this.shutdown(false);
        this.set({ ...this.state, phase: 'error', busy: false, error: (e as Error).message });
      }
    }
    return this.state;
  }

  private async connect(base: string, exited: () => string | undefined, gen: number): Promise<GenesisClient> {
    const deadline = Date.now() + START_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (gen !== this.generation) throw new Error('cancelled');
      const why = exited();
      if (why) throw new Error(`${why}. Виж дневника за подробности.`);
      try {
        const probe = new GenesisClient(base, Buffer.alloc(32));
        const hello = await probe.hello(1500);
        const key = readKey();
        if (!key) throw new Error('няма ключ');
        if (hello.app !== 'genesis' || hello.key_id !== keyId(key)) {
          throw new Error('на порта отговаря друг Genesis (ключът не съвпада)');
        }
        return new GenesisClient(base, key);
      } catch (e) {
        if ((e as Error).message.startsWith('на порта')) throw e;
        await sleep(350);
      }
    }
    throw new Error('Genesis не тръгна за 90 секунди. Виж дневника.');
  }

  async shutdown(markIdle = true): Promise<void> {
    const proc = this.proc;
    this.proc = undefined;
    this.client = undefined;
    if (markIdle) this.generation++;
    if (proc && this.pidFile) {
      try { rmSync(this.pidFile, { force: true }); } catch { /* best effort */ }
    }
    if (proc && proc.exitCode === null && proc.pid) {
      // The agent runs shell commands of its own: end the whole tree.
      await new Promise<void>((resolve) => {
        execFile('taskkill.exe', ['/pid', String(proc.pid), '/T', '/F'], { windowsHide: true }, () => resolve());
      });
    }
  }

  // ── the conversation ──────────────────────────────────────────────────────

  private async pump(gen: number): Promise<void> {
    let failures = 0;
    while (gen === this.generation && this.client) {
      try {
        const reply = await this.client.call<EventsBatch & { ok: boolean }>(
          'events', { after: this.after, wait: 20 }, 35_000);
        failures = 0;
        if (gen !== this.generation) return;
        const reset = reply.reset || (this.epoch !== '' && reply.epoch !== this.epoch);
        this.epoch = reply.epoch;
        this.after = reply.last;
        this.eventLog = reset ? reply.events : [...this.eventLog, ...reply.events];
        if (this.eventLog.length > EVENT_LOG) this.eventLog = this.eventLog.slice(-EVENT_LOG);
        const wasBusy = this.state.busy;
        if (wasBusy !== reply.busy) this.set({ ...this.state, busy: reply.busy });
        if (reply.events.length || reset) {
          this.emit('events', { ...reply, reset } satisfies EventsBatch);
        }
        if (wasBusy && !reply.busy) void this.refreshStatus();
      } catch (e) {
        if (gen !== this.generation) return;
        failures++;
        if (e instanceof ProtocolError && e.kind === 'unauthorized') {
          this.set({ ...this.state, phase: 'error', busy: false, error: `Отказан достъп: ${e.message}` });
          return;
        }
        // Network hiccups heal on their own; a reply that cannot be read will not.
        if (!(e instanceof ProtocolError && e.kind === 'network') && failures >= PROTOCOL_FAILURES) {
          this.set({ ...this.state, phase: 'error', busy: false, error: `Връзката с агента се развали: ${(e as Error).message}` });
          return;
        }
        await sleep(Math.min(5000, 400 * failures));
      }
    }
  }

  async refreshStatus(): Promise<void> {
    if (!this.client) return;
    try {
      const s = await this.client.call<{ model?: string; workspace?: string; version?: string; busy: boolean;
                                          features?: string[] }>('status');
      this.set({ ...this.state, model: s.model, agentWorkspace: s.workspace, version: s.version, busy: s.busy,
                 commands: !!s.features?.includes('commands') });
    } catch {
      /* the next poll will tell */
    }
  }

  private async op(op: string, args: Record<string, unknown> = {}): Promise<SendResult> {
    if (!this.client) return { ok: false, error: 'not_ready' };
    try {
      return await this.client.call<SendResult>(op, args);
    } catch (e) {
      return { ok: false, error: (e as Error).message };
    }
  }

  send(text: string) { return this.op('send', { text }); }
  stop() { return this.op('stop'); }
  clear() { return this.op('clear'); }
  confirm(id: string, allow: boolean) { return this.op('confirm', { id, allow }); }

  /** A `/` command the agent answers without the model. Some ask the network
   *  (a provider's model list, GitHub for /update), hence the long timeout. */
  async command(name: string, arg: Record<string, unknown>): Promise<CommandResult> {
    if (!this.client) return { ok: false, error: 'Агентът още не е готов.' };
    if (!this.state.commands) {
      return { ok: false, error: 'old_agent' };
    }
    try {
      const r = await this.client.call<CommandResult>('command', { name, arg }, 60_000);
      if (name === 'set_model' || name === 'maxcoding' || name.startsWith('local_')) void this.refreshStatus();
      return r;
    } catch (e) {
      return { ok: false, error: (e as Error).message };
    }
  }

  events(): GenesisEvent[] {
    return this.eventLog;
  }

  // ── log ───────────────────────────────────────────────────────────────────

  private partial = '';

  private chunk(data: Buffer): void {
    const text = this.partial + data.toString('utf8').replace(ANSI, '');
    const lines = text.split(/\r?\n/);
    this.partial = lines.pop() ?? '';
    for (const l of lines) this.line(l);
  }

  private line(raw: string): void {
    // `genesis serve` prints a QR code and the pairing link for phones: the
    // window has no use for them, and the key has no business in a log.
    if (QR_LINE.test(raw)) return;
    const text = raw.replace(/#k=[A-Za-z0-9_-]+/g, '#k=•••');
    this.log.push(text);
    if (this.log.length > LOG_LINES) this.log.splice(0, this.log.length - LOG_LINES);
    this.emit('log', text);
  }

  logText(): string {
    return this.log.join('\n');
  }

  private set(next: BackendState): BackendState {
    this.state = next;
    this.emit('state', next);
    return next;
  }
}
