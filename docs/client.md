# 客户端与 SSH 隧道

面板只监听服务器的 `127.0.0.1:17321`，公网无法直连。客户端做的事很简单：
用 SSH 私钥登录服务器，在本机开回环隧道，然后用浏览器打开它。

```
本机浏览器 → http://127.0.0.1:17321 → SSH 隧道 → 服务器 127.0.0.1:17321 → 面板
```

客户端会同时映射四个端口，因为面板里「新窗口打开」的三个入口是在**本机浏览器**里打开的：

| 本机端口 | 服务器端口 | 用途 |
| --- | --- | --- |
| 17321 | 17321 | 面板本体 |
| 6185 | 6185 | AstrBot WebUI |
| 5099 | 5099 | SnowLuma 控制台 |
| 6081 | 6081 | noVNC（QQ 扫码） |

如果某个端口在本机已被占用，客户端会跳过它并在日志里写明，其余端口照常映射。

两个平台各有一份实现，功能对齐、交互不同：

| | Windows | macOS |
| --- | --- | --- |
| 文件 | `client/BotManagerClient.ps1`（+ 单文件 exe） | `client/macos/bot-manager.command` |
| 界面 | PowerShell + WinForms | bash + `osascript` 原生对话框 |
| 凭据存储 | DPAPI（按当前 Windows 账户加密） | 钥匙串（service `bot-manager-client`） |
| 口令私钥 | `ssh-keygen` 解出临时副本，断开即删 | `SSH_ASKPASS` 临时喂口令，不产生副本 |
| 详见 | 本文（下半部分默认讲 Windows） | [macOS 说明](../client/macos/README.md) |


## 一、获取

- **Windows 单文件 exe**：见仓库 Release 附件 `BotManagerClient.exe`，双击即用，无需安装。
- **Windows 源码**：`client/BotManagerClient.ps1`，用 `run-client.vbs` 或
  `powershell -NoProfile -STA -ExecutionPolicy Bypass -File BotManagerClient.ps1` 启动。
- **macOS**：Release 附件 `BotManagerClient-macos-*.zip`，解压后双击
  `bot-manager.command`；首次运行需右键「打开」或在终端
  `xattr -d com.apple.quarantine` 放行。详见 [macOS 说明](../client/macos/README.md)。

依赖 Windows 10/11 自带的 OpenSSH（`ssh.exe`、`ssh-keygen.exe`）与 PowerShell 5.1，
不需要额外运行时。

## 二、首次配置

| 字段 | 说明 |
| --- | --- |
| 服务器 / 端口 | 服务器地址与 SSH 端口（默认 22） |
| 用户名 | 登录用户，通常是 `root` |
| 私钥文件 | 你的 SSH 私钥；同目录有 `.pub` 时可以直接安装公钥 |
| 私钥口令 | 私钥加密时填写（见下文） |
| 面板账号 / 密码 | 面板 Basic 认证，用于「复制密码」按钮 |
| 本地端口 | 隧道在本机监听的端口，默认 17321 |

顺序：**测试连接** → 看到「连接成功：这个私钥可以用」→ **连接并打开面板**。
连上后状态栏显示 `已连接：http://127.0.0.1:17321/`，浏览器会自动打开，
面板密码同时被放进剪贴板，登录框直接 `Ctrl+V`。

## 三、没有私钥

1. 点「生成密钥…」，选择保存位置，生成 `id_ed25519` 与 `id_ed25519.pub`，
   公钥自动复制到剪贴板；
2. 点「安装公钥到服务器…」，会弹出命令行窗口，输入服务器登录密码即可把公钥
   追加进 `~/.ssh/authorized_keys`；
3. 回到主界面点「测试连接」验证。

## 四、带口令的私钥怎么处理

Windows 上 `ssh` 无法可靠地弹出密码输入框（`SSH_ASKPASS` 的提示串会被拼进命令行），
所以客户端用 `ssh-keygen -p` 在会话目录解密出一份**临时副本**供本次连接使用：
文件名是 `session-key-<随机串>`，权限收紧到当前用户，断开连接或关闭窗口立即删除。

如果不希望留下任何解密副本，可以：

```powershell
ssh-keygen -p -f C:\path\to\key -N ""
```

去掉口令，或为客户端单独生成一把无口令的部署专用密钥，用完即删。

## 五、设置存在哪里

```
%LOCALAPPDATA%\BotManagerClient\
├── settings.json    # 服务器、端口、用户、私钥路径、本地端口（密码类字段为密文）
├── known_hosts      # 首次连接时确认的主机指纹
├── ssh.log          # 隧道进程的日志（失败时看这里）
└── client.log       # 客户端自身启动日志（双击 exe 无控制台时排障用）
```

`settings.json` 里的密码与口令用 Windows DPAPI（CurrentUser 作用域）加密，
换用户或换机器都无法解密；删除该目录即可清空所有本地配置。

## 六、自检与诊断

