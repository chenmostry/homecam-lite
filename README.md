# 旧机看家 HomeCam Lite

**交付状态：v0.1 源码预览版。** 包含 Android 摄像端、网页查看端、服务端、部署脚本和自动化构建配置；APK 编译、真实手机及公网中转仍需按 [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) 完成验证。

HomeCam Lite 把一台旧 Android 手机变成由用户手动开启的家庭摄像头。主要摄像端是 Android 原生应用，在前台服务中采集画面；管理员和查看者使用浏览器。项目也提供仅限前台运行的 PWA（渐进式网页应用）摄像端，适用于浏览器支持摄像头功能的设备。

## 使用流程

1. 部署 Python 服务和独立的 coturn 中继服务，并为站点配置域名、DNS 和可信 HTTPS 证书。
2. 在网页端登录管理员账号并生成配对码，再把服务地址和配对码手动输入 Android 摄像端。
3. 在手机上手动启动摄像服务并保持其运行。查看者在浏览器登录后选择已配对的摄像头。
4. Python 服务负责认证和转发 WebRTC 信令；音视频经配置的 TURN 中继（网络转发服务）传输，并由摄像端与查看端之间的 WebRTC DTLS-SRTP（媒体加密协议）保护。

查看者可把快照下载到自己的设备。HomeCam Lite 不在服务器录像。

## 范围与限制

- 首版建议从 1–2 台摄像头开始，每台摄像头同时最多 1 名查看者，这是 v0.1 的固定上限。多台摄像头同时观看时，上传和中继流量会叠加。
- 默认目标为 640×480、12 fps，视频约 450 kbps，音频约 32 kbps；设备和网络允许时可选 720p。
- 视频和音频合计约 482 kbps。持续观看一小时约产生 217 MB 的单路公网出站媒体流量；24 小时约 5.2 GB，连续 30 天约 156 GB。此估算使用十进制单位，不含 IP/UDP/TURN/DTLS 开销、重传和其他流量。请按实际云厂商计费口径留出网络余量，并让 TURN 带宽与中继分配数量配额容纳目标码率及协议开销。
- 没有查看者时，不会持续上传媒体流；摄像端可以在本机保持采集预热。
- 面向小规模部署，服务端只处理信令和加密媒体中继，不包含 FFmpeg 转码、云端录像、云端 AI 或 SFU。资源占用尚未通过目标环境负载测试；部署前应按计划并发、码率和流量进行验证。
- Android 摄像服务由用户启动并授权。Android 的相机权限、前台服务规则和设备厂商的省电策略都可能限制或中止后台运行。PWA 摄像端要求浏览器页面保持前台，并经用户授权摄像头；它不提供隐蔽运行，也不保证后台持续采集。
- 生产环境必须有真实域名、指向服务器的 DNS 记录和可信 TLS 证书。开发模式仅供明确配置的本机 localhost 使用，不能用于公网部署。

## 开发环境

需要 Python 3.12、pip 和 Node.js（用于 JavaScript 语法检查）。Android 应用需要 Android SDK 和 JDK，具体版本见 [`docs/ANDROID.md`](docs/ANDROID.md)。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p .local
export HOMECAM_DEV=1
export HOMECAM_ORIGIN=http://127.0.0.1:8088
export HOMECAM_DB="$PWD/.local/homecam.db"
python -m homecam init-admin
python -m homecam
```

初始化命令会交互式要求设置管理员口令；先完成初始化，再启动服务。以上环境变量仅用于本机开发，开发模式自动生成临时 TURN 密钥且允许不配置 TURN URL。生产环境必须用受限权限的配置提供 `HOMECAM_TURN_SECRET`、`HOMECAM_TURN_URLS` 和可信 `HOMECAM_ORIGIN`，不要提交任何真实口令或密钥。

运行后在浏览器打开 `http://127.0.0.1:8088`。本地开发主要用于界面、API 和信令检查；真实摄像头权限与 WebRTC 媒体连接通常需要安全上下文和可用 TURN 服务。

运行 Python 单元测试：

```sh
python -m unittest discover -s tests -v
```

JavaScript 语法和部署配置渲染检查由 [CI 工作流](.github/workflows/checks.yml)执行。Android 应用构建请按 [`docs/ANDROID.md`](docs/ANDROID.md) 操作；服务器安装步骤和部署预检见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 安全与运维

- 不要将 Python 服务直接暴露到公网；生产环境应由已配置 HTTPS 的反向代理转发。
- 生产环境强制 `iceTransportPolicy: relay`（仅允许中继链路），媒体经自有 TURN 中继传输。TURN 临时凭据由服务端签发，浏览器不接收 TURN 共享密钥。
- 设备令牌和管理员会话可以撤销。数据库和生成的密钥配置仅应允许服务账户及管理员读取。
- 使用部署配置的受限 TURN 中继端口和网络策略。中继分配数、码率限制应与实际视频码率、预期查看人数和网络余量相匹配。
- Android 前台运行对用户可见。应用不会在开机时偷偷开启摄像头，也不声称能绕过 Android 或手机厂商限制。

另见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)、[`docs/PROTOCOL.md`](docs/PROTOCOL.md)、[`docs/OPERATIONS.md`](docs/OPERATIONS.md)，以及 [`docs/GITHUB.md`](docs/GITHUB.md) 中的仓库安全和 Actions APK 获取步骤。

## 参考资料

- [Android 前台服务](https://developer.android.com/develop/background-work/services/fgs)
- [Android 从后台启动前台服务的限制](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start)
- [Android 前台服务类型](https://developer.android.com/develop/background-work/services/fgs/service-types)
- [Android 使用的 WebRTC SDK](https://github.com/webrtc-sdk/android)
- [MDN：`getUserMedia()`](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)
- [MDN：Screen Wake Lock API](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API)
- [coturn 当前服务说明](https://github.com/coturn/coturn/blob/master/README.turnserver) 与 [示例配置](https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf)
- [aiohttp 官方文档](https://docs.aiohttp.org/)
