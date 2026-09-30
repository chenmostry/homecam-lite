# GitHub 与 Android 构建

源码仓库的可见性由仓库所有者决定。无论仓库公开或私有，都不要提交部署配置、`.env`、SQLite 数据库、运行日志、APK、Android 签名文件、SSH 私钥、TURN secret 或管理员口令。

## 检查仓库内容

提交前检查 `.gitignore`，运行 `git status --short` 与 `git diff --cached --check`，确认没有本地配置、数据库、构建输出或凭据进入提交。部署配置应在部署时生成，并保存在服务器或管理员受控的安全位置。

## 从 Actions 获取 APK

Android workflow 仅手动触发。操作者须先阅读 Android SDK 许可，并在 GitHub Actions 的手动运行表单中明确勾选接受许可；普通代码推送不会构建 APK或替操作者接受许可。

构建成功后，从对应的 Actions run 下载 `homecam-lite-android-debug` artifact。该 APK 使用 debug 签名，仅供测试；不同机器或 CI 的临时 debug 签名可能不同，覆盖安装可能失败。长期发布应按 [`ANDROID.md`](ANDROID.md) 管理固定签名密钥，并通过受控渠道保存。

自动构建通过不能代替 [`ACCEPTANCE.md`](ACCEPTANCE.md) 中的实体设备和跨网络验收。
