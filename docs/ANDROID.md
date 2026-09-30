# Android 摄像端：构建、安装与限制

原生摄像端支持 Android 8.0（API 26）及以上，使用 Java 17、Android Gradle Plugin 8.9.2、Gradle 8.11.1 和 Android SDK 35。WebRTC 依赖为 `io.github.webrtc-sdk:android:150.7871.01`，HTTP/WebSocket 客户端为 OkHttp 4.12.0。依赖版本固定在 `android/app/build.gradle`。

项目当前没有 Gradle Wrapper。请从仓库根目录构建；本机需要 JDK 17、Gradle 8.11.1、Android SDK Command-line Tools，以及 SDK Platform 35 和 Build Tools 35.0.0。先用 Android SDK Manager 安装所需组件并接受 Android SDK 许可，然后运行：

APK 应在开发机或 GitHub Actions 上构建；不要在资源受限的生产云服务器上安装完整 Android SDK 或执行 Gradle 构建。

```sh
gradle --version
sdkmanager "platforms;android-35" "build-tools;35.0.0" "platform-tools"
gradle -p android --no-daemon :app:assembleDebug :app:lintDebug
```

生成的调试 APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。若本机未将 `gradle` 和 `sdkmanager` 加入 `PATH`，请使用其完整路径。Android SDK 的位置可通过 `ANDROID_HOME` 或 `ANDROID_SDK_ROOT` 提供给 Gradle。

仓库的 [Android Actions 工作流](../.github/workflows/android.yml)在推送、Pull Request 和手动触发时，设置 JDK 17、Gradle 8.11.1 与 Android SDK 35，运行 Debug 构建和 lint，验证 APK 签名，并把调试 APK 作为 Actions artifact 上传。只有工作流成功后，才能从对应运行记录下载该构建产物。本仓库说明本身不表示已有 APK、发布版本或已在真机测试。

## 安装和签名

启用手机的开发者选项与 USB 调试，并连接已授权的手机后，可在仓库根目录安装：

```sh
adb devices
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.homecamlite/.MainActivity
```

调试构建由 Android Gradle Plugin 使用本机的调试密钥自动签名。不同电脑和 GitHub Actions 通常有不同的调试证书。升级安装时，Android 要求新 APK 的签名证书与已安装版本相同；签名不一致时，需先卸载旧版再安装。卸载会删除应用私有数据和配对凭据。不要把调试签名当成公开发布身份。

可检查 APK 的签名证书和文件摘要：

```sh
"$ANDROID_HOME/build-tools/35.0.0/apksigner" verify --verbose --print-certs android/app/build/outputs/apk/debug/app-debug.apk
sha256sum android/app/build/outputs/apk/debug/app-debug.apk
```

对外分发时，应由项目维护者使用受控、离线备份的发布密钥签名，并通过独立渠道公布签名证书指纹或 APK SHA-256。不要将密钥、密码或签名配置提交到仓库。当前项目没有配置发布密钥或自动发布流程；`assembleRelease` 产物不能据此视为正式发布版。若通过浏览器或文件管理器侧载 APK，Android 可能要求允许该安装来源；仅安装来源、构建记录和签名指纹都可核验的文件。

## 首次使用

1. 在可信的 HTTPS HomeCam Lite 服务器上，由管理员生成一次性配对码。
2. 在手机打开应用，填写服务器源地址与配对码。配对令牌由 Android Keystore 生成的 AES-GCM 密钥加密后保存在应用私有存储，并通过认证附加数据绑定服务器 origin 和设备 ID；应用不保存管理员密码。
3. 用户点“开始看护”后，应用才请求摄像头权限；只有勾选麦克风时才请求麦克风权限。Android 13 及以上还会询问通知权限。麦克风未启用或权限被拒绝时，只运行摄像头前台服务，运行时只声明 camera 服务类型。
4. Android 13 及以上若拒绝通知权限，前台服务仍会显示在系统的活动应用 / Task Manager 界面，但其通知可能不出现在通知栏；具体停止入口也受系统版本与厂商界面影响。应用内仍提供“停止摄像”。

摄像服务只能由用户在应用可见时手动启动，不会随开机自动启动，也不会隐藏摄像状态。运行时通知说明摄像状态并提供停止按钮。服务为锁屏续流持有有限时长、定期续期的 partial wake lock，并由前台服务维持较高进程优先级；这些措施不能阻止系统、用户或设备厂商结束应用。

## 锁屏与设备厂商限制

目标行为是用户手动启动后在锁屏时继续采集和传输。它不是所有 Android 设备都能保证的承诺。摄像头和麦克风权限受 Android 前台服务与“使用时”权限规则约束；尤其 Android 14 及以上，需要在应用处于可启动状态、权限已授予时启动 camera / microphone 前台服务。应用不在后台、通知操作或开机广播中偷偷开启摄像头。

熄屏后是否持续送帧还取决于相机 HAL、WebRTC 编码器、Wi-Fi/移动网络、温控策略、省电模式和厂商电池管理。部分厂商系统会在屏幕关闭、锁定或长时间闲置后冻结应用、断网、停止摄像头或杀掉服务。可在目标手机上自行允许后台活动、将应用加入电池优化例外或维持供电，但菜单名称因厂商而异，电量消耗和发热也会增加；这些设置不能保证每台手机都持续运行。若进程或服务被系统停止，用户需重新打开应用并手动启动，应用不会自动恢复拍摄。

使用前应在目标机型上测试：先在亮屏时确认可连接，再锁屏观察视频是否继续、通知与停止入口是否可用，并确认停止应用后服务器显示离线。尚未完成真实手机测试时，不应宣称该机型已验证可锁屏运行。

相关官方说明：[前台服务启动限制](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start)、[前台服务类型](https://developer.android.com/develop/background-work/services/fgs/service-types)、[Android 13 通知权限与前台服务](https://developer.android.com/about/versions/13/behavior-changes-13)、[Gradle Actions](https://github.com/gradle/actions)。
