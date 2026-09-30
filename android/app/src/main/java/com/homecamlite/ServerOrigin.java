package com.homecamlite;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/** Accepts a server origin only, never an arbitrary API path or credential-bearing URL. */
final class ServerOrigin {
    private ServerOrigin() {}

    static String parse(String input) {
        if (input == null || input.trim().isEmpty()) {
            throw new IllegalArgumentException("请输入服务器 HTTPS 地址。");
        }
        final URI uri;
        try {
            uri = new URI(input.trim());
        } catch (URISyntaxException ex) {
            throw new IllegalArgumentException("服务器地址格式无效。");
        }
        if (!"https".equalsIgnoreCase(uri.getScheme())
                || uri.getHost() == null
                || uri.getRawUserInfo() != null
                || uri.getRawQuery() != null
                || uri.getRawFragment() != null
                || !(uri.getRawPath() == null || uri.getRawPath().isEmpty() || "/".equals(uri.getRawPath()))) {
            throw new IllegalArgumentException("只接受可信 HTTPS 服务器的源地址，不含路径、参数或账号密码。");
        }
        int port = uri.getPort();
        if (port == 0 || port > 65535 || port < -1) {
            throw new IllegalArgumentException("服务器端口无效。");
        }
        String host = uri.getHost().toLowerCase(Locale.ROOT);
        if (host.indexOf('%') >= 0) {
            throw new IllegalArgumentException("服务器地址中的主机名无效。");
        }
        if (host.indexOf(':') >= 0 && !host.startsWith("[")) {
            host = "[" + host + "]";
        }
        String authority = host + (port == -1 || port == 443 ? "" : ":" + port);
        return "https://" + authority;
    }
}
