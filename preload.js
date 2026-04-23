const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('kyc', {
  // Clipboard
  copyText:  (text)   => ipcRenderer.invoke('copy-text', text),
  copyImage: (b64png) => ipcRenderer.invoke('copy-image', b64png),
  // Shell
  openUrl:   (url)    => ipcRenderer.invoke('open-url', url),
  // Server port
  getPort:   ()       => ipcRenderer.invoke('get-port'),
})
