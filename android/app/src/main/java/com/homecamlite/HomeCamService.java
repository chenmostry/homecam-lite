package com.homecamlite;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.webrtc.AudioSource;
import org.webrtc.AudioTrack;
import org.webrtc.Camera1Enumerator;
import org.webrtc.Camera2Enumerator;
import org.webrtc.CameraEnumerator;
import org.webrtc.CameraVideoCapturer;
import org.webrtc.DataChannel;
import org.webrtc.DefaultVideoDecoderFactory;
import org.webrtc.DefaultVideoEncoderFactory;
import org.webrtc.EglBase;
import org.webrtc.IceCandidate;
import org.webrtc.MediaConstraints;
import org.webrtc.PeerConnection;
import org.webrtc.PeerConnectionFactory;
import org.webrtc.RtpParameters;
import org.webrtc.RtpSender;
import org.webrtc.RtpTransceiver;
import org.webrtc.SdpObserver;
import org.webrtc.SessionDescription;
import org.webrtc.SurfaceTextureHelper;
import org.webrtc.VideoSource;
import org.webrtc.VideoTrack;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.TimeUnit;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/** User-started foreground camera service. It never starts at boot or hides its notification. */
public final class HomeCamService extends Service {
    public static final String ACTION_START = "com.homecamlite.action.START";
    public static final String ACTION_STOP = "com.homecamlite.action.STOP";
    public static final String EXTRA_ORIGIN = "origin";
    public static final String EXTRA_DEVICE_ID = "device_id";
    public static final String EXTRA_AUDIO = "audio";
    public static final String EXTRA_FRONT_CAMERA = "front_camera";
    public static final String EXTRA_HIGH_QUALITY = "high_quality";

    private static final String TAG = "HomeCamService";
    private static final String CHANNEL_ID = "homecam_camera";
    private static final int NOTIFICATION_ID = 2801;
    private static final int WS_MAX_MESSAGE_CHARS = 64 * 1024;
    private static final long WAKE_LOCK_MAX_MS = TimeUnit.HOURS.toMillis(6);
    private static final long WAKE_LOCK_RENEW_MS = TimeUnit.HOURS.toMillis(5);
    private static volatile boolean serviceActive;

    private final Handler mainHandler = new Handler(android.os.Looper.getMainLooper());
    private HandlerThread workerThread;
    private Handler worker;
    private volatile boolean foreground;
    private volatile boolean active;
    private String origin;
    private String deviceId;
    private String token;
    private boolean audioEnabled;
    private boolean frontCamera;
    private boolean highQuality;

    private OkHttpClient httpClient;
    private OkHttpClient webSocketClient;
    private WebSocketSession socket;
    private long socketGeneration;
    private int reconnectAttempt;
    private Runnable reconnectTask;
    private Runnable authDeadlineTask;
    private Runnable monitorTask;
    private Runnable wakeRenewTask;

    private PeerSession peerSession;
    private PeerConnectionFactory factory;
    private EglBase eglBase;
    private CameraVideoCapturer capturer;
    private SurfaceTextureHelper surfaceTextureHelper;
    private VideoSource videoSource;
    private VideoTrack videoTrack;
    private AudioSource audioSource;
    private AudioTrack audioTrack;
    private PowerManager.WakeLock wakeLock;

    public static boolean isActive() {
        return serviceActive;
    }

