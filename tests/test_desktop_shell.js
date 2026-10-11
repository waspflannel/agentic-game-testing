'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const rules = require('../desktop/shell.cjs');
const { PYTHON_SHA256, PYTHON_VERSION, LINUX_PYTHON_SHA256 } = require('../desktop/prepare.cjs');
const origin = 'http://127.0.0.1:32123';
const idle = () => ({ owners: [], operations: [], draining: false, viewer_urls: [] });

// Tests are main/preload boundary checks with stubbed Electron and a loopback fixture: no real Electron, Python, credentials,
// controller, existing runtime data, external network, workers or provider are started.
test('diagnostic user-data option requires an explicit absolute path, no silent checkout fallback', () => {
  assert.equal(rules.diagnosticDataRoot(path.resolve('desktop', 'output', 'fixture with spaces')),
    path.resolve('desktop', 'output', 'fixture with spaces'));
  for (const value of ['', 'relative-data', null, true]) assert.throws(() => rules.diagnosticDataRoot(value), /explicit absolute/);
});

test('launch config has independent 256-bit secrets, UUID and explicit data root', () => {
  const first = rules.launchConfig('C:\\fixture with spaces\\controller');
  const second = rules.launchConfig('other');
  assert.deepEqual(Object.keys(first).sort(), ['data_root', 'instance_id', 'owner_key', 'session_key']);
  for (const key of ['session_key', 'owner_key']) {
    assert.match(first[key], /^[A-Za-z0-9_-]{43,}$/);
    assert.notEqual(first[key], second[key]);
  }
  assert.notEqual(first.session_key, first.owner_key);
  assert.match(first.instance_id, /^[0-9a-f-]{36}$/);
  assert.equal(first.data_root, 'C:\\fixture with spaces\\controller');
});

test('child environment excludes inherited runtime hooks, API credentials and Electron Node mode', () => {
  const env = rules.childEnvironment({ PATH: 'system path', SystemRoot: 'C:\\Windows', APPDATA: 'user',
    PYTHONPATH: 'host', PYTHONHOME: 'host', ashad_data: 'live', CODEX_HOME: 'personal',
    OPENAI_API_KEY: 'secret', ANTHROPIC_API_KEY: 'secret', CUSTOM_API_TOKEN: 'secret',
    ELECTRON_RUN_AS_NODE: '1', NODE_OPTIONS: '--require bad', HTTP_PROXY: 'proxy' });
  assert.deepEqual(env, { PATH: 'system path', SystemRoot: 'C:\\Windows', APPDATA: 'user' });
});

test('linux child environment keeps session, keyring and display variables only', () => {
  const input = { PATH: '/usr/bin', HOME: '/home/u', DBUS_SESSION_BUS_ADDRESS: 'unix:path=/run/bus', DISPLAY: ':0',
    XAUTHORITY: '/home/u/.Xauthority', XDG_RUNTIME_DIR: '/run/user/1000', PYTHONPATH: 'host', HTTP_PROXY: 'proxy',
    OPENAI_API_KEY: 'secret', ELECTRON_RUN_AS_NODE: '1' };
  assert.deepEqual(rules.childEnvironment(input, 'linux'), { PATH: '/usr/bin', HOME: '/home/u',
    DBUS_SESSION_BUS_ADDRESS: 'unix:path=/run/bus', DISPLAY: ':0', XAUTHORITY: '/home/u/.Xauthority',
    XDG_RUNTIME_DIR: '/run/user/1000' });
  assert.deepEqual(rules.childEnvironment(input, 'win32'), { PATH: '/usr/bin' });
});

test('python layout is platform specific', () => {
  assert.deepEqual(rules.pythonLayout('win32'), { executable: 'python.exe', sitePackages: path.join('Lib', 'site-packages') });
  assert.deepEqual(rules.pythonLayout('linux'), { executable: path.join('bin', 'python3'),
    sitePackages: path.join('lib', 'python3.13', 'site-packages') });
});