```powershell
# 无界面自检：设置读写、DPAPI、引号拼接、端口探测、口令私钥解密、ssh 可用性
powershell -NoProfile -STA -File client\BotManagerClient.ps1 -SelfTest

# 链路诊断：测试连接 → 建隧道 → 检查四个端口 → 探测面板与三个服务的 HTTP 状态
powershell -NoProfile -STA -File client\BotManagerClient.ps1 -Diagnose
```

诊断模式的输出同时写入 `client.log`（管道里中文可能被编码弄乱，看日志更稳）：

```
[隧道] SSH PID 27544
[隧道] 本地端口已监听
[转发] AstrBot(6185) -> 已转发 HTTP 200
[转发] SnowLuma(5099) -> 已转发 HTTP 200
[转发] noVNC(6081) -> 已转发 HTTP 200
[面板] /api/health 200，链路可用
```

退出码 `0` 表示整条链路可用。

## 七、断开与遗留隧道

「断开」只会关闭**本客户端自己启动**的那个 `ssh` 进程。如果断开后 17321 仍然能打开面板，
说明还有别处启动的隧道在跑（例如旧的启动脚本、或另一个客户端窗口），
此时状态栏会显示「仍有外部隧道占用 17321」，日志里会写明占用者 PID。

再次点「连接并打开面板」时，如果发现端口已被占用，会弹窗三选一：

| 选项 | 行为 |
| --- | --- |
| 是 | 结束占用端口的 `ssh` 进程（不是 ssh 时会拒绝），然后建立本客户端的隧道 |
| 否 | 直接沿用已有隧道；本客户端不会接管，断开时也不会关闭它 |
| 取消 | 不做任何改动 |

## 八、服务密码重置

客户端底部「服务密码重置」有三个按钮，通过 SSH 调用服务器上的
`/opt/bot-manager/current/reset-service-password.py`：

| 目标 | 实际改动 | 影响 |
| --- | --- | --- |
| AstrBot | 重写 `cmd_config.json` 的 pbkdf2 摘要并**重启 AstrBot** | 约 10 秒不可用 |
| SnowLuma | 重写 `/app/data/config/webui.json`（scrypt）并重启 `snowluma` 进程 | 只重启 bridge，**QQ 不掉线** |
| noVNC / VNC | 写 `/app/data/config/vnc-password`、重生成 `/root/.vnc/passwd`、重启 x11vnc | 已连接的 VNC 会断开，刷新页面重连 |

要点：

- 密码**走 stdin**，不出现在命令行参数或 shell 历史里；服务端脚本也只用 `read` 接收。
- 重置后服务端会**立刻用新密码试登录**，返回「已重置并验证登录成功」才算成功；
  失败时把服务端的原始原因带回来，不会假装成功。
- VNC 密码最长 **8 位**（RFB 协议限制），输入框会限制长度。
- AstrBot 账号固定 `astrbot`，SnowLuma 账号固定 `admin`，两者都不在重置范围内。
- 无界面等价的调用方式（密码从标准输入读一行）：

```powershell
"AstrBot-Temp-2026" | powershell -NoProfile -STA -File client\BotManagerClient.ps1 -ResetTarget astrbot
```

返回单行 JSON：`{"ok":true,"target":"astrbot","message":"已重置并验证登录成功","detail":"备份：..."}`

## 九、重新打包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File client\build-client.ps1
```

用 Windows 自带的 IExpress 生成单文件 exe，输出到 `client\dist\`。
两个必须注意的点：

- IExpress 的 `.sed` 必须是 **CRLF + 系统 ANSI 编码**，写成 UTF-8 会静默失败；
- `AppLaunched` 直接写 `powershell.exe ... -File BotManagerClient.ps1`，
  不要再套一层 `.vbs`——部分机器上脚本宿主被策略限制，套一层就拉不起来。

## 十、常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 服务器拒绝了这个私钥 | 公钥没装或选错私钥 | 用「安装公钥到服务器…」，或核对 `.pub` 是否在 `authorized_keys` |
| 私钥口令不正确 | 口令错，或私钥格式不支持 | 重新输入；必要时 `ssh-keygen -p` 去口令 |
| 本地端口被其他程序占用 | 已有别的隧道或程序占了 17321 | 连接时选「是」接管，或换本地端口（如 17331） |
| 断开后仍能打开面板 | 有别的隧道进程在跑 | 看 `client.log` 里的 PID，或用连接时的「接管」选项 |
| 面板里 AstrBot/noVNC 打不开 | 对应端口没被转发（本机被占用） | `-Diagnose` 看 `[转发]` 行；释放本机端口后重连 |
| 隧道已建立，但面板没有响应 | 服务器上 `bot-manager` 没跑 | `systemctl status bot-manager` |
| 连接被拒绝：主机密钥变了 | 服务器重装过 | 确认是本人操作后删除 `known_hosts` 再连 |
| 双击 exe 没反应 | 启动阶段报错但无控制台 | 看 `client.log`，或直接跑 ps1 看报错 |
