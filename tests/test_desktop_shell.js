'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const rules = require('../desktop/shell.cjs');
const { PYTHON_SHA256, PYTHON_VERSION } = require('../desktop/prepare.cjs');
const origin = 'http://127.0.0.1:32123';
const idle = () => ({ owners: [], operations: [], draining: false, viewer_urls: [] });

// Tests are pure main/preload boundary checks: no Electron, Python, credentials,
// controller, existing runtime data, network, workers or provider are started.
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

test('startup trusts exact private announcement and authenticated ready identity, never health', () => {
  const config = rules.launchConfig('fixture');
  assert.equal(rules.announcement(JSON.stringify({ protocol: rules.PROTOCOL, instance_id: config.instance_id,
    port: 32123 }), config.instance_id), origin);
  for (const value of [{ status: 'ok' }, { protocol: 'other', instance_id: config.instance_id, port: 32123 },
    { protocol: rules.PROTOCOL, instance_id: 'another owner', port: 32123 },
    { protocol: rules.PROTOCOL, instance_id: config.instance_id, port: 0 },
    { protocol: rules.PROTOCOL, instance_id: config.instance_id, port: '32123' }]) {
    assert.throws(() => rules.announcement(JSON.stringify(value), config.instance_id));
    assert.equal(rules.readyIdentity(value, config.instance_id) && value.protocol !== rules.PROTOCOL, false);
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
      assert.deepEqual(body, { stop_runs: Boolean(status.owners.length), cancel_operations: Boolean(status.operations.length) });
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
  const pins = (await fs.readFile(path.join(__dirname, '../desktop/python-requirements.lock'), 'utf8')).trim().split('\n');
  assert.equal(pins.length, 49);
  assert.ok(pins.every(line => /^[A-Za-z0-9_.-]+==[0-9.]+$/.test(line)));
});
