# Ubuntu 24.04 部署

本指南部署独立的 aiohttp 应用和 coturn systemd unit，并生成单独的 Caddy site snippet，面向 Ubuntu 24.04。具体主机资源与现有服务应在部署前单独检查。安装器默认只预检；只有显式传入 `--apply` 才写入 HomeCam 文件。它不安装软件、不启停服务、不改 SSH、全局 Caddyfile、现有 coturn 配置或防火墙，也不会自动启用 HomeCam units。

## 前提与端口

先准备真实域名和 A 记录，使域名指向服务器公网 IPv4；等待 DNS 生效。生产站点必须用 Caddy 或现有 HTTPS 入口提供可信 TLS。不能用 IP 地址、自签名证书或开发模式替代生产 TLS。提供商网络规则由管理员另行确认：公网 TCP 80/443（Caddy/ACME）、TCP 与 UDP 3478（TURN 客户端入口）、UDP 49160–49179（TURN relay）。本工具不自动开端口或改 NAT。检查提供商映射、公网/私网地址与主机路由相符。

coturn 配置启用 UDP relay、禁用 TCP peer relay（`no-tcp-relay`）。这**不**禁用客户端通过 TCP 连接 TURN 的 3478 监听端口；配置中 TCP/UDP 客户端入口均可用。WebRTC 生产策略仍是 `relay`。

## 安全安装系统依赖

先检查现有 Caddy/coturn 服务和软件包。不要升级整个主机，不要为 HomeCam 停止或替换已运行的服务。下面仅尝试安装缺少的 `python3.12-venv`、`coturn`、`caddy` 包；若包不在当前已批准的 APT 源中，先停止并由管理员决定软件来源。安装期间通过临时 `policy-rc.d` 拒绝软件包自动启动服务。脚本若发现已有 `policy-rc.d`（包括符号链接）会失败退出，不覆盖它；退出陷阱只移除本次创建且 inode 相同的临时文件。

```bash
sudo bash <<'ROOT'
set -euo pipefail
policy_file=/usr/sbin/policy-rc.d
policy_tmp=''
policy_linked=0
cleanup_policy() {
  if [ "$policy_linked" -eq 1 ] && [ -n "$policy_tmp" ] && [ "$policy_file" -ef "$policy_tmp" ]; then
    rm -f -- "$policy_file"
  fi
  if [ -n "$policy_tmp" ]; then rm -f -- "$policy_tmp"; fi
}
trap cleanup_policy EXIT
if [ -e "$policy_file" ] || [ -L "$policy_file" ]; then
  echo "已有 $policy_file；保留原文件并停止此 APT 步骤。" >&2
  exit 1
fi
policy_tmp=$(mktemp /usr/sbin/.policy-rc.d.homecam.XXXXXX)
printf '#!/bin/sh\nexit 101\n' > "$policy_tmp"
chmod 755 "$policy_tmp"
if ! ln -- "$policy_tmp" "$policy_file"; then
  echo "无法安全创建临时 policy-rc.d；未覆盖现有文件。" >&2
  exit 1
fi
policy_linked=1

packages=()
for package in python3.12-venv coturn caddy; do
  if ! dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
    packages+=("$package")
  fi
done
if [ "${#packages[@]}" -gt 0 ]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
else
  echo "所需软件包均已安装；未运行 APT。"
fi
ROOT
```

脚本成功或失败退出后会清理临时 `policy-rc.d`。再检查它已恢复到原先“不存在”的状态。不要为避免服务自启而留下一份全局 `policy-rc.d`。

## 生成配置并预览

在受信任的开发机或管理员终端中，从项目根目录生成部署配置。默认配置不允许 coturn 把主机私网地址作为 peer；TURN 共享密钥只写入权限为 `600` 的本地 JSON 文件，不会显示在终端：

```sh
cd homecam-lite
python3 deploy/homecam-deploy.py --init-config \
  --config $HOME/homecam-lite-deploy.json \
  --hostname cam.example.com --public-ip 203.0.113.10 --private-ip 10.0.0.2
chmod 600 $HOME/homecam-lite-deploy.json
```

