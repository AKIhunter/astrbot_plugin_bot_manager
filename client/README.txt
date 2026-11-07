Bot Manager 客户端 — 使用说明
================================

一、这是什么
  一个 Windows 桌面小工具：用 SSH 私钥登录你的服务器，在本机开一条回环隧道，
  然后用浏览器查看服务器上的 Bot Manager 管理面板（127.0.0.1:17321）。
  面板本身只监听服务器回环地址，所以必须通过隧道访问，公网直连是打不开的。

二、第一次使用
  1. 双击 BotManagerClient.exe（单文件版）或 run-client.vbs。
  2. 「服务器」填 IP 或域名，「端口」填 SSH 端口（默认 22），「用户名」填 root。
  3. 「私钥文件」点「浏览…」选你的私钥（例如 my-server.pem）。
  4. 若私钥带口令，把它填到「私钥口令」。
  5. 点「测试连接」，看到「连接成功：这个私钥可以用」再往下走。
  6. 点「连接并打开面板」，浏览器会自动打开 http://127.0.0.1:17321/。

三、面板登录
  面板需要账号密码（Basic 认证）。在客户端「面板账号 / 密码」里填一次并「保存设置」，
  之后点「复制密码」即可把密码放进剪贴板，浏览器登录框直接 Ctrl+V。
  这个账号密码也可以在 AstrBot 插件配置页「Bot Manager 集成管理 → 管理面板登录账号/密码」
  里改，插件会自动推送到服务器并立即生效，不用重启面板。

四、没有私钥怎么办
  1. 点「生成密钥…」，选择保存位置（默认放在本机配置目录），会生成 id_ed25519 / id_ed25519.pub，
     公钥自动复制到剪贴板。
  2. 点「安装公钥到服务器…」，会打开一个命令行窗口，按提示输入服务器登录密码，
     公钥会被追加进服务器的 ~/.ssh/authorized_keys。
  3. 回到主界面点「测试连接」验证。

五、设置存在哪里
  %LOCALAPPDATA%\BotManagerClient\settings.json
  密码与私钥口令用 Windows DPAPI 按当前用户加密，换用户或换机器都解不开。
  同一目录下的 known_hosts 记录首次连接时确认过的服务器指纹。

六、带口令的私钥怎么处理
  Windows 上的 ssh 无法可靠地弹出密码输入框，客户端会在会话目录里解密出一份临时副本
  (session-key-*) 供本次连接使用，断开连接或关闭窗口时立即删除。
  如果你介意，可以用 ssh-keygen -p -f <私钥> -N "" 去掉口令，或另建一把无口令的部署专用密钥。

七、常见问题
  - 「服务器拒绝了这个私钥」：公钥没装进服务器，或用错了私钥。
  - 「本地端口被其他程序占用」：把「本地端口」改成 17331 之类的其他端口。
  - 「隧道已建立，但面板没有响应」：服务器上 bot-manager 服务没在跑，
    在服务器执行 systemctl status bot-manager 查看。
  - 想排查链路：powershell -NoProfile -STA -File BotManagerClient.ps1 -Diagnose
    （会依次输出连接测试、隧道、面板健康检查的结果）
  - 想校验程序自身：powershell -NoProfile -STA -File BotManagerClient.ps1 -SelfTest

八、兼容性
  Windows 10 / 11 自带 OpenSSH 客户端（Windows/System32/OpenSSH/ssh.exe）与 PowerShell 5.1，
  不需要额外安装任何东西。
