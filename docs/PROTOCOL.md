# HomeCam Lite v0.1 协作协议

产品中文名“旧机看家”，独立实现，不复制商业应用素材。本协议对应项目根目录内的各组件。

## 首版范围

Python 3.12 + aiohttp + SQLite (stdlib)，无前端构建、无外部 CDN。网页提供查看端和备用摄像端；Android 8+ 原生应用负责前台摄像服务、常驻通知与停止入口、可选麦克风、支持锁屏后继续工作（需具体机型验证），不得偷偷开机启动拍摄。APK 由开发机或 GitHub Actions 构建，不应在资源有限的生产云机上构建。原生 ES modules / HTML / CSS PWA 备用摄像端需保持前台亮屏。生产可使用 Caddy HTTPS、coturn TURN 和 Ubuntu systemd 部署。生产默认 `iceTransportPolicy: relay`，必须经自有服务器中转；开发环境允许 `all` 并明确标记。1–2 路摄像头、每路默认最多 1 名观看者。默认 640x480 / 12fps / 450kbps 视频，音频约 32kbps；可选720p。无服务器录像、云端 AI、推送通知、双向对讲。查看端可截图；下载发生在查看者设备。

## API（JSON，snake_case 字段）

- `GET /healthz` → `{ok:true}`，不得包含版本、凭据或配置。
- `POST /api/login {password}` → `{ok:true}`，设置 HttpOnly、SameSite=Strict、生产 Secure 的 `homecam_session` cookie。
- `GET /api/session` → `{authenticated: boolean}`。
- `POST /api/logout {}` → `{ok:true}`，删除服务端会话并断开它的查看 WS。
- `GET /api/devices` → `{devices:[{id,name,online,created_at,viewer_count}]}`，admin only。
- `POST /api/pairings {name}` → `{code,expires_at}`，admin only；8位随机大写字母/数字、10分钟、一用即失效。
- `POST /api/pairings/redeem {code}` → `{device_id,device_token,name}`；Android 摄像端通过 Keystore 加密保存 token，备用网页摄像端保存到本机 localStorage。无管理员密码写入摄像端。
- `DELETE /api/devices/{id}` → `{ok:true}`，admin only；立即吊销 token、踢出连接、结束观看。
- `GET /api/ice` → `{ice_servers:[{urls:[...],username,credential}],ice_transport_policy:"relay",expires_at}`；admin cookie 或 `Authorization: Bearer DEVICE_TOKEN`。HMAC-SHA1 coturn REST 临时口令 TTL 3600秒。共享密钥绝不送前端。
- 失败统一 `{error: "中文或英文错误描述"}`，使用适当4xx。

`created_at`和`expires_at`统一为ISO8601 UTC字符串，末尾`Z`。原生客户端对HTTP变更请求和WS握手显式设置`Origin`为用户选择的HTTPS服务origin；不绕过TLS证书验证。认证失败/吊销WS使用close1008，让客户端停止自动重试并提示重新配对或登录。

所有可变API要求 Origin 精确等于 APP_ORIGIN；开发模式仅允许指定localhost origin。WS也要求匹配Origin。GET API不返回跨源CORS许可。请求体/WS每帧上限64KiB；登录与配对兑换需要限速；密码scrypt哈希，token/session存SHA256摘要。DB文件只服务用户可读。通过初始化CLI读取getpass设置管理员口令，无默认口令。

## WebSocket `/ws`

连接后5秒内必须首发 auth。viewer 用cookie会话 + `{type:"auth",role:"viewer"}`；camera 用 `{type:"auth",role:"camera",device_id,device_token}`。禁止URL携带凭据。认证完成服务器回复 `{type:"ready",peer_id,role}`，viewer另收devices消息。角色/peer_id完全由服务端验证。

- viewer → `{type:"watch",device_id}`。
- server → viewer `{type:"watching",device_id,peer_id:CAMERA_PEER_ID,session_id}`。
- server → camera `{type:"viewer-joined",peer_id:VIEWER_PEER_ID,session_id}`。camera创建RTCPeerConnection、加入本地tracks、创建offer。
- 任一端 → `{type:"signal",target:OTHER_PEER_ID,session_id,data:{type:"offer"|"answer",sdp:{type:"offer"|"answer",sdp:"..."}}}` 或 `data:{type:"ice",candidate:RTCIceCandidateInit}`。
- server转发 → `{type:"signal",source:SENDER_PEER_ID,session_id,data:...}`。必须核对现存watch membership，禁止任意peer间转发。
- viewer → `{type:"unwatch"}`；server → camera `{type:"viewer-left",peer_id:VIEWER_PEER_ID,session_id}`。
- camera断线/吊销 → viewer `{type:"peer-left",session_id}`。
- 摄像头online状态变化 → viewers `{type:"devices",devices:[...]}`。
- server异常 → `{type:"error",error:"..."}`。

双方每次建立新PC前 GET /api/ice；远端SDP应用前ICE排队。查看持续45分钟时主动unwatch/watch建立新PC刷新TURN凭据（先发unwatch再等watching/短延时重建，不并发offer）。WS断线指数退避重连，查看端重新watch所选设备；老连接事件不得覆盖新连接。camera权限撤销、track ended、点击停止必须关闭WS/全部PC和tracks，在线状态相应清除。前端收到viewer-left、peer-left或拒绝要清理对应PC。

## 配置约定

`HOMECAM_DB=/var/lib/homecam-lite/homecam.db`；`HOMECAM_HOST=127.0.0.1`；`HOMECAM_PORT=8088`；`HOMECAM_ORIGIN=https://cam.example.com`；`HOMECAM_DEV=0`；`HOMECAM_TURN_URLS=turn:203.0.113.10:3478?transport=udp,turn:203.0.113.10:3478?transport=tcp`；`HOMECAM_TURN_SECRET`；`HOMECAM_MAX_VIEWERS=1`。其中 `203.0.113.10` 是文档示例地址，部署时替换为实际公网地址。可增加配置但必须记录并通知集成人。

v0.1 固定每台摄像头同时最多 1 名观看者；配置值必须为 1。多摄像头可分别观看，不能通过把配置改为 2 来启用同一路多观看者。

服务入口 `python -m homecam`，初始化 `python -m homecam init-admin`，支持 `--reset` 但必须重新getpass明确输入并撤销全部旧admin session。`requirements.txt`由服务端负责。前端目录 `web/`，静态allowlist，`/` -> index.html。设置CSP self、nosniff、no-referrer、frame-ancestors none；media blob允许；不缓存API或私密录像；service worker只缓存明确静态asset。

## 部署约束

部署脚本应提供只读预检并在冲突时失败退出；不修改 SSH、现有应用、全局反向代理配置、已有 coturn 配置或防火墙，也不自动启停服务。可生成独立 systemd units；配置文件权限为 600/640，使用专用服务用户和 `MemoryMax`。公网与私网地址通过部署参数提供。coturn NAT 使用 `external-ip=PUBLIC/PRIVATE`，relay 端口限制为 UDP 49160–49179，禁用无认证与管理 CLI，限制 allocation 配额与 bandwidth，默认拦截私网/loopback/link-local/multicast/metadata。若端到端中转测试证明确需允许本机私网 relay IP，只能显式选择例外，并先由部署者限制该服务用户的目的端口；`allowed-peer-ip` 本身不限制端口。生产 HTTPS 需要可信证书；域名 DNS/TLS 前提必须写清。未在真实主机和 Android 设备上测试时不能声称已实测。
