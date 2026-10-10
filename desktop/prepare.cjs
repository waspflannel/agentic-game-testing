'use strict';
const { createHash } = require('node:crypto');
const { execFile, spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const { createReadStream, createWriteStream } = require('node:fs');
const path = require('node:path');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const { promisify } = require('node:util');
const { childEnvironment, pythonLayout } = require('./shell.cjs');

const PYTHON_VERSION = '3.13.16';
const PYTHON_URL = `https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.zip`;
const PYTHON_SHA256 = 'bbf675bb5e763c1efbb09a3a461b259d81598a63c30c4b0d7ea11b9f063df159';
const LINUX_PYTHON_URL = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261009/cpython-3.13.16%2B20261009-x86_64-unknown-linux-gnu-install_only.tar.gz';
const LINUX_PYTHON_SHA256 = '1cca1041f693f110a2e0d16417090b8996258c7a121e90eeab66c2f40ea407f2';
const root = path.resolve(__dirname, '..');
const runtime = path.join(__dirname, 'runtime');
const pythonRoot = path.join(runtime, 'python');
const partialPython = path.join(runtime, 'python.partial');
const archive = process.platform === 'win32'
  ? { url: PYTHON_URL, sha256: PYTHON_SHA256, file: 'python.zip', hosts: ['www.python.org'] }
  : { url: LINUX_PYTHON_URL, sha256: LINUX_PYTHON_SHA256, file: 'python.tar.gz',
      hosts: ['github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'] };
const layout = pythonLayout(process.platform);
const python = path.join(pythonRoot, layout.executable);
const site = path.join(pythonRoot, layout.sitePackages);
const lock = path.join(__dirname, 'python-requirements.lock');
const cache = path.resolve(process.env.ASHAD_DESKTOP_CACHE || path.join(root, '.artifacts', 'desktop packaging p0'));
const environment = childEnvironment(process.env);

async function exists(file) { try { await fs.access(file); return true; } catch { return false; } }
async function sha256(file) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}
function run(executable, args, env = environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { cwd: runtime, env, stdio: 'inherit', windowsHide: true });
    child.once('error', reject);
    child.once('exit', code => code === 0 ? resolve() : reject(new Error(`${path.basename(executable)} failed (${code}).`)));
  });
}
// Destructive replacement is restricted to these generated resource directories.
async function removeGenerated(directory) {
  const resolved = path.resolve(directory);
  if (![pythonRoot, partialPython, site, path.join(site, 'ashad'), path.join(site, 'integration'),
    path.join(site, 'ashad_resources')].includes(resolved)) {
    throw new Error(`Refusing to remove unexpected path: ${resolved}`);
  }
  await fs.rm(resolved, { recursive: true, force: true });
}
// Only Git-tracked sources ship; ignored/untracked local files (keys, data, scratch) never do.
async function copyTracked(source, destination) {
  let stdout;
  try {
    ({ stdout } = await promisify(execFile)('git', ['ls-files', '-z', '--', source],
      { cwd: root, env: environment, maxBuffer: 16 << 20, windowsHide: true }));
  } catch (error) {
    throw new Error(`git ls-files failed (${error.message.trim()}). Packaging copies tracked files only and requires Git on PATH.`);
  }
  const files = stdout.split('\0').filter(Boolean);
  if (!files.length) throw new Error(`No tracked files under ${source}; refusing to package an empty resource.`);
  for (const file of files) {
    const target = path.join(destination, path.relative(source, file));
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.copyFile(path.join(root, file), target);
  }
}

