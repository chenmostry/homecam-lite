package com.homecamlite;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Small app-private store. The bearer token is encrypted with an Android Keystore AES key. */
final class DeviceStore {
    private static final String PREFS = "homecam_secure";
    private static final String KEY_ALIAS = "homecam.device-token.aes-gcm.v1";
    private static final String ORIGIN = "origin";
    private static final String DEVICE_ID = "device_id";
    private static final String DEVICE_NAME = "device_name";
    private static final String TOKEN = "device_token_ciphertext";
    // Display text only. Callers must use HomeCamService.isActive() for live service state.
    private static final String STATUS = "service_status";
    private static final int GCM_TAG_BITS = 128;

    private DeviceStore() {}

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static void savePairing(Context context, String origin, String deviceId, String name, String token)
            throws Exception {
        if (origin == null || deviceId == null || token == null || token.isEmpty()) {
            throw new IllegalArgumentException("配对响应缺少设备凭据。");
        }
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
        cipher.updateAAD(aad(origin, deviceId));
        byte[] encrypted = cipher.doFinal(token.getBytes(StandardCharsets.UTF_8));
        byte[] iv = cipher.getIV();
        byte[] stored = new byte[iv.length + encrypted.length];
        System.arraycopy(iv, 0, stored, 0, iv.length);
        System.arraycopy(encrypted, 0, stored, iv.length, encrypted.length);
        prefs(context).edit()
                .putString(ORIGIN, origin)
                .putString(DEVICE_ID, deviceId)
                .putString(DEVICE_NAME, name == null ? "摄像设备" : name)
                .putString(TOKEN, Base64.encodeToString(stored, Base64.NO_WRAP))
                .apply();
    }

    static String getToken(Context context, String expectedOrigin, String expectedDeviceId) {
        SharedPreferences p = prefs(context);
        String origin = p.getString(ORIGIN, null);
        String deviceId = p.getString(DEVICE_ID, null);
        String encoded = p.getString(TOKEN, null);
        if (origin == null || deviceId == null || encoded == null
                || !origin.equals(expectedOrigin) || !deviceId.equals(expectedDeviceId)) {
            return null;
        }
        try {
            byte[] stored = Base64.decode(encoded, Base64.NO_WRAP);
            if (stored.length <= 12) {
                return null;
            }
            byte[] iv = new byte[12];
            byte[] encrypted = new byte[stored.length - iv.length];
            System.arraycopy(stored, 0, iv, 0, iv.length);
            System.arraycopy(stored, iv.length, encrypted, 0, encrypted.length);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), new GCMParameterSpec(GCM_TAG_BITS, iv));
            cipher.updateAAD(aad(origin, deviceId));
            return new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
        } catch (Exception ex) {
            return null;
        }
    }

    static String getOrigin(Context context) {
        return prefs(context).getString(ORIGIN, null);
    }

    static String getDeviceId(Context context) {
        return prefs(context).getString(DEVICE_ID, null);
    }

    static String getDeviceName(Context context) {
        return prefs(context).getString(DEVICE_NAME, "摄像设备");
    }

    static boolean isPaired(Context context) {
        SharedPreferences p = prefs(context);
        return p.getString(ORIGIN, null) != null
                && p.getString(DEVICE_ID, null) != null
                && p.getString(TOKEN, null) != null;
    }

    static void clearPairing(Context context) {
        prefs(context).edit().remove(ORIGIN).remove(DEVICE_ID).remove(DEVICE_NAME).remove(TOKEN).apply();
    }

    static void setStatus(Context context, String status) {
        prefs(context).edit().putString(STATUS, status).apply();
    }

    static String getStatus(Context context) {
        return prefs(context).getString(STATUS, "已停止");
    }

    private static byte[] aad(String origin, String deviceId) {
        return (origin + "\n" + deviceId).getBytes(StandardCharsets.UTF_8);
    }

    private static SecretKey getOrCreateKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance("AndroidKeyStore");
        keyStore.load(null);
        java.security.Key key = keyStore.getKey(KEY_ALIAS, null);
        if (key instanceof SecretKey) {
            return (SecretKey) key;
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build());
        return generator.generateKey();
    }
}
