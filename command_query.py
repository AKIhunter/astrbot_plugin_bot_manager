"""从运行中的 AstrBot 注册表读取命令，包含分组、别名和继承的权限。"""

import inspect
import functools
import json
import os
from pathlib import Path

HELP = """Bot Manager 集成管理（仅 AstrBot 管理员）
/botmanager [帮助]：完整管理指令
/botmanager 状态：用量采集、搜索补丁、提醒状态
/botmanager 余额：查询 DeepSeek 实时余额
/botmanager 用量 [今天|昨天|7天|30天|全部]：调用数、Token、未知用量、插件排行
/botmanager 搜索 [今天|昨天|7天|30天|全部]：搜索/抓取次数和计量费用
/botmanager 指令 [关键词或插件名] [页码]：查询服务器已注册指令（含停用插件）
/botmanager 详情 <完整指令名> [页码]：参数、所有别名、完整原生说明

入口别名：/bm、/bot管理
查询示例：/bm 指令；/bm 指令 2；/bm 指令 陪伴；/bm 详情 陪伴 改画像
兼容原指令：/账户余额、/余额、/钱包
时间按北京时间；搜索计量与模型 Token 分开统计。
其他插件的指令仍按各自权限执行；详情中的 [参数] 为可选，<参数> 为必填。"""


def _original_handler(handler):
    # 框架加载后用 partial(handler, plugin) 绑定 self，不能读取 partial 的文档。
    while isinstance(handler, functools.partial):
        handler = handler.func
    if inspect.ismethod(handler):
        handler = handler.__func__
    return inspect.unwrap(handler)


def _parameter_text(handler):
    signature = inspect.signature(_original_handler(handler))
    parts = []
    for index, parameter in enumerate(signature.parameters.values()):
        if index < 2:
            continue
        if getattr(parameter.annotation, "__name__", "") == "GreedyStr":
            parts.append(f"[{parameter.name}...]")
        elif parameter.default is inspect.Parameter.empty:
            parts.append(f"<{parameter.name}>")
        else:
            parts.append(f"[{parameter.name}={parameter.default}]")
    return " ".join(parts)


def command_catalog():
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = list(star_handlers_registry)
    groups = [
        (handler, item)
        for handler in handlers for item in handler.event_filters
        if isinstance(item, CommandGroupFilter)
    ]

    def group_name(group):
        parent = getattr(group, "parent_group", None)
        return (group_name(parent) + " " if parent else "") + group.group_name

    entries = []
    for handler in handlers:
        plugin = star_map.get(handler.handler_module_path)
        if plugin is None:
            continue
        filters = list(handler.event_filters)
        for item in filters:
            if not isinstance(item, (CommandFilter, CommandGroupFilter)):
                continue
            names = sorted(set(item.get_complete_command_names()))
            if not names:
                continue
            # 取原始主名组合，排序后的第一项可能是别名。
            if isinstance(item, CommandGroupFilter):
                canonical = group_name(item)
            else:
                parents = getattr(item, "parent_command_names", []) or [""]
                parent = next((
                    group_name(group) for owner, group in groups
                    if owner.handler_module_path == handler.handler_module_path
                    and group_name(group) in parents
                ), parents[0])
                canonical = (parent + " " if parent else "") + item.command_name
            inherited = list(filters)
            # 子命令只有 CommandFilter。组级权限保存在组的 Handler 上，
            # 必须沿完整名称找回；只读子命令过滤器会漏标管理员权限。
            for owner, group in groups:
                if owner.handler_module_path != handler.handler_module_path:
                    continue
                if any(
                    name.startswith(parent + " ")
                    for name in names for parent in group.get_complete_command_names()
                ):
                    inherited.extend(owner.event_filters)
                    inherited.extend(getattr(group, "custom_filter_list", []))
            for candidate in filters:
                if isinstance(candidate, CommandGroupFilter):
                    current = candidate
                    while current:
                        inherited.extend(getattr(current, "custom_filter_list", []))
                        current = getattr(current, "parent_group", None)
                inherited.extend(getattr(candidate, "custom_filter_list", []))
            permissions = sorted({
                str(f.permission_type.name)
                for f in inherited if hasattr(f, "permission_type")
            })
            labels = {
                "ADMIN": "AstrBot管理员", "MEMBER": "成员",
                "GROUP_ADMIN": "群聊限AstrBot管理员",
                "SHARED_GROUP_ADMIN": "管理员/隔离会话",
            }
            permission = "、".join(labels.get(p, p) for p in permissions) or "按插件校验"
            # 不执行其他插件过滤器，查询列表本身不触发指令的副作用。
            entries.append({
                "command": canonical,
                "names": names,
                "plugin": plugin.name,
                "displayName": plugin.display_name or plugin.name,
                "enabled": bool(handler.enabled),
                "pluginActive": bool(plugin.activated),
                "permission": permission,
                "parameters": _parameter_text(handler.handler)
                if isinstance(item, CommandFilter) else "",
                "description": handler.desc or inspect.getdoc(_original_handler(handler.handler)) or "",
                "group": isinstance(item, CommandGroupFilter),
            })
    unique = {}
    for entry in entries:
        key = (entry["plugin"], entry["command"], entry["group"])
        unique[key] = entry
    return sorted(unique.values(), key=lambda e: (e["plugin"] or "", e["command"]))


