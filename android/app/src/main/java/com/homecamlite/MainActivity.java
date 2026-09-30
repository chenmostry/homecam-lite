package com.homecamlite;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** Small visible, user-controlled setup screen for pairing and starting camera streaming. */
public final class MainActivity extends Activity {
    private static final int REQUEST_START_PERMISSIONS = 41;
    private static final Pattern PAIR_CODE = Pattern.compile("[A-Z0-9]{8}");
    private static final MediaType JSON_MEDIA = MediaType.parse("application/json; charset=utf-8");

    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private EditText serverField;
    private EditText codeField;
    private TextView pairingStatus;
    private TextView serviceStatus;
    private CheckBox audioBox;
    private RadioGroup lensGroup;
    private Spinner qualitySpinner;
    private String requestedOrigin;

    private final Runnable statusRefresh = new Runnable() {
        @Override public void run() {
            if (serviceStatus != null) {
                serviceStatus.setText("摄像状态：" + DeviceStore.getStatus(MainActivity.this));
            }
            main.postDelayed(this, 1000);
        }
    };

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // The saved status is presentation text, not evidence that a service still exists.
        // A process restart clears the live service flag; discard any stale "running" label.
        if (!HomeCamService.isActive()) DeviceStore.setStatus(this, "已停止");
        buildUi();
        String savedOrigin = DeviceStore.getOrigin(this);
        if (savedOrigin != null) serverField.setText(savedOrigin);
        main.post(statusRefresh);
    }

    @Override protected void onDestroy() {
        main.removeCallbacks(statusRefresh);
        network.shutdown();
        super.onDestroy();
    }

    private void buildUi() {
        ScrollView scroll = new ScrollView(this);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        page.setPadding(pad, pad, pad, pad);
        scroll.addView(page);

        TextView title = new TextView(this);
        title.setText("旧机看家");
        title.setTextSize(26);
        page.addView(title, matchWrap());

        addText(page, "只配对你信任的 HTTPS 服务器。摄像头由你手动启动，运行时会显示前台通知；熄屏续流受手机厂商电源管理影响。", 16);
        addText(page, "服务器只填写源地址，例如 https://cam.example.com；不要加路径、参数或账号密码。配对码由服务器管理员生成，10 分钟内有效且只能使用一次。", 14);

        serverField = new EditText(this);
        serverField.setSingleLine(true);
        serverField.setHint("https://cam.example.com");
        serverField.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        page.addView(serverField, matchWrap());

        codeField = new EditText(this);
        codeField.setSingleLine(true);
        codeField.setHint("8 位配对码");
        codeField.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS);
        page.addView(codeField, matchWrap());

        Button pairButton = new Button(this);
        pairButton.setText("配对摄像设备");
        page.addView(pairButton, matchWrap());
        pairButton.setOnClickListener(v -> redeemPairing(pairButton));

        pairingStatus = addText(page, pairingDescription(), 14);

        addText(page, "摄像头", 18);
        lensGroup = new RadioGroup(this);
        lensGroup.setOrientation(RadioGroup.HORIZONTAL);
        RadioButton back = new RadioButton(this);
        back.setText("后置");
        back.setId(1);
        RadioButton front = new RadioButton(this);
        front.setText("前置");
        front.setId(2);
        lensGroup.addView(back, new RadioGroup.LayoutParams(0, dp(48), 1f));
        lensGroup.addView(front, new RadioGroup.LayoutParams(0, dp(48), 1f));
        back.setChecked(true);
        page.addView(lensGroup, matchWrap());

        addText(page, "画质", 18);
        qualitySpinner = new Spinner(this);
        qualitySpinner.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"标准 640×480 / 12 fps", "高清 1280×720 / 15 fps"}));
        page.addView(qualitySpinner, matchWrap());

        audioBox = new CheckBox(this);
        audioBox.setText("启用麦克风（可选；只发送声音给当前观看者）");
        page.addView(audioBox, matchWrap());

        addText(page, "Android 13 及以上会询问通知权限。允许后可在通知栏查看状态并停止；拒绝后前台服务仍会显示在系统的活动应用界面，但通知栏中可能不可见。麦克风权限只在勾选麦克风后申请。", 14);

        Button start = new Button(this);
        start.setText("开始看护");
        page.addView(start, matchWrap());
        start.setOnClickListener(v -> requestPermissionsAndStart());

        Button stop = new Button(this);
        stop.setText("停止摄像");
        page.addView(stop, matchWrap());
        stop.setOnClickListener(v -> stopCamera());

        serviceStatus = addText(page, "", 16);
        setContentView(scroll);
    }

    private void redeemPairing(Button pairButton) {
        final String origin;
        try {
            origin = ServerOrigin.parse(serverField.getText().toString());
        } catch (IllegalArgumentException ex) {
            serverField.setError(ex.getMessage());
            return;
        }
        String code = codeField.getText().toString().trim().toUpperCase(java.util.Locale.ROOT);
        if (!PAIR_CODE.matcher(code).matches()) {
            codeField.setError("请输入 8 位大写字母或数字配对码。");
            return;
        }
        if (HomeCamService.isActive()) {
            Toast.makeText(this, "请先停止当前摄像，再配对设备。", Toast.LENGTH_LONG).show();
            return;
        }
        pairButton.setEnabled(false);
        pairingStatus.setText("正在连接并兑换一次性配对码…");
        network.execute(() -> {
            try {
                JSONObject body = new JSONObject().put("code", code);
                Request request = new Request.Builder()
                        .url(origin + "/api/pairings/redeem")
                        .header("Origin", origin)
                        .post(RequestBody.create(body.toString(), JSON_MEDIA))
                        .build();
                OkHttpClient client = new OkHttpClient.Builder()
                        .followRedirects(false)
                        .followSslRedirects(false)
                        .callTimeout(20, java.util.concurrent.TimeUnit.SECONDS)
                        .build();
                try (Response response = client.newCall(request).execute()) {
                    String responseText = response.body() == null ? "{}" : response.body().string();
                    JSONObject json = new JSONObject(responseText);
                    if (!response.isSuccessful()) {
                        throw new IOException(json.optString("error", "配对失败，请检查服务器和配对码。"));
                    }
                    String deviceId = json.optString("device_id", "");
                    String token = json.optString("device_token", "");
                    String name = json.optString("name", "摄像设备");
                    if (deviceId.isEmpty() || token.isEmpty()) throw new IOException("服务器返回的配对信息不完整。");
                    DeviceStore.savePairing(getApplicationContext(), origin, deviceId, name, token);
                    main.post(() -> {
                        serverField.setText(origin);
                        codeField.setText("");
                        pairingStatus.setText("已配对：" + name + "\n设备令牌已加密保存在本机，不会保存管理员密码。");
                        Toast.makeText(this, "配对成功。准备好后手动开始摄像。", Toast.LENGTH_LONG).show();
                        pairButton.setEnabled(true);
                    });
                }
            } catch (Exception ex) {
                String message = ex.getMessage() == null ? "连接失败，请检查 HTTPS 服务器地址。" : ex.getMessage();
                main.post(() -> {
                    pairingStatus.setText("配对未完成：" + message);
                    pairButton.setEnabled(true);
                });
            }
        });
    }

    private void requestPermissionsAndStart() {
        final String origin;
        try {
            origin = ServerOrigin.parse(serverField.getText().toString());
        } catch (IllegalArgumentException ex) {
            serverField.setError(ex.getMessage());
            return;
        }
        String storedOrigin = DeviceStore.getOrigin(this);
        String deviceId = DeviceStore.getDeviceId(this);
        if (storedOrigin == null || deviceId == null || !storedOrigin.equals(origin)
                || DeviceStore.getToken(this, origin, deviceId) == null) {
            Toast.makeText(this, "此服务器尚未配对。若更改了服务器地址，请重新输入配对码。", Toast.LENGTH_LONG).show();
            return;
        }
        if (HomeCamService.isActive()) {
            Toast.makeText(this, "摄像服务已经运行。", Toast.LENGTH_SHORT).show();
            return;
        }
        requestedOrigin = origin;
        List<String> missing = new ArrayList<>();
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.CAMERA);
        }
        if (audioBox.isChecked() && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.RECORD_AUDIO);
        }
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        if (!missing.isEmpty()) {
            requestPermissions(missing.toArray(new String[0]), REQUEST_START_PERMISSIONS);
        } else {
            startCamera(origin);
        }
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_START_PERMISSIONS) return;
        boolean cameraGranted = checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
        if (!cameraGranted) {
            Toast.makeText(this, "需要允许摄像头权限才能开始看护。", Toast.LENGTH_LONG).show();
            return;
        }
        if (requestedOrigin != null) startCamera(requestedOrigin);
    }

    private void startCamera(String origin) {
        String deviceId = DeviceStore.getDeviceId(this);
        if (DeviceStore.getToken(this, origin, deviceId) == null) {
            Toast.makeText(this, "设备凭据无法读取，请重新配对。", Toast.LENGTH_LONG).show();
            return;
        }
        boolean microphone = audioBox.isChecked()
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
        if (audioBox.isChecked() && !microphone) {
            Toast.makeText(this, "麦克风权限未授予，将只发送视频。", Toast.LENGTH_LONG).show();
        }
        Intent intent = new Intent(this, HomeCamService.class)
                .setAction(HomeCamService.ACTION_START)
                .putExtra(HomeCamService.EXTRA_ORIGIN, origin)
                .putExtra(HomeCamService.EXTRA_DEVICE_ID, deviceId)
                .putExtra(HomeCamService.EXTRA_AUDIO, microphone)
                .putExtra(HomeCamService.EXTRA_FRONT_CAMERA, lensGroup.getCheckedRadioButtonId() == 2)
                .putExtra(HomeCamService.EXTRA_HIGH_QUALITY, qualitySpinner.getSelectedItemPosition() == 1);
        try {
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(intent);
            else startService(intent);
        } catch (RuntimeException ex) {
            String detail = ex.getMessage();
            if (detail == null || detail.trim().isEmpty()) detail = ex.getClass().getSimpleName();
            if (detail.length() > 120) detail = detail.substring(0, 120);
            String message = "系统未能启动前台摄像服务：" + detail;
            DeviceStore.setStatus(this, message);
            if (serviceStatus != null) serviceStatus.setText("摄像状态：" + message);
            Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        }
    }

    private void stopCamera() {
        Intent intent = new Intent(this, HomeCamService.class).setAction(HomeCamService.ACTION_STOP);
        startService(intent);
    }

    private String pairingDescription() {
        if (!DeviceStore.isPaired(this)) return "尚未配对。";
        return "已配对：" + DeviceStore.getDeviceName(this)
                + "\n绑定服务器：" + DeviceStore.getOrigin(this)
                + "\n设备令牌保存在 Android Keystore 加密的应用私有存储中。";
    }

    private TextView addText(LinearLayout parent, String value, int size) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(size);
        text.setPadding(0, dp(8), 0, dp(8));
        parent.addView(text, matchWrap());
        return text;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
