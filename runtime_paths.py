"""公共发行版读取 AstrBot 数据目录，保留既有 Linux 服务器数据位置。"""

import os
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

DATA_ROOT = Path(get_astrbot_data_path())
DATABASE_PATH = DATA_ROOT / "plugin_usage.db"
STATE_PATH = DATA_ROOT / "plugin_data" / "astrbot_plugin_usage_telemetry" / "balance-notifications.json"
# 配套服务可以继续使用 /etc 原路径；其他部署通过环境变量指定内部令牌。
TOKEN_PATH = Path(os.environ.get("BOT_MANAGER_INTERNAL_TOKEN_PATH", "/etc/bot-manager/plugin-token"))
SERVICE_BASE = os.environ.get("BOT_MANAGER_INTERNAL_BASE_URL", "http://127.0.0.1:17321").rstrip("/")
