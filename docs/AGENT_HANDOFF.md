# 开发与部署交接

HomeCam Lite v0.1 使用 Android 原生前台摄像服务作为主要摄像端，PWA 作为备用摄像端。网页提供管理和查看功能；服务端负责 API、信令与 coturn 中继，不录像、不转码。每台摄像头同时最多允许一名查看者。

## 当前状态

- 后端、部署脚本、前端和浏览器端到端自动检查均已通过。
- Android APK 尚未构建，也未在实体手机上验收。
- 项目尚未完成生产环境部署、可信 TLS 验证或跨网络 TURN 媒体验收。
- Android Actions workflow 仅手动触发；运行前须由操作者明确确认接受 Android SDK 许可。

## 开发与部署

- 先阅读 [`DEPLOY.md`](DEPLOY.md) 和 [`ACCEPTANCE.md`](ACCEPTANCE.md)。部署脚本默认只读预检，只有显式传入 `--apply` 才会写入 HomeCam 文件。
- 部署前检查系统版本、端口、域名、DNS、NAT、安全组和现有服务。遇到冲突应停止并调查，不要为了 HomeCam 停止或覆盖其他服务。
- 生产配置、管理员口令、TURN secret、SSH 私钥、签名密钥、数据库和日志不得提交到 Git。
- 默认禁止 coturn 访问私网 peer。只有端到端测试证明需要时，才评估显式私网例外并落实服务账户网络限制。

## 发布验收

APK 应在开发机或 CI 构建。自动检查通过后，仍须在实体 Android 设备和两个独立网络上测试摄像头权限、锁屏行为、撤销清理、可信 TLS、真实视频和 relay candidate。未完成的项目应标为待验证，不得表述为已部署或已实测。
