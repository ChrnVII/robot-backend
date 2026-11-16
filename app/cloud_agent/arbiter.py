"""命令仲裁：去重、序号、过期判定。

云端可能重发（弱网下必然重发），而云台动作**不幂等**——重发一次就是转两下。
所以机器人侧必须自己挡住重复，不能指望链路只送一次。

四条判定，顺序不能换（对应方案 §5.4）：

1. **deadline 过了就不执行。** 恢复瞬间一股脑到齐的旧方向会让球机"抽搐"，
   而用户早就松手了。
2. **command_id 见过就直接回放上次结果。** 不重复执行，但要回同一个答案——
   "我没收到结果"和"你没执行"是两件事。
3. **sequence 不新就丢弃，并回 stale。** 左右两条乱序到达时，只有这条能保证
   球机最后停在正确的方向。stale 不是错误，是"这条已经被更新的指令替代了"。
4. **执行并记住结果。**

序号按 `(session_id, target)` 分组：两个会话各自递增，互不影响。
去重表按 TTL 清理，不是永久增长——这条表只在"重发窗口"内有意义。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEDUP_TTL_SECONDS = 30.0
MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class Envelope:
    """命令信封（方案 §5.1）。字段全部可选：老云端不发，老 agent 也不认。"""

    command_id: str
    session_id: str
    sequence: int
    deadline: float | None
    idempotency_key: str
    trace_id: str
    target: str = "camera"

    @property
    def group(self) -> tuple[str, str]:
        """序号分组：(会话, 目标)。两个会话各自递增，互不干扰。"""
        return (self.session_id, self.target)


def parse_envelope(payload: dict[str, Any], fallback_id: str) -> Envelope:
    """从 payload 里读信封。缺字段一律给安全默认值，绝不因为缺字段抛错。

    缺 `deadline` 表示"不过期"（老云端），缺 `sequence` 表示"不参与新旧比较"。
    这两种情况只能退化成没有保护，而不是拒绝命令——否则一升级云端，
    老机器人就全哑了。
    """

    def _float(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    def _int(value: Any) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return 0

    command_id = payload.get("command_id")
    session_id = payload.get("session_id")
    target = payload.get("target")
    return Envelope(
        command_id=command_id if isinstance(command_id, str) and command_id else fallback_id,
        session_id=session_id if isinstance(session_id, str) and session_id else "",
        sequence=_int(payload.get("sequence")),
        deadline=_float(payload.get("deadline")),
        idempotency_key=str(payload.get("idempotency_key") or command_id or fallback_id),
        trace_id=str(payload.get("trace_id") or ""),
        target=target if isinstance(target, str) and target else "camera",
    )


@dataclass(slots=True)
class _Entry:
    result: dict[str, Any]
    expires_at: float


@dataclass
class CommandArbiter:
    dedup_ttl: float = DEDUP_TTL_SECONDS
    max_entries: int = MAX_ENTRIES
    #: 便于测试注入假时钟。
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        self._results: dict[str, _Entry] = {}
        self._sequences: dict[tuple[str, str], int] = {}

    # --- 判定 -----------------------------------------------------------

    def is_expired(self, envelope: Envelope) -> bool:
        if envelope.deadline is None:
            return False
        return self.clock() > envelope.deadline

    def replay(self, envelope: Envelope) -> dict[str, Any] | None:
        entry = self._results.get(envelope.command_id)
        if entry is None:
            return None
        if entry.expires_at <= self.clock():
            self._results.pop(envelope.command_id, None)
            return None
        return entry.result

    def is_stale(self, envelope: Envelope) -> bool:
        """序号不比已执行的新 → 丢弃。

        `sequence == 0` 表示"这条命令不参与顺序"（老云端或 stop 之前的版本），
        直接放行：宁可多执行一次，也不要因为字段缺失把用户的停止命令丢掉。
        """
        if envelope.sequence <= 0:
            return False
        last = self._sequences.get(envelope.group)
        return last is not None and envelope.sequence <= last

    def accept(self, envelope: Envelope) -> None:
        """执行成功后推进该分组的序号。"""
        if envelope.sequence > 0:
            self._sequences[envelope.group] = envelope.sequence

    def remember(self, envelope: Envelope, result: dict[str, Any]) -> None:
        now = self.clock()
        self._results[envelope.command_id] = _Entry(result=result, expires_at=now + self.dedup_ttl)
        if len(self._results) > self.max_entries:
            self._evict(now)

    # --- 维护 -----------------------------------------------------------

    def _evict(self, now: float) -> None:
        """先清过期，再按过期时间从早到晚砍到上限。"""
        for key in [k for k, entry in self._results.items() if entry.expires_at <= now]:
            self._results.pop(key, None)
        overflow = len(self._results) - self.max_entries
        if overflow <= 0:
            return
        for key, _ in sorted(self._results.items(), key=lambda item: item[1].expires_at)[:overflow]:
            self._results.pop(key, None)

    def stats(self) -> dict[str, int]:
        return {
            "dedup_entries": len(self._results),
            "sequence_groups": len(self._sequences),
        }

    def forget_sessions(self, session_id: str) -> None:
        """会话结束后清掉它的序号，避免同一 session 复用时被旧序号挡住。"""
        for key in [k for k in self._sequences if k[0] == session_id]:
            self._sequences.pop(key, None)
