"""
全局日志配置
- stdlib logging，格式含 trace_id（contextvars 注入，未在请求上下文时为 "-"）
- 控制台输出；日志级别由 LOG_LEVEL 环境变量控制（默认 INFO）
- 同时写入 data/logs/platform.log，便于演示时查日志文件
"""

import logging
import os
import sys
from contextvars import ContextVar

# 请求级 trace_id 上下文变量（中间件写入，日志 Filter 读取）
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="-")

_CONFIGURED = False


class TraceIdFilter(logging.Filter):
    """把当前上下文的 trace_id 注入每条日志记录"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_ctx.get("-")
        return True


def setup_logging():
    """初始化全局 logging（幂等）"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s [%(levelname)s] [trace:%(trace_id)s] %(name)s: %(message)s"

    root = logging.getLogger()
    root.setLevel(level)

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt))
    console.addFilter(TraceIdFilter())
    root.addHandler(console)

    # 文件
    log_dir = os.environ.get("LOG_DIR", "./data/logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(os.path.join(log_dir, "platform.log"), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt))
        file_handler.addFilter(TraceIdFilter())
        root.addHandler(file_handler)
    except OSError:
        root.warning("日志目录不可写，仅输出控制台: %s", log_dir)

    # 降噪：第三方库
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger"""
    setup_logging()
    return logging.getLogger(name)
