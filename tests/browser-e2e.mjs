import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const adminPassword = 'e2e-test-administrator-password';
const cameraName = 'E2E fake camera';
const python = process.env.PYTHON || 'python';

async function unusedPort() {
  const server = createServer();
  await new Promise((resolveListen, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolveListen);
  });
  const { port } = server.address();
  await new Promise((resolveClose, reject) => server.close((error) => error ? reject(error) : resolveClose()));
  return port;
}

function initializeDatabase(dbPath) {
  const setup = spawnSync(python, ['-c', [
    'import os',
    'from homecam.database import Database',
    "db = Database(os.environ['HOMECAM_DB'])",
    'db.open()',
    `db.set_admin_password(${JSON.stringify(adminPassword)})`,
    'db.close()',
  ].join('; ')], {
    cwd: repoRoot,
    env: { ...process.env, HOMECAM_DB: dbPath },
    encoding: 'utf8',
    timeout: 30_000,
  });
  if (setup.error || setup.status !== 0) {
    throw new Error(`Could not initialize the E2E database: ${setup.error?.message || setup.stderr || setup.stdout}`);
  }
}

async function waitForBackend(url, child, getLogs) {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Backend exited before becoming ready.\n${getLogs()}`);
    try {
      const response = await fetch(`${url}/healthz`, { signal: AbortSignal.timeout(1_000) });
      if (response.ok) return;
    } catch { /* The server is still starting. */ }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 150));
  }
  throw new Error(`Backend did not become ready.\n${getLogs()}`);
}

async function main() {
  const tempDir = await mkdtemp(resolve(tmpdir(), 'homecam-e2e-'));
  const dbPath = resolve(tempDir, 'homecam.db');
  const port = await unusedPort();
  const origin = `http://127.0.0.1:${port}`;
  let backend;
  let browser;
  let backendLogs = '';
  const appendLog = (chunk) => {
    backendLogs = `${backendLogs}${chunk}`.slice(-12_000);
  };

  try {
    initializeDatabase(dbPath);
    backend = spawn(python, ['-m', 'homecam'], {
      cwd: repoRoot,
      env: {
        ...process.env,
        HOMECAM_DEV: '1',
        HOMECAM_DB: dbPath,
        HOMECAM_HOST: '127.0.0.1',
        HOMECAM_PORT: String(port),
        HOMECAM_ORIGIN: origin,
        HOMECAM_TURN_SECRET: 'e2e-turn-secret-which-is-at-least-32-bytes',
        HOMECAM_TURN_URLS: '',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    backend.stdout.setEncoding('utf8').on('data', appendLog);
    backend.stderr.setEncoding('utf8').on('data', appendLog);
    await waitForBackend(origin, backend, () => backendLogs);

    browser = await chromium.launch({
      headless: true,
      args: [
        '--use-fake-device-for-media-stream',
        '--use-fake-ui-for-media-stream',
        '--autoplay-policy=no-user-gesture-required',
        '--disable-features=WebRtcHideLocalIpsWithMdns',
      ],
    });
    const viewerContext = await browser.newContext({ serviceWorkers: 'block' });
    const cameraContext = await browser.newContext({ permissions: ['camera'], serviceWorkers: 'block' });
    await cameraContext.addInitScript(() => {
      window.__homecamStreams = [];
      const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async (...args) => {
        const stream = await originalGetUserMedia(...args);
        window.__homecamStreams.push(stream);
        return stream;
      };
      window.__homecamSockets = [];
      const OriginalWebSocket = window.WebSocket;
      window.WebSocket = new Proxy(OriginalWebSocket, {
        construct(target, args) {
          const socket = Reflect.construct(target, args, target);
          window.__homecamSockets.push(socket);
          return socket;
        },
      });
    });

    const viewer = await viewerContext.newPage();
    const camera = await cameraContext.newPage();
    for (const [role, page] of [['viewer', viewer], ['camera', camera]]) {
      page.on('pageerror', (error) => { throw new Error(`${role} page error: ${error.message}`); });
    }

    await viewer.goto(`${origin}/#/viewer`);
    await viewer.locator('#login-password').fill(adminPassword);
    await viewer.locator('#login-form button[type="submit"]').click();
    await viewer.locator('#viewer-dashboard').waitFor({ state: 'visible' });
    await viewer.locator('#pairing-name').fill(cameraName);
    await viewer.locator('#pairing-form button[type="submit"]').click();
    await viewer.locator('#pairing-result').waitFor({ state: 'visible' });
    const pairingCode = (await viewer.locator('#pairing-code').textContent()).trim();
    assert.match(pairingCode, /^[A-Z0-9]{8}$/);

    await camera.goto(`${origin}/#/camera`);
    await camera.locator('#enroll-code').fill(pairingCode);
    await camera.locator('#enroll-form button[type="submit"]').click();
    await camera.locator('#camera-console').waitFor({ state: 'visible' });
    await camera.locator('#camera-start').click();
    await camera.waitForFunction(() => window.__homecamStreams?.length === 1, null, { timeout: 10_000 });

    const watchButton = viewer.getByRole('button', { name: `查看：${cameraName}` });
    await watchButton.waitFor({ state: 'visible', timeout: 15_000 });
    await watchButton.click();
    await viewer.waitForFunction(() => {
      const video = document.querySelector('#viewer-video');
      const frames = video?.getVideoPlaybackQuality?.().totalVideoFrames || 0;
      return video?.videoWidth > 0 && video?.videoHeight > 0 && frames > 0;
    }, null, { timeout: 45_000 });
    const decoded = await viewer.locator('#viewer-video').evaluate((video) => ({
      width: video.videoWidth,
      height: video.videoHeight,
      frames: video.getVideoPlaybackQuality().totalVideoFrames,
    }));
    assert.ok(decoded.width > 0 && decoded.height > 0 && decoded.frames > 0, 'viewer should decode actual fake-camera frames');

    let confirmText = '';
    viewer.once('dialog', async (dialog) => {
      confirmText = dialog.message();
      await dialog.accept();
    });
    await viewer.getByRole('button', { name: `吊销并移除：${cameraName}` }).click();
    assert.match(confirmText, /确定吊销并移除/);
    await camera.waitForFunction(() => {
      const streams = window.__homecamStreams || [];
      const sockets = window.__homecamSockets || [];
      const tracksEnded = streams.length === 1 && streams[0].getTracks().length > 0
        && streams[0].getTracks().every((track) => track.readyState === 'ended');
      const previewReleased = document.querySelector('#camera-video')?.srcObject === null;
      const socketClosed = sockets.length > 0 && sockets.every((socket) => socket.readyState === WebSocket.CLOSED);
      return tracksEnded && previewReleased && socketClosed;
    }, null, { timeout: 15_000 });
    await camera.locator('#camera-enroll').waitFor({ state: 'visible' });
    await viewer.waitForFunction(() => document.querySelectorAll('#devices-list .device-card').length === 0);

    console.log(`Browser E2E passed: paired camera, decoded ${decoded.width}x${decoded.height} frames, and revocation closed capture and WebSocket.`);
  } finally {
    await browser?.close().catch(() => {});
    if (backend && backend.exitCode === null) {
      backend.kill('SIGTERM');
      await Promise.race([once(backend, 'exit'), new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000))]);
      if (backend.exitCode === null) backend.kill('SIGKILL');
    }
    await rm(tempDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
