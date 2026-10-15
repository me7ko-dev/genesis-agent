import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron';
import type { BackendState, EventsBatch, GenesisApi } from '../shared/types';

function listen<T>(channel: string, cb: (v: T) => void): () => void {
  const handler = (_e: IpcRendererEvent, v: T) => cb(v);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

const api: GenesisApi = {
  state: () => ipcRenderer.invoke('state'),
  settings: () => ipcRenderer.invoke('settings'),
  events: () => ipcRenderer.invoke('events'),
  send: (text, images) => ipcRenderer.invoke('send', text, images ?? []),
  transcribe: (mime, data) => ipcRenderer.invoke('transcribe', mime, data),
  speak: (text) => ipcRenderer.invoke('speak', text),
  setSpeak: (on) => ipcRenderer.invoke('set-speak', on),
  stop: () => ipcRenderer.invoke('stop'),
  clear: () => ipcRenderer.invoke('clear'),
  confirm: (id, allow, session) => ipcRenderer.invoke('confirm', id, allow, session === true),
  command: (name, arg) => ipcRenderer.invoke('command', name, arg ?? {}),
  openWorkspace: (path) => ipcRenderer.invoke('open-workspace', path),
  forgetWorkspace: (path) => ipcRenderer.invoke('forget-workspace', path),
  restart: () => ipcRenderer.invoke('restart'),
  logs: () => ipcRenderer.invoke('logs'),
  reveal: (path) => ipcRenderer.invoke('reveal', path),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  openTerminal: () => ipcRenderer.invoke('open-terminal'),
  setSidebar: (open) => ipcRenderer.invoke('set-sidebar', open),
  onState: (cb) => listen<BackendState>('state', cb),
  onEvents: (cb) => listen<EventsBatch>('events', cb),
  onLog: (cb) => listen<string>('log', cb),
};

contextBridge.exposeInMainWorld('genesis', api);
