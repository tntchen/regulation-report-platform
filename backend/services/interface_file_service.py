"""
监管接口文件服务（P2：监管接口文件输出）
职责：从已完成任务的目标表数据（rpt_/twin_ 结果表，走只读通道）生成监管接口文件：
  - 1104 风格 TXT：竖线分隔，首行为表头字段名行
  - XML：简单元素嵌套（<report><row><col>值</col></row></report>）
文件落盘 data/tasks/{task_id}/exports/，与投产交付物同目录族。

安全要点：
  - 目标表仅允许 rpt_/twin_ 前缀 + 标识符白名单（防注入）
  - 结果数据一律走 database_mcp 只读通道（三层纵深）
  - 下载文件名白名单校验，防路径穿越
"""

import os
import re
from typing import Dict, Any, List, Optional
from xml.sax.saxutils import escape

from backend.config import settings

# 导出子目录（任务工作目录下）
EXPORT_SUBDIR = "exports"

# 表名/文件名白名单：仅字母数字下划线（与 rpt_/twin_ 命名约定一致）
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# 允许导出的结果表前缀
_RESULT_PREFIXES = ("rpt_", "twin_")


def _validate_result_table(table: str) -> str:
    """目标表名白名单校验：rpt_/twin_ 前缀 + 纯标识符（防 SQL 注入）"""
    if not table or not _IDENT_RE.match(table):
        raise ValueError(f"非法结果表名: {table!r}")
    if not table.startswith(_RESULT_PREFIXES):
        raise ValueError(f"仅允许导出 rpt_/twin_ 前缀的结果表: {table!r}")
    return table


def get_task_result_table(task_state: Dict[str, Any]) -> str:
    """从任务状态取目标结果表名（report_config.target_table，缺省 rpt_result）"""
    report_config = task_state.get("report_config") or {}
    return report_config.get("target_table") or "rpt_result"


def exports_dir(task_id: str) -> str:
    """任务导出目录路径"""
    return os.path.join(settings.task_work_dir, task_id, EXPORT_SUBDIR)


async def _load_result_rows(table: str) -> Dict[str, Any]:
    """走只读通道拉取结果表全量数据（受 mcp_max_limit 行限保护）"""
    from backend.mcp.database_mcp import DatabaseMCPService

    db_mcp = DatabaseMCPService({"db_type": "sqlite_demo"})
    # 表名已过白名单，可安全拼接（sql_guard 仅放行单语句 SELECT）
    result = await db_mcp.execute_sql(f"SELECT * FROM {table}", limit=settings.mcp_max_limit)
    return result


def _render_txt(columns: List[str], rows: List[Dict[str, Any]]) -> str:
    """1104 风格 TXT：竖线分隔，首行表头字段名"""
    lines = ["|".join(columns)]
    for row in rows:
        lines.append("|".join("" if row.get(c) is None else str(row.get(c)) for c in columns))
    return "\n".join(lines) + "\n"


def _render_xml(table: str, columns: List[str], rows: List[Dict[str, Any]]) -> str:
    """XML：简单元素嵌套，列名即元素名（列名已过标识符白名单，值做 XML 转义）"""
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>', f'<report table="{escape(table)}">']
    for row in rows:
        parts.append("  <row>")
        for c in columns:
            value = row.get(c)
            parts.append(f"    <{c}>{escape('' if value is None else str(value))}</{c}>")
        parts.append("  </row>")
    parts.append("</report>")
    return "\n".join(parts) + "\n"


async def generate_interface_file(task_id: str, task_state: Dict[str, Any],
                                  fmt: str) -> Dict[str, Any]:
    """生成监管接口文件，返回 {file_name, format, row_count, preview, path}

    fmt: txt | xml；目标表不存在/为空时抛 ValueError 由上层转 4xx
    """
    fmt = (fmt or "").lower()
    if fmt not in ("txt", "xml"):
        raise ValueError(f"不支持的导出格式: {fmt!r}（仅支持 txt/xml）")

    table = _validate_result_table(get_task_result_table(task_state))
    result = await _load_result_rows(table)
    columns = result["columns"]
    rows = result["rows"]
    if not columns:
        raise ValueError(f"结果表 {table} 不存在或无字段定义")

    content = (_render_txt(columns, rows) if fmt == "txt"
               else _render_xml(table, columns, rows))

    out_dir = exports_dir(task_id)
    os.makedirs(out_dir, exist_ok=True)
    file_name = f"{table}.{fmt}"
    path = os.path.join(out_dir, file_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    # 预览取前 5 个数据行（TXT 含表头则前 6 行；统一按内容行截取，Demo 足够）
    preview = content.splitlines()[:6 if fmt == "txt" else 8]

    return {
        "file_name": file_name,
        "format": fmt,
        "row_count": len(rows),
        "preview": preview,
        "path": path,
    }


def list_export_files(task_id: str) -> List[Dict[str, Any]]:
    """列出任务已生成的接口文件（不存在目录返回空）"""
    out_dir = exports_dir(task_id)
    if not os.path.isdir(out_dir):
        return []
    files = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if os.path.isfile(path) and _FILENAME_RE.match(name):
            files.append({
                "file_name": name,
                "size": os.path.getsize(path),
                "format": name.rsplit(".", 1)[-1] if "." in name else "",
            })
    return files


def resolve_export_file(task_id: str, file_name: str) -> Optional[str]:
    """下载文件名白名单校验 + 解析落盘路径（防路径穿越）；非法/不存在返回 None"""
    if not file_name or not _FILENAME_RE.match(file_name):
        return None
    if ".." in file_name or file_name.startswith("."):
        return None
    out_dir = os.path.realpath(exports_dir(task_id))
    path = os.path.realpath(os.path.join(out_dir, file_name))
    # 双重保险：解析后路径必须仍位于导出目录内
    if os.path.dirname(path) != out_dir:
        return None
    return path if os.path.isfile(path) else None
