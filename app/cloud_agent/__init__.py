"""机器人工控机上的云端接入进程：只读取件、主动外连、不监听入站 TCP。

与 monitor 仓库里的 `robot-monitor-agent` 是同一个能力面的超集：
线上协议、沙箱、上传器都保持一致；新增的是本地 UDS 控制面与守护进程形态。
"""

__version__ = "0.1.0"

from .sandbox import Sandbox, SandboxError, is_within

__all__ = ["Sandbox", "SandboxError", "__version__", "is_within"]
