# Bot Manager 集成管理 2.0.0

合并模型/向量用量采集、DeepSeek 搜索/抓取计量、余额查询与提醒，
增加统一管理入口和服务器全部注册指令查询。
技术 ID 沿用 `astrbot_plugin_usage_telemetry`，避免迁移历史配置与状态。

## 指令

全部管理入口仅限 AstrBot 管理员；默认前缀 `/`，入口可用
`/botmanager`、`/bm`、`/bot管理`。

| 指令 | 功能 |
| --- | --- |
| `/bm`、`/bm 帮助` | 完整管理帮助 |
| `/bm 状态` | 提供商采集、搜索补丁、提醒任务及指令统计 |
| `/bm 余额` | DeepSeek 实时余额 |
| `/bm 用量 [时间]` | 采集调用、Token、未知用量、插件排行 |
| `/bm 搜索 [时间]` | 成功搜索/抓取次数及计量费用 |
| `/bm 指令 [关键词或插件名] [页码]` | 注册指令、参数、别名、权限及停用状态 |
| `/bm 详情 <完整指令名> [页码]` | 完整原生命令说明、全部别名 |

时间支持 `今天`、`昨天`、`7天`、`30天`、`全部`，按北京时间。
兼容 `/账户余额`、`/余额`、`/钱包` 和管理员 LLM 工具 `get_wallet_balance`。
停用插件只显示帮助，查询不会启用或执行它们。

## 代码逻辑

- `main.py`：沿用提供商与底层客户端包装、取消/重试计量、提醒状态机；
  生命周期内启停搜索包装，15秒刷新提供商与指令快照；统一管理员指令分发。
- `search_meter.py`：两个 DeepSeek 工具各包装一次；有结果或成功但结果为空时计量；
  异常及配置错误不计量。512条队列后台上报，保留结果未知/丢弃计数；
  卸载时还原自己拥有的包装，不误覆盖其他包装。
- `command_query.py`：读取真实 Handler/Star 注册表，包含停用插件的已注册命令；
  使用完整指令名识别父组，继承其权限；列表8项一页，详情2400字符一页。
  查询不调用其他插件的过滤器或处理函数。
- `usage_query.py`：以只读连接读取原用量库；将未知用量明确显示；
  搜索库由本机受限接口汇总，避免只读打开 WAL 库仍需创建共享文件的问题。

数据库和状态沿用：

- `<AstrBot 数据目录>/plugin_usage.db`
- `<AstrBot 数据目录>/plugin_data/astrbot_plugin_usage_telemetry/balance-notifications.json`
- `<AstrBot 数据目录>/plugin_data/astrbot_plugin_usage_telemetry/command-catalog.json`
- 服务器搜索流水：`/var/lib/bot-manager/metrics.db`

公共发行版使用 AstrBot 的数据目录 API；原服务器工作目录不变时仍读取原数据。
`BOT_MANAGER_INTERNAL_BASE_URL` 可指定配套服务地址，默认 `http://127.0.0.1:17321`。
`BOT_MANAGER_INTERNAL_TOKEN_PATH` 可指定内部令牌文件，默认 `/etc/bot-manager/plugin-token`。
两个环境变量应在启动 AstrBot 前设置。没有配套服务时，余额与搜索汇总不可用；
模型用量采集和注册指令查询仍可使用。不要把上游模型 API Key 当成内部令牌。

## 服务端依赖与升级

AstrBot >=4.29.0；Python 3.10+，标准库实现，无额外依赖。
本机 Bot Manager 服务需提供并使用原内部令牌校验：

- `GET /api/internal/deepseek-balance`
- `GET /api/internal/search-usage?since=<秒时间戳>&until=<秒时间戳>`
- `POST /api/internal/search-event`

令牌从 `/etc/bot-manager/plugin-token` 读取，不随源码包分发。
新增只读接口的源码位于服务端目录 `server-panel.py`；本次服务器已同步升级。
源码 ZIP 是插件源码包；在别处安装时也需要匹配的服务端内部接口。

旧 `astrbot_plugin_search_meter` 必须移出插件加载目录后重启，不能让两个计量器并存。
服务端安装器上传全部5个运行文件，保留并移出旧搜索插件，失败时恢复旧目录。
插件维护源码位于 [源码总库](https://github.com/AKIhunter/astrbot_plugin) 的 `bot-manager/`，
本仓库为市场发布镜像。
部署前必须核对当前线上基线。

## 验证与限制

21项采集回归、17项真实框架合并测试、4项内部搜索接口测试通过。
线上核对采集PID/哈希、两搜索补丁、全部历史调用ID及真实账户查询权限。

采集调用数与上游实际请求数不同，重试明细请在集成平台查看。
进程强退或响应丢失仍可能导致搜索流水缺失，未知/失败记录需和官方账单对账；
不进行无法判重的自动重试。第三方插件没有注册原生说明时，查询会如实提示。
