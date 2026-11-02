# Bot Manager 独立维护

维护仓库：https://github.com/AKIhunter/astrbot_plugin_bot_manager

2026-11-02 起，本仓库维护插件和配套服务，不再使用总库与发布镜像双份源码。
默认分支为 `main`，已有插件 ID、版本、市场登记地址和提交历史继续保留。

| 内容 | 维护位置 | 获取方式 |
| --- | --- | --- |
| AstrBot 插件 | 仓库根目录 | 市场安装或 GitHub 插件 ZIP |
| 配套服务 | `service/` | 完整 `git clone` |
| 场景与使用步骤 | `README.md` | 仓库首页 |
| 迁移来源与原哈希 | `docs/migration-manifest.json` | 仓库中只读查询 |

日常修改直接在本仓库完成，验证后提交并推送；无需运行 `git subtree split`，
也无需回写 `astrbot_plugin`。服务改动不应新增插件源码副本。
不要提交账户配置、API Key、内部接口令牌、SSH 私钥或数据库。

插件安装 ZIP 会排除 `service/` 与 `docs/`，但 README 和六个必要插件文件仍保留。
发布前同时检查根目录 `metadata.yaml`、`main.py` 以及实际 GitHub ZIP，
避免服务目录混入插件安装包。服务安装器保留旧部署包目录结构兼容性。

插件安装入口和原市场条目不变：
https://cloud.astrbot.app/plugin/AKIhunter/astrbot_plugin_usage_telemetry
本次仓库整理不是插件功能升级，不重复提交已在审核中的相同版本。

原总库保留历史快照并归档。源码迁入前的文件清单及 SHA-256 记在
`migration-manifest.json`；服务脚本只调整源码定位，插件核心代码不变。
归档可通过 GitHub 的仓库设置撤销，历史源码也仍可读取。
恢复维护应优先在本仓库继续，不要用旧总库快照覆盖新版本或服务器数据。