`cam.example.com`、公网示例地址 `203.0.113.10`（文档保留地址）和私网示例地址 `10.0.0.2` 都必须替换为实际部署值。部署前确认私网地址当前分配给该主机、上游 NAT 映射正确。不要把生产服务器地址、配置文件或其中的密钥提交到 Git、聊天、工单或 APK。

通过已有的安全传输通道将项目和配置文件放到服务器（例如项目 `$HOME/homecam-lite`、配置 `$HOME/homecam-lite-deploy.json`），保持配置文件权限 `600`。本指南不要求记录 SSH 密码、私钥或凭据。服务器上先运行只读预检，再确认输出：

```sh
sudo chmod 600 $HOME/homecam-lite-deploy.json
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json
```

预检仅检查系统版本、依赖、配置和应用/relay 所需端口；不会联网或更改服务。对端口冲突、非 HomeCam 管理文件、错误 IP、系统版本不符或缺少依赖，安装会停止。不要手动停止冲突端口的所有者来“让检查通过”。安装器只检查 HomeCam 的 8088、3478 和 49160–49179 端口，不验证提供商防火墙、DNS 或 TLS。

检查无误后，显式应用：

```sh
sudo python3 $HOME/homecam-lite/deploy/homecam-deploy.py \
  --config $HOME/homecam-lite-deploy.json --apply
```

应用会复制一个带时间戳的 release 到 `/opt/homecam-lite/releases/`、创建 `/opt/homecam-lite/current`，在 `/etc/homecam-lite/` 放置受限配置，并安装 `homecam-lite.service` 和 `homecam-turn.service`。若替换由本安装器生成且带管理标记的文件，会在同目录保存权限 `600` 的备份。发现非管理文件或现有服务账户时会拒绝覆盖。它只运行 `systemctl daemon-reload`，不会启动、停止或启用服务。

## 首次启动

先创建管理员口令。命令以专用应用用户运行，口令通过交互提示输入：

```sh
sudo -u homecam env HOMECAM_DB=/var/lib/homecam-lite/homecam.db \
  PYTHONPATH=/opt/homecam-lite/current \
  /opt/homecam-lite/current/.venv/bin/python -m homecam init-admin
```

Caddy snippet 在 `/etc/caddy/homecam-lite.caddy`。先检查现有 `/etc/caddy/Caddyfile` 及站点是否已占用该域名，再用 `sudoedit` **只添加**这一行，不要覆盖或重写全局文件：

```caddyfile
import /etc/caddy/homecam-lite.caddy
```

确认语法、域名和现有站点配置后再执行 `sudo caddy validate --config /etc/caddy/Caddyfile`。检查服务与端口：

```sh
sudo systemctl is-active caddy || true
sudo ss -ltnp
```

若 Caddy 已在运行且验证成功，显式执行 `sudo systemctl reload caddy` 载入新增 import。若这是经检查确认无冲突的首次 Caddy 启动，显式执行 `sudo systemctl enable --now caddy`；该命令不由 HomeCam 安装器执行。若 80/443 已由其他代理占用、同域名已有站点、Caddy unit 状态与预期不同或验证失败，先查明现有路由，不要停止其他应用或替换现有代理配置。

确认 Caddy 已代理正确域名、可信 TLS 已签发、提供商网络规则生效后，才显式启动 HomeCam：

```sh
sudo systemctl enable --now homecam-turn.service homecam-lite.service
sudo systemctl --no-pager --full status homecam-turn.service homecam-lite.service
curl --fail --silent --show-error https://cam.example.com/healthz
```

健康检查应返回 `{"ok":true}`。检查日志：

```sh
sudo journalctl -u homecam-lite.service -u homecam-turn.service -n 100 --no-pager
```

