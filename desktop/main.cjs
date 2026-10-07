'use strict';
// Some development launchers set this globally. Never pass it to our controller or SDK children.
delete process.env.ELECTRON_RUN_AS_NODE;
const { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } = require('electron');
const { spawn } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const { once } = require('node:events');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const readline = require('node:readline');
const { setTimeout: delay } = require('node:timers/promises');
const rules = require('./shell.cjs');

let window, controller, origin, config, applicationSession;
let ready = false, quitApproved = false, drainConfirmed = false, quitting = false, dirtyDraft = false, startupFailure = false;
let stderr = '';
const viewers = new Set();

function focusWindow() {
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function showError(title, message) {
  focusWindow();
  return dialog.showMessageBox(window, { type: 'error', title, message, buttons: ['Keep open'], noLink: true });
}

function safeMessage(value) {
  let message = String(value);
  for (const key of [config?.session_key, config?.owner_key]) if (key) message = message.replaceAll(key, '[redacted]');
  return message.slice(-5000);
}

function ownerRequest(route, body, timeout = 5000) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : JSON.stringify(body);
    const request = http.request(origin + route, { method: payload ? 'POST' : 'GET',
      headers: { 'X-ASHAD-Owner': config.owner_key, ...(payload ? {
        'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {}) },
    }, response => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        text += chunk;
        if (text.length > 65536) request.destroy(new Error('Controller response exceeded its desktop protocol limit.'));
      });
      response.on('error', reject);
      response.on('end', () => {
        try {
          const value = JSON.parse(text);
          if (response.statusCode !== 200) throw new Error(`Controller refused ${route}: ${JSON.stringify(value)}`);
          resolve(value);
        } catch (error) { reject(error); }
      });
    });
    request.on('error', reject);
    if (timeout) request.setTimeout(timeout, () => request.destroy(new Error('Owned controller did not respond. No shutdown was assumed.')));
    request.end(payload);
  });
}