def save_catalog(path, catalog, runtime):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"runtime": runtime, "commands": catalog}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def select(catalog, keyword):
    text = keyword.strip().lstrip("/").casefold()
    if not text:
        return catalog
    return [
        entry for entry in catalog
        if text in " ".join([
            *entry["names"], entry["plugin"] or "", entry["displayName"] or "",
            entry["description"],
        ]).casefold()
    ]


def list_commands(catalog, keyword="", page=1):
    found = select(catalog, keyword)
    if not found:
        return f"没有匹配「{keyword}」的已注册指令。可用 /bm 指令 查看全部。"
    pages = (len(found) + 7) // 8
    if not 1 <= page <= pages:
        return f"页码超出范围：共 {pages} 页，请输入 1～{pages}。"
    lines = [f"服务器指令：{len(found)} 项，第 {page}/{pages} 页"]
    for entry in found[(page - 1) * 8:page * 8]:
        state = " [插件停用]" if not entry.get("pluginActive", True) else (
            " [已禁用]" if not entry["enabled"] else ""
        )
        lines.append(
            f"/{entry['command']} {entry['parameters']}{state}\n"
            f"  {entry['displayName']} · {entry['permission']}"
        )
        aliases = [name for name in entry["names"] if name != entry["command"]]
        if aliases:
            preview = "、".join("/" + name for name in aliases)
            lines.append("  别名：" + (preview if len(preview) <= 180 else preview[:180] + "…（详见详情）"))
        if entry["description"]:
            lines.append("  " + entry["description"].splitlines()[0][:180])
    search = f" {keyword}" if keyword else ""
    if page < pages:
        lines.append(f"下一页：/bm 指令{search} {page + 1}")
    lines.append("完整参数与说明：/bm 详情 <指令名>")
    return "\n".join(lines)


def command_details(catalog, keyword, page=1):
    normalized = keyword.strip().lstrip("/").casefold()
    exact = [
        entry for entry in catalog
        if normalized in [name.casefold() for name in entry["names"]]
    ]
    if not exact:
        return list_commands(catalog, keyword, page)
    chunks = []
    for entry in exact:
        text = (
            f"/{entry['command']} {entry['parameters']}\n"
            f"插件：{entry['displayName']} ({entry['plugin']})\n"
            f"权限：{entry['permission']}；"
            f"状态：{'插件停用' if not entry.get('pluginActive', True) else ('启用' if entry['enabled'] else '禁用')}\n"
            f"全部名称：{'、'.join('/' + name for name in entry['names'])}\n"
            f"{entry['description'] or '插件未提供原生命令说明，请参考该插件帮助。'}"
        )
        chunks.extend(text[index:index + 2400] for index in range(0, len(text), 2400))
    if not 1 <= page <= len(chunks):
        return f"详情共 {len(chunks)} 页，请输入 1～{len(chunks)}。"
    tail = f"\n详情 {page}/{len(chunks)} 页"
    if page < len(chunks):
        tail += f"\n下一页：/bm 详情 {keyword} {page + 1}"
    return chunks[page - 1] + tail
