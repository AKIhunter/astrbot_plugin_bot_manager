# 更新日志

## 2.2.0 — 2026-11-08

### 新增：凭据可以在插件里配

- 插件配置页新增 `deepseek_api_key`、`panel_admin_username`、`panel_admin_password`、
  `credential_sync_enable`、`credential_sync_interval_minutes`。
- 新增凭据下发任务：按指纹比对，只在配置变化时把密钥与面板账密推送到本机面板，
  状态落 `credential-sync.json`，只保留 `sha256` 前 16 位。
- 新增 `/bm 凭据`（查看同步状态）与 `/bm 凭据 同步`（立即重新下发）。

### 新增：面板内部接口

- `POST /api/internal/provider-credentials` 写入余额凭据；
- `POST /api/internal/panel-credentials` 设置面板账号口令，服务端计算 scrypt 摘要、
  原子替换并保留 8 份备份；
- `GET /api/internal/credentials` 返回配置状态，不回传密钥；
- `PanelCredentialVerifier` 改为按文件 `mtime` 热加载，改密立即生效，无需重启。

### 新增：Windows 客户端

- `client/BotManagerClient.ps1`：SSH 私钥 → 回环隧道 → 浏览器打开面板；
  支持生成密钥、安装公钥、复制面板密码、单文件 exe 打包（`build-client.ps1`）。
- 配置保存在 `%LOCALAPPDATA%\BotManagerClient\`，密码与私钥口令用 DPAPI 加密；
  带口令的私钥在会话目录解密出临时副本，断开即删。

### 其他

- 面板侧栏「SSH 隧道目标」改为读取 `/etc/bot-manager/tunnel-target`，
  仓库模板只保留占位符，不再把服务器地址写进源码。
- 插件配置项说明与 README 中的示例统一为占位符，移除真实 QQ 号、群号与服务器地址。
- 安装器支持直接从仓库根目录读取插件源码（自动暂存，不落库），
  修复「需要手工把插件复制到 `service/`」的版本漂移隐患。

## 2.1.0 — 2026-11-07

- 余额提醒搬进插件配置页：阈值、检查间隔、QQ/群目标、平台标识、致谢开关、
  未配置时的自动回退策略、两段自定义文案（支持 `{balance}`/`{threshold}`）。
- 新增 `/bm 通知`、`/bm 通知 测试`；测试按 UMO 去重，避免同一目标重发多次。
- 采集口径修正：运行时观测到的向量调用只作观测计数，不再与网关流水重复计入调用数。

## 2.0.0 — 2026-11-02

- 合并模型/向量用量采集、DeepSeek 搜索/抓取计量、余额查询与提醒，
  统一管理入口 `/bm` 与服务器注册指令查询。
- 技术 ID 沿用 `astrbot_plugin_usage_telemetry`，历史配置与状态不迁移。
- 配套服务迁入本仓库 `service/`，不再维护「总库 + 发布镜像」双份源码。

## 1.x

- 单点功能版本：用量采集、余额提醒、指令查询分别迭代，详见仓库历史提交。
