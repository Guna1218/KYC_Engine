const { app, BrowserWindow, ipcMain, clipboard, nativeImage, shell, dialog } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const http = require('http')
const fs = require('fs')

const PORT = 7523
let pyProcess = null
let mainWindow = null

// ── Resolve Python sidecar path ──────────────────────────────────────────────
function getPythonExe() {
  if (app.isPackaged) {
    const bin = process.platform === 'win32' ? 'kyc_server.exe' : 'kyc_server'
    return path.join(process.resourcesPath, 'python', 'kyc_server', bin)
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

function getServerScript() {
  if (app.isPackaged) return null
  return path.join(__dirname, 'python', 'server.py')
}

// ── Spawn Python FastAPI server ───────────────────────────────────────────────
function startPython() {
  const exe = getPythonExe()
  const script = getServerScript()
  const modelDir = path.join(app.isPackaged ? process.resourcesPath : __dirname, 'model')

  // Guard: show friendly error if binary is missing in packaged mode
  if (app.isPackaged && !fs.existsSync(exe)) {
    dialog.showErrorBox(
      'KYC FORMATTER — Build Step Missing',
      'Python server binary not found.\n\n' +
      'Expected at:\n' + exe + '\n\n' +
      'Fix:\n' +
      '1. Run  scripts\\build_python.bat  to freeze the Python server\n' +
      '2. Then run  npm run dist:win  again to repackage'
    )
    app.quit()
    return
  }

  const args = script ? [script] : []
  const env = { ...process.env, KYC_PORT: String(PORT), KYC_MODEL_DIR: modelDir }

  console.log('[main] Starting Python:', exe, args.join(' '))
  pyProcess = spawn(exe, args, { env, stdio: ['ignore', 'pipe', 'pipe'] })
  pyProcess.stdout.on('data', d => console.log('[python]', d.toString().trim()))
  pyProcess.stderr.on('data', d => console.error('[python:err]', d.toString().trim()))
  pyProcess.on('exit', code => console.log('[python] exited with code', code))
}

// ── Poll until server is ready ────────────────────────────────────────────────
function waitForServer(retries = 60, delay = 1000) {
  return new Promise((resolve, reject) => {
    let attempts = 0
    const check = () => {
      const req = http.get(`http://127.0.0.1:${PORT}/api/health`, res => {
        if (res.statusCode === 200) return resolve()
        retry()
      })
      req.on('error', retry)
      req.setTimeout(400, () => { req.destroy(); retry() })
    }
    const retry = () => {
      if (++attempts >= retries) return reject(new Error('Python server did not start'))
      setTimeout(check, delay)
    }
    check()
  })
}

// ── Create main window ────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1050,
    height: 750,
    minWidth: 800,
    minHeight: 500,
    backgroundColor: '#080808',
    icon: path.join(__dirname, 'assets', 'icon.ico'),
    title: 'KYC FORMATTER',
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
    autoHideMenuBar: true,
  })
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'))
  mainWindow.on('closed', () => { mainWindow = null })
}

// ── IPC handlers ──────────────────────────────────────────────────────────────
ipcMain.handle('copy-text', async (_e, text) => {
  try { clipboard.writeText(text); return { ok: true } } catch(e) { return { ok: false, error: e.message } }
})
ipcMain.handle('copy-image', async (_e, b64png) => {
  try { clipboard.writeImage(nativeImage.createFromBuffer(Buffer.from(b64png,'base64'))); return { ok: true } } catch(e) { return { ok: false, error: e.message } }
})
ipcMain.handle('open-url', async (_e, url) => { shell.openExternal(url); return { ok: true } })
ipcMain.handle('get-port', () => PORT)

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  startPython()
  try { await waitForServer(); console.log('[main] Python server ready on port', PORT) }
  catch(e) { console.error('[main] Server failed to start:', e.message) }
  createWindow()
})

app.on('window-all-closed', () => {
  if (pyProcess) { pyProcess.kill(); pyProcess = null }
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})