# Bot Manager 客户端 · macOS

用 SSH 私钥把服务器面板安全地拉到本机：建四条回环隧道，用浏览器打开，
并提供测试连接、生成密钥、安装公钥、重置服务密码。

## 一、运行要求

- macOS 12 或更新（更老的版本同样可用，只依赖系统自带组件）
- 系统自带的 `bash`、`ssh`、`ssh-keygen`、`osascript`、`security`、`lsof`、`curl`
- 不需要安装 Python / Node / Homebrew

## 二、第一次使用

1. 解压后把 `bot-manager.command` 放到任意目录（建议 `~/Applications/`）。
2. 首次下载会被 Gatekeeper 拦：右键 →「打开」，或执行一次
   ```bash
   xattr -d com.apple.quarantine /path/to/bot-manager.command
   chmod +x /path/to/bot-manager.command
   ```
3. 双击运行。第一次会让你填：
   服务器地址、SSH 端口（默认 22）、登录用户（通常 `root`）、私钥路径、本地端口（默认 17321）。
4. 点「测试连接」，看到「这把私钥可以登录 …」后点「连接并打开面板」。

隧道会一次映射四个端口，因为面板里「新窗口打开」的三项是在**本机浏览器**打开的：

| 本机端口 | 服务器端口 | 用途 |
| --- | --- | --- |
| 17321 | 17321 | 面板本体 |
| 6185 | 6185 | AstrBot WebUI |
| 5099 | 5099 | SnowLuma 控制台 |
| 6081 | 6081 | noVNC（QQ 扫码） |

某个端口在本机已被占用时会跳过它，并在日志里写明，其余端口照常映射。

## 三、菜单说明

| 菜单项 | 行为 |
| --- | --- |
| 连接并打开面板 | 端口被占用时先问你要「接管」还是「沿用已有隧道」，然后建隧道并打开浏览器 |
| 测试连接 | 只用一次 `ssh ... echo BM_OK` 验证私钥可用，不建隧道 |
| 断开连接 | 只关闭本客户端启动的 ssh 进程；若端口仍被别的隧道占用会明确提示 |
| 重置服务密码 | 选 AstrBot / SnowLuma / noVNC，输入新密码（两次），由服务器端脚本执行并回读结果 |
| 生成密钥… | `ssh-keygen -t ed25519`，公钥自动复制到剪贴板，可选立刻装到服务器 |
| 安装公钥到服务器… | 输入一次服务器登录密码，把 `.pub` 追加到 `authorized_keys` |
| 复制面板密码 | 从钥匙串取出面板密码放进剪贴板 |
| 设置… | 改服务器、端口、用户、私钥路径、本地端口；私钥口令与面板密码存钥匙串 |
| 查看日志 | 显示最近 20 行 `~/Library/Logs/BotManagerClient/client.log` |

## 四、密码与密钥存在哪

- 配置文件：`~/.config/bot-manager-client/settings.json`（权限 600，只有服务器地址/端口/路径，**不含密码**）
- 面板密码、私钥口令：**macOS 钥匙串**，service 名为 `bot-manager-client`
  查看：钥匙串访问 → 搜索 `bot-manager-client`
- 隧道日志：`~/Library/Logs/BotManagerClient/ssh.log`
- 客户端日志：`~/Library/Logs/BotManagerClient/client.log`

带口令的私钥不需要解密副本：客户端用 `SSH_ASKPASS` 把口令临时喂给 ssh
（`SSH_ASKPASS_REQUIRE=force`），口令不落盘、不进 argv。

如果你的 OpenSSH 版本较老不支持 `SSH_ASKPASS_REQUIRE`，可以改用 Homebrew 的 ssh：

```bash
BM_SSH_BIN=/opt/homebrew/bin/ssh /path/to/bot-manager.command
```

## 五、命令行排障

```bash
# 环境自检：ssh / 钥匙串 / 配置读写 / 端口探测
/path/to/bot-manager.command --selftest

# 链路诊断：连接测试 → 建隧道 → 四个端口 → 面板健康检查
/path/to/bot-manager.command --diagnose

# 版本
/path/to/bot-manager.command --version
```

诊断输出示例：

```
服务器：root@203.0.113.9:22
私钥：/Users/me/.ssh/id_ed25519
[连接测试] 成功：这把私钥可以登录 root@203.0.113.9
[隧道] 已启动（PID 41233）
[转发] 6185 -> 已转发
[转发] 5099 -> 已转发
[转发] 6081 -> 已转发
[面板] /api/health 200，链路可用
```

## 六、常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 双击没反应 / 提示无法验证 | Gatekeeper 隔离属性 | 右键「打开」，或 `xattr -d com.apple.quarantine` |
| `Permission denied (publickey)` | 公钥没装或私钥不对 | 用「安装公钥到服务器…」，或换私钥路径 |
| 提示 `Load key ... incorrect passphrase` | 私钥有口令但钥匙串里没有 | 「设置…」里重填私钥口令 |
| 隧道建了但面板无响应 | 服务器上 `bot-manager` 没跑 | `ssh` 上去 `systemctl status bot-manager` |
| 端口被占用 | 有别的隧道或程序占了 17321 | 连接时选「接管」，或换本地端口（如 17331） |
| 断开后仍能打开面板 | 端口由别的隧道占着 | 看日志里的 PID，或连接时选「接管」 |

## 七、与 Windows 版的差异

功能对齐：连接/断开、四端口转发、遗留隧道接管、生成密钥、安装公钥、三个服务的密码重置、日志。

实现差异：

| | Windows | macOS |
| --- | --- | --- |
| 界面 | PowerShell + WinForms | bash + `osascript` 原生对话框 |
| 凭据存储 | DPAPI（按用户加密） | 钥匙串（Keychain） |
| 口令私钥 | `ssh-keygen` 解密出临时副本，断开即删 | `SSH_ASKPASS` 临时喂口令，不产生副本 |
| 分发 | 单文件 exe | 单个 `.command` 脚本 |

macOS 版为首次发布，未在真实 Mac 上做过端到端验证（开发环境无 Mac），
逻辑用桩程序覆盖了隧道参数、端口跳过、结果解析与自检。
遇到问题请附 `--diagnose` 输出与 `client.log`。
