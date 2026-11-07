# 配置项与凭据下发

插件的配置都在 AstrBot 插件设置页「Bot Manager 集成管理」里，定义以
[`_conf_schema.json`](../_conf_schema.json) 为准。改完保存后，凭据类配置需要一次
AstrBot 重启才会进入新建任务；其余配置在下次轮询时生效。

## 一、余额提醒

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `notice_enable` | bool | `true` | 总开关。关闭后不轮询余额，`/bm 余额` 仍可手动查 |
| `low_balance_threshold` | float | `5.0` | 低于该值进入一个低余额周期；回升到阈值以上后重新武装 |
| `check_interval_minutes` | int | `5` | 轮询间隔，最小 60 秒 |
| `notify_target_qq` | list | `[]` | 私聊目标，可填多个，也支持一行用逗号/分号分隔 |
| `notify_target_groups` | list | `[]` | 群聊目标，同上 |
| `notify_platform` | string | `""` | 留空自动从最近会话推断（例如 `snowluma_onebot`） |
| `recharge_thanks_enable` | bool | `true` | 余额从低位回升（增幅 ≥ 0.01）时发一次致谢 |
| `recharge_thanks_group` | bool | `true` | 关掉则致谢只发私聊 |
| `fallback_auto_channel` | bool | `true` | 目标全空时沿用「记住管理员最近会话」的旧行为 |
| `low_balance_custom_text` | text | `""` | 留空按人设生成；可用 `{balance}`、`{threshold}` |
| `recharge_thanks_custom_text` | text | `""` | 留空按人设生成；可用 `{balance}` |

目标解析顺序：先按配置拼 UMO（`<平台>:FriendMessage:<QQ>` /
`<平台>:GroupMessage:<群号>`）；配置为空且回退开启时才用旧通道。
同一低余额周期只提醒一次，`sent` 的目标不会重发。

## 二、凭据下发

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `deepseek_api_key` | string | `""` | DeepSeek 平台 API Key，非空才下发到面板 |
| `panel_admin_password` | string | `""` | 面板登录密码（8-256 位）；非空才下发。**账号固定 `admin`，不开放配置** |
| `credential_sync_enable` | bool | `true` | 关掉后不再自动下发，`/bm 凭据 同步` 可手动触发 |
| `credential_sync_interval_minutes` | int | `30` | 指纹比对间隔，最小 5 分钟；没变化不推送 |

面板只有 `admin` 一个账号：安装器、`--reset-panel-password` 重置脚本与内部接口都写死这个名字，
所以插件侧也只填密码。登录框的 `WWW-Authenticate` 域里同样标注了 `username: admin`。
如果旧配置里还留着 `panel_admin_username`，插件会忽略它并在 `/bm 凭据` 里提示一次。

规则：

- 只在**指纹变化**时推送。状态写在
  `<数据目录>/plugin_data/astrbot_plugin_usage_telemetry/credential-sync.json`，
  只保留 `sha256` 前 16 位，不落明文；
- 因此你在面板「接口配置」页手工改的密钥不会被插件反复覆盖；
- 把配置清空只撤销本地下发记录，**不会**删除面板上已保存的凭据；
- 单项失败不影响另一项，失败原因记录在 `/bm 凭据` 的最后结果里；
- 密码过短（少于 8 位）会被面板拒绝（400），本地配置不受影响。

服务地址与令牌路径可用环境变量覆盖，须在启动 AstrBot 前设置：

| 环境变量 | 默认 |
| --- | --- |
| `BOT_MANAGER_INTERNAL_BASE_URL` | `http://127.0.0.1:17321` |
| `BOT_MANAGER_INTERNAL_TOKEN_PATH` | `/etc/bot-manager/plugin-token` |

## 三、面板侧配置

面板的「接口配置」页可以查看与修改余额服务凭据，字段与
`/etc/bot-manager/provider-credentials.json` 一一对应：

```json
{
  "version": 2,
  "deepseek": { "apiKey": "" },
  "siliconflow": { "apiKey": "" },
  "volcengine": { "accessKeyId": "", "secretAccessKey": "" },
  "bailian": { "accessKeyId": "", "accessKeySecret": "" },
  "custom": []
}
```

也可以在服务器上用配置程序写入内置字段（只覆盖内置项，自定义项原样保留）：

```bash
sudo python3 /opt/bot-manager/current/server-configure.py < /root/balances.json
```

`/root/balances.json` 只需包含要更新的字段：`deepseekApiKey`、`volcAccessKeyId`、
`volcSecretAccessKey`。文件权限保持仅管理员可读，不要为了配置把它改成全局可读。

单价、币种与优惠时段在「用量 → 单价配置」里改，落盘到
`/etc/bot-manager/model-pricing.json`；本地费用只是估算，不能替代官方账单。

## 四、常见配置错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/bm 凭据` 显示「未配置」 | 密钥或密码为空 | 在插件设置页填写并保存 |
| 显示「失败（HTTP 403）」 | 内部令牌不对或面板换了机器 | 核对 `/etc/bot-manager/plugin-token` 与 AstrBot 读取的路径 |
| 显示「失败（HTTP 400）」 | 密码太短，或内部令牌对应的面板版本过旧 | 密码至少 8 位；面板需 2.2.0 及以上 |
| 提示改了密码但登录不上 | 用了旧密码 | 面板热加载以文件为准，重新保存一次或看 `credentials.json` 的 mtime |
| 余额一直是「未知」 | 面板没有可用凭据或查询失败 | `/bm 余额` 看错误详情，或在面板「接口配置」页直接测试 |
