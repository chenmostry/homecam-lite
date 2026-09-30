# 运维说明

HomeCam Lite 与主机既有服务分开运行：Python API unit 为 `homecam-lite.service`，TURN unit 为 `homecam-turn.service`。应用绑定 `127.0.0.1:8088`；coturn 监听配置的 TURN 客户端入口，并只分配 UDP relay 49160–49179。**不要**因 HomeCam 故障停止 SSH、反向代理、其他 TURN 服务或其他应用。v0.1 每台摄像头只保留 1 名查看者。

## 状态和日志

```sh
sudo systemctl --no-pager --full status homecam-lite.service homecam-turn.service
sudo journalctl -u homecam-lite.service -u homecam-turn.service -n 100 --no-pager
curl --fail --silent --show-error https://cam.example.com/healthz
```

健康检查只验证反代后的 API 存活，不代表摄像端已配对、TURN 端口可达或媒体正在 relay。升级/重启或检查前先记下输出和时间；不要公开粘贴可能包含地址、会话或配置路径的完整日志。

启动、停止或重启时只操作 HomeCam unit：

```sh
sudo systemctl restart homecam-turn.service homecam-lite.service
sudo systemctl stop homecam-lite.service homecam-turn.service
sudo systemctl start homecam-turn.service homecam-lite.service
```

TLS/证书、反向代理域名和既有站点路由须按主机既有运维流程检查，不能用替换全局反向代理配置的方式修复单个 HomeCam 站点。

## 更新应用

先备份数据库并核对新代码来源/版本。将已审阅的项目文件更新到 `$HOME/homecam-lite`，不要覆盖服务器私密配置。先只读预检，再显式安装：

```sh
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json --apply
sudo systemctl restart homecam-turn.service homecam-lite.service
sudo systemctl --no-pager --full status homecam-turn.service homecam-lite.service
```

部署工具保留带时间戳的 release；`current` 指向当前版本。保留上个 release 直到健康检查、登录、配对和媒体 relay 验收完成。若更新失败，停用/回退只涉及 HomeCam release 与 unit；切换 `current` 到已知 release 后 daemon-reload/restart HomeCam units。不要删除数据库或其他服务数据。

## 密钥与管理员

本地部署 JSON 和服务器 `/etc/homecam-lite/{homecam.env,turnserver.conf}` 含 TURN 共享密钥，必须限制读取；应用和 coturn 服务用户只读各自需要的文件。密钥不发到客户端，API 只签发短时 TURN credential。管理员口令应通过交互式 CLI 输入；不要写在 shell 命令参数、环境文件、聊天或 issue。

轮换 TURN secret 时，新值先写入本地配置，然后将该配置应用到服务器并重启两个 HomeCam units。轮换会立即使此前签发的 TURN credential 失效：

```sh
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json --rotate-turn-secret
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json --apply
sudo systemctl restart homecam-turn.service homecam-lite.service
```

轮换前确认此配置文件就是当前服务器版本的部署配置，并安全留存；不要把其内容打印、复制到普通工单或提交 Git。应用 `--rotate-turn-secret` 仅修改本地 JSON，不更新服务器，必须完成后续预检和 `--apply`。

管理员口令遗失时，在服务器交互运行重置流程；此 CLI 从当前工作目录导入源码，因此显式设置 `PYTHONPATH`：

```sh
sudo -u homecam env PYTHONPATH=/opt/homecam-lite/current \
  HOMECAM_DB=/var/lib/homecam-lite/homecam.db \
  /opt/homecam-lite/current/.venv/bin/python -m homecam init-admin --reset
```

不要从 SQLite 中手工编辑口令 hash。设备令牌泄露时，从管理员 UI 撤销设备并重新配对。退出登录、吊销设备和变更后应验证连接已断开。

## 数据和恢复

SQLite 数据位于 `/var/lib/homecam-lite/homecam.db`，由 `homecam` 用户读写，服务目录权限为 `0700`。数据库保存账户、设备和会话等元数据，不存储服务器端录像。备份时短暂停应用服务以保证简单文件副本一致，然后保留权限并恢复服务：

```sh
sudo systemctl stop homecam-lite.service
sudo install -o homecam -g homecam -m 0600 \
  /var/lib/homecam-lite/homecam.db \
  /var/lib/homecam-lite/homecam.db.backup-$(date -u +%Y%m%dT%H%M%SZ)
sudo systemctl start homecam-lite.service
```

请确认目标备份路径安全、空间充足且权限符合数据保留要求；备份文件和部署 JSON 应加密存储并定期做恢复演练。若备份命令失败，先确认应用服务恢复运行。截图由查看者下载到自己的设备，不在服务器备份中。

## 网络故障和私网例外

确认 DNS/TLS、上游安全组/NAT 和主机已有防火墙后，再检查 TURN 3478 TCP/UDP 与 UDP 49160–49179。coturn 的 `no-tcp-relay` 禁用 TCP peer relay，不禁用客户端 TCP 3478 入口。通过客户端 WebRTC 统计确认最终选中的 candidate pair 是 relay，并在摄像端与查看端处于不同网络时复测；只看到 API 登录成功或 ICE gathering 完成，不代表媒体已中继。

默认 coturn 拒绝 loopback、私网、link-local、metadata 和 multicast 等 peer 范围。不要为绕过连接问题打开全部私网。若真实 relay 证据说明需要对服务器自身地址自转发，先读 [`DEPLOY.md`](DEPLOY.md) 的“TURN 私网例外”：明确 opt-in `--allow-private-relay` 并配置持久、UID 限定的 OUTPUT guard。coturn 的 `allowed-peer-ip` 仅匹配地址，不限制端口；规则必须把 `homecam-turn` UID 对服务器私网地址和公网映射地址的 UDP 输出都约束到 49160–49179，以免 NAT loopback 绕过。不要让安装脚本自动改防火墙。

## 资源和容量

服务 unit 设置 `MemoryMax=192M`，coturn 设置 `MemoryMax=256M`。这只是进程上限，不代表任何特定主机上的容量保证。持续观看默认媒体目标约 482 kbps/路，relay 公网流量另有协议与重传开销。记录实际 CPU、内存、swap、云账单和连续观看时长；若主机资源受压，停止 HomeCam units 并调查，不要扩大同时查看人数。应在开发机或 CI 环境构建 Android APK。
