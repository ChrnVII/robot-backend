"""命令仲裁：去重、序号、过期。

这三条是"点云台没反应"和"球机自己抽搐"的分界线：

- 不去重 → 弱网重发让云台转两下（动作不幂等）；
- 不比序号 → 左右两条乱序到达时，球机停在错误的方向；
- 不判过期 → 断网恢复瞬间一股脑执行积压的旧方向。

时间用假时钟，避免测试里出现 sleep。
"""

from __future__ import annotations

import pytest

from app.cloud_agent.arbiter import CommandArbiter, parse_envelope

pytestmark = pytest.mark.no_database


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def envelope(payload: dict, fallback: str = "fallback") -> object:
    return parse_envelope(payload, fallback)


def make_arbiter() -> tuple[CommandArbiter, Clock]:
    clock = Clock()
    return CommandArbiter(clock=clock), clock


# --- 信封解析 -----------------------------------------------------------


def test_envelope_defaults_are_safe_for_old_cloud():
    """老云端不发信封：不能抛错，只能退化成"没有保护"。"""
    env = envelope({}, "req-1")

    assert env.command_id == "req-1"
    assert env.session_id == ""
    assert env.sequence == 0
    assert env.deadline is None
    assert env.idempotency_key == "req-1"


def test_envelope_reads_all_fields():
    env = envelope(
        {
            "command_id": "c-1",
            "session_id": "ctl-1",
            "sequence": 7,
            "deadline": 1761033600.5,
            "idempotency_key": "ptz-left-7",
            "trace_id": "tr-1",
        }
    )

    assert (env.command_id, env.session_id, env.sequence) == ("c-1", "ctl-1", 7)
    assert env.deadline == 1761033600.5
    assert env.idempotency_key == "ptz-left-7"
    assert env.trace_id == "tr-1"


def test_envelope_ignores_wrong_types():
    """字段类型不对时按"没给"处理，而不是 500。"""
    env = envelope({"command_id": 42, "sequence": "3", "deadline": "soon"})

    assert env.command_id == "fallback"
    assert env.sequence == 0
    assert env.deadline is None


# --- 过期 ---------------------------------------------------------------


def test_expired_command_is_refused():
    arbiter, clock = make_arbiter()
    env = envelope({"command_id": "c-1", "deadline": clock.now - 0.1})

    assert arbiter.is_expired(env) is True


def test_command_without_deadline_never_expires():
    arbiter, _ = make_arbiter()
    assert arbiter.is_expired(envelope({"command_id": "c-1"})) is False


# --- 去重 ---------------------------------------------------------------


def test_replay_returns_the_previous_result():
    arbiter, clock = make_arbiter()
    env = envelope({"command_id": "c-1"})

    arbiter.remember(env, {"direct": "left", "state": "succeeded"})
    clock.advance(1.0)

    assert arbiter.replay(env) == {"direct": "left", "state": "succeeded"}


def test_replay_expires_after_ttl():
    """重发窗口之外不该继续回放：30s 后同一个 command_id 是"新的一次操作"。"""
    arbiter, clock = make_arbiter()
    env = envelope({"command_id": "c-1"})
    arbiter.remember(env, {"ok": True})

    clock.advance(arbiter.dedup_ttl + 0.1)

    assert arbiter.replay(env) is None


def test_dedup_table_is_bounded():
    arbiter, _ = make_arbiter()
    arbiter.max_entries = 5

    for index in range(20):
        arbiter.remember(envelope({"command_id": f"c-{index}"}), {"i": index})

    assert len(arbiter._results) <= 5


# --- 序号 ---------------------------------------------------------------


def test_older_sequence_is_stale():
    arbiter, _ = make_arbiter()
    arbiter.accept(envelope({"session_id": "ctl-1", "sequence": 10}))

    assert arbiter.is_stale(envelope({"session_id": "ctl-1", "sequence": 9})) is True
    assert arbiter.is_stale(envelope({"session_id": "ctl-1", "sequence": 10})) is True
    assert arbiter.is_stale(envelope({"session_id": "ctl-1", "sequence": 11})) is False


def test_sequences_are_per_session():
    arbiter, _ = make_arbiter()
    arbiter.accept(envelope({"session_id": "ctl-1", "sequence": 100}))

    # 另一个会话从 1 开始，不该被上一个会话的序号挡住
    assert arbiter.is_stale(envelope({"session_id": "ctl-2", "sequence": 1})) is False


def test_zero_sequence_always_passes():
    """没有序号的命令（老云端 / 停止命令）不能被挡——宁可多执行一次。"""
    arbiter, _ = make_arbiter()
    arbiter.accept(envelope({"session_id": "ctl-1", "sequence": 50}))

    assert arbiter.is_stale(envelope({"session_id": "ctl-1", "sequence": 0})) is False


def test_forget_sessions_clears_only_that_session():
    arbiter, _ = make_arbiter()
    arbiter.accept(envelope({"session_id": "ctl-1", "sequence": 5}))
    arbiter.accept(envelope({"session_id": "ctl-2", "sequence": 5}))

    arbiter.forget_sessions("ctl-1")

    assert arbiter.is_stale(envelope({"session_id": "ctl-1", "sequence": 1})) is False
    assert arbiter.is_stale(envelope({"session_id": "ctl-2", "sequence": 1})) is True


@pytest.mark.parametrize("sequence", [1, 2, 99])
def test_stats_reports_groups(sequence):
    arbiter, _ = make_arbiter()
    arbiter.accept(envelope({"session_id": "ctl-1", "sequence": sequence}))

    assert arbiter.stats()["sequence_groups"] == 1