应用只绑定 `127.0.0.1:8088`。管理员从浏览器登录并配对后，用 Android 原生摄像端作为主要摄像端；PWA 是需前台运行的备用摄像端。部署时不能声称 Android 已构建/测试或真实媒体已连通。按 [`ACCEPTANCE.md`](ACCEPTANCE.md) 在两个不同网络上确认真实画面、选中的 ICE candidate pair 为 relay、coturn 分配与日志，并记录 CPU、内存和流量。TURN 共享密钥不会下发给浏览器；服务端只签发临时凭据。

## TURN 私网例外

`allowed-peer-ip=<PRIVATE_IP>` 是地址级允许规则，**不是** `49160–49179` 的端口范围；它会为该 peer IP 放行所有 peer 端口。默认值为关闭。只有真实端到端 relay 测试证明摄像端/查看端连接此服务器私网地址确有需要时，才启用 opt-in。

已有部署配置时，先用 `sudoedit $HOME/homecam-lite-deploy.json` 将 `"allow_private_relay": false` 改为 `true`，保存后执行 `sudo chmod 600 $HOME/homecam-lite-deploy.json`。这保留现有 TURN secret。`--init-config` 不会覆盖同名配置文件。

若要新建一份配置而接受生成新的 TURN secret，请使用一个不存在的文件名：

```sh
python3 deploy/homecam-deploy.py --init-config \
  --config $HOME/homecam-lite-private-relay.json \
  --hostname cam.example.com --public-ip 203.0.113.10 --private-ip 10.0.0.2 \
  --allow-private-relay
```

将生成的新文件设为 `chmod 600 $HOME/homecam-lite-private-relay.json`，并在后续预检、apply 和运维命令中一致使用它。此路径会生成新 secret；apply 后必须重启两个 HomeCam units，旧 TURN 凭据将失效。

应用这份配置**之前**，须在管理员负责的、持久化的 nftables OUTPUT 规则中配置并检查 `homecam-turn` UID 限制。下列命令示例展示约束关系：只允许 TURN 用户向服务器自身私网地址或公网映射地址的 UDP 49160–49179 发包，丢弃该用户向这两个地址的其他端口/协议的包，以防本机 NAT loopback 绕过限制。先确认规则不会与主机既有防火墙策略冲突；由防火墙负责人纳入持久化配置。不要直接把临时命令当成永久防火墙配置，也不要让安装器自动添加规则。

```sh
sudo nft add table inet homecam_turn_guard
sudo nft 'add chain inet homecam_turn_guard output { type filter hook output priority filter; policy accept; }'
sudo nft add rule inet homecam_turn_guard output meta skuid homecam-turn ip daddr 10.0.0.2 udp dport 49160-49179 accept
sudo nft add rule inet homecam_turn_guard output meta skuid homecam-turn ip daddr 10.0.0.2 drop
sudo nft add rule inet homecam_turn_guard output meta skuid homecam-turn ip daddr 203.0.113.10 udp dport 49160-49179 accept
sudo nft add rule inet homecam_turn_guard output meta skuid homecam-turn ip daddr 203.0.113.10 drop
sudo nft list chain inet homecam_turn_guard output
```

命令中的地址均为示例；部署前替换为实际公网和私网地址，并确认命中正确 UID、目标地址和 relay 端口。若现有防火墙由其他工具管理，应在该工具中实现同等规则，避免并行管理规则集。`--allow-private-relay` 不会增加 relay 端口范围，不会改变 `no-tcp-relay`，也不应被理解为可安全访问整个私网。

## 回退

只停止或禁用 HomeCam 自己的 unit：

```sh
sudo systemctl disable --now homecam-lite.service homecam-turn.service
```

若已为 HomeCam 添加了 Caddy import，手工移除该单行、验证 Caddyfile 并按既有流程 reload。不要删除 HomeCam 数据库、现有 Caddyfile、其他服务、coturn 包配置或防火墙规则；数据库与部署配置需要另行按 [`OPERATIONS.md`](OPERATIONS.md) 备份/保留。