test('startup trusts exact private announcement and authenticated ready identity, never health', () => {
  const config = rules.launchConfig('fixture');
  assert.equal(rules.announcement(JSON.stringify({ protocol: rules.PROTOCOL, instance_id: config.instance_id,
    port: 32123 }), config.instance_id), origin);
  // [rejected announcement, whether its identity alone would pass authenticated readiness]
  for (const [value, identity] of [[{ status: 'ok' }, false],
    [{ protocol: 'other', instance_id: config.instance_id, port: 32123 }, false],
    [{ protocol: rules.PROTOCOL, instance_id: 'another owner', port: 32123 }, false],
    [{ protocol: rules.PROTOCOL, instance_id: config.instance_id, port: 0 }, true],
    [{ protocol: rules.PROTOCOL, instance_id: config.instance_id, port: '32123' }, true]]) {
    assert.throws(() => rules.announcement(JSON.stringify(value), config.instance_id));
    assert.equal(rules.readyIdentity(value, config.instance_id), identity);
  }
  assert.equal(rules.readyIdentity({ status: 'ok' }, config.instance_id), false);
  assert.equal(rules.readyIdentity({ protocol: rules.PROTOCOL, instance_id: config.instance_id }, config.instance_id), true);
  assert.equal(rules.readyIdentity({ protocol: rules.PROTOCOL, instance_id: 'wrong' }, config.instance_id), false);
});

test('a positive drain acknowledgement also requires a clean actual controller exit', () => {
  assert.equal(rules.cleanControllerExit(0, null), true);
  for (const [code, signal] of [[1, null], [null, 'SIGTERM'], [0, 'SIGTERM'], [null, null], [undefined, undefined]]) {
    assert.equal(rules.cleanControllerExit(code, signal), false);
  }
});

test('isolated app session injects only same-origin HTTP, including static and MJPEG; owner is blocked', () => {
  for (const suffix of ['/', '/api/bootstrap', '/static/app.js', '/api/runs/r/players/p/live?epoch=e']) {
    const result = rules.desktopHeaders({ url: origin + suffix,
      requestHeaders: { Accept: '*/*', 'x-ASHAD-desktop': 'forged', 'X-ASHAD-Owner': 'forged' } }, origin, 'private');
    assert.deepEqual(result, { cancel: false, requestHeaders: { Accept: '*/*', 'X-ASHAD-Desktop': 'private' } });
  }
  assert.deepEqual(rules.desktopHeaders({ url: origin + '/_desktop/shutdown', requestHeaders: {} }, origin, 'private'),
    { cancel: true, requestHeaders: {} });
  for (const url of ['https://auth.openai.com/', 'http://127.0.0.1:32124/', 'http://localhost:32123/',
    'http://127.0.0.1.evil.test:32123/', 'http://user@127.0.0.1:32123/']) {
    assert.equal(rules.appUrl(url, origin), false);
    assert.deepEqual(rules.desktopHeaders({ url, requestHeaders: { 'X-ASHAD-Desktop': 'private' } }, origin, 'private'),
      { cancel: false, requestHeaders: {} });
  }
});

test('privileged IPC requires exact webContents, main frame and current owned origin', () => {
  const frame = { url: origin + '/#/settings' };
  const contents = { mainFrame: frame };
  assert.equal(rules.privilegedSender({ sender: contents, senderFrame: frame }, contents, origin), true);
  assert.equal(rules.privilegedSender({ sender: {}, senderFrame: frame }, contents, origin), false);
  assert.equal(rules.privilegedSender({ sender: contents, senderFrame: { url: frame.url } }, contents, origin), false);
  frame.url = 'https://chatgpt.com/';
  assert.equal(rules.privilegedSender({ sender: contents, senderFrame: frame }, contents, origin), false);
});

test('external login uses the existing HTTPS host allowlist and viewer approval is exact', () => {
  for (const url of ['https://auth.openai.com/authorize?state=a', 'https://chatgpt.com/auth/login']) assert.equal(rules.loginUrl(url), true);
  for (const url of ['http://auth.openai.com/', 'https://auth.openai.com.evil.test/', 'https://openai.com/',
    'https://auth.openai.com:8443/', 'https://user@chatgpt.com/', 'file:///C:/game.exe']) assert.equal(rules.loginUrl(url), false);
  const viewer = 'http://127.0.0.1:6080/vnc.html';
  assert.equal(rules.viewerUrl(viewer, [viewer], origin), true);
  for (const url of [viewer + '?password=x', viewer + '#key=x', 'http://user@127.0.0.1:6080/vnc.html',
    'http://127.0.0.1:6080/other', origin + '/', 'file:///C:/viewer.html']) {
    assert.equal(rules.viewerUrl(url, [viewer, origin + '/'], origin), false);
  }
});

