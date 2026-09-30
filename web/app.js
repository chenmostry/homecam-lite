import { enqueueRemoteIce, flushRemoteIce } from './ice-queue.mjs';

const $ = (selector) => document.querySelector(selector);
const byId = (id) => document.getElementById(id);
const STORAGE = Object.freeze({ token: 'homecam.device_token', id: 'homecam.device_id', name: 'homecam.device_name' });
const REFRESH_MS = 45 * 60 * 1000;

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

const app = {
  route: 'viewer',
  authenticated: false,
  viewerSocket: null,
  viewerReady: false,
  viewerIntentionalClose: false,
  viewerRetry: 0,
  viewerReconnectTimer: null,
  viewerPeerId: null,
  viewerWatchWanted: false,
  viewerDeviceId: null,
  viewerSession: null,
  viewerStatsTimer: null,
  viewerRefreshTimer: null,
  viewerStatsPrevious: null,
  viewerRemoteAudio: false,
  devices: [],
  cameraToken: readStorage(STORAGE.token),
  cameraDeviceId: readStorage(STORAGE.id),
  cameraDeviceName: readStorage(STORAGE.name),
  cameraStream: null,
  cameraSocket: null,
  cameraReady: false,
  cameraActive: false,
  cameraFatal: false,
  cameraRetry: 0,
  cameraReconnectTimer: null,
  cameraSession: null,
  cameraStatsTimer: null,
  cameraStatsPrevious: null,
  wakeWanted: false,
  wakeSentinel: null,
  dimmed: false,
  toastTimer: null,
};

function readStorage(key) {
  try { return localStorage.getItem(key) || ''; } catch { return ''; }
}

function writeStorage(key, value) {
  try { localStorage.setItem(key, value); } catch { /* The camera token is required for this browser enrollment. */ }
}

function removeStorage(key) {
  try { localStorage.removeItem(key); } catch { /* Storage may be disabled by browser policy. */ }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers,
  };
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    init.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError('无法连接服务，请检查网络后重试。');
  }
  let payload = {};
  try { payload = await response.json(); } catch { /* An empty error response is still handled below. */ }
  if (!response.ok) {
    throw new ApiError(typeof payload.error === 'string' ? payload.error : `请求失败（${response.status}）`, response.status);
  }
  return payload;
}

function showMessage(element, message = '', kind = '') {
  if (!element) return;
  element.textContent = message;
  element.classList.toggle('is-error', kind === 'error');
  element.classList.toggle('is-success', kind === 'success');
}

function setBusy(button, busy, busyLabel = '请稍候…') {
  if (!button) return () => {};
  const oldText = button.textContent;
  button.disabled = busy;
  if (busy) button.textContent = busyLabel;
  return () => {
    button.disabled = false;
    button.textContent = oldText;
  };
}

function setNetworkStatus(online, label) {
  const dot = byId('network-dot');
  const text = byId('network-label');
  dot.classList.toggle('status-dot-good', online);
  text.textContent = label;
}

