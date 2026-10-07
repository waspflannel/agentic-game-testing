'use strict';
const { randomBytes, randomUUID } = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');

const PROTOCOL = 'ashad-desktop/1';

// Preserve OS discovery/keyring necessities, not Python hooks, provider credentials,
// development overrides, proxy injection or ELECTRON_RUN_AS_NODE.
function childEnvironment(inherited) {
  const allowed = new Set(('SYSTEMROOT SYSTEMDRIVE WINDIR COMSPEC PATH PATHEXT TEMP TMP APPDATA LOCALAPPDATA ' +
    'USERPROFILE HOMEDRIVE HOMEPATH USERNAME COMPUTERNAME PROGRAMFILES PROGRAMFILES(X86) PROGRAMW6432 ' +
    'PROGRAMDATA ALLUSERSPROFILE OS NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE').split(' '));
  return Object.fromEntries(Object.entries(inherited).filter(([key]) => allowed.has(key.toUpperCase())));
}

function diagnosticDataRoot(value) {
  if (typeof value !== 'string' || !value || !path.isAbsolute(value)) {
    throw new Error('--user-data-dir requires an explicit absolute writable directory. No default data was changed.');
  }
  return path.resolve(value);
}

function launchConfig(dataRoot) {
  return { data_root: dataRoot, session_key: randomBytes(32).toString('base64url'),
    owner_key: randomBytes(32).toString('base64url'), instance_id: randomUUID() };
}

function announcement(line, instanceId) {
  const value = JSON.parse(line);
  if (value.protocol !== PROTOCOL || value.instance_id !== instanceId ||
      !Number.isInteger(value.port) || value.port < 1 || value.port > 65535) {
    throw new Error('Controller announced an unexpected desktop identity or port. No other controller was adopted.');
  }
  return `http://127.0.0.1:${value.port}`;
}

function readyIdentity(value, instanceId) {
  return value?.protocol === PROTOCOL && value.instance_id === instanceId;
}

function cleanControllerExit(code, signal) {
  return code === 0 && !signal;
}

function parsedUrl(value) {
  try { return typeof value === 'string' ? new URL(value) : null; } catch { return null; }
}

function appUrl(value, origin) {
  const url = parsedUrl(value);
  return Boolean(url && origin && url.origin === origin && !url.username && !url.password);
}

function loginUrl(value) {
  const url = parsedUrl(value);
  return Boolean(url && url.protocol === 'https:' && !url.username && !url.password && !url.port &&
    ['auth.openai.com', 'chatgpt.com'].includes(url.hostname));
}

function viewerUrl(value, configured, appOrigin) {
  const url = parsedUrl(value);
  return Boolean(url && ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password &&
    !url.search && !url.hash && url.origin !== appOrigin && configured.some(item => {
      const approved = parsedUrl(item);
      return approved && approved.href === url.href;
    }));
}

function privilegedSender(event, contents, origin) {
  return Boolean(contents && event.sender === contents && event.senderFrame === contents.mainFrame &&
    appUrl(event.senderFrame?.url, origin));
}

function desktopHeaders(details, origin, key) {
  const headers = Object.fromEntries(Object.entries(details.requestHeaders || {}).filter(([name]) =>
    !['x-ashad-desktop', 'x-ashad-owner'].includes(name.toLowerCase())));
  const own = appUrl(details.url, origin);
  const administrative = own && new URL(details.url).pathname.startsWith('/_desktop/');
  if (own && !administrative) headers['X-ASHAD-Desktop'] = key;
  return { cancel: Boolean(administrative), requestHeaders: headers };
}

function validatedStatus(value) {
  if (!value || !Array.isArray(value.owners) || !Array.isArray(value.operations) ||
      typeof value.draining !== 'boolean' || !Array.isArray(value.viewer_urls) ||
      !value.viewer_urls.every(item => typeof item === 'string') ||
      !value.owners.every(item => item && typeof item.run_id === 'string' &&
        typeof item.state === 'string' && typeof item.reason === 'string') ||
      !value.operations.every(item => item && typeof item.kind === 'string')) {
    throw new Error('Controller returned invalid shutdown status; keep the application open and recover ownership.');
  }
  return value;
}

function quitPrompt(status, dirty) {
  const work = status.owners.length || status.operations.length;
  if (!work && !dirty) return null;
  const detail = [
    ...status.owners.map(owner => `${owner.run_id}: ${owner.state} — ${owner.reason}`),
    ...status.operations.map(operation => `In flight: ${operation.kind}`),
    ...(dirty ? ['The unsent draft will be discarded.'] : []),
    ...(work ? ['Quit waits for confirmed teardown and cancellation. Closing a viewer does not hand back control.'] : []),
  ].join('\n');
  return { type: 'warning', title: 'Quit ASHAD?', message: work ? 'Stop/cancel work and quit?' : 'Discard the unsent draft?',
    detail, buttons: [work ? 'Stop/cancel and quit' : 'Discard draft and quit', 'Cancel'], defaultId: 1, cancelId: 1, noLink: true };
}

async function confirmDrain(getStatus, dirty, confirm, shutdown) {
  const status = validatedStatus(await getStatus());
  const prompt = quitPrompt(status, dirty);
  if (prompt && await confirm(prompt) !== 0) return false;
  const result = await shutdown({ stop_runs: status.owners.length > 0,
    cancel_operations: status.operations.length > 0, expected_run_ids: status.owners.map(owner => owner.run_id) });
  if (result?.drained !== true) throw new Error('Controller did not confirm a completed admission/ownership/operation drain.');
  return true;
}

async function selectedManifest(selection, kind) {
  if (selection == null) return null;
  if (!['manifest', 'folder'].includes(kind) || typeof selection !== 'string' || !path.isAbsolute(selection)) {
    throw new Error('Select a manifest.json file or build folder using the native chooser.');
  }
  const selected = await fs.stat(selection);
  if (kind === 'folder' ? !selected.isDirectory() : !selected.isFile()) throw new Error('The selected build path has disappeared.');
  const manifest = kind === 'folder' ? path.join(selection, 'manifest.json') : selection;
  if (path.basename(manifest).toLowerCase() !== 'manifest.json' || !(await fs.stat(manifest)).isFile()) {
    throw new Error('The selected build must contain manifest.json. Selection does not import or launch it.');
  }
  return fs.realpath(manifest);
}

module.exports = { PROTOCOL, childEnvironment, diagnosticDataRoot, launchConfig, announcement, readyIdentity, cleanControllerExit, appUrl, loginUrl,
  viewerUrl, privilegedSender, desktopHeaders, validatedStatus, quitPrompt, confirmDrain, selectedManifest };