test('native chooser returns null on cancel; resolves existing manifest only, never imports it', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ashad shell fixture '));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  assert.equal(await rules.selectedManifest(null, 'folder'), null);
  const manifest = path.join(root, 'manifest.json');
  await assert.rejects(rules.selectedManifest(root, 'folder'));
  await fs.writeFile(manifest, 'not imported, parsed or executed');
  assert.equal(await rules.selectedManifest(root, 'folder'), await fs.realpath(manifest));
  assert.equal(await rules.selectedManifest(manifest, 'manifest'), await fs.realpath(manifest));
  await assert.rejects(rules.selectedManifest('relative/manifest.json', 'manifest'));
  await assert.rejects(rules.selectedManifest(root, 'manifest'));
  await fs.writeFile(path.join(root, 'other.json'), '{}');
  await assert.rejects(rules.selectedManifest(path.join(root, 'other.json'), 'manifest'));
  assert.equal(await fs.readFile(manifest, 'utf8'), 'not imported, parsed or executed');
});

test('status must account for all owners, operations, drain state and trusted viewer strings', () => {
  assert.deepEqual(rules.validatedStatus(idle()), idle());
  for (const value of [null, {}, { ...idle(), owners: [{ id: 'run' }] },
    { ...idle(), operations: [{ type: 'tune' }] }, { ...idle(), draining: 'false' },
    { ...idle(), viewer_urls: [42] }]) assert.throws(() => rules.validatedStatus(value));
});

test('cancel leaves every run and provider operation untouched; prompt lists all IDs and defaults Cancel', async () => {
  const status = { ...idle(), owners: [{ run_id: 'run-1', state: 'Paused', reason: 'human control' },
    { run_id: 'run-2', state: 'Ended', reason: 'teardown unconfirmed' }], operations: [{ kind: 'agent_tuning' }] };
  let shutdowns = 0;
  const result = await rules.confirmDrain(async () => status, true, async prompt => {
    assert.equal(prompt.defaultId, 1);
    assert.equal(prompt.cancelId, 1);
    assert.deepEqual(prompt.buttons, ['Stop/cancel and quit', 'Cancel']);
    assert.match(prompt.detail, /run-1/);
    assert.match(prompt.detail, /run-2/);
    assert.match(prompt.detail, /agent_tuning/);
    return 1;
  }, async () => { shutdowns++; });
  assert.equal(result, false);
  assert.equal(shutdowns, 0);
});

test('dirty-only quit prompts discard; idle quit still drains; work quits need positive confirmed drain', async () => {
  assert.equal(rules.quitPrompt(idle(), false), null);
  assert.deepEqual(rules.quitPrompt(idle(), true).buttons, ['Discard draft and quit', 'Cancel']);
  for (const status of [idle(), { ...idle(), owners: [{ run_id: 'run', state: 'Running', reason: 'active' }],
    operations: [{ kind: 'planning' }] }]) {
    assert.equal(await rules.confirmDrain(async () => status, false, async () => 0, async body => {
      assert.deepEqual(body, { stop_runs: Boolean(status.owners.length), cancel_operations: Boolean(status.operations.length),
        expected_run_ids: status.owners.map(owner => owner.run_id) });
      return { drained: true };
    }), true);
  }
  for (const drained of [false, undefined, 'true']) {
    await assert.rejects(rules.confirmDrain(async () => idle(), false, async () => 0, async () => ({ drained })), /did not confirm/);
  }
  await assert.rejects(rules.confirmDrain(async () => idle(), false, async () => 0,
    async () => { throw new Error('409 teardown unconfirmed'); }), /409 teardown unconfirmed/);
});

