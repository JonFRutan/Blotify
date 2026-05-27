'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Expose a minimal, safe API to the renderer (web page).
// All server comms still go through fetch/WebSocket to the FastAPI backend.
contextBridge.exposeInMainWorld('blotify', {
  /** Open a native folder in the OS file manager */
  openFolder: (path) => ipcRenderer.invoke('open-folder', path),
  /** Current platform string: 'linux' | 'darwin' | 'win32' */
  platform: () => ipcRenderer.invoke('platform'),
  /** True when running inside Electron (vs. plain browser) */
  isElectron: true,
});