    @Override public void onCreate() {
        super.onCreate();
        workerThread = new HandlerThread("HomeCamSerial");
        workerThread.start();
        worker = new Handler(workerThread.getLooper());
        createNotificationChannel();
        httpClient = new OkHttpClient.Builder()
                .followRedirects(false)
                .followSslRedirects(false)
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(15, TimeUnit.SECONDS)
                .callTimeout(20, TimeUnit.SECONDS)
                .build();
        webSocketClient = httpClient.newBuilder()
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .pingInterval(20, TimeUnit.SECONDS)
                .build();
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            if (worker != null) worker.post(() -> stopRuntime("已停止"));
            else {
                serviceActive = false;
                DeviceStore.setStatus(this, "已停止");
                stopSelf(startId);
            }
            return START_NOT_STICKY;
        }
        if (intent == null || !ACTION_START.equals(intent.getAction())) {
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        if (serviceActive) return START_NOT_STICKY;

        origin = intent.getStringExtra(EXTRA_ORIGIN);
        deviceId = intent.getStringExtra(EXTRA_DEVICE_ID);
        audioEnabled = intent.getBooleanExtra(EXTRA_AUDIO, false)
                && Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
        frontCamera = intent.getBooleanExtra(EXTRA_FRONT_CAMERA, false);
        highQuality = intent.getBooleanExtra(EXTRA_HIGH_QUALITY, false);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            DeviceStore.setStatus(this, "摄像头权限未授予，无法启动");
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, buildNotification("正在启动摄像头…"), foregroundTypes());
            } else {
                startForeground(NOTIFICATION_ID, buildNotification("正在启动摄像头…"));
            }
            foreground = true;
            serviceActive = true;
            DeviceStore.setStatus(this, "正在启动摄像头…");
            worker.post(this::startRuntime);
        } catch (RuntimeException ex) {
            serviceActive = false;
            DeviceStore.setStatus(this, "系统未能启动前台摄像服务：" + safeMessage(ex));
            stopSelf(startId);
        }
        return START_NOT_STICKY;
    }

    private int foregroundTypes() {
        if (Build.VERSION.SDK_INT < 29) return 0;
        int types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
        if (audioEnabled && Build.VERSION.SDK_INT >= 30) {
            types |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
        }
        return types;
    }

    private void startRuntime() {
        if (active) return;
        active = true;
        try {
            if (origin == null || deviceId == null) throw new IllegalStateException("配对信息不完整。");
            token = DeviceStore.getToken(this, origin, deviceId);
            if (token == null) {
                DeviceStore.clearPairing(this);
                stopRuntime("设备配对已失效，请重新配对");
                return;
            }
            acquireWakeLock();
            initializeMedia();
            updateStatus("摄像头已开启，正在连接服务器…");
            openWebSocket();
            scheduleMonitor();
        } catch (Exception ex) {
            Log.e(TAG, "Unable to start camera", ex);
            stopRuntime("摄像头启动失败：" + safeMessage(ex));
        }
    }

    private void initializeMedia() throws Exception {
        synchronized (HomeCamService.class) {
            if (!webrtcInitialized) {
                PeerConnectionFactory.initialize(
                        PeerConnectionFactory.InitializationOptions.builder(getApplicationContext())
                                .createInitializationOptions());
                webrtcInitialized = true;
            }
        }
        eglBase = EglBase.create();
        factory = PeerConnectionFactory.builder()
                .setVideoEncoderFactory(new DefaultVideoEncoderFactory(eglBase.getEglBaseContext(), true, true))
                .setVideoDecoderFactory(new DefaultVideoDecoderFactory(eglBase.getEglBaseContext()))
                .createPeerConnectionFactory();

        videoSource = factory.createVideoSource(false);
        surfaceTextureHelper = SurfaceTextureHelper.create("HomeCamCameraCapture", eglBase.getEglBaseContext());
        if (surfaceTextureHelper == null) throw new IllegalStateException("无法创建摄像头纹理线程。");
        capturer = createCameraCapturer(frontCamera);
        if (capturer == null) throw new IllegalStateException("未找到所选方向的摄像头。");
        capturer.initialize(surfaceTextureHelper, getApplicationContext(), videoSource.getCapturerObserver());
        videoTrack = factory.createVideoTrack("homecam-video", videoSource);
        videoTrack.setEnabled(true);
        capturer.startCapture(highQuality ? 1280 : 640, highQuality ? 720 : 480, highQuality ? 15 : 12);

        if (audioEnabled && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            audioSource = factory.createAudioSource(new MediaConstraints());
            audioTrack = factory.createAudioTrack("homecam-audio", audioSource);
            audioTrack.setEnabled(true);
        } else {
            audioEnabled = false;
        }
    }

    private static boolean webrtcInitialized;

    private CameraVideoCapturer createCameraCapturer(boolean wantFront) {
        CameraEnumerator enumerator;
        try {
            enumerator = Camera2Enumerator.isSupported(this)
                    ? new Camera2Enumerator(this)
                    : new Camera1Enumerator(true);
        } catch (RuntimeException ex) {
            enumerator = new Camera1Enumerator(true);
        }
        String selected = null;
        String first = null;
        for (String name : enumerator.getDeviceNames()) {
            if (first == null) first = name;
            if (wantFront && enumerator.isFrontFacing(name)) {
                selected = name;
                break;
            }
            if (!wantFront && enumerator.isBackFacing(name)) {
                selected = name;
                break;
            }
        }
        if (selected == null) selected = first;
        if (selected == null) return null;
        return enumerator.createCapturer(selected, new CameraVideoCapturer.CameraEventsHandler() {
            @Override public void onCameraError(String errorDescription) {
                postWorker(() -> stopRuntime("摄像头错误：" + errorDescription));
            }

            @Override public void onCameraDisconnected() {
                postWorker(() -> stopRuntime("摄像头已断开。"));
            }

            @Override public void onCameraFreezed(String errorDescription) {
                postWorker(() -> stopRuntime("摄像头画面停止：" + errorDescription));
            }

            @Override public void onCameraOpening(String cameraName) {
                postWorker(() -> updateStatus("正在打开摄像头…"));
            }

            @Override public void onFirstFrameAvailable() {
                postWorker(() -> updateStatus("摄像头已开启，等待观看者…"));
            }

            @Override public void onCameraClosed() {
                // Expected during an explicit stop or camera switch.
            }
        });
    }

    private void openWebSocket() {
        if (!active || socket != null) return;
        final WebSocketSession session = new WebSocketSession(++socketGeneration);
        String webSocketUrl = "wss://" + origin.substring("https://".length()) + "/ws";
        Request request = new Request.Builder()
                .url(webSocketUrl)
                .header("Origin", origin)
                .build();
        socket = session;
        session.webSocket = webSocketClient.newWebSocket(request, new WebSocketListener() {
            @Override public void onOpen(WebSocket webSocket, Response response) {
                postWorker(() -> onSocketOpen(session));
            }

            @Override public void onMessage(WebSocket webSocket, String text) {
                postWorker(() -> onSocketMessage(session, text));
            }

            @Override public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                int code = response == null ? -1 : response.code();
                postWorker(() -> onSocketFailure(session, code, safeMessage(t)));
            }

            @Override public void onClosing(WebSocket webSocket, int code, String reason) {
                postWorker(() -> onSocketClosing(session, code));
            }

            @Override public void onClosed(WebSocket webSocket, int code, String reason) {
                postWorker(() -> onSocketFailure(session, code, reason));
            }
        });
    }

    private void onSocketOpen(WebSocketSession session) {
        if (!isCurrent(session)) return;
        try {
            JSONObject auth = new JSONObject()
                    .put("type", "auth")
                    .put("role", "camera")
                    .put("device_id", deviceId)
                    .put("device_token", token);
            if (!sendJson(session, auth)) {
                failSocket(session, -1, "无法发送设备认证信息");
                return;
            }
            authDeadlineTask = () -> {
                if (isCurrent(session) && !session.authenticated) {
                    failSocket(session, -1, "服务器未在 5 秒内完成设备认证");
                }
            };
            worker.postDelayed(authDeadlineTask, 5000);
        } catch (JSONException ex) {
            stopRuntime("无法准备设备认证信息。");
        }
    }

    private void onSocketMessage(WebSocketSession session, String text) {
        if (!isCurrent(session)) return;
        if (text == null || text.length() > WS_MAX_MESSAGE_CHARS
                || text.getBytes(java.nio.charset.StandardCharsets.UTF_8).length > WS_MAX_MESSAGE_CHARS) {
            failSocket(session, 1009, "服务器消息超过 64 KiB 限制");
            return;
        }
        try {
            JSONObject message = new JSONObject(text);
            String type = message.optString("type", "");
            switch (type) {
                case "ready":
                    if (!"camera".equals(message.optString("role", ""))) {
                        fatalAuthentication("服务器未确认摄像端身份。", true);
                        return;
                    }
                    session.authenticated = true;
                    if (authDeadlineTask != null) worker.removeCallbacks(authDeadlineTask);
                    authDeadlineTask = null;
                    reconnectAttempt = 0;
                    updateStatus("已连接服务器，等待观看者…");
                    break;
                case "viewer-joined":
                    if (!session.authenticated) {
                        fatalAuthentication("服务器在认证前发送了观看请求。", true);
                        return;
                    }
                    String peerId = message.optString("peer_id", "");
                    String sessionId = message.optString("session_id", "");
                    if (peerId.isEmpty() || sessionId.isEmpty()) break;
                    startPeer(peerId, sessionId);
                    break;
                case "viewer-left":
                    if (peerSession != null
                            && peerSession.peerId.equals(message.optString("peer_id", ""))
                            && peerSession.sessionId.equals(message.optString("session_id", ""))) {
                        closePeerSession();
                        updateStatus("观看已结束，等待下一位观看者…");
                    }
                    break;
                case "signal":
                    handleSignal(session, message);
                    break;
                case "error":
                    if (!session.authenticated) {
                        fatalAuthentication("服务器拒绝了设备认证。", true);
                    } else {
                        Log.w(TAG, "Signaling error received from server");
                        updateStatus("服务器拒绝了当前信令请求。");
                    }
                    break;
                default:
                    // Ignore unrelated control events such as viewer presence lists.
                    break;
            }
        } catch (JSONException ex) {
            failSocket(session, 1007, "服务器返回的消息格式无效");
        }
    }

    private void onSocketClosing(WebSocketSession session, int code) {
        if (code == 1008 && isCurrent(session)) {
            fatalAuthentication("服务器已吊销或拒绝此设备。请重新配对。", true);
        }
    }

    private void onSocketFailure(WebSocketSession session, int code, String reason) {
        if (!isCurrent(session)) return;
        if (code == 1008 || code == 401 || code == 403) {
            fatalAuthentication("设备认证已失效或被撤销，请重新配对。", true);
            return;
        }
        if (authDeadlineTask != null) worker.removeCallbacks(authDeadlineTask);
        authDeadlineTask = null;
        socket = null;
        session.closed = true;
        closePeerSession();
        updateStatus("服务器连接中断，正在重连…");
        scheduleReconnect();
    }

    private void failSocket(WebSocketSession session, int code, String reason) {
        if (!isCurrent(session)) return;
        if (session.webSocket != null) session.webSocket.close(code >= 1000 && code <= 4999 ? code : 1001, reason);
        if (code == 1008 || code == 401 || code == 403) {
            fatalAuthentication("设备认证已失效或被撤销，请重新配对。", true);
        } else {
            onSocketFailure(session, code, reason);
        }
    }

    private void fatalAuthentication(String message, boolean clearCredentials) {
        if (clearCredentials) DeviceStore.clearPairing(this);
        stopRuntime(message);
    }

    private boolean isCurrent(WebSocketSession session) {
        return active && socket == session && !session.closed;
    }

    private boolean sendJson(WebSocketSession session, JSONObject object) {
        return isCurrent(session) && session.webSocket != null && session.webSocket.send(object.toString());
    }

    private void scheduleReconnect() {
        if (!active || reconnectTask != null) return;
        int exponent = Math.min(reconnectAttempt++, 6);
        long delaySeconds = Math.min(60, 1L << exponent);
        reconnectTask = () -> {
            reconnectTask = null;
            if (active && socket == null) openWebSocket();
        };
        worker.postDelayed(reconnectTask, TimeUnit.SECONDS.toMillis(delaySeconds));
    }

    private void startPeer(String peerId, String sessionId) {
        if (peerSession != null && peerSession.peerId.equals(peerId)
                && peerSession.sessionId.equals(sessionId)) return;
        closePeerSession();
        peerSession = new PeerSession(peerId, sessionId);
        updateStatus("有观看者接入，正在取得 TURN 配置…");
        fetchIce(peerSession);
    }

    private void fetchIce(PeerSession wanted) {
        if (!active || peerSession != wanted || socket == null || !socket.authenticated) return;
        Request request = new Request.Builder()
                .url(origin + "/api/ice")
                .header("Origin", origin)
                .header("Authorization", "Bearer " + token)
                .get()
                .build();
        httpClient.newCall(request).enqueue(new Callback() {
            @Override public void onFailure(Call call, IOException e) {
                postWorker(() -> retryIce(wanted, "暂时无法取得 TURN 配置"));
            }

            @Override public void onResponse(Call call, Response response) {
                try (Response closeable = response) {
                    int code = closeable.code();
                    if (code == 401 || code == 403) {
                        postWorker(() -> fatalAuthentication("设备认证已失效或被撤销，请重新配对。", true));
                        return;
                    }
                    if (!closeable.isSuccessful()) {
                        postWorker(() -> retryIce(wanted, "服务器暂时无法提供 TURN 配置"));
                        return;
                    }
                    ResponseBody body = closeable.body();
                    if (body == null) {
                        postWorker(() -> retryIce(wanted, "TURN 配置为空"));
                        return;
                    }
                    String jsonText = readBoundedBody(body, WS_MAX_MESSAGE_CHARS);
                    List<PeerConnection.IceServer> servers = parseIceServers(jsonText);
                    postWorker(() -> createPeerConnection(wanted, servers));
                } catch (Exception ex) {
                    postWorker(() -> retryIce(wanted, "TURN 配置无效"));
                }
            }
        });
    }

    private List<PeerConnection.IceServer> parseIceServers(String jsonText) throws JSONException {
        JSONObject envelope = new JSONObject(jsonText);
        JSONArray entries = envelope.getJSONArray("ice_servers");
        List<PeerConnection.IceServer> result = new ArrayList<>();
        boolean hasRelay = false;
        for (int i = 0; i < entries.length(); i++) {
            JSONObject entry = entries.optJSONObject(i);
            if (entry == null) continue;
            String username = entry.optString("username", "");
            String credential = entry.optString("credential", "");
            JSONArray urls = entry.optJSONArray("urls");
            if (urls == null) {
                String single = entry.optString("urls", "");
                if (!single.isEmpty()) {
                    result.add(PeerConnection.IceServer.builder(single)
                            .setUsername(username).setPassword(credential).createIceServer());
                    if (single.startsWith("turn:") || single.startsWith("turns:")) hasRelay = true;
                }
                continue;
            }
            for (int j = 0; j < urls.length(); j++) {
                String url = urls.optString(j, "");
                if (url.isEmpty()) continue;
                result.add(PeerConnection.IceServer.builder(url)
                        .setUsername(username).setPassword(credential).createIceServer());
                if (url.startsWith("turn:") || url.startsWith("turns:")) hasRelay = true;
            }
        }
        if (result.isEmpty() || !hasRelay) throw new JSONException("Relay ICE server is missing");
        return result;
    }

    private String readBoundedBody(ResponseBody body, int maxBytes) throws IOException {
        java.io.InputStream input = body.byteStream();
        java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int total = 0;
        int count;
        while ((count = input.read(buffer)) != -1) {
            total += count;
            if (total > maxBytes) throw new IOException("Response exceeded size limit");
            output.write(buffer, 0, count);
        }
        return output.toString(java.nio.charset.StandardCharsets.UTF_8.name());
    }

    private void retryIce(PeerSession wanted, String message) {
        if (!active || peerSession != wanted) return;
        int exponent = Math.min(wanted.iceRetryAttempt++, 5);
        long delaySeconds = Math.min(30, 1L << exponent);
        int nextAttempt = wanted.iceRetryAttempt;
        closePeerSession();
        PeerSession retry = new PeerSession(wanted.peerId, wanted.sessionId);
        retry.iceRetryAttempt = nextAttempt;
        peerSession = retry;
        updateStatus(message + "，稍後重試…");
        retry.iceRetryTask = () -> {
            retry.iceRetryTask = null;
            fetchIce(retry);
        };
        worker.postDelayed(retry.iceRetryTask, TimeUnit.SECONDS.toMillis(delaySeconds));
    }

    private void createPeerConnection(PeerSession wanted, List<PeerConnection.IceServer> servers) {
        if (!active || peerSession != wanted || socket == null || !socket.authenticated) return;
        try {
            PeerConnection.RTCConfiguration configuration = new PeerConnection.RTCConfiguration(servers);
            configuration.iceTransportsType = PeerConnection.IceTransportsType.RELAY;
            configuration.sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN;
            configuration.bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE;
            PeerConnection pc = factory.createPeerConnection(configuration, new PeerConnection.Observer() {
                @Override public void onSignalingChange(PeerConnection.SignalingState state) {}
                @Override public void onIceConnectionChange(PeerConnection.IceConnectionState state) {
                    postWorker(() -> {
                        if (!isCurrentPeer(wanted)) return;
                        if (state == PeerConnection.IceConnectionState.CONNECTED
                                || state == PeerConnection.IceConnectionState.COMPLETED) {
                            updateStatus(audioEnabled
                                    ? "视频已连接，正在向观看者发送画面和声音。"
                                    : "视频已连接，正在向观看者发送画面。");
                        } else if (state == PeerConnection.IceConnectionState.CHECKING) {
                            updateStatus("正在建立视频连接…");
                        } else if (state == PeerConnection.IceConnectionState.DISCONNECTED
                                || state == PeerConnection.IceConnectionState.FAILED) {
                            updateStatus("视频连接中断，等待观看端重连…");
                        }
                    });
                }
                @Override public void onIceConnectionReceivingChange(boolean receiving) {}
                @Override public void onIceGatheringChange(PeerConnection.IceGatheringState state) {}
                @Override public void onIceCandidate(IceCandidate candidate) {
                    postWorker(() -> onLocalIceCandidate(wanted, candidate));
                }
                @Override public void onIceCandidatesRemoved(IceCandidate[] candidates) {}
                @Override public void onAddStream(org.webrtc.MediaStream stream) {}
                @Override public void onRemoveStream(org.webrtc.MediaStream stream) {}
                @Override public void onDataChannel(DataChannel channel) { channel.close(); }
                @Override public void onRenegotiationNeeded() {}
                @Override public void onAddTrack(org.webrtc.RtpReceiver receiver,
                                                  org.webrtc.MediaStream[] streams) {}
                @Override public void onTrack(RtpTransceiver transceiver) {}
            });
            if (pc == null) throw new IllegalStateException("无法创建 WebRTC 连接。");
            wanted.pc = pc;
            List<String> streamIds = Collections.singletonList("homecam-stream");
            RtpSender videoSender = pc.addTrack(videoTrack, streamIds);
            applyVideoLimits(videoSender, highQuality ? 800_000 : 450_000, highQuality ? 15 : 12);
            if (audioEnabled && audioTrack != null) pc.addTrack(audioTrack, streamIds);
            updateStatus("正在生成视频连接…");
            pc.createOffer(new SdpObserver() {
                @Override public void onCreateSuccess(SessionDescription description) {
                    postWorker(() -> setLocalOffer(wanted, description));
                }
                @Override public void onSetSuccess() {}
                @Override public void onCreateFailure(String error) {
                    postWorker(() -> retryIce(wanted, "创建视频 offer 失败"));
                }
                @Override public void onSetFailure(String error) {
                    postWorker(() -> retryIce(wanted, "设置本地视频描述失败"));
                }
            }, new MediaConstraints());
        } catch (Exception ex) {
            Log.e(TAG, "Unable to create peer connection", ex);
            retryIce(wanted, "无法建立视频连接");
        }
    }

    private void applyVideoLimits(RtpSender sender, int maxBitrate, int maxFps) {
        if (sender == null) return;
        try {
            RtpParameters parameters = sender.getParameters();
            for (RtpParameters.Encoding encoding : parameters.encodings) {
                encoding.maxBitrateBps = maxBitrate;
                encoding.maxFramerate = maxFps;
            }
            sender.setParameters(parameters);
        } catch (RuntimeException ex) {
            Log.w(TAG, "Could not apply video bitrate cap", ex);
        }
    }

    private void setLocalOffer(PeerSession wanted, SessionDescription description) {
        if (!isCurrentPeer(wanted)) return;
        wanted.pc.setLocalDescription(new SdpObserver() {
            @Override public void onCreateSuccess(SessionDescription ignored) {}
            @Override public void onSetSuccess() {
                postWorker(() -> sendOfferAndLocalCandidates(wanted, description));
            }
            @Override public void onCreateFailure(String error) {}
            @Override public void onSetFailure(String error) {
                postWorker(() -> retryIce(wanted, "应用本地视频描述失败"));
            }
        }, description);
    }

    private void sendOfferAndLocalCandidates(PeerSession wanted, SessionDescription description) {
        if (!isCurrentPeer(wanted) || wanted.offerSent) return;
        try {
            JSONObject sdp = new JSONObject()
                    .put("type", description.type.canonicalForm())
                    .put("sdp", description.description);
            JSONObject data = new JSONObject().put("type", "offer").put("sdp", sdp);
            if (!sendSignal(wanted, data)) return;
            wanted.offerSent = true;
            while (!wanted.localCandidates.isEmpty()) {
                sendLocalCandidate(wanted, wanted.localCandidates.remove(0));
            }
        } catch (JSONException ex) {
            peerSetupFailed(wanted, "无法编码视频 offer");
        }
    }

    private void onLocalIceCandidate(PeerSession wanted, IceCandidate candidate) {
        if (!isCurrentPeer(wanted)) return;
        if (!wanted.offerSent) {
            wanted.localCandidates.add(candidate);
        } else {
            sendLocalCandidate(wanted, candidate);
        }
    }

    private void sendLocalCandidate(PeerSession wanted, IceCandidate candidate) {
        try {
            JSONObject init = new JSONObject()
                    .put("candidate", candidate.sdp)
                    .put("sdpMid", candidate.sdpMid == null ? JSONObject.NULL : candidate.sdpMid)
                    .put("sdpMLineIndex", candidate.sdpMLineIndex);
            sendSignal(wanted, new JSONObject().put("type", "ice").put("candidate", init));
        } catch (JSONException ex) {
            Log.w(TAG, "Unable to encode local ICE candidate", ex);
        }
    }

    private boolean sendSignal(PeerSession wanted, JSONObject data) {
        if (!isCurrentPeer(wanted) || socket == null || !socket.authenticated) return false;
        try {
            JSONObject message = new JSONObject()
                    .put("type", "signal")
                    .put("target", wanted.peerId)
                    .put("session_id", wanted.sessionId)
                    .put("data", data);
            return sendJson(socket, message);
        } catch (JSONException ex) {
            return false;
        }
    }

    private void handleSignal(WebSocketSession session, JSONObject message) throws JSONException {
        if (!session.authenticated || peerSession == null || peerSession.pc == null) return;
        PeerSession current = peerSession;
        if (!current.peerId.equals(message.optString("source", ""))
                || !current.sessionId.equals(message.optString("session_id", ""))) return;
        JSONObject data = message.optJSONObject("data");
        if (data == null) return;
        String type = data.optString("type", "");
        if ("answer".equals(type)) {
            JSONObject sdp = data.optJSONObject("sdp");
            if (sdp == null) return;
            String sdpType = sdp.optString("type", "answer");
            if (!"answer".equals(sdpType)) return;
            SessionDescription remote = new SessionDescription(
                    SessionDescription.Type.fromCanonicalForm(sdpType), sdp.optString("sdp", ""));
            current.pc.setRemoteDescription(new SdpObserver() {
                @Override public void onCreateSuccess(SessionDescription ignored) {}
                @Override public void onSetSuccess() {
                    postWorker(() -> {
                        if (!isCurrentPeer(current)) return;
                        current.remoteDescriptionSet = true;
                        while (!current.remoteCandidates.isEmpty()) {
                            current.pc.addIceCandidate(current.remoteCandidates.remove(0));
                        }
                    });
                }
                @Override public void onCreateFailure(String error) {}
                @Override public void onSetFailure(String error) {
                    postWorker(() -> peerSetupFailed(current, "应用观看端视频描述失败"));
                }
            }, remote);
        } else if ("ice".equals(type)) {
            JSONObject candidate = data.optJSONObject("candidate");
            if (candidate == null) return;
            String candidateText = candidate.optString("candidate", "");
            if (candidateText.isEmpty()) return;
            String mid = candidate.isNull("sdpMid") ? null : candidate.optString("sdpMid", null);
            int line = candidate.optInt("sdpMLineIndex", 0);
            IceCandidate parsed = new IceCandidate(mid, line, candidateText);
            if (current.remoteDescriptionSet) current.pc.addIceCandidate(parsed);
            else current.remoteCandidates.add(parsed);
        }
    }

    private void peerSetupFailed(PeerSession wanted, String message) {
        if (!isCurrentPeer(wanted)) return;
        updateStatus(message + "，等待观看端重新连接…");
        closePeerSession();
    }

    private boolean isCurrentPeer(PeerSession wanted) {
        return active && peerSession == wanted && wanted.pc != null;
    }

    private void closePeerSession() {
        PeerSession old = peerSession;
        peerSession = null;
        if (old == null) return;
        if (old.iceRetryTask != null && worker != null) worker.removeCallbacks(old.iceRetryTask);
        old.iceRetryTask = null;
        discardPeerConnection(old);
    }

    private void discardPeerConnection(PeerSession session) {
        if (session == null) return;
        if (session.pc != null) {
            try { session.pc.close(); } catch (RuntimeException ignored) {}
            try { session.pc.dispose(); } catch (RuntimeException ignored) {}
            session.pc = null;
        }
        session.remoteCandidates.clear();
        session.localCandidates.clear();
        session.offerSent = false;
        session.remoteDescriptionSet = false;
    }

    private void scheduleMonitor() {
        monitorTask = new Runnable() {
            @Override public void run() {
                if (!active) return;
                if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                    stopRuntime("攝像頭權限已撤銷，攝像服務已停止。");
                    return;
                }
                if (audioEnabled && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    audioEnabled = false;
                    if (audioTrack != null) audioTrack.setEnabled(false);
                    updateStatus("麥克風權限已撤銷，已靜音；攝像仍在運行。");
                }
                if (videoTrack != null && videoTrack.state() == org.webrtc.MediaStreamTrack.State.ENDED) {
                    stopRuntime("攝像頭畫面已結束，攝像服務已停止。");
                    return;
                }
                worker.postDelayed(this, 2000);
            }
        };
        worker.postDelayed(monitorTask, 2000);
    }

    private void acquireWakeLock() {
        PowerManager powerManager = (PowerManager) getSystemService(POWER_SERVICE);
        if (powerManager == null) return;
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "HomeCamLite:camera-stream");
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire(WAKE_LOCK_MAX_MS);
        wakeRenewTask = new Runnable() {
            @Override public void run() {
                if (!active || wakeLock == null) return;
                try {
                    if (wakeLock.isHeld()) wakeLock.release();
                    wakeLock.acquire(WAKE_LOCK_MAX_MS);
                    worker.postDelayed(this, WAKE_LOCK_RENEW_MS);
                } catch (RuntimeException ex) {
                    Log.w(TAG, "Wake lock could not be renewed", ex);
                }
            }
        };
        worker.postDelayed(wakeRenewTask, WAKE_LOCK_RENEW_MS);
    }

    private void scheduleReconnectAfterStop() {
        if (reconnectTask != null && worker != null) worker.removeCallbacks(reconnectTask);
        reconnectTask = null;
    }

    private void stopRuntime(String finalStatus) {
        if (!active && !foreground) {
            serviceActive = false;
            DeviceStore.setStatus(this, finalStatus);
            stopSelf();
            return;
        }
        active = false;
        serviceActive = false;
        scheduleReconnectAfterStop();
        if (authDeadlineTask != null && worker != null) worker.removeCallbacks(authDeadlineTask);
        if (monitorTask != null && worker != null) worker.removeCallbacks(monitorTask);
        if (wakeRenewTask != null && worker != null) worker.removeCallbacks(wakeRenewTask);
        authDeadlineTask = null;
        monitorTask = null;
        wakeRenewTask = null;
        if (socket != null) {
            WebSocketSession old = socket;
            socket = null;
            old.closed = true;
            if (old.webSocket != null) {
                old.webSocket.close(1000, "camera stopped");
                old.webSocket.cancel();
            }
        }
        closePeerSession();
        releaseMedia();
        releaseWakeLock();
        token = null;
        DeviceStore.setStatus(this, finalStatus);
        mainHandler.post(() -> {
            if (foreground) {
                stopForeground(STOP_FOREGROUND_REMOVE);
                foreground = false;
            }
            stopSelf();
        });
    }

    private void releaseMedia() {
        if (capturer != null) {
            try { capturer.stopCapture(); } catch (InterruptedException ex) { Thread.currentThread().interrupt(); }
            catch (RuntimeException ex) { Log.w(TAG, "Camera stop failed", ex); }
            try { capturer.dispose(); } catch (RuntimeException ignored) {}
            capturer = null;
        }
        if (videoTrack != null) {
            try { videoTrack.setEnabled(false); videoTrack.dispose(); } catch (RuntimeException ignored) {}
            videoTrack = null;
        }
        if (audioTrack != null) {
            try { audioTrack.setEnabled(false); audioTrack.dispose(); } catch (RuntimeException ignored) {}
            audioTrack = null;
        }
        if (videoSource != null) { try { videoSource.dispose(); } catch (RuntimeException ignored) {} videoSource = null; }
        if (audioSource != null) { try { audioSource.dispose(); } catch (RuntimeException ignored) {} audioSource = null; }
        if (surfaceTextureHelper != null) {
            try { surfaceTextureHelper.dispose(); } catch (RuntimeException ignored) {}
            surfaceTextureHelper = null;
        }
        if (factory != null) { try { factory.dispose(); } catch (RuntimeException ignored) {} factory = null; }
        if (eglBase != null) { try { eglBase.release(); } catch (RuntimeException ignored) {} eglBase = null; }
    }

    private void releaseWakeLock() {
        if (wakeLock != null) {
            try { if (wakeLock.isHeld()) wakeLock.release(); } catch (RuntimeException ignored) {}
            wakeLock = null;
        }
    }

    private void updateStatus(String status) {
        DeviceStore.setStatus(this, status);
        if (foreground) mainHandler.post(() -> {
            if (foreground) {
                NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (manager != null) manager.notify(NOTIFICATION_ID, buildNotification(status));
            }
        });
    }

    private Notification buildNotification(String status) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent content = PendingIntent.getActivity(this, NOTIFICATION_ID, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, HomeCamService.class).setAction(ACTION_STOP);
        PendingIntent stopIntent = PendingIntent.getService(this, NOTIFICATION_ID + 1, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification_camera)
                .setContentTitle("旧机看家 · " + DeviceStore.getDeviceName(this))
                .setContentText(status)
                .setStyle(new Notification.BigTextStyle().bigText(status))
                .setCategory(Notification.CATEGORY_SERVICE)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(content)
                .addAction(new Notification.Action.Builder(R.drawable.ic_notification_camera, "停止摄像", stopIntent).build());
        return builder.build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL_ID,
                    "看护摄像服务", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("显示摄像服务状态，并提供立即停止操作。");
            channel.setShowBadge(false);
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) manager.createNotificationChannel(channel);
        }
    }

    private void postWorker(Runnable task) {
        Handler handler = worker;
        if (handler != null) handler.post(task);
    }

    private String safeMessage(Throwable error) {
        String message = error == null ? null : error.getMessage();
        if (message == null || message.trim().isEmpty()) return "未知错误";
        return message.length() > 120 ? message.substring(0, 120) : message;
    }

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public void onDestroy() {
        if (active || foreground) {
            Handler handler = worker;
            if (handler != null) handler.post(() -> stopRuntime("已停止"));
            else {
                active = false;
                serviceActive = false;
                DeviceStore.setStatus(this, "已停止");
                releaseMedia();
                releaseWakeLock();
            }
        }
        serviceActive = false;
        if (workerThread != null) workerThread.quitSafely();
        super.onDestroy();
    }

    private static final class WebSocketSession {
        final long generation;
        WebSocket webSocket;
        boolean authenticated;
        boolean closed;
        WebSocketSession(long generation) { this.generation = generation; }
    }

    private static final class PeerSession {
        final String peerId;
        final String sessionId;
        final List<IceCandidate> localCandidates = new ArrayList<>();
        final List<IceCandidate> remoteCandidates = new ArrayList<>();
        PeerConnection pc;
        boolean offerSent;
        boolean remoteDescriptionSet;
        int iceRetryAttempt;
        Runnable iceRetryTask;
        PeerSession(String peerId, String sessionId) {
            this.peerId = peerId;
            this.sessionId = sessionId;
        }
    }
}