test('preload exposes exactly three validated methods only in the app main frame; no secrets', async () => {
  const source = await fs.readFile(path.join(__dirname, '../desktop/preload.cjs'), 'utf8');
  let bridge;
  const calls = [];
  const context = { process: { isMainFrame: true }, location: { protocol: 'http:', hostname: '127.0.0.1' },
    require: () => ({ contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'ashadDesktop'); bridge = value; } },
      ipcRenderer: { invoke: async (...args) => { calls.push(args); return null; } } }) };
  vm.runInNewContext(source, context);
  assert.deepEqual(Object.keys(bridge).sort(), ['chooseBuild', 'requestQuit', 'setDirtyDraft']);
  assert.equal(Object.isFrozen(bridge), true);
  await bridge.chooseBuild();
  await bridge.setDirtyDraft(true);
  await bridge.requestQuit();
  assert.deepEqual(calls, [['ashad:choose-build', 'manifest'], ['ashad:set-dirty-draft', true], ['ashad:request-quit']]);
  await assert.rejects(bridge.chooseBuild('arbitrary'));
  await assert.rejects(bridge.setDirtyDraft('yes'));
  for (const location of [{ protocol: 'https:', hostname: 'chatgpt.com' }, { protocol: 'file:', hostname: '' }]) {
    bridge = null;
    vm.runInNewContext(source, { ...context, location });
    assert.equal(bridge, null);
  }
  bridge = null;
  vm.runInNewContext(source, { ...context, process: { isMainFrame: false } });
  assert.equal(bridge, null);
});