function toWebSocketUrl() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/ws`;
}

function formatLocalTime(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
}

function setRoute(route) {
  const nextRoute = route === 'camera' ? 'camera' : 'viewer';
  app.route = nextRoute;
  byId('viewer-page').hidden = nextRoute !== 'viewer';
  byId('camera-page').hidden = nextRoute !== 'camera';
  document.querySelectorAll('.role-link').forEach((link) => {
    const active = link.dataset.route === nextRoute;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

function applyHashRoute() {
  const route = location.hash.replace(/^#\/?/, '').split('/')[0];
  setRoute(route === 'camera' ? 'camera' : 'viewer');
}

function showViewerLogin(message = '') {
  app.authenticated = false;
  byId('viewer-login').hidden = false;
  byId('viewer-dashboard').hidden = true;
  showMessage(byId('login-message'), message, message ? 'error' : '');
}

function showViewerDashboard() {
  app.authenticated = true;
  byId('viewer-login').hidden = true;
  byId('viewer-dashboard').hidden = false;
  showMessage(byId('login-message'));
}

function describeApiError(error) {
  return error instanceof ApiError ? error.message : '发生意外错误，请稍后重试。';
}

async function boot() {
  applyHashRoute();
  updateVisibilityWarning();
  setupCameraIdentity();
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  try {
    await api('/healthz');
    setNetworkStatus(true, '服务在线');
  } catch {
    setNetworkStatus(false, '服务离线');
  }
  try {
    const session = await api('/api/session');
    if (session.authenticated === true) {
      showViewerDashboard();
      await refreshDevices();
      connectViewerSocket();
    } else {
      showViewerLogin();
    }
  } catch (error) {
    showViewerLogin(error.status ? '请登录以查看和管理摄像头。' : '当前无法连接服务器，联网后可登录查看。');
    if (!error.status) setNetworkStatus(false, '无法连接');
  }
}

byId('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const input = byId('login-password');
  const button = form.querySelector('button[type="submit"]');
  const restore = setBusy(button, true, '正在登录…');
  showMessage(byId('login-message'));
  const password = input.value;
  try {
    await api('/api/login', { method: 'POST', body: { password } });
    input.value = '';
    showViewerDashboard();
    await refreshDevices();
    connectViewerSocket();
  } catch (error) {
    showMessage(byId('login-message'), describeApiError(error), 'error');
  } finally {
    input.value = '';
    restore();
  }
});

byId('logout-button').addEventListener('click', async () => {
  byId('logout-button').disabled = true;
  app.viewerWatchWanted = false;
  sendViewer({ type: 'unwatch' });
  clearViewerSession('已退出查看');
  try { await api('/api/logout', { method: 'POST', body: {} }); } catch { /* Local logout still clears the view. */ }
  closeViewerSocket(true);
  showViewerLogin('已安全退出。');
  byId('logout-button').disabled = false;
});

byId('pairing-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const nameInput = byId('pairing-name');
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const restore = setBusy(button, true, '正在生成…');
  byId('pairing-result').hidden = true;
  showMessage(byId('pairing-message'));
  try {
    const result = await api('/api/pairings', { method: 'POST', body: { name: nameInput.value.trim() } });
    byId('pairing-code').textContent = String(result.code || '').toUpperCase();
    byId('pairing-expiry').textContent = result.expires_at ? `有效至 ${formatLocalTime(result.expires_at)}` : '10 分钟内有效 · 仅能使用一次';
    byId('pairing-result').hidden = false;
    showMessage(byId('pairing-message'), '配对码已创建。此代码只显示在当前页面，请在摄像端输入。', 'success');
    nameInput.value = '';
  } catch (error) {
    handleAdminApiError(error, byId('pairing-message'));
  } finally {
    restore();
  }
});

byId('copy-code').addEventListener('click', async () => {
  const code = byId('pairing-code').textContent;
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code);
    showMessage(byId('pairing-message'), '配对码已复制。', 'success');
  } catch {
    showMessage(byId('pairing-message'), '浏览器未允许复制，请手动抄写配对码。', 'error');
  }
});

byId('refresh-devices').addEventListener('click', refreshDevices);

async function handleAdminApiError(error, messageNode) {
  showMessage(messageNode, describeApiError(error), 'error');
  if (error.status === 401) await handleViewerAuthExpired('登录已过期，请重新登录。');
}

async function refreshDevices() {
  if (!app.authenticated) return;
  const button = byId('refresh-devices');
  button.disabled = true;
  try {
    const result = await api('/api/devices');
    app.devices = Array.isArray(result.devices) ? result.devices : [];
    renderDevices();
    showMessage(byId('devices-message'));
  } catch (error) {
    app.devices = [];
    renderDevices('暂时无法读取摄像头列表。请检查服务器连接后重试。');
    await handleAdminApiError(error, byId('devices-message'));
  } finally {
    button.disabled = false;
  }
}

function renderDevices(errorText = '') {
  const list = byId('devices-list');
  list.replaceChildren();
  if (errorText) {
    const item = document.createElement('li');
    item.className = 'empty-row';
    const symbol = document.createElement('span');
    symbol.className = 'empty-symbol';
    symbol.setAttribute('aria-hidden', 'true');
    symbol.textContent = '!';
    const text = document.createElement('span');
    text.textContent = errorText;
    item.append(symbol, text);
    list.append(item);
    return;
  }
  if (!app.devices.length) {
    const item = document.createElement('li');
    item.className = 'empty-row';
    const symbol = document.createElement('span');
    symbol.className = 'empty-symbol';
    symbol.setAttribute('aria-hidden', 'true');
    symbol.textContent = '⌁';
    const text = document.createElement('span');
    text.textContent = '还没有配对设备。创建一次性配对码即可开始。';
    item.append(symbol, text);
    list.append(item);
    return;
  }
  for (const device of app.devices) {
    const item = document.createElement('li');
    item.className = 'device-card';
    const info = document.createElement('div');
    info.className = 'device-info';
    const icon = document.createElement('span');
    icon.className = 'device-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = '▣';
    const meta = document.createElement('div');
    meta.className = 'device-meta';
    const name = document.createElement('strong');
    name.textContent = device.name || '未命名设备';
    const subtitle = document.createElement('small');
    const dot = document.createElement('span');
    dot.className = `status-dot${device.online ? ' status-dot-good' : ''}`;
    const stateText = document.createElement('span');
    const viewerCount = Number.isFinite(device.viewer_count) ? device.viewer_count : 0;
    stateText.textContent = device.online ? `在线 · ${viewerCount} 位观看者` : '离线';
    subtitle.append(dot, stateText);
    meta.append(name, subtitle);
    info.append(icon, meta);
    const actions = document.createElement('div');
    actions.className = 'device-actions';
    const watch = document.createElement('button');
    watch.type = 'button';
    watch.className = 'button button-primary button-small';
    watch.textContent = app.viewerDeviceId === device.id && app.viewerWatchWanted ? '正在查看' : '查看画面';
    watch.disabled = !device.online;
    watch.setAttribute('aria-label', `${device.online ? '查看' : '设备离线'}：${device.name || '未命名设备'}`);
    watch.addEventListener('click', () => beginWatching(device));
    const revoke = document.createElement('button');
    revoke.type = 'button';
    revoke.className = 'text-button';
    revoke.textContent = '移除';
    revoke.setAttribute('aria-label', `吊销并移除：${device.name || '未命名设备'}`);
    revoke.addEventListener('click', () => revokeDevice(device));
    actions.append(watch, revoke);
    item.append(info, actions);
    list.append(item);
  }
}

async function revokeDevice(device) {
  const deviceName = device.name || '此摄像头';
  if (!window.confirm(`确定吊销并移除“${deviceName}”吗？此手机会立即失去连接，需要重新配对才能恢复。`)) return;
  try {
    await api(`/api/devices/${encodeURIComponent(device.id)}`, { method: 'DELETE' });
    if (app.viewerDeviceId === device.id) {
      app.viewerWatchWanted = false;
      app.viewerDeviceId = null;
      sendViewer({ type: 'unwatch' });
      clearViewerSession('设备已吊销。');
    }
    await refreshDevices();
  } catch (error) {
    await handleAdminApiError(error, byId('devices-message'));
  }
}

function connectViewerSocket() {
  if (!app.authenticated || (app.viewerSocket && app.viewerSocket.readyState < WebSocket.CLOSING)) return;
  if (app.viewerReconnectTimer) clearTimeout(app.viewerReconnectTimer);
  app.viewerReconnectTimer = null;
  app.viewerIntentionalClose = false;
  const socket = new WebSocket(toWebSocketUrl());
  app.viewerSocket = socket;
  app.viewerReady = false;
  socket.addEventListener('open', () => {
    if (app.viewerSocket !== socket) return;
    try { socket.send(JSON.stringify({ type: 'auth', role: 'viewer' })); } catch { socket.close(); }
  });
  socket.addEventListener('message', (event) => {
    if (app.viewerSocket !== socket) return;
    handleViewerMessage(socket, event.data);
  });
  socket.addEventListener('close', (event) => {
    if (app.viewerSocket !== socket) return;
    app.viewerReady = false;
    app.viewerPeerId = null;
    clearViewerSession('连接中断');
    if (event.code === 1008) {
      handleViewerAuthExpired('查看端会话已失效，请重新登录。');
      return;
    }
    if (!app.viewerIntentionalClose && app.authenticated) scheduleViewerReconnect();
  });
  socket.addEventListener('error', () => {
    if (app.viewerSocket === socket) setNetworkStatus(false, '连接中断');
  });
}

function scheduleViewerReconnect() {
  if (app.viewerReconnectTimer || !app.authenticated) return;
  const delay = Math.min(30000, 800 * (2 ** Math.min(app.viewerRetry, 5)));
  app.viewerRetry += 1;
  setNetworkStatus(false, '正在重连');
  app.viewerReconnectTimer = setTimeout(() => {
    app.viewerReconnectTimer = null;
    if (app.authenticated) connectViewerSocket();
  }, delay);
}

function closeViewerSocket(intentional = false) {
  app.viewerIntentionalClose = intentional;
  if (app.viewerReconnectTimer) clearTimeout(app.viewerReconnectTimer);
  app.viewerReconnectTimer = null;
  const socket = app.viewerSocket;
  app.viewerSocket = null;
  app.viewerReady = false;
  app.viewerPeerId = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'viewer closed');
}

function sendViewer(message, socket = app.viewerSocket) {
  if (!socket || socket.readyState !== WebSocket.OPEN || socket !== app.viewerSocket) return false;
  try { socket.send(JSON.stringify(message)); return true; } catch { return false; }
}

function handleViewerMessage(socket, raw) {
  let message;
  try { message = JSON.parse(raw); } catch { return; }
  if (socket !== app.viewerSocket || !message || typeof message.type !== 'string') return;
  if (message.type === 'ready') {
    if (message.role !== 'viewer') return;
    app.viewerReady = true;
    app.viewerRetry = 0;
    app.viewerPeerId = message.peer_id || null;
    setNetworkStatus(true, '服务在线');
    refreshDevices();
    if (app.viewerWatchWanted && app.viewerDeviceId) sendViewer({ type: 'watch', device_id: app.viewerDeviceId }, socket);
    return;
  }
  if (message.type === 'devices') {
    app.devices = Array.isArray(message.devices) ? message.devices : [];
    renderDevices();
    return;
  }
  if (message.type === 'watching') {
    if (!app.viewerWatchWanted || message.device_id !== app.viewerDeviceId || typeof message.session_id !== 'string') return;
    if (app.viewerSession?.sessionId === message.session_id) return;
    clearViewerSession('正在建立媒体连接');
    app.viewerSession = {
      sessionId: message.session_id,
      deviceId: message.device_id,
      peerId: message.peer_id,
      pc: null,
      cancelled: false,
      answerPromise: null,
      remoteReady: false,
      iceQueue: [],
      socket,
    };
    byId('viewer-placeholder').hidden = false;
    byId('viewer-placeholder').querySelector('strong').textContent = '正在连接摄像头…';
    setViewerBadge(false, '协商中');
    byId('link-status').textContent = '正在协商';
    scheduleViewerRefresh(app.viewerSession);
    return;
  }
  if (message.type === 'signal') {
    const session = app.viewerSession;
    if (!session || session.cancelled || message.session_id !== session.sessionId || message.source !== session.peerId || socket !== session.socket) return;
    const data = message.data;
    if (!data || typeof data.type !== 'string') return;
    if (data.type === 'offer' && data.sdp) handleViewerOffer(socket, session, data.sdp);
    else if (data.type === 'ice' && data.candidate) handleViewerIce(socket, session, data.candidate);
    return;
  }
  if (message.type === 'peer-left') {
    if (app.viewerSession && message.session_id === app.viewerSession.sessionId) {
      clearViewerSession('摄像头连接已断开；设备重新上线后，请重新点击“查看画面”');
    }
    return;
  }
  if (message.type === 'error') {
    const errorText = typeof message.error === 'string' ? message.error : '服务端拒绝了本次连接。';
    showMessage(byId('watch-message'), errorText, 'error');
    if (/unauthori[sz]ed|session|登录|认证|未授权/i.test(errorText)) handleViewerAuthExpired(errorText);
    else if (!app.viewerReady) handleViewerAuthExpired('查看端验证失败，请重新登录。');
  }
}

async function beginWatching(device) {
  if (!app.authenticated || !device?.online) return;
  if (app.viewerSession || app.viewerWatchWanted) sendViewer({ type: 'unwatch' });
  clearViewerSession('正在连接…');
  app.viewerDeviceId = device.id;
  app.viewerWatchWanted = true;
  byId('viewer-placeholder').hidden = false;
  byId('viewer-placeholder').querySelector('strong').textContent = '正在连接摄像头…';
  byId('viewer-placeholder').querySelector('span:last-child').textContent = '等待摄像端响应';
  showMessage(byId('watch-message'), '正在请求实时画面…');
  renderDevices();
  byId('unwatch-button').disabled = false;
  if (!app.viewerSocket || app.viewerSocket.readyState >= WebSocket.CLOSING) connectViewerSocket();
  else if (app.viewerReady) sendViewer({ type: 'watch', device_id: device.id });
}

byId('unwatch-button').addEventListener('click', () => {
  app.viewerWatchWanted = false;
  app.viewerDeviceId = null;
  sendViewer({ type: 'unwatch' });
  clearViewerSession('已断开查看');
  showMessage(byId('watch-message'), '已断开实时画面。');
  renderDevices();
});

function scheduleViewerRefresh(session) {
  if (app.viewerRefreshTimer) clearTimeout(app.viewerRefreshTimer);
  app.viewerRefreshTimer = setTimeout(() => {
    if (app.viewerSession !== session || session.cancelled || !app.viewerWatchWanted || !app.viewerReady) return;
    const socket = app.viewerSocket;
    sendViewer({ type: 'unwatch' }, socket);
    clearViewerSession('正在刷新安全连接…');
    setTimeout(() => {
      if (app.viewerWatchWanted && app.viewerDeviceId && app.viewerSocket === socket && app.viewerReady) {
        sendViewer({ type: 'watch', device_id: app.viewerDeviceId }, socket);
      }
    }, 450);
  }, REFRESH_MS);
}

function clearViewerSession(message = '') {
  if (app.viewerRefreshTimer) clearTimeout(app.viewerRefreshTimer);
  app.viewerRefreshTimer = null;
  if (app.viewerStatsTimer) clearInterval(app.viewerStatsTimer);
  app.viewerStatsTimer = null;
  const session = app.viewerSession;
  app.viewerSession = null;
  if (session) {
    session.cancelled = true;
    try { session.pc?.close(); } catch { /* Already closed. */ }
  }
  app.viewerStatsPrevious = null;
  app.viewerRemoteAudio = false;
  const video = byId('viewer-video');
  video.srcObject = null;
  video.muted = true;
  byId('viewer-placeholder').hidden = false;
  byId('viewer-placeholder').querySelector('strong').textContent = '选择一台摄像头';
  byId('viewer-placeholder').querySelector('span:last-child').textContent = '实时画面会显示在这里';
  byId('video-live-tag').hidden = true;
  byId('sound-button').disabled = true;
  byId('sound-button').textContent = '开启声音';
  byId('snapshot-button').disabled = true;
  byId('unwatch-button').disabled = !app.viewerWatchWanted;
  byId('link-status').textContent = message || '等待连接';
  byId('receive-rate').textContent = '—';
  byId('round-trip').textContent = '—';
  byId('decoded-fps').textContent = '—';
  byId('ice-path').textContent = '—';
  setViewerBadge(false, app.viewerWatchWanted ? '等待连接' : '未连接');
}

async function handleViewerOffer(socket, session, description) {
  if (session.answerPromise) return session.answerPromise;
  session.answerPromise = (async () => {
    try {
      const ice = await api('/api/ice');
      if (!viewerSessionCurrent(socket, session)) return;
      const pc = new RTCPeerConnection({ iceServers: ice.ice_servers || [], iceTransportPolicy: ice.ice_transport_policy || 'all' });
      session.pc = pc;
      installViewerPeerHandlers(socket, session, pc);
      await pc.setRemoteDescription(description);
      if (!viewerSessionCurrent(socket, session)) return;
      session.remoteReady = true;
      await drainViewerIce(socket, session);
      const answer = await pc.createAnswer();
      if (!viewerSessionCurrent(socket, session)) return;
      await pc.setLocalDescription(answer);
      if (!viewerSessionCurrent(socket, session)) return;
      sendViewer({ type: 'signal', target: session.peerId, session_id: session.sessionId, data: { type: 'answer', sdp: pc.localDescription.toJSON() } }, socket);
      byId('link-status').textContent = '已发送连接应答';
      startViewerStats(session, pc);
    } catch (error) {
      if (!viewerSessionCurrent(socket, session)) return;
      showMessage(byId('watch-message'), `无法建立媒体连接：${describeApiError(error)}`, 'error');
      clearViewerSession('连接失败');
    }
  })();
  return session.answerPromise;
}

function viewerSessionCurrent(socket, session) {
  return socket === app.viewerSocket && session.socket === socket && app.viewerSession === session && !session.cancelled;
}

function installViewerPeerHandlers(socket, session, pc) {
  pc.addEventListener('track', (event) => {
    if (!viewerSessionCurrent(socket, session) || session.pc !== pc) return;
    if (event.track.kind === 'audio') {
      app.viewerRemoteAudio = true;
      byId('sound-button').disabled = false;
    }
    const video = byId('viewer-video');
    if (event.streams && event.streams[0]) video.srcObject = event.streams[0];
    else {
      const stream = video.srcObject instanceof MediaStream ? video.srcObject : new MediaStream();
      stream.addTrack(event.track);
      video.srcObject = stream;
    }
    if (event.track.kind === 'video') {
      byId('viewer-placeholder').hidden = true;
      byId('video-live-tag').hidden = false;
      byId('snapshot-button').disabled = false;
      setViewerBadge(true, '已连接');
      byId('link-status').textContent = '已连接 · 正在读取链路';
      video.play().catch(() => {});
      event.track.addEventListener('ended', () => {
        if (viewerSessionCurrent(socket, session)) clearViewerSession('摄像头画面已结束');
      }, { once: true });
    }
  });
  pc.addEventListener('connectionstatechange', () => {
    if (!viewerSessionCurrent(socket, session) || session.pc !== pc) return;
    const status = pc.connectionState;
    if (status === 'connected') {
      setViewerBadge(true, '已连接');
      byId('link-status').textContent = '已连接';
    } else if (status === 'connecting') {
      setViewerBadge(false, '连接中');
      byId('link-status').textContent = '连接中';
    } else if (status === 'disconnected') {
      setViewerBadge(false, '尝试恢复');
      byId('link-status').textContent = '尝试恢复连接';
    } else if (status === 'failed' || status === 'closed') {
      clearViewerSession(status === 'failed' ? '连接失败' : '连接已关闭');
    }
  });
  pc.addEventListener('iceconnectionstatechange', () => {
    if (!viewerSessionCurrent(socket, session) || session.pc !== pc) return;
    if (pc.iceConnectionState === 'failed') {
      setViewerBadge(false, '链路失败');
      byId('link-status').textContent = '链路失败';
    }
  });
  pc.addEventListener('icecandidate', (event) => {
    if (!event.candidate || !viewerSessionCurrent(socket, session) || session.pc !== pc) return;
    sendViewer({ type: 'signal', target: session.peerId, session_id: session.sessionId, data: { type: 'ice', candidate: event.candidate.toJSON() } }, socket);
  });
}

function handleViewerIce(socket, session, candidate) {
  if (!viewerSessionCurrent(socket, session)) return;
  enqueueRemoteIce(session, candidate);
}

async function drainViewerIce(socket, session) {
  await flushRemoteIce(session, () => viewerSessionCurrent(socket, session));
}

function setViewerBadge(live, text) {
  const badge = byId('viewer-live-badge');
  badge.classList.toggle('is-live', live);
  badge.querySelector('.status-dot').classList.toggle('status-dot-good', live);
  badge.lastChild.textContent = text;
}

byId('sound-button').addEventListener('click', async () => {
  const video = byId('viewer-video');
  if (!app.viewerRemoteAudio || !video.srcObject) return;
  video.muted = !video.muted;
  byId('sound-button').textContent = video.muted ? '开启声音' : '静音';
  if (!video.muted) {
    try { await video.play(); } catch { showMessage(byId('watch-message'), '浏览器未允许播放声音，请再次轻触“开启声音”。', 'error'); }
  }
});

byId('snapshot-button').addEventListener('click', () => {
  const video = byId('viewer-video');
  if (!video.srcObject || !video.videoWidth || !video.videoHeight) {
    showMessage(byId('watch-message'), '画面尚未就绪，连接后即可保存快照。', 'error');
    return;
  }
  const canvas = byId('snapshot-canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const context = canvas.getContext('2d');
  if (!context) return;
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    if (!blob) {
      showMessage(byId('watch-message'), '当前浏览器无法生成 JPEG 快照。', 'error');
      return;
    }
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `homecam-${new Date().toISOString().replaceAll(':', '-')}.jpg`;
    link.click();
    URL.revokeObjectURL(url);
    showMessage(byId('watch-message'), 'JPEG 快照已下载到当前设备。', 'success');
  }, 'image/jpeg', 0.92);
});

function startViewerStats(session, pc) {
  if (app.viewerStatsTimer) clearInterval(app.viewerStatsTimer);
  app.viewerStatsPrevious = null;
  const tick = () => readPeerStats('viewer', session, pc);
  tick();
  app.viewerStatsTimer = setInterval(tick, 2000);
}

function pickVideoStats(stats, type) {
  const candidates = [];
  stats.forEach((report) => {
    const kind = report.kind || report.mediaType;
    if (report.type === type && kind === 'video' && !report.isRemote) candidates.push(report);
  });
  return candidates.sort((a, b) => (b.bytesReceived || b.bytesSent || 0) - (a.bytesReceived || a.bytesSent || 0))[0] || null;
}

function selectedCandidatePair(stats) {
  let selectedId = null;
  stats.forEach((report) => {
    if (report.type === 'transport' && report.selectedCandidatePairId) selectedId = report.selectedCandidatePairId;
  });
  if (selectedId) return stats.get(selectedId) || null;
  let selected = null;
  stats.forEach((report) => {
    if (report.type === 'candidate-pair' && (report.selected || (report.nominated && report.state === 'succeeded'))) selected = report;
  });
  return selected;
}

async function readPeerStats(side, session, pc) {
  if (pc.connectionState === 'closed') return;
  const stillCurrent = side === 'viewer'
    ? viewerSessionCurrent(session.socket, session) && session.pc === pc
    : cameraSessionCurrent(session.socket, session) && session.pc === pc;
  if (!stillCurrent) return;
  try {
    const stats = await pc.getStats();
    if (side === 'viewer' ? !viewerSessionCurrent(session.socket, session) : !cameraSessionCurrent(session.socket, session)) return;
    const report = pickVideoStats(stats, side === 'viewer' ? 'inbound-rtp' : 'outbound-rtp');
    const bytesKey = side === 'viewer' ? 'bytesReceived' : 'bytesSent';
    let rate = null;
    let fps = null;
    if (report) {
      const now = performance.now();
      const previous = side === 'viewer' ? app.viewerStatsPrevious : app.cameraStatsPrevious;
      const bytes = Number(report[bytesKey]);
      const frames = Number(side === 'viewer' ? report.framesDecoded : report.framesEncoded);
      const durationSeconds = previous ? (now - previous.at) / 1000 : 0;
      if (previous && durationSeconds > 0 && Number.isFinite(bytes) && bytes >= previous.bytes) rate = ((bytes - previous.bytes) * 8) / durationSeconds / 1000;
      if (previous && durationSeconds > 0 && Number.isFinite(frames) && frames >= previous.frames) fps = (frames - previous.frames) / durationSeconds;
      else if (Number.isFinite(Number(report.framesPerSecond))) fps = Number(report.framesPerSecond);
      const next = { at: now, bytes: Number.isFinite(bytes) ? bytes : 0, frames: Number.isFinite(frames) ? frames : 0 };
      if (side === 'viewer') app.viewerStatsPrevious = next;
      else app.cameraStatsPrevious = next;
    }
    const pair = selectedCandidatePair(stats);
    const local = pair?.localCandidateId ? stats.get(pair.localCandidateId) : null;
    const remote = pair?.remoteCandidateId ? stats.get(pair.remoteCandidateId) : null;
    const route = pair && local && remote
      ? `${local.candidateType || '?'} / ${remote.candidateType || '?'}${local.protocol ? ` · ${String(local.protocol).toUpperCase()}` : ''}`
      : '候选链路等待中';
    const rtt = pair && Number.isFinite(Number(pair.currentRoundTripTime)) ? `${Math.round(Number(pair.currentRoundTripTime) * 1000)} ms` : '—';
    const frameText = report && Number.isFinite(Number(report.framesDecoded ?? report.framesEncoded))
      ? `${fps === null ? '—' : `${fps.toFixed(1)} fps`} · ${side === 'viewer' ? Number(report.framesDecoded) : Number(report.framesEncoded)} 帧`
      : '—';
    if (side === 'viewer') {
      byId('receive-rate').textContent = rate === null ? '读取中' : `${rate.toFixed(0)} kbps`;
      byId('round-trip').textContent = rtt;
      byId('decoded-fps').textContent = frameText;
      byId('ice-path').textContent = route;
      if (pair && local?.candidateType === 'relay') byId('link-status').textContent = '已连接 · TURN relay';
    } else {
      byId('camera-peer-status').textContent = pc.connectionState === 'connected' ? '观看端已连接' : pc.connectionState;
      byId('camera-ice-path').textContent = route;
      byId('camera-send-rate').textContent = rate === null ? '读取中' : `${rate.toFixed(0)} kbps`;
      byId('camera-fps').textContent = frameText;
      byId('camera-rtt').textContent = rtt;
    }
  } catch { /* Statistics support differs by browser; the live session remains useful without it. */ }
}

async function handleViewerAuthExpired(message) {
  if (!app.authenticated && byId('viewer-login').hidden === false) return;
  app.viewerWatchWanted = false;
  app.viewerDeviceId = null;
  sendViewer({ type: 'unwatch' });
  closeViewerSocket(true);
  clearViewerSession('需要重新登录');
  showViewerLogin(message);
}

function setupCameraIdentity() {
  const hasToken = Boolean(app.cameraToken);
  byId('camera-enroll').hidden = hasToken;
  byId('camera-console').hidden = !hasToken;
  byId('camera-device-name').textContent = hasToken ? `当前绑定：${app.cameraDeviceName || '旧手机摄像头'}` : '';
}

byId('enroll-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const restore = setBusy(button, true, '正在绑定…');
  showMessage(byId('enroll-message'));
  const code = byId('enroll-code').value.trim().toUpperCase();
  try {
    const result = await api('/api/pairings/redeem', { method: 'POST', body: { code } });
    if (!result.device_token || !result.device_id) throw new ApiError('服务器未返回有效的设备绑定信息。');
    app.cameraToken = result.device_token;
    app.cameraDeviceId = result.device_id;
    app.cameraDeviceName = result.name || '';
    writeStorage(STORAGE.token, app.cameraToken);
    writeStorage(STORAGE.id, app.cameraDeviceId);
    writeStorage(STORAGE.name, app.cameraDeviceName);
    byId('enroll-code').value = '';
    setupCameraIdentity();
    showMessage(byId('camera-message'), '绑定完成。此页面不会自动启动摄像头，请在你准备好时手动开始。', 'success');
  } catch (error) {
    showMessage(byId('enroll-message'), describeApiError(error), 'error');
  } finally {
    restore();
  }
});

byId('forget-device').addEventListener('click', async () => {
  if (app.cameraActive && !window.confirm('摄像头正在采集。停止采集并从此手机移除绑定吗？')) return;
  if (!app.cameraActive && !window.confirm('从此手机移除此绑定？管理员列表中的设备需要由查看端单独吊销。')) return;
  await stopCamera('已停止并清除此手机上的绑定。');
  app.cameraToken = '';
  app.cameraDeviceId = '';
  app.cameraDeviceName = '';
  removeStorage(STORAGE.token);
  removeStorage(STORAGE.id);
  removeStorage(STORAGE.name);
  setupCameraIdentity();
});

byId('camera-start').addEventListener('click', startCamera);
byId('camera-stop').addEventListener('click', () => stopCamera('摄像头已停止，设备连接已关闭。'));
byId('camera-facing').addEventListener('change', cameraSettingsChanged);
byId('camera-resolution').addEventListener('change', cameraSettingsChanged);
byId('camera-microphone').addEventListener('change', cameraSettingsChanged);

function cameraSettingsChanged() {
  if (app.cameraActive) showMessage(byId('camera-message'), '设置已更改。停止采集后再开始即可应用新设置。');
}

async function startCamera() {
  if (!app.cameraToken || app.cameraActive) return;
  if (!navigator.mediaDevices?.getUserMedia) {
    showCameraError('此浏览器不支持摄像头访问。请使用现代浏览器和 HTTPS 页面。');
    return;
  }
  const button = byId('camera-start');
  const restore = setBusy(button, true, '正在请求权限…');
  showMessage(byId('camera-message'));
  const height = byId('camera-resolution').value === '720' ? 720 : 480;
  const videoConstraints = {
    facingMode: { ideal: byId('camera-facing').value },
    width: { ideal: Math.round(height * 4 / 3) },
    height: { ideal: height },
    frameRate: { ideal: 12, max: 15 },
  };
  const constraints = { video: videoConstraints, audio: byId('camera-microphone').checked };
  try {
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    if (!app.cameraToken) {
      stream.getTracks().forEach((track) => track.stop());
      throw new ApiError('设备绑定已移除，请重新配对。');
    }
    app.cameraStream = stream;
    app.cameraActive = true;
    app.cameraFatal = false;
    byId('camera-video').srcObject = stream;
    byId('camera-video').play().catch(() => {});
    byId('camera-placeholder').hidden = true;
    byId('camera-live-tag').hidden = false;
    byId('capture-indicator').hidden = false;
    byId('camera-active-caption').hidden = false;
    byId('camera-start').hidden = true;
    byId('camera-stop').hidden = false;
    byId('camera-facing').disabled = true;
    byId('camera-resolution').disabled = true;
    byId('camera-microphone').disabled = true;
    setCameraStatus('摄像头已启动 · 正在连接服务', true);
    showMessage(byId('camera-message'), '摄像头和已选择的麦克风正在本机工作。停止采集会关闭所有媒体轨道。');
    for (const track of stream.getTracks()) {
      track.addEventListener('ended', () => {
        if (app.cameraActive) stopCamera(`${track.kind === 'video' ? '摄像头' : '麦克风'}轨道已结束，连接已清理。`);
      }, { once: true });
    }
    connectCameraSocket();
    if (app.wakeWanted) requestWakeLock();
  } catch (error) {
    showCameraError(translateMediaError(error));
  } finally {
    restore();
    if (app.cameraActive) byId('camera-start').hidden = true;
  }
}

function translateMediaError(error) {
  if (error?.name === 'NotAllowedError' || error?.name === 'PermissionDeniedError') return '没有获得摄像头或麦克风权限。请在浏览器设置中允许访问后重试。';
  if (error?.name === 'NotFoundError' || error?.name === 'DevicesNotFoundError') return '没有找到所需的摄像头或麦克风。';
  if (error?.name === 'NotReadableError' || error?.name === 'TrackStartError') return '摄像头正被其他应用使用，或暂时无法读取。';
  if (error?.name === 'OverconstrainedError') return '所选摄像头不支持当前画面设置，请切换清晰度或摄像头方向。';
  if (error instanceof ApiError) return error.message;
  return '无法启动摄像头。请确认使用 HTTPS，并检查浏览器的设备权限。';
}

function showCameraError(message) {
  setCameraStatus(message, false, true);
  showMessage(byId('camera-message'), message, 'error');
}

function setCameraStatus(message, active, error = false) {
  const banner = byId('camera-state-banner');
  banner.classList.toggle('is-active', Boolean(active));
  banner.classList.toggle('is-error', Boolean(error));
  byId('camera-state-text').textContent = message;
}

function connectCameraSocket() {
  if (!app.cameraActive || !app.cameraToken || app.cameraFatal || (app.cameraSocket && app.cameraSocket.readyState < WebSocket.CLOSING)) return;
  if (app.cameraReconnectTimer) clearTimeout(app.cameraReconnectTimer);
  app.cameraReconnectTimer = null;
  const socket = new WebSocket(toWebSocketUrl());
  app.cameraSocket = socket;
  app.cameraReady = false;
  socket.addEventListener('open', () => {
    if (app.cameraSocket !== socket || !app.cameraActive) return;
    try {
      socket.send(JSON.stringify({ type: 'auth', role: 'camera', device_id: app.cameraDeviceId, device_token: app.cameraToken }));
    } catch { socket.close(); }
  });
  socket.addEventListener('message', (event) => {
    if (app.cameraSocket !== socket) return;
    handleCameraMessage(socket, event.data);
  });
  socket.addEventListener('close', (event) => {
    if (app.cameraSocket !== socket) return;
    app.cameraReady = false;
    clearCameraSession('观看端已断开');
    if (event.code === 1008) {
      invalidateCameraBinding('设备绑定已失效或已被管理员吊销，请重新配对。');
      return;
    }
    if (app.cameraActive && !app.cameraFatal) scheduleCameraReconnect();
  });
  socket.addEventListener('error', () => {
    if (app.cameraSocket === socket && app.cameraActive) setCameraStatus('服务连接中断 · 正在重连', true);
  });
}

function scheduleCameraReconnect() {
  if (app.cameraReconnectTimer || !app.cameraActive || app.cameraFatal) return;
  const delay = Math.min(30000, 800 * (2 ** Math.min(app.cameraRetry, 5)));
  app.cameraRetry += 1;
  setCameraStatus('服务连接中断 · 正在重连', true);
  app.cameraReconnectTimer = setTimeout(() => {
    app.cameraReconnectTimer = null;
    if (app.cameraActive && !app.cameraFatal) connectCameraSocket();
  }, delay);
}

function sendCamera(message, socket = app.cameraSocket) {
  if (!socket || socket !== app.cameraSocket || socket.readyState !== WebSocket.OPEN) return false;
  try { socket.send(JSON.stringify(message)); return true; } catch { return false; }
}

function handleCameraMessage(socket, raw) {
  let message;
  try { message = JSON.parse(raw); } catch { return; }
  if (socket !== app.cameraSocket || !message || typeof message.type !== 'string') return;
  if (message.type === 'ready') {
    if (message.role !== 'camera') {
      invalidateCameraBinding('服务端未确认摄像端身份。请重新配对。');
      return;
    }
    app.cameraReady = true;
    app.cameraRetry = 0;
    setCameraStatus('摄像头已启动 · 等待查看端连接', true);
    showMessage(byId('camera-message'), '摄像端服务已连接。当前没有远端观看者。');
    return;
  }
  if (message.type === 'viewer-joined') {
    if (!app.cameraReady || typeof message.session_id !== 'string' || typeof message.peer_id !== 'string') return;
    if (app.cameraSession?.sessionId === message.session_id) return;
    clearCameraSession('正在建立安全媒体连接');
    const session = {
      sessionId: message.session_id,
      peerId: message.peer_id,
      pc: null,
      cancelled: false,
      offerPromise: null,
      remoteReady: false,
      iceQueue: [],
      socket,
    };
    app.cameraSession = session;
    createCameraOffer(socket, session);
    return;
  }
  if (message.type === 'viewer-left') {
    if (app.cameraSession && message.session_id === app.cameraSession.sessionId) clearCameraSession('观看端已断开');
    return;
  }
  if (message.type === 'signal') {
    const session = app.cameraSession;
    if (!session || session.cancelled || message.session_id !== session.sessionId || message.source !== session.peerId || socket !== session.socket) return;
    const data = message.data;
    if (!data || typeof data.type !== 'string') return;
    if (data.type === 'answer' && data.sdp) handleCameraAnswer(socket, session, data.sdp);
    else if (data.type === 'ice' && data.candidate) handleCameraIce(socket, session, data.candidate);
    return;
  }
  if (message.type === 'error') {
    const text = typeof message.error === 'string' ? message.error : '服务端拒绝了设备连接。';
    if (!app.cameraReady || /token|令牌|吊销|未授权|认证|配对失效|invalid/i.test(text)) {
      invalidateCameraBinding('设备令牌无效或绑定已吊销。请在查看端重新配对。');
    } else {
      showMessage(byId('camera-message'), text, 'error');
    }
  }
}

async function createCameraOffer(socket, session) {
  if (session.offerPromise) return session.offerPromise;
  session.offerPromise = (async () => {
    try {
      const ice = await api('/api/ice', { headers: { Authorization: `Bearer ${app.cameraToken}` } });
      if (!cameraSessionCurrent(socket, session)) return;
      const pc = new RTCPeerConnection({ iceServers: ice.ice_servers || [], iceTransportPolicy: ice.ice_transport_policy || 'all' });
      session.pc = pc;
      installCameraPeerHandlers(socket, session, pc);
      for (const track of app.cameraStream?.getTracks() || []) pc.addTrack(track, app.cameraStream);
      const videoSender = pc.getSenders().find((sender) => sender.track?.kind === 'video');
      if (videoSender) {
        try {
          const params = videoSender.getParameters();
          params.encodings = params.encodings?.length ? params.encodings : [{}];
          params.encodings[0].maxBitrate = byId('camera-resolution').value === '720' ? 1200000 : 450000;
          params.encodings[0].maxFramerate = 12;
          await videoSender.setParameters(params);
        } catch { /* Browser-specific encoder controls are optional; capture constraints still apply. */ }
      }
      if (!cameraSessionCurrent(socket, session)) return;
      const offer = await pc.createOffer();
      if (!cameraSessionCurrent(socket, session)) return;
      await pc.setLocalDescription(offer);
      if (!cameraSessionCurrent(socket, session)) return;
      sendCamera({ type: 'signal', target: session.peerId, session_id: session.sessionId, data: { type: 'offer', sdp: pc.localDescription.toJSON() } }, socket);
      setCameraStatus('已向查看端发送连接请求', true);
      startCameraStats(session, pc);
    } catch (error) {
      if (!cameraSessionCurrent(socket, session)) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        invalidateCameraBinding('设备令牌无效或绑定已吊销。请在查看端重新配对。');
        return;
      }
      showMessage(byId('camera-message'), `无法建立媒体连接：${describeApiError(error)}`, 'error');
      clearCameraSession('媒体连接失败');
    }
  })();
  return session.offerPromise;
}

function cameraSessionCurrent(socket, session) {
  return socket === app.cameraSocket && session.socket === socket && app.cameraSession === session && !session.cancelled && app.cameraActive;
}

function installCameraPeerHandlers(socket, session, pc) {
  pc.addEventListener('icecandidate', (event) => {
    if (!event.candidate || !cameraSessionCurrent(socket, session) || session.pc !== pc) return;
    sendCamera({ type: 'signal', target: session.peerId, session_id: session.sessionId, data: { type: 'ice', candidate: event.candidate.toJSON() } }, socket);
  });
  pc.addEventListener('connectionstatechange', () => {
    if (!cameraSessionCurrent(socket, session) || session.pc !== pc) return;
    if (pc.connectionState === 'connected') setCameraStatus('摄像头已启动 · 查看端已连接', true);
    else if (pc.connectionState === 'connecting') setCameraStatus('摄像头已启动 · 正在连接查看端', true);
    else if (pc.connectionState === 'disconnected') setCameraStatus('查看链路暂时中断 · 正在恢复', true);
    else if (pc.connectionState === 'failed') {
      showMessage(byId('camera-message'), '媒体链路连接失败。请检查服务器 TURN 配置和网络后重试。', 'error');
      clearCameraSession('媒体链路失败');
    }
  });
  pc.addEventListener('iceconnectionstatechange', () => {
    if (cameraSessionCurrent(socket, session) && session.pc === pc && pc.iceConnectionState === 'failed') {
      showMessage(byId('camera-message'), 'ICE 链路失败。请检查 TURN 服务是否在线。', 'error');
    }
  });
}

async function handleCameraAnswer(socket, session, description) {
  if (!cameraSessionCurrent(socket, session) || !session.pc || session.remoteReady) return;
  try {
    await session.pc.setRemoteDescription(description);
    if (!cameraSessionCurrent(socket, session)) return;
    session.remoteReady = true;
    await drainCameraIce(socket, session);
    setCameraStatus('摄像头已启动 · 正在连接查看端', true);
  } catch {
    if (cameraSessionCurrent(socket, session)) {
      showMessage(byId('camera-message'), '查看端的连接应答无效，媒体连接已清理。', 'error');
      clearCameraSession('媒体连接失败');
    }
  }
}

function handleCameraIce(socket, session, candidate) {
  if (!cameraSessionCurrent(socket, session)) return;
  enqueueRemoteIce(session, candidate);
}

async function drainCameraIce(socket, session) {
  await flushRemoteIce(session, () => cameraSessionCurrent(socket, session));
}

function clearCameraSession(message = '') {
  if (app.cameraStatsTimer) clearInterval(app.cameraStatsTimer);
  app.cameraStatsTimer = null;
  app.cameraStatsPrevious = null;
  const session = app.cameraSession;
  app.cameraSession = null;
  if (session) {
    session.cancelled = true;
    try { session.pc?.close(); } catch { /* Already closed. */ }
  }
  byId('camera-peer-status').textContent = app.cameraActive ? message || '等待观看端' : '未连接';
  byId('camera-ice-path').textContent = '—';
  byId('camera-send-rate').textContent = '—';
  byId('camera-fps').textContent = '—';
  byId('camera-rtt').textContent = '—';
}

function startCameraStats(session, pc) {
  if (app.cameraStatsTimer) clearInterval(app.cameraStatsTimer);
  app.cameraStatsPrevious = null;
  const tick = () => readPeerStats('camera', session, pc);
  tick();
  app.cameraStatsTimer = setInterval(tick, 2000);
}

async function invalidateCameraBinding(message) {
  app.cameraFatal = true;
  await stopCamera(message, { preserveFatal: true });
  app.cameraToken = '';
  app.cameraDeviceId = '';
  app.cameraDeviceName = '';
  removeStorage(STORAGE.token);
  removeStorage(STORAGE.id);
  removeStorage(STORAGE.name);
  setupCameraIdentity();
  showMessage(byId('enroll-message'), message, 'error');
}

async function stopCamera(message = '摄像头已停止。', options = {}) {
  app.cameraActive = false;
  if (!options.preserveFatal) app.cameraFatal = false;
  if (app.cameraReconnectTimer) clearTimeout(app.cameraReconnectTimer);
  app.cameraReconnectTimer = null;
  app.cameraRetry = 0;
  clearCameraSession('未连接');
  const socket = app.cameraSocket;
  app.cameraSocket = null;
  app.cameraReady = false;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'camera stopped');
  if (app.cameraStream) app.cameraStream.getTracks().forEach((track) => track.stop());
  app.cameraStream = null;
  byId('camera-video').srcObject = null;
  byId('camera-placeholder').hidden = false;
  byId('camera-live-tag').hidden = true;
  byId('capture-indicator').hidden = true;
  byId('camera-active-caption').hidden = true;
  byId('camera-start').hidden = false;
  byId('camera-start').disabled = false;
  byId('camera-stop').hidden = true;
  byId('camera-facing').disabled = false;
  byId('camera-resolution').disabled = false;
  byId('camera-microphone').disabled = false;
  app.wakeWanted = false;
  byId('wake-lock-button').setAttribute('aria-pressed', 'false');
  byId('wake-lock-button').textContent = '保持屏幕唤醒：关';
  await releaseWakeLock();
  if (message) {
    setCameraStatus(message, false, options.error === true);
    showMessage(byId('camera-message'), message, options.error ? 'error' : '');
  }
}

byId('wake-lock-button').addEventListener('click', async () => {
  if (app.wakeWanted) {
    app.wakeWanted = false;
    await releaseWakeLock();
    byId('wake-lock-button').setAttribute('aria-pressed', 'false');
    byId('wake-lock-button').textContent = '保持屏幕唤醒：关';
    showMessage(byId('camera-message'), '已允许系统按自身策略休眠。');
    return;
  }
  if (!('wakeLock' in navigator)) {
    showMessage(byId('camera-message'), '此浏览器不支持屏幕唤醒锁。请检查系统显示设置。', 'error');
    return;
  }
  app.wakeWanted = true;
  byId('wake-lock-button').setAttribute('aria-pressed', 'true');
  byId('wake-lock-button').textContent = '保持屏幕唤醒：开';
  await requestWakeLock();
});

async function requestWakeLock() {
  if (!app.wakeWanted || !app.cameraActive || document.visibilityState !== 'visible' || !navigator.wakeLock?.request) return;
  try {
    app.wakeSentinel = await navigator.wakeLock.request('screen');
    app.wakeSentinel.addEventListener('release', () => {
      app.wakeSentinel = null;
      if (app.wakeWanted && app.cameraActive) showMessage(byId('camera-message'), '系统释放了屏幕唤醒锁。你可以再次点击保持屏幕唤醒。');
    }, { once: true });
    showMessage(byId('camera-message'), '屏幕唤醒锁已启用。系统电量策略仍可能覆盖此设置。', 'success');
  } catch {
    showMessage(byId('camera-message'), '无法启用屏幕唤醒锁。请在可见页面中重试。', 'error');
  }
}

async function releaseWakeLock() {
  const sentinel = app.wakeSentinel;
  app.wakeSentinel = null;
  if (sentinel && !sentinel.released) {
    try { await sentinel.release(); } catch { /* The operating system may already have released it. */ }
  }
}

byId('dim-button').addEventListener('click', () => setDimmed(!app.dimmed));
byId('restore-screen').addEventListener('click', () => setDimmed(false));
byId('dim-overlay').addEventListener('click', (event) => {
  if (event.target === byId('dim-overlay')) setDimmed(false);
});

function setDimmed(dimmed) {
  app.dimmed = dimmed;
  byId('dim-overlay').hidden = !dimmed;
  byId('dim-button').setAttribute('aria-pressed', String(dimmed));
  byId('dim-button').textContent = dimmed ? '恢复亮度' : '调暗画面';
  if (dimmed) byId('restore-screen').focus();
  else byId('dim-button').focus();
}

function updateVisibilityWarning() {
  const hidden = document.visibilityState !== 'visible';
  byId('visibility-warning').hidden = !(hidden && app.cameraActive);
  if (!hidden && app.wakeWanted && app.cameraActive && !app.wakeSentinel) requestWakeLock();
}

document.addEventListener('visibilitychange', updateVisibilityWarning);
window.addEventListener('hashchange', applyHashRoute);

boot();
