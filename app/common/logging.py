"""日志配置。

统一使用标准库 logging；生产环境由 systemd/journald 收集，日志不写敏感凭据。
"""

import logging

from app.common.config import get_settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging() -> None:
    """按环境设置根日志级别；在应用工厂中调用一次。"""
    level = logging.DEBUG if get_settings().environment == "dev" else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
