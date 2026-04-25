/**
 * bud — Electron main process
 * Spawns the Python FastAPI server, then opens a BrowserWindow pointed at it.
 */

const { app, BrowserWindow, shell, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

const PORT    = 8082;
const DEV_URL = `http://127.0.0.1:${PORT}`;

let mainWindow = null;
let tray       = null;
let pyProcess  = null;

// ── Start Python backend ──────────────────────────────────────────────────────
function startPython() {
  const appDir = app.isPackaged
    ? path.join(process.resourcesPath, 'app')
    : path.join(__dirname, '..');

  const python = process.platform === 'win32' ? 'python' : 'python3';
  pyProcess = spawn(python, ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd:   appDir,
    stdio: 'pipe',
    env:   { ...process.env, PORT: String(PORT) },
  });

  pyProcess.stdout.on('data', d => console.log('[py]', d.toString().trim()));
  pyProcess.stderr.on('data', d => console.error('[py]', d.toString().trim()));
  pyProcess.on('exit', code => console.log('[py] exited with code', code));
}

// ── Wait for server ready ─────────────────────────────────────────────────────
function waitForServer(retries = 30) {
  return new Promise((resolve, reject) => {
    const check = () => {
      http.get(DEV_URL, res => {
        res.resume();
        resolve();
      }).on('error', () => {
        if (--retries <= 0) return reject(new Error('Server did not start'));
        setTimeout(check, 500);
      });
    };
    check();
  });
}

// ── Create window ─────────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width:           1280,
    height:          820,
    minWidth:        900,
    minHeight:       600,
    title:           'bud',
    backgroundColor: '#0e0c0a',
    webPreferences:  { nodeIntegration: false, contextIsolation: true },
    show: false,
  });

  mainWindow.loadURL(DEV_URL);
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Open external links in browser, not Electron
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Minimise to tray instead of closing
  mainWindow.on('close', e => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

// ── System tray ───────────────────────────────────────────────────────────────
function createTray() {
  const iconPath = path.join(__dirname, '..', 'static', 'logo.png');
  const icon = nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 });
  tray = new Tray(icon);
  tray.setToolTip('bud — Personal AI Agent');
  const menu = Menu.buildFromTemplate([
    { label: 'Open bud',  click: () => { mainWindow.show(); mainWindow.focus(); } },
    { type: 'separator' },
    { label: 'Quit',      click: () => { app.isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on('double-click', () => { mainWindow.show(); mainWindow.focus(); });
}

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  startPython();
  try {
    await waitForServer();
  } catch(e) {
    console.error('Could not connect to Python server:', e.message);
  }
  createWindow();
  createTray();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('will-quit', () => {
  if (pyProcess) pyProcess.kill();
});

app.on('activate', () => {
  if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
});
