"""camera.* 处理器：参数翻译、租约、二进制回传。

这几条是"云端点了云台真的会转"的最小证据链：
命令名 → 后端动作名、速度档位 → 0–1、会话 → 租约、快照 → 二进制通道。
中间任何一环错位，表现都是"点了没反应"，而日志里看不出问题。
"""

from __future__ import annotations

import pytest

from app.cloud_agent.errors import AgentCommandError
from app.cloud_agent.handlers import HandlerContext, LeaseKeeper, dispatch
from app.cloud_agent.media import MediaError
from app.cloud_agent.protocol import CommandName, ErrorCode

pytestmark = pytest.mark.no_database


class FakeBackend:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.leases: list[str] = []
        self.released: list[str] = []
        self.snapshot_calls: list[str | None] = []
        self.preset_response = {"items": [{"presetId": 1, "name": "P1"}], "cameraId": "VAR-1"}

    async def acquire_lease(self, holder: str, mode: str = "manual_remote") -> dict:
        self.leases.append(holder)
        return {"leaseId": f"lease-{len(self.leases)}", "mode": mode}

    async def release_lease(self, lease_id: str, reason: str = "released") -> None:
        self.released.append(lease_id)

    async def command(self, *, action: str, payload: dict, lease_id: str, idempotency_key: str, trace_id: str, target: str = "camera") -> dict:
        self.commands.append(
            {
                "action": action,
                "payload": payload,
                "lease_id": lease_id,
                "idempotency_key": idempotency_key,
                "trace_id": trace_id,
                "target": target,
            }
        )
        return {"state": "succeeded"}

    async def presets(self, camera_id: str | None = None) -> dict:
        return self.preset_response

    async def snapshot(self, camera_id: str | None = None) -> tuple[bytes, dict]:
        self.snapshot_calls.append(camera_id)
        return b"\xff\xd8jpeg-bytes", {"captured_at": "2026-11-16T07:00:00Z", "size": 12}


class FakeMedia:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, str]] = []
        self.stopped: list[str] = []

    async def start(self, session_id: str, camera_id: str, profile: str) -> dict:
        self.started.append((session_id, camera_id, profile))
        return {"codec": "avc1.64001F", "size": [640, 360], "profile": profile}

    async def stop(self, session_id: str, reason: str = "stopped") -> bool:
        self.stopped.append(session_id)
        return True


class FailingMedia(FakeMedia):
    async def start(self, session_id: str, camera_id: str, profile: str) -> dict:
        raise MediaError("stream_disabled", "未配置 RTSP 地址")


class Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []

    async def __call__(self, request_id: str, image: bytes, meta: dict) -> None:
        self.calls.append((request_id, image, meta))


def make_context(media: FakeMedia | None = None) -> tuple[HandlerContext, FakeBackend, FakeMedia, Sink]:
    backend = FakeBackend()
    media = media or FakeMedia()
    sink = Sink()
    ctx = HandlerContext(
        sandbox=None,  # type: ignore[arg-type] - camera.* 不碰文件系统
        uploads=None,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        media=media,  # type: ignore[arg-type]
        leases=LeaseKeeper(backend),  # type: ignore[arg-type]
        snapshots=sink,
    )
    return ctx, backend, media, sink


def ptz_payload(**overrides) -> dict:
    payload = {
        "command_id": "c-1",
        "session_id": "ctl-1",
        "sequence": 1,
        "action": "start",
        "direct": "left",
        "speed": 4,
    }
    payload.update(overrides)
    return payload


# --- 云台 ---------------------------------------------------------------


async def test_ptz_maps_speed_and_uses_a_lease():
    ctx, backend, _, _ = make_context()
    result = await dispatch(CommandName.CAMERA_PTZ, ptz_payload(speed=7), ctx)

    assert result["direct"] == "left"
    assert result["state"] == "succeeded"
    assert backend.leases == ["cloud:ctl-1"]           # 会话绑租约
    command = backend.commands[0]
    assert command["action"] == "camera.ptz"           # 驱动侧动作名
    assert command["payload"] == {"direct": "left", "speed": 1.0}
    assert command["lease_id"] == "lease-1"