async function prepare() {
  if (!['win32', 'linux'].includes(process.platform) || process.arch !== 'x64') throw new Error('Packaging supports Windows x64 and Linux x64 only.');
  await fs.mkdir(runtime, { recursive: true });
  const zip = path.join(runtime, archive.file);
  const cached = path.join(cache, archive.file);
  if (!await exists(zip)) {
    if (await exists(cached)) await fs.copyFile(cached, zip);
    else {
      const response = await fetch(archive.url);
      if (!response.ok || new URL(response.url).protocol !== 'https:' || !archive.hosts.includes(new URL(response.url).hostname)) throw new Error('Official Python runtime download failed.');
      await pipeline(Readable.fromWeb(response.body), createWriteStream(zip, { flags: 'wx' }));
    }
  }
  if (await sha256(zip) !== archive.sha256) throw new Error(`Private Python archive SHA256 mismatch. Remove only desktop/runtime/${archive.file} and retry the official download.`);
  const marker = path.join(runtime, 'prepared.json');
  let prior = {};
  if (await exists(marker)) prior = JSON.parse(await fs.readFile(marker, 'utf8'));
  if (prior.python_sha256 !== archive.sha256 || !await exists(python)) {
    // Extract beside the target and rename into place: an interrupted extraction never looks complete.
    await fs.rm(marker, { force: true });
    prior = {};
    await removeGenerated(partialPython);
    if (process.platform === 'win32') {
      const powershell = path.join(environment.SystemRoot || environment.SYSTEMROOT || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
      await run(powershell, ['-NoProfile', '-NonInteractive', '-Command',
        'Expand-Archive -LiteralPath $env:ASHAD_BUILD_ZIP -DestinationPath $env:ASHAD_BUILD_PYTHON -Force'],
        { ...environment, ASHAD_BUILD_ZIP: zip, ASHAD_BUILD_PYTHON: partialPython });
    } else {
      await fs.mkdir(partialPython, { recursive: true });
      await run('/usr/bin/tar', ['-xzf', zip, '-C', partialPython, '--strip-components=1']);
    }
    await removeGenerated(pythonRoot);
    await fs.rename(partialPython, pythonRoot);
  }
  const lockHash = await sha256(lock);
  if (prior.requirements_sha256 !== lockHash) {
    // A changed lock starts from an empty site, never an upgrade over stale distributions.
    await fs.rm(marker, { force: true });
    await removeGenerated(site);
    await fs.mkdir(site, { recursive: true });
    // Full official distribution supports normal site initialization/native keyring,
    // unlike the embeddable _pth runtime. pip/ensurepip are build-time only: the
    // package.json extraResources filter excludes them from the packaged runtime.
    await run(python, ['-I', '-B', '-m', 'ensurepip']);
    // --isolated ignores pip.ini/PIP_* index settings; every wheel must match its locked hash.
    await run(python, ['-I', '-B', '-m', 'pip', 'install', '--isolated', '--index-url', 'https://pypi.org/simple',
      '--require-hashes', '--disable-pip-version-check', '--no-deps', '--only-binary=:all:', '--no-compile',
      '--target', site, '--cache-dir', path.join(cache, 'pip-cache'), '-r', lock]);
    await fs.writeFile(marker, JSON.stringify({ python_version: PYTHON_VERSION, python_sha256: archive.sha256,
      requirements_sha256: lockHash }, null, 2) + '\n');
  }
  await removeGenerated(path.join(site, 'ashad'));
  await copyTracked('ashad', path.join(site, 'ashad'));
  await removeGenerated(path.join(site, 'integration'));
  await copyTracked('integration/fixtures.py', path.join(site, 'integration', 'fixtures.py'));
  const resources = path.join(site, 'ashad_resources');
  await removeGenerated(resources);
  await copyTracked('worker', path.join(resources, 'worker'));
  await copyTracked('scripts/provision-workers.ps1', path.join(resources, 'scripts', 'provision-workers.ps1'));
  const keyringCheck = process.platform === 'win32'
    ? 'from keyring.backends.Windows import WinVaultKeyring; assert WinVaultKeyring.priority > 0; '
    : 'import secretstorage, jeepney; from keyring.backends.SecretService import Keyring; ';
  // Check every resolved pin and real subprocess resource without login/provider/game activity.
  await run(python, ['-I', '-B', '-c',
    'import sys; from pathlib import Path; from importlib.metadata import version; ' +
    `assert sys.version.split()[0] == "${PYTHON_VERSION}"; ` +
    'from packaging.requirements import Requirement; ' +
    'reqs=[Requirement(line.split(" --hash")[0]) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]; ' +
    'pins=[(r.name, next(iter(r.specifier)).version) for r in reqs if r.marker is None or r.marker.evaluate()]; ' +
    'assert all(version(name)==expected for name,expected in pins); ' +
    'import ashad.__main__, ashad.watchdog, ashad.app, integration.fixtures; ' +
    keyringCheck +
    'from codex_cli_bin import bundled_codex_path; assert bundled_codex_path().is_file(); ' +
    'assert (Path(ashad.app.__file__).parent/"static"/"index.html").is_file(); ' +
    'resources=Path(ashad.app.__file__).parents[1]/"ashad_resources"; ' +
    'assert (resources/"worker"/"Dockerfile").is_file() and (resources/"worker"/"prepare_probe.py").is_file(); ' +
    'assert (resources/"scripts"/"provision-workers.ps1").is_file(); ' +
    'print(f"Prepared private Python, {len(pins)} hash-locked pins, keyring import, Codex resources and controller/static resources.")', lock]);
}

if (require.main === module) prepare().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { PYTHON_VERSION, PYTHON_URL, PYTHON_SHA256, LINUX_PYTHON_URL, LINUX_PYTHON_SHA256, prepare, copyTracked };
