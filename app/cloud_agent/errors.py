"""agent 侧的错误类型。

沙箱的 `SandboxError` 只表示"路径不允许"，和"命令参数不合法"是两回事，
错误码必须分开——前端要靠 `forbidden` 判断是路径问题、靠 `invalid_arguments`
判断是调用方 bug。
"""

from __future__ import annotations

from .protocol import ErrorCode


class AgentCommandError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class UploadCancelled(Exception):
    """A 侧已经回收了这个 upload。不需要再回报失败——它比我们更清楚。"""


class UploadFailed(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")