async def test_ptz_stop_forces_stop_direction():
    """松开时前端可能只发 action=stop，方向字段还是上一次的——必须覆盖成 stop。"""
    ctx, backend, _, _ = make_context()

    await dispatch(CommandName.CAMERA_PTZ, ptz_payload(action="stop", direct="left"), ctx)

    assert backend.commands[0]["payload"]["direct"] == "stop"


async def test_ptz_speed_is_clamped():
    ctx, backend, _, _ = make_context()

    await dispatch(CommandName.CAMERA_PTZ, ptz_payload(speed=99), ctx)
    await dispatch(
        CommandName.CAMERA_PTZ, ptz_payload(sequence=2, speed=-3, command_id="c-2"), ctx
    )

    assert backend.commands[0]["payload"]["speed"] == 1.0
    assert backend.commands[1]["payload"]["speed"] == 0.143   # 1/7


async def test_ptz_rejects_unknown_direction():
    ctx, backend, _, _ = make_context()

    with pytest.raises(AgentCommandError) as raised:
        await dispatch(CommandName.CAMERA_PTZ, ptz_payload(direct="sideways"), ctx)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENTS
    assert backend.commands == []


async def test_same_command_id_is_executed_once():
    """弱网重发：第二次必须回放结果，不能让云台转两下。"""
    ctx, backend, _, _ = make_context()

    first = await dispatch(CommandName.CAMERA_PTZ, ptz_payload(), ctx)
    second = await dispatch(CommandName.CAMERA_PTZ, ptz_payload(), ctx)

    assert second.get("replayed") is True
    assert second["direct"] == first["direct"]
    assert len(backend.commands) == 1


async def test_expired_command_is_not_executed():
    ctx, backend, _, _ = make_context()

    with pytest.raises(AgentCommandError) as raised:
        await dispatch(CommandName.CAMERA_PTZ, ptz_payload(deadline=1.0), ctx)

    assert raised.value.code is ErrorCode.EXPIRED
    assert backend.commands == []


async def test_stale_sequence_is_dropped():
    ctx, backend, _, _ = make_context()

    await dispatch(CommandName.CAMERA_PTZ, ptz_payload(sequence=5), ctx)
    with pytest.raises(AgentCommandError) as raised:
        await dispatch(
            CommandName.CAMERA_PTZ, ptz_payload(sequence=4, command_id="c-2"), ctx
        )

    assert raised.value.code is ErrorCode.STALE
    assert len(backend.commands) == 1


# --- 预置点 -------------------------------------------------------------


async def test_preset_list_passes_through():
    ctx, _, _, _ = make_context()

    result = await dispatch(CommandName.CAMERA_PRESET_LIST, {}, ctx)

    assert result["items"] == [{"presetId": 1, "name": "P1"}]
    assert result["cameraId"] == "VAR-1"


async def test_preset_goto_uses_driver_action_name():
    """链路上的命令是 camera.preset.goto，驱动里的动作是 camera.goto_preset。"""
    ctx, backend, _, _ = make_context()

    result = await dispatch(
        CommandName.CAMERA_PRESET_GOTO,
        {"command_id": "c-3", "session_id": "ctl-1", "sequence": 2, "preset": "3"},
        ctx,
    )

    assert result["preset"] == 3
    assert backend.commands[0]["action"] == "camera.goto_preset"
    assert backend.commands[0]["payload"] == {"preset": 3}


async def test_preset_goto_rejects_bad_preset():
    ctx, backend, _, _ = make_context()

    with pytest.raises(AgentCommandError) as raised:
        await dispatch(
            CommandName.CAMERA_PRESET_GOTO,
            {"command_id": "c-4", "session_id": "ctl-1", "preset": "P3"},
            ctx,
        )

    assert raised.value.code is ErrorCode.INVALID_ARGUMENTS
    assert backend.commands == []


