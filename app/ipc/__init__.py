"""机器人进程 IPC 协议层（Unix Domain Socket JSON Lines）。

client 对端以稳定错误码拒绝时抛出 JsonSocketError（对齐旧 Java PeerRejectedException）。
"""

from .json_socket import JsonSocketClient, JsonSocketError, JsonSocketServer

__all__ = ["JsonSocketClient", "JsonSocketError", "JsonSocketServer"]
