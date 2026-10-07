'use strict';
const { contextBridge, ipcRenderer } = require('electron');

// No bridge on local startup pages, remote authentication or isolated viewers.
if (process.isMainFrame && location.protocol === 'http:' && location.hostname === '127.0.0.1') {
  contextBridge.exposeInMainWorld('ashadDesktop', Object.freeze({
    chooseBuild: (kind = 'manifest') => {
      if (!['manifest', 'folder'].includes(kind)) return Promise.reject(new Error('Choose manifest or folder.'));
      return ipcRenderer.invoke('ashad:choose-build', kind);
    },
    setDirtyDraft: value => {
      if (typeof value !== 'boolean') return Promise.reject(new Error('Draft dirty state must be boolean.'));
      return ipcRenderer.invoke('ashad:set-dirty-draft', value);
    },
    requestQuit: () => ipcRenderer.invoke('ashad:request-quit'),
  }));
}
