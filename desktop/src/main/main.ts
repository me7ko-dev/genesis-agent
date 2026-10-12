import { app, BrowserWindow, dialog, ipcMain, nativeTheme, shell } from 'electron';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { Backend, findExe, genesisHome, reapOrphan } from './backend';
import { loadSettings, pushRecent, saveSettings } from './settings';

const DEV_URL = process.env.GENESIS_DESKTOP_DEV_URL;
const BG = '#0d0f14';

let win: BrowserWindow | null = null;
const backend = new Backend();
let settings = loadSettings();

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
}

function createWindow(): void {
  nativeTheme.themeSource = 'dark';
  win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 760,
    minHeight: 520,
    show: false,
    backgroundColor: BG,
    title: 'Genesis',
    icon: join(__dirname, '../renderer/icon.png'),
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: BG, symbolColor: '#9aa3b2', height: 40 },
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.once('ready-to-show', () => win?.show());

  // Links in answers open in the browser, never inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (DEV_URL && url.startsWith(DEV_URL)) return;
    e.preventDefault();
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
  });

  // Screenshots for checking the design: GENESIS_DESKTOP_SHOT=out.png[,delayMs[,scrollPx]]
  const shot = process.env.GENESIS_DESKTOP_SHOT;
  if (shot) {
    // GENESIS_DESKTOP_SHOT=out.png,delay,scroll — scroll: pixels down inside an open `/` window.
    const [out, delay, scroll] = shot.split(',');
    win.webContents.once('did-finish-load', () => {
      setTimeout(async () => {
        if (scroll) {
          await win?.webContents.executeJavaScript(`document.querySelector('.sheet-body')?.scrollBy(0, ${Number(scroll) || 0})`);
          await new Promise((r) => setTimeout(r, 300));
        }
        const img = await win?.webContents.capturePage();
        if (img) writeFileSync(out, img.toPNG());
      }, Number(delay) || 4000);
    });
  }

  // Screenshots of a `/` window: GENESIS_DESKTOP_SHEET=usage opens it once the agent is up.
  const hash = process.env.GENESIS_DESKTOP_SHEET ? `sheet=${process.env.GENESIS_DESKTOP_SHEET}` : '';
  if (DEV_URL) void win.loadURL(hash ? `${DEV_URL}#${hash}` : DEV_URL);
  else void win.loadFile(join(__dirname, '../renderer/index.html'), hash ? { hash } : undefined);
  win.on('closed', () => { win = null; });
}

function send(channel: string, value: unknown): void {
  if (win && !win.isDestroyed()) win.webContents.send(channel, value);
}

backend.on('state', (s) => send('state', s));
backend.on('events', (b) => send('events', b));
backend.on('log', (l) => send('log', l));
if (process.env.GENESIS_DESKTOP_DEBUG) {
  backend.on('state', (s) => console.log('[state]', JSON.stringify(s)));
  backend.on('events', (b) => console.log('[events]', JSON.stringify(b).slice(0, 400)));
}

async function openWorkspace(path?: string) {
  let target = path;
  if (!target) {
    const res = await dialog.showOpenDialog(win!, {
      title: 'Папка на проекта',
      properties: ['openDirectory', 'createDirectory'],
      defaultPath: settings.lastWorkspace,
    });
    if (res.canceled || !res.filePaths[0]) return backend.state;
    target = res.filePaths[0];
  }
  if (!existsSync(target)) {
    settings = { ...settings, recent: settings.recent.filter((p) => p !== target) };
    saveSettings(settings);
    return { ...backend.state, phase: 'error' as const, error: `Папката не съществува: ${target}` };
  }
  settings = pushRecent(settings, target);
  saveSettings(settings);
  return backend.start(target, settings.exe);
}

function registerIpc(): void {
  ipcMain.handle('state', () => backend.state);
  ipcMain.handle('settings', () => settings);
  ipcMain.handle('events', () => backend.events());
  ipcMain.handle('send', (_e, text: string) => backend.send(String(text)));
  ipcMain.handle('stop', () => backend.stop());
  ipcMain.handle('clear', () => backend.clear());
  ipcMain.handle('confirm', (_e, id: string, allow: boolean) => backend.confirm(String(id), !!allow));
  ipcMain.handle('command', (_e, name: string, arg: Record<string, unknown>) =>
    backend.command(String(name), arg && typeof arg === 'object' ? arg : {}));
  ipcMain.handle('open-workspace', (_e, path?: string) => openWorkspace(path));
  ipcMain.handle('forget-workspace', (_e, path: string) => {
    settings = { ...settings, recent: settings.recent.filter((p) => p !== path) };
    saveSettings(settings);
    return settings;
  });
  ipcMain.handle('restart', () =>
    backend.state.workspace ? backend.start(backend.state.workspace, settings.exe) : backend.state);
  ipcMain.handle('logs', () => backend.logText());
  ipcMain.handle('reveal', (_e, path: string) => { void shell.openPath(String(path)); });
  ipcMain.handle('open-external', (_e, url: string) => {
    if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
  });
  ipcMain.handle('set-sidebar', (_e, open: boolean) => {
    settings = { ...settings, sidebar: !!open };
    saveSettings(settings);
  });
  // The terminal version in the same folder: for /model, /update, /history…
  ipcMain.handle('open-terminal', async () => {
    const exe = await findExe(settings.exe);
    const cwd = backend.state.workspace || join(genesisHome(), 'workspace');
    if (!existsSync(cwd)) mkdirSync(cwd, { recursive: true });
    const cmd = exe ? `"${exe}"` : 'genesis';
    spawn('cmd.exe', ['/c', 'start', '"Genesis"', 'wt.exe', '-d', cwd, 'cmd', '/k', cmd], {
      cwd, detached: true, windowsHide: true,
    }).on('error', () => {
      spawn('cmd.exe', ['/c', 'start', 'cmd', '/k', cmd], { cwd, detached: true });
    });
  });
}

// End-to-end check without clicking: GENESIS_DESKTOP_AUTOSEND="task" sends it once the agent is up.
const autosend = process.env.GENESIS_DESKTOP_AUTOSEND;
if (autosend) {
  const onReady = (s: { phase: string }) => {
    if (s.phase !== 'ready') return;
    backend.off('state', onReady);
    void backend.send(autosend).then((r) => console.log('[autosend]', JSON.stringify(r)));
  };
  backend.on('state', onReady);
}

app.whenReady().then(async () => {
  registerIpc();
  createWindow();
  backend.pidFile = join(app.getPath('userData'), 'agent.json');
  await reapOrphan(backend.pidFile);
  if (settings.lastWorkspace && existsSync(settings.lastWorkspace)) {
    void backend.start(settings.lastWorkspace, settings.exe);
  } else {
    void findExe(settings.exe).then((exe) => {
      if (!exe) backend.emit('state', (backend.state = { ...backend.state, phase: 'missing' }));
    });
  }
});

app.on('window-all-closed', () => {
  void backend.shutdown().finally(() => app.quit());
});

app.on('before-quit', () => { void backend.shutdown(); });
