import { contextBridge, ipcRenderer } from 'electron';
let pending: unknown;
let listener: ((draft: unknown) => void) | undefined;
let changedListener: ((event: unknown) => void) | undefined;
ipcRenderer.on('desktop:application-draft', (_event, draft: unknown) => {
  if (listener) listener(draft); else pending = draft;
});
ipcRenderer.on('desktop:data-changed', (_event, value: unknown) => { changedListener?.(value); });
contextBridge.exposeInMainWorld('recruitopsDesktop', Object.freeze({
  onApplicationDraft(callback: (draft: unknown) => void) {
    if (typeof callback !== 'function') throw new Error('Invalid callback');
    listener = callback;
    if (pending !== undefined) { const draft = pending; pending = undefined; callback(draft); }
    return () => { if (listener === callback) listener = undefined; };
  },
  onDataChanged(callback: (event: unknown) => void) {
    if (typeof callback !== 'function') throw new Error('Invalid callback');
    changedListener = callback;
    return () => { if (changedListener === callback) changedListener = undefined; };
  },
  applySavedConfiguration() { return ipcRenderer.invoke('desktop:apply-saved-configuration'); }
}));