test('packaging pins exact tooling/full Python and keeps subprocess resources outside ASAR with per-user NSIS', async () => {
  const packageFile = JSON.parse(await fs.readFile(path.join(__dirname, '../desktop/package.json'), 'utf8'));
  assert.deepEqual(packageFile.devDependencies, { electron: '44.6.0', 'electron-builder': '26.15.3' });
  assert.equal(packageFile.build.asar, true);
  const main = await fs.readFile(path.join(__dirname, '../desktop/main.cjs'), 'utf8');
  assert.match(main, /spawn\(python, \['-I', '-B', '-X', 'utf8', '-m'/);
  assert.equal(packageFile.build.nsis.perMachine, false);
  assert.equal(packageFile.build.nsis.allowElevation, false);
  assert.equal(packageFile.build.nsis.deleteAppDataOnUninstall, false);
  assert.ok(packageFile.build.extraResources.some(item => item.from === 'runtime/python' && item.to === 'python'));
  assert.equal(PYTHON_VERSION, '3.13.16');
  assert.equal(PYTHON_SHA256, 'bbf675bb5e763c1efbb09a3a461b259d81598a63c30c4b0d7ea11b9f063df159');
  const pins = (await fs.readFile(path.join(__dirname, '../desktop/python-requirements.lock'), 'utf8')).trim().split(/\r?\n/);
  assert.equal(pins.length, 45);
  assert.ok(pins.every(line => /^[A-Za-z0-9_.-]+==[0-9.]+(; sys_platform == "(win32|linux)")? ( ?--hash=sha256:[0-9a-f]{64})+$/.test(line)), 'every pin is hash-locked');
  const pin = name => pins.find(line => line.startsWith(`${name}==`));
  for (const name of ['pywin32', 'pywin32-ctypes']) assert.ok(pin(name).includes('sys_platform == "win32"'), name);
  for (const name of ['SecretStorage', 'jeepney']) assert.ok(pin(name).includes('sys_platform == "linux"'), name);
  const names = pins.map(line => line.split(/==|;/)[0].toLowerCase());
  for (const devOnly of ['pytest', 'pytest-asyncio', 'pluggy', 'iniconfig', 'pygments', 'colorama', 'pip']) assert.ok(!names.includes(devOnly), devOnly);
  const prepare = await fs.readFile(path.join(__dirname, '../desktop/prepare.cjs'), 'utf8');
  for (const flag of ["'--isolated'", "'--index-url', 'https://pypi.org/simple'", "'--require-hashes'", "'--no-deps'", "'--only-binary=:all:'"]) {
    assert.ok(prepare.includes(flag), flag);
  }
  // Build-time pip/ensurepip never ship; Electron itself cannot be repurposed as Node or a debug target.
  const python = packageFile.build.extraResources.find(item => item.from === 'runtime/python');
  for (const excluded of ['!Lib/ensurepip/**', '!Lib/site-packages/pip/**', '!Lib/site-packages/pip-*.dist-info/**',
    '!lib/python3.13/ensurepip/**', '!lib/python3.13/site-packages/pip/**', '!lib/python3.13/site-packages/pip-*.dist-info/**', '!bin/pip*']) {
    assert.ok(python.filter.includes(excluded), excluded);
  }
  assert.equal(packageFile.build.linux.executableName, 'ashad');
  assert.ok(packageFile.build.linux.target.some(item => item.target === 'deb'));
  assert.match(LINUX_PYTHON_SHA256, /^[0-9a-f]{64}$/);
  assert.deepEqual(packageFile.build.electronFuses, { runAsNode: false, enableNodeOptionsEnvironmentVariable: false,
    enableNodeCliInspectArguments: false });
});

test('packaging copies Git-tracked sources only, never ignored or untracked local files', async t => {
  const target = await fs.mkdtemp(path.join(os.tmpdir(), 'ashad tracked copy '));
  t.after(() => fs.rm(target, { recursive: true, force: true }));
  const { copyTracked } = require('../desktop/prepare.cjs');
  const tracked = execFileSync('git', ['ls-files', '-z', '--', 'worker'], { cwd: path.join(__dirname, '..'), encoding: 'utf8' })
    .split('\0').filter(Boolean).map(file => path.relative('worker', file)).sort();
  await copyTracked('worker', target);
  const copied = (await fs.readdir(target, { recursive: true, withFileTypes: true })).filter(entry => entry.isFile())
    .map(entry => path.relative(target, path.join(entry.parentPath, entry.name))).sort();
  assert.ok(tracked.includes('Dockerfile'));
  assert.deepEqual(copied, tracked);
  assert.ok(!copied.some(file => file.split(path.sep).includes('__pycache__')));
});

// Real main.cjs against stubbed Electron and a fake controller child. The only "controller"
// is a loopback fixture answering the owner protocol; no Python, data or worker is involved.
async function loadMain(t, { announce = 'valid', status = idle(), drainExit = 0 } = {}) {
  const Module = require('node:module');
  const { EventEmitter } = require('node:events');
  const { PassThrough } = require('node:stream');
  const http = require('node:http');
  const syncFs = require('node:fs');
  const mainPath = require.resolve('../desktop/main.cjs');
  const calls = { quit: 0, kills: 0, reloads: 0, dialogs: [], shutdowns: [] };
  const responses = [];
  let config, menu;
  const child = Object.assign(new EventEmitter(), { pid: 4242, exitCode: null, signalCode: null,
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    kill() { calls.kills++; setImmediate(() => exit(null, 'SIGTERM')); return true; } });
  function exit(code, signal) { Object.assign(child, { exitCode: code, signalCode: signal }); child.emit('exit', code, signal); }
  const server = http.createServer((request, response) => {
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      if (request.headers['x-ashad-owner'] !== config.owner_key) return response.writeHead(401).end('{}');
      const reply = value => response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(value));
      if (request.url === '/_desktop/ready') return reply({ protocol: rules.PROTOCOL, instance_id: config.instance_id });
      if (request.url === '/_desktop/status') return reply(status);
      calls.shutdowns.push(JSON.parse(body));
      reply({ drained: true });
      setImmediate(() => exit(drainExit, null));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); if (child.exitCode === null && !child.signalCode) exit(0, null); });
  child.stdin.on('data', chunk => {
    config = JSON.parse(chunk);
    const identity = announce === 'foreign' ? 'another owner' : config.instance_id;
    if (announce !== 'none') child.stdout.write(JSON.stringify({ protocol: rules.PROTOCOL, instance_id: identity, port: server.address().port }) + '\n');
  });
  const contents = Object.assign(new EventEmitter(), { mainFrame: { url: '' }, setWindowOpenHandler() {}, reload() { calls.reloads++; } });
  class BrowserWindow extends EventEmitter {
    constructor() { super(); this.webContents = contents; }
    isDestroyed() { return false; } isMinimized() { return false; } show() {} focus() {}
    loadFile() { return Promise.resolve(); }
    loadURL(url) { contents.mainFrame.url = url; return Promise.resolve(); }
  }
  const fakeSession = { setPermissionRequestHandler() {}, setPermissionCheckHandler() {},
    webRequest: { onBeforeSendHeaders() {}, onBeforeRequest() {} } };
  const handlers = new Map();
  const app = Object.assign(new EventEmitter(), { isPackaged: false, commandLine: { hasSwitch: () => false },
    setName() {}, requestSingleInstanceLock: () => true, whenReady: () => Promise.resolve(),
    getPath: () => path.join(os.tmpdir(), 'ashad never created'), quit() { calls.quit++; }, exit() {} });
  const electron = { app, BrowserWindow, Menu: { buildFromTemplate: template => template, setApplicationMenu: value => { menu = value; } },
    session: { fromPartition: () => fakeSession }, ipcMain: { handle: (name, handler) => handlers.set(name, handler) }, shell: {},
    dialog: { showMessageBox: async (_window, options) => {
      calls.dialogs.push(options);
      return { response: options.type === 'warning' ? responses.shift() ?? 1 : 0 };
    } } };
  const load = Module._load;
  Module._load = function (request, parent, ...rest) {
    if (parent?.filename === mainPath) {
      if (request === 'electron') return electron;
      if (request === 'node:child_process') return { spawn: () => child };
      if (request === 'node:fs') return { ...syncFs, existsSync: () => true, mkdirSync() {} };
    }
    return load.call(this, request, parent, ...rest);
  };
  try {
    delete require.cache[mainPath];
    require(mainPath);
  } finally { Module._load = load; }
  async function until(condition) {
    for (let i = 0; i < 400 && !condition(); i++) await new Promise(resolve => setTimeout(resolve, 5));
    assert.ok(condition(), 'condition not reached');
  }
  const ipc = (name, ...args) => handlers.get(name)({ sender: contents, senderFrame: contents.mainFrame }, ...args);
  async function closeWindow() { // A void quit path, e.g. the window close button before readiness.
    const seen = calls.dialogs.length;
    app.emit('before-quit', { preventDefault() {} });
    await until(() => calls.dialogs.length > seen);
    return calls.dialogs.at(-1);
  }
  const reload = () => menu[1].submenu[0].click();
  if (announce === 'valid') await until(() => contents.mainFrame.url.startsWith('http://127.0.0.1:'));
  else await until(() => config);
  return { app, calls, responses, exit, contents, ipc, closeWindow, reload, until };
}

