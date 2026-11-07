# 安装与配套服务

插件可以在任何 AstrBot 4.29+ 环境安装；配套服务（面板 + 采集器）只面向
Linux + systemd + `/var/lib/astrbot` + `astrbot` 系统用户的布局。
其他环境需要自行适配路径、权限、账户与容器网络。

## 一、只装插件

在 AstrBot 插件管理里用仓库地址安装：

```
https://github.com/AKIhunter/astrbot_plugin_bot_manager
```

或从插件市场安装 `astrbot_plugin_usage_telemetry`（技术 ID 保持不变，历史配置不迁移）。
装好启用后，用 AstrBot 管理员发送 `/bm 状态`。

只需要用量统计与指令查询时，到这里就够了：余额、搜索汇总、网页都需要配套服务。

## 二、装配套服务

### 前置条件

- root 权限；`python3`、`systemctl`、`ss` 可用
- AstrBot 数据目录在 `/var/lib/astrbot`，插件目录是 `/var/lib/astrbot/data/plugins`
- 已有 AstrBot 配置与数据备份

### 步骤

```bash
git clone https://github.com/AKIhunter/astrbot_plugin_bot_manager.git
cd astrbot_plugin_bot_manager
sudo sh service/install-server.sh service
```

安装器会从**仓库根目录**读取插件源码（缺文件时自动暂存到临时目录，不落库），
因此不需要手工复制 `<插件目录>`；`service/astrbot_plugin_usage_telemetry/` 只作为旧布局兼容。

安装过程会：

1. 在 `/opt/bot-manager/releases/<时间戳>-<pid>/` 生成新版本目录，并把 `current` 指向它；
2. 写入 `/etc/systemd/system/bot-manager.service` 并 `enable`；
3. 首次安装时生成面板账号（`admin`）与随机密码，**只显示一次**，请立即保存；
4. 生成内部令牌 `/etc/bot-manager/plugin-token`（`0640 root:astrbot`）；
5. 备份并用新版覆盖 `astrbot_plugin_usage_telemetry`，失败时回滚旧目录；
6. 重启 `bot-manager` 与 `astrbot`，并做一轮健康检查，任一检查失败会恢复到上一版。

### 目录与文件

| 路径 | 用途 | 权限 |
| --- | --- | --- |
| `/opt/bot-manager/current` | 指向当前 release 的软链接 | `root:root` |
| `/opt/bot-manager/releases/` | 历史版本，用于回滚 | `root:root` |
| `/etc/bot-manager/credentials.json` | 面板登录口令摘要（scrypt） | `0600 root:root` |
| `/etc/bot-manager/provider-credentials.json` | 余额服务凭据（API Key 等） | `0600 root:root` |
| `/etc/bot-manager/plugin-token` | 插件调用内部接口的令牌 | `0640 root:astrbot` |
| `/etc/bot-manager/model-pricing.json` | 单价与优惠时段 | `0600 root:root` |
| `/etc/bot-manager/tunnel-target` | 面板侧栏显示的隧道目标（可选） | `0640 root:astrbot` |
| `/var/lib/bot-manager/metrics.db` | 资源采样、搜索流水、余额快照 | `astrbot` 可写 |

面板只监听 `127.0.0.1:17321`，不对公网开放；访问方式见[客户端与 SSH 隧道](client.md)。

### 重置面板密码

```bash
cd astrbot_plugin_bot_manager
sudo sh service/install-server.sh service --reset-panel-password
```

新密码同样只显示一次。也可以在 AstrBot 插件设置页改（见[配置](configuration.md)），
面板按文件 mtime 热加载，不需要重启。

## 三、升级

服务与插件在同一个仓库维护，升级仍然走同一条命令；安装器会新建 release 目录，
旧版本保留在 `releases/` 下可直接回滚。

已有服务器上如果只想替换个别文件，可以用 `tools/deploy-release.py`：

```bash
python3 tools/deploy-release.py --id 20260101T-example --stage /tmp/stage --restart bot-manager
```

它做的是：复制当前 release → 覆盖 `/tmp/stage` 下的同名文件 → 重算 `SHA256SUMS` →
校验全部通过才用 `mv -T` 原子切换 `current` 软链接。任何一步失败都不会动线上目录。

## 四、验证清单

```bash
systemctl is-active bot-manager astrbot
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:17321/api/health
```

- `/api/health` 返回 `200`；
- `/bm 状态` 显示采集挂钩数量、提醒与凭据任务在运行；
- `/bm 余额` 能返回金额（前提是面板侧已有可用凭据）；
- 正常使用一次 DeepSeek 搜索后，`/bm 搜索 今天` 出现流水。

## 五、安全加固（建议）

面板本身只绑回环，公网打不到；真正的暴露面是 SSH 与 AstrBot 自己的 WebUI。

### fail2ban 护 SSH

服务器上最常见的攻击是 SSH 密码爆破（实测一天数千次）。装 fail2ban 即可压住，
不影响现有的密钥登录：

```bash
apt-get install -y fail2ban python3-systemd
```

`/etc/fail2ban/jail.d/bot-manager.local`：

```ini
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 <你自己的出口 IP>
backend = systemd
bantime = 1h
findtime = 10m
maxretry = 5
banaction = iptables-multiport

[sshd]
enabled = true
port = 22
```

```bash
systemctl enable --now fail2ban
fail2ban-client status sshd          # 看当前封禁
fail2ban-client set sshd unbanip 1.2.3.4   # 误封时手动解封
```

`ignoreip` 一定要写上自己的常用 IP，否则改密码时连错 5 次会被自己关在门外。

### 不要给面板配按 IP 的 jail

面板经隧道访问时，来源地址永远是 `127.0.0.1`。用 fail2ban 按 IP 封面板，
封的就是你自己。面板的登录限速已内置在进程里（见 [内部接口](api.md#三登录防护)），不需要额外规则。

### 另外两处建议

- AstrBot WebUI 若不需要公网访问，把它绑回 `127.0.0.1`，从面板里嵌进去看，公网只留 SSH。
- SSH 只留密钥登录（`PasswordAuthentication no` + `PermitRootLogin prohibit-password`），
  改前确认公钥可登录并保留一个已连上的会话，改完立刻开新连接验证。

## 六、卸载

```bash
sudo systemctl disable --now bot-manager
sudo rm -f /etc/systemd/system/bot-manager.service
sudo rm -rf /opt/bot-manager /etc/bot-manager /var/lib/bot-manager
```

卸载服务不会删除 AstrBot 的插件与用量库；插件单独在 AstrBot 插件管理里卸载。
`/etc/bot-manager` 含凭据与令牌，确认不再使用后再删除。
