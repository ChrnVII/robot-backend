"""路径沙箱：把 B 的文件访问锁死在一个根目录里。

目标平台是 Linux（生产与测试一致）。不再保留 Windows 分支：盘符、UNC、
大小写不敏感、反斜杠分隔符这些概念在 POSIX 上都不存在，留着只会让人误以为
需要兼容它们。

三个决定：

1. **协议里传相对路径，不传绝对路径。**
   A 不需要知道 B 的根目录在哪，也不需要理解路径语义。所有路径在线上都是
   相对路径（用 `/` 分隔），B 自己拼到根目录上。这样 A 侧根本不存在路径穿越面。

2. **先 resolve() 再判断包含关系，顺序不能反。**
   `~/share/../../.ssh` 能通过字符串前缀比较然后再逃出去；根目录里的软链接
   也可能指向磁盘任意位置。`resolve()` 会把 `..` 和软链接都展开成真实路径，
   展开之后再比前缀才是可靠的。Linux 上建软链接不需要任何特权，这一条是必须的。

3. **拒绝任何 `..`，哪怕它最终没有逃出根目录。**
   `a/../b` 的结果就是 `b`，但没有理由接受这种写法。一个含 `..` 的请求只可能
   是前端 bug 或者攻击，两种情况都该拒绝并留日志，而不是猜它想干什么。
"""

from __future__ import annotations

import os
from pathlib import Path

SEP = "/"                 # 协议分隔符，固定为 `/`，与运行平台无关
MAX_REL_DEPTH = 128

# POSIX 上单个文件名最长 255 字节，这里给个整体上限防止构造超长路径
MAX_REL_LENGTH = 4096


class SandboxError(Exception):
    """路径被拒绝。调用方应转成 ErrorCode.FORBIDDEN，而不是 500。"""


def is_within(child: Path | str, parent: Path | str) -> bool:
    """child 是否等于 parent 或位于其内部。

    必须是公开函数：这是唯一挡住软链接越界的那道判断，值得被单独测。

    注意这里用 `os.sep` 而不是协议分隔符：比的是已经 resolve 过的真实本地路径，
    该用本地分隔符。Linux 上 `os.sep` 就是 `/`，与协议层一致。
    """
    c = os.path.abspath(str(child))
    p = os.path.abspath(str(parent))
    if c == p:
        return True
    prefix = p if p.endswith(os.sep) else p + os.sep
    return c.startswith(prefix)


class Sandbox:
    """一个根目录，一个门卫。

    v1 只支持单根目录（对应"一台 B、一个共享目录"的部署）。将来要多根，
    在协议里加 root_id，不要在这里塞 list 然后让 rel 语义变得含糊。
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if root is None or str(root).strip() == "":
            raise SandboxError("root directory is not configured")

        expanded = Path(root).expanduser()
        try:
            resolved = expanded.resolve()
        except (OSError, RuntimeError) as exc:
            raise SandboxError(f"cannot resolve root {root!r}: {exc}") from exc

        if not resolved.is_dir():
            raise SandboxError(f"root is not a directory: {resolved}")

        self.root = resolved

    def normalize_rel(self, rel: str) -> str:
        """把外部传来的相对路径规范化。越界或畸形直接抛错，不做"尽力修复"。"""
        if not isinstance(rel, str):
            raise SandboxError("path must be a string")

        if "\x00" in rel:
            raise SandboxError("path contains a null byte")

        # 空串表示根目录本身
        if rel == "":
            return ""

        if rel.startswith(SEP):
            raise SandboxError("absolute paths are not accepted, send a path relative to root")

        if len(rel) > MAX_REL_LENGTH:
            raise SandboxError(f"path is too long (>{MAX_REL_LENGTH} chars)")

        # 只按 `/` 切。反斜杠在 POSIX 上是合法的文件名字符，不能当分隔符处理，
        # 否则一个名为 `a\b` 的文件会被错误地解析成两级目录。
        segments = [seg for seg in rel.split(SEP) if seg not in ("", ".")]

        if len(segments) > MAX_REL_DEPTH:
            raise SandboxError(f"path is too deep (>{MAX_REL_DEPTH} segments)")

        if any(seg == ".." for seg in segments):
            raise SandboxError("path traversal is not accepted")

        return SEP.join(segments)

    def resolve(self, rel: str) -> Path:
        """相对路径 → 校验过的绝对路径。"""
        clean = self.normalize_rel(rel)
        candidate = self.root if clean == "" else self.root / clean

        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            # 软链接环、悬空虚链、无法访问的网络挂载都会落到这里
            raise SandboxError(f"cannot resolve path {rel!r}: {exc}") from exc

        if not is_within(resolved, self.root):
            raise SandboxError(f"path escapes root: {rel!r}")

        return resolved

