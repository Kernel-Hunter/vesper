// The page gets native pickers, explicit folder/window actions and dropped-file paths;
// it gets no general Node.js or Electron access.

import { contextBridge, ipcRenderer, webUtils } from 'electron'

contextBridge.exposeInMainWorld('vesperDesktop', {
  pickFolder: (): Promise<string | null> => ipcRenderer.invoke('vesper:pick-folder'),
  pickFiles: (): Promise<string[]> => ipcRenderer.invoke('vesper:pick-files'),
  openSessionWindow: (id: string): Promise<boolean> => ipcRenderer.invoke('vesper:open-session-window', id),
  openFolder: (path: string, target: 'finder' | 'vscode'): Promise<boolean> => ipcRenderer.invoke('vesper:open-folder', path, target),
  pathForFile: (file: File): string => webUtils.getPathForFile(file),
})