test('crashed controller after readiness can be quit after a stale-records confirmation, exactly once', async t => {
  const main = await loadMain(t);
  main.exit(3, null);
  await main.until(() => main.calls.dialogs.some(item => item.title === 'ASHAD controller disconnected'));
  main.responses.push(1);
  assert.equal(await main.ipc('ashad:request-quit'), false);
  assert.match(main.calls.dialogs.at(-1).message, /no longer running \(3\).*stale.*watchdog/s);
  assert.equal(main.calls.quit, 0);
  main.responses.push(0);
  assert.equal(await main.ipc('ashad:request-quit'), true);
  assert.equal(await main.ipc('ashad:request-quit'), true);
  assert.equal(main.calls.quit, 1);
  assert.deepEqual(main.calls.shutdowns, []);
});

test('a live child still starting refuses quit; once startup fails and it exits, quit needs no drain', async t => {
  const main = await loadMain(t, { announce: 'none' });
  const refused = await main.closeWindow();
  assert.equal(refused.title, 'ASHAD could not safely quit');
  assert.match(refused.message, /still starting/);
  assert.equal(main.calls.kills, 0);
  main.exit(1, null);
  await main.until(() => main.calls.dialogs.some(item => item.title === 'ASHAD startup failed'));
  const seen = main.calls.dialogs.length;
  main.app.emit('before-quit', { preventDefault() {} });
  await main.until(() => main.calls.quit === 1);
  assert.equal(main.calls.dialogs.length, seen); // It never became ready, so no records were at risk.
  assert.equal(main.calls.kills, 0);
});

test('a never-ready child after startup failure is terminated only after confirmation', async t => {
  const main = await loadMain(t, { announce: 'foreign' });
  await main.until(() => main.calls.dialogs.some(item => item.title === 'ASHAD startup failed'));
  main.responses.push(1);
  assert.match((await main.closeWindow()).message, /never became ready and accepted no work/);
  assert.equal(main.calls.kills, 0);
  assert.equal(main.calls.quit, 0);
  main.responses.push(0);
  await main.closeWindow();
  await main.until(() => main.calls.quit === 1);
  assert.equal(main.calls.kills, 1);
});

