# 内部接口

面板（`service/server-panel.py`）只绑定 `127.0.0.1`，并对每个请求校验 `Host` 头，
只接受 `127.0.0.1` 与 `localhost`。所有响应都带 `Cache-Control: no-store`、
`X-Frame-Options: DENY` 与内容安全策略。

## 一、两种鉴权

| 用途 | 方式 | 凭据来源 |
| --- | --- | --- |
| 浏览器访问面板 | HTTP Basic，用户名 + 口令 | `/etc/bot-manager/credentials.json`（scrypt 摘要） |
| 插件调用内部接口 | 请求头 `X-Bot-Manager-Token` | `/etc/bot-manager/plugin-token`（≥32 字符） |

`/api/health` 不需要鉴权；`/api/internal/*` 只认令牌，不走 Basic。
写操作还会额外要求 `X-Bot-Manager-Request: <确认值>`，防止误触的表单提交。

## 二、面板接口（Basic 认证）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/`、`/index.html` | 面板单页 |
| GET | `/api/health` | 存活探针，返回 `{"ok":true}` |
| GET | `/api/config` | 外部链接、刷新间隔、`tunnel`（隧道目标展示值） |
| GET | `/api/status` | 服务状态（AstrBot/SnowLuma/noVNC/OneBot） |
| GET | `/api/metrics?range=&app=` | 资源曲线，`range` 为 `1h`/`24h`/`7d` 等 |
| GET | `/api/balances?refresh=1` | 各服务商余额（`refresh=1` 强制刷新） |
| GET | `/api/provider-config` | 余额凭据摘要（不含密钥）；`?reveal=1` 需确认头 |
| POST | `/api/provider-config` | 更新凭据，确认头 `provider-config` |
| GET | `/api/usage-report` | 用量报表（时间范围、模型、插件筛选） |
| GET | `/api/usage-pricing` | 单价配置 |
| POST | `/api/usage-pricing` | 更新单价，确认头 `usage-pricing` |
| GET | `/api/usage-models` | 可筛选的模型清单 |
| GET | `/api/usage-export.csv` | 导出明细 CSV |
| GET | `/api/logs?service=&order=` | 聚合日志，`service` ∈ `astrbot`/`snowluma`/`novnc`/`onebot` |

`?reveal=1` 需要同时带 `X-Bot-Manager-Request: provider-config-reveal`，
用于防止误让浏览器缓存明文密钥。

## 三、内部接口（令牌认证）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/internal/deepseek-balance` | DeepSeek 余额快照，含 `balances[]` 与查询时间 |
| GET | `/api/internal/search-usage?since=&until=` | 搜索/抓取计数，单位秒 |
| POST | `/api/internal/search-event` | 插件上报搜索/抓取事件（1-50 条/次，2 小时内补报） |
| GET | `/api/internal/credentials` | 面板口令账号与余额凭据的配置状态，**不回传密钥** |
| POST | `/api/internal/provider-credentials` | 写入余额凭据，body 形如 `{"deepseek":{"apiKey":"..."}}` |
| POST | `/api/internal/panel-credentials` | 设置面板账号口令，body `{"username":"...","password":"..."}` |

### panel-credentials 的约束

- 用户名 1-64 位，仅字母、数字、点、下划线、短横线；
- 口令 8-256 位；服务端用 `scrypt(salt=16B, n=2^14, r=8, p=1, dklen=32)` 生成摘要；
- 写入前把旧文件备份为 `credentials.json.bak.<UTC 时间戳>`，最多保留 8 份；
- 采用「临时文件 + `os.replace`」原子替换，权限固定 `0600 root:root`；
- 面板按文件 `mtime` 热加载，新口令**立即生效**，无需重启；文件损坏时保留旧口令。

### 请求体限制

内部 JSON 接口单次最多 8 KiB，超出或非 `application/json` 直接 4xx；
搜索事件接口限制 1-50 条、单条 4 KiB。所有接口都有超时，避免面板被慢客户端拖住。

## 四、插件侧调用方式

插件只用环境变量控制的两项配置：

```python
SERVICE_BASE = os.environ.get("BOT_MANAGER_INTERNAL_BASE_URL", "http://127.0.0.1:17321")
TOKEN_PATH   = os.environ.get("BOT_MANAGER_INTERNAL_TOKEN_PATH", "/etc/bot-manager/plugin-token")
```

调用失败只影响当前功能（余额查询、搜索汇总或凭据下发），不会中断 AstrBot 主流程；
失败原因会写进 `/bm 状态` 与 `/bm 凭据` 的可见结果里。

## 五、为什么不直接暴露端口

面板拥有服务状态、日志、用量明细与凭据管理能力，属于高权限界面。
只绑回环 + SSH 隧道把「谁能访问」收敛到服务器账号本身，
不需要再维护一套对公网开放的 HTTPS 与反向代理配置。