# --- 媒体会话 -----------------------------------------------------------


async def test_stream_start_passes_profile_and_camera():
    ctx, _, media, _ = make_context()

    result = await dispatch(
        CommandName.CAMERA_STREAM_START,
        {"command_id": "c-5", "session_id": "med-1", "camera_id": "VAR-1", "profile": "low"},
        ctx,
    )

    assert media.started == [("med-1", "VAR-1", "low")]
    assert result["codec"] == "avc1.64001F"


async def test_default_camera_alias_is_normalised_to_empty():
    """云端用 "default" 表示"那台球形相机"，但本地相机目录里没有这个编号。

    透传会得到 unknown_camera，本地预览 WS 直接被 1008 关闭——表现是页面
    永远停在"等待画面"，而 agent 日志里只有一条 error 帧。
    """
    ctx, _, media, _ = make_context()

    await dispatch(
        CommandName.CAMERA_STREAM_START,
        {"command_id": "c-20", "session_id": "med-9", "camera_id": "default"},
        ctx,
    )

    assert media.started == [("med-9", "", "low")]


async def test_default_camera_alias_is_normalised_to_empty():
    """云端用 "default" 表示"那台球形相机"，但本地相机目录里没有这个编号。

    透传会得到 unknown_camera，本地预览 WS 直接被 1008 关闭——表现是页面
    永远停在"等待画面"，而 agent 日志里只有一条 error 帧。
    """
    ctx, _, media, _ = make_context()

    await dispatch(
        CommandName.CAMERA_STREAM_START,
        {"command_id": "c-20", "session_id": "med-9", "camera_id": "default"},
        ctx,
    )

    assert media.started == [("med-9", "", "low")]


async def test_stream_start_requires_session():
    ctx, _, media, _ = make_context()

    with pytest.raises(AgentCommandError) as raised:
        await dispatch(CommandName.CAMERA_STREAM_START, {"command_id": "c-6"}, ctx)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENTS
    assert media.started == []


async def test_stream_start_failure_maps_to_stream_disabled():
    ctx, backend, _, _ = make_context(media=FailingMedia())

    with pytest.raises(AgentCommandError) as raised:
        await dispatch(
            CommandName.CAMERA_STREAM_START,
            {"command_id": "c-7", "session_id": "med-2"},
            ctx,
        )

    assert raised.value.code is ErrorCode.STREAM_DISABLED


async def test_stream_stop_releases_the_lease():
    """会话结束必须交还控制权，否则本地人要等租约过期才能操作。"""
    ctx, backend, media, _ = make_context()
    await dispatch(
        CommandName.CAMERA_STREAM_START,
        {"command_id": "c-8", "session_id": "med-3"},
        ctx,
    )
    # 先真的控一次，才会真的持有租约
    await dispatch(CommandName.CAMERA_PTZ, ptz_payload(session_id="med-3", command_id="c-9"), ctx)

    await dispatch(
        CommandName.CAMERA_STREAM_STOP,
        {"command_id": "c-10", "session_id": "med-3"},
        ctx,
    )

    assert media.stopped == ["med-3"]
    assert backend.released == ["lease-1"]


# --- 快照 ---------------------------------------------------------------


async def test_snapshot_sends_binary_and_returns_only_metadata():
    ctx, backend, _, sink = make_context()

    result = await dispatch(
        CommandName.CAMERA_SNAPSHOT,
        {"command_id": "c-11", "session_id": "ctl-1", "camera_id": "VAR-1"},
        ctx,
    )

    assert backend.snapshot_calls == ["VAR-1"]
    assert sink.calls and sink.calls[0][1] == b"\xff\xd8jpeg-bytes"
    assert result["size"] == len(b"\xff\xd8jpeg-bytes")
    # 图像本体绝不能出现在命令回包里（256KB 单帧上限 + base64 膨胀）
    assert "image" not in result and "jpeg" not in result