test('concurrent quit requests drain once; an unclean exit after drain then allows a confirmed quit', async t => {
  const clean = await loadMain(t);
  assert.deepEqual(await Promise.all([clean.ipc('ashad:request-quit'), clean.ipc('ashad:request-quit')]), [true, false]);
  assert.equal(clean.calls.quit, 1);
  assert.deepEqual(clean.calls.shutdowns, [{ stop_runs: false, cancel_operations: false, expected_run_ids: [] }]);
  const unclean = await loadMain(t, { drainExit: 1 });
  assert.equal(await unclean.ipc('ashad:request-quit'), false);
  assert.match(unclean.calls.dialogs.at(-1).message, /exited uncleanly/);
  assert.equal(unclean.calls.quit, 0);
  unclean.responses.push(0);
  assert.equal(await unclean.ipc('ashad:request-quit'), true);
  assert.match(unclean.calls.dialogs.at(-1).message, /no longer running \(1\)/);
  assert.equal(unclean.calls.quit, 1);
  assert.equal(unclean.calls.shutdowns.length, 1);
});

test('dirty draft prompts before View reload and resets on main-frame navigation or renderer loss', async t => {
  const main = await loadMain(t);
  await main.ipc('ashad:set-dirty-draft', true);
  main.contents.emit('did-start-navigation', { isMainFrame: true, isSameDocument: true });
  main.contents.emit('did-start-navigation', { isMainFrame: false, isSameDocument: false });
  main.responses.push(1);
  main.reload();
  await main.until(() => main.calls.dialogs.some(item => item.title === 'Reload ASHAD?'));
  assert.equal(main.calls.reloads, 0);
  main.contents.emit('did-start-navigation', { isMainFrame: true, isSameDocument: false });
  main.reload();
  await main.until(() => main.calls.reloads === 1);
  await main.ipc('ashad:set-dirty-draft', true);
  main.contents.emit('render-process-gone', {}, { reason: 'crashed' });
  main.reload();
  await main.until(() => main.calls.reloads === 2);
  assert.equal(main.calls.dialogs.filter(item => item.title === 'Reload ASHAD?').length, 1);
  // A reset draft no longer produces a discard prompt on quit.
  assert.equal(await main.ipc('ashad:request-quit'), true);
  assert.ok(!main.calls.dialogs.some(item => item.title === 'Quit ASHAD?'));
});

test('launchConfig includes checkout_root only when given', () => {
  assert.equal('checkout_root' in rules.launchConfig('data'), false);
  assert.equal('checkout_root' in rules.launchConfig('data', null), false);
  assert.equal(rules.launchConfig('data', '/work/checkout').checkout_root, '/work/checkout');
});

test('validatedStatus accepts restart_for_fix null or a loop id and rejects others', () => {
  for (const value of [undefined, null, 'loop_ab-12_X']) {
    assert.doesNotThrow(() => rules.validatedStatus({ ...idle(), restart_for_fix: value }));
  }
  for (const value of ['', 'loop_', 'run_1', 'loop_a b', `loop_${'a'.repeat(101)}`, 7, true, {}]) {
    assert.throws(() => rules.validatedStatus({ ...idle(), restart_for_fix: value }));
  }
});

test("fixPrUrl allows only the fixed repo's pull URLs", () => {
  assert.equal(rules.fixPrUrl('https://github.com/waspflannel/agentic-game-testing/pull/41'), true);
  for (const url of ['https://github.com/other/agentic-game-testing/pull/41',
    'http://github.com/waspflannel/agentic-game-testing/pull/41',
    'https://user@github.com/waspflannel/agentic-game-testing/pull/41',
    'https://github.com:8443/waspflannel/agentic-game-testing/pull/41',
    'https://github.com/waspflannel/agentic-game-testing/pull/41?x=1',
    'https://github.com/waspflannel/agentic-game-testing/pull/41#c',
    'https://github.com/waspflannel/agentic-game-testing/pull/1/files',
    'https://github.com/waspflannel/agentic-game-testing/pull/abc', null, 42]) assert.equal(rules.fixPrUrl(url), false);
});
