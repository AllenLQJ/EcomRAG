"""应用日志只记录元数据；异常保留类型和位置，删除消息、源代码和局部变量。"""
import logging
import re
import threading
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

request_id = ContextVar("request_id", default="-")
_lock = threading.Lock()


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = request_id.get()
        return True


def log_exception(logger, exc):
    # 不把原异常放入 LogRecord：caplog、其他处理器也不能取得异常消息。
    locations = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        locations.append(f"{Path(code.co_filename).name}:{tb.tb_lineno}:{code.co_name}")
        tb = tb.tb_next
    name = re.sub(r"[^a-zA-Z0-9_]", "_", type(exc).__name__)[:100]
    sanitized = RuntimeError("exception details redacted")
    logger.exception("unhandled_exception type=%s stack=%s", name, " -> ".join(locations),
                     exc_info=(RuntimeError, sanitized, None))


def configure_logging(settings):
    """重复建 app 时替换本模块持有的处理器，保留 pytest/宿主已有处理器。"""
    root = logging.getLogger()
    with _lock:
        folder = Path(settings.log_dir)
        folder.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s")
        handlers = [TimedRotatingFileHandler(folder / "app.log", when="midnight", interval=1,
                                            backupCount=settings.log_backup_days, encoding="utf-8",
                                            utc=True, delay=True)]
        if settings.log_to_console:
            handlers.append(logging.StreamHandler())
        for handler in list(root.handlers):
            if getattr(handler, "_ecomrag", False):
                root.removeHandler(handler)
                handler.close()
        for handler in handlers:
            handler._ecomrag = True
            handler.setFormatter(formatter)
            handler.addFilter(RequestContextFilter())
            root.addHandler(handler)
        root.setLevel(settings.log_level)
        # 兼容 TestClient 使用 httpx 或 httpx2；请求行可能含查询参数。
        for name in ("httpx", "httpx2", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        for name in ("pymysql", "pymilvus", "grpc"):
            logging.getLogger(name).setLevel(logging.WARNING)