function launchController() {
  const resources = app.isPackaged ? process.resourcesPath : path.join(__dirname, 'runtime');
  const python = path.join(resources, 'python', 'python.exe');
  if (!fs.existsSync(python)) throw new Error('Private Python runtime is missing. Rebuild with npm run prepare:runtime, or reinstall the complete ASHAD package. Global Python is not used.');
  const dataRoot = path.join(app.getPath('userData'), 'controller');
  fs.mkdirSync(dataRoot, { recursive: true });
  config = rules.launchConfig(dataRoot);
  controller = spawn(python, ['-I', '-B', '-X', 'utf8', '-m', 'ashad', '--desktop'], {
    cwd: resources, env: rules.childEnvironment(process.env), windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  controller.stderr.setEncoding('utf8');
  controller.stderr.on('data', chunk => { stderr = safeMessage(stderr + chunk).slice(-5000); });
  // The per-launch keys travel through this private pipe, never command line/environment/logs.
  controller.stdin.on('error', () => {});
  controller.stdin.end(JSON.stringify(config) + '\n');
  controller.on('exit', (code, signal) => {
    if (ready && !quitApproved && !drainConfirmed && !quitting) { // The quit handshake reports its own failures.
      void showError('ASHAD controller disconnected',
        `The owned controller exited (${signal || code}). Retained records may now be stale. No bot was resumed and no replacement controller was adopted.\n\n` +
        'Use the existing watchdog/recovery procedure before restarting. A viewer closing does not hand back control.');
    }
  });
  return new Promise((resolve, reject) => {
    const lines = readline.createInterface({ input: controller.stdout, crlfDelay: Infinity });
    const timeout = setTimeout(() => finish(new Error('Controller startup did not announce readiness. Check its data lock/runtime resources; no process was killed or adopted.')), 45000);
    function finish(error, value) {
      clearTimeout(timeout);
      lines.removeListener('line', onLine);
      controller.removeListener('error', onError);
      controller.removeListener('exit', onExit);
      lines.close();
      controller.stdout.resume(); // Drain subsequent logs; never block the child on a full pipe.
      error ? reject(error) : resolve(value);
    }
    function onLine(line) {
      try { finish(null, rules.announcement(line, config.instance_id)); }
      catch (error) { finish(error); }
    }
    function onError(error) { finish(error); }
    function onExit(code) { finish(new Error(`Private controller exited during startup (${code}). ${stderr}`)); }
    lines.on('line', onLine);
    controller.once('error', onError);
    controller.once('exit', onExit);
  });
}

async function waitUntilReady() {
  const deadline = Date.now() + 60000;
  let lastError;
  while (Date.now() < deadline) {
    if (!controllerRunning()) throw new Error(`Private controller exited during readiness. ${stderr}`);
    let identity;
    try { identity = await ownerRequest('/_desktop/ready'); } catch (error) { lastError = error; }
    if (identity) {
      // A mismatched authenticated identity is not an invitation to adopt/retry another process.
      if (!rules.readyIdentity(identity, config.instance_id)) throw new Error('Owned controller identity/protocol mismatch; no generic health endpoint was trusted.');
      return;
    }
    await delay(150);
  }
  throw new Error(`Controller was not ready: ${lastError?.message || 'no authenticated response'}. ${stderr}`);
}

function controllerRunning() {
  return Boolean(controller?.pid) && controller.exitCode === null && !controller.signalCode;
}

async function confirmed(title, message, action) {
  focusWindow();
  const { response } = await dialog.showMessageBox(window, { type: 'warning', title, message,
    detail: dirtyDraft ? 'The unsent draft will be discarded.' : '', buttons: [action, 'Keep open'], defaultId: 1, cancelId: 1, noLink: true });
  return response === 0;
}

function approveQuit() {
  quitApproved = true;
  for (const viewer of viewers) if (!viewer.isDestroyed()) viewer.close();
  app.quit();
  return true;
}

async function requestQuit() {
  if (quitApproved) return true;
  if (quitting) return false;
  quitting = true;
  try {
    if (!controllerRunning()) {
      // Nothing left to drain: missing resources, failed launch, crash, or unclean exit after drain.
      if (!ready) return approveQuit();
      return await confirmed('Quit ASHAD?', `The owned controller is no longer running (${controller.signalCode || controller.exitCode}). ` +
        'Retained run records may be stale and were not reconciled. The independent watchdog stays running for recovery.',
      'Quit anyway') && approveQuit();
    }
    if (!ready) {
      if (!startupFailure) throw new Error('The private controller is still starting. Wait for it to become ready or for startup to fail, then quit.');
      // It never passed authenticated readiness: the app never loaded it, so it accepted no work.
      if (!await confirmed('Quit ASHAD?', 'The private controller never became ready and accepted no work. Terminate it and quit? ' +
        'Existing records, locks and any watchdog are left for recovery.', 'Terminate and quit')) return false;
      if (controllerRunning()) {
        const exited = once(controller, 'exit');
        controller.kill();
        await exited;
      }
      return approveQuit();
    }
    const drained = await rules.confirmDrain(() => ownerRequest('/_desktop/status'), dirtyDraft,
      async prompt => (await dialog.showMessageBox(window, prompt)).response,
      body => ownerRequest('/_desktop/shutdown', body, 0));
    // No force timeout: teardown, cancellation and resource drain are the backend's authority.
    if (!drained) return false;
    drainConfirmed = true;
    // A confirmed HTTP drain is required AND the owned process must actually exit.
    if (controller.exitCode === null && !controller.signalCode) {
      await new Promise(resolve => controller.once('exit', resolve));
    }
    if (!rules.cleanControllerExit(controller.exitCode, controller.signalCode)) {
      throw new Error(`Controller acknowledged drain but exited uncleanly (${controller.signalCode || controller.exitCode}). Resource cleanup was not confirmed; no successful quit was reported.`);
    }
    return approveQuit();
  } catch (error) {
    await showError('ASHAD could not safely quit', safeMessage(error.message) +
      '\n\nThe application stays open. Review the listed owning runs and recovery/reconciliation controls; retry only after confirmed teardown.');
    return false;
  } finally { quitting = false; }
}

async function chooseBuild(kind) {
  if (!['manifest', 'folder'].includes(kind)) throw new Error('Build chooser kind must be manifest or folder.');
  const result = await dialog.showOpenDialog(window, { title: kind === 'folder' ? 'Choose a supported build folder' : 'Choose manifest.json',
    properties: [kind === 'folder' ? 'openDirectory' : 'openFile'],
    ...(kind === 'manifest' ? { filters: [{ name: 'Build manifest (manifest.json)', extensions: ['json'] }] } : {}),
  });
  return rules.selectedManifest(result.canceled ? null : result.filePaths[0], kind);
}

function registerBridge() {
  function requireSender(event) {
    if (!rules.privilegedSender(event, window?.webContents, origin) || !ready) throw new Error('Desktop bridge is available only to the owned application main frame.');
  }
  ipcMain.handle('ashad:choose-build', (event, kind) => { requireSender(event); return chooseBuild(kind); });
  ipcMain.handle('ashad:set-dirty-draft', (event, value) => {
    requireSender(event);
    if (typeof value !== 'boolean') throw new Error('Draft dirty state must be a boolean.');
    dirtyDraft = value;
  });
  ipcMain.handle('ashad:request-quit', (event, ...args) => {
    requireSender(event);
    if (args.length) throw new Error('Quit accepts no arguments.');
    return requestQuit();
  });
}

async function openViewer(url) {
  const status = rules.validatedStatus(await ownerRequest('/_desktop/status'));
  if (!rules.viewerUrl(url, status.viewer_urls, origin)) throw new Error('Desktop URL is not approved by the owned controller configuration.');
  const isolated = session.fromPartition(`ashad-viewer-${randomUUID()}`);
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  const viewer = new BrowserWindow({ width: 1200, height: 800, title: 'ASHAD human desktop — explicit handback required',
    webPreferences: { session: isolated, contextIsolation: true, sandbox: true, nodeIntegration: false, webviewTag: false },
  });
  viewers.add(viewer);
  viewer.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  viewer.webContents.on('will-navigate', (event, target) => { if (!rules.viewerUrl(target, [url], origin)) event.preventDefault(); });
  viewer.webContents.on('will-redirect', (event, target) => { if (!rules.viewerUrl(target, [url], origin)) event.preventDefault(); });
  viewer.webContents.on('will-attach-webview', event => event.preventDefault());
  viewer.on('closed', () => {
    viewers.delete(viewer);
    void isolated.clearStorageData();
    // Navigation/closing is not takeover_finish and never resumes bots.
  });
  await viewer.loadURL(url);
}

async function openApprovedLink(url) {
  try {
    if (rules.loginUrl(url)) await shell.openExternal(url);
    else await openViewer(url);
  } catch (error) { await showError('Link blocked', safeMessage(error.message)); }
}

// Desktop mode disables the renderer's beforeunload, so a dirty draft is confirmed here.
async function reloadView() {
  if (dirtyDraft && !await confirmed('Reload ASHAD?', 'Reload the view?', 'Discard draft and reload')) return;
  dirtyDraft = false;
  window.webContents.reload();
}

function createWindow() {
  applicationSession = session.fromPartition(`ashad-app-${randomUUID()}`); // In-memory, isolated from viewer/auth contexts.
  applicationSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  applicationSession.setPermissionCheckHandler(() => false);
  applicationSession.webRequest.onBeforeSendHeaders((details, callback) => callback(rules.desktopHeaders(details, origin, config?.session_key)));
  // The application session never loads remote pages/resources, even on HTTP redirect.
  applicationSession.webRequest.onBeforeRequest((details, callback) => {
    const localStatus = details.url.startsWith('file:') && !origin;
    callback({ cancel: !localStatus && !rules.appUrl(details.url, origin) });
  });
  window = new BrowserWindow({ width: 1440, height: 960, minWidth: 800, minHeight: 600, title: 'ASHAD', show: false,
    backgroundColor: '#141414', titleBarStyle: 'hidden', titleBarOverlay: { color: '#1a1a1a', symbolColor: '#a8a8a8', height: 40 },
    webPreferences: { session: applicationSession, preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true, sandbox: true, nodeIntegration: false, webviewTag: false },
  });
  window.on('ready-to-show', () => window.show());
  window.on('close', event => { if (!quitApproved) { event.preventDefault(); void requestQuit(); } });
  window.webContents.setWindowOpenHandler(({ url }) => { void openApprovedLink(url); return { action: 'deny' }; });
  window.webContents.on('will-navigate', (event, url) => {
    if (!rules.appUrl(url, origin)) { event.preventDefault(); void openApprovedLink(url); }
  });
  window.webContents.on('will-redirect', (event, url) => { if (!rules.appUrl(url, origin)) event.preventDefault(); });
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.on('will-prevent-unload', event => {
    // A dirty browser beforeunload may not veto an already confirmed, drained quit.
    if (quitApproved) event.preventDefault();
  });
  window.webContents.on('did-start-navigation', details => {
    // A new main-frame document has no draft; only same-document (hash) changes keep it.
    if (details.isMainFrame && !details.isSameDocument) dirtyDraft = false;
  });
  window.webContents.on('render-process-gone', () => {
    dirtyDraft = false;
    // Only recreate the view. The owned controller is never relaunched here.
    void showError('ASHAD view disconnected', 'The renderer exited. Use View → Reload to reconnect to the same controller. No game was started or resumed.');
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'File', submenu: [{ label: 'Quit', accelerator: 'Alt+F4', click: () => { void requestQuit(); } }] },
    { label: 'View', submenu: [{ label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => { void reloadView(); } }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
  ]));
  return window.loadFile(path.join(__dirname, 'status.html'));
}

app.setName('ASHAD');
let diagnosticError;
try {
  if (app.commandLine.hasSwitch('user-data-dir')) {
    // Explicit diagnostic isolation only: no alternate backend, auth bypass or data import.
    const dataRoot = rules.diagnosticDataRoot(app.commandLine.getSwitchValue('user-data-dir'));
    fs.mkdirSync(dataRoot, { recursive: true });
    app.setPath('userData', dataRoot);
    app.setPath('sessionData', dataRoot);
  }
} catch (error) { diagnosticError = error; }
if (diagnosticError) {
  dialog.showErrorBox('ASHAD data directory is unavailable', safeMessage(diagnosticError.message));
  app.exit(1);
} else if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', focusWindow);
  app.on('before-quit', event => { if (!quitApproved) { event.preventDefault(); void requestQuit(); } });
  app.on('window-all-closed', () => { if (quitApproved) app.quit(); });
  app.whenReady().then(async () => {
    registerBridge();
    await createWindow();
    try {
      origin = await launchController();
      await waitUntilReady();
      ready = true;
      await window.loadURL(origin + '/');
    } catch (error) {
      startupFailure = true;
      await showError('ASHAD startup failed', safeMessage(error.message) +
        '\n\nCheck package resources, the writable controller data directory and existing controller/watchdog locks. Do not delete live locks or stop another instance. No development data was imported.');
    }
  }).catch(error => { startupFailure = true; void showError('ASHAD shell failed', safeMessage(error.message)); });
}
