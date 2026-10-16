"""IPC 稳定错误码：跨进程拒绝语义的唯一事实来源。

历史实现把 Python 异常类名当作错误码回传（``type(error).__name__``，
如 "KeyError"/"FileNotFoundError"），对端一旦重构类名就静默破坏线上契约。
本模块定义稳定错误码；服务端异常经 :func:`ipc_error_code` 归一化后回传，
客户端/网关层沿用同一张码表解释。

兼容策略：旧类名（KeyError / FileNotFoundError）在滚动升级期间仍可能出现在
线上（旧版五进程 + 新版后端混布），消费侧用 :func:`is_not_found_code` 同时
接受新码 ``NOT_FOUND`` 与旧类名。
"""

from __future__ import annotations


class IpcErrorCode:
    """跨进程稳定错误码（对齐后端错误码表的大写下划线风格）。"""

    #: 对端找不到请求所指向的资源（任务不存在、图片不存在等）。
    NOT_FOUND = "NOT_FOUND"
    #: 请求载荷缺失字段 / 类型非法 / 不支持的操作。
    INVALID_REQUEST = "INVALID_REQUEST"
    #: 请求的 operation 不被对端实现。
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    #: 对端显式拒绝（业务规则冲突等，语义同旧 peer_rejected）。
    PEER_REJECTED = "PEER_REJECTED"
    #: 对端未归类内部错误。
    INTERNAL = "INTERNAL_ERROR"


#: 旧线协议中承担 NOT_FOUND 语义的异常类名（仅用于兼容旧对端）。
LEGACY_NOT_FOUND_CODES = ("KeyError", "FileNotFoundError")

_EXCEPTION_CODE_MAP: tuple[tuple[type[BaseException], str], ...] = (
    (KeyError, IpcErrorCode.NOT_FOUND),
    (FileNotFoundError, IpcErrorCode.NOT_FOUND),
    (LookupError, IpcErrorCode.NOT_FOUND),
    (ValueError, IpcErrorCode.INVALID_REQUEST),
    (TypeError, IpcErrorCode.INVALID_REQUEST),
    (ArithmeticError, IpcErrorCode.INVALID_REQUEST),
)


def ipc_error_code(error: BaseException) -> str:
    """把服务端异常归一化为稳定错误码（未归类异常按内部错误处理）。"""
    for exception_type, code in _EXCEPTION_CODE_MAP:
        if isinstance(error, exception_type):
            return code
    return IpcErrorCode.INTERNAL


def is_not_found_code(code: str) -> bool:
    """判断错误码是否表达 NOT_FOUND（含旧异常类名兼容）。"""
    return code == IpcErrorCode.NOT_FOUND or code in LEGACY_NOT_FOUND_CODES
