'use strict';

const { app, BrowserWindow, shell, dialog, Menu, ipcMain } = require('electron');
const { spawn } = require('child_process');
const net  = require('net');
const http = require('http');
const path = require('path');

const IS_DEV = !app.isPackaged;
const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';
const ROOT   = path.join(__dirname, '..');  // project root (dev) / resources root (prod)

let mainWindow   = null;
let serverProcess = null;
let SERVER_PORT  = 0;

// ── Port discovery ──────────────────────────────────────────────────────────

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

// ── Wait until FastAPI is accepting connections ─────────────────────────────

function waitForServer(port, timeoutMs = 40_000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const attempt = () => {
      const req = http.get(`http://127.0.0.1:${port}/`, res => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() > deadline) return reject(new Error('Server did not start within 40 s'));
        setTimeout(attempt, 400);
      });
      req.setTimeout(800, () => req.destroy());
    };
    setTimeout(attempt, 300); // give spawn a head start
  });
}

// ── Binary resolution ───────────────────────────────────────────────────────

function binDir() {
  return path.join(process.resourcesPath, 'bin', process.platform);
}

function serverBin() {
  // PyInstaller --onedir produces a server/ subdirectory containing the exe.
  // Structure: bin/{platform}/server/server[.exe]
  return path.join(binDir(), 'server', IS_WIN ? 'server.exe' : 'server');
}

function spotdlBin() {
  return path.join(binDir(), IS_WIN ? 'spotdl.exe' : 'spotdl');
}

function ffmpegBin() {
  try {
    // ffmpeg-static resolves to the correct platform binary.
    // In packaged app this is inside app.asar.unpacked.
    return require('ffmpeg-static');
  } catch {
    return IS_WIN ? 'ffmpeg.exe' : 'ffmpeg';
  }
}

// ── Start Python server ─────────────────────────────────────────────────────

async function startServer(port) {
  const env = {
    ...process.env,
    PORT: String(port),
    BLOTIFY_FFMPEG: ffmpegBin() ?? '',
  };

  let cmd, args, opts;

  if (IS_DEV) {
    // Development: delegate to uv so the venv is used automatically
    cmd  = 'uv';
    args = ['run', 'python', 'main.py'];
    opts = { cwd: ROOT, env };
  } else {
    // Production: use the PyInstaller-compiled server binary
    env.SPOTDL_BIN = spotdlBin();
    cmd  = serverBin();
    args = [];
    opts = { env };
  }

  serverProcess = spawn(cmd, args, {
    ...opts,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  serverProcess.stdout?.on('data', d => process.stdout.write('[server] ' + d));
  serverProcess.stderr?.on('data', d => process.stderr.write('[server] ' + d));
  serverProcess.on('exit', code => {
    if (code !== 0 && code !== null)
      console.error(`[server] exited with code ${code}`);
  });

  await waitForServer(port);
}

// ── Browser window ──────────────────────────────────────────────────────────

async function createWindow(port) {
  mainWindow = new BrowserWindow({
    width:    1120,
    height:   760,
    minWidth: 820,
    minHeight: 560,
    title: 'Blotify',
    backgroundColor: '#0d1117',
    // Uncomment after adding assets/icon.png:
    // icon: path.join(ROOT, 'assets', 'icon.png'),
    titleBarStyle: IS_MAC ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // External links → OS browser, not a new Electron window
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });

  await mainWindow.loadURL(`http://127.0.0.1:${port}`);

  if (IS_DEV) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

// ── Application menu ────────────────────────────────────────────────────────

function buildMenu() {
  const template = [
    ...(IS_MAC ? [{
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    }] : []),
    {
      label: 'File',
      submenu: [
        {
          label: 'Open Downloads Folder',
          accelerator: IS_MAC ? 'Cmd+Shift+O' : 'Ctrl+Shift+O',
          click: openDownloadsFolder,
        },
        { type: 'separator' },
        IS_MAC ? { role: 'close' } : { role: 'quit' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        ...(IS_DEV ? [{ role: 'toggleDevTools' }, { type: 'separator' }] : []),
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        ...(IS_MAC
          ? [{ type: 'separator' }, { role: 'front' }]
          : [{ role: 'close' }]),
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function openDownloadsFolder() {
  try {
    const res = await fetch(`http://127.0.0.1:${SERVER_PORT}/api/output-dir`);
    const { dir } = await res.json();
    if (dir) shell.openPath(dir);
  } catch { /* server not ready */ }
}

// ── IPC ─────────────────────────────────────────────────────────────────────

ipcMain.handle('open-folder', (_, folderPath) => shell.openPath(folderPath));
ipcMain.handle('platform', () => process.platform);

// ── App lifecycle ───────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  SERVER_PORT = await getFreePort();

  try {
    await startServer(SERVER_PORT);
  } catch (err) {
    dialog.showErrorBox(
      'Blotify — startup error',
      `Could not start the download server:\n\n${err.message}`,
    );
    app.quit();
    return;
  }

  buildMenu();
  await createWindow(SERVER_PORT);

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0)
      await createWindow(SERVER_PORT);
  });
});

app.on('window-all-closed', () => {
  if (!IS_MAC) app.quit();
});

app.on('before-quit', () => {
  if (serverProcess) {
    serverProcess.kill('SIGTERM');
    serverProcess = null;
  }
});
