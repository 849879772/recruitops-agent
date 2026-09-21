import { contextBridge, ipcRenderer } from 'electron';
contextBridge.exposeInMainWorld('desktop', Object.freeze({
  command: (command: unknown) => ipcRenderer.invoke('desktop:command', command),
  state: () => ipcRenderer.invoke('desktop:state'),
  onState: (listener: (state: unknown) => void) => {
    const handler = (_event: unknown, state: unknown) => listener(state);
    ipcRenderer.on('desktop:state', handler);
    return () => ipcRenderer.removeListener('desktop:state', handler);
  }
}));
